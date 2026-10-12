"""``ResultStorage`` ABC + ``LocalStorage`` split out of ``storage.py`` (issue #1890)."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from pathlib import Path

log = logging.getLogger("osimflow.storage")


class ResultStorage(ABC):
    """Abstract base class for result storage backends.

    All concrete backends must implement the three sync methods and their
    async equivalents.  The Campaign calls the sync methods after successful
    simulation and KPI extraction to persist results to remote storage.
    DaskTaskQueue and future async executors use the async methods directly,
    avoiding blocking I/O in the worker event loop (issue #1282).
    """

    name: str

    @abstractmethod
    def upload_file(self, local_path: Path, remote_path: str) -> None:
        """Upload a local file to remote storage.

        Parameters
        ----------
        local_path
            Path to the local file to upload.
        remote_path
            Remote destination path (relative to the bucket/container root).

        Raises
        ------
        OSError
            When the local file does not exist or upload fails.
        """

    @abstractmethod
    async def upload_file_async(self, local_path: Path, remote_path: str) -> None:
        """Async variant of :meth:`upload_file`.

        Used by async executors (e.g. DaskTaskQueue) to avoid blocking the
        event loop.  Concrete backends implement this using an async I/O
        library or by running the sync method in a thread pool.
        """

    @abstractmethod
    def download_file(self, remote_path: str, local_path: Path) -> None:
        """Download a remote file to local storage.

        Parameters
        ----------
        remote_path
            Remote source path (relative to the bucket/container root).
        local_path
            Local destination path. Parent directories are created if needed.

        Raises
        ------
        FileNotFoundError
            When the remote path does not exist in storage.
        OSError
            When download fails.
        """

    @abstractmethod
    async def download_file_async(self, remote_path: str, local_path: Path) -> None:
        """Async variant of :meth:`download_file`.

        Used by async executors (e.g. DaskTaskQueue) to avoid blocking the
        event loop.  Concrete backends implement this using an async I/O
        library or by running the sync method in a thread pool.
        """

    @abstractmethod
    def list_results(self, prefix: str = "") -> list[str]:
        """List all result paths under a prefix.

        Parameters
        ----------
        prefix
            Filter results to those whose remote path starts with *prefix*.
            Empty string means list all.

        Returns
        -------
        list[str]
            List of remote paths (relative to the bucket/container root)
            matching *prefix*, sorted lexicographically.
        """

    @abstractmethod
    async def list_results_async(self, prefix: str = "") -> list[str]:
        """Async variant of :meth:`list_results`.

        Used by async executors (e.g. DaskTaskQueue) to avoid blocking the
        event loop.  Concrete backends implement this using an async I/O
        library or by running the sync method in a thread pool.
        """

    def put_if_absent(self, remote_path: str, data: bytes) -> bool:
        """Atomically create *remote_path* only if it does not exist (issue #1881).

        Returns ``True`` when this call created the object and ``False`` when
        it already existed. Backends without a conditional-create primitive
        keep this default and fail closed, because the detached-submit claim
        record relies on the atomicity.
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not support conditional create (put_if_absent)"
        )

    def upload_dir(self, local_dir: Path, remote_prefix: str) -> None:
        """Upload all files from a local directory recursively.

        Parameters
        ----------
        local_dir
            Local directory to upload. All files under this directory are
            uploaded, preserving the directory structure under *remote_prefix*.
        remote_prefix
            Remote prefix prepended to each file's relative path inside
            *local_dir*.
        """
        if not local_dir.is_dir():
            log.warning("upload_dir: %s is not a directory — skipping", local_dir)
            return
        for file_path in sorted(local_dir.rglob("*")):
            if file_path.is_file():
                rel = file_path.relative_to(local_dir)
                remote_path = f"{remote_prefix}/{rel}" if remote_prefix else str(rel)
                try:
                    self.upload_file(file_path, remote_path)
                    log.debug("uploaded %s -> %s", file_path, remote_path)
                except OSError as exc:
                    log.warning(
                        "upload_dir: failed to upload %s -> %s: %s", file_path, remote_path, exc
                    )


class LocalStorage(ResultStorage):
    """Local filesystem backend (current behaviour, no-op for remote).

    This backend performs no actual upload — files are already on the local
    filesystem where the Campaign writes them.  It is used when
    ``result_storage_backend`` is ``"local"`` (the default) or when no
    remote backend is configured.
    """

    name = "local"

    def upload_file(self, local_path: Path, remote_path: str) -> None:
        log.debug("LocalStorage: %s (remote_path=%s) — no-op", local_path, remote_path)

    def download_file(self, remote_path: str, local_path: Path) -> None:
        log.debug(
            "LocalStorage: download %s -> %s — no-op",
            remote_path,
            local_path,
        )

    def list_results(self, prefix: str = "") -> list[str]:
        log.debug("LocalStorage: list_results(prefix=%r) — no-op", prefix)
        return []

    async def upload_file_async(self, local_path: Path, remote_path: str) -> None:
        del local_path, remote_path

    async def download_file_async(self, remote_path: str, local_path: Path) -> None:
        del remote_path, local_path

    async def list_results_async(self, prefix: str = "") -> list[str]:
        del prefix
        return []
