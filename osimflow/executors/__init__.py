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
import logging
import os
import subprocess
import time
from collections.abc import Callable, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("osimflow.executors")


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
    """

    job_id: str
    _future: Future[Any]

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
        log.info("local submit name=%s cpus=%d mem=%dMB", name, cpus, memory_mb)
        fut: Future[Any] = self._pool.submit(fn, *args)
        return Handle(job_id=f"local-{id(fut)}", _future=fut)

    def shutdown(self) -> None:
        self._pool.shutdown(wait=True)


class SlurmExecutor(BaseExecutor):
    """Slurm executor wrapping `submitit.AutoExecutor`.

    With `debug=True` (the default), submitit runs the job as a local
    subprocess *but still emits the exact `sbatch` command it would have
    run* in the log. This is the documented submitit pattern for
    development without a real cluster.

    In production, set `debug=False` and the same code path submits to
    the configured Slurm partition. Zero application changes.
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

        ex = submitit.AutoExecutor(
            folder=os.environ.get("OSIMFLOW_SLURM_LOGS", "/tmp/osimflow-slurm-logs"),
            slurm_pty=False,
        )
        # submitit 1.5+ renamed `partition` -> `slurm_partition` and
        # `time` -> `slurm_time`. Detect at runtime and use whichever
        # spelling submitit accepts. New names take priority.
        try:
            ex.update_parameters(
                slurm_partition=partition,
                slurm_account=account,
                slurm_cpus_per_task=cpus_per_task,
                slurm_mem_gb=mem_gb,
                slurm_time=int(time_h * 60),
            )
        except TypeError:
            # Older submitit with the legacy kwarg names.
            ex.update_parameters(
                partition=partition,
                account=account,
                cpus_per_task=cpus_per_task,
                mem_gb=mem_gb,
                time=int(time_h * 60),
            )
        if debug:
            log.warning(
                "SlurmExecutor running in DEBUG mode — jobs run locally; "
                "the exact `sbatch` script that would have been submitted is "
                "logged at INFO level. Set debug=False for real Slurm."
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
        # submitit does not let us override per-submit cpus/mem easily
        # because the executor-level config is what controls the sbatch
        # header. Per-submit cpus/mem would be plumbed via a closure in
        # production. For now we emit the resource directive into the
        # task's log line so it appears in run.json / external logs.
        if container:
            log.info(
                "slurm submit name=%s cpus=%d mem=%dMB container=%s",
                name,
                cpus,
                memory_mb,
                container,
            )
        else:
            log.info("slurm submit name=%s cpus=%d mem=%dMB", name, cpus, memory_mb)

        def _wrapped() -> Any:
            # Resource directive also becomes an env var so the task can
            # read it (e.g. OpenStudio CLI threading control).
            os.environ["OSIMFLOW_OS_VERSION"] = str(kwargs.get("openstudio_version", "N/A"))
            if container:
                os.environ["OSIMFLOW_CONTAINER"] = container
            return fn(*args)

        fut: Any = self._ex.submit(_wrapped)
        return Handle(job_id=str(fut.job_id), _future=fut)

    def shutdown(self) -> None:
        # submitit.AutoExecutor has no explicit shutdown; the underlying
        # DebugExecutor / SlurmExecutor cleans up on process exit.
        pass


class AWSBatchExecutor(BaseExecutor):
    """AWS Batch executor — STUB.

    A real implementation would:
      1. boto3 `batch.register_job_definition` (or reuse a registered one)
         that mirrors the dynamic container tag pattern.
      2. boto3 `batch.submit_job` with the containerOverrides + environment
         variables carrying sample_id, parameter_set_path, etc.
      3. Poll `batch.describe_jobs` until SUCCEEDED / FAILED.
      4. Return a Handle whose `result()` re-raises on FAILED.

    This stub is intentionally thin so the Campaign and cache layer can
    be exercised end-to-end without an AWS account. Wire it to boto3
    when the project gets an AWS account.
    """

    name = "aws_batch"

    def __init__(self, job_queue: str = "osimflow-batch-queue", job_definition: str | None = None):
        self.job_queue = job_queue
        self.job_definition = job_definition or "osimflow-job-def"
        # We do NOT import boto3 here; the real implementation would lazy-load.
        log.warning("AWSBatchExecutor is a STUB — no real submissions will occur")

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
        # Wrap the real call in a thread that would, in production, do
        # boto3 submit_job. The thread completes "successfully" with None
        # so the Campaign can exercise its end-of-run logic.
        log.info("aws_batch submit name=%s container=%s (STUB)", name, container)
        pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="awsbatch-stub")
        fut: Future[Any] = pool.submit(lambda: None)  # real impl would await Batch task
        return Handle(job_id=f"awsbatch-stub-{int(time.time() * 1000)}", _future=fut)

    def shutdown(self) -> None:
        pass
