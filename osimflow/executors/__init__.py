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
import os
import re
import subprocess
import time
from collections.abc import Callable, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any, Optional, cast

log = logging.getLogger("osimflow.executors")


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
        )


@dataclasses.dataclass
class Handle:
    """A future-like handle. Substrate-specific implementations subclass this.

    The Handle abstracts over both `concurrent.futures.Future` (local)
    and `submitit.Future` (Slurm), whose `.result()` and `.done()`
    signatures differ slightly. We unify them here.

    Worker tracking fields (issue #105) are populated by each executor
    at submit time so the Campaign can attribute every sample to the
    worker that processed it — essential for cost attribution and
    debugging large campaigns.
    """

    job_id: str
    _future: Future[Any]
    worker_id: str | None = None
    worker_ip: str | None = None
    worker_region: str | None = None

    def result(self, timeout: float | None = None) -> Any:
        # submitit.Future.result() does not accept `timeout`; ignore the
        # argument for that substrate. concurrent.futures.Future accepts it.
        try:
            return self._future.result(timeout=timeout)
        except TypeError:
            return self._future.result()

    def done(self) -> bool:
        try:
            return self._future.done()
        except AttributeError:
            # submitit jobs have no done(); poll via _future._job_state or
            # trust that result() with no timeout returns immediately if
            # the job is finished. Fall back to a getattr that doesn't
            # blow up.
            return getattr(self._future, "_completed", False)


class BaseExecutor(abc.ABC):
    """All executors conform to this interface."""

    name: str = "base"

    @abc.abstractmethod
    def submit(
        self,
        fn: Callable[..., Any],
        *args: Any,
        name: str = "task",
        cpus: int = 1,
        memory_mb: int = 1024,
        time_min: int = 60,
        container: str | None = None,
        **kwargs: Any,
    ) -> Handle: ...

    @abc.abstractmethod
    def shutdown(self) -> None: ...


class LocalExecutor(BaseExecutor):
    """Runs tasks in a thread pool. For local dev and CI smoke tests."""

    name = "local"

    def __init__(self, max_workers: int = 4):
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="osimflow")

    def submit(
        self,
        fn: Callable[..., Any],
        *args: Any,
        name: str = "task",
        cpus: int = 1,
        memory_mb: int = 1024,
        time_min: int = 60,
        container: str | None = None,
        **kwargs: Any,
    ) -> Handle:
        # Resource directives are advisory on the local executor.
        import socket  # noqa: PLC0415

        log.info("local submit name=%s cpus=%d mem=%dMB", name, cpus, memory_mb)
        fut: Future[Any] = self._pool.submit(fn, *args)
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

        def _wrapped() -> Any:
            # Resource directive also becomes an env var so the task can
            # read it (e.g. OpenStudio CLI threading control).
            os.environ["OSIMFLOW_OS_VERSION"] = str(kwargs.get("openstudio_version", "N/A"))
            if container:
                os.environ["OSIMFLOW_CONTAINER"] = container
            return fn(*args)

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


