"""Executor abstraction for OSimFlow campaigns.

A `BaseExecutor` is a thin wrapper that takes a Python callable and runs it
on some compute substrate (local thread, Slurm job, AWS Batch task, etc.)
and returns a handle. The handle exposes:

  * `.result(timeout=None)` — block until done, return the callable's return
    value or re-raise.
  * `.job_id` — a substrate-specific identifier (Slurm job ID, Batch task ARN,
    thread name, etc.) for log correlation.
  * `.done()` — non-blocking check.

This mirrors `submitit.Future`-style ergonomics intentionally: it is the
mental model the team will use, and the SlurmExecutor returns real
`submitit.Future` objects directly.
"""

import abc
import dataclasses
import json
import logging
import math
import os
import random
import re
import subprocess
import threading
import time
import warnings
from collections.abc import Callable, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from importlib.metadata import entry_points
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Optional, cast

from osimflow.executors.base import BaseExecutor, Handle
from osimflow.executors.azure_batch_executor import AzureBatchExecutor as AzureBatchExecutor
from osimflow.executors.dask_jobqueue_executor import DaskJobQueueExecutor as DaskJobQueueExecutor
from osimflow.executors.docker_swarm_executor import DockerSwarmExecutor
from osimflow.executors.google_batch_executor import GoogleBatchExecutor as GoogleBatchExecutor
from osimflow.executors.kubernetes_executor import KubernetesExecutor as KubernetesExecutor
from osimflow.executors.pbs_executor import PBSExecutor as PBSExecutor
from osimflow.executors.transport import (
    coerce_transport_mode,
    materialize_object_storage_result,
    resolve_result_for_callback,
)

log = logging.getLogger("osimflow.executors")

if TYPE_CHECKING:
    from osimflow.health import CheckResult

#: Entry-point group for third-party executor plug-ins (issue #432).
EXECUTOR_ENTRY_POINT_GROUP = "osimflow.executors"

__all__ = [
    "AWSBatchExecutor",
    "AzureBatchExecutor",
    "BaseExecutor",
    "DaskJobQueueExecutor",
    "DockerSwarmExecutor",
    "ExecutorRegistry",
    "GoogleBatchExecutor",
    "Handle",
    "KubernetesExecutor",
    "LocalExecutor",
    "NomadExecutor",
    "PBSExecutor",
    "SlurmExecutor",
]


# ---------------------------------------------------------------------------
# Per-step resource defaults (issue #39)
# ---------------------------------------------------------------------------
# Sensible defaults for each DAG step. Used by BaseExecutor.submit() when
# no explicit overrides are passed. See docs/resource-allocation.md for
# the rationale and tuning guidance.
#
# Keys match the step names used in osimflow/campaign.py.
# Values are dicts with {cpus, memory_mb, time_min}.
DEFAULT_STEP_RESOURCES: dict[str, dict[str, int]] = {
    "GENERATE_LHS_SAMPLES": {"cpus": 1, "memory_mb": 2048, "time_min": 5},
    "APPLY_PARAMETERS": {"cpus": 1, "memory_mb": 512, "time_min": 10},
    "RUN_OPENSTUDIO_SIM": {"cpus": 4, "memory_mb": 8192, "time_min": 240},
    "EXTRACT_KPIS": {"cpus": 1, "memory_mb": 2048, "time_min": 10},
    "AGGREGATE_RESULTS": {"cpus": 2, "memory_mb": 4096, "time_min": 15},
    "GENERATE_BASIC_PLOTS": {"cpus": 1, "memory_mb": 2048, "time_min": 10},
}


def get_step_resources(step_name: str) -> dict[str, int]:
    """Return resource defaults for a DAG step.

    Falls back to ``{"cpus": 1, "memory_mb": 1024, "time_min": 60}``
    when *step_name* is not in :data:`DEFAULT_STEP_RESOURCES`.
    """
    return DEFAULT_STEP_RESOURCES.get(
        step_name,
        {"cpus": 1, "memory_mb": 1024, "time_min": 60},
    )


