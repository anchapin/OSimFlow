"""Unit tests for S3ArtifactStorage (issue #601).

Tests the centralized S3 artifact storage backend that uploads base
simulation assets (.osm, .epw) once and generates pre-signed URLs
for remote executor nodes.
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


class TestS3ArtifactStorage:
    """Tests for S3ArtifactStorage class."""

    @pytest.fixture
    def mock_boto3(self) -> MagicMock:
        """Mock boto3 at the import site (boto3.Session() called in __init__)."""
        fake_boto3 = MagicMock()
        fake_session = MagicMock()
        fake_client = MagicMock()
        fake_session.client.return_value = fake_client
        fake_boto3.Session.return_value = fake_session
        return fake_boto3, fake_session, fake_client

    @pytest.fixture
    def temp_dir(self) -> Path:
        """Create a temporary directory for test files."""
        with tempfile.TemporaryDirectory() as td:
            yield Path(td)

    @pytest.fixture
    def sample_osm(self, temp_dir: Path) -> Path:
        """Create a sample .osm file."""
        osm_path = temp_dir / "model.osm"
        osm_path.write_text(
            '<OpenStudioModel>\n  <Version VersionId="3.11.0"/>\n</OpenStudioModel>',
            encoding="utf-8",
        )
        return osm_path

    @pytest.fixture
    def sample_epw(self, temp_dir: Path) -> Path:
        """Create a sample .epw file."""
        epw_path = temp_dir / "weather.epw"
        epw_path.write_text(
            "LOCATION,Chicago Ohare Intl Ap,IL,USA,TMY3,725300,41.98,-87.92,-6.0,\n"
            "1,1,1,1,0,0,0\n"
            "0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0\n",
            encoding="utf-8",
        )
        return epw_path

    def test_init_defaults(self, mock_boto3: tuple[MagicMock, MagicMock, MagicMock]) -> None:
        """Test S3ArtifactStorage initialization with defaults."""
        fake_boto3, fake_session, _ = mock_boto3
        with patch("boto3.Session", return_value=fake_session):
            with patch.dict("sys.modules", {"boto3": fake_boto3}):
                import importlib

                import osimflow.storage as storage_mod

                importlib.reload(storage_mod)
                from osimflow.storage import S3ArtifactStorage

                store = S3ArtifactStorage(bucket="my-artifacts", prefix="campaign-123")

                assert store.bucket == "my-artifacts"
                assert store.prefix == "campaign-123"
                assert store.region is None
                assert store.endpoint_url is None
                assert store.presigned_url_expiration_seconds == 3600

    def test_init_with_all_options(
        self, mock_boto3: tuple[MagicMock, MagicMock, MagicMock]
    ) -> None:
        """Test S3ArtifactStorage initialization with all options."""
        fake_boto3, fake_session, _ = mock_boto3
        with patch("boto3.Session", return_value=fake_session):
            with patch.dict("sys.modules", {"boto3": fake_boto3}):
                import importlib

                import osimflow.storage as storage_mod

                importlib.reload(storage_mod)
                from osimflow.storage import S3ArtifactStorage

                store = S3ArtifactStorage(
                    bucket="my-artifacts",
                    prefix="project/campaign-123",
                    endpoint_url="https://minio.local:9000",
                    region="us-west-2",
                    presigned_url_expiration_seconds=7200,
                )

                assert store.bucket == "my-artifacts"
                assert store.prefix == "project/campaign-123"
                assert store.endpoint_url == "https://minio.local:9000"
                assert store.region == "us-west-2"
                assert store.presigned_url_expiration_seconds == 7200

    def test_init_with_empty_prefix(
        self, mock_boto3: tuple[MagicMock, MagicMock, MagicMock]
    ) -> None:
        """Test S3ArtifactStorage handles empty prefix correctly."""
        fake_boto3, fake_session, _ = mock_boto3
        with patch("boto3.Session", return_value=fake_session):
            with patch.dict("sys.modules", {"boto3": fake_boto3}):
                import importlib

                import osimflow.storage as storage_mod

                importlib.reload(storage_mod)
                from osimflow.storage import S3ArtifactStorage

                store = S3ArtifactStorage(bucket="my-artifacts", prefix="")
                assert store.prefix == ""

                store2 = S3ArtifactStorage(bucket="my-artifacts", prefix=None)  # type: ignore[arg-type]
                assert store2.prefix == ""

    def test_remote_path_with_prefix(
        self, mock_boto3: tuple[MagicMock, MagicMock, MagicMock]
    ) -> None:
        """Test _remote correctly prepends prefix."""
        fake_boto3, fake_session, _ = mock_boto3
        with patch("boto3.Session", return_value=fake_session):
            with patch.dict("sys.modules", {"boto3": fake_boto3}):
                import importlib

                import osimflow.storage as storage_mod

                importlib.reload(storage_mod)
                from osimflow.storage import S3ArtifactStorage

                store = S3ArtifactStorage(bucket="my-artifacts", prefix="campaign-123")
                assert store._remote("base/model.osm") == "campaign-123/base/model.osm"
                assert store._remote("weather.epw") == "campaign-123/weather.epw"

    def test_remote_path_without_prefix(
        self, mock_boto3: tuple[MagicMock, MagicMock, MagicMock]
    ) -> None:
        """Test _remote returns path unchanged when no prefix."""
        fake_boto3, fake_session, _ = mock_boto3
        with patch("boto3.Session", return_value=fake_session):
            with patch.dict("sys.modules", {"boto3": fake_boto3}):
                import importlib

                import osimflow.storage as storage_mod

                importlib.reload(storage_mod)
                from osimflow.storage import S3ArtifactStorage

                store = S3ArtifactStorage(bucket="my-artifacts", prefix="")
                assert store._remote("base/model.osm") == "base/model.osm"

    def test_upload_artifact_success(
        self,
        mock_boto3: tuple[MagicMock, MagicMock, MagicMock],
        sample_osm: Path,
    ) -> None:
        """Test successful artifact upload."""
        fake_boto3, fake_session, fake_client = mock_boto3
        with patch("boto3.Session", return_value=fake_session):
            with patch.dict("sys.modules", {"boto3": fake_boto3}):
                import importlib

                import osimflow.storage as storage_mod

                importlib.reload(storage_mod)
                from osimflow.storage import S3ArtifactStorage

                store = S3ArtifactStorage(bucket="my-artifacts", prefix="campaign-123")
                store.upload_artifact(sample_osm, "base/model.osm")

                fake_client.upload_file.assert_called_once_with(
                    str(sample_osm), "my-artifacts", "campaign-123/base/model.osm"
                )

    def test_upload_artifact_file_not_found(
        self, mock_boto3: tuple[MagicMock, MagicMock, MagicMock]
    ) -> None:
        """Test upload raises FileNotFoundError for missing file."""
        fake_boto3, fake_session, _ = mock_boto3
        with patch("boto3.Session", return_value=fake_session):
            with patch.dict("sys.modules", {"boto3": fake_boto3}):
                import importlib

                import osimflow.storage as storage_mod

                importlib.reload(storage_mod)
                from osimflow.storage import S3ArtifactStorage

                store = S3ArtifactStorage(bucket="my-artifacts", prefix="")

                with pytest.raises(FileNotFoundError, match="local file not found"):
                    store.upload_artifact(Path("/nonexistent/model.osm"), "base/model.osm")

    def test_upload_artifact_failure(
        self,
        mock_boto3: tuple[MagicMock, MagicMock, MagicMock],
        sample_osm: Path,
    ) -> None:
        """Test upload raises OSError on failure."""
        fake_boto3, fake_session, fake_client = mock_boto3
        fake_client.upload_file.side_effect = Exception("S3 upload failed")

        with patch("boto3.Session", return_value=fake_session):
            with patch.dict("sys.modules", {"boto3": fake_boto3}):
                import importlib

                import osimflow.storage as storage_mod

                importlib.reload(storage_mod)
                from osimflow.storage import S3ArtifactStorage

                store = S3ArtifactStorage(bucket="my-artifacts", prefix="")

                with pytest.raises(OSError, match="upload failed"):
                    store.upload_artifact(sample_osm, "base/model.osm")

    def test_generate_presigned_url_success(
        self, mock_boto3: tuple[MagicMock, MagicMock, MagicMock]
    ) -> None:
        """Test successful pre-signed URL generation."""
        fake_boto3, fake_session, fake_client = mock_boto3
        fake_client.generate_presigned_url.return_value = (
            "https://my-artifacts.s3.amazonaws.com/campaign-123/base/model.osm?"
            "X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Expires=3600"
        )

        with patch("boto3.Session", return_value=fake_session):
            with patch.dict("sys.modules", {"boto3": fake_boto3}):
                import importlib

                import osimflow.storage as storage_mod

                importlib.reload(storage_mod)
                from osimflow.storage import S3ArtifactStorage

                store = S3ArtifactStorage(bucket="my-artifacts", prefix="campaign-123")
                url = store.generate_presigned_url("base/model.osm")

                assert url.startswith("https://my-artifacts.s3.amazonaws.com/")
                fake_client.generate_presigned_url.assert_called_once_with(
                    "get_object",
                    Params={"Bucket": "my-artifacts", "Key": "campaign-123/base/model.osm"},
                    ExpiresIn=3600,
                )

    def test_generate_presigned_url_custom_expiration(
        self, mock_boto3: tuple[MagicMock, MagicMock, MagicMock]
    ) -> None:
        """Test pre-signed URL generation with custom expiration."""
        fake_boto3, fake_session, fake_client = mock_boto3
        fake_client.generate_presigned_url.return_value = "https://example.com/signed"

        with patch("boto3.Session", return_value=fake_session):
            with patch.dict("sys.modules", {"boto3": fake_boto3}):
                import importlib

                import osimflow.storage as storage_mod

                importlib.reload(storage_mod)
                from osimflow.storage import S3ArtifactStorage

                store = S3ArtifactStorage(
                    bucket="my-artifacts",
                    prefix="campaign-123",
                    presigned_url_expiration_seconds=7200,
                )
                url = store.generate_presigned_url("base/model.osm", expiration_seconds=1800)

                fake_client.generate_presigned_url.assert_called_once_with(
                    "get_object",
                    Params={"Bucket": "my-artifacts", "Key": "campaign-123/base/model.osm"},
                    ExpiresIn=1800,
                )

    def test_generate_presigned_url_failure(
        self, mock_boto3: tuple[MagicMock, MagicMock, MagicMock]
    ) -> None:
        """Test pre-signed URL generation raises OSError on failure."""
        fake_boto3, fake_session, fake_client = mock_boto3
        fake_client.generate_presigned_url.side_effect = Exception("URL generation failed")

        with patch("boto3.Session", return_value=fake_session):
            with patch.dict("sys.modules", {"boto3": fake_boto3}):
                import importlib

                import osimflow.storage as storage_mod

                importlib.reload(storage_mod)
                from osimflow.storage import S3ArtifactStorage

                store = S3ArtifactStorage(bucket="my-artifacts", prefix="")

                with pytest.raises(OSError, match="presigned URL generation failed"):
                    store.generate_presigned_url("base/model.osm")

    def test_artifact_exists_true(self, mock_boto3: tuple[MagicMock, MagicMock, MagicMock]) -> None:
        """Test artifact_exists returns True when artifact exists."""
        fake_boto3, fake_session, fake_client = mock_boto3

        with patch("boto3.Session", return_value=fake_session):
            with patch.dict("sys.modules", {"boto3": fake_boto3}):
                import importlib

                import osimflow.storage as storage_mod

                importlib.reload(storage_mod)
                from osimflow.storage import S3ArtifactStorage

                store = S3ArtifactStorage(bucket="my-artifacts", prefix="campaign-123")
                exists = store.artifact_exists("base/model.osm")

                assert exists is True
                fake_client.head_object.assert_called_once_with(
                    Bucket="my-artifacts", Key="campaign-123/base/model.osm"
                )

    def test_artifact_exists_false(
        self, mock_boto3: tuple[MagicMock, MagicMock, MagicMock]
    ) -> None:
        """Test artifact_exists returns False when artifact does not exist."""
        fake_boto3, fake_session, fake_client = mock_boto3
        fake_client.head_object.side_effect = Exception("Not found")

        with patch("boto3.Session", return_value=fake_session):
            with patch.dict("sys.modules", {"boto3": fake_boto3}):
                import importlib

                import osimflow.storage as storage_mod

                importlib.reload(storage_mod)
                from osimflow.storage import S3ArtifactStorage

                store = S3ArtifactStorage(bucket="my-artifacts", prefix="campaign-123")
                exists = store.artifact_exists("base/model.osm")

                assert exists is False

    def test_list_artifacts_success(
        self, mock_boto3: tuple[MagicMock, MagicMock, MagicMock]
    ) -> None:
        """Test list_artifacts returns sorted list of artifacts."""
        fake_boto3, fake_session, fake_client = mock_boto3

        mock_paginator = MagicMock()
        fake_client.get_paginator.return_value = mock_paginator
        mock_paginator.paginate.return_value = iter(
            [
                {
                    "Contents": [
                        {"Key": "campaign-123/base/model.osm"},
                        {"Key": "campaign-123/weather.epw"},
                    ]
                },
            ]
        )

        with patch("boto3.Session", return_value=fake_session):
            with patch.dict("sys.modules", {"boto3": fake_boto3}):
                import importlib

                import osimflow.storage as storage_mod

                importlib.reload(storage_mod)
                from osimflow.storage import S3ArtifactStorage

                store = S3ArtifactStorage(bucket="my-artifacts", prefix="campaign-123")
                artifacts = store.list_artifacts()

                assert artifacts == ["base/model.osm", "weather.epw"]

    def test_list_artifacts_with_filter(
        self, mock_boto3: tuple[MagicMock, MagicMock, MagicMock]
    ) -> None:
        """Test list_artifacts with prefix filter."""
        fake_boto3, fake_session, fake_client = mock_boto3

        mock_paginator = MagicMock()
        fake_client.get_paginator.return_value = mock_paginator
        mock_paginator.paginate.return_value = iter(
            [
                {"Contents": [{"Key": "campaign-123/base/model.osm"}]},
            ]
        )

        with patch("boto3.Session", return_value=fake_session):
            with patch.dict("sys.modules", {"boto3": fake_boto3}):
                import importlib

                import osimflow.storage as storage_mod

                importlib.reload(storage_mod)
                from osimflow.storage import S3ArtifactStorage

                store = S3ArtifactStorage(bucket="my-artifacts", prefix="campaign-123")
                artifacts = store.list_artifacts(prefix="base")

                assert artifacts == ["base/model.osm"]
                fake_client.get_paginator.assert_called_with("list_objects_v2")

    def test_list_artifacts_empty(self, mock_boto3: tuple[MagicMock, MagicMock, MagicMock]) -> None:
        """Test list_artifacts returns empty list when no artifacts."""
        fake_boto3, fake_session, fake_client = mock_boto3

        mock_paginator = MagicMock()
        fake_client.get_paginator.return_value = mock_paginator
        mock_paginator.paginate.return_value = iter([{}])

        with patch("boto3.Session", return_value=fake_session):
            with patch.dict("sys.modules", {"boto3": fake_boto3}):
                import importlib

                import osimflow.storage as storage_mod

                importlib.reload(storage_mod)
                from osimflow.storage import S3ArtifactStorage

                store = S3ArtifactStorage(bucket="my-artifacts", prefix="campaign-123")
                artifacts = store.list_artifacts()

                assert artifacts == []

    def test_list_artifacts_failure(
        self, mock_boto3: tuple[MagicMock, MagicMock, MagicMock]
    ) -> None:
        """Test list_artifacts raises OSError on failure."""
        fake_boto3, fake_session, fake_client = mock_boto3
        fake_client.get_paginator.side_effect = Exception("List failed")

        with patch("boto3.Session", return_value=fake_session):
            with patch.dict("sys.modules", {"boto3": fake_boto3}):
                import importlib

                import osimflow.storage as storage_mod

                importlib.reload(storage_mod)
                from osimflow.storage import S3ArtifactStorage

                store = S3ArtifactStorage(bucket="my-artifacts", prefix="")

                with pytest.raises(OSError, match="list_artifacts failed"):
                    store.list_artifacts()

    def test_lazy_client_initialization(
        self, mock_boto3: tuple[MagicMock, MagicMock, MagicMock]
    ) -> None:
        """Test that S3 client is created lazily on first use."""
        fake_boto3, fake_session, fake_client = mock_boto3
        with patch("boto3.Session", return_value=fake_session):
            with patch.dict("sys.modules", {"boto3": fake_boto3}):
                import importlib

                import osimflow.storage as storage_mod

                importlib.reload(storage_mod)
                from osimflow.storage import S3ArtifactStorage

                store = S3ArtifactStorage(bucket="my-artifacts", prefix="")

                assert store._client is None

                _ = store.client

                fake_session.client.assert_called_once()

    def test_client_cached_after_first_access(
        self, mock_boto3: tuple[MagicMock, MagicMock, MagicMock]
    ) -> None:
        """Test that S3 client is cached after first access."""
        fake_boto3, fake_session, fake_client = mock_boto3
        with patch("boto3.Session", return_value=fake_session):
            with patch.dict("sys.modules", {"boto3": fake_boto3}):
                import importlib

                import osimflow.storage as storage_mod

                importlib.reload(storage_mod)
                from osimflow.storage import S3ArtifactStorage

                store = S3ArtifactStorage(bucket="my-artifacts", prefix="")

                client1 = store.client
                client2 = store.client

                assert client1 is client2
                fake_session.client.assert_called_once()


class TestS3ArtifactStorageIntegration:
    """Integration-style tests for S3ArtifactStorage with real file operations."""

    def test_upload_and_presigned_url_flow(self, tmp_path: Path) -> None:
        """Test the complete upload and pre-signed URL flow."""
        osm_path = tmp_path / "model.osm"
        osm_path.write_text("<OpenStudioModel/>", encoding="utf-8")

        fake_boto3 = MagicMock()
        fake_session = MagicMock()
        fake_client = MagicMock()
        fake_session.client.return_value = fake_client
        fake_boto3.Session.return_value = fake_session

        expected_url = "https://bucket.s3.amazonaws.com/key?signature=abc123"
        fake_client.upload_file.return_value = None
        fake_client.generate_presigned_url.return_value = expected_url

        with patch("boto3.Session", return_value=fake_session):
            with patch.dict("sys.modules", {"boto3": fake_boto3}):
                import importlib

                import osimflow.storage as storage_mod

                importlib.reload(storage_mod)
                from osimflow.storage import S3ArtifactStorage

                store = S3ArtifactStorage(
                    bucket="test-bucket",
                    prefix="test-campaign",
                    region="us-east-1",
                )

                store.upload_artifact(osm_path, "base/model.osm")
                url = store.generate_presigned_url("base/model.osm")

                fake_client.upload_file.assert_called_once()
                fake_client.generate_presigned_url.assert_called_once()

                assert url == expected_url


class TestS3EndpointTLSPolicy:
    """Endpoint TLS validation for S3 backends (issue #1386).

    Mirrors the Redis ``_validate_redis_url`` policy: reject non-`https://`
    endpoints unless the operator explicitly opts in via
    ``allow_insecure_endpoint=True`` / ``--allow-insecure-storage-endpoint``.
    Loopback hosts (``localhost``, ``127.0.0.1``, ``::1``, ``0.0.0.0``) are
    exempt because they never traverse a real network — same exemption the
    Redis validator uses.
    """

    @pytest.fixture
    def mock_boto3(self) -> tuple[MagicMock, MagicMock, MagicMock]:
        """Mock boto3 at the import site (boto3.Session() called in __init__)."""
        fake_boto3 = MagicMock()
        fake_session = MagicMock()
        fake_client = MagicMock()
        fake_session.client.return_value = fake_client
        fake_boto3.Session.return_value = fake_session
        return fake_boto3, fake_session, fake_client

    def test_http_non_loopback_rejected_by_default(self) -> None:
        """http:// to a non-loopback host raises ValueError (issue #1386)."""
        from osimflow.storage import _validate_storage_endpoint

        with pytest.raises(ValueError, match="issue #1386"):
            _validate_storage_endpoint("http://minio.example.com:9000")

    def test_http_non_loopback_accepted_when_allow_insecure(self, caplog) -> None:
        """allow_insecure=True accepts http:// with a WARNING (issue #1386)."""
        from osimflow.storage import _validate_storage_endpoint

        with caplog.at_level(logging.WARNING, logger="osimflow.storage"):
            _validate_storage_endpoint("http://minio.example.com:9000", allow_insecure=True)
        assert any("INSECURE S3 endpoint" in rec.message for rec in caplog.records)

    def test_http_loopback_passes_without_opt_in(self) -> None:
        """http://loopback is allowed even without allow_insecure (no network)."""
        from osimflow.storage import _validate_storage_endpoint

        # Should not raise.
        _validate_storage_endpoint("http://localhost:9000")
        _validate_storage_endpoint("http://127.0.0.1:9000")
        _validate_storage_endpoint("http://0.0.0.0:9000")
        _validate_storage_endpoint("http://[::1]:9000")

    def test_https_always_passes(self) -> None:
        """https:// endpoints are accepted unconditionally."""
        from osimflow.storage import _validate_storage_endpoint

        # Should not raise.
        _validate_storage_endpoint("https://s3.amazonaws.com")
        _validate_storage_endpoint("https://minio.example.com:9000")

    def test_none_and_empty_pass(self) -> None:
        """None and empty endpoint URLs are no-ops."""
        from osimflow.storage import _validate_storage_endpoint

        # Should not raise.
        _validate_storage_endpoint(None)
        _validate_storage_endpoint("")
        _validate_storage_endpoint(None, allow_insecure=True)
        _validate_storage_endpoint("", allow_insecure=True)

    def test_unknown_scheme_rejected(self) -> None:
        """A non-http(s) scheme raises ValueError (issue #1386)."""
        from osimflow.storage import _validate_storage_endpoint

        with pytest.raises(ValueError, match="issue #1386"):
            _validate_storage_endpoint("ftp://s3.example.com")
        with pytest.raises(ValueError, match="issue #1386"):
            _validate_storage_endpoint("s3://my-bucket/")

    def test_s3_storage_rejects_plaintext_endpoint(self) -> None:
        """S3Storage(bucket, endpoint_url='http://...') raises ValueError."""
        from osimflow.storage import S3Storage

        with pytest.raises(ValueError, match="issue #1386"):
            S3Storage(bucket="b", endpoint_url="http://minio.example.com:9000")

    def test_s3_storage_accepts_https_endpoint(self, mock_boto3) -> None:
        """S3Storage with https:// endpoint constructs cleanly."""
        fake_boto3, fake_session, _ = mock_boto3
        with patch("boto3.Session", return_value=fake_session):
            with patch.dict("sys.modules", {"boto3": fake_boto3}):
                import importlib

                import osimflow.storage as storage_mod

                importlib.reload(storage_mod)
                from osimflow.storage import S3Storage

                store = S3Storage(bucket="b", endpoint_url="https://minio.example.com:9000")
                assert store.endpoint_url == "https://minio.example.com:9000"

    def test_s3_storage_allows_plaintext_with_opt_in(self, mock_boto3, caplog) -> None:
        """S3Storage with http + allow_insecure_endpoint=True emits WARNING."""
        fake_boto3, fake_session, _ = mock_boto3
        with patch("boto3.Session", return_value=fake_session):
            with patch.dict("sys.modules", {"boto3": fake_boto3}):
                import importlib

                import osimflow.storage as storage_mod

                importlib.reload(storage_mod)
                from osimflow.storage import S3Storage

                with caplog.at_level(logging.WARNING, logger="osimflow.storage"):
                    store = S3Storage(
                        bucket="b",
                        endpoint_url="http://minio.example.com:9000",
                        allow_insecure_endpoint=True,
                    )
                assert store.endpoint_url == "http://minio.example.com:9000"
                assert any("INSECURE S3 endpoint" in rec.message for rec in caplog.records)

    def test_s3_storage_loopback_endpoint_passes(self, mock_boto3) -> None:
        """S3Storage with http://localhost passes without allow_insecure."""
        fake_boto3, fake_session, _ = mock_boto3
        with patch("boto3.Session", return_value=fake_session):
            with patch.dict("sys.modules", {"boto3": fake_boto3}):
                import importlib

                import osimflow.storage as storage_mod

                importlib.reload(storage_mod)
                from osimflow.storage import S3Storage

                # No opt-in needed for loopback.
                store = S3Storage(bucket="b", endpoint_url="http://localhost:9000")
                assert store.endpoint_url == "http://localhost:9000"

    def test_s3_artifact_storage_rejects_plaintext_endpoint(self) -> None:
        """S3ArtifactStorage rejects http:// endpoint by default (issue #1386)."""
        from osimflow.storage import S3ArtifactStorage

        with pytest.raises(ValueError, match="issue #1386"):
            S3ArtifactStorage(bucket="b", endpoint_url="http://minio.example.com:9000")

    def test_s3_artifact_storage_accepts_https_endpoint(self, mock_boto3) -> None:
        """S3ArtifactStorage with https:// endpoint constructs cleanly."""
        fake_boto3, fake_session, _ = mock_boto3
        with patch("boto3.Session", return_value=fake_session):
            with patch.dict("sys.modules", {"boto3": fake_boto3}):
                import importlib

                import osimflow.storage as storage_mod

                importlib.reload(storage_mod)
                from osimflow.storage import S3ArtifactStorage

                store = S3ArtifactStorage(bucket="b", endpoint_url="https://minio.example.com:9000")
                assert store.endpoint_url == "https://minio.example.com:9000"

    def test_s3_artifact_storage_allows_plaintext_with_opt_in(self, mock_boto3, caplog) -> None:
        """S3ArtifactStorage with http + opt-in emits WARNING."""
        fake_boto3, fake_session, _ = mock_boto3
        with patch("boto3.Session", return_value=fake_session):
            with patch.dict("sys.modules", {"boto3": fake_boto3}):
                import importlib

                import osimflow.storage as storage_mod

                importlib.reload(storage_mod)
                from osimflow.storage import S3ArtifactStorage

                with caplog.at_level(logging.WARNING, logger="osimflow.storage"):
                    store = S3ArtifactStorage(
                        bucket="b",
                        endpoint_url="http://minio.example.com:9000",
                        allow_insecure_endpoint=True,
                    )
                assert store.endpoint_url == "http://minio.example.com:9000"
                assert any("INSECURE S3 endpoint" in rec.message for rec in caplog.records)

    def test_build_result_storage_rejects_plaintext_endpoint(self) -> None:
        """build_result_storage enforces the TLS policy (issue #1386)."""
        from osimflow.storage import build_result_storage

        with pytest.raises(ValueError, match="issue #1386"):
            build_result_storage(
                backend="s3",
                bucket="my-bucket",
                endpoint_url="http://minio.example.com:9000",
            )

    def test_build_result_storage_passes_https_endpoint(self) -> None:
        """build_result_storage with https:// endpoint constructs cleanly."""
        from osimflow.storage import S3Storage, build_result_storage

        store = build_result_storage(
            backend="s3",
            bucket="my-bucket",
            endpoint_url="https://minio.example.com:9000",
        )
        assert isinstance(store, S3Storage)
        assert store.endpoint_url == "https://minio.example.com:9000"

    def test_build_result_storage_passes_opt_in_flag(self, caplog) -> None:
        """build_result_storage forwards allow_insecure_endpoint to the backend."""
        from osimflow.storage import S3Storage, build_result_storage

        with caplog.at_level(logging.WARNING, logger="osimflow.storage"):
            store = build_result_storage(
                backend="s3",
                bucket="my-bucket",
                endpoint_url="http://minio.example.com:9000",
                allow_insecure_endpoint=True,
            )
        assert isinstance(store, S3Storage)
        assert store.endpoint_url == "http://minio.example.com:9000"
        assert any("INSECURE S3 endpoint" in rec.message for rec in caplog.records)
