"""``AzureBlobStorage`` split out of ``storage.py`` (issue #1890)."""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from osimflow._storage_base import ResultStorage
from osimflow._storage_retry import (
    _TRANSIENT_RETRY_ATTEMPTS,
    _TRANSIENT_RETRY_BASE_S,
    _TRANSIENT_RETRY_CAP_S,
    _is_transient_storage_error,
)

if TYPE_CHECKING:
    import azure.storage.blob.aio as azure_blob_async

log = logging.getLogger("osimflow.storage")


class AzureBlobStorage(ResultStorage):
    """Azure Blob Storage backend.

    Uses ``azure-storage-blob`` to upload/download files.  Credentials are
    sourced from the managed identity attached to the Azure Batch compute
    node, or from ``DefaultAzureCredential`` which tries multiple auth
    methods including environment variables, workload identity, and managed
    identity.
    """

    name = "azure"

    def __init__(
        self,
        container: str,
        prefix: str = "",
        *,
        account_url: str | None = None,
    ) -> None:
        """Initialize the Azure Blob backend.

        Parameters
        ----------
        container
            Azure Blob container name.
        prefix
            Prefix prepended to all remote paths.
        account_url
            Optional account URL (e.g.
            ``"https://myaccount.blob.core.windows.net"``).  When omitted,
            the backend uses ``DefaultAzureCredential`` with the standard
            environment variables (``AZURE_STORAGE_ACCOUNT``,
            ``AZURE_STORAGE_CLIENT_ID``, etc.).
        """
        self.container = container
        self.prefix = prefix.rstrip("/")
        self.account_url = account_url
        self._service: azure_blob_async.ContainerClient | None = None

    @property
    def service(self) -> azure_blob_async.ContainerClient:
        if self._service is None:
            from azure.identity.aio import DefaultAzureCredential  # noqa: PLC0415
            from azure.storage.blob.aio import ContainerClient  # noqa: PLC0415

            if self.account_url:
                self._service = ContainerClient(
                    account_url=self.account_url,
                    container_name=self.container,
                    credential=DefaultAzureCredential(),
                )
            else:
                self._service = ContainerClient(
                    container_url=f"https://{self.container}.blob.core.windows.net",
                    container_name=self.container,
                    credential=DefaultAzureCredential(),
                )
        return self._service

    def _remote(self, path: str) -> str:
        if self.prefix:
            return f"{self.prefix}/{path}"
        return path

    async def _upload_file_async(self, local_path: Path, remote_path: str) -> None:
        from azure.identity.aio import DefaultAzureCredential  # noqa: PLC0415
        from azure.storage.blob.aio import ContainerClient  # noqa: PLC0415

        if not local_path.is_file():
            raise FileNotFoundError(f"AzureBlobStorage: local file not found: {local_path}")
        remote = self._remote(remote_path)
        log.debug(
            "AzureBlobStorage: upload %s -> %s/%s",
            local_path,
            self.container,
            remote,
        )
        try:
            if self.account_url:
                svc: azure_blob_async.ContainerClient = ContainerClient(
                    account_url=self.account_url,
                    container_name=self.container,
                    credential=DefaultAzureCredential(),
                )
            else:
                svc = ContainerClient(
                    container_url=f"https://{self.container}.blob.core.windows.net",
                    container_name=self.container,
                    credential=DefaultAzureCredential(),
                )
            blob_client = svc.get_blob_client(remote)
            with local_path.open("rb") as f:
                for attempt in range(1, _TRANSIENT_RETRY_ATTEMPTS + 1):
                    try:
                        await blob_client.upload_blob(f, overwrite=True)
                        break
                    except Exception as exc:  # noqa: BLE001 — classified below
                        if attempt >= _TRANSIENT_RETRY_ATTEMPTS or not _is_transient_storage_error(
                            exc
                        ):
                            raise
                        delay = min(
                            _TRANSIENT_RETRY_BASE_S * (2 ** (attempt - 1)),
                            _TRANSIENT_RETRY_CAP_S,
                        )
                        log.warning(
                            "AzureBlobStorage: upload transient failure (attempt %d/%d), "
                            "retrying in %.1fs: %s",
                            attempt,
                            _TRANSIENT_RETRY_ATTEMPTS,
                            delay,
                            exc,
                        )
                        await asyncio.sleep(delay)
        except Exception as exc:
            log.error(
                "AzureBlobStorage: upload failed %s/%s: %s",
                self.container,
                remote,
                exc,
            )
            raise OSError(f"AzureBlobStorage: upload failed for {remote_path}") from exc

    def upload_file(self, local_path: Path, remote_path: str) -> None:
        import asyncio  # noqa: PLC0415

        # Issue #1690: refuse calls made from inside a running event loop.
        # Bridging through ``asyncio.run`` inside a thread-pool worker would
        # otherwise block the caller's loop for the full upload duration
        # (sync wrappers must only be used from sync contexts — async callers
        # should use ``upload_file_async`` or ``ResultStorageUploader``).
        # The raise is placed in the ``else`` clause so it fires OUTSIDE the
        # ``except RuntimeError`` handler below; an earlier version of this
        # guard raised inside the same ``try`` and was swallowed by its own
        # ``except RuntimeError: pass`` — dead code that let async callers
        # fall through to the thread-pool bridge.
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise RuntimeError(
                "AzureBlobStorage.upload_file is sync-only; a running event loop "
                "is active. Use upload_file_async or ResultStorageUploader instead."
            )
        try:
            import concurrent.futures  # noqa: PLC0415

            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                fut = pool.submit(
                    lambda: asyncio.run(self._upload_file_async(local_path, remote_path))
                )
                fut.result()
        except Exception as exc:
            log.error(
                "AzureBlobStorage: synchronous upload failed for %s: %s",
                remote_path,
                exc,
            )
            raise OSError(f"AzureBlobStorage: upload failed for {remote_path}") from exc

    async def _download_file_async(self, remote_path: str, local_path: Path) -> None:
        from azure.identity.aio import DefaultAzureCredential  # noqa: PLC0415
        from azure.storage.blob.aio import ContainerClient  # noqa: PLC0415

        remote = self._remote(remote_path)
        local_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            if self.account_url:
                svc: azure_blob_async.ContainerClient = ContainerClient(
                    account_url=self.account_url,
                    container_name=self.container,
                    credential=DefaultAzureCredential(),
                )
            else:
                svc = ContainerClient(
                    container_url=f"https://{self.container}.blob.core.windows.net",
                    container_name=self.container,
                    credential=DefaultAzureCredential(),
                )
            blob_client = svc.get_blob_client(remote)
            with local_path.open("wb") as f:
                for attempt in range(1, _TRANSIENT_RETRY_ATTEMPTS + 1):
                    try:
                        downloader = await blob_client.download_blob()
                        await downloader.readinto(f)
                        break
                    except Exception as exc:  # noqa: BLE001 — classified below
                        if attempt >= _TRANSIENT_RETRY_ATTEMPTS or not _is_transient_storage_error(
                            exc
                        ):
                            raise
                        delay = min(
                            _TRANSIENT_RETRY_BASE_S * (2 ** (attempt - 1)),
                            _TRANSIENT_RETRY_CAP_S,
                        )
                        log.warning(
                            "AzureBlobStorage: download transient failure (attempt %d/%d), "
                            "retrying in %.1fs: %s",
                            attempt,
                            _TRANSIENT_RETRY_ATTEMPTS,
                            delay,
                            exc,
                        )
                        await asyncio.sleep(delay)
        except Exception as exc:
            log.error(
                "AzureBlobStorage: download failed %s/%s -> %s: %s",
                self.container,
                remote,
                local_path,
                exc,
            )
            raise OSError(f"AzureBlobStorage: download failed for {remote_path}") from exc

    def download_file(self, remote_path: str, local_path: Path) -> None:
        import asyncio  # noqa: PLC0415

        # Issue #1690: mirror the upload-side guard. The ``raise`` lives in
        # the ``else`` clause so the ``except RuntimeError: pass`` below
        # cannot swallow it (the previous version raised inside the same
        # ``try`` block and was dead-code; see ``upload_file`` for the full
        # rationale).
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise RuntimeError(
                "AzureBlobStorage.download_file is sync-only; a running event "
                "loop is active. Use download_file_async or ResultStorageUploader instead."
            )
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                fut = pool.submit(
                    lambda: asyncio.run(self._download_file_async(remote_path, local_path))
                )
                fut.result()
        except Exception as exc:
            log.error(
                "AzureBlobStorage: synchronous download failed for %s: %s",
                remote_path,
                exc,
            )
            raise OSError(f"AzureBlobStorage: download failed for {remote_path}") from exc

    def list_results(self, prefix: str = "") -> list[str]:
        remote_prefix = self._remote(prefix) if prefix else self.prefix
        log.debug(
            "AzureBlobStorage: list_results prefix=%r -> %s/%s",
            prefix,
            self.container,
            remote_prefix,
        )
        try:
            from azure.identity.aio import DefaultAzureCredential  # noqa: PLC0415
            from azure.storage.blob.aio import ContainerClient  # noqa: PLC0415

            if self.account_url:
                svc: azure_blob_async.ContainerClient = ContainerClient(
                    account_url=self.account_url,
                    container_name=self.container,
                    credential=DefaultAzureCredential(),
                )
            else:
                svc = ContainerClient(
                    container_url=f"https://{self.container}.blob.core.windows.net",
                    container_name=self.container,
                    credential=DefaultAzureCredential(),
                )
            keys: list[str] = []
            import asyncio  # noqa: PLC0415

            async def _list() -> None:
                nonlocal keys
                async for blob in svc.list_blobs(name_starts_with=remote_prefix):
                    key = blob.name
                    if self.prefix and key.startswith(self.prefix + "/"):
                        key = key[len(self.prefix) + 1 :]
                    keys.append(key)

            asyncio.run(_list())
            return sorted(keys)
        except Exception as exc:
            log.error("AzureBlobStorage: list_results failed: %s", exc)
            raise OSError("AzureBlobStorage: list_results failed") from exc

    async def upload_file_async(self, local_path: Path, remote_path: str) -> None:
        await self._upload_file_async(local_path, remote_path)

    async def download_file_async(self, remote_path: str, local_path: Path) -> None:
        await self._download_file_async(remote_path, local_path)

    async def list_results_async(self, prefix: str = "") -> list[str]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self.list_results, prefix)
