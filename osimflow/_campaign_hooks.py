"""Shell hooks and webhook for Campaign (issue #1462 extraction).

Extracted from ``osimflow.campaign``: the ``--init-script`` /
``--finalize-script`` execution hooks (issue #108) and the
``--webhook-url`` completion callback (issue #283).  All functions are
pure with respect to the campaign — they receive the config, trace,
and executor name, and return nothing (hooks raise on failure per the
original contract; webhook delivery is best-effort).

Issue #1685: ``run_init_script`` and ``run_finalize_script`` now
honour ``cfg.init_script_timeout`` / ``cfg.finalize_script_timeout``
(defaults: 600 s).  On expiry the child process group is killed and
the init hook raises ``CampaignError`` (so the campaign aborts before
any step runs) while the finalize hook logs a warning and returns
(best-effort: the ``run.json`` rewrite, webhook, and ``cache.close()``
in ``Campaign.run``'s ``finally`` block must still run).
"""

import contextlib
import logging
import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Any

from .config import CampaignConfig
from .monitoring import RunTrace
from .webhook import WebhookClient

log = logging.getLogger("osimflow.campaign")

# Default timeouts (seconds) when ``cfg.init_script_timeout`` /
# ``cfg.finalize_script_timeout`` are unset.  Documented as a finite
# bound — a hung init script stuck on an NFS mount or a lock blocks
# ``Campaign.run()`` before any step runs; a hung finalize script blocks
# the ``finally`` block that writes ``run.json`` and fires the webhook
# (issue #1685).
_DEFAULT_INIT_SCRIPT_TIMEOUT_S: float = 600.0
_DEFAULT_FINALIZE_SCRIPT_TIMEOUT_S: float = 600.0


def _resolve_timeout(cfg: CampaignConfig, attr: str, default: float) -> float:
    """Return a positive timeout (seconds) for a hook subprocess.

    Honours the composed ``cfg.dag`` value when present; falls back to
    the legacy flat field (read-through ``__getattr__``) so callers
    that pre-date the new fields keep working.
    """
    raw = getattr(cfg, attr, None)
    if raw is None:
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{attr} must be a float, got {raw!r}") from exc
    if value <= 0:
        raise ValueError(f"{attr} must be > 0, got {value}")
    return value


