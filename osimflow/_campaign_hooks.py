"""Shell hooks and webhook for Campaign (issue #1462 extraction).

Extracted from ``osimflow.campaign``: the ``--init-script`` /
``--finalize-script`` execution hooks (issue #108) and the
``--webhook-url`` completion callback (issue #283).  All functions are
pure with respect to the campaign — they receive the config, trace,
and executor name, and return nothing (hooks raise on failure per the
original contract; webhook delivery is best-effort).
"""

import logging
import os
import subprocess
import time
from typing import Any

from .config import CampaignConfig
from .monitoring import RunTrace
from .webhook import WebhookClient

log = logging.getLogger("osimflow.campaign")


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

    Raises ``subprocess.CalledProcessError`` if the script exits
    non-zero, which aborts the campaign.
    """
    script = cfg.init_script
    if script is None:
        return
    if not script.is_file():
        raise FileNotFoundError(f"Init script not found: {script!r}")
    env = hook_env(cfg, executor_name)
    log.info("running init script: %s", script)
    t0 = time.time()
    result = subprocess.run(  # noqa: S603
        [str(script)],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    elapsed = time.time() - t0
    trace.init_script_duration_s = elapsed
    if result.stdout:
        for line in result.stdout.splitlines():
            log.info("init-script stdout: %s", line)
    if result.stderr:
        for line in result.stderr.splitlines():
            log.info("init-script stderr: %s", line)
    log.info("init script completed in %.2fs", elapsed)


def run_finalize_script(
    cfg: CampaignConfig,
    trace: RunTrace,
    executor_name: str,
    status: str,
    duration_s: float,
) -> None:
    """Run the finalize script after the last campaign step.

    Best-effort: a non-zero exit code is logged but does NOT raise.
    """
    script = cfg.finalize_script
    if script is None:
        return
    if not script.is_file():
        log.warning("finalize script not found: %s — skipping", script)
        return
    env = hook_env(cfg, executor_name)
    env["OSIMFLOW_STATUS"] = status
    env["OSIMFLOW_DURATION_S"] = f"{duration_s:.2f}"
    log.info("running finalize script: %s", script)
    t0 = time.time()
    try:
        result = subprocess.run(  # noqa: S603
            [str(script)],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
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
    except Exception as exc:
        elapsed = time.time() - t0
        trace.finalize_script_duration_s = elapsed
        log.warning("finalize script error: %s (best-effort — continuing)", exc, exc_info=True)


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
