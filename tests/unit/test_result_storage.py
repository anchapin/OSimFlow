"""Tests for osimflow/storage.py (issue #339)."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest


class TestLocalStorage:
    def test_upload_file_is_noop(self, tmp_path: Path) -> None:
        from osimflow.storage import LocalStorage

        store = LocalStorage()
        local_file = tmp_path / "test.txt"
        local_file.write_text("hello")
        store.upload_file(local_file, "remote/test.txt")
        assert local_file.exists()

    def test_download_file_is_noop(self, tmp_path: Path) -> None:
        from osimflow.storage import LocalStorage

        store = LocalStorage()
        store.download_file("remote/test.txt", tmp_path / "out.txt")

    def test_list_results_returns_empty(self) -> None:
        from osimflow.storage import LocalStorage

        store = LocalStorage()
        assert store.list_results("prefix/") == []
        assert store.list_results("") == []


class TestBuildResultStorage:
    def test_local_backend(self) -> None:
        from osimflow.storage import LocalStorage, build_result_storage

        store = build_result_storage(backend="local", bucket="any-bucket")
        assert isinstance(store, LocalStorage)
        assert store.name == "local"

    def test_s3_backend(self) -> None:
        from osimflow.storage import S3Storage, build_result_storage

        store = build_result_storage(backend="s3", bucket="my-bucket", prefix="run1")
        assert isinstance(store, S3Storage)
        assert store.name == "s3"
        assert store.bucket == "my-bucket"
        assert store.prefix == "run1"

    def test_gs_backend(self) -> None:
        from osimflow.storage import GCSStorage, build_result_storage

        store = build_result_storage(backend="gs", bucket="my-bucket", prefix="run1")
        assert isinstance(store, GCSStorage)
        assert store.name == "gs"
        assert store.bucket == "my-bucket"
        assert store.prefix == "run1"

    def test_azure_backend(self) -> None:
        from osimflow.storage import AzureBlobStorage, build_result_storage

        store = build_result_storage(backend="azure", bucket="my-container", prefix="run1")
        assert isinstance(store, AzureBlobStorage)
        assert store.name == "azure"
        assert store.container == "my-container"
        assert store.prefix == "run1"

    def test_unknown_backend_raises(self) -> None:
        from osimflow.storage import build_result_storage

        with pytest.raises(ValueError, match="unknown result_storage_backend"):
            build_result_storage(backend="unknown", bucket="any")


class TestS3Storage:
    def test_remote_path_with_prefix(self) -> None:
        from osimflow.storage import S3Storage

        store = S3Storage(bucket="my-bucket", prefix="run1/results")
        assert store._remote("sim/0001/eplusout.sql") == "run1/results/sim/0001/eplusout.sql"

    def test_remote_path_without_prefix(self) -> None:
        from osimflow.storage import S3Storage

        store = S3Storage(bucket="my-bucket")
        assert store._remote("sim/0001/eplusout.sql") == "sim/0001/eplusout.sql"

    def test_upload_file_missing_local_raises(self) -> None:
        from osimflow.storage import S3Storage

        store = S3Storage(bucket="my-bucket")
        with pytest.raises(FileNotFoundError, match="local file not found"):
            store.upload_file(Path("/nonexistent/file.txt"), "remote.txt")


class TestGCSStorage:
    def test_remote_path_with_prefix(self) -> None:
        from osimflow.storage import GCSStorage

        store = GCSStorage(bucket="my-bucket", prefix="run1/results")
        assert store._remote("kpis/kpi_0001.json") == "run1/results/kpis/kpi_0001.json"

    def test_remote_path_without_prefix(self) -> None:
        from osimflow.storage import GCSStorage

        store = GCSStorage(bucket="my-bucket")
        assert store._remote("kpis/kpi_0001.json") == "kpis/kpi_0001.json"

    def test_upload_file_missing_local_raises(self) -> None:
        from osimflow.storage import GCSStorage

        store = GCSStorage(bucket="my-bucket")
        with pytest.raises(FileNotFoundError, match="local file not found"):
            store.upload_file(Path("/nonexistent/file.txt"), "remote.txt")


class TestAzureBlobStorage:
    def test_remote_path_with_prefix(self) -> None:
        from osimflow.storage import AzureBlobStorage

        store = AzureBlobStorage(container="my-container", prefix="run1/results")
        assert store._remote("kpis/kpi_0001.json") == "run1/results/kpis/kpi_0001.json"

    def test_remote_path_without_prefix(self) -> None:
        from osimflow.storage import AzureBlobStorage

        store = AzureBlobStorage(container="my-container")
        assert store._remote("kpis/kpi_0001.json") == "kpis/kpi_0001.json"

    def test_upload_file_missing_local_raises(self) -> None:
        from osimflow.storage import AzureBlobStorage

        store = AzureBlobStorage(container="my-container")
        with pytest.raises(OSError, match="upload failed"):
            store.upload_file(Path("/nonexistent/file.txt"), "remote.txt")


class TestResultStorageUploader:
    def test_local_storage_upload_file(self, tmp_path: Path) -> None:
        from osimflow.storage import LocalStorage, ResultStorageUploader

        local_file = tmp_path / "test.txt"
        local_file.write_text("hello")
        store = LocalStorage()
        uploader = ResultStorageUploader(store)
        uploader.upload_file(local_file, "remote/test.txt")
        uploader.close()

    def test_local_storage_upload_dir(self, tmp_path: Path) -> None:
        from osimflow.storage import LocalStorage, ResultStorageUploader

        subdir = tmp_path / "sub"
        subdir.mkdir()
        (subdir / "a.txt").write_text("a")
        (subdir / "b.txt").write_text("b")
        store = LocalStorage()
        uploader = ResultStorageUploader(store)
        uploader.upload_dir(subdir, "remote/sub")
        uploader.close()

    def test_close_idempotent(self) -> None:
        from osimflow.storage import LocalStorage, ResultStorageUploader

        store = LocalStorage()
        uploader = ResultStorageUploader(store)
        uploader.close()
        uploader.close()

    def test_upload_retries_then_succeeds(self, tmp_path: Path) -> None:
        from osimflow.storage import ResultStorage, ResultStorageUploader

        class FlakyStorage(ResultStorage):
            name = "flaky"

            def __init__(self) -> None:
                self.calls = 0

            def upload_file(self, local_path: Path, remote_path: str) -> None:
                self.calls += 1
                if self.calls == 1:
                    raise OSError("transient")

            def download_file(self, remote_path: str, local_path: Path) -> None:
                return None

            def list_results(self, prefix: str = "") -> list[str]:
                return []

            async def upload_file_async(self, local_path: Path, remote_path: str) -> None:
                return asyncio.to_thread(self.upload_file, local_path, remote_path)

            async def download_file_async(self, remote_path: str, local_path: Path) -> None:
                return asyncio.to_thread(self.download_file, remote_path, local_path)

            async def list_results_async(self, prefix: str = "") -> list[str]:
                return asyncio.to_thread(self.list_results, prefix)

        local_file = tmp_path / "retry.txt"
        local_file.write_text("x")
        store = FlakyStorage()
        uploader = ResultStorageUploader(
            store,
            worker_count=1,
            max_retries=2,
            retry_backoff_s=0.0,
        )
        uploader.upload_file(local_file, "remote/retry.txt")
        uploader.close()
        assert store.calls == 2

    def test_upload_retry_applies_jitter(self, tmp_path: Path) -> None:
        """Verify jitter is applied to retry backoff (issue #1089).

        ``random.uniform(0, sleep_s)`` should replace the raw backoff so
        concurrent uploaders do not retry in lockstep.
        """
        from osimflow.storage import ResultStorage, ResultStorageUploader

        class FlakyStorage(ResultStorage):
            name = "flaky"

            def __init__(self) -> None:
                self.calls = 0

            def upload_file(self, local_path: Path, remote_path: str) -> None:
                self.calls += 1
                if self.calls < 3:
                    raise OSError("transient")

            def download_file(self, remote_path: str, local_path: Path) -> None:
                return None

            def list_results(self, prefix: str = "") -> list[str]:
                return []

            def delete_file(self, remote_path: str) -> None:
                pass

            async def upload_file_async(self, local_path: Path, remote_path: str) -> None:
                return asyncio.to_thread(self.upload_file, local_path, remote_path)

            async def download_file_async(self, remote_path: str, local_path: Path) -> None:
                return asyncio.to_thread(self.download_file, remote_path, local_path)

            async def list_results_async(self, prefix: str = "") -> list[str]:
                return asyncio.to_thread(self.list_results, prefix)

        from unittest.mock import patch

        local_file = tmp_path / "retry.txt"
        local_file.write_text("x")
        store = FlakyStorage()
        uploader = ResultStorageUploader(
            store,
            worker_count=1,
            max_retries=2,
            retry_backoff_s=1.0,
        )

        sleep_durations: list[float] = []
        with patch("osimflow.storage.time.sleep", side_effect=sleep_durations.append):
            with patch(
                "osimflow.storage.random.uniform",
                side_effect=lambda lo, hi: lo + (hi - lo) * 0.5,
            ):
                uploader.upload_file(local_file, "remote/retry.txt")
                uploader.close()

        assert store.calls == 3
        assert len(sleep_durations) == 2
        # First retry: sleep_s=1.0, jitter=0.5
        assert sleep_durations[0] == pytest.approx(0.5)
        # Second retry: sleep_s=2.0, jitter=1.0
        assert sleep_durations[1] == pytest.approx(1.0)

    def test_close_surfaces_upload_failure(self, tmp_path: Path) -> None:
        from osimflow.storage import ResultStorage, ResultStorageUploader

        class FailingStorage(ResultStorage):
            name = "failing"

            def upload_file(self, local_path: Path, remote_path: str) -> None:
                raise OSError("hard failure")

            def download_file(self, remote_path: str, local_path: Path) -> None:
                return None

            def list_results(self, prefix: str = "") -> list[str]:
                return []

            async def upload_file_async(self, local_path: Path, remote_path: str) -> None:
                return asyncio.to_thread(self.upload_file, local_path, remote_path)

            async def download_file_async(self, remote_path: str, local_path: Path) -> None:
                return asyncio.to_thread(self.download_file, remote_path, local_path)

            async def list_results_async(self, prefix: str = "") -> list[str]:
                return asyncio.to_thread(self.list_results, prefix)

        local_file = tmp_path / "fail.txt"
        local_file.write_text("x")
        uploader = ResultStorageUploader(
            FailingStorage(),
            worker_count=1,
            max_retries=1,
            retry_backoff_s=0.0,
        )
        uploader.upload_file(local_file, "remote/fail.txt")
        with pytest.raises(OSError, match="failed uploads"):
            uploader.close()

    def test_upload_queue_applies_backpressure_when_full(self, tmp_path: Path) -> None:
        from osimflow.storage import ResultStorage, ResultStorageUploader

        class SlowStorage(ResultStorage):
            name = "slow"

            def upload_file(self, local_path: Path, remote_path: str) -> None:
                time.sleep(0.2)

            def download_file(self, remote_path: str, local_path: Path) -> None:
                return None

            def list_results(self, prefix: str = "") -> list[str]:
                return []

            async def upload_file_async(self, local_path: Path, remote_path: str) -> None:
                return asyncio.to_thread(self.upload_file, local_path, remote_path)

            async def download_file_async(self, remote_path: str, local_path: Path) -> None:
                return asyncio.to_thread(self.download_file, remote_path, local_path)

            async def list_results_async(self, prefix: str = "") -> list[str]:
                return asyncio.to_thread(self.list_results, prefix)

        files = [tmp_path / f"bp-{idx}.txt" for idx in range(3)]
        for file_path in files:
            file_path.write_text("x")

        uploader = ResultStorageUploader(
            SlowStorage(),
            max_queue_size=1,
            worker_count=1,
            max_retries=0,
            retry_backoff_s=0.0,
        )
        t0 = time.monotonic()
        uploader.upload_file(files[0], "remote/0.txt")
        uploader.upload_file(files[1], "remote/1.txt")
        uploader.upload_file(files[2], "remote/2.txt")
        enqueue_elapsed = time.monotonic() - t0
        uploader.close()

        assert enqueue_elapsed >= 0.15


class TestCampaignConfigStorageFields:
    def test_result_storage_backend_default(self) -> None:
        from osimflow.config import CampaignConfig

        cfg = CampaignConfig(
            input_variables=Path("variables.yml"),
            template_sim_package=Path("template"),
            n_samples=10,
            outdir=Path("outdir"),
            openstudio_version="3.11.0",
        )
        assert cfg.result_storage_backend == "local"
        assert cfg.result_storage_bucket == ""
        assert cfg.result_storage_endpoint is None

    def test_result_storage_backend_explicit(self) -> None:
        from osimflow.config import CampaignConfig

        cfg = CampaignConfig(
            input_variables=Path("variables.yml"),
            template_sim_package=Path("template"),
            n_samples=10,
            outdir=Path("outdir"),
            openstudio_version="3.11.0",
            result_storage_backend="s3",
            result_storage_bucket="my-bucket",
            result_storage_endpoint="https://minio.local:9000",
        )
        assert cfg.result_storage_backend == "s3"
        assert cfg.result_storage_bucket == "my-bucket"
        assert cfg.result_storage_endpoint == "https://minio.local:9000"


class TestLoadConfigStorageArgs:
    def test_result_storage_args(self, tmp_path: Path) -> None:
        from osimflow.config import load_config

        variables = tmp_path / "variables.yml"
        variables.write_text(
            "variables:\n  - name: x\n    distribution: uniform\n    min: 0\n    max: 1"
        )
        template = tmp_path / "template"
        template.mkdir()
        (template / "workflow.osw").write_text('{"steps": []}')
        outdir = tmp_path / "outdir"
        outdir.mkdir()

        args = {
            "input_variables": str(variables),
            "template_sim_package": str(template),
            "n_samples": "10",
            "outdir": str(outdir),
            "openstudio_version": "3.11.0",
            "result_storage_backend": "s3",
            "result_storage_bucket": "my-bucket",
            "result_storage_endpoint": "https://minio.local:9000",
        }
        cfg = load_config(args)
        assert cfg.result_storage_backend == "s3"
        assert cfg.result_storage_bucket == "my-bucket"
        assert cfg.result_storage_endpoint == "https://minio.local:9000"

    def test_result_storage_local_default(self, tmp_path: Path) -> None:
        from osimflow.config import load_config

        variables = tmp_path / "variables.yml"
        variables.write_text(
            "variables:\n  - name: x\n    distribution: uniform\n    min: 0\n    max: 1"
        )
        template = tmp_path / "template"
        template.mkdir()
        (template / "workflow.osw").write_text('{"steps": []}')
        outdir = tmp_path / "outdir"
        outdir.mkdir()

        args = {
            "input_variables": str(variables),
            "template_sim_package": str(template),
            "n_samples": "10",
            "outdir": str(outdir),
            "openstudio_version": "3.11.0",
        }
        cfg = load_config(args)
        assert cfg.result_storage_backend == "local"
        assert cfg.result_storage_bucket == ""


class TestTransientRetry:
    """Transient storage errors retry with backoff (issue #1398)."""

    @pytest.fixture(autouse=True)
    def _fast_sleep(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("osimflow.storage.time.sleep", lambda _s: None)

    def test_s3_download_retries_transient_then_succeeds(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from osimflow.storage import S3Storage

        store = S3Storage(bucket="b")
        calls: list[int] = []

        class _Client:
            def download_file(self, bucket: str, remote: str, local: str) -> None:
                calls.append(1)
                target = Path(local)
                target.parent.mkdir(parents=True, exist_ok=True)
                if len(calls) < 3:
                    raise ConnectionResetError("connection reset by peer")
                target.write_text("data", encoding="utf-8")

        monkeypatch.setattr(type(store), "client", property(lambda self: _Client()))
        out = tmp_path / "out.sql"
        store.download_file("sim/0001/eplusout.sql", out)
        assert len(calls) == 3
        assert out.read_text() == "data"

    def test_s3_download_gives_up_after_three_attempts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from osimflow.storage import S3Storage

        store = S3Storage(bucket="b")
        calls: list[int] = []

        class _Client:
            def download_file(self, bucket: str, remote: str, local: str) -> None:
                calls.append(1)
                raise ConnectionError("connection refused")

        monkeypatch.setattr(type(store), "client", property(lambda self: _Client()))
        with pytest.raises(OSError, match="download failed"):
            store.download_file("sim/0001/eplusout.sql", tmp_path / "out.sql")
        assert len(calls) == 3

    def test_s3_permanent_error_fails_fast(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from osimflow.storage import S3Storage

        store = S3Storage(bucket="b")
        calls: list[int] = []

        class _Client:
            def download_file(self, bucket: str, remote: str, local: str) -> None:
                calls.append(1)
                raise KeyError("NoSuchKey")

        monkeypatch.setattr(type(store), "client", property(lambda self: _Client()))
        with pytest.raises(OSError, match="download failed"):
            store.download_file("missing.sql", tmp_path / "out.sql")
        assert len(calls) == 1  # no retry on permanent errors

    def test_s3_upload_retries_5xx_then_succeeds(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from osimflow.storage import S3Storage

        store = S3Storage(bucket="b")
        src = tmp_path / "kpi.json"
        src.write_text("{}", encoding="utf-8")
        calls: list[int] = []

        class _Client:
            def upload_file(self, local: str, bucket: str, remote: str) -> None:
                calls.append(1)
                if len(calls) < 2:
                    raise OSError("500 InternalError: internal error")

        monkeypatch.setattr(type(store), "client", property(lambda self: _Client()))
        store.upload_file(src, "kpis/kpi_0001.json")
        assert len(calls) == 2

    def test_gcs_download_retries_transient_then_succeeds(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from osimflow.storage import GCSStorage

        store = GCSStorage(bucket="b")
        calls: list[int] = []

        class _Blob:
            def download_to_filename(self, local: str) -> None:
                calls.append(1)
                target = Path(local)
                target.parent.mkdir(parents=True, exist_ok=True)
                if len(calls) < 3:
                    raise ConnectionResetError("503 Service Unavailable")
                target.write_text("data", encoding="utf-8")

        class _Bucket:
            def blob(self, remote: str) -> _Blob:
                return _Blob()

        monkeypatch.setattr(type(store), "bucket_obj", property(lambda self: _Bucket()))
        out = tmp_path / "kpi.json"
        store.download_file("kpis/kpi_0001.json", out)
        assert len(calls) == 3
        assert out.read_text() == "data"

    def test_gcs_upload_retries_throttle_then_succeeds(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from osimflow.storage import GCSStorage

        store = GCSStorage(bucket="b")
        src = tmp_path / "kpi.json"
        src.write_text("{}", encoding="utf-8")
        calls: list[int] = []

        class _Blob:
            def upload_from_filename(self, local: str) -> None:
                calls.append(1)
                if len(calls) < 2:
                    raise OSError("429 SlowDown: reduce your request rate")

        class _Bucket:
            def blob(self, remote: str) -> _Blob:
                return _Blob()

        monkeypatch.setattr(type(store), "bucket_obj", property(lambda self: _Bucket()))
        store.upload_file(src, "kpis/kpi_0001.json")
        assert len(calls) == 2

    def test_transient_classifier(self) -> None:
        from osimflow.storage import _is_transient_storage_error

        assert _is_transient_storage_error(ConnectionError("conn reset"))
        assert _is_transient_storage_error(TimeoutError("timed out"))
        assert _is_transient_storage_error(OSError("503 Service Unavailable"))
        assert _is_transient_storage_error(OSError("429 SlowDown"))
        assert not _is_transient_storage_error(KeyError("NoSuchKey"))
        assert not _is_transient_storage_error(OSError("403 Forbidden"))


class TestS3StorageTransfer:
    """S3 upload/list transfer bodies (issue #1452 coverage)."""

    def test_client_property_is_cached_and_honors_endpoint(self) -> None:
        from osimflow.storage import S3Storage

        store = S3Storage(bucket="b")
        first = store.client
        assert store.client is first

        with_endpoint = S3Storage(bucket="b", endpoint_url="https://minio.local:9000")
        assert with_endpoint.client is with_endpoint.client

    def test_upload_file_success(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from osimflow.storage import S3Storage

        store = S3Storage(bucket="my-bucket", prefix="run1")
        src = tmp_path / "kpi.json"
        src.write_text("{}", encoding="utf-8")
        seen: list[tuple[str, str, str]] = []

        class _Client:
            def upload_file(self, local: str, bucket: str, remote: str) -> None:
                seen.append((local, bucket, remote))

        monkeypatch.setattr(type(store), "client", property(lambda self: _Client()))
        store.upload_file(src, "kpis/kpi_0001.json")
        assert seen == [(str(src), "my-bucket", "run1/kpis/kpi_0001.json")]

    def test_upload_file_wraps_permanent_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from osimflow.storage import S3Storage

        store = S3Storage(bucket="my-bucket")
        src = tmp_path / "kpi.json"
        src.write_text("{}", encoding="utf-8")

        class _Client:
            def upload_file(self, local: str, bucket: str, remote: str) -> None:
                raise KeyError("NoSuchBucket")

        monkeypatch.setattr(type(store), "client", property(lambda self: _Client()))
        with pytest.raises(OSError, match="upload failed"):
            store.upload_file(src, "kpis/kpi_0001.json")

    def test_list_results_paginates_and_strips_prefix(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from osimflow.storage import S3Storage

        store = S3Storage(bucket="my-bucket", prefix="run1")

        class _Paginator:
            def paginate(self, Bucket: str, Prefix: str) -> list[dict]:
                assert Prefix == "run1"
                return [
                    {
                        "Contents": [
                            {"Key": "run1/sim/0001/eplusout.sql"},
                            {"Key": "run1/kpis/kpi_0001.json"},
                        ]
                    },
                    {"Contents": [{"Key": "run1/zz-last.txt"}]},
                    {},
                ]

        class _Client:
            def get_paginator(self, operation: str) -> _Paginator:
                assert operation == "list_objects_v2"
                return _Paginator()

        monkeypatch.setattr(type(store), "client", property(lambda self: _Client()))
        assert store.list_results("") == [
            "kpis/kpi_0001.json",
            "sim/0001/eplusout.sql",
            "zz-last.txt",
        ]

    def test_list_results_wraps_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from osimflow.storage import S3Storage

        store = S3Storage(bucket="my-bucket")

        class _Client:
            def get_paginator(self, operation: str) -> object:
                raise OSError("access denied")

        monkeypatch.setattr(type(store), "client", property(lambda self: _Client()))
        with pytest.raises(OSError, match="list_results failed"):
            store.list_results("")

    def test_async_wrappers(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import asyncio

        from osimflow.storage import S3Storage

        store = S3Storage(bucket="my-bucket")
        src = tmp_path / "a.txt"
        src.write_text("x", encoding="utf-8")

        class _Paginator:
            def paginate(self, Bucket: str, Prefix: str) -> list[dict]:
                return [{"Contents": [{"Key": "a.txt"}]}]

        class _Client:
            def upload_file(self, local: str, bucket: str, remote: str) -> None:
                pass

            def download_file(self, bucket: str, remote: str, local: str) -> None:
                Path(local).write_text("x", encoding="utf-8")

            def get_paginator(self, operation: str) -> _Paginator:
                return _Paginator()

        monkeypatch.setattr(type(store), "client", property(lambda self: _Client()))
        asyncio.run(store.upload_file_async(src, "a.txt"))
        out = tmp_path / "out" / "a.txt"
        asyncio.run(store.download_file_async("a.txt", out))
        assert out.read_text() == "x"
        assert asyncio.run(store.list_results_async("")) == ["a.txt"]


class TestGCSStorageTransfer:
    """GCS client property + transfer bodies (issue #1452 coverage)."""

    def _install_fake_gcs_module(self, monkeypatch: pytest.MonkeyPatch, created: list) -> None:
        import sys
        import types

        google_mod = types.ModuleType("google")
        cloud_mod = types.ModuleType("google.cloud")
        storage_mod = types.ModuleType("google.cloud.storage")

        class Client:
            def __init__(self, **kwargs: object) -> None:
                self.kwargs = kwargs
                created.append(self)

        storage_mod.Client = Client
        google_mod.cloud = cloud_mod
        cloud_mod.storage = storage_mod
        monkeypatch.setitem(sys.modules, "google", google_mod)
        monkeypatch.setitem(sys.modules, "google.cloud", cloud_mod)
        monkeypatch.setitem(sys.modules, "google.cloud.storage", storage_mod)

    def test_client_property_builds_sdk_client(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from osimflow.storage import GCSStorage

        created: list = []
        self._install_fake_gcs_module(monkeypatch, created)

        store = GCSStorage(bucket="b")
        client = store.client
        assert created == [client]
        assert client.kwargs == {}

        with_project = GCSStorage(bucket="b", project_id="my-project")
        assert with_project.client is not None
        assert created[-1].kwargs == {"project": "my-project"}

    def test_upload_file_success(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from osimflow.storage import GCSStorage

        store = GCSStorage(bucket="b", prefix="run1")
        src = tmp_path / "kpi.json"
        src.write_text("{}", encoding="utf-8")
        seen: list[str] = []

        class _Blob:
            def upload_from_filename(self, local: str) -> None:
                seen.append(local)

        class _Bucket:
            def blob(self, remote: str) -> _Blob:
                seen.append(remote)
                return _Blob()

        monkeypatch.setattr(type(store), "bucket_obj", property(lambda self: _Bucket()))
        store.upload_file(src, "kpis/kpi_0001.json")
        assert seen == ["run1/kpis/kpi_0001.json", str(src)]

    def test_upload_file_wraps_error(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from osimflow.storage import GCSStorage

        store = GCSStorage(bucket="b")
        src = tmp_path / "kpi.json"
        src.write_text("{}", encoding="utf-8")

        class _Blob:
            def upload_from_filename(self, local: str) -> None:
                raise KeyError("configuration-quota-exceeded")

        class _Bucket:
            def blob(self, remote: str) -> _Blob:
                return _Blob()

        monkeypatch.setattr(type(store), "bucket_obj", property(lambda self: _Bucket()))
        with pytest.raises(OSError, match="upload failed"):
            store.upload_file(src, "kpi.json")

    def test_download_file_success(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from osimflow.storage import GCSStorage

        store = GCSStorage(bucket="b")

        class _Blob:
            def download_to_filename(self, local: str) -> None:
                Path(local).write_text("data", encoding="utf-8")

        class _Bucket:
            def blob(self, remote: str) -> _Blob:
                return _Blob()

        monkeypatch.setattr(type(store), "bucket_obj", property(lambda self: _Bucket()))
        out = tmp_path / "out" / "kpi.json"
        store.download_file("kpis/kpi_0001.json", out)
        assert out.read_text() == "data"

    def test_download_file_wraps_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from osimflow.storage import GCSStorage

        store = GCSStorage(bucket="b")

        class _Blob:
            def download_to_filename(self, local: str) -> None:
                raise KeyError("notFound")

        class _Bucket:
            def blob(self, remote: str) -> _Blob:
                return _Blob()

        monkeypatch.setattr(type(store), "bucket_obj", property(lambda self: _Bucket()))
        with pytest.raises(OSError, match="download failed"):
            store.download_file("missing.json", tmp_path / "out.json")

    def test_list_results_strips_prefix(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from types import SimpleNamespace

        from osimflow.storage import GCSStorage

        store = GCSStorage(bucket="b", prefix="run1")
        seen_prefixes: list[str] = []

        class _Bucket:
            def list_blobs(self, prefix: str) -> list:
                seen_prefixes.append(prefix)
                return [
                    SimpleNamespace(name="run1/kpis/a.json"),
                    SimpleNamespace(name="run1/zz.txt"),
                ]

        monkeypatch.setattr(type(store), "bucket_obj", property(lambda self: _Bucket()))
        assert store.list_results("") == ["kpis/a.json", "zz.txt"]
        assert seen_prefixes == ["run1"]

    def test_list_results_wraps_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from osimflow.storage import GCSStorage

        store = GCSStorage(bucket="b")

        class _Bucket:
            def list_blobs(self, prefix: str) -> list:
                raise OSError("backend error")

        monkeypatch.setattr(type(store), "bucket_obj", property(lambda self: _Bucket()))
        with pytest.raises(OSError, match="list_results failed"):
            store.list_results("")

    def test_async_wrappers(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import asyncio

        from osimflow.storage import GCSStorage

        store = GCSStorage(bucket="b")
        src = tmp_path / "a.txt"
        src.write_text("x", encoding="utf-8")

        class _Blob:
            def upload_from_filename(self, local: str) -> None:
                pass

            def download_to_filename(self, local: str) -> None:
                Path(local).write_text("x", encoding="utf-8")

        class _Bucket:
            def blob(self, remote: str) -> _Blob:
                return _Blob()

            def list_blobs(self, prefix: str) -> list:
                return []

        monkeypatch.setattr(type(store), "bucket_obj", property(lambda self: _Bucket()))
        asyncio.run(store.upload_file_async(src, "a.txt"))
        out = tmp_path / "out" / "a.txt"
        asyncio.run(store.download_file_async("a.txt", out))
        assert out.read_text() == "x"
        assert asyncio.run(store.list_results_async("")) == []


class _AzureFake:
    """Knobs + call records shared by the fake azure modules."""

    def __init__(self) -> None:
        self.upload_calls = 0
        self.upload_failures = 0
        self.upload_error: Exception = ConnectionResetError("connection reset")
        self.uploaded: list[str] = []
        self.download_error: Exception | None = None
        self.download_calls = 0
        self.content = b"data"
        self.blob_names: list[str] = []
        self.list_error: Exception | None = None
        self.containers: list[dict] = []


def _install_fake_azure_modules(monkeypatch: pytest.MonkeyPatch, settings: _AzureFake) -> None:
    import sys
    import types
    from types import SimpleNamespace

    azure_mod = types.ModuleType("azure")
    identity_mod = types.ModuleType("azure.identity")
    identity_aio_mod = types.ModuleType("azure.identity.aio")
    storage_mod = types.ModuleType("azure.storage")
    blob_mod = types.ModuleType("azure.storage.blob")
    blob_aio_mod = types.ModuleType("azure.storage.blob.aio")

    class DefaultAzureCredential:
        pass

    class _Downloader:
        async def readinto(self, f: object) -> int:
            f.write(settings.content)
            return len(settings.content)

    class _BlobClient:
        def __init__(self, remote: str) -> None:
            self.remote = remote

        async def upload_blob(self, f: object, overwrite: bool = True) -> None:
            settings.upload_calls += 1
            if settings.upload_failures > 0:
                settings.upload_failures -= 1
                raise settings.upload_error
            settings.uploaded.append(self.remote)

        async def download_blob(self) -> _Downloader:
            settings.download_calls += 1
            if settings.download_error is not None:
                raise settings.download_error
            return _Downloader()

    class ContainerClient:
        def __init__(self, **kwargs: object) -> None:
            settings.containers.append(kwargs)

        def get_blob_client(self, remote: str) -> _BlobClient:
            return _BlobClient(remote)

        def list_blobs(self, name_starts_with: str = "") -> object:
            if settings.list_error is not None:
                raise settings.list_error

            async def _gen():
                for name in settings.blob_names:
                    yield SimpleNamespace(name=name)

            return _gen()

    identity_aio_mod.DefaultAzureCredential = DefaultAzureCredential
    blob_aio_mod.ContainerClient = ContainerClient
    azure_mod.identity = identity_mod
    identity_mod.aio = identity_aio_mod
    azure_mod.storage = storage_mod
    storage_mod.blob = blob_mod
    blob_mod.aio = blob_aio_mod
    for name, mod in [
        ("azure", azure_mod),
        ("azure.identity", identity_mod),
        ("azure.identity.aio", identity_aio_mod),
        ("azure.storage", storage_mod),
        ("azure.storage.blob", blob_mod),
        ("azure.storage.blob.aio", blob_aio_mod),
    ]:
        monkeypatch.setitem(sys.modules, name, mod)


class TestAzureBlobStorageTransfer:
    """Azure service property + transfer bodies (issue #1452 coverage)."""

    @pytest.fixture(autouse=True)
    def _fast_azure_sleep(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def _noop(_delay: float) -> None:
            return None

        monkeypatch.setattr("asyncio.sleep", _noop)

    def test_service_property_both_branches(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from osimflow.storage import AzureBlobStorage

        settings = _AzureFake()
        _install_fake_azure_modules(monkeypatch, settings)

        store = AzureBlobStorage(container="c", account_url="https://acct.blob.core.windows.net")
        assert store.service is store._service
        assert settings.containers[-1]["account_url"] == "https://acct.blob.core.windows.net"

        plain = AzureBlobStorage(container="c")
        assert plain.service is plain.service
        assert "container_url" in settings.containers[-1]

    def test_upload_file_success(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from osimflow.storage import AzureBlobStorage

        settings = _AzureFake()
        _install_fake_azure_modules(monkeypatch, settings)
        store = AzureBlobStorage(container="c", prefix="run1")
        src = tmp_path / "kpi.json"
        src.write_text("{}", encoding="utf-8")

        store.upload_file(src, "kpis/kpi_0001.json")
        assert settings.uploaded == ["run1/kpis/kpi_0001.json"]

    def test_upload_file_retries_transient_then_succeeds(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from osimflow.storage import AzureBlobStorage

        settings = _AzureFake()
        settings.upload_failures = 1
        _install_fake_azure_modules(monkeypatch, settings)
        store = AzureBlobStorage(container="c")
        src = tmp_path / "kpi.json"
        src.write_text("{}", encoding="utf-8")

        store.upload_file(src, "kpi.json")
        assert settings.upload_calls == 2
        assert settings.uploaded == ["kpi.json"]

    def test_upload_file_exhausts_transient_retries(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from osimflow.storage import AzureBlobStorage

        settings = _AzureFake()
        settings.upload_failures = 99
        _install_fake_azure_modules(monkeypatch, settings)
        store = AzureBlobStorage(container="c")
        src = tmp_path / "kpi.json"
        src.write_text("{}", encoding="utf-8")

        with pytest.raises(OSError, match="upload failed"):
            store.upload_file(src, "kpi.json")
        assert settings.upload_calls == 3

    def test_upload_file_wraps_permanent_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from osimflow.storage import AzureBlobStorage

        settings = _AzureFake()
        settings.upload_failures = 1
        settings.upload_error = KeyError("AuthorizationFailure")
        _install_fake_azure_modules(monkeypatch, settings)
        store = AzureBlobStorage(container="c")
        src = tmp_path / "kpi.json"
        src.write_text("{}", encoding="utf-8")

        with pytest.raises(OSError, match="upload failed"):
            store.upload_file(src, "kpi.json")
        assert settings.upload_calls == 1

    def test_download_file_success(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from osimflow.storage import AzureBlobStorage

        settings = _AzureFake()
        _install_fake_azure_modules(monkeypatch, settings)
        store = AzureBlobStorage(container="c")
        out = tmp_path / "out" / "kpi.json"

        store.download_file("kpis/kpi_0001.json", out)
        assert out.read_text() == "data"

    def test_download_file_wraps_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from osimflow.storage import AzureBlobStorage

        settings = _AzureFake()
        settings.download_error = KeyError("BlobNotFound")
        _install_fake_azure_modules(monkeypatch, settings)
        store = AzureBlobStorage(container="c")

        with pytest.raises(OSError, match="download failed"):
            store.download_file("missing.json", tmp_path / "out.json")

    def test_list_results_strips_prefix(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from osimflow.storage import AzureBlobStorage

        settings = _AzureFake()
        settings.blob_names = ["run1/kpis/a.json", "run1/zz.txt"]
        _install_fake_azure_modules(monkeypatch, settings)
        store = AzureBlobStorage(container="c", prefix="run1")

        assert store.list_results("") == ["kpis/a.json", "zz.txt"]

    def test_list_results_wraps_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from osimflow.storage import AzureBlobStorage

        settings = _AzureFake()
        settings.list_error = OSError("container missing")
        _install_fake_azure_modules(monkeypatch, settings)
        store = AzureBlobStorage(container="c")

        with pytest.raises(OSError, match="list_results failed"):
            store.list_results("")

    def test_async_wrappers(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import asyncio

        from osimflow.storage import AzureBlobStorage

        settings = _AzureFake()
        _install_fake_azure_modules(monkeypatch, settings)
        store = AzureBlobStorage(container="c")
        src = tmp_path / "a.txt"
        src.write_text("x", encoding="utf-8")
        out = tmp_path / "out" / "a.txt"

        asyncio.run(store.upload_file_async(src, "a.txt"))
        asyncio.run(store.download_file_async("a.txt", out))
        assert out.read_text() == "data"
        assert settings.download_calls == 1
        assert settings.uploaded == ["a.txt"]


class TestResultStorageBaseUploadDir:
    """Default ``ResultStorage.upload_dir`` walk (issue #1452 coverage)."""

    @staticmethod
    def _recording_storage(monkeypatch: pytest.MonkeyPatch) -> tuple:
        from osimflow.storage import ResultStorage

        class RecordingStorage(ResultStorage):
            name = "recording"

            def __init__(self) -> None:
                self.calls: list[tuple[str, str]] = []

            def upload_file(self, local_path: Path, remote_path: str) -> None:
                if "broken" in local_path.name:
                    raise OSError("simulated failure")
                self.calls.append((local_path.name, remote_path))

            def download_file(self, remote_path: str, local_path: Path) -> None:
                return None

            def list_results(self, prefix: str = "") -> list[str]:
                return []

            async def upload_file_async(self, local_path: Path, remote_path: str) -> None:
                return asyncio.to_thread(self.upload_file, local_path, remote_path)

            async def download_file_async(self, remote_path: str, local_path: Path) -> None:
                return asyncio.to_thread(self.download_file, remote_path, local_path)

            async def list_results_async(self, prefix: str = "") -> list[str]:
                return asyncio.to_thread(self.list_results, prefix)

        return RecordingStorage()

    def test_upload_dir_recursive_with_prefix(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = self._recording_storage(monkeypatch)
        (tmp_path / "sub").mkdir()
        (tmp_path / "a.txt").write_text("a")
        (tmp_path / "sub" / "b.txt").write_text("b")

        store.upload_dir(tmp_path, "prefix")
        assert store.calls == [("a.txt", "prefix/a.txt"), ("b.txt", "prefix/sub/b.txt")]

    def test_upload_dir_without_prefix_uses_relative_paths(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = self._recording_storage(monkeypatch)
        (tmp_path / "a.txt").write_text("a")

        store.upload_dir(tmp_path, "")
        assert store.calls == [("a.txt", "a.txt")]

    def test_upload_dir_swallows_per_file_oserrors(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = self._recording_storage(monkeypatch)
        (tmp_path / "broken.bin").write_text("b")
        (tmp_path / "good.txt").write_text("g")

        store.upload_dir(tmp_path, "pre")
        assert store.calls == [("good.txt", "pre/good.txt")]

    def test_upload_dir_missing_dir_is_noop(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = self._recording_storage(monkeypatch)
        store.upload_dir(tmp_path / "does-not-exist", "pre")
        assert store.calls == []

    def test_upload_dir_empty_dir_is_noop(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = self._recording_storage(monkeypatch)
        store.upload_dir(tmp_path, "pre")
        assert store.calls == []


class TestResultStorageUploaderEdgeCases:
    """Uploader close/async/azure paths (issue #1452 coverage)."""

    def test_upload_after_close_raises(self, tmp_path: Path) -> None:
        from osimflow.storage import LocalStorage, ResultStorageUploader

        f = tmp_path / "a.txt"
        f.write_text("x")
        uploader = ResultStorageUploader(LocalStorage(), worker_count=1)
        uploader.close()
        with pytest.raises(OSError, match="uploader is closed"):
            uploader.upload_file(f, "r/a.txt")

    def test_upload_file_async_bypasses_queue(self, tmp_path: Path) -> None:
        import asyncio

        from osimflow.storage import LocalStorage, ResultStorageUploader

        f = tmp_path / "a.txt"
        f.write_text("x")
        uploader = ResultStorageUploader(LocalStorage(), worker_count=1)
        asyncio.run(uploader.upload_file_async(f, "r/a.txt"))
        uploader.close()

    def test_upload_dir_missing_dir_is_noop(self, tmp_path: Path) -> None:
        from osimflow.storage import LocalStorage, ResultStorageUploader

        uploader = ResultStorageUploader(LocalStorage(), worker_count=1)
        uploader.upload_dir(tmp_path / "does-not-exist", "pre")
        uploader.close()

    def test_azure_uploads_route_through_dedicated_executor(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from osimflow.storage import AzureBlobStorage, ResultStorageUploader

        store = AzureBlobStorage(container="c")
        routed: list[str] = []

        async def _fake_upload(local_path: Path, remote_path: str) -> None:
            routed.append(remote_path)

        monkeypatch.setattr(store, "_upload_file_async", _fake_upload)
        f = tmp_path / "a.txt"
        f.write_text("x")

        uploader = ResultStorageUploader(store, worker_count=1)
        uploader.upload_file(f, "r/a.txt")
        uploader.close()
        assert routed == ["r/a.txt"]
        assert uploader._azure_executor is None