def _run_hook_subprocess(
    *,
    script: Path,
    env: dict[str, str],
    timeout: float,
    label: str,
) -> subprocess.CompletedProcess[str]:
    """Spawn a hook subprocess with a wall-clock timeout and kill the
    process group on expiry (issue #1685).

    Uses ``Popen`` + ``start_new_session=True`` so we can reliably kill
    the child and any descendants on timeout via ``os.killpg`` — the
    simpler ``subprocess.run(timeout=...)`` is not guaranteed to kill
    the child on every Python version / platform combination.  Returns
    a ``CompletedProcess`` on success; raises ``subprocess.TimeoutExpired``
    (after killing the child) on expiry.
    """
    t0 = time.time()
    proc = subprocess.Popen(  # noqa: S603
        [str(script)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        # Best-effort kill of the entire process group (covers shell
        # scripts that spawn long-lived children themselves).  We do
        # this before logging so the child is gone before any other
        # operator-facing side effects.
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            # Race: the child already exited.  Fall back to a direct
            # kill so any orphaned descendants are reaped.
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        elapsed = time.time() - t0
        log.error(
            "%s script %r exceeded %.1fs timeout (killed pid %s after %.1fs)",
            label,
            str(script),
            timeout,
            proc.pid,
            elapsed,
        )
        raise
    return subprocess.CompletedProcess(
        args=[str(script)],
        returncode=proc.returncode,
        stdout=stdout,
        stderr=stderr,
    )


def hook_env(cfg: CampaignConfig, executor_name: str) -> dict[str, str]:
    """Build the environment dict for hook scripts."""
    base = dict(os.environ)
    base["OSIMFLOW_OUTDIR"] = str(cfg.outdir)
    base["OSIMFLOW_N_SAMPLES"] = str(cfg.n_samples)
    base["OSIMFLOW_EXECUTOR"] = executor_name
    base["OSIMFLOW_ALGORITHM"] = cfg.algorithm
    if cfg.shard_count is not None and cfg.shard_index is not None:
        base["OSIMFLOW_SHARD_COUNT"] = str(cfg.shard_count)
        base["OSIMFLOW_SHARD_INDEX"] = str(cfg.shard_index)
    if cfg.shard_start is not None and cfg.shard_end is not None:
        base["OSIMFLOW_SHARD_START"] = str(cfg.shard_start)
        base["OSIMFLOW_SHARD_END"] = str(cfg.shard_end)
    return base


def run_init_script(cfg: CampaignConfig, trace: RunTrace, executor_name: str) -> None:
    """Run the init script before the first campaign step.

    Honours ``cfg.init_script_timeout`` (default 600 s) — a hung init
    script (e.g. stuck on an NFS mount or a lock) is killed via
    ``SIGKILL`` on the child process group and a ``CampaignError`` is
    raised so the campaign aborts before any step runs (issue #1685).

    Raises ``subprocess.CalledProcessError`` if the script exits
    non-zero (preserved pre-#1685 contract).
    """
    script = cfg.init_script
    if script is None:
        return
    if not script.is_file():
        raise FileNotFoundError(f"Init script not found: {script!r}")
    timeout = _resolve_timeout(cfg, "init_script_timeout", _DEFAULT_INIT_SCRIPT_TIMEOUT_S)
    env = hook_env(cfg, executor_name)
    log.info("running init script: %s (timeout=%.1fs)", script, timeout)
    t0 = time.time()
    try:
        result = _run_hook_subprocess(
            script=script,
            env=env,
            timeout=timeout,
            label="init",
        )
    except subprocess.TimeoutExpired as exc:
        elapsed = time.time() - t0
        trace.init_script_duration_s = elapsed
        # Lazy import to avoid a top-level cycle (campaign → hooks →
        # campaign) and to keep _campaign_hooks importable from
        # contexts that don't pull the full Campaign surface.
        from .campaign import CampaignError  # noqa: PLC0415

        raise CampaignError(
            f"Init script {str(script)!r} exceeded {timeout:.1f}s timeout "
            f"(killed after {elapsed:.1f}s). Aborting campaign before any "
            "step runs (issue #1685)."
        ) from exc
    elapsed = time.time() - t0
    trace.init_script_duration_s = elapsed
    if result.stdout:
        for line in result.stdout.splitlines():
            log.info("init-script stdout: %s", line)
    if result.stderr:
        for line in result.stderr.splitlines():
            log.info("init-script stderr: %s", line)
    log.info("init script completed in %.2fs", elapsed)
    if result.returncode != 0:
        # Mirror ``subprocess.run(..., check=True)`` so the existing
        # CalledProcessError contract is preserved.
        raise subprocess.CalledProcessError(
            returncode=result.returncode,
            cmd=[str(script)],
            output=result.stdout,
            stderr=result.stderr,
        )


def run_finalize_script(
    cfg: CampaignConfig,
    trace: RunTrace,
    executor_name: str,
    status: str,
    duration_s: float,
) -> None:
    """Run the finalize script after the last campaign step.

    Honours ``cfg.finalize_script_timeout`` (default 600 s) — a hung
    finalize script is killed via ``SIGKILL`` on the child process
    group and the timeout is logged as a warning, but the function
    returns normally so the ``finally`` block in ``Campaign.run()``
    can still rewrite ``run.json``, fire the webhook, and
    ``cache.close()`` (issue #1685).

    Best-effort: a non-zero exit code is logged but does NOT raise.
    """
    script = cfg.finalize_script
    if script is None:
        return
    if not script.is_file():
        log.warning("finalize script not found: %s — skipping", script)
        return
    timeout = _resolve_timeout(cfg, "finalize_script_timeout", _DEFAULT_FINALIZE_SCRIPT_TIMEOUT_S)
    env = hook_env(cfg, executor_name)
    env["OSIMFLOW_STATUS"] = status
    env["OSIMFLOW_DURATION_S"] = f"{duration_s:.2f}"
    log.info("running finalize script: %s (timeout=%.1fs)", script, timeout)
    t0 = time.time()
    try:
        result = _run_hook_subprocess(
            script=script,
            env=env,
            timeout=timeout,
            label="finalize",
        )
    except subprocess.TimeoutExpired:
        elapsed = time.time() - t0
        trace.finalize_script_duration_s = elapsed
        log.warning(
            "finalize script %r exceeded %.1fs timeout (killed after %.1fs; "
            "best-effort — continuing so run.json / webhook / cache.close() "
            "still run in the Campaign.run() finally block; issue #1685)",
            str(script),
            timeout,
            elapsed,
        )
        return
    except Exception as exc:
        elapsed = time.time() - t0
        trace.finalize_script_duration_s = elapsed
        log.warning("finalize script error: %s (best-effort — continuing)", exc, exc_info=True)
        return
    elapsed = time.time() - t0
    trace.finalize_script_duration_s = elapsed
    if result.stdout:
        for line in result.stdout.splitlines():
            log.info("finalize-script stdout: %s", line)
    if result.stderr:
        for line in result.stderr.splitlines():
            log.info("finalize-script stderr: %s", line)
    if result.returncode != 0:
        log.warning(
            "finalize script exited %d (best-effort — continuing)",
            result.returncode,
        )
    else:
        log.info("finalize script completed in %.2fs", elapsed)


def maybe_fire_webhook(
    cfg: CampaignConfig,
    trace: RunTrace,
    campaign_status: str,
    elapsed_s: float,
) -> None:
    """Fire a webhook callback if ``cfg.webhook_url`` is configured (issue #283).

    Best-effort: delivery failures are logged but do not propagate.
    The webhook is sent after the GENERATE_BASIC_PLOTS step, in the
    ``finally`` block of ``run()``, so it fires regardless of success
    or failure — ``campaign_status`` will be ``"success"``,
    ``"failure"``, or ``"cancelled"``.
    """
    if not cfg.webhook_url:
        return

    n_succeeded = sum(1 for s in trace.per_sample if s.status == "ok")
    n_failed = sum(1 for s in trace.per_sample if s.status == "failed")

    client = WebhookClient(url=cfg.webhook_url)
    payload: Any = client.build_payload(
        campaign_id=trace.campaign_id,
        status=campaign_status,
        elapsed_s=elapsed_s,
        n_samples=cfg.n_samples,
        n_succeeded=n_succeeded,
        n_failed=n_failed,
        total_cost_usd=trace.total_cost_usd if trace.total_cost_usd > 0 else None,
        outdir=str(cfg.outdir),
    )

    log.info("firing webhook to %s (status=%s)", cfg.webhook_url, campaign_status)
    ok = client.deliver(payload)
    if not ok:
        log.warning(
            "webhook delivery to %s failed (campaign_status=%s)",
            cfg.webhook_url,
            campaign_status,
        )
