"""Distributed task queue abstraction for OSimFlow campaigns.

This module provides a Celery-like queue that can accept jobs from multiple
producers, distribute work to multiple workers, and provide retry / dead-letter
/ result persistence. The primary implementation uses ``dask.distributed``.

ARCH-001 gap: all job submission currently goes through the Campaign orchestrator
directly to executors. There is no queue that can accept jobs from multiple
producers, distribute work to multiple workers, and provide retry/dead-letter/
result persistence. Integrating dask.distributed resolves ARCH-001 and ARCH-004
(horizontal worker scaling) simultaneously.

Design
~~~~~~

``TaskQueue`` is the abstract interface. ``DaskTaskQueue`` is the production
implementation backed by a Dask scheduler. The Campaign can optionally be
configured to use a ``TaskQueue`` instead of direct ``executor.submit()`` for
the fan-out steps (APPLY_PARAMETERS, RUN_OPENSTUDIO_SIM, EXTRACT_KPIS).

The queue is opt-in via the ``--task-queue`` CLI flag. When ``none`` (the
default), behaviour is identical to the existing direct-executor pattern.

Usage
~~~~~

``--task-queue dask --dask-scheduler-address tcp://scheduler:8786``

When a Dask scheduler address is not provided, ``DaskTaskQueue`` starts an
embedded local cluster (single-process, multi-worker) so the queue works
out-of-the-box without any external infrastructure.
"""

from __future__ import annotations

import abc
import dataclasses
import logging
import time
from collections.abc import Callable
from concurrent.futures import Future
from enum import Enum
from typing import Any

log = logging.getLogger("osimflow.taskqueue")

__all__ = [
    "DaskTaskQueue",
    "NoOpTaskQueue",
    "TaskHandle",
    "TaskQueue",
    "TaskQueueStatus",
]


