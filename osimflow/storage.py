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

__all__ = [
    "ResultStorage",
    "LocalStorage",
    "S3Storage",
    "GCSStorage",
    "AzureBlobStorage",
    "S3ArtifactStorage",
    "ResultStorageUploader",
    "build_result_storage",
]

import asyncio
import concurrent.futures
import logging
import queue
import random
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse

if TYPE_CHECKING:
    import azure.storage.blob.aio as azure_blob_async
    import boto3.typeing as boto3_typing
    import google.cloud.storage as gcs


log = logging.getLogger("osimflow.storage")


_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0"})


def _validate_storage_endpoint(endpoint_url: str | None, *, allow_insecure: bool = False) -> None:
    """Validate that an S3 endpoint URL uses TLS (issue #1386).

    Mirrors :func:`osimflow.distributed_cache._validate_redis_url`: rejects
    ``http://`` endpoints unless the operator has explicitly opted in with
    ``allow_insecure=True`` (typically only for local MinIO / dev).  The
    loopback hosts (localhost, ``127.0.0.1``, ``::1``, ``0.0.0.0``) are
    exempt because they never traverse a real network — same exception
    the Redis validator uses.

    Parameters
    ----------
    endpoint_url
        The candidate endpoint URL.  ``None`` and ``""`` pass silently —
        there is no custom endpoint to validate.
    allow_insecure
        When ``True``, ``http://`` endpoints are accepted with a loud
        ``WARNING``.  Defaults to ``False`` (fail-closed).

    Raises
    ------
    ValueError
        When ``endpoint_url`` is non-empty, non-loopback, and uses a
        scheme other than ``https`` while ``allow_insecure`` is ``False``.
    """
    if not endpoint_url:
        return

    parsed = urlparse(endpoint_url)
    scheme = (parsed.scheme or "").lower()
    host = (parsed.hostname or "").lower()

    if scheme == "https":
        return

    if scheme != "http":
        raise ValueError(
            f"invalid S3 endpoint URL (issue #1386): {endpoint_url!r} "
            f"uses scheme {scheme!r}; expected 'https://'. "
            f"Non-HTTPS endpoints are rejected unless "
            f"--allow-insecure-storage-endpoint is set."
        )

    if host in _LOOPBACK_HOSTS:
        return

    if allow_insecure:
        log.warning(
            "INSECURE S3 endpoint (issue #1386): %s uses plaintext HTTP; "
            "SigV4 signing material and campaign artifacts will traverse "
            "the network in cleartext. This is allowed because "
            "--allow-insecure-storage-endpoint was set — do not use in "
            "production.",
            endpoint_url,
        )
        return

    raise ValueError(
        f"insecure S3 endpoint URL (issue #1386): {endpoint_url!r} uses "
        f"plaintext HTTP and is not a loopback host. Non-HTTPS storage "
        f"endpoints leak AWS SigV4 signing material and campaign "
        f"artifacts in cleartext. Use https:// or pass "
        f"--allow-insecure-storage-endpoint to override (dev/test only)."
    )


# ---------------------------------------------------------------------------
# Transient-error retry for remote storage I/O (issue #1398)
# ---------------------------------------------------------------------------

#: Total attempts per operation (1 initial + this many retries - 1 ... i.e.
#: ``_TRANSIENT_RETRY_ATTEMPTS`` calls total). Issue #1398 acceptance:
#: "3 attempts / 30s cap".
_TRANSIENT_RETRY_ATTEMPTS = 3
#: Exponential backoff base (attempt N sleeps ``base * 2**(N-1)``).
_TRANSIENT_RETRY_BASE_S = 2.0
#: Backoff ceiling so a pathological outage doesn't stall the orchestrator.
_TRANSIENT_RETRY_CAP_S = 30.0

#: Lowercased substrings that mark an exception as a *transient* storage
#: error — 5xx responses, throttling, and connection-level failures.
#: Non-matching exceptions propagate immediately (fail fast on permanent
#: errors such as 404 / auth denial).
_TRANSIENT_ERROR_MARKERS = (
    "503",
    "502",
    "500",
    "429",
    "slow down",
    "throttl",
    "service unavailable",
    "internal error",
    "bad gateway",
    "connection reset",
    "connection refused",
    "timed out",
    "timeout",
)


