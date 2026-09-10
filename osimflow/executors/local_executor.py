"""Local thread-pool executor for OSimFlow campaigns.

Runs each step callable in a ``concurrent.futures`` thread pool — the
dev/CI substrate. Extracted from ``osimflow/executors/__init__.py``
(issue #1463) so the package init holds only the registry and
re-exports. Also re-exports :func:`run_subprocess`, the per-sample
log capture helper (issue #6; canonical home is
``osimflow/_subprocess_utils.py``, issue #910).
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any
from unittest.mock import patch

from osimflow._subprocess_utils import (
    run_subprocess,  # noqa: F401 — re-exported for the historical import path
    terminate_active_subprocesses,
)
from osimflow.executors.base import BaseExecutor, Handle
from osimflow.executors.transport import ResultTransportConfig, validate_transport_mode

__all__ = ["LocalExecutor", "run_subprocess"]

log = logging.getLogger("osimflow.executors")


class LocalExecutor(BaseExecutor):
    """Runs tasks in a thread pool. For local dev and CI smoke tests."""

    name = "local"

    #: Local execution has no substrate quota to bump against — set the
    #: default to ``inf`` so the shared limiter is constructed as a no-op
    #: (issue #1563). The base class default is ``None`` so we record
    #: the policy decision explicitly here.
    default_submit_rps: float | None = float("inf")

    def __init__(
        self,
        max_workers: int | None = None,
        max_concurrent_samples: int | None = None,
        *,
        submit_rps: float | None = None,
    ):
        if max_workers is None:
            import os

            max_workers = os.cpu_count() or 4
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="osimflow")
        if max_concurrent_samples is not None:
            self._semaphore: threading.Semaphore | None = threading.BoundedSemaphore(
                max_concurrent_samples
            )
        else:
            self._semaphore = None
        # Issue #1563: shared rate limiter (no-op at the default
        # ``inf``). Users that want to artificially throttle local
        # fan-out can pass ``submit_rps=20`` (for example) for
        # conformance checks.
        self._init_rate_limiter(submit_rps)

    def _do_submit(
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
        transport: ResultTransportConfig | None = None,
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
        # Issue #1473: validate the transport capability matrix instead
        # of silently discarding an unsupported mode.
        validate_transport_mode(self.name, transport.mode if transport is not None else None)
        _unused = [
            ("openstudio_version", openstudio_version),
            ("result_hint", result_hint),
            ("remote_command", remote_command),
            ("result_storage_backend", transport.backend if transport else None),
            ("result_storage_bucket", transport.bucket if transport else None),
            ("result_storage_prefix", transport.prefix if transport else None),
            ("result_storage_endpoint", transport.endpoint if transport else None),
            ("variables_json", variables_json),
            ("stdout_path", stdout_path),
            ("stderr_path", stderr_path),
            ("max_retries", max_retries),
            ("worker_id", worker_id),
        ]
        for kw_name, kw_value in _unused:
            if kw_value is not None:
                log.warning(
                    "LocalExecutor.submit: %s is not supported locally and will be ignored (value=%r)",
                    kw_name,
                    kw_value,
                )
        if kwargs:
            log.warning(
                "LocalExecutor.submit: %d unexpected kwargs ignored: %s",
                len(kwargs),
                list(kwargs.keys()),
            )

        log.info("local submit name=%s cpus=%d mem=%dMB", name, cpus, memory_mb)

        if cpus > 1 or memory_mb > 1024:
            log.warning(
                "LocalExecutor.submit: cpus=%d and memory_mb=%d are advisory only — "
                "ThreadPoolExecutor does not enforce per-task resource limits. "
                "For hard limits use SlurmExecutor or AWSBatchExecutor.",
                cpus,
                memory_mb,
            )

        if env:

            def _with_env() -> Any:
                # Issue #1406: replace the racy ``os.environ.clear()`` /
                # ``os.environ.update(...)`` finally clause with a
                # ``unittest.mock.patch.dict`` context manager. It is
                # stdlib, transitive-dep-free, recursive-safe (nested
                # ``with patch.dict(...)`` blocks compose correctly),
                # and guarantees save/restore even when ``fn(*args)``
                # raises. ``clear=False`` preserves the original
                # merge semantic where the supplied ``env`` overrides
                # pre-existing ``os.environ`` entries without wiping
                # unmentioned vars. Snapshot mutation races against
                # other threads remain inherent to ``os.environ`` being
                # process-shared — callers must not rely on cross-thread
                # ``os.environ`` reads inside ``fn`` for correctness.
                with patch.dict(os.environ, env, clear=False):
                    return fn(*args)

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

    def cancel(self) -> None:
        """Cancel queued futures, then terminate in-flight work subprocesses
        (issues #1538, #1686).

        The local substrate's "job" is the work subprocess spawned by
        :func:`run_subprocess` inside a pool thread (e.g. the real
        ``openstudio.cli run`` invocation). The order of operations is
        load-bearing for cancellation correctness:

        1. ``super().cancel()`` runs first — it snapshots every live
           handle, clears the registry, and calls ``Handle.cancel()``
           on each. For ``LocalExecutor`` that means ``Future.cancel()``:
           queued (never-started) futures return ``True`` and are
           cancelled outright; running futures return ``False`` and
           keep executing until the kill below takes effect.

        2. ``terminate_active_subprocesses()`` then sends SIGTERM to
           each registered child, waits the bounded grace period
           (issue #1686), and escalates to SIGKILL if needed. The
           running worker threads' ``communicate()`` returns once the
           child is reaped, freeing the pool worker.

        Pre-#1686 the order was reversed (kill first, then cancel),
        but that exposed a race: with the new SIGKILL grace period
        the kill takes hundreds of milliseconds to a few seconds, so a
        queued future could be dequeued and started by the freed pool
        worker between the kill and the cancel sweep — ``super().cancel()``
        would then see the queued future as already-running and skip
        it. Cancelling queued futures *first* removes the race because
        the cancellation runs atomically against the pool's task
        queue: once ``Future.cancel()`` returns ``True`` for a queued
        future, the pool will skip it on dequeue regardless of when
        the worker thread frees up.

        The pool threads themselves are not killed (and should not
        be — they are shared between tasks). Killing the registered
        children is the local analogue of TerminateJob / scancel /
        allocation stop on the remote substrates.
        """
        super().cancel()
        killed = terminate_active_subprocesses()
        if killed:
            log.info("local executor: terminated %d in-flight subprocess(es)", killed)

    def shutdown(self) -> None:
        self._pool.shutdown(wait=True)
