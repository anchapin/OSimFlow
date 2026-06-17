"""Result storage backends for distributed campaigns (issue #339).

Provides a pluggable abstraction over local filesystem, S3, GCS, and
Azure Blob Storage so multi-node campaigns can aggregate results without
a shared filesystem.

Example
-------
>>> from osimflow.storage import S3Storage
>>> store = S3Storage(bucket="my-campaign-results", prefix="run1")
>>> store.upload_file(Path("kpi_0001.json"), "kpis/kpi_0001.json")
>>> store.list_results("kpis/")
['kpis/kpi_0001.json']
"""

from __future__ import annotations

import concurrent.futures
import logging
import queue
import threading
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import azure.storage.blob.aio as azure_blob_async
    import boto3.typeing as boto3_typing
    import google.cloud.storage as gcs


log = logging.getLogger("osimflow.storage")


class ResultStorage(ABC):
    """Abstract base class for result storage backends.

    All concrete backends must implement the three sync methods.
    The Campaign calls these after successful simulation and KPI extraction
    to persist results to remote storage.
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


class S3Storage(ResultStorage):
    """Amazon S3 storage backend.

    Uses ``boto3`` to upload/download files.  Credentials are sourced from
    the IAM role attached to the compute environment (AWS Batch), the
    ECS task role, or the default credential chain (``~/.aws/credentials``,
    environment variables, etc.).  Long-lived access keys are NOT used.
    """

    name = "s3"

    def __init__(
        self,
        bucket: str,
        prefix: str = "",
        *,
        endpoint_url: str | None = None,
    ) -> None:
        """Initialize the S3 backend.

        Parameters
        ----------
        bucket
            S3 bucket name (e.g. ``"my-campaign-results"``).
        prefix
            Prefix prepended to all remote paths (e.g. ``"run1/results"``).
        endpoint_url
            Optional custom endpoint URL for S3-compatible stores
            (MinIO, Cloudflare R2, etc.).
        """
        import boto3  # noqa: PLC0415

        self.bucket = bucket
        self.prefix = prefix.rstrip("/")
        self.endpoint_url = endpoint_url
        self._client: boto3_typing.S3Client | None = None
        self._s3 = boto3.Session()

    @property
    def client(self) -> boto3_typing.S3Client:
        """Lazily-created S3 client (boto3 Session-scoped for thread safety)."""
        if self._client is None:
            kwargs: dict[str, str] = {}
            if self.endpoint_url:
                kwargs["endpoint_url"] = self.endpoint_url
            self._client = self._s3.client("s3", **kwargs)
        return self._client

    def _remote(self, path: str) -> str:
        if self.prefix:
            return f"{self.prefix}/{path}"
        return path

    def upload_file(self, local_path: Path, remote_path: str) -> None:
        if not local_path.is_file():
            raise FileNotFoundError(f"S3Storage: local file not found: {local_path}")
        remote = self._remote(remote_path)
        log.debug("S3Storage: upload %s -> s3://%s/%s", local_path, self.bucket, remote)
        try:
            self.client.upload_file(str(local_path), self.bucket, remote)
        except Exception as exc:
            log.error("S3Storage: upload failed s3://%s/%s: %s", self.bucket, remote, exc)
            raise OSError(f"S3Storage: upload failed for {remote_path}") from exc

    def download_file(self, remote_path: str, local_path: Path) -> None:
        remote = self._remote(remote_path)
        local_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.client.download_file(self.bucket, remote, str(local_path))
        except Exception as exc:
            log.error(
                "S3Storage: download failed s3://%s/%s -> %s: %s",
                self.bucket,
                remote,
                local_path,
                exc,
            )
            raise OSError(f"S3Storage: download failed for {remote_path}") from exc

    def list_results(self, prefix: str = "") -> list[str]:
        remote_prefix = self._remote(prefix) if prefix else self.prefix
        log.debug(
            "S3Storage: list_results prefix=%r -> s3://%s/%s",
            prefix,
            self.bucket,
            remote_prefix,
        )
        try:
            paginator = self.client.get_paginator("list_objects_v2")
            pages = paginator.paginate(Bucket=self.bucket, Prefix=remote_prefix)
            keys: list[str] = []
            for page in pages:
                for obj in page.get("Contents", []):
                    key = obj["Key"]
                    assert isinstance(key, str)
                    if self.prefix and key.startswith(self.prefix + "/"):
                        key = key[len(self.prefix) + 1 :]
                    keys.append(key)
            return sorted(keys)
        except Exception as exc:
            log.error("S3Storage: list_results failed: %s", exc)
            raise OSError("S3Storage: list_results failed") from exc


class GCSStorage(ResultStorage):
    """Google Cloud Storage (GCS) backend.

    Uses ``google-cloud-storage`` to upload/download files.  Credentials are
    sourced from the service account attached to the compute environment
    (GCE, GKE, Cloud Run, etc.) via Application Default Credentials (ADC).
    """

    name = "gs"

    def __init__(
        self,
        bucket: str,
        prefix: str = "",
        *,
        project_id: str | None = None,
    ) -> None:
        """Initialize the GCS backend.

        Parameters
        ----------
        bucket
            GCS bucket name.
        prefix
            Prefix prepended to all remote paths.
        project_id
            Optional Google Cloud project ID.  When omitted, the backend
            uses the project from the attached service account's ADC.
        """
        self.bucket = bucket
        self.prefix = prefix.rstrip("/")
        self.project_id = project_id
        self._client: gcs.Client | None = None

    @property
    def client(self) -> gcs.Client:
        if self._client is None:
            import google.cloud.storage  # noqa: PLC0415

            kwargs: dict[str, str] = {}
            if self.project_id:
                kwargs["project"] = self.project_id
            self._client = google.cloud.storage.Client(**kwargs)
        return self._client

    @property
    def bucket_obj(self) -> gcs.Bucket:
        return self.client.bucket(self.bucket)

    def _remote(self, path: str) -> str:
        if self.prefix:
            return f"{self.prefix}/{path}"
        return path

    def upload_file(self, local_path: Path, remote_path: str) -> None:
        if not local_path.is_file():
            raise FileNotFoundError(f"GCSStorage: local file not found: {local_path}")
        remote = self._remote(remote_path)
        log.debug("GCSStorage: upload %s -> gs://%s/%s", local_path, self.bucket, remote)
        try:
            blob = self.bucket_obj.blob(remote)
            blob.upload_from_filename(str(local_path))
        except Exception as exc:
            log.error("GCSStorage: upload failed gs://%s/%s: %s", self.bucket, remote, exc)
            raise OSError(f"GCSStorage: upload failed for {remote_path}") from exc

    def download_file(self, remote_path: str, local_path: Path) -> None:
        remote = self._remote(remote_path)
        local_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            blob = self.bucket_obj.blob(remote)
            blob.download_to_filename(str(local_path))
        except Exception as exc:
            log.error(
                "GCSStorage: download failed gs://%s/%s -> %s: %s",
                self.bucket,
                remote,
                local_path,
                exc,
            )
            raise OSError(f"GCSStorage: download failed for {remote_path}") from exc

    def list_results(self, prefix: str = "") -> list[str]:
        remote_prefix = self._remote(prefix) if prefix else self.prefix
        log.debug(
            "GCSStorage: list_results prefix=%r -> gs://%s/%s",
            prefix,
            self.bucket,
            remote_prefix,
        )
        try:
            blobs = list(self.bucket_obj.list_blobs(prefix=remote_prefix))
            keys: list[str] = []
            for blob in blobs:
                key = blob.name
                if self.prefix and key.startswith(self.prefix + "/"):
                    key = key[len(self.prefix) + 1 :]
                keys.append(key)
            return sorted(keys)
        except Exception as exc:
            log.error("GCSStorage: list_results failed: %s", exc)
            raise OSError("GCSStorage: list_results failed") from exc


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
                await blob_client.upload_blob(f, overwrite=True)
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

        try:
            asyncio.get_running_loop()
            raise RuntimeError(
                "AzureBlobStorage.upload_file is async; use ResultStorageUploader for batch uploads"
            )
        except RuntimeError:
            pass
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
                downloader = await blob_client.download_blob()
                await downloader.readinto(f)
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
        import concurrent.futures  # noqa: PLC0415

        try:
            asyncio.get_running_loop()
            raise RuntimeError(
                "AzureBlobStorage.download_file is async; use "
                "ResultStorageUploader for batch downloads"
            )
        except RuntimeError:
            pass
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


def build_result_storage(
    backend: str,
    bucket: str,
    prefix: str = "",
    *,
    endpoint_url: str | None = None,
    project_id: str | None = None,
    account_url: str | None = None,
) -> ResultStorage:
    """Factory: build the correct ResultStorage from a backend name.

    Parameters
    ----------
    backend
        One of ``"local"``, ``"s3"``, ``"gs"``, ``"azure"``.
    bucket
        Bucket/container name (used for all backends except ``"local"``).
    prefix
        Prefix prepended to all remote paths.
    endpoint_url
        Custom S3 endpoint (MinIO, R2, etc.) for ``"s3"`` backend.
    project_id
        Google Cloud project for ``"gs"`` backend.
    account_url
        Azure Blob account URL for ``"azure"`` backend.

    Returns
    -------
    ResultStorage
        The concrete storage backend instance.

    Raises
    ------
    ValueError
        When *backend* is not one of the supported values.
    """
    if backend == "local":
        return LocalStorage()
    if backend == "s3":
        return S3Storage(bucket=bucket, prefix=prefix, endpoint_url=endpoint_url)
    if backend == "gs":
        return GCSStorage(bucket=bucket, prefix=prefix, project_id=project_id)
    if backend == "azure":
        return AzureBlobStorage(container=bucket, prefix=prefix, account_url=account_url)
    raise ValueError(f"unknown result_storage_backend: {backend!r}")


class ResultStorageUploader:
    """Bounded, retrying upload queue for any ResultStorage backend."""

    def __init__(
        self,
        storage: ResultStorage,
        *,
        max_queue_size: int = 128,
        worker_count: int = 2,
        max_retries: int = 3,
        retry_backoff_s: float = 0.5,
    ) -> None:
        self._storage = storage
        self._azure_executor: concurrent.futures.ThreadPoolExecutor | None = None
        self._queue: queue.Queue[tuple[Path, str] | None] = queue.Queue(maxsize=max(1, max_queue_size))
        self._worker_count = max(1, worker_count)
        self._max_retries = max(0, max_retries)
        self._retry_backoff_s = max(0.0, retry_backoff_s)
        self._closed = False
        self._close_lock = threading.Lock()
        self._errors: list[str] = []
        self._error_lock = threading.Lock()
        self._workers: list[threading.Thread] = []
        for idx in range(self._worker_count):
            worker = threading.Thread(
                target=self._worker_loop,
                name=f"osimflow-upload-{idx}",
                daemon=True,
            )
            worker.start()
            self._workers.append(worker)

    def _raise_if_failed(self) -> None:
        with self._error_lock:
            if not self._errors:
                return
            sample = "; ".join(self._errors[:3])
        raise OSError(f"result storage uploader has failed uploads: {sample}")

    def _upload_once(self, local_path: Path, remote_path: str) -> None:
        if isinstance(self._storage, AzureBlobStorage):
            import asyncio  # noqa: PLC0415
            import concurrent.futures  # noqa: PLC0415

            if self._azure_executor is None:
                self._azure_executor = concurrent.futures.ThreadPoolExecutor(max_workers=4)
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                async_method = self._storage._upload_file_async(local_path, remote_path)
                future = self._azure_executor.submit(lambda: loop.run_until_complete(async_method))
                future.result()
            finally:
                loop.close()
        else:
            self._storage.upload_file(local_path, remote_path)

    def _worker_loop(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                self._queue.task_done()
                return
            local_path, remote_path = item
            try:
                last_error: Exception | None = None
                for attempt in range(self._max_retries + 1):
                    try:
                        self._upload_once(local_path, remote_path)
                        last_error = None
                        break
                    except Exception as exc:  # noqa: BLE001
                        last_error = exc
                        if attempt < self._max_retries:
                            sleep_s = min(60.0, self._retry_backoff_s * (2**attempt))
                            log.warning(
                                "result storage: retry %d/%d for %s -> %s after %s",
                                attempt + 1,
                                self._max_retries,
                                local_path,
                                remote_path,
                                exc,
                            )
                            if sleep_s > 0:
                                time.sleep(sleep_s)
                if last_error is not None:
                    msg = f"{local_path} -> {remote_path}: {last_error}"
                    with self._error_lock:
                        self._errors.append(msg)
                    log.error("result storage: upload failed after retries: %s", msg)
            finally:
                self._queue.task_done()

    def upload_file(self, local_path: Path, remote_path: str) -> None:
        """Enqueue a single file upload, applying backpressure when full."""
        self._raise_if_failed()
        with self._close_lock:
            if self._closed:
                raise OSError("result storage uploader is closed")
        self._queue.put((local_path, remote_path))
        self._raise_if_failed()

    def upload_dir(self, local_dir: Path, remote_prefix: str) -> None:
        """Enqueue a directory tree for upload."""
        if not local_dir.is_dir():
            return
        for file_path in sorted(local_dir.rglob("*")):
            if file_path.is_file():
                rel = file_path.relative_to(local_dir)
                remote_path = f"{remote_prefix}/{rel}" if remote_prefix else str(rel)
                self.upload_file(file_path, remote_path)

    def close(self) -> None:
        """Drain the upload queue, stop workers, and surface upload failures."""
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
        self._queue.join()
        for _ in self._workers:
            self._queue.put(None)
        for worker in self._workers:
            worker.join()
        self._workers = []
        if self._azure_executor is not None:
            self._azure_executor.shutdown(wait=True)
            self._azure_executor = None
        self._raise_if_failed()
