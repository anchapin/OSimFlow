"""Local thread-pool executor for OSimFlow campaigns.

Runs each step callable in a ``concurrent.futures`` thread pool — the
dev/CI substrate. Extracted from ``osimflow/executors/__init__.py``
(issue #1463) so the package init holds only the registry and
re-exports. Also hosts :func:`run_subprocess`, the per-sample log
capture helper (issue #6).
"""

from __future__ import annotations

import logging
import subprocess
import threading
from collections.abc import Callable, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any
from unittest.mock import patch

from osimflow.executors.base import BaseExecutor, Handle
from osimflow.executors.transport import validate_transport_mode

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
            shell=False,
        )


class LocalExecutor(BaseExecutor):
    """Runs tasks in a thread pool. For local dev and CI smoke tests."""

    name = "local"

    def __init__(self, max_workers: int | None = None, max_concurrent_samples: int | None = None):
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
        # Issue #1473: validate the transport capability matrix instead
        # of silently discarding an unsupported mode.
        validate_transport_mode(self.name, result_transport_mode)
        _unused = [
            ("openstudio_version", openstudio_version),
            ("result_hint", result_hint),
            ("remote_command", remote_command),
            ("result_storage_backend", result_storage_backend),
            ("result_storage_bucket", result_storage_bucket),
            ("result_storage_prefix", result_storage_prefix),
            ("result_storage_endpoint", result_storage_endpoint),
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

    def shutdown(self) -> None:
        self._pool.shutdown(wait=True)
