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
