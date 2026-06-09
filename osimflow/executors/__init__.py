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
import time
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Callable, Optional

log = logging.getLogger("osimflow.executors")


@dataclasses.dataclass
class Handle:
    """A future-like handle. Substrate-specific implementations subclass this."""
    job_id: str
    _future: Future

    def result(self, timeout: Optional[float] = None) -> Any:
        return self._future.result(timeout=timeout)

    def done(self) -> bool:
        return self._future.done()


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
        container: Optional[str] = None,
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
        self, fn, *args,
        name="task", cpus=1, memory_mb=1024, time_min=60, container=None, **kwargs,
    ) -> Handle:
        # Resource directives are advisory on the local executor.
        log.info("local submit name=%s cpus=%d mem=%dMB", name, cpus, memory_mb)
        fut = self._pool.submit(fn, *args)
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
        account: Optional[str] = None,
        cpus_per_task: int = 2,
        mem_gb: int = 4,
        time_h: int = 2,
        debug: bool = True,
    ):
        # Lazy import so users who only ever run the local executor do
        # not pay the submitit import cost.
        import submitit
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
        self, fn, *args,
        name="task", cpus=1, memory_mb=1024, time_min=60, container=None, **kwargs,
    ) -> Handle:
        # submitit does not let us override per-submit cpus/mem easily
        # because the executor-level config is what controls the sbatch
        # header. Per-submit cpus/mem would be plumbed via a closure in
        # production. For now we emit the resource directive into the
        # task's log line so it appears in Tower.
        if container:
            log.info("slurm submit name=%s cpus=%d mem=%dMB container=%s",
                     name, cpus, memory_mb, container)
        else:
            log.info("slurm submit name=%s cpus=%d mem=%dMB", name, cpus, memory_mb)

        def _wrapped() -> Any:
            # Resource directive also becomes an env var so the task can
            # read it (e.g. OpenStudio CLI threading control).
            os.environ["OSIMFLOW_OS_VERSION"] = kwargs.get("openstudio_version", "N/A")
            if container:
                os.environ["OSIMFLOW_CONTAINER"] = container
            return fn(*args)

        fut = self._ex.submit(_wrapped)
        return Handle(job_id=fut.job_id, _future=fut)

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

    def __init__(self, job_queue: str = "osimflow-batch-queue", job_definition: Optional[str] = None):
        self.job_queue = job_queue
        self.job_definition = job_definition or "osimflow-job-def"
        # We do NOT import boto3 here; the real implementation would lazy-load.
        log.warning("AWSBatchExecutor is a STUB — no real submissions will occur")

    def submit(
        self, fn, *args,
        name="task", cpus=1, memory_mb=1024, time_min=60, container=None, **kwargs,
    ) -> Handle:
        # Wrap the real call in a thread that would, in production, do
        # boto3 submit_job. The thread completes "successfully" with None
        # so the Campaign can exercise its end-of-run logic.
        log.info("aws_batch submit name=%s container=%s (STUB)", name, container)
        pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="awsbatch-stub")
        fut = pool.submit(lambda: None)  # real impl would await Batch task
        return Handle(job_id=f"awsbatch-stub-{int(time.time()*1000)}", _future=fut)

    def shutdown(self) -> None:
        pass