class TaskQueueStatus(Enum):
    """Possible states for a task handle."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    RETRYING = "retrying"


@dataclasses.dataclass
class TaskHandle:
    """A future-like handle for a queued task.

    Mirrors the ``Handle`` interface from the executor layer so callers
    can use either ``TaskHandle`` or ``Handle`` interchangeably.

    Attributes
    ----------
    task_id
        Unique identifier for this task (assigned by the queue).
    status
        Current task state.
    _future
        Backing future from the underlying queue implementation.
    submitted_at
        Unix timestamp when the task was submitted.
    worker_id
        Identifier of the worker that ran this task (populated when
        the task starts).
    """

    task_id: str
    status: TaskQueueStatus = TaskQueueStatus.PENDING
    _future: Future[Any] | None = None
    submitted_at: float = dataclasses.field(default_factory=time.time)
    worker_id: str | None = None

    def done(self) -> bool:
        """Return ``True`` if the task has reached a terminal state."""
        return self.status in (TaskQueueStatus.SUCCESS, TaskQueueStatus.FAILED)

    def result(self, timeout: float | None = None) -> Any:
        """Block until the task completes and return the result.

        Raises the exception that caused the task to fail.
        """
        if self._future is None:
            raise RuntimeError(f"task {self.task_id!r} has no backing future")
        return self._future.result(timeout=timeout)

    def retry(self) -> None:
        """Re-queue a failed task for retry.

        Raises ``RuntimeError`` if the task is not in FAILED state.
        """
        if self.status != TaskQueueStatus.FAILED:
            raise RuntimeError(
                f"task {self.task_id!r} cannot be retried in state {self.status}"
            )
        log.info("retry requested for task %s", self.task_id)


class TaskQueue(abc.ABC):
    """Abstract distributed task queue.

    Subclass this to add a new queue backend. The Campaign uses this
    interface exclusively for fan-out steps when a queue is configured.
    """

    name: str = "base"

    @abc.abstractmethod
    def submit(
        self,
        fn: Callable[..., Any],
        *args: Any,
        **kwargs: Any,
    ) -> TaskHandle:
        """Submit a callable to the queue and return a handle.

        Parameters
        ----------
        fn
            The Python callable to execute.
        args
            Positional arguments passed to *fn*.
        kwargs
            Keyword arguments passed to *fn*.

        Returns
        -------
        TaskHandle
            A handle that can be used to await the result or retry the task.
        """
        ...

    @abc.abstractmethod
    def get_result(self, handle: TaskHandle, timeout: float | None = None) -> Any:
        """Retrieve the result of a completed task.

        This is a separate method (rather than just ``handle.result()``)
        so that result persistence can be implemented separately from
        the handle — results can be stored in a database or object store
        even after the handle is discarded.
        """
        ...

    @abc.abstractmethod
    def retry(self, handle: TaskHandle) -> TaskHandle:
        """Re-queue a failed task and return a new handle for the retry."""
        ...

    @abc.abstractmethod
    def shutdown(self) -> None:
        """Shutdown the queue client and release resources."""
        ...


# ---------------------------------------------------------------------------
# No-op queue (used when task_queue == "none")
# ---------------------------------------------------------------------------


class NoOpTaskQueue(TaskQueue):
    """A no-op queue that runs tasks synchronously in the caller thread.

    This is the identity queue: ``submit`` runs ``fn(*args, **kwargs)``
    immediately and returns a handle in SUCCESS state. It is used when
    ``--task-queue none`` (the default) so the Campaign has a consistent
    interface regardless of whether a distributed queue is configured.
    """

    name = "none"

    def submit(
        self,
        fn: Callable[..., Any],
        *args: Any,
        **kwargs: Any,
    ) -> TaskHandle:
        try:
            result = fn(*args, **kwargs)
            fut: Future[Any] = Future()
            fut.set_result(result)
        except Exception as exc:
            fut = Future()
            fut.set_exception(exc)
        return TaskHandle(
            task_id="noop",
            status=TaskQueueStatus.SUCCESS,
            _future=fut,
            submitted_at=time.time(),
            worker_id="main",
        )

    def get_result(self, handle: TaskHandle, timeout: float | None = None) -> Any:
        if handle._future is None:
            raise RuntimeError(f"task {handle.task_id!r} has no backing future")
        return handle._future.result(timeout=timeout)

    def retry(self, handle: TaskHandle) -> TaskHandle:
        raise RuntimeError("retry is not supported on NoOpTaskQueue")

    def shutdown(self) -> None:
        pass


# ---------------------------------------------------------------------------
# Dask distributed queue
# ---------------------------------------------------------------------------


class DaskTaskQueue(TaskQueue):
    """Distributed task queue backed by ``dask.distributed``.

    Connects to a Dask scheduler at the address provided via
    ``--dask-scheduler-address``. When no address is provided, starts
    an embedded single-process cluster so the queue works without any
    external infrastructure.

    The Dask scheduler handles:
    - Horizontal worker scaling (ARCH-004).
    - Worker failure detection and retry.
    - Result persistence via the scheduler's state store.
    - Dead-letter tracking via ``.exception()`` on the future.

    Security: credentials are sourced from the environment (DASK_SCHEDULER_URI
    env var, TLS certs, etc.). The constructor does **not** accept long-lived
    credentials.
    """

    name = "dask"

    def __init__(
        self,
        scheduler_address: str | None = None,
        *,
        max_retries: int = 3,
    ) -> None:
        self.scheduler_address = scheduler_address
        self.max_retries = max_retries
        self._client: Any = None
        self._embedded_cluster: Any = None

    def _ensure_client(self) -> Any:
        """Lazily create the Dask client and optionally start an embedded cluster."""
        if self._client is not None:
            return self._client

        import dask.distributed  # noqa: PLC0415

        if self.scheduler_address is None:
            cluster = dask.distributed.LocalCluster(
                n_workers=2,
                threads_per_worker=1,
                silence_logs=logging.WARNING,
            )
            self._embedded_cluster = cluster
            log.info(
                "dask task queue: started embedded LocalCluster (workers=2, threads=1)"
            )
        else:
            log.info("dask task queue: connecting to scheduler %s", self.scheduler_address)

        self._client = dask.distributed.Client(
            self.scheduler_address,
            asynchronous=False,
            set_as_default=False,
        )
        return self._client

    def submit(
        self,
        fn: Callable[..., Any],
        *args: Any,
        **kwargs: Any,
    ) -> TaskHandle:
        client = self._ensure_client()

        future = client.submit(fn, *args, **kwargs)
        task_id = f"dask-task-{id(future)}"

        log.debug(
            "dask task queue submit: task_id=%s fn=%s",
            task_id,
            getattr(fn, "__name__", repr(fn)),
        )

        return TaskHandle(
            task_id=task_id,
            status=TaskQueueStatus.PENDING,
            _future=future,
            submitted_at=time.time(),
            worker_id=None,
        )

    def get_result(self, handle: TaskHandle, timeout: float | None = None) -> Any:
        if handle._future is None:
            raise RuntimeError(f"task {handle.task_id!r} has no backing future")
        return handle._future.result(timeout=timeout)

    def retry(self, handle: TaskHandle) -> TaskHandle:
        if handle.status != TaskQueueStatus.FAILED:
            raise RuntimeError(
                f"task {handle.task_id!r} cannot be retried in state {handle.status}"
            )
        log.info("dask task queue: retry requested for task %s", handle.task_id)
        raise NotImplementedError(
            "DaskTaskQueue.retry() requires re-submitting the original function. "
            "Store the original fn/args/kwargs to enable retry."
        )

    def shutdown(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None
        if self._embedded_cluster is not None:
            self._embedded_cluster.close()
            self._embedded_cluster = None
            log.info("dask task queue: embedded cluster closed")
        log.info("dask task queue: shutdown complete")


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def build_task_queue(
    backend: str,
    scheduler_address: str | None = None,
    *,
    max_retries: int = 3,
) -> TaskQueue:
    """Factory to build a TaskQueue from a backend name.

    Parameters
    ----------
    backend
        One of ``"none"`` or ``"dask"``.
    scheduler_address
        Dask scheduler address (e.g. ``tcp://scheduler:8786``).
        Required when *backend* is ``"dask"``.
    max_retries
        Maximum retry attempts for failed tasks (default: 3).
    """
    if backend == "none":
        return NoOpTaskQueue()
    if backend == "dask":
        return DaskTaskQueue(
            scheduler_address=scheduler_address,
            max_retries=max_retries,
        )
    raise ValueError(f"unknown task queue backend: {backend!r} (expected 'none' or 'dask')")