# ---------------------------------------------------------------------------
# Per-sample log capture (issue #6)
# ---------------------------------------------------------------------------
def run_subprocess(
    cmd: Sequence[str],
    *,
    stdout_path: Path,
    stderr_path: Path,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    check: bool = False,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a subprocess and redirect stdout/stderr to per-sample log files.

    This is the LocalExecutor-side analogue of what the Slurm and AWS
    Batch executors will do at the substrate level: capture the process
    output into `${outdir}/work/sim/<sample_id>/{stdout,stderr}.log` so
    the user can `cat` the files to debug a failed sample without
    re-running the campaign.

    Both files are created (possibly empty) before the subprocess is
    invoked, so the paths exist on disk even when the process is killed
    before flushing its output buffers. The `text=True` flag decodes
    output as UTF-8; the `errors="replace"` policy keeps us from
    crashing on a stray non-UTF-8 byte in an EnergyPlus log.

    Returns the `CompletedProcess`. The `stdout` / `stderr` attributes of
    the return value are empty strings because the output went to disk
    (use `stdout_path.read_text()` to recover the captured output).

    The function does NOT raise on non-zero exit when `check=False` (the
    default); the caller decides how to surface failures. The Campaign
    inspects the return code and writes the per-sample status.
    """
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    stderr_path.parent.mkdir(parents=True, exist_ok=True)
    # Open in write mode so the files are guaranteed to exist on disk
    # (possibly empty) regardless of whether the subprocess ever flushes
    # its output. Text mode + errors="replace" for EnergyPlus robustness.
    with (
        stdout_path.open("w", encoding="utf-8", errors="replace") as out_f,
        stderr_path.open("w", encoding="utf-8", errors="replace") as err_f,
    ):
        return subprocess.run(  # nosec  # caller owns the argv
            list(cmd),
            stdout=out_f,
            stderr=err_f,
            cwd=str(cwd) if cwd is not None else None,
            env=env,
            check=check,
            timeout=timeout,
            text=True,
            shell=False,
        )


class LocalExecutor(BaseExecutor):
    """Runs tasks in a thread pool. For local dev and CI smoke tests."""

    name = "local"

    def __init__(self, max_workers: int = 4, max_concurrent_samples: int | None = None):
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="osimflow")
        if max_concurrent_samples is not None:
            self._semaphore: threading.Semaphore | None = threading.BoundedSemaphore(
                max_concurrent_samples
            )
        else:
            self._semaphore = None

    def submit(
        self,
        fn: Callable[..., Any],
        *args: Any,
        name: str = "task",
        cpus: int = 1,
        memory_mb: int = 1024,
        time_min: int = 60,
        container: str | None = None,
        container_digest: str | None = None,
        openstudio_version: str | None = None,
        result_hint: Any = None,
        remote_command: str | None = None,
        result_transport_mode: str | None = None,
        result_storage_backend: str | None = None,
        result_storage_bucket: str | None = None,
        result_storage_prefix: str | None = None,
        result_storage_endpoint: str | None = None,
        variables_json: str | None = None,
        env: dict[str, str] | None = None,
        stdout_path: Any = None,
        stderr_path: Any = None,
        max_retries: int | None = None,
        worker_id: str | None = None,
        **kwargs: Any,
    ) -> Handle:
        import os
        import socket

        self._container_digest = container_digest
        del openstudio_version, result_hint, remote_command, result_transport_mode  # noqa: F841
        del result_storage_backend, result_storage_bucket, result_storage_prefix  # noqa: F841
        del result_storage_endpoint, variables_json, stdout_path, stderr_path  # noqa: F841
        del max_retries, worker_id, kwargs  # noqa: F841, ARG002

        log.info("local submit name=%s cpus=%d mem=%dMB", name, cpus, memory_mb)

        if env:

            def _with_env() -> Any:
                original = os.environ.copy()
                os.environ.update(env)
                try:
                    return fn(*args)
                finally:
                    os.environ.clear()
                    os.environ.update(original)

            if self._semaphore is not None:
                sem = self._semaphore

                def _wrapped() -> Any:
                    with sem:
                        return _with_env()

                fut: Future[Any] = self._pool.submit(_wrapped)
            else:
                fut = self._pool.submit(_with_env)
        elif self._semaphore is not None:
            sem = self._semaphore

            def _wrapped() -> Any:
                with sem:
                    return fn(*args)

            fut = self._pool.submit(_wrapped)
        else:
            fut = self._pool.submit(fn, *args)
        return Handle(
            job_id=f"local-{id(fut)}",
            _future=fut,
            worker_id="local",
            worker_ip=socket.gethostname(),
            worker_region=None,
        )

    def shutdown(self) -> None:
        self._pool.shutdown(wait=True)


def _apply_slurm_params(
    auto_executor: Any,
    *,
    partition: str,
    account: str | None,
    cpus_per_task: int,
    mem_gb: int,
    time_min: int,
    qos: str | None = None,
    constraint: str | None = None,
    gres: str | None = None,
) -> None:
    """Apply Slurm parameters to a submitit AutoExecutor with cross-version
    kwarg compatibility.

    submitit 1.5+ renamed `partition` -> `slurm_partition`, `time` ->
    `slurm_time`, and added `slurm_qos` / `slurm_constraint` /
    `slurm_gres`. This helper tries the new spelling first; on
    `TypeError` (older submitit) it retries with the legacy kwarg
    names. `slurm_qos` / `slurm_constraint` / `slurm_gres` are silently
    dropped on legacy submitit (callers on old submitit cannot use the
    advanced flags). `None` values are filtered out so submitit does
    not receive explicit-None assignments for unset directives.
    """
    new_kwargs: dict[str, Any] = {
        "slurm_partition": partition,
        "slurm_account": account,
        "slurm_cpus_per_task": cpus_per_task,
        "slurm_mem_gb": mem_gb,
        "slurm_time": time_min,
        "slurm_qos": qos,
        "slurm_constraint": constraint,
        "slurm_gres": gres,
    }
    new_kwargs = {k: v for k, v in new_kwargs.items() if v is not None}
    legacy_kwargs: dict[str, Any] = {
        "partition": partition,
        "account": account,
        "cpus_per_task": cpus_per_task,
        "mem_gb": mem_gb,
        "time": time_min,
    }
    try:
        auto_executor.update_parameters(**new_kwargs)
    except TypeError:
        auto_executor.update_parameters(**legacy_kwargs)


class SlurmExecutor(BaseExecutor):
    """Slurm executor wrapping `submitit.AutoExecutor`.

    With `debug=True` (the default), submitit runs the job as a local
    subprocess *but still emits the exact `sbatch` command it would have
    run* in the log. This is the documented submitit pattern for
    development without a real cluster.

    In production, set `debug=False` and the same code path submits to
    the configured Slurm partition. Zero application changes.

    Per-submit resource directives (`cpus`, `memory_mb`, `time_min`) are
    honored by constructing a fresh `AutoExecutor` per submission with
    the desired overrides — submitit 1.5+ does not propagate per-call
    kwargs to its inner `update_parameters`. The result is that the
    per-sample cpus/mem/time show up in the actual sbatch header (when
    `debug=False` runs against a real cluster) or in the dry-run
    debug-logged script (when `debug=True`).

    Advanced directives (`qos`, `constraint`, `gres`) are forwarded to
    the executor-level sbatch header for advanced workloads (e.g. GPU
    jobs). All are optional; the corresponding `#SBATCH` line is omitted
    when unset. Requires submitit >= 1.5.
    """

    name = "slurm"

    def __init__(
        self,
        partition: str = "short",
        account: str | None = None,
        cpus_per_task: int = 2,
        mem_gb: int = 4,
        time_h: int = 2,
        debug: bool = True,
        qos: str | None = None,
        constraint: str | None = None,
        gres: str | None = None,
    ):
        # Lazy import so users who only ever run the local executor do
        # not pay the submitit import cost.
        import submitit  # noqa: PLC0415

        self._submitit = submitit

        self.partition = partition
        self.account = account
        self.cpus_per_task = cpus_per_task
        self.mem_gb = mem_gb
        self.time_h = time_h
        self.debug = debug
        self.qos = qos
        self.constraint = constraint
        self.gres = gres

        ex = submitit.AutoExecutor(
            folder=os.environ.get("OSIMFLOW_SLURM_LOGS", "/tmp/osimflow-slurm-logs"),
            slurm_pty=False,
        )
        _apply_slurm_params(
            ex,
            partition=partition,
            account=account,
            cpus_per_task=cpus_per_task,
            mem_gb=mem_gb,
            time_min=int(time_h * 60),
            qos=qos,
            constraint=constraint,
            gres=gres,
        )
        if debug:
            log.warning(
                "SlurmExecutor running in DEBUG mode — jobs run locally; "
                "the exact `sbatch` script that would have been submitted is "
                "logged at INFO level under the `submitit` logger. "
                "Set debug=False for real Slurm."
            )
        self._ex = ex

    def submit(
        self,
        fn: Callable[..., Any],
        *args: Any,
        name: str = "task",
        cpus: int = 1,
        memory_mb: int = 1024,
        time_min: int = 60,
        container: str | None = None,
        container_digest: str | None = None,
        openstudio_version: str | None = None,
        result_hint: Any = None,
        remote_command: str | None = None,
        result_transport_mode: str | None = None,
        result_storage_backend: str | None = None,
        result_storage_bucket: str | None = None,
        result_storage_prefix: str | None = None,
        result_storage_endpoint: str | None = None,
        variables_json: str | None = None,
        env: dict[str, str] | None = None,
        stdout_path: Any = None,
        stderr_path: Any = None,
        max_retries: int | None = None,
        worker_id: str | None = None,
        **kwargs: Any,
    ) -> Handle:
        # Issue #4: per-submit resource directives. submitit 1.5+ does
        # NOT propagate per-submit kwargs to its inner update_parameters
        # — `ex.submit(fn, **kwargs)` passes the kwargs to `fn` itself.
        # The submitit-recommended pattern for per-call overrides is to
        # build a fresh `AutoExecutor` per submission with the desired
        # parameters, then call `.submit()` on it. We do that here so
        # per-sample resources (different memory ceilings for a heavy
        # sample, etc.) are honored in the resulting sbatch header, not
        # just logged.
        # Unused fields: result_hint, remote_command, result_transport_mode,
        # result_storage_*, variables_json, env, stdout/stderr_path, max_retries,
        # worker_id — accepted for API compatibility but not consumed locally.
        if container:
            log.info(
                "slurm submit name=%s cpus=%d mem=%dMB time_min=%d container=%s",
                name,
                cpus,
                memory_mb,
                time_min,
                container,
            )
        else:
            log.info(
                "slurm submit name=%s cpus=%d mem=%dMB time_min=%d",
                name,
                cpus,
                memory_mb,
                time_min,
            )

        # Convert MB -> GB (rounded up so we never under-allocate).
        # submitit speaks integer GB on `slurm_mem_gb`; fractional
        # values are accepted on some versions but integer is the
        # safe cross-version path.
        mem_gb_override = max(1, (memory_mb + 1023) // 1024)

        # Build a fresh AutoExecutor for this submission so the
        # per-call resource directives flow into the rendered sbatch
        # header. The folder is shared (so log files cluster per
        # campaign), and the `slurm_pty=False` matches the init-time
        # config. debug=True uses submitit.DebugExecutor (local) which
        # also honors the per-submit overrides (they appear in the
        # debug-logged sbatch script).
        call_ex = self._submitit.AutoExecutor(
            folder=self._ex.folder,
            slurm_pty=False,
        )
        _apply_slurm_params(
            call_ex,
            partition=self.partition,
            account=self.account,
            cpus_per_task=cpus,
            mem_gb=mem_gb_override,
            time_min=time_min,
            qos=self.qos,
            constraint=self.constraint,
            gres=self.gres,
        )

        # Issue #654: capture values at closure-creation time to avoid
        # late-binding in fast loops where the outer scope changes before
        # _wrapped executes.
        os_version = str(openstudio_version or "N/A")
        cont = container
        fn_to_call = fn
        args_to_pass = args

        def _wrapped() -> Any:
            # Resource directive also becomes an env var so the task can
            # read it (e.g. OpenStudio CLI threading control).
            os.environ["OSIMFLOW_OS_VERSION"] = os_version
            if cont:
                os.environ["OSIMFLOW_CONTAINER"] = cont
            return fn_to_call(*args_to_pass)

        fut: Any = call_ex.submit(_wrapped)
        return Handle(
            job_id=str(fut.job_id),
            _future=fut,
            worker_id=str(fut.job_id),
            worker_ip=None,
            worker_region=None,
        )

    def shutdown(self) -> None:
        # submitit.AutoExecutor has no explicit shutdown; the underlying
        # DebugExecutor / SlurmExecutor cleans up on process exit.
        pass


def _aws_error_code(exc: Exception) -> str:
    """Extract AWS/boto error code from an exception, or empty string if not applicable."""
    try:
        return exc.response.get("Error", {}).get("Code", "") if hasattr(exc, "response") else ""
    except Exception:  # noqa: BLE001
        return ""


def _nomad_error_code(exc: Exception) -> int:
    """Extract HTTP status code from a Nomad/Consulate exception, or 0 if not applicable."""
    try:
        return getattr(exc, "status_code", 0) or 0
    except Exception:  # noqa: BLE001
        return 0


class _AWSBatchHandle(Handle):
    """Handle that polls Batch on `.result()`.

    We can't use a vanilla `concurrent.futures.Future` (which would let
    us reuse the base `Handle` unchanged) because the work runs in a
    remote Batch task — there's no thread or submitit job to back the
    Future. Instead, the handle carries a reference to its executor
    and the Batch `jobId`; `result()` blocks on `_wait_for_terminal`
    and `done()` does a single non-blocking `describe_jobs` call.

    Spot retry logic (issue #131) lives here so that `submit()` can
    return immediately (issue #262).  When `result()` detects a Spot
    interruption, it resubmits using the stored `_submit_params` and
    retries up to ``executor.max_retries`` times before falling back
    to on-demand or failing.

    Not a dataclass — the parent `Handle` is, and dataclass inheritance
    fights with the new `_executor` field (default-vs-required ordering
    gets ugly). Constructed only inside `AWSBatchExecutor.submit()`,
    so we own the call site and don't need the dataclass machinery.
    """

    _GHOST_RETRIES = 3

    def __init__(
        self,
        job_id: str,
        executor: "AWSBatchExecutor",
        submit_params: dict[str, Any],
        *,
        result_hint: Any = None,
    ) -> None:
        self.job_id = job_id
        self._executor = executor
        self._submit_params = submit_params
        self._result_hint = result_hint
        # Keep a `Future` so the base-class `.result(timeout=...)` /
        # `.done()` paths remain reachable; we cache the poll result
        # in it so concurrent callers don't re-poll.
        self._future: Future[Any] = Future()
        # Worker tracking (issue #105): populate at submit time.
        self.worker_id: str | None = job_id
        self.worker_ip: str | None = None
        self.worker_region: str | None = os.environ.get("AWS_REGION")
        # Cost tracking (issue #126): populated after job completes.
        self.cost_usd: float | None = None
        self.billed_duration_seconds: float | None = None

    def _apply_cost(self, job: dict[str, Any]) -> None:
        """Compute and store per-job cost from a completed job dict."""
        started = job.get("startedAt")
        stopped = job.get("stoppedAt")
        if started is not None and stopped is not None:
            self.billed_duration_seconds = max(0.0, (stopped - started) / 1000.0)
        cost_usd, _spot_savings = self._executor._calculate_job_cost(job)  # noqa: SLF001
        if cost_usd > 0:
            self.cost_usd = cost_usd

    def result(self, timeout: float | None = None) -> Any:
        # Timeout tracking: elapsed time is shared across spot-retry
        # iterations so the caller-supplied deadline is honoured
        # regardless of how many times the job is resubmitted.
        start = time.monotonic()
        remaining: float | None = None  # None means "no timeout"

        # Spot retry loop (issue #131): on Spot interruption, resubmit
        # up to max_retries times, then fall back to on-demand or fail.
        effective_max_retries = max(0, self._executor.max_retries)
        for attempt in range(effective_max_retries + 1):
            # Compute remaining time for this poll iteration.
            if timeout is not None:
                elapsed = time.monotonic() - start
                remaining = timeout - elapsed
                if remaining <= 0:
                    raise TimeoutError(
                        f"Timed out after {elapsed:.1f}s waiting for job {self.job_id!r}"
                    )

            try:
                job = self._executor._wait_for_terminal(self.job_id, timeout=remaining)  # noqa: SLF001
            except Exception as exc:  # noqa: BLE001 — let KeyboardInterrupt/SystemExit propagate
                self._future.set_exception(exc)
                raise

            self._apply_cost(job)
            status = job.get("status")
            if status == "SUCCEEDED":
                resolved = resolve_result_for_callback(self._result_hint, default=None)
                self._future.set_result(resolved)
                return resolved

            # Job FAILED — check if it was a Spot interruption.
            reason = job.get("statusReason", "")
            is_spot = self._executor._is_spot_interruption(reason)  # noqa: SLF001

            if is_spot and attempt < effective_max_retries:
                backoff = min(5.0 * (2**attempt), 60.0)
                jittered_backoff = random.uniform(0, backoff)
                log.warning(
                    "Spot interrupted (attempt %d/%d), retrying in %.1fs: %s",
                    attempt + 1,
                    effective_max_retries,
                    jittered_backoff,
                    reason,
                )
                time.sleep(jittered_backoff)
                # Resubmit and update the tracked job_id.
                self.job_id = self._executor._submit_job(**self._submit_params)  # noqa: SLF001
                self.worker_id = self.job_id
                continue

            if is_spot and attempt >= effective_max_retries:
                # Exhausted retries — fall back or fail.
                if self._executor.fallback_to_on_demand:
                    log.warning(
                        "Spot retries exhausted (%d), falling back to on-demand",
                        effective_max_retries,
                    )
                    self.job_id = self._executor._submit_job(**self._submit_params)  # noqa: SLF001
                    self.worker_id = self.job_id
                    # Poll the on-demand job (no more retries).
                    try:
                        job = self._executor._wait_for_terminal(self.job_id, timeout=remaining)  # noqa: SLF001
                    except Exception as exc:  # noqa: BLE001 — let KeyboardInterrupt/SystemExit propagate
                        self._future.set_exception(exc)
                        raise
                    self._apply_cost(job)
                    status = job.get("status")
                    if status == "SUCCEEDED":
                        resolved = resolve_result_for_callback(self._result_hint, default=None)
                        self._future.set_result(resolved)
                        return resolved
                    reason = job.get("statusReason", "unknown reason")
                    msg = f"AWS Batch job {self.job_id!r} {status}: {reason}"
                    self._future.set_exception(RuntimeError(msg))
                    raise RuntimeError(msg)
                raise RuntimeError(f"Spot retries exhausted ({effective_max_retries}): {reason}")

            # Non-spot failure — don't retry, raise immediately.
            msg = f"AWS Batch job {self.job_id!r} {status}: {reason}"
            self._future.set_exception(RuntimeError(msg))
            raise RuntimeError(msg)

        # Unreachable, but satisfies the type checker.
        raise RuntimeError("result loop exited unexpectedly")  # pragma: no cover

    def done(self) -> bool:
        # A single non-blocking `describe_jobs` is the cheapest probe.
        # If the task is in a terminal state, we've already finished;
        # otherwise we're still running. Anything else (UNKNOWN status,
        # network blip) is treated as not-done. Ghost jobs (deleted or
        # never-created) return an empty list — after N consecutive
        # empty responses we raise to break the indefinite-wait loop.
        for attempt in range(self._GHOST_RETRIES):
            try:
                response = self._executor._get_client().describe_jobs(  # noqa: SLF001
                    jobs=[self.job_id]
                )
            except Exception as exc:  # noqa: BLE001 — never raise from done()
                log.warning("Polling error for %s: %s", self.job_id, exc)
                self.error = exc
                return False
            jobs = response.get("jobs", [])
            if jobs:
                break
            log.debug(
                "Empty describe_jobs for %s, attempt %d/%d",
                self.job_id,
                attempt + 1,
                self._GHOST_RETRIES,
            )
        else:
            # Ghost job: not found after _GHOST_RETRIES consecutive empty
            # responses. Per the base Handle.done() contract (base.py:100),
            # polling errors must be captured and returned as False, not raised.
            self.error = RuntimeError(
                f"Ghost job: job ID {self.job_id!r} not found after {self._GHOST_RETRIES} retries"
            )
            return False
        status = jobs[0].get("status", "")
        return status in ("SUCCEEDED", "FAILED")


# ---------------------------------------------------------------------------
# Token-bucket rate limiter + spot-price cache (issue #1010)
# ---------------------------------------------------------------------------
class _TokenBucketRateLimiter:
    """Thread-safe token-bucket rate limiter for AWS Batch submit throttling.

    Shared across all ``AWSBatchExecutor`` instances via
    :meth:`get_shared` so that concurrent submissions from multiple
    executor objects stay within the per-account submit_job rate limit
    (issue #1010).  AWS Batch documents ``submit_job`` at 1 000 TPS per
    account; the default of 800 RPS leaves headroom for burst contention
    between concurrent fan-out threads.
    """

    DEFAULT_RPS: float = 800.0

    _INSTANCES: ClassVar[dict[float, "_TokenBucketRateLimiter"]] = {}
    _INSTANCES_LOCK: ClassVar[threading.Lock] = threading.Lock()

    def __init__(self, rps: float = DEFAULT_RPS) -> None:
        self._disabled: bool = rps <= 0
        self._rate: float = float(rps) if not self._disabled else 0.0
        self._capacity: int = max(int(rps), 1) if not self._disabled else 1
        self._tokens: float = float(self._capacity)
        self._last_refill: float = time.monotonic()
        self._lock: threading.Lock = threading.Lock()

    @classmethod
    def get_shared(cls, rps: float | None = None) -> "_TokenBucketRateLimiter":
        """Return a shared limiter for the requested RPS (singleton per RPS).

        All executor instances requesting the same RPS share the same
        bucket, ensuring the aggregate submit rate stays bounded.
        ``rps=None`` falls back to the default (800). ``rps=0`` disables
        rate limiting entirely (use with caution).
        """
        effective: float = cls.DEFAULT_RPS if rps is None else float(rps)
        with cls._INSTANCES_LOCK:
            if effective not in cls._INSTANCES:
                cls._INSTANCES[effective] = cls(rps=effective)
            return cls._INSTANCES[effective]

    def acquire(self) -> None:
        """Block until a token is available, then consume one."""
        if self._disabled:
            return
        while True:
            with self._lock:
                now = time.monotonic()
                elapsed = now - self._last_refill
                self._tokens = min(
                    float(self._capacity),
                    self._tokens + elapsed * self._rate,
                )
                self._last_refill = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                deficit = 1.0 - self._tokens
                wait_s = deficit / self._rate
            # Sleep without holding the lock so other threads aren't blocked.
            time.sleep(wait_s)


class _SpotPriceCache:
    """Thread-safe TTL cache for EC2 Spot price lookups (issue #1010).

    Keyed by ``(region, instance_type, product_description)`` so that
    campaigns with different configurations don't share stale prices.
    The 60-second TTL is short enough to pick up price changes while
    still amortizing the per-sample EC2 API call cost.
    """

    def __init__(self, ttl_s: float = 60.0) -> None:
        self._ttl_s: float = ttl_s
        self._cache: dict[tuple[str | None, str | None, str], tuple[float, float]] = {}
        self._lock: threading.Lock = threading.Lock()

    def get(
        self,
        key: tuple[str | None, str | None, str],
    ) -> float | None:
        """Return the cached price if within TTL, else ``None``."""
        with self._lock:
            cached = self._cache.get(key)
            if cached is None:
                return None
            price, ts = cached
            if time.monotonic() - ts >= self._ttl_s:
                del self._cache[key]
                return None
            return price

    def set(
        self,
        key: tuple[str | None, str | None, str],
        price: float,
    ) -> None:
        """Store a spot price in the cache with the current timestamp."""
        with self._lock:
            self._cache[key] = (price, time.monotonic())

    def clear(self) -> None:
        """Clear all cached entries (useful for tests)."""
        with self._lock:
            self._cache.clear()


class AWSBatchExecutor(BaseExecutor):
    """AWS Batch executor (issue #5).

    Wraps `boto3.client('batch').submit_job` to launch one Batch task per
    call, then polls `describe_jobs` (with exponential backoff) until the
    task reaches a terminal state. The returned `Handle` carries the
    Batch `jobId` and blocks on `.result()` until the task succeeds; on
    failure it re-raises a `RuntimeError` whose message includes the
    Batch `statusReason` so the Campaign's `except Exception` path logs
    a useful line.

    Resource directives (`cpus`, `memory_mb`, `time_min`) are mapped to
    the Batch `containerOverrides` (`vcpus`, `memory` in MiB, `timeout`
    in seconds). Per-sample `OSIMFLOW_OS_VERSION` and `OSIMFLOW_CONTAINER`
    are carried as Batch environment variables — the same env vars
    `SlurmExecutor` exports, so downstream work scripts can be
    substrate-agnostic.

    Security: the boto3 client
    sources credentials from the IAM role attached to the Batch compute
    environment. The constructor does **not** accept
    `aws_access_key_id` / `aws_secret_access_key`; passing long-lived
    keys would violate the security policy. The ``region_name`` parameter
    pins the region passed to boto3; when ``None``, boto3 follows the
    IAM role's region (or ``AWS_REGION`` env var / ``~/.aws/config``).

    Spot instance retry + price ceiling (issue #131):
    When `max_spot_price_usd` is set, the executor queries the current
    Spot price via the EC2 API before submitting and rejects jobs that
    would exceed the ceiling. When `fallback_to_on_demand` is set and
    the price ceiling is breached (or max retries are exhausted after
    Spot interruptions), the executor falls back to submitting to the
    on-demand queue. `max_retries` controls how many times a
    Spot-interrupted job is retried before fallback or failure. Each
    retry uses exponential backoff starting at 5 seconds, capped at
    60 seconds.

    boto3 is lazy-imported inside `__init__` so the local-executor /
    slurm-executor paths do not pay the import cost.
    """

    name = "aws_batch"

    @property
    def requires_remote_runner_payload(self) -> bool:
        return True

    # Default pricing estimates (USD per vCPU-hour). Conservative defaults
    # used when the Spot price cannot be queried or the instance type is
    # unknown. These are intentionally slightly above market average to
    # keep estimates within 20% of the actual AWS bill (issue #126).
    DEFAULT_ON_DEMAND_PRICE_PER_VCPU_HOUR: float = 0.05
    DEFAULT_SPOT_PRICE_PER_VCPU_HOUR: float = 0.03

    # Issue #1081: digest pinning. Class attribute default ensures the
    # attribute exists even when __init__ is bypassed (e.g. tests using __new__).
    _container_digest: str | None = None

    # Sentinel used in statusReason to identify Spot interruptions.
    _SPOT_INTERRUPTION_MARKERS: tuple[str, ...] = (
        "Spot interruption",
        "Spot Instance termination",
        "spot",
    )

    # AWS error codes that should trigger a submit retry (issue #1010).
    _THROTTLE_ERRORS: tuple[str, ...] = (
        "ThrottlingException",
        "RequestLimitExceeded",
    )

    _DEFAULT_SUBMIT_RPS: float = 800.0

    def __init__(
        self,
        job_queue: str = "osimflow-batch-queue",
        job_definition: str | None = None,
        poll_interval_s: float = 5.0,
        max_poll_interval_s: float = 60.0,
        region_name: str | None = None,
        *,
        max_spot_price_usd: float | None = None,
        fallback_to_on_demand: bool = False,
        max_retries: int = 3,
        ecr_repository: str | None = None,
        instance_type: str | None = None,
        submit_rps: float | None = None,
    ):
        # Lazy import: keeps the boto3 import cost off the local /
        # slurm executor paths. ImportError here is intentional: the
        # user opted into the [aws] extra, so a missing boto3 is a
        # user error, not a silent fallback.
        import boto3  # noqa: PLC0415
        from botocore.config import Config as BotoConfig  # noqa: PLC0415

        self._boto3 = boto3
        # boto3.client("batch") without a configured region raises
        # NoRegionError immediately, so we defer client construction
        # to first use. The region still comes from the IAM role /
        # AWS_REGION env / ~/.aws/config — `region_name=None` just
        # tells boto3 to follow that chain rather than pin a region.
        self._region_name = region_name
        self._client: Any = None
        self._ec2_client: Any = None
        self.job_queue = job_queue
        self.job_definition = job_definition or "osimflow-job-def"
        # Issue #1081: digest pinning. Initialized in the constructor so
        # ``_resolve_container_image`` is callable without going through
        # ``submit()`` (e.g. unit tests); overridden by ``submit()``.
        self._container_digest: str | None = None
        self.poll_interval_s = poll_interval_s
        self.max_poll_interval_s = max_poll_interval_s
        self.max_spot_price_usd = max_spot_price_usd
        self.fallback_to_on_demand = fallback_to_on_demand
        self.max_retries = max_retries
        self.ecr_repository = ecr_repository
        self._instance_type = instance_type
        self._submit_rps = submit_rps
        # boto3 retry config with adaptive mode for ThrottlingException
        # handling (issue #1010). Adaptive mode uses client-side rate
        # limiting + exponential backoff with jitter.
        self._retry_config = BotoConfig(
            retries={"mode": "adaptive", "max_attempts": 10},
        )
        # Token-bucket submit rate limiter, shared across all executor
        # instances (issue #1010). Prevents ThrottlingException at fan-out.
        self._submit_limiter = _TokenBucketRateLimiter.get_shared(rps=submit_rps)
        # Spot price cache with 60s TTL — avoids one EC2 API call per
        # sample in a 10K-sample campaign (issue #1010).
        self._spot_price_cache = _SpotPriceCache(ttl_s=60.0)

    def _resolve_container_image(self, version: str | None) -> str:
        """Resolve the container image URI.

        When ``ecr_repository`` is set, returns ``<ecr_repo>:<version>``.
        Otherwise falls back to Docker Hub ``nrel/openstudio:<version>``.

        Issue #1081: when the caller pins images by SHA256 digest,
        the digest is returned verbatim and overrides every tag-based
        resolution path below.
        """
        container_digest = self._container_digest
        if container_digest:
            return container_digest
        tag = version or "latest"
        if self.ecr_repository:
            return f"{self.ecr_repository}:{tag}"
        return f"nrel/openstudio:{tag}"

    def _get_client(self) -> Any:
        """Lazy boto3 Batch client construction.

        Deferring to first use lets the constructor succeed on hosts
        that have boto3 installed but no AWS config (e.g. CI runners
        that only test the executor wiring with mocked clients).
        Production deployments will have AWS_REGION set or an IAM
        role / ~/.aws/config in place.
        """
        if self._client is None:
            self._client = self._boto3.client(
                "batch",
                region_name=self._region_name,
                config=self._retry_config,
            )
        return self._client

    def _get_ec2_client(self) -> Any:
        """Lazy boto3 EC2 client for Spot price queries."""
        if self._ec2_client is None:
            self._ec2_client = self._boto3.client(
                "ec2",
                region_name=self._region_name,
                config=self._retry_config,
            )
        return self._ec2_client

    def _get_spot_price(self) -> float:
        """Query the current Spot price for the instance type.

        Cached with a 60-second TTL keyed by ``(region, instance_type, os)``
        (issue #1010).  When ``max_spot_price_usd`` is set, the ceiling
        check reuses the cached value across all samples in a campaign
        instead of making one EC2 API call per sample.

        Uses ``describe_spot_price_history`` with a single-result query
        to get the most recent price. Returns the price in USD per
        instance-hour. Raises ``RuntimeError`` if the query fails or
        returns no results.

        When ``_instance_type`` is set, the query is scoped to that
        instance type so the ceiling check is reliable (issue #792).
        When it is not set, the query returns the lowest price across
        all instance types and a warning is logged.
        """
        product = "Linux/UNIX"
        cache_key: tuple[str | None, str | None, str] = (
            self._region_name,
            self._instance_type,
            product,
        )
        cached = self._spot_price_cache.get(cache_key)
        if cached is not None:
            return cached

        kwargs: dict[str, Any] = {
            "MaxResults": 1,
            "ProductDescriptions": [product],
        }
        if self._instance_type is not None:
            kwargs["InstanceTypes"] = [self._instance_type]
        response = self._get_ec2_client().describe_spot_price_history(**kwargs)
        histories = response.get("SpotPriceHistory", [])
        if not histories:
            raise RuntimeError("describe_spot_price_history returned no results")
        price = float(histories[0]["SpotPrice"])

        self._spot_price_cache.set(cache_key, price)
        return price

    def _is_spot_interruption(self, reason: str | None) -> bool:
        """Return True if the failure reason indicates a Spot interruption."""
        if not reason:
            return False
        lower = reason.lower()
        return any(marker.lower() in lower for marker in self._SPOT_INTERRUPTION_MARKERS)

    def _check_spot_price_ceiling(self) -> None:
        """Check the Spot price against the configured ceiling.

        Raises ``RuntimeError`` when the Spot price exceeds the ceiling
        and ``fallback_to_on_demand`` is False. When fallback is enabled,
        logs a warning and returns (caller should switch to on-demand).
        """
        if self.max_spot_price_usd is None:
            return
        current_price = self._get_spot_price()
        if current_price <= self.max_spot_price_usd:
            return
        msg = f"Spot price ${current_price:.4f} exceeds ceiling ${self.max_spot_price_usd:.4f}"
        if self.fallback_to_on_demand:
            log.warning("%s — falling back to on-demand", msg)
            return
        raise RuntimeError(msg)

    @staticmethod
    def _infer_step_name(submit_name: str) -> str:
        """Map a submit name to the remote_runner step identifier.

        Same mapping as ``NomadExecutor._infer_step_name`` and
        ``KubernetesExecutor._infer_step_name``: the Campaign names
        fan-out tasks ``apply_<sid>`` / ``sim_<sid>`` / ``kpi_<sid>`` and
        the single-shot steps ``aggregate`` / ``plots``; the remote runner
        resolves the work function from the step identifier.
        """
        lower = submit_name.lower()
        if lower.startswith("apply_"):
            return "apply"
        if lower.startswith("sim_"):
            return "sim"
        if lower.startswith("kpi_"):
            return "extract"
        if lower.startswith("aggregate"):
            return "aggregate"
        if lower.startswith("plots"):
            return "plots"
        return "unknown"

    def _build_environment(
        self,
        *,
        container: str | None,
        openstudio_version: str | None,
        task_payload: str | None = None,
        result_transport_mode: str | None = None,
        result_storage_backend: str | None = None,
        result_storage_bucket: str | None = None,
        result_storage_prefix: str | None = None,
        result_storage_endpoint: str | None = None,
    ) -> list[dict[str, str]]:
        """Build the Batch `environment` list from the per-submit kwargs.

        The serialized task payload travels in ``OSIMFLOW_TASK_PAYLOAD`` and
        the result-transport contract in the ``OSIMFLOW_RESULT_*`` vars so
        ``osimflow.remote_runner`` can execute the step and push results to
        object storage (issue #996). ``OSIMFLOW_STUB_SIM`` is propagated
        from the orchestrator environment when set so remote pods honour
        the orchestrator's stub-vs-real CLI choice.
        """
        env: list[dict[str, str]] = []
        if openstudio_version is not None:
            env.append({"name": "OSIMFLOW_OS_VERSION", "value": str(openstudio_version)})
        # Resolve container image using the standard resolution logic
        # which respects container_digest, ecr_repository, and the
        # container parameter.
        resolved = self._resolve_container_image(openstudio_version)
        # If a custom container was passed, it takes precedence
        if container is not None:
            resolved = container
        env.append({"name": "OSIMFLOW_CONTAINER", "value": resolved})
        if task_payload is not None:
            env.append({"name": "OSIMFLOW_TASK_PAYLOAD", "value": task_payload})
        if result_transport_mode is not None:
            env.append({"name": "OSIMFLOW_RESULT_TRANSPORT_MODE", "value": result_transport_mode})
        if result_storage_backend is not None:
            env.append({"name": "OSIMFLOW_RESULT_STORAGE_BACKEND", "value": result_storage_backend})
        if result_storage_bucket is not None:
            env.append({"name": "OSIMFLOW_RESULT_STORAGE_BUCKET", "value": result_storage_bucket})
        if result_storage_prefix is not None:
            env.append({"name": "OSIMFLOW_RESULT_STORAGE_PREFIX", "value": result_storage_prefix})
        if result_storage_endpoint is not None:
            env.append(
                {"name": "OSIMFLOW_RESULT_STORAGE_ENDPOINT", "value": result_storage_endpoint}
            )
        stub_sim = os.environ.get("OSIMFLOW_STUB_SIM")
        if stub_sim is not None:
            env.append({"name": "OSIMFLOW_STUB_SIM", "value": stub_sim})
        return env

    def _build_container_overrides(
        self,
        *,
        cpus: int,
        memory_mb: int,
        environment: list[dict[str, str]],
        command: list[str] | None = None,
    ) -> dict[str, Any]:
        """Translate OSimFlow resource directives to Batch overrides.

        The Batch API takes memory in MiB; `memory_mb` is in megabytes
        and we treat the two as equivalent (the difference is < 5% and
        Batch's documented unit is MiB, so 1:1 keeps the intent clear
        to anyone reading the submit_job call).

        When ``command`` is provided, it overrides the job definition's
        container command (e.g. to run ``python -m osimflow.remote_runner``).
        """
        overrides: dict[str, Any] = {
            "vcpus": cpus,
            "memory": memory_mb,
            "environment": environment,
        }
        if command is not None:
            overrides["command"] = command
        return overrides

    def _calculate_job_cost(
        self,
        job: dict[str, Any],
        vcpus: int = 1,
    ) -> tuple[float, float]:
        """Estimate cost for a completed Batch job (issue #126).

        Uses the job's ``startedAt`` and ``stoppedAt`` timestamps to
        determine billed duration, then multiplies by the per-vCPU-hour
        rate.  For Spot jobs, the rate is the lower Spot price; the
        difference between Spot and On-Demand is the savings.

        Returns (cost_usd, spot_savings_usd).  Both default to 0.0 when
        timestamps or pricing data are unavailable.

        Parameters
        ----------
        job
            The Batch ``describe_jobs`` response dict for the completed job.
        vcpus
            Number of vCPUs allocated to the job (from container overrides
            or the job definition).
        """
        started = job.get("startedAt")
        stopped = job.get("stoppedAt")
        if started is None or stopped is None:
            return 0.0, 0.0

        # Batch timestamps are milliseconds since epoch.
        duration_s = max(0.0, (stopped - started) / 1000.0)
        if duration_s <= 0:
            return 0.0, 0.0

        duration_hours = duration_s / 3600.0

        # Determine the effective Spot price.
        spot_price = self.DEFAULT_SPOT_PRICE_PER_VCPU_HOUR
        try:
            queried_price = self._get_spot_price()
            if queried_price > 0:
                spot_price = queried_price
        except Exception as exc:
            log.warning("could not query Spot price for cost calc, using default: %s", exc)

        on_demand_price = self.DEFAULT_ON_DEMAND_PRICE_PER_VCPU_HOUR
        cost_usd = duration_hours * vcpus * on_demand_price
        spot_savings = duration_hours * vcpus * (on_demand_price - spot_price)

        return cost_usd, spot_savings

    def _wait_for_terminal(self, job_id: str, timeout: float | None = None) -> dict[str, Any]:
        """Poll `describe_jobs` with exponential backoff until the task
        reaches a terminal state. Returns the final job dict.

        Raises:
            TimeoutError: if *timeout* seconds elapse before a terminal state.
        """
        delay = self.poll_interval_s
        start = time.monotonic()
        while True:
            # boto3's describe_jobs returns a TypedDict at runtime, but
            # the type is too granular to be useful here — we treat the
            # response as a plain dict and access .get() on each level.
            # `cast` to `Any` keeps mypy strict mode happy without
            # polluting the rest of the function.
            response: dict[str, Any] = self._get_client().describe_jobs(jobs=[job_id])
            jobs = response.get("jobs", [])
            if not jobs:
                raise RuntimeError(f"describe_jobs returned no job for jobId={job_id!r}")
            job = jobs[0]
            status = job.get("status", "UNKNOWN")
            if status in ("SUCCEEDED", "FAILED"):
                return cast(dict[str, Any], job)

            # Enforce timeout before sleeping.
            if timeout is not None:
                elapsed = time.monotonic() - start
                remaining = timeout - elapsed
                if remaining <= 0:
                    raise TimeoutError(f"Timed out after {elapsed:.1f}s waiting for job {job_id!r}")
                # Cap the sleep so we don't overshoot the timeout.
                delay = min(delay, remaining)

            log.info("aws_batch poll jobId=%s status=%s (sleeping %.1fs)", job_id, status, delay)
            time.sleep(delay)
            # Exponential backoff, capped.
            delay = min(delay * 2, self.max_poll_interval_s)

    def _submit_job(
        self,
        *,
        name: str,
        cpus: int,
        memory_mb: int,
        time_min: int,
        environment: list[dict[str, str]],
        command: list[str] | None = None,
        job_queue: str | None = None,
    ) -> str:
        """Submit a single Batch job and return the jobId.

        Uses *job_queue* if provided, otherwise ``self.job_queue``.

        Throttles via the shared token-bucket rate limiter (issue #1010)
        and retries on ``ThrottlingException`` / ``RequestLimitExceeded``
        with exponential backoff as defense-in-depth on top of boto3's
        adaptive retry mode.

        When ``command`` is provided, it overrides the job definition's
        container command (e.g. to run ``python -m osimflow.remote_runner``).
        """
        queue = job_queue or self.job_queue
        overrides = self._build_container_overrides(
            cpus=cpus,
            memory_mb=memory_mb,
            environment=environment,
            command=command,
        )
        attempt_duration_seconds = int(time_min) * 60
        submit_kwargs: dict[str, Any] = {
            "jobName": name,
            "jobQueue": queue,
            "jobDefinition": self.job_definition,
            "containerOverrides": overrides,
            "timeout": {"attemptDurationSeconds": attempt_duration_seconds},
        }
        # Acquire a rate-limiter token before submitting (issue #1010).
        self._submit_limiter.acquire()
        response = self._submit_job_with_retry(submit_kwargs)
        job_id: str = str(response["jobId"])
        log.info("aws_batch submit_job -> jobId=%s queue=%s", job_id, queue)
        return job_id

    def _submit_job_with_retry(self, submit_kwargs: dict[str, Any]) -> dict[str, Any]:
        """Call ``submit_job`` with retry on throttle exceptions (issue #1010).

        boto3's adaptive retry config (``retry_mode='adaptive'``) handles
        transport-level retries.  This wrapper provides defense-in-depth
        for ``ThrottlingException`` that propagates to our code, applying
        exponential backoff with up to 5 attempts.
        """
        import botocore.exceptions  # noqa: PLC0415

        max_attempts = 5
        delay = 0.5
        for attempt in range(max_attempts):
            try:
                return self._get_client().submit_job(**submit_kwargs)  # type: ignore[no-any-return]
            except botocore.exceptions.ClientError as exc:
                code = _aws_error_code(exc)
                if code in self._THROTTLE_ERRORS and attempt < max_attempts - 1:
                    log.warning(
                        "submit_job throttled (attempt %d/%d), retrying in %.1fs: %s",
                        attempt + 1,
                        max_attempts,
                        delay,
                        code,
                    )
                    time.sleep(random.uniform(0, delay))
                    delay = min(delay * 2, 30.0)
                    continue
                raise
        raise RuntimeError("submit_job throttle retry exhausted")  # pragma: no cover

    def submit(
        self,
        fn: Callable[..., Any],
        *args: Any,
        name: str = "task",
        cpus: int = 1,
        memory_mb: int = 1024,
        time_min: int = 60,
        container: str | None = None,
        container_digest: str | None = None,
        openstudio_version: str | None = None,
        result_hint: Any = None,
        remote_command: str | None = None,
        result_transport_mode: str | None = None,
        result_storage_backend: str | None = None,
        result_storage_bucket: str | None = None,
        result_storage_prefix: str | None = None,
        result_storage_endpoint: str | None = None,
        variables_json: str | None = None,
        env: dict[str, str] | None = None,
        stdout_path: Any = None,
        stderr_path: Any = None,
        max_retries: int | None = None,
        worker_id: str | None = None,
        **kwargs: Any,
    ) -> Handle:
        self._container_digest = container_digest
        del variables_json, env, stdout_path, stderr_path, max_retries, worker_id, kwargs  # noqa: F841, ARG002
        self._container_digest = container_digest

        log.info(
            "aws_batch submit name=%s cpus=%d mem=%dMB time_min=%d container=%s",
            name,
            cpus,
            memory_mb,
            time_min,
            container,
        )

        # Ephemeral-runner contract (issue #996, #1077): serialize the step
        # call into the task payload; the Batch-side
        # ``python -m osimflow.remote_runner`` decodes it and executes the
        # work function in container-local storage.
        step_name = self._infer_step_name(name)
        task_payload = self._build_task_payload(
            step_name=step_name,
            args=args,
            kwargs={},
            result_hint=result_hint,
            name=name,
        )

        if remote_command:
            command: list[str] = ["/bin/sh", "-c", remote_command]
        else:
            command = ["python", "-m", "osimflow.remote_runner"]

        environment = self._build_environment(
            container=container,
            openstudio_version=openstudio_version,
            task_payload=task_payload,
            result_transport_mode=(
                str(result_transport_mode) if result_transport_mode is not None else None
            ),
            result_storage_backend=(
                str(result_storage_backend) if result_storage_backend is not None else None
            ),
            result_storage_bucket=(
                str(result_storage_bucket) if result_storage_bucket is not None else None
            ),
            result_storage_prefix=(
                str(result_storage_prefix) if result_storage_prefix is not None else None
            ),
            result_storage_endpoint=(
                str(result_storage_endpoint) if result_storage_endpoint is not None else None
            ),
        )

        # --- Spot price ceiling check (issue #131, #792) ---
        # Fast, non-blocking check: query the current Spot price and
        # either raise or fall back to on-demand. This gate runs before
        # any job submission so we don't waste a Batch task that would
        # immediately be more expensive than the ceiling.
        if self.max_spot_price_usd is not None:
            if self._instance_type is None:
                log.warning(
                    "instance_type is not set — spot price ceiling check "
                    "queries the minimum across all instance types and may "
                    "not reflect the actual cost (issue #792). Set "
                    "--aws-batch-instance-type to scope the check."
                )
            try:
                current_price = self._get_spot_price()
                if current_price > self.max_spot_price_usd:
                    msg = (
                        f"Spot price ${current_price:.4f} exceeds ceiling "
                        f"${self.max_spot_price_usd:.4f}"
                    )
                    if self.fallback_to_on_demand:
                        log.warning("%s — falling back to on-demand", msg)
                    else:
                        raise RuntimeError(msg)
            except RuntimeError:
                raise
            except Exception as exc:
                if self.max_spot_price_usd is not None:
                    raise
                log.warning("could not check Spot price: %s", exc)

        # Submit the job to AWS Batch and return immediately (issue #262).
        # Spot retry logic lives in _AWSBatchHandle.result() so that
        # submit() is non-blocking — a prerequisite for concurrent fan-out.
        del fn  # noqa: ARG002 — work runs inside the Batch container via remote_runner

        submit_params: dict[str, Any] = {
            "name": name,
            "cpus": cpus,
            "memory_mb": memory_mb,
            "time_min": time_min,
            "environment": environment,
            "command": command,
        }
        job_id = self._submit_job(**submit_params)

        return _AWSBatchHandle(
            job_id=job_id,
            executor=self,
            submit_params=submit_params,
            result_hint=result_hint,
        )

    def shutdown(self) -> None:
        # boto3 clients hold an HTTP session that is closed by the
        # underlying botocore session on GC; nothing actionable here.
        pass


# ---------------------------------------------------------------------------
# Nomad executor (issue #27)
# ---------------------------------------------------------------------------
# DNS-1123 label: lowercase alphanumeric + dashes, max 63 chars. Nomad
# job names must satisfy this; the executor slugifies user-supplied
# names so a sample id like "sample-0" lands as a valid job name
# without Nomad rejecting the job spec.
_DNS1123_LABEL = re.compile(r"[^a-z0-9-]+")
_DNS1123_TRIM = re.compile(r"^-+|-+$")


def _slugify_job_name(name: str) -> str:
    """Convert a user-supplied task name into a DNS-1123 label.

    Nomad rejects job specs whose ``Name`` is not a DNS-1123 label. The
    Campaign passes names like ``"sim_<sample_id>"`` (snake_case + angle
    chars); we need to lowercase, replace non-alphanumerics with
    dashes, trim leading/trailing dashes, and clip to 63 chars.
    """
    slug = name.lower()
    slug = _DNS1123_LABEL.sub("-", slug)
    slug = _DNS1123_TRIM.sub("", slug)
    slug = slug[:63]
    if not slug:
        slug = "task"
    return slug


class _NomadClient:
    """Thin HTTP client for the Nomad HTTP API.

    Wraps ``urllib.request.urlopen`` so the executor can be tested by
    patching a single function (the same pattern the boto3 executor
    uses with ``boto3.client``). The client carries the Nomad address
    and ACL token (sourced from the environment at construction time)
    and exposes ``submit_job(spec)`` / ``get_allocation(alloc_id)``
    helpers that return parsed JSON.

    The client is lazy-imported (``urllib.request`` is stdlib, so this
    is mostly about deferring the import out of ``__init__.py``). No
    third-party HTTP library is required.

    TLS verification: when ``verify_tls`` is ``True`` (the default),
    the client delegates to ``urllib.request.urlopen`` (the stdlib
    default, which uses system CA certs and verifies the server cert).
    When ``False``, the client builds a custom opener with an SSL
    context that skips certificate verification (for development with
    self-signed certs). Tests patch ``urllib.request.urlopen`` to
    intercept the wire format; this works because the
    ``verify_tls=True`` path calls ``self.urlopen`` directly.
    """

    def __init__(
        self,
        address: str,
        token: str | None,
        verify_tls: bool = True,
        tls: bool = False,
        cert: str | None = None,
        key: str | None = None,
        ca_cert: str | None = None,
    ) -> None:
        import ssl  # noqa: PLC0415
        import urllib.request  # noqa: PLC0415

        self.address = address.rstrip("/")
        self.token = token
        self.verify_tls = verify_tls
        self.tls = tls
        self.cert = cert
        self.key = key
        self.ca_cert = ca_cert

        # Store urlopen so tests can patch ``urllib.request.urlopen``
        # and intercept every request through ``self.urlopen``.
        self.urlopen = urllib.request.urlopen

        # Build custom opener based on TLS configuration:
        # - tls=False: plain HTTP (no TLS)
        # - tls=True, verify_tls=True, cert+key: mTLS with custom client certs
        # - tls=True, verify_tls=True: TLS with system CA certs
        # - tls=True, verify_tls=False: TLS with CERT_NONE (skip verification)
        # - verify_tls=False alone (tls=False): legacy behavior for dev with
        #   self-signed certs - uses HTTPSHandler with CERT_NONE even without tls=True
        self._opener: urllib.request.OpenerDirector | None = None
        self._ssl_context: ssl.SSLContext | None = None

        if not tls and verify_tls:
            # Plain HTTP, no TLS
            pass
        elif verify_tls and cert and key:
            # mTLS: custom SSL context with client cert + CA verification.
            # Defer loading cert/key/ca until first request so tests can
            # verify parameters without requiring real certificate files.
            ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ssl_context.check_hostname = True
            ssl_context.verify_mode = ssl.CERT_REQUIRED
            self._ssl_context = ssl_context
            self._opener = urllib.request.build_opener(
                urllib.request.HTTPSHandler(context=ssl_context),
            )
        elif verify_tls:
            # TLS with system CA certs (no client cert)
            self._opener = None  # use stdlib urlopen
        else:
            # verify_tls=False: skip cert verification (legacy dev mode)
            ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE
            self._opener = urllib.request.build_opener(
                urllib.request.HTTPSHandler(context=ssl_context),
            )

    def _request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Issue a single HTTP request and return the parsed JSON body.

        ``path`` is appended to ``self.address``; the ACL token is
        forwarded as the ``X-Nomad-Token`` header (Nomad's documented
        header for external clients — see
        https://developer.hashicorp.com/nomad/api-docs).
        """
        import urllib.error  # noqa: PLC0415
        import urllib.request  # noqa: PLC0415

        url = f"{self.address}{path}"
        data: bytes | None = None
        headers: dict[str, str] = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if self.token:
            # Nomad canonicalizes the header name to lowercase.
            headers["X-Nomad-Token"] = self.token
        request = urllib.request.Request(
            url,
            data=data,
            headers=headers,
            method=method,
        )
        try:
            # Lazily load certificates on first mTLS request.
            if self._ssl_context is not None:
                if self.ca_cert:
                    self._ssl_context.load_verify_locations(cafile=self.ca_cert)
                if self.cert and self.key:
                    self._ssl_context.load_cert_chain(certfile=self.cert, keyfile=self.key)
                self._ssl_context = None  # only load once

            if self._opener is not None:
                # verify_tls=False or mTLS: use the custom opener.
                with self._opener.open(request) as resp:
                    payload = resp.read()
            else:
                # verify_tls=True: use stdlib urlopen (system CA certs, test-mockable).
                with self.urlopen(request) as resp:
                    payload = resp.read()
        except urllib.error.HTTPError as exc:  # pragma: no cover — error path
            body_text = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(
                f"Nomad {method} {path} failed: HTTP {exc.code} {body_text!r}"
            ) from exc
        if not payload:
            return {}
        return cast(dict[str, Any], json.loads(payload.decode("utf-8")))

    def submit_job(self, spec: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/v1/jobs", body=spec)

    def register_job(self, spec: dict[str, Any]) -> dict[str, Any]:
        """Register (or update) a job spec via ``POST /v1/jobs``.

        Idempotent — re-registering an identical job is a no-op.
        Returns the Nomad response (``JobID``, ``EvalID``, ``Index``).
        """
        return self._request("POST", "/v1/jobs", body=spec)

    def dispatch_job(self, job_id: str, meta: dict[str, str] | None = None) -> dict[str, Any]:
        """Dispatch a parameterized job via ``POST /v1/job/{job_id}/dispatch``.

        ``meta`` is the per-dispatch payload that lands as
        ``NOMAD_META_<key>`` env vars inside the task container. Returns
        the Nomad dispatch response carrying ``JobID`` and ``EvalID`` of
        the child (dispatched) job.
        """
        body: dict[str, Any] = {}
        if meta:
            body["Meta"] = meta
        return self._request("POST", f"/v1/job/{job_id}/dispatch", body=body)

    def get_allocation(self, alloc_id: str) -> dict[str, Any]:
        return self._request("GET", f"/v1/allocation/{alloc_id}")

    def get_eval_allocations(self, eval_id: str) -> list[dict[str, Any]]:
        """Return the allocations created by an evaluation.

        ``GET /v1/evaluation/{eval_id}/allocations`` returns a list of
        allocation stubs. Each stub carries ``ID``, ``JobID``,
        ``ClientStatus``, etc.
        """
        result = self._request("GET", f"/v1/evaluation/{eval_id}/allocations")
        # The API returns a list, but ``_request`` casts to dict. When
        # the response is a JSON array we need to unwrap it.
        if isinstance(result, dict) and not result:
            return []
        if isinstance(result, list):
            return result
        return []

    def get_job_allocations(self, job_id: str) -> list[dict[str, Any]]:
        """Return all allocations for a job.

        ``GET /v1/job/{job_id}/allocations`` returns a list of
        allocation stubs.
        """
        result = self._request("GET", f"/v1/job/{job_id}/allocations")
        if isinstance(result, list):
            return result
        return []

    def resolve_allocation(self, eval_id: str, job_id: str, *, timeout_s: float = 30.0) -> str:
        """Resolve a submitted job's evaluation to its allocation ID.

        Nomad's ``POST /v1/jobs`` returns an ``EvalID`` but not the
        allocation ID directly. We resolve it by:

          1. Looking up allocations from the evaluation.
          2. Falling back to looking up allocations from the job.

        Returns the first allocation ID found, or raises ``RuntimeError``
        if no allocation is created within the polling window.
        """
        effective_timeout_s = max(float(timeout_s), 0.1)
        deadline = time.monotonic() + effective_timeout_s
        poll_delay = 0.5
        while time.monotonic() < deadline:
            # Try eval-based lookup first (fast path).
            allocs = self.get_eval_allocations(eval_id)
            if allocs:
                return str(allocs[0].get("ID", ""))
            # Fall back to job-based lookup.
            allocs = self.get_job_allocations(job_id)
            if allocs:
                return str(allocs[0].get("ID", ""))
            time.sleep(poll_delay)
            poll_delay = min(poll_delay * 1.5, 5.0)
        raise RuntimeError(
            f"No allocation created for eval={eval_id!r} job={job_id!r} "
            f"within {effective_timeout_s:.1f}s"
        )


class _NomadHandle(Handle):
    """Handle that polls Nomad on ``.result()``.

    Mirrors ``_AWSBatchHandle``: the work runs on a remote Nomad client
    (not a thread or submitit job), so we cannot back the Future with
    a local completion. Instead, the handle carries a reference to
    its executor and the Nomad ``jobId``; ``result()`` blocks on
    ``_wait_for_terminal`` and ``done()`` does a single non-blocking
    allocation lookup.

    The handle's ``_future`` is set when ``result()`` reaches a terminal
    state so concurrent callers don't re-poll. The base-class
    ``.result(timeout=...)`` / ``.done()`` paths remain reachable
    through the cached Future.
    """

    _GHOST_RETRIES = 3

    def __init__(
        self,
        job_id: str,
        eval_id: str,
        executor: "NomadExecutor",
        *,
        local_future: Future[Any] | None = None,
        result_hint: Any = None,
        result_transport_mode: str = "auto",
        result_storage_backend: str | None = None,
        result_storage_bucket: str | None = None,
        result_storage_prefix: str | None = None,
        result_storage_endpoint: str | None = None,
    ) -> None:
        self.job_id = job_id
        self._eval_id = eval_id
        self._allocation_id: str | None = None
        self._executor = executor
        self._local_future = local_future
        self._result_hint = result_hint
        self._result_transport_mode = coerce_transport_mode(result_transport_mode)
        self._result_storage_backend = result_storage_backend
        self._result_storage_bucket = result_storage_bucket
        self._result_storage_prefix = result_storage_prefix
        self._result_storage_endpoint = result_storage_endpoint
        self._future: Future[Any] = Future()
        # Worker tracking (issue #105): populate at submit time.
        # allocation_id is not yet resolved; worker_id uses the job ID
        # and is updated to the allocation ID when available.
        self.worker_id: str | None = job_id
        self.worker_ip: str | None = None
        self.worker_region: str | None = executor.datacentre

    def _ensure_allocation_id(self) -> str:
        """Lazily resolve the eval/job to a concrete allocation ID.

        The first call queries the Nomad API to resolve the evaluation
        into an allocation. The result is cached so subsequent calls
        return immediately.
        """
        if self._allocation_id is None:
            timeout_s = float(getattr(self._executor, "allocation_resolution_timeout_s", 30.0))
            try:
                self._allocation_id = self._executor._client.resolve_allocation(  # noqa: SLF001
                    eval_id=self._eval_id,
                    job_id=self.job_id,
                    timeout_s=timeout_s,
                )
            except TypeError:
                self._allocation_id = self._executor._client.resolve_allocation(  # noqa: SLF001
                    eval_id=self._eval_id,
                    job_id=self.job_id,
                )
            # Guard against resolve_allocation returning None or "None" (the
            # str(None) string, e.g. when resolve_allocation itself had a bug
            # and called str() on a None result).  Both are invalid allocation
            # IDs that would cause downstream NoneType errors.
            if self._allocation_id is None or self._allocation_id == "None":
                raise RuntimeError(
                    f"resolve_allocation returned {self._allocation_id!r} for eval={self._eval_id!r} "
                    f"job={self.job_id!r}; allocation could not be resolved"
                )
        return self._allocation_id

    def result(self, timeout: float | None = None) -> Any:
        start = time.monotonic()
        remaining: float | None = None
        alloc_id = self._ensure_allocation_id()
        try:
            if timeout is not None:
                elapsed = time.monotonic() - start
                remaining = timeout - elapsed
                if remaining <= 0:
                    raise TimeoutError(
                        f"Timed out after {elapsed:.1f}s waiting for allocation {alloc_id!r}"
                    )
            alloc = self._executor._wait_for_terminal(alloc_id, timeout=remaining)  # noqa: SLF001
        except Exception as exc:  # noqa: BLE001 — let KeyboardInterrupt/SystemExit propagate
            self._future.set_exception(exc)
            raise
        status = alloc.get("ClientStatus", "unknown")
        task_states = alloc.get("TaskStates", {}) or {}
        if status == "complete":
            local_result: Any = resolve_result_for_callback(
                self._result_hint,
                default=None,
                transport_mode=self._result_transport_mode,
            )
            local_result = materialize_object_storage_result(
                local_result,
                transport_mode=self._result_transport_mode,
                result_storage_backend=self._result_storage_backend,
                result_storage_bucket=self._result_storage_bucket,
                result_storage_prefix=self._result_storage_prefix,
                result_storage_endpoint=self._result_storage_endpoint,
            )
            if self._local_future is not None:
                local_result = self._local_future.result(timeout=timeout)
            self._future.set_result(local_result)
            return local_result
        # FAILED (or any non-complete terminal state — ``failed``,
        # ``lost``): re-raise with the most useful status description
        # we can extract from the task events. The Campaign's
        # `except Exception` path needs a string it can log.
        description = self._extract_failure_description(task_states)
        msg = f"Nomad allocation {alloc_id!r} {status}: {description}"
        self._future.set_exception(RuntimeError(msg))
        raise RuntimeError(msg)

    def done(self) -> bool:  # noqa: PLR0911
        # If the future is already finished (terminal status observed
        # by a prior ``result()`` call), report done without making
        # another HTTP call. This mirrors the base ``Handle.done()``
        # contract — a cached terminal state is authoritative.
        if self._future.done():
            return True
        # Allocation not resolved yet — not done.
        if self._allocation_id is None:
            try:
                self._ensure_allocation_id()
            except Exception as exc:  # noqa: BLE001
                log.warning("Polling error for %s: %s", self.job_id, exc)
                self.error = exc
                return False
        # Do a non-blocking allocation lookup. If the task is in a
        # terminal state, we've already finished; otherwise we're still
        # running. Ghost allocations (deleted or never-created) return
        # an empty dict — after N consecutive empty responses we raise
        # to break the indefinite-wait loop.
        assert self._allocation_id is not None  # guaranteed after _ensure
        for attempt in range(self._GHOST_RETRIES):
            try:
                alloc = self._executor._client.get_allocation(self._allocation_id)  # noqa: SLF001
            except Exception as exc:  # noqa: BLE001 — never raise from done()
                log.warning("Polling error for %s: %s", self.job_id, exc)
                self.error = exc
                return False
            if alloc:
                break
            log.debug(
                "Empty allocation for %s, attempt %d/%d",
                self.job_id,
                attempt + 1,
                self._GHOST_RETRIES,
            )
        else:
            # Ghost allocation: not found after _GHOST_RETRIES consecutive
            # empty responses. Per the base Handle.done() contract (base.py:100),
            # polling errors must be captured and returned as False, not raised.
            self.error = RuntimeError(
                f"Ghost job: job ID {self.job_id!r} not found after {self._GHOST_RETRIES} retries"
            )
            return False
        status = alloc.get("ClientStatus", "")
        if status not in ("complete", "failed", "lost"):
            return False
        # An allocation in a terminal Nomad state is "done" only when the
        # local future also finished without raising.  A FAILED/CANCELLED
        # local future must still report done() == False so that result()
        # gets called and propagates the error instead of silently succeeding.
        # Use result() instead of done() to distinguish FAILED (raises) from
        # COMPLETED (returns normally) per the done()/result() contract.
        # When there is no local future, we can only report done() == True
        # if the allocation itself succeeded (status == "complete").
        # Failed/lost allocations must return False so callers invoke result()
        # and receive the error.
        if self._local_future is None:
            return cast(bool, status == "complete")
        done: bool
        try:
            self._local_future.result()
            done = True
        except Exception:
            done = False
        return done

    @staticmethod
    def _extract_failure_description(task_states: dict[str, Any]) -> str:
        """Walk the task state events to find the first failure
        description (e.g. ``"Exit Code: 137 (OOM killed)"``).

        Nomad's failed-task events carry a human-readable
        ``Description``; we surface the first one so the Campaign
        log line is actionable. Falls back to ``"unknown reason"``
        if no description is available.
        """
        best: tuple[int, str] | None = None
        priority_by_type = {
            "Driver Failure": 50,
            "Failed Validation": 45,
            "Terminated": 40,
            "Not Restarting": 30,
            "Restarting": 10,
        }
        for state in task_states.values():
            if not isinstance(state, dict):
                continue
            for event in state.get("Events", []) or []:
                desc = (
                    event.get("Description") or event.get("DisplayMessage") or event.get("Message")
                )
                if desc:
                    message = str(desc)
                    event_type = str(event.get("Type", ""))
                    score = priority_by_type.get(event_type, 20)
                    if message.lower().startswith("task restarting"):
                        score = min(score, 5)
                    if best is None or score > best[0]:
                        best = (score, message)
        if best is not None:
            return best[1]
        return "unknown reason"


class NomadExecutor(BaseExecutor):
    """HashiCorp Nomad batch executor (issue #27, issue #135).

    Supports two dispatch modes:

    * **Dispatch mode**: registers a parameterized job spec once,
      then uses ``POST /v1/job/osimflow-worker/dispatch`` for per-sample
      work. Each ``submit()`` call dispatches a child job with the sample
      parameters as Nomad meta vars.
    * **Direct mode** (default/backward compatible): builds and submits a unique ``batch`` job
      per ``submit()`` call via ``POST /v1/jobs``. Used when the
      parameterized job is not yet registered or when ``use_dispatch`` is
      ``False``.

    Resource directives (``cpus``, ``memory_mb``, ``time_min``) are
    mapped to the Nomad ``resources`` block (``CPU`` in MHz, ``MemoryMB``
    in MB). Per-sample ``OSIMFLOW_OS_VERSION`` and ``OSIMFLOW_CONTAINER``
    are carried as task env vars — the same env vars ``SlurmExecutor``
    and ``AWSBatchExecutor`` export, so downstream work scripts can be
    substrate-agnostic.

    Security: the Nomad ACL token is sourced from the ``NOMAD_TOKEN``
    env var (the documented Nomad pattern for CI/automation). The
    constructor does **not** accept a ``token`` kwarg; passing a
    long-lived token would violate the same security policy the AWS
    Batch executor enforces. Similarly, no address is pinned — the
    ``NOMAD_ADDR`` env var (or constructor kwarg) decides.

    The HTTP transport is stdlib ``urllib.request``, lazy-imported
    inside ``_NomadClient`` so the local-executor / slurm-executor /
    aws-batch paths do not pay the import cost. Tests patch
    ``urllib.request.urlopen`` to mock the wire format.
    """

    name = "nomad"

    @property
    def requires_remote_runner_payload(self) -> bool:
        return True

    # The parameterized job ID used for dispatch mode.
    DISPATCH_JOB_ID = "osimflow-worker"
    _LEGACY_DISPATCH_POLICY_ALIASES: ClassVar[dict[str, str]] = {
        "direct": "keep_manual",
        "dispatch": "force_dispatch",
        "auto": "auto_prefer_dispatch",
    }
    _VALID_DISPATCH_POLICIES: ClassVar[set[str]] = {
        "keep_manual",
        "force_dispatch",
        "auto_prefer_dispatch",
    }

    def __init__(
        self,
        address: str | None = None,
        datacentre: str = "dc1",
        poll_interval_s: float = 5.0,
        max_poll_interval_s: float = 60.0,
        use_dispatch: bool = False,
        dispatch_policy: str | None = None,
        estimated_run_size: int | None = None,
        fanout_submit_rate_per_sec: float | None = None,
        fanout_submit_chunk_size: int = 0,
        allocation_resolution_timeout_s: float = 30.0,
        remote_results_only: bool = True,
        verify_tls: bool = True,
        tls: bool = False,
        cert: str | None = None,
        key: str | None = None,
        ca_cert: str | None = None,
    ):
        # Address precedence: explicit kwarg > NOMAD_ADDR env > 127.0.0.1.
        # Pinning the address in code would hard-code the deployment,
        # which is a portability trap.
        self.address = address or os.environ.get("NOMAD_ADDR") or "http://127.0.0.1:4646"
        # Token precedence: NOMAD_TOKEN env var only. The constructor
        # does NOT accept a token kwarg (see test_nomad_executor_does_not_accept_token_kwarg).
        self.datacentre = datacentre
        self.poll_interval_s = self._sanitize_positive_delay(poll_interval_s, fallback=5.0)
        max_interval = self._sanitize_positive_delay(max_poll_interval_s, fallback=60.0)
        self.max_poll_interval_s = max(max_interval, self.poll_interval_s)
        self._fanout_submit_rate_per_sec = (
            self._sanitize_positive_delay(fanout_submit_rate_per_sec, fallback=1.0)
            if fanout_submit_rate_per_sec is not None
            else None
        )
        self._fanout_submit_chunk_size = max(int(fanout_submit_chunk_size), 0)
        self.estimated_run_size = (
            max(int(estimated_run_size), 0) if estimated_run_size is not None else None
        )
        self._auto_dispatch_threshold = (
            self._fanout_submit_chunk_size if self._fanout_submit_chunk_size > 0 else 25
        )
        self._submit_count = 0
        self._active_waiters = 0
        self._waiters_lock = threading.Lock()
        if dispatch_policy is None:
            resolved_dispatch_policy = "force_dispatch" if use_dispatch else "keep_manual"
        else:
            resolved_dispatch_policy = self._LEGACY_DISPATCH_POLICY_ALIASES.get(
                dispatch_policy, dispatch_policy
            )
        if resolved_dispatch_policy not in self._VALID_DISPATCH_POLICIES:
            raise ValueError(
                "dispatch_policy must be one of: "
                "keep_manual, force_dispatch, auto_prefer_dispatch "
                "(legacy aliases: direct, dispatch, auto) "
                f"(got {resolved_dispatch_policy!r})"
            )
        self.dispatch_policy = resolved_dispatch_policy
        self._manual_dispatch_requested = bool(use_dispatch)
        self.use_dispatch = self._select_dispatch_mode()
        self.allocation_resolution_timeout_s = max(float(allocation_resolution_timeout_s), 0.1)
        self.remote_results_only = remote_results_only
        if not remote_results_only:
            warnings.warn(
                "Nomad local-callable compatibility mode (--no-nomad-remote-results-only) is deprecated "
                "and will be removed after one minor release. Migrate now by using the default "
                "remote-results mode and removing the compatibility flag.",
                DeprecationWarning,
                stacklevel=2,
            )
        self.verify_tls = verify_tls
        self.tls = tls
        self.cert = cert
        self.key = key
        self.ca_cert = ca_cert
        # SEC-009 (issue #1112): a bearer token sent over plain HTTP to a
        # non-local address can be intercepted by anyone on the network
        # path. TLS stays opt-in for backwards compatibility with dev
        # clusters, but non-local use without it is almost certainly a
        # misconfiguration — warn loudly on both the warnings channel and
        # the logger.
        if not tls and not self._is_local_address(self.address):
            warnings.warn(
                f"Nomad TLS is DISABLED for non-local address {self.address}: "
                "the NOMAD_TOKEN ACL token is transmitted in cleartext and can "
                "be intercepted (SEC-009). Enable TLS with --nomad-tls and "
                "configure --nomad-cert/--nomad-key/--nomad-ca-cert.",
                UserWarning,
                stacklevel=2,
            )
            log.warning(
                "SEC-009: Nomad TLS disabled for non-local address %s — "
                "NOMAD_TOKEN transmitted in cleartext",
                self.address,
            )
        self._dispatch_job_registered = False
        # Compatibility mode:
        # - remote_results_only=True (default): do not run local callables; Handle.result()
        #   returns result_hint on terminal success, enabling fully remote flows.
        # - remote_results_only=False: legacy compatibility path that runs callables locally.
        self._local_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="osimflow-nomad")
        self._client = _NomadClient(
            address=self.address,
            token=os.environ.get("NOMAD_TOKEN"),
            verify_tls=verify_tls,
            tls=tls,
            cert=cert,
            key=key,
            ca_cert=ca_cert,
        )

    @staticmethod
    def _is_local_address(address: str) -> bool:
        """True when *address* points at the local machine (issue #1112).

        Loopback addresses are exempt from the cleartext-token warning
        because loopback traffic never leaves the host.
        """
        import urllib.parse  # noqa: PLC0415

        candidate = address if "//" in address else f"//{address}"
        try:
            hostname = (urllib.parse.urlsplit(candidate).hostname or "").lower()
        except ValueError:
            return False
        if not hostname:
            return False
        if hostname in {"localhost", "::1"} or hostname.startswith("127."):
            return True
        # Bracketed IPv6 loopback variants ([::1], [0:0:0:0:0:0:0:1]).
        return hostname in {"0:0:0:0:0:0:0:1"}

    @staticmethod
    def _sanitize_positive_delay(value: float | None, *, fallback: float) -> float:
        if value is None:
            return fallback
        try:
            delay = float(value)
        except (TypeError, ValueError):
            return fallback
        if not math.isfinite(delay) or delay <= 0:
            return fallback
        return delay

    def _select_dispatch_mode(self) -> bool:
        if self.dispatch_policy == "force_dispatch":
            return True
        if self.dispatch_policy == "keep_manual":
            return self._manual_dispatch_requested
        if self._manual_dispatch_requested:
            return True
        if self.estimated_run_size is not None:
            return self.estimated_run_size >= self._auto_dispatch_threshold
        return self._submit_count >= self._auto_dispatch_threshold

    def fanout_submit_chunk_size(self, total: int) -> int:
        """Return the bounded chunk size for Nomad fan-out submission."""
        if total <= 0:
            return 1
        chunk = self._fanout_submit_chunk_size
        if chunk <= 0:
            return total
        return min(total, max(1, chunk))

    @property
    def fanout_submit_rate_per_sec(self) -> float | None:
        """Return the fan-out submit rate in submissions per second."""
        return self._fanout_submit_rate_per_sec

    def fanout_submit_interval_s(self) -> float:
        """Return the per-submit pacing interval for Nomad fan-out submission."""
        rate = self._fanout_submit_rate_per_sec
        if rate is None or rate <= 0:
            return 0.0
        return 1.0 / rate

    @staticmethod
    def _resolve_nomad_image(
        *,
        container: str | None,
        openstudio_version: str | None,
    ) -> str:
        """Resolve task image for Nomad with local-tag preference + fallback.

        Resolution order:
        1) explicit submit(container=...)
        2) OSIMFLOW_NOMAD_PREFERRED_IMAGE env override
        3) OSIMFLOW_OPENSTUDIO_CONTAINER_IMAGE env override
        4) nrel/openstudio:<openstudio_version|latest>
        """
        if container:
            return container
        preferred = os.environ.get("OSIMFLOW_NOMAD_PREFERRED_IMAGE")
        if preferred:
            return preferred
        openstudio_image = os.environ.get("OSIMFLOW_OPENSTUDIO_CONTAINER_IMAGE")
        if openstudio_image:
            return openstudio_image
        tag = openstudio_version or "latest"
        return f"nrel/openstudio:{tag}"

    def _build_job_spec(
        self,
        *,
        name: str,
        cpus: int,
        memory_mb: int,
        container: str | None,
        openstudio_version: str | None,
        remote_command: str | None = None,
        task_payload: str | None = None,
        result_transport_mode: str | None = None,
        result_storage_backend: str | None = None,
        result_storage_bucket: str | None = None,
        result_storage_prefix: str | None = None,
        result_storage_endpoint: str | None = None,
    ) -> dict[str, Any]:
        """Build a Nomad ``batch`` job spec for one OpenStudio task.

        The spec uses a single task group with a single ``docker`` task
        that runs the standard NREL OpenStudio container (or a custom
        ``container`` tag if the caller passed one). Per-sample
        metadata travels in the job ``Meta`` block — useful when the
        cluster does not have shared storage for the seed model and
        the work script needs to fetch it.

        ``CPU`` is in MHz (Nomad resource model); a 1-cpu job is
        1000 MHz. ``MemoryMB`` is in MB. ``time_min`` is mapped to a
        task-group-level ``KillTimeout`` (Go duration string) so a
        runaway task is hard-killed by the Nomad client.
        """
        env: dict[str, str] = {}
        if openstudio_version is not None:
            env["OSIMFLOW_OS_VERSION"] = str(openstudio_version)
        if container is not None:
            env["OSIMFLOW_CONTAINER"] = container
        if task_payload is not None:
            env["OSIMFLOW_TASK_PAYLOAD"] = task_payload
        if result_transport_mode is not None:
            env["OSIMFLOW_RESULT_TRANSPORT_MODE"] = result_transport_mode
        if result_storage_backend is not None:
            env["OSIMFLOW_RESULT_STORAGE_BACKEND"] = result_storage_backend
        if result_storage_bucket is not None:
            env["OSIMFLOW_RESULT_STORAGE_BUCKET"] = result_storage_bucket
        if result_storage_prefix is not None:
            env["OSIMFLOW_RESULT_STORAGE_PREFIX"] = result_storage_prefix
        if result_storage_endpoint is not None:
            env["OSIMFLOW_RESULT_STORAGE_ENDPOINT"] = result_storage_endpoint

        image = self._resolve_nomad_image(
            container=container,
            openstudio_version=openstudio_version,
        )
        task_command = remote_command or "python -m osimflow.remote_runner"
        import uuid  # noqa: PLC0415

        job_id = _slugify_job_name(f"osimflow-{name}-{uuid.uuid4().hex[:8]}")

        return {
            "Job": {
                "ID": job_id,
                "Name": job_id,
                "Type": "batch",
                "Datacenters": [self.datacentre],
                "Meta": {
                    "OSIMFLOW_SAMPLE_NAME": name,
                    "OSIMFLOW_OS_VERSION": str(openstudio_version or ""),
                },
                "TaskGroups": [
                    {
                        "Name": "osimflow",
                        "Tasks": [
                            {
                                "Name": "osimflow",
                                "Driver": "docker",
                                "Config": {
                                    "image": image,
                                    "entrypoint": [
                                        "/bin/sh",
                                        "-c",
                                        task_command,
                                    ],
                                },
                                "Resources": {
                                    "CPU": int(cpus) * 1000,
                                    "MemoryMB": int(memory_mb),
                                },
                                "Restart": {
                                    "Attempts": 0,
                                },
                                "Env": env,
                            }
                        ],
                    }
                ],
            }
        }

    def _build_dispatch_job_spec(self) -> dict[str, Any]:
        """Build the parameterized job spec for dispatch mode (issue #135).

        Returns a Nomad ``batch`` job spec with ``ParameterizedJob`` set
        so the executor can dispatch child jobs via
        ``POST /v1/job/osimflow-worker/dispatch``.

        Security:
          * ``privileged = false`` — no host-level access.
          * Memory limited to 4096 MB, CPU to 2000 MHz (2 logical CPUs).
          * No host network, no bind mounts.
        """
        default_image = self._resolve_nomad_image(
            container=os.environ.get("OSIMFLOW_NOMAD_PREFERRED_IMAGE"),
            openstudio_version="3.11.0",
        )
        return {
            "Job": {
                "ID": self.DISPATCH_JOB_ID,
                "Name": self.DISPATCH_JOB_ID,
                "Type": "batch",
                "Datacenters": [self.datacentre],
                "ParameterizedJob": {
                    "MetaRequired": ["sample_id"],
                    "MetaOptional": [
                        "variables_json",
                        "openstudio_version",
                        "container_image",
                        "task_payload",
                        "result_transport_mode",
                        "result_storage_backend",
                        "result_storage_bucket",
                        "result_storage_prefix",
                        "result_storage_endpoint",
                    ],
                },
                "Meta": {
                    "variables_json": "{}",
                    "openstudio_version": "3.11.0",
                    "container_image": default_image,
                    "task_payload": "{}",
                    "result_transport_mode": "auto",
                    "result_storage_backend": "",
                    "result_storage_bucket": "",
                    "result_storage_prefix": "",
                    "result_storage_endpoint": "",
                },
                "TaskGroups": [
                    {
                        "Name": "osimflow",
                        "Tasks": [
                            {
                                "Name": "simulate",
                                "Driver": "docker",
                                "Config": {
                                    "image": default_image,
                                    "command": "/bin/sh",
                                    "args": ["-c", "python -m osimflow.remote_runner"],
                                    "privileged": False,
                                },
                                "Resources": {
                                    "CPU": 2000,
                                    "MemoryMB": 4096,
                                },
                                "Restart": {
                                    "Attempts": 0,
                                },
                                "Env": {},
                            }
                        ],
                    }
                ],
            }
        }

    def _ensure_dispatch_job_registered(self) -> None:
        """Register the parameterized job spec (idempotent).

        Called once on the first ``submit()`` when ``use_dispatch`` is
        True. Subsequent calls are no-ops.
        """
        if self._dispatch_job_registered:
            return
        spec = self._build_dispatch_job_spec()
        log.info("nomad: registering parameterized dispatch job %s", self.DISPATCH_JOB_ID)
        self._client.register_job(spec)
        self._dispatch_job_registered = True

    def _wait_for_terminal(
        self, allocation_id: str, timeout: float | None = None
    ) -> dict[str, Any]:
        """Poll ``GET /v1/allocation/<id>`` with exponential backoff
        until the allocation reaches a terminal state (``complete`` /
        ``failed`` / ``lost``). Returns the final allocation dict.

        Raises:
            TimeoutError: if *timeout* seconds elapse before a terminal state.
        """
        delay = self.poll_interval_s
        phase_offset = 0.0
        start = time.monotonic()
        with self._waiters_lock:
            self._active_waiters += 1
            active_waiters = self._active_waiters
        try:
            if active_waiters > 1:
                phase_offset = (sum(ord(ch) for ch in allocation_id) % 10) / 100.0
            while True:
                alloc = self._client.get_allocation(allocation_id)
                status = alloc.get("ClientStatus", "UNKNOWN")
                if status in ("complete", "failed", "lost"):
                    return alloc
                if timeout is not None:
                    elapsed = time.monotonic() - start
                    remaining = timeout - elapsed
                    if remaining <= 0:
                        raise TimeoutError(
                            f"Timed out after {elapsed:.1f}s waiting for allocation {allocation_id!r}"
                        )
                    sleep_for = min(delay + phase_offset, remaining, self.max_poll_interval_s)
                else:
                    sleep_for = min(delay + phase_offset, self.max_poll_interval_s)
                log.info(
                    "nomad poll alloc=%s status=%s (sleeping %.2fs, active_waiters=%d)",
                    allocation_id,
                    status,
                    sleep_for,
                    active_waiters,
                )
                time.sleep(sleep_for)
                concurrency_pressure = max(active_waiters - 8, 0) * 0.05
                rate_pressure = 0.0
                if self._fanout_submit_rate_per_sec is not None:
                    rate_pressure = max(self._fanout_submit_rate_per_sec - 10.0, 0.0) * 0.01
                backoff_factor = 1.6 + min(concurrency_pressure + rate_pressure, 0.4)
                delay = min(delay * backoff_factor, self.max_poll_interval_s)
        finally:
            with self._waiters_lock:
                self._active_waiters = max(self._active_waiters - 1, 0)

    def submit(
        self,
        fn: Callable[..., Any],
        *args: Any,
        name: str = "task",
        cpus: int = 1,
        memory_mb: int = 1024,
        time_min: int = 60,
        container: str | None = None,
        container_digest: str | None = None,
        openstudio_version: str | None = None,
        result_hint: Any = None,
        remote_command: str | None = None,
        result_transport_mode: str | None = None,
        result_storage_backend: str | None = None,
        result_storage_bucket: str | None = None,
        result_storage_prefix: str | None = None,
        result_storage_endpoint: str | None = None,
        variables_json: str | None = None,
        env: dict[str, str] | None = None,
        stdout_path: Any = None,
        stderr_path: Any = None,
        max_retries: int | None = None,
        worker_id: str | None = None,
        **kwargs: Any,
    ) -> Handle:
        # Assemble local_callable_kwargs from the explicit fields we received.
        # stdout_path, stderr_path, env are for the campaign's use (not Nomad's env).
        local_callable_kwargs: dict[str, Any] = {}
        del kwargs  # noqa: F841, ARG002

        step_name = self._infer_step_name(name)
        task_payload = self._build_task_payload(
            step_name=step_name,
            args=args,
            kwargs=local_callable_kwargs,
            result_hint=result_hint,
            name=name,
        )
        local_future: Future[Any] | None = None
        if not self.remote_results_only:
            local_future = self._local_pool.submit(fn, *args, **local_callable_kwargs)
        else:
            del fn, args
        self._submit_count += 1
        dispatch_mode = self._select_dispatch_mode()
        self.use_dispatch = dispatch_mode

        log.info(
            "nomad submit name=%s cpus=%d mem=%dMB time_min=%d container=%s dispatch=%s "
            "policy=%s threshold=%d count=%d remote_results_only=%s",
            name,
            cpus,
            memory_mb,
            time_min,
            container,
            dispatch_mode,
            self.dispatch_policy,
            self._auto_dispatch_threshold,
            self._submit_count,
            self.remote_results_only,
        )

        if dispatch_mode:
            # Dispatch mode: register the parameterized job once, then
            # dispatch per-sample work via POST /v1/job/{id}/dispatch.
            self._ensure_dispatch_job_registered()

            # Build the per-dispatch meta payload.
            image = self._resolve_nomad_image(
                container=container,
                openstudio_version=(str(openstudio_version) if openstudio_version else None),
            )
            meta: dict[str, str] = {
                "sample_id": _slugify_job_name(name),
                "openstudio_version": str(openstudio_version or ""),
                "container_image": image,
            }
            if variables_json is not None:
                meta["variables_json"] = (
                    variables_json
                    if isinstance(variables_json, str)
                    else json.dumps(variables_json)
                )
            meta["task_payload"] = task_payload
            meta["result_transport_mode"] = (
                str(result_transport_mode) if result_transport_mode is not None else "auto"
            )
            if result_storage_backend is not None:
                meta["result_storage_backend"] = str(result_storage_backend)
            if result_storage_bucket is not None:
                meta["result_storage_bucket"] = str(result_storage_bucket)
            if result_storage_prefix is not None:
                meta["result_storage_prefix"] = str(result_storage_prefix)
            if result_storage_endpoint is not None:
                meta["result_storage_endpoint"] = str(result_storage_endpoint)

            response = self._client.dispatch_job(self.DISPATCH_JOB_ID, meta=meta)
        else:
            # Legacy (direct) mode: build and submit a unique job per call.
            spec = self._build_job_spec(
                name=name,
                cpus=cpus,
                memory_mb=memory_mb,
                container=container,
                openstudio_version=openstudio_version,
                remote_command=(str(remote_command) if remote_command else None),
                task_payload=task_payload,
                result_transport_mode=(
                    str(result_transport_mode) if result_transport_mode is not None else "auto"
                ),
                result_storage_backend=(
                    str(result_storage_backend) if result_storage_backend is not None else None
                ),
                result_storage_bucket=(
                    str(result_storage_bucket) if result_storage_bucket is not None else None
                ),
                result_storage_prefix=(
                    str(result_storage_prefix) if result_storage_prefix is not None else None
                ),
                result_storage_endpoint=(
                    str(result_storage_endpoint) if result_storage_endpoint is not None else None
                ),
            )
            response = self._client.submit_job(spec)

        job_id = response.get("JobID") or (spec["Job"]["ID"] if not dispatch_mode else "")
        eval_id = response.get("EvalID", "")
        log.info("nomad submit_job -> jobId=%s evalId=%s", job_id, eval_id)

        # Return a lazy handle: the allocation is resolved from the
        # evaluation on first ``result()`` / ``done()`` call, so the
        # submit path stays fast. This matches the
        # LocalExecutor / SlurmExecutor / AWSBatchExecutor
        # ergonomics — ``submit()`` is non-blocking; ``result()``
        # blocks — and lets the Campaign's
        # ``.result(timeout=...)`` semantics work uniformly across
        # substrates.
        return _NomadHandle(
            job_id=job_id,
            eval_id=eval_id,
            executor=self,
            local_future=local_future,
            result_hint=result_hint,
            result_transport_mode=(
                str(result_transport_mode) if result_transport_mode is not None else "auto"
            ),
            result_storage_backend=(
                str(result_storage_backend) if result_storage_backend is not None else None
            ),
            result_storage_bucket=(
                str(result_storage_bucket) if result_storage_bucket is not None else None
            ),
            result_storage_prefix=(
                str(result_storage_prefix) if result_storage_prefix is not None else None
            ),
            result_storage_endpoint=(
                str(result_storage_endpoint) if result_storage_endpoint is not None else None
            ),
        )

    def shutdown(self) -> None:
        self._local_pool.shutdown(wait=True)

    @staticmethod
    def _infer_step_name(submit_name: str) -> str:
        lower = submit_name.lower()
        if lower.startswith("apply_"):
            return "apply"
        if lower.startswith("sim_"):
            return "sim"
        if lower.startswith("kpi_"):
            return "extract"
        if lower.startswith("aggregate"):
            return "aggregate"
        if lower.startswith("plots"):
            return "plots"
        return "unknown"


# ======================================================================
# Executor registry + entry-point plug-in discovery (issue #432)
# ======================================================================


class ExecutorRegistry:
    """Global registry that maps executor names to their classes.

    Mirrors the ``AlgorithmRegistry`` pattern: built-in executors are
    registered explicitly at import time, and third-party executors can be
    auto-discovered via ``entry_points`` by calling :meth:`discover_plugins`.

    The registry enables introspection (``list_available()``) and a uniform
    lookup path (``get(name)``) so that the CLI and Campaign can validate
    executor names without hard-coding a choices list.

    Health checks (issue #1024) are stored alongside each registered
    executor via :meth:`register_health_check`. The health module iterates
    the registry to dispatch one check per executor instead of hard-coding
    an executor list. The check functions themselves live in
    ``osimflow/health.py`` (kept there to avoid pulling the health module
    into every executor's import path); the registration call is the only
    cross-module coupling.

    Typical usage::

        cls = ExecutorRegistry.get("local")
        executor = cls(max_workers=4)

    For third-party executor packages, add this to ``pyproject.toml``::

        [project.entry-points."osimflow.executors"]
        my_exec = "my_package.executors:MyExecutor"
    """

    _registry: dict[str, type[BaseExecutor]] = {}
    _health_checks: dict[str, "Callable[[], CheckResult]"] = {}

    @classmethod
    def register(cls, name: str, executor_cls: type[BaseExecutor]) -> None:
        """Register *executor_cls* under *name*."""
        cls._registry[name] = executor_cls
        log.debug("registered executor %s -> %s", name, executor_cls.__qualname__)

    @classmethod
    def get(cls, name: str) -> type[BaseExecutor]:
        """Return the executor class registered under *name*.

        Raises
        ------
        ValueError
            If *name* is not registered, with a helpful message listing
            available executors.
        """
        if name not in cls._registry:
            available = ", ".join(sorted(cls._registry)) or "(none)"
            raise ValueError(f"unknown executor '{name}'. Available executors: {available}")
        return cls._registry[name]

    @classmethod
    def list_available(cls) -> list[str]:
        """Return the sorted list of registered executor names."""
        return sorted(cls._registry)

    @classmethod
    def register_health_check(cls, name: str, check_fn: "Callable[[], CheckResult]") -> None:
        """Register a health check for executor *name* (issue #1024).

        ``check_fn`` must be a zero-argument callable returning a
        ``CheckResult``. It runs in :meth:`osimflow.health.run_health_checks`
        for every registered executor.

        Raises
        ------
        ValueError
            If *name* is not a registered executor.
        """
        if name not in cls._registry:
            available = ", ".join(sorted(cls._registry)) or "(none)"
            raise ValueError(
                f"cannot register health check for '{name}': executor not registered. "
                f"Available executors: {available}"
            )
        cls._health_checks[name] = check_fn
        log.debug("registered health check for executor %s", name)

    @classmethod
    def get_health_check(cls, name: str) -> "Callable[[], CheckResult] | None":
        """Return the health check callable registered for *name*, or None."""
        return cls._health_checks.get(name)

    @classmethod
    def iter_health_checks(cls) -> "list[tuple[str, Callable[[], CheckResult]]]":
        """Return ``[(name, check_fn), ...]`` for every executor that has a check.

        Sorted by executor name for deterministic output. Executors
        registered without a health check are skipped — this lets us add
        a new executor before its check is in place without breaking the
        health subcommand (the regression test asserts coverage of all
        built-ins).
        """
        pairs: list[tuple[str, Callable[[], CheckResult]]] = []
        for name in sorted(cls._registry):
            check = cls._health_checks.get(name)
            if check is not None:
                pairs.append((name, check))
        return pairs

    @classmethod
    def clear_health_checks(cls) -> None:
        """Test helper: drop every registered health check.

        Production code never calls this. Tests use it to start from a
        clean slate when checking the registration loop in isolation.
        """
        cls._health_checks.clear()

    @classmethod
    def discover_plugins(cls) -> int:
        """Discover and auto-register executors from installed entry points.

        Scans the ``osimflow.executors`` entry point group and loads each
        entry point.  Loaded objects that are ``BaseExecutor`` subclasses are
        registered under the entry-point ``name``.

        The method is **safe** — if no plug-ins are found it silently returns
        ``0``.  Import or type errors for individual plug-ins are logged at
        ``WARNING`` level and skipped so a single broken plug-in never breaks
        the registry.

        Returns
        -------
        int
            The number of plug-ins successfully registered.
        """
        try:
            eps = list(entry_points(group=EXECUTOR_ENTRY_POINT_GROUP))
        except Exception:  # noqa: BLE001 — never crash on metadata issues
            return 0

        if not eps:
            return 0

        count = 0
        for ep in eps:
            try:
                obj = ep.load()
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "failed to load executor plug-in '%s' (%s): %s",
                    ep.name,
                    ep.value,
                    exc,
                )
                continue

            if not (isinstance(obj, type) and issubclass(obj, BaseExecutor)):
                log.warning(
                    "executor plug-in '%s' (%s) is not a BaseExecutor subclass — skipping",
                    ep.name,
                    ep.value,
                )
                continue

            cls.register(ep.name, obj)
            log.info("discovered executor plug-in '%s' -> %s", ep.name, ep.value)
            count += 1

        return count


# ======================================================================
# Register built-in executors
# ======================================================================
ExecutorRegistry.register("local", LocalExecutor)
ExecutorRegistry.register("slurm", SlurmExecutor)
ExecutorRegistry.register("aws_batch", AWSBatchExecutor)
ExecutorRegistry.register("nomad", NomadExecutor)
ExecutorRegistry.register("azure_batch", AzureBatchExecutor)
ExecutorRegistry.register("google_batch", GoogleBatchExecutor)
ExecutorRegistry.register("kubernetes", KubernetesExecutor)
ExecutorRegistry.register("pbs", PBSExecutor)
ExecutorRegistry.register("dask_jobqueue", DaskJobQueueExecutor)
ExecutorRegistry.register("docker_swarm", DockerSwarmExecutor)

# Discover third-party executor plug-ins (no-op when none installed).
ExecutorRegistry.discover_plugins()


# ---------------------------------------------------------------------------
# Health-check registration (issue #1024, fix #1053)
# ---------------------------------------------------------------------------
# The per-executor health checks live in ``osimflow.health`` to keep the
# health module out of every executor's import path. The health module
# also tries to register its checks at import time, but that path is
# order-dependent: if ``osimflow.health`` is imported before
# ``osimflow.executors`` (e.g. when pytest-xdist workers import the
# health module first), the registration silently no-ops because the
# registry is still empty. Calling it here — after every built-in
# executor is registered — guarantees the binding is in place regardless
# of import order. The call is idempotent (``_register_executor_health_checks``
# re-binds the same callables, so repeated invocations are safe).
try:
    from osimflow.health import _register_executor_health_checks  # noqa: PLC0415

    _register_executor_health_checks()
except Exception:  # noqa: BLE001 — never break executor import over a health wiring glitch
    pass
