"""Shared executor primitives: BaseExecutor, Handle, and SubmitRequest.

These are in a separate module to avoid circular import issues when
per-executor modules (e.g. azure_batch_executor.py) need to inherit
from them before the full __init__.py is initialized.
"""

from __future__ import annotations

__all__ = ["BaseExecutor", "Handle"]

import abc
import dataclasses
from collections.abc import Callable
from concurrent.futures import Future
from typing import Any


@dataclasses.dataclass
class SubmitRequest:
    """Explicit typed submission request for BaseExecutor.submit().

    Replaces the implicit ``**kwargs`` contract with an explicit dataclass
    so each executor knows precisely which fields are available and the
    type checker enforces that all required fields are present (issue #725).

    Callers should construct this object and pass it to
    ``executor.submit(request)``.  The dataclass also carries
    executor-specific fields (``result_hint``, ``result_transport_mode``,
    etc.) that are consumed by specific executors — unused fields are
    simply ignored by executors that don't need them.
    """

    fn: Callable[..., Any]
    args: tuple[Any, ...]
    name: str = "task"
    cpus: int = 1
    memory_mb: int = 1024
    time_min: int = 60
    container: str | None = None
    openstudio_version: str | None = None

    # ---- Executor-specific optional fields ----
    # Consumed by cloud/Batch executors (AWS, Azure, Google, Nomad, K8s).
    result_hint: Any = None
    # Nomad-specific: remote command override.
    remote_command: str | None = None
    # Result transport / object-storage fields (Nomad, cloud executors).
    result_transport_mode: str | None = None
    result_storage_backend: str | None = None
    result_storage_bucket: str | None = None
    result_storage_prefix: str | None = None
    result_storage_endpoint: str | None = None
    # Nomad-specific: JSON-encoded variable overrides.
    variables_json: str | None = None
    # PBS debug mode: env vars to propagate.
    env: dict[str, str] | None = None
    # Per-sample log paths (campaign.py → LocalExecutor).
    stdout_path: Any = None
    stderr_path: Any = None
    # Retry / worker tracking.
    max_retries: int | None = None
    worker_id: str | None = None


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

    Error tracking (issue #721): polling errors are captured here so
    callers can distinguish "still running" from "failed with error".
    """

    job_id: str
    _future: Future[Any]
    worker_id: str | None = None
    worker_ip: str | None = None
    worker_region: str | None = None
    cost_usd: float | None = None
    billed_duration_seconds: float | None = None
    error: Exception | None = None

    def result(self, timeout: float | None = None) -> Any:
        if self.error is not None:
            raise self.error
        try:
            return self._future.result(timeout=timeout)
        except TypeError:
            return self._future.result()

    def done(self) -> bool:
        try:
            return self._future.done()
        except AttributeError:
            return getattr(self._future, "_completed", False)

    def is_failed(self) -> bool:
        """Return True if a polling error has been captured."""
        return self.error is not None


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
    ) -> Handle: ...

    @abc.abstractmethod
    def shutdown(self) -> None: ...

    def cancel(self) -> None:
        """Cancel all active futures (issue #255).

        Override in subclasses that manage their own job queues
        (Slurm, AWS Batch) to send cancellation signals to the
        underlying substrate. The base implementation is a no-op
        for executors that do not need explicit cancellation.
        """
        return None

    def fanout_submit_chunk_size(self, total: int) -> int:
        """Return the bounded chunk size for fan-out submission.

        Override in executors that need to limit the number of
        concurrent submissions (e.g., Nomad's rate-limiting).
        The default returns *total* (no chunking).
        """
        return total

    def get_bounded_fanout_chunk_size(self, total: int) -> int:
        """Return the bounded chunk size; delegates to fanout_submit_chunk_size.

        This method exists so NomadExecutor can expose a
        fanout_submit_chunk_size property without losing the bounded
        computation. Subclasses that override fanout_submit_chunk_size
        should not need to override this.
        """
        return self.fanout_submit_chunk_size(total)

    def fanout_submit_interval_s(self) -> float:
        """Return the per-submit pacing interval for fan-out submission.

        Override in executors that need to pace submissions
        (e.g., Nomad's submit rate limiting). The default
        returns 0.0 (no pacing).
        """
        return 0.0
