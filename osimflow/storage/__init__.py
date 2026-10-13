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
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from osimflow.storage.azure import AzureBlobStorage  # noqa: F401 — re-exported
from osimflow.storage.base import LocalStorage, ResultStorage  # noqa: F401 — re-exported
from osimflow.storage.endpoint import (  # noqa: F401 — re-exported
    _LOOPBACK_HOSTS,
    _validate_storage_endpoint,
)
from osimflow.storage.retry import (  # noqa: F401 — re-exported
    _TRANSIENT_ERROR_MARKERS,
    _TRANSIENT_RETRY_ATTEMPTS,
    _TRANSIENT_RETRY_BASE_S,
    _TRANSIENT_RETRY_CAP_S,
    _is_transient_storage_error,
)

if TYPE_CHECKING:
    import boto3.typeing as boto3_typing
    import google.cloud.storage as gcs


log = logging.getLogger("osimflow.storage")


def _retry_transient_storage(op: str, fn: Callable[[], None]) -> None:
    """Run *fn* with jittered exponential backoff on transient storage errors.

    Thin wrapper over :func:`osimflow.executors.base.retry_with_backoff`
    (issue #1781) that preserves the historic
    ``"<op> transient failure (attempt N/3), retrying in {delay}s: {exc}"``
    log line via an ``on_retry`` callback and re-uses the existing
    ``_is_transient_storage_error`` classifier.  Permanent errors and
    exhausted retries propagate immediately.  Sleep uses full jitter
    (``random.uniform(0, min(delay, cap))``) so a fleet of concurrent
    workers hitting a transient 503 no longer wake up at the same
    deterministic instant.
    """
    from osimflow.executors.base import retry_with_backoff  # noqa: PLC0415

    def _on_retry(exc: BaseException, attempt: int, window: float) -> None:
        log.warning(
            "%s transient failure (attempt %d/%d), retrying in %.1fs: %s",
            op,
            attempt,
            _TRANSIENT_RETRY_ATTEMPTS,
            window,
            exc,
        )

    retry_with_backoff(
        fn,
        retry_on=_is_transient_storage_error,
        max_attempts=_TRANSIENT_RETRY_ATTEMPTS,
        initial_delay_s=_TRANSIENT_RETRY_BASE_S,
        max_delay_s=_TRANSIENT_RETRY_CAP_S,
        jitter=True,
        on_retry=_on_retry,
    )


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

    def put_if_absent(self, remote_path: str, data: bytes) -> bool:
        """S3 conditional write (``If-None-Match: *``); ``False`` if the key exists."""
        remote = self._remote(remote_path)
        try:
            self.client.put_object(Bucket=self.bucket, Key=remote, Body=data, IfNoneMatch="*")
        except Exception as exc:
            code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
            if code in {"PreconditionFailed", "ConditionalRequestConflict"}:
                return False
            log.error("S3Storage: conditional put failed s3://%s/%s: %s", self.bucket, remote, exc)
            raise OSError(f"S3Storage: conditional put failed for {remote_path}") from exc
        return True


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
        from botocore.exceptions import BotoCoreError, ClientError  # noqa: PLC0415

        remote = self._remote(remote_path)
        try:
            self.client.head_object(Bucket=self.bucket, Key=remote)
            return True
        except (ClientError, BotoCoreError):
            # any boto failure means the object is not confirmed present
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
                    # Intentionally broad: ResultStorage is a pluggable ABC with no
                    # shared backend-error base (boto/azure/gcs/third-party SDKs raise
                    # unrelated types). Every failure is retried with backoff and the
                    # last one is recorded in self._errors, never swallowed.
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

    def _drain(self, timeout_s: float | None) -> bool:
        """Wait for every queued upload to finish (issue #1672).

        ``Queue.join()`` has no timeout parameter, so the bounded mode
        runs it on a short-lived daemon thread joined with the deadline:
        if that thread is still alive the drain is abandoned.  Returns
        ``True`` when the queue drained, ``False`` when ``timeout_s``
        elapsed first.
        """
        if timeout_s is None:
            self._queue.join()
            return True
        drain = threading.Thread(
            target=self._queue.join,
            name="osimflow-upload-drain",
            daemon=True,
        )
        drain.start()
        drain.join(timeout_s)
        return not drain.is_alive()

    def _abandon_pending(self) -> int:
        """Discard queued-not-started items; return how many were dropped.

        ``task_done()`` is still called for every discarded item so the
        abandoned ``join()`` drain thread (if mid-wake) observes a
        consistent ``unfinished_tasks`` count and exits cleanly.
        """
        abandoned = 0
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                return abandoned
            self._queue.task_done()
            abandoned += 1

    def _stop_workers(self, timeout_s: float | None) -> None:
        """Signal worker shutdown and join them under the deadline.

        Workers are daemon threads, so a worker still grinding through
        retry backoff sleeps past the deadline is simply logged and
        left to die at process exit — ``close()`` must return, not wait.
        """
        for _ in self._workers:
            self._queue.put(None)
        deadline = None if timeout_s is None else time.monotonic() + timeout_s
        for worker in self._workers:
            if deadline is None:
                worker.join()
            else:
                remaining = deadline - time.monotonic()
                if remaining > 0:
                    worker.join(timeout=remaining)
        stragglers = [worker.name for worker in self._workers if worker.is_alive()]
        if stragglers:
            log.warning(
                "result storage: %d upload(s) still in flight after close "
                "deadline (workers %s abandoned as daemon threads)",
                self._queue.unfinished_tasks,
                ", ".join(stragglers),
            )
        self._workers = []

    def close(
        self,
        timeout_s: float | None = None,
        *,
        raise_on_failure: bool = True,
    ) -> None:
        """Drain the upload queue, stop workers, and surface upload failures.

        Parameters
        ----------
        timeout_s : float | None
            Bound (seconds) on how long ``close()`` waits for queued
            uploads to drain and for the workers to exit (issue #1672).
            ``None`` (the default) preserves the legacy unbounded drain
            for direct/library callers.  When the deadline passes,
            remaining items are logged and abandoned: an auxiliary
            storage outage must never stall teardown past the bound.
        raise_on_failure : bool
            When ``False``, permanently-failed uploads are logged at
            ERROR instead of raising ``OSError`` — for best-effort
            teardown call sites (``Campaign.run()``'s finally block)
            where raising would crash a successful campaign or mask the
            original in-flight exception.
        """
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
        drained = self._drain(timeout_s)
        if not drained:
            abandoned = self._abandon_pending()
            if abandoned:
                log.error(
                    "result storage: close deadline of %ss exceeded — "
                    "%d queued upload(s) abandoned",
                    timeout_s,
                    abandoned,
                )
        self._stop_workers(timeout_s)
        if self._azure_executor is not None:
            if drained:
                self._azure_executor.shutdown(wait=True)
            else:
                # Deadline already exceeded: do not add another
                # unbounded wait on in-flight Azure futures.
                self._azure_executor.shutdown(wait=False, cancel_futures=True)
            self._azure_executor = None
        if raise_on_failure:
            self._raise_if_failed()
        else:
            with self._error_lock:
                errors = list(self._errors)
            if errors:
                log.error(
                    "result storage: %d upload(s) failed permanently (best-effort close): %s",
                    len(errors),
                    "; ".join(errors[:3]),
                )