def _is_transient_storage_error(exc: BaseException) -> bool:
    """Return True when *exc* looks like a retryable transient storage error.

    ``ConnectionError`` covers ``urllib.error.URLError`` wrappers and
    socket-level resets; ``TimeoutError`` covers read/connect timeouts.
    Everything else is classified by scanning the exception text for
    retryable HTTP / provider markers (5xx, throttling, 429).
    """
    if isinstance(exc, ConnectionError | TimeoutError):
        return True
    text = f"{type(exc).__name__}: {exc}".lower()
    return any(marker in text for marker in _TRANSIENT_ERROR_MARKERS)


def _retry_transient_storage(op: str, fn: Callable[[], None]) -> None:
    """Run *fn* with exponential backoff on transient storage errors.

    Retries up to ``_TRANSIENT_RETRY_ATTEMPTS`` total attempts, sleeping
    ``min(base * 2**(attempt-1), cap)`` between them (issue #1398:
    "3 attempts / 30s cap"). Permanent errors and exhausted retries
    propagate immediately.
    """
    for attempt in range(1, _TRANSIENT_RETRY_ATTEMPTS + 1):
        try:
            fn()
            return
        except Exception as exc:  # noqa: BLE001 — classified below
            if attempt >= _TRANSIENT_RETRY_ATTEMPTS or not _is_transient_storage_error(exc):
                raise
            delay = min(_TRANSIENT_RETRY_BASE_S * (2 ** (attempt - 1)), _TRANSIENT_RETRY_CAP_S)
            log.warning(
                "%s transient failure (attempt %d/%d), retrying in %.1fs: %s",
                op,
                attempt,
                _TRANSIENT_RETRY_ATTEMPTS,
                delay,
                exc,
            )
            time.sleep(delay)


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
        allow_insecure_endpoint: bool = False,
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
            (MinIO, Cloudflare R2, etc.).  Must use ``https://`` for
            non-loopback hosts (issue #1386).
        allow_insecure_endpoint
            Allow ``http://`` endpoints for non-loopback hosts.  When
            ``True``, a ``WARNING`` is logged.  Defaults to ``False``
            (fail-closed).  Mirrors the Redis
            ``--require-redis-auth`` opt-in.
        """
        import boto3  # noqa: PLC0415

        _validate_storage_endpoint(endpoint_url, allow_insecure=allow_insecure_endpoint)
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
            _retry_transient_storage(
                "S3Storage: upload",
                lambda: self.client.upload_file(str(local_path), self.bucket, remote),
            )
        except Exception as exc:
            log.error("S3Storage: upload failed s3://%s/%s: %s", self.bucket, remote, exc)
            raise OSError(f"S3Storage: upload failed for {remote_path}") from exc

    def download_file(self, remote_path: str, local_path: Path) -> None:
        remote = self._remote(remote_path)
        local_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            _retry_transient_storage(
                "S3Storage: download",
                lambda: self.client.download_file(self.bucket, remote, str(local_path)),
            )
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

    async def upload_file_async(self, local_path: Path, remote_path: str) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self.upload_file, local_path, remote_path)

    async def download_file_async(self, remote_path: str, local_path: Path) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self.download_file, remote_path, local_path)

    async def list_results_async(self, prefix: str = "") -> list[str]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self.list_results, prefix)


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
            _retry_transient_storage(
                "GCSStorage: upload",
                lambda: self.bucket_obj.blob(remote).upload_from_filename(str(local_path)),
            )
        except Exception as exc:
            log.error("GCSStorage: upload failed gs://%s/%s: %s", self.bucket, remote, exc)
            raise OSError(f"GCSStorage: upload failed for {remote_path}") from exc

    def download_file(self, remote_path: str, local_path: Path) -> None:
        remote = self._remote(remote_path)
        local_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            _retry_transient_storage(
                "GCSStorage: download",
                lambda: self.bucket_obj.blob(remote).download_to_filename(str(local_path)),
            )
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

    async def upload_file_async(self, local_path: Path, remote_path: str) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self.upload_file, local_path, remote_path)

    async def download_file_async(self, remote_path: str, local_path: Path) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self.download_file, remote_path, local_path)

    async def list_results_async(self, prefix: str = "") -> list[str]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self.list_results, prefix)


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

    async def upload_file_async(self, local_path: Path, remote_path: str) -> None:
        await self._upload_file_async(local_path, remote_path)

    async def download_file_async(self, remote_path: str, local_path: Path) -> None:
        await self._download_file_async(remote_path, local_path)

    async def list_results_async(self, prefix: str = "") -> list[str]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self.list_results, prefix)


class S3ArtifactStorage:
    """S3-backed centralized artifact storage for large simulation files.

    This class handles uploading base simulation assets (``.osm``, ``.idf``,
    and ``.epw`` files) to S3 exactly once at campaign creation, then
    generates pre-signed URLs so remote executor nodes can download them
    directly at high speed without routing through the local machine.

    Credentials are sourced from the IAM role attached to the compute
    environment (AWS Batch), the ECS task role, or the default credential
    chain (``~/.aws/credentials``, environment variables, etc.).
    Long-lived access keys are NOT used.

    Usage
    -----
    >>> from osimflow.storage import S3ArtifactStorage
    >>> store = S3ArtifactStorage(bucket="my-artifacts", prefix="campaign-123")
    >>> store.upload_artifact(Path("model.osm"), "base/model.osm")
    >>> url = store.generate_presigned_url("base/model.osm")
    >>> # Remote nodes use the pre-signed URL to download directly
    """

    name = "s3_artifact"

    def __init__(
        self,
        bucket: str,
        prefix: str = "",
        *,
        endpoint_url: str | None = None,
        region: str | None = None,
        presigned_url_expiration_seconds: int = 3600,
        allow_insecure_endpoint: bool = False,
    ) -> None:
        """Initialize the S3 artifact storage backend.

        Parameters
        ----------
        bucket
            S3 bucket name (e.g. ``"my-campaign-artifacts"``).
        prefix
            Prefix prepended to all artifact paths (e.g. ``"campaign-123"``).
        endpoint_url
            Optional custom endpoint URL for S3-compatible stores
            (MinIO, Cloudflare R2, etc.).  Must use ``https://`` for
            non-loopback hosts (issue #1386).
        region
            AWS region for the S3 bucket. When None, uses the region from
            the IAM role or default credential chain. Required for
            presigned URL generation with some bucket configurations.
        presigned_url_expiration_seconds
            How long pre-signed URLs remain valid (default: 3600 = 1 hour).
            Remote nodes should download artifacts within this window.
        allow_insecure_endpoint
            Allow ``http://`` endpoints for non-loopback hosts.  When
            ``True``, a ``WARNING`` is logged.  Defaults to ``False``
            (fail-closed).  Mirrors the Redis
            ``--require-redis-auth`` opt-in.
        """
        import boto3  # noqa: PLC0415

        _validate_storage_endpoint(endpoint_url, allow_insecure=allow_insecure_endpoint)
        self.bucket = bucket
        self.prefix = prefix.rstrip("/") if prefix else ""
        self.endpoint_url = endpoint_url
        self.region = region
        self.presigned_url_expiration_seconds = presigned_url_expiration_seconds
        self._client: boto3_typing.S3Client | None = None
        self._s3 = boto3.Session()

    @property
    def client(self) -> boto3_typing.S3Client:
        """Lazily-created S3 client (boto3 Session-scoped for thread safety)."""
        if self._client is None:
            kwargs: dict[str, str] = {"region_name": self.region} if self.region else {}
            if self.endpoint_url:
                kwargs["endpoint_url"] = self.endpoint_url
            self._client = self._s3.client("s3", **kwargs)
        return self._client

    def _remote(self, path: str) -> str:
        if self.prefix:
            return f"{self.prefix}/{path}"
        return path

    def upload_artifact(self, local_path: Path, remote_path: str) -> None:
        """Upload a base simulation artifact to S3.

        Parameters
        ----------
        local_path
            Path to the local artifact file (``.osm``, ``.epw``, etc.).
        remote_path
            Remote destination path relative to the bucket/prefix.

        Raises
        ------
        FileNotFoundError
            When the local file does not exist.
        OSError
            When the upload fails.
        """
        if not local_path.is_file():
            raise FileNotFoundError(f"S3ArtifactStorage: local file not found: {local_path}")
        remote = self._remote(remote_path)
        log.debug(
            "S3ArtifactStorage: upload %s -> s3://%s/%s",
            local_path,
            self.bucket,
            remote,
        )
        try:
            self.client.upload_file(str(local_path), self.bucket, remote)
            log.info(
                "S3ArtifactStorage: uploaded artifact %s -> s3://%s/%s",
                local_path.name,
                self.bucket,
                remote,
            )
        except Exception as exc:
            log.error(
                "S3ArtifactStorage: upload failed s3://%s/%s: %s",
                self.bucket,
                remote,
                exc,
            )
            raise OSError(f"S3ArtifactStorage: upload failed for {remote_path}") from exc

    def generate_presigned_url(
        self,
        remote_path: str,
        expiration_seconds: int | None = None,
    ) -> str:
        """Generate a pre-signed URL for an artifact.

        Remote executor nodes use this URL to download the artifact directly
        from S3 at high speed without routing through the local machine.

        Parameters
        ----------
        remote_path
            Remote path of the artifact (relative to bucket/prefix).
        expiration_seconds
            URL expiration time in seconds. When None, uses the default
            from the constructor (3600 seconds / 1 hour).

        Returns
        -------
        str
            The pre-signed URL. Valid for the specified duration.

        Raises
        ------
        OSError
            When URL generation fails.
        """
        remote = self._remote(remote_path)
        expiration = (
            expiration_seconds
            if expiration_seconds is not None
            else self.presigned_url_expiration_seconds
        )
        log.debug(
            "S3ArtifactStorage: generating presigned URL for s3://%s/%s (expires in %ds)",
            self.bucket,
            remote,
            expiration,
        )
        try:
            url = self.client.generate_presigned_url(
                "get_object",
                Params={"Bucket": self.bucket, "Key": remote},
                ExpiresIn=expiration,
            )
            return url  # type: ignore[no-any-return]
        except Exception as exc:
            log.error(
                "S3ArtifactStorage: presigned URL generation failed s3://%s/%s: %s",
                self.bucket,
                remote,
                exc,
            )
            raise OSError(
                f"S3ArtifactStorage: presigned URL generation failed for {remote_path}"
            ) from exc

    def artifact_exists(self, remote_path: str) -> bool:
        """Check whether an artifact already exists in S3.

        Parameters
        ----------
        remote_path
            Remote path of the artifact (relative to bucket/prefix).

        Returns
        -------
        bool
            True if the artifact exists, False otherwise.
        """
        remote = self._remote(remote_path)
        try:
            self.client.head_object(Bucket=self.bucket, Key=remote)
            return True
        except Exception:
            return False

    def list_artifacts(self, prefix: str = "") -> list[str]:
        """List all artifacts under a prefix.

        Parameters
        ----------
        prefix
            Filter to artifacts whose remote path starts with this prefix.
            Empty string means list all artifacts under the storage prefix.

        Returns
        -------
        list[str]
            List of remote paths (relative to the bucket root) for artifacts
            matching the prefix, sorted lexicographically.
        """
        remote_prefix = self._remote(prefix) if prefix else self.prefix
        log.debug(
            "S3ArtifactStorage: list_artifacts prefix=%r -> s3://%s/%s",
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
            log.error("S3ArtifactStorage: list_artifacts failed: %s", exc)
            raise OSError("S3ArtifactStorage: list_artifacts failed") from exc


def build_result_storage(
    backend: str,
    bucket: str,
    prefix: str = "",
    *,
    endpoint_url: str | None = None,
    project_id: str | None = None,
    account_url: str | None = None,
    allow_insecure_endpoint: bool = False,
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
        Custom S3 endpoint (MinIO, R2, etc.) for ``"s3"`` backend.  Must
        use ``https://`` for non-loopback hosts unless
        ``allow_insecure_endpoint=True`` (issue #1386).
    project_id
        Google Cloud project for ``"gs"`` backend.
    account_url
        Azure Blob account URL for ``"azure"`` backend.
    allow_insecure_endpoint
        Opt-in flag for plaintext ``http://`` S3 endpoints (dev/test
        only — logs a ``WARNING`` and forwards to the storage backend).

    Returns
    -------
    ResultStorage
        The concrete storage backend instance.

    Raises
    ------
    ValueError
        When *backend* is not one of the supported values, or when
        ``endpoint_url`` is a non-HTTPS non-loopback URL and
        ``allow_insecure_endpoint`` is ``False`` (issue #1386).
    """
    if backend == "local":
        return LocalStorage()
    if backend == "s3":
        return S3Storage(
            bucket=bucket,
            prefix=prefix,
            endpoint_url=endpoint_url,
            allow_insecure_endpoint=allow_insecure_endpoint,
        )
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
        self._queue: queue.Queue[tuple[Path, str] | None] = queue.Queue(
            maxsize=max(1, max_queue_size)
        )
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
                                time.sleep(random.uniform(0, sleep_s))
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

    async def upload_file_async(self, local_path: Path, remote_path: str) -> None:
        """Direct async upload using the backend's native async method.

        Unlike :meth:`upload_file`, this does not use the worker queue.
        Use this from async executors (e.g. DaskTaskQueue workers) to avoid
        blocking the event loop.
        """
        await self._storage.upload_file_async(local_path, remote_path)

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