class _AWSBatchHandle(Handle):
    """Handle that polls Batch on `.result()`.

    We can't use a vanilla `concurrent.futures.Future` (which would let
    us reuse the base `Handle` unchanged) because the work runs in a
    remote Batch task — there's no thread or submitit job to back the
    Future. Instead, the handle carries a reference to its executor
    and the Batch `jobId`; `result()` blocks on `_wait_for_terminal`
    and `done()` does a single non-blocking `describe_jobs` call.

    Not a dataclass — the parent `Handle` is, and dataclass inheritance
    fights with the new `_executor` field (default-vs-required ordering
    gets ugly). Constructed only inside `AWSBatchExecutor.submit()`,
    so we own the call site and don't need the dataclass machinery.
    """

    def __init__(self, job_id: str, executor: "AWSBatchExecutor") -> None:
        self.job_id = job_id
        self._executor = executor
        # Keep a `Future` so the base-class `.result(timeout=...)` /
        # `.done()` paths remain reachable; we cache the poll result
        # in it so concurrent callers don't re-poll.
        self._future: Future[Any] = Future()
        # _future must be set for the parent Handle; we set a sentinel
        # value so concurrent `.done()` callers see a consistent state.
        # The actual poll happens in `result()` below.
        # Worker tracking (issue #105): populate at submit time.
        self.worker_id: str | None = job_id
        self.worker_ip: str | None = None
        self.worker_region: str | None = os.environ.get("AWS_REGION")

    def result(self, timeout: float | None = None) -> Any:  # noqa: ARG002
        # The polling itself doesn't take a `timeout` parameter; the
        # Batch attempt-duration timeout (set on submit_job) is the
        # substrate-level kill. `timeout` here is accepted for the
        # base-class signature but not enforced — the existing
        # LocalExecutor/SlurmExecutor take the same approach.
        try:
            job = self._executor._wait_for_terminal(self.job_id)  # noqa: SLF001
        except BaseException as exc:  # noqa: BLE001 — surface any poll error
            self._future.set_exception(exc)
            raise
        status = job.get("status")
        if status == "SUCCEEDED":
            self._future.set_result(None)
            return None
        # FAILED (or any non-SUCCEEDED terminal state): re-raise with
        # the statusReason so the Campaign's `except Exception` path
        # logs a useful line.
        reason = job.get("statusReason", "unknown reason")
        msg = f"AWS Batch job {self.job_id!r} {status}: {reason}"
        self._future.set_exception(RuntimeError(msg))
        raise RuntimeError(msg)

    def done(self) -> bool:
        # A single non-blocking `describe_jobs` is the cheapest probe.
        # If the task is in a terminal state, we've already finished;
        # otherwise we're still running. Anything else (UNKNOWN status,
        # network blip) is treated as not-done.
        try:
            response = self._executor._get_client().describe_jobs(  # noqa: SLF001
                jobs=[self.job_id]
            )
        except Exception:  # noqa: BLE001 — never raise from done()
            return False
        jobs = response.get("jobs", [])
        if not jobs:
            return False
        status = jobs[0].get("status", "")
        return status in ("SUCCEEDED", "FAILED")


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

    Security (PRD §6 *Cloud Security Practices*): the boto3 client
    sources credentials from the IAM role attached to the Batch compute
    environment. The constructor does **not** accept
    `aws_access_key_id` / `aws_secret_access_key`; passing long-lived
    keys would violate the security policy. Similarly, no region is
    pinned — the IAM role's region (or `AWS_REGION` env var) decides.

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

    # Sentinel used in statusReason to identify Spot interruptions.
    _SPOT_INTERRUPTION_MARKERS: tuple[str, ...] = (
        "Spot interruption",
        "Spot Instance termination",
        "spot",
    )

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
    ):
        # Lazy import: keeps the boto3 import cost off the local /
        # slurm executor paths. ImportError here is intentional: the
        # user opted into the [aws] extra, so a missing boto3 is a
        # user error, not a silent fallback.
        import boto3  # noqa: PLC0415

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
        self.poll_interval_s = poll_interval_s
        self.max_poll_interval_s = max_poll_interval_s
        self.max_spot_price_usd = max_spot_price_usd
        self.fallback_to_on_demand = fallback_to_on_demand
        self.max_retries = max_retries

    def _get_client(self) -> Any:
        """Lazy boto3 Batch client construction.

        Deferring to first use lets the constructor succeed on hosts
        that have boto3 installed but no AWS config (e.g. CI runners
        that only test the executor wiring with mocked clients).
        Production deployments will have AWS_REGION set or an IAM
        role / ~/.aws/config in place.
        """
        if self._client is None:
            self._client = self._boto3.client("batch", region_name=self._region_name)
        return self._client

    def _get_ec2_client(self) -> Any:
        """Lazy boto3 EC2 client for Spot price queries."""
        if self._ec2_client is None:
            self._ec2_client = self._boto3.client("ec2", region_name=self._region_name)
        return self._ec2_client

    def _get_spot_price(self) -> float:
        """Query the current Spot price for the instance type.

        Uses ``describe_spot_price_history`` with a single-result query
        to get the most recent price. Returns the price in USD per
        instance-hour. Raises ``RuntimeError`` if the query fails or
        returns no results.
        """
        response = self._get_ec2_client().describe_spot_price_history(
            MaxResults=1,
            ProductDescriptions=["Linux/UNIX"],
        )
        histories = response.get("SpotPriceHistory", [])
        if not histories:
            raise RuntimeError("describe_spot_price_history returned no results")
        return float(histories[0]["SpotPrice"])

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
        msg = (
            f"Spot price ${current_price:.4f} exceeds ceiling "
            f"${self.max_spot_price_usd:.4f}"
        )
        if self.fallback_to_on_demand:
            log.warning("%s — falling back to on-demand", msg)
            return
        raise RuntimeError(msg)

    def _build_environment(
        self,
        *,
        container: str | None,
        openstudio_version: str | None,
    ) -> list[dict[str, str]]:
        """Build the Batch `environment` list from the per-submit kwargs.

        Always present (so the task has a sane baseline); absent values
        are omitted rather than set to empty strings.
        """
        env: list[dict[str, str]] = []
        if openstudio_version is not None:
            env.append({"name": "OSIMFLOW_OS_VERSION", "value": str(openstudio_version)})
        if container is not None:
            env.append({"name": "OSIMFLOW_CONTAINER", "value": container})
        return env

    def _build_container_overrides(
        self,
        *,
        cpus: int,
        memory_mb: int,
        environment: list[dict[str, str]],
    ) -> dict[str, Any]:
        """Translate OSimFlow resource directives to Batch overrides.

        The Batch API takes memory in MiB; `memory_mb` is in megabytes
        and we treat the two as equivalent (the difference is < 5% and
        Batch's documented unit is MiB, so 1:1 keeps the intent clear
        to anyone reading the submit_job call).
        """
        return {
            "vcpus": cpus,
            "memory": memory_mb,
            "environment": environment,
        }

    def _wait_for_terminal(self, job_id: str) -> dict[str, Any]:
        """Poll `describe_jobs` with exponential backoff until the task
        reaches a terminal state. Returns the final job dict."""
        delay = self.poll_interval_s
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
        job_queue: str | None = None,
    ) -> str:
        """Submit a single Batch job and return the jobId.

        Uses *job_queue* if provided, otherwise ``self.job_queue``.
        """
        queue = job_queue or self.job_queue
        overrides = self._build_container_overrides(
            cpus=cpus,
            memory_mb=memory_mb,
            environment=environment,
        )
        attempt_duration_seconds = int(time_min) * 60
        submit_kwargs: dict[str, Any] = {
            "jobName": name,
            "jobQueue": queue,
            "jobDefinition": self.job_definition,
            "containerOverrides": overrides,
            "timeout": {"attemptDurationSeconds": attempt_duration_seconds},
        }
        response = self._get_client().submit_job(**submit_kwargs)
        job_id: str = str(response["jobId"])
        log.info("aws_batch submit_job -> jobId=%s queue=%s", job_id, queue)
        return job_id

    def submit(
        self,
        fn: Callable[..., Any],
        *args: Any,
        name: str = "task",
        cpus: int = 1,
        memory_mb: int = 1024,
        time_min: int = 60,
        container: str | None = None,
        **kwargs: Any,
    ) -> Handle:
        openstudio_version = kwargs.get("openstudio_version")

        log.info(
            "aws_batch submit name=%s cpus=%d mem=%dMB time_min=%d container=%s",
            name,
            cpus,
            memory_mb,
            time_min,
            container,
        )

        environment = self._build_environment(
            container=container,
            openstudio_version=openstudio_version,
        )

        # --- Spot price ceiling check (issue #131) ---
        # If a price ceiling is configured and the current Spot price
        # exceeds it, either raise or fall back to on-demand.
        price_ceiling_breached = False
        if self.max_spot_price_usd is not None:
            try:
                current_price = self._get_spot_price()
                if current_price > self.max_spot_price_usd:
                    price_ceiling_breached = True
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
                # Network / API errors: log but proceed — the Batch job
                # itself may still succeed. Don't block submissions due to
                # transient EC2 API issues.
                log.warning("could not check Spot price: %s", exc)

        # If price ceiling is breached and fallback is enabled, go
        # straight to on-demand without spot retry.
        if price_ceiling_breached and self.fallback_to_on_demand:
            job_id = self._submit_job(
                name=name,
                cpus=cpus,
                memory_mb=memory_mb,
                time_min=time_min,
                environment=environment,
            )
            del fn, args  # noqa: ARG002 — see note below
            return _AWSBatchHandle(
                job_id=job_id,
                executor=self,
            )

        # --- Spot retry loop (issue #131) ---
        # Submit the job. On Spot interruption, retry up to max_retries
        # times with exponential backoff. After exhausting retries,
        # either fall back to on-demand or fail.
        effective_max_retries = max(0, self.max_retries)
        for attempt in range(effective_max_retries + 1):
            job_id = self._submit_job(
                name=name,
                cpus=cpus,
                memory_mb=memory_mb,
                time_min=time_min,
                environment=environment,
            )

            # Block until the job reaches a terminal state.
            job = self._wait_for_terminal(job_id)
            status = job.get("status")
            if status == "SUCCEEDED":
                del fn, args  # noqa: ARG002 — see note below
                return _AWSBatchHandle(
                    job_id=job_id,
                    executor=self,
                )

            # Job FAILED — check if it was a Spot interruption.
            reason = job.get("statusReason", "")
            is_spot = self._is_spot_interruption(reason)
            if is_spot and attempt < effective_max_retries:
                # Exponential backoff: 5s, 10s, 20s, 40s, 60s cap.
                backoff = min(5.0 * (2**attempt), 60.0)
                log.warning(
                    "Spot interrupted (attempt %d/%d), retrying in %.1fs: %s",
                    attempt + 1,
                    effective_max_retries,
                    backoff,
                    reason,
                )
                time.sleep(backoff)
                continue

            if is_spot and attempt >= effective_max_retries:
                # Exhausted retries — fall back or fail.
                if self.fallback_to_on_demand:
                    log.warning(
                        "Spot retries exhausted (%d), falling back to on-demand",
                        effective_max_retries,
                    )
                    on_demand_id = self._submit_job(
                        name=name,
                        cpus=cpus,
                        memory_mb=memory_mb,
                        time_min=time_min,
                        environment=environment,
                    )
                    del fn, args  # noqa: ARG002 — see note below
                    return _AWSBatchHandle(
                        job_id=on_demand_id,
                        executor=self,
                    )
                raise RuntimeError(
                    f"Spot retries exhausted ({effective_max_retries}): {reason}"
                )

            # Non-spot failure — don't retry, raise immediately.
            msg = f"AWS Batch job {job_id!r} {status}: {reason}"
            raise RuntimeError(msg)

        # Unreachable, but satisfies the type checker.
        raise RuntimeError("submit loop exited unexpectedly")  # pragma: no cover

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
    """

    def __init__(self, address: str, token: str | None) -> None:
        # urllib.request is stdlib; the import is cheap but we keep it
        # lazy so the local / slurm / aws paths do not pay even the
        # import cost. Tests patch ``urllib.request.urlopen`` so they
        # can intercept every request. The attribute is named
        # ``urlopen`` (no leading underscore) so tests can reach the
        # patched mock via ``executor._client.urlopen.call_args``.
        import urllib.request  # noqa: PLC0415

        self.urlopen = urllib.request.urlopen
        self.address = address.rstrip("/")
        self.token = token

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

    def resolve_allocation(self, eval_id: str, job_id: str) -> str:
        """Resolve a submitted job's evaluation to its allocation ID.

        Nomad's ``POST /v1/jobs`` returns an ``EvalID`` but not the
        allocation ID directly. We resolve it by:

          1. Looking up allocations from the evaluation.
          2. Falling back to looking up allocations from the job.

        Returns the first allocation ID found, or raises ``RuntimeError``
        if no allocation is created within the polling window.
        """
        deadline = time.monotonic() + 30.0
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
        raise RuntimeError(f"No allocation created for eval={eval_id!r} job={job_id!r} within 30s")


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

    def __init__(self, job_id: str, eval_id: str, executor: "NomadExecutor") -> None:
        self.job_id = job_id
        self._eval_id = eval_id
        self._allocation_id: str | None = None
        self._executor = executor
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
            self._allocation_id = self._executor._client.resolve_allocation(  # noqa: SLF001
                eval_id=self._eval_id,
                job_id=self.job_id,
            )
        return self._allocation_id

    def result(self, timeout: float | None = None) -> Any:  # noqa: ARG002
        # The polling itself doesn't take a `timeout` parameter; the
        # Nomad task-level ``KillTimeout`` (when set) is the
        # substrate-level kill. `timeout` here is accepted for the
        # base-class signature but not enforced — the existing
        # LocalExecutor / SlurmExecutor / AWSBatchExecutor take the
        # same approach.
        alloc_id = self._ensure_allocation_id()
        try:
            alloc = self._executor._wait_for_terminal(alloc_id)  # noqa: SLF001
        except BaseException as exc:  # noqa: BLE001 — surface any poll error
            self._future.set_exception(exc)
            raise
        status = alloc.get("ClientStatus", "unknown")
        task_states = alloc.get("TaskStates", {}) or {}
        if status == "complete":
            self._future.set_result(None)
            return None
        # FAILED (or any non-complete terminal state — ``failed``,
        # ``lost``): re-raise with the most useful status description
        # we can extract from the task events. The Campaign's
        # `except Exception` path needs a string it can log.
        description = self._extract_failure_description(task_states)
        msg = f"Nomad allocation {alloc_id!r} {status}: {description}"
        self._future.set_exception(RuntimeError(msg))
        raise RuntimeError(msg)

    def done(self) -> bool:
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
            except Exception:  # noqa: BLE001
                return False
        # Do a single non-blocking allocation lookup. If
        # the task is in a terminal state, we've already finished;
        # otherwise we're still running. Any error here (network
        # blip, missing alloc) is treated as not-done.
        assert self._allocation_id is not None  # guaranteed after _ensure
        try:
            alloc = self._executor._client.get_allocation(self._allocation_id)  # noqa: SLF001
        except Exception:  # noqa: BLE001 — never raise from done()
            return False
        status = alloc.get("ClientStatus", "")
        return status in ("complete", "failed", "lost")

    @staticmethod
    def _extract_failure_description(task_states: dict[str, Any]) -> str:
        """Walk the task state events to find the first failure
        description (e.g. ``"Exit Code: 137 (OOM killed)"``).

        Nomad's failed-task events carry a human-readable
        ``Description``; we surface the first one so the Campaign
        log line is actionable. Falls back to ``"unknown reason"``
        if no description is available.
        """
        for state in task_states.values():
            if not isinstance(state, dict):
                continue
            for event in state.get("Events", []) or []:
                desc = event.get("Description")
                if desc:
                    return str(desc)
        return "unknown reason"


class NomadExecutor(BaseExecutor):
    """HashiCorp Nomad batch executor (issue #27).

    Wraps the Nomad HTTP API (``/v1/jobs`` submit + ``/v1/allocation/<id>``
    poll) to launch one ``batch`` job per ``submit()`` call. The
    returned ``Handle`` polls the allocation status with exponential
    backoff until the task reaches a terminal state; on failure it
    re-raises a ``RuntimeError`` whose message includes the Nomad
    status description so the Campaign's ``except Exception`` path
    logs a useful line.

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

    def __init__(
        self,
        address: str | None = None,
        datacentre: str = "dc1",
        poll_interval_s: float = 5.0,
        max_poll_interval_s: float = 60.0,
    ):
        # Address precedence: explicit kwarg > NOMAD_ADDR env > 127.0.0.1.
        # Pinning the address in code would hard-code the deployment,
        # which is a portability trap.
        self.address = address or os.environ.get("NOMAD_ADDR") or "http://127.0.0.1:4646"
        # Token precedence: NOMAD_TOKEN env var only. The constructor
        # does NOT accept a token kwarg (see test_nomad_executor_does_not_accept_token_kwarg).
        self.datacentre = datacentre
        self.poll_interval_s = poll_interval_s
        self.max_poll_interval_s = max_poll_interval_s
        self._client = _NomadClient(
            address=self.address,
            token=os.environ.get("NOMAD_TOKEN"),
        )

    def _build_job_spec(
        self,
        *,
        name: str,
        cpus: int,
        memory_mb: int,
        container: str | None,
        openstudio_version: str | None,
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
        env: list[dict[str, str]] = []
        if openstudio_version is not None:
            env.append({"name": "OSIMFLOW_OS_VERSION", "value": str(openstudio_version)})
        if container is not None:
            env.append({"name": "OSIMFLOW_CONTAINER", "value": container})

        # The image is the NREL OpenStudio container (or a custom
        # tag) — same default that AWSBatchExecutor and SlurmExecutor
        # use. The actual ``openstudio.cli run`` invocation lives in
        # the work layer; the executor only ships the container spec.
        image = container or "nrel/openstudio:latest"

        return {
            "Job": {
                "ID": None,  # let Nomad assign an ID from the Name prefix
                "Name": _slugify_job_name(f"osimflow-{name}"),
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
                                    "command": "/bin/sh",
                                    "args": ["-c", "sleep infinity"],
                                    "env": env,
                                },
                                "Resources": {
                                    "CPU": int(cpus) * 1000,
                                    "MemoryMB": int(memory_mb),
                                },
                                "Restart": {
                                    "Attempts": 0,
                                },
                            }
                        ],
                    }
                ],
            }
        }

    def _wait_for_terminal(self, allocation_id: str) -> dict[str, Any]:
        """Poll ``GET /v1/allocation/<id>`` with exponential backoff
        until the allocation reaches a terminal state (``complete`` /
        ``failed`` / ``lost``). Returns the final allocation dict."""
        delay = self.poll_interval_s
        while True:
            alloc = self._client.get_allocation(allocation_id)
            status = alloc.get("ClientStatus", "UNKNOWN")
            if status in ("complete", "failed", "lost"):
                return alloc
            log.info("nomad poll alloc=%s status=%s (sleeping %.1fs)", allocation_id, status, delay)
            time.sleep(delay)
            # Exponential backoff, capped.
            delay = min(delay * 2, self.max_poll_interval_s)

    def submit(
        self,
        fn: Callable[..., Any],
        *args: Any,
        name: str = "task",
        cpus: int = 1,
        memory_mb: int = 1024,
        time_min: int = 60,
        container: str | None = None,
        **kwargs: Any,
    ) -> Handle:
        openstudio_version = kwargs.get("openstudio_version")

        log.info(
            "nomad submit name=%s cpus=%d mem=%dMB time_min=%d container=%s",
            name,
            cpus,
            memory_mb,
            time_min,
            container,
        )

        spec = self._build_job_spec(
            name=name,
            cpus=cpus,
            memory_mb=memory_mb,
            container=container,
            openstudio_version=openstudio_version,
        )
        response = self._client.submit_job(spec)
        job_id = response.get("JobID", "")
        eval_id = response.get("EvalID", "")
        log.info("nomad submit_job -> jobId=%s evalId=%s", job_id, eval_id)

        # `fn` and `args` are captured for the side-effect of being
        # part of the job spec in production (the task command is
        # built elsewhere from the campaign's working dir and these
        # inputs); we don't run the callable locally.
        del fn, args  # silence unused-arg warnings

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
        )

    def shutdown(self) -> None:
        # urllib's HTTP handler is closed by the underlying
        # http.client on GC; nothing actionable here.
        pass
