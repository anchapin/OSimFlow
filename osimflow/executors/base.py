"""Shared executor primitives: BaseExecutor and Handle.

These are in a separate module to avoid circular import issues when
per-executor modules (e.g. azure_batch_executor.py) need to inherit
from them before the full __init__.py is initialized.
"""

from __future__ import annotations

import abc
import dataclasses
from collections.abc import Callable
from concurrent.futures import Future
from typing import Any


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
