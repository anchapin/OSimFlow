"""Tests for file upload/download API endpoints (issue #273)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="osimflow[api] extra required")
pytest.importorskip("slowapi", reason="osimflow[api] extra required")
from fastapi.testclient import TestClient

from osimflow.api import create_app

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_outdir(tmp_path: Path) -> Path:
    """Create a temporary output directory with a sample run.json."""
    run_json = {
        "schema_version": 1,
        "campaign_id": "test-campaign-files",
        "started_at": 1000.0,
        "finished_at": 2000.0,
        "config_summary": {"executor": "local", "n_samples": 5},
        "steps": [],
        "per_sample": [],
    }
    (tmp_path / "run.json").write_text(json.dumps(run_json))
    return tmp_path


@pytest.fixture
def rw_client(tmp_outdir: Path) -> TestClient:
    """Read-write TestClient (required for upload/delete)."""
    app = create_app(outdir=tmp_outdir, read_only=False)
    return TestClient(app)


@pytest.fixture
def ro_client(tmp_outdir: Path) -> TestClient:
    """Read-only TestClient (default)."""
    app = create_app(outdir=tmp_outdir, read_only=True)
    return TestClient(app)


# ---------------------------------------------------------------------------
# Upload tests
# ---------------------------------------------------------------------------


class TestFileUpload:
    """Tests for POST /api/v1/files/upload."""

    def test_upload_seed_model(self, rw_client: TestClient) -> None:
        content = b'{"model_type": "OS:Model"}'
        resp = rw_client.post(
            "/api/v1/files/upload?category=seed_model",
            files={"file": ("building.osm", content, "application/json")},
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["filename"] == "building.osm"
        assert data["category"] == "seed_model"
        assert data["size_bytes"] == len(content)
        assert data["file_id"]
        assert data["path"]

    def test_upload_measure_rb(self, rw_client: TestClient) -> None:
        content = b"# measure script\nputs 'hello'"
        resp = rw_client.post(
            "/api/v1/files/upload?category=measure",
            files={"file": ("my_measure.rb", content)},
        )
        assert resp.status_code == 201
        assert resp.json()["category"] == "measure"

    def test_upload_measure_py(self, rw_client: TestClient) -> None:
        content = b"def measure(model)\n  pass\n"
        resp = rw_client.post(
            "/api/v1/files/upload?category=measure",
            files={"file": ("my_measure.py", content)},
        )
        assert resp.status_code == 201
        assert resp.json()["filename"] == "my_measure.py"

    def test_upload_weather_epw(self, rw_client: TestClient) -> None:
        content = b"LOCATION,USA,CO,DENVER\n"
        resp = rw_client.post(
            "/api/v1/files/upload?category=weather",
            files={"file": ("denver.epw", content)},
        )
        assert resp.status_code == 201
        assert resp.json()["category"] == "weather"

    def test_upload_config_yml(self, rw_client: TestClient) -> None:
        content = b"variables:\n  - name: test\n"
        resp = rw_client.post(
            "/api/v1/files/upload?category=config",
            files={"file": ("variables.yml", content)},
        )
        assert resp.status_code == 201
        assert resp.json()["category"] == "config"

    def test_upload_config_yaml(self, rw_client: TestClient) -> None:
        content = b"variables:\n  - name: test\n"
        resp = rw_client.post(
            "/api/v1/files/upload?category=config",
            files={"file": ("variables.yaml", content)},
        )
        assert resp.status_code == 201
        assert resp.json()["filename"] == "variables.yaml"

    def test_upload_rejects_wrong_extension(self, rw_client: TestClient) -> None:
        """An .exe file should be rejected for any category."""
        resp = rw_client.post(
            "/api/v1/files/upload?category=seed_model",
            files={"file": ("malware.exe", b"MZ")},
        )
        assert resp.status_code == 400
        assert "not allowed" in resp.json()["detail"]

    def test_upload_rejects_wrong_category_for_ext(self, rw_client: TestClient) -> None:
        """An .osm file should be rejected for the weather category."""
        resp = rw_client.post(
            "/api/v1/files/upload?category=weather",
            files={"file": ("model.osm", b"{}")},
        )
        assert resp.status_code == 400

    def test_upload_rejects_unknown_category(self, rw_client: TestClient) -> None:
        resp = rw_client.post(
            "/api/v1/files/upload?category=executable",
            files={"file": ("test.osm", b"{}")},
        )
        assert resp.status_code == 400
        assert "Unknown category" in resp.json()["detail"]

    def test_upload_requires_read_write(self, ro_client: TestClient) -> None:
        """Upload must be rejected in read-only mode."""
        resp = ro_client.post(
            "/api/v1/files/upload?category=seed_model",
            files={"file": ("test.osm", b"{}")},
        )
        assert resp.status_code == 403

    def test_upload_no_filename_returns_422(self, rw_client: TestClient) -> None:
        """Upload without a filename should be rejected (422 from FastAPI validation)."""
        resp = rw_client.post(
            "/api/v1/files/upload?category=seed_model",
            files={"file": (None, b"{}")},
        )
        assert resp.status_code == 422

    def test_upload_overwrites_same_name(self, rw_client: TestClient) -> None:
        """Uploading a file with the same name should overwrite."""
        content_v1 = b"version 1"
        resp1 = rw_client.post(
            "/api/v1/files/upload?category=seed_model",
            files={"file": ("building.osm", content_v1)},
        )
        assert resp1.status_code == 201
        id_v1 = resp1.json()["file_id"]

        content_v2 = b"version 2 - longer content"
        resp2 = rw_client.post(
            "/api/v1/files/upload?category=seed_model",
            files={"file": ("building.osm", content_v2)},
        )
        assert resp2.status_code == 201
        id_v2 = resp2.json()["file_id"]

        # New file should have a new ID
        assert id_v2 != id_v1
        assert resp2.json()["size_bytes"] == len(content_v2)

    def test_upload_sanitizes_traversal_filename(self, rw_client: TestClient) -> None:
        """Path traversal in filename should be rejected."""
        resp = rw_client.post(
            "/api/v1/files/upload?category=seed_model",
            files={"file": ("../../../etc/passwd", b"root:x:0:0")},
        )
        assert resp.status_code == 400

    def test_upload_too_large(self, rw_client: TestClient) -> None:
        """Files exceeding 100 MB should be rejected (413)."""
        # We mock the size check by sending a request — in practice
        # the test client reads the full content into memory.  We test
        # with a smaller override approach by checking the error code
        # for a clearly oversized file.  This is a smoke test.
        from osimflow.api import files as files_mod

        original = files_mod.MAX_FILE_SIZE_BYTES
        try:
            files_mod.MAX_FILE_SIZE_BYTES = 10  # 10 bytes limit
            resp = rw_client.post(
                "/api/v1/files/upload?category=seed_model",
                files={"file": ("test.osm", b"This is more than ten bytes")},
            )
            assert resp.status_code == 413
        finally:
            files_mod.MAX_FILE_SIZE_BYTES = original

    def test_upload_no_outdir_returns_503(self) -> None:
        """Upload with no outdir configured returns 503."""
        app = create_app(outdir=None, read_only=False)
        client = TestClient(app)
        resp = client.post(
            "/api/v1/files/upload?category=seed_model",
            files={"file": ("test.osm", b"{}")},
        )
        assert resp.status_code == 503


# ---------------------------------------------------------------------------
# List tests
# ---------------------------------------------------------------------------


class TestFileList:
    """Tests for GET /api/v1/files."""

    def test_list_empty(self, rw_client: TestClient) -> None:
        resp = rw_client.get("/api/v1/files")
        assert resp.status_code == 200
        data = resp.json()
        assert data["files"] == []
        assert data["total"] == 0

    def test_list_after_upload(self, rw_client: TestClient) -> None:
        # Upload two files
        rw_client.post(
            "/api/v1/files/upload?category=seed_model",
            files={"file": ("a.osm", b"{}")},
        )
        rw_client.post(
            "/api/v1/files/upload?category=weather",
            files={"file": ("b.epw", b"LOCATION")},
        )

        resp = rw_client.get("/api/v1/files")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 2
        filenames = {f["filename"] for f in data["files"]}
        assert filenames == {"a.osm", "b.epw"}

    def test_list_filter_by_category(self, rw_client: TestClient) -> None:
        rw_client.post(
            "/api/v1/files/upload?category=seed_model",
            files={"file": ("a.osm", b"{}")},
        )
        rw_client.post(
            "/api/v1/files/upload?category=weather",
            files={"file": ("b.epw", b"LOCATION")},
        )

        resp = rw_client.get("/api/v1/files?category=weather")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["files"][0]["filename"] == "b.epw"
        assert data["files"][0]["category"] == "weather"

    def test_list_preserves_all_fields(self, rw_client: TestClient) -> None:
        rw_client.post(
            "/api/v1/files/upload?category=config",
            files={"file": ("vars.yml", b"variables:\n  - name: x\n")},
        )

        resp = rw_client.get("/api/v1/files")
        assert resp.status_code == 200
        f = resp.json()["files"][0]
        assert "file_id" in f
        assert f["filename"] == "vars.yml"
        assert f["category"] == "config"
        assert isinstance(f["size_bytes"], int)
        assert isinstance(f["path"], str)


# ---------------------------------------------------------------------------
# Download tests
# ---------------------------------------------------------------------------


class TestFileDownload:
    """Tests for GET /api/v1/files/{file_id}."""

    def test_download_full_file(self, rw_client: TestClient) -> None:
        content = b'{"model_type": "building"}'
        upload_resp = rw_client.post(
            "/api/v1/files/upload?category=seed_model",
            files={"file": ("building.osm", content)},
        )
        file_id = upload_resp.json()["file_id"]

        dl_resp = rw_client.get(f"/api/v1/files/{file_id}")
        assert dl_resp.status_code == 200
        assert dl_resp.content == content
        assert "Accept-Ranges" in dl_resp.headers
        assert dl_resp.headers["Content-Length"] == str(len(content))

    def test_download_content_type_osm(self, rw_client: TestClient) -> None:
        upload_resp = rw_client.post(
            "/api/v1/files/upload?category=seed_model",
            files={"file": ("test.osm", b"{}")},
        )
        file_id = upload_resp.json()["file_id"]

        dl_resp = rw_client.get(f"/api/v1/files/{file_id}")
        assert "application/json" in dl_resp.headers.get("content-type", "")

    def test_download_content_type_epw(self, rw_client: TestClient) -> None:
        upload_resp = rw_client.post(
            "/api/v1/files/upload?category=weather",
            files={"file": ("test.epw", b"LOCATION")},
        )
        file_id = upload_resp.json()["file_id"]

        dl_resp = rw_client.get(f"/api/v1/files/{file_id}")
        assert "text/plain" in dl_resp.headers.get("content-type", "")

    def test_download_content_type_yml(self, rw_client: TestClient) -> None:
        upload_resp = rw_client.post(
            "/api/v1/files/upload?category=config",
            files={"file": ("test.yml", b"key: value\n")},
        )
        file_id = upload_resp.json()["file_id"]

        dl_resp = rw_client.get(f"/api/v1/files/{file_id}")
        assert "text/yaml" in dl_resp.headers.get("content-type", "")

    def test_download_nonexistent_id(self, rw_client: TestClient) -> None:
        resp = rw_client.get("/api/v1/files/deadbeef")
        assert resp.status_code == 404

    def test_download_range_request(self, rw_client: TestClient) -> None:
        content = b"0123456789abcdef"
        upload_resp = rw_client.post(
            "/api/v1/files/upload?category=seed_model",
            files={"file": ("test.osm", content)},
        )
        file_id = upload_resp.json()["file_id"]

        dl_resp = rw_client.get(
            f"/api/v1/files/{file_id}",
            headers={"Range": "bytes=4-7"},
        )
        assert dl_resp.status_code == 206
        assert dl_resp.content == b"4567"
        assert dl_resp.headers["Content-Range"] == f"bytes 4-7/{len(content)}"
        assert dl_resp.headers["Content-Length"] == "4"

    def test_download_range_suffix(self, rw_client: TestClient) -> None:
        """bytes=-N should return the last N bytes."""
        content = b"0123456789abcdef"
        upload_resp = rw_client.post(
            "/api/v1/files/upload?category=seed_model",
            files={"file": ("test.osm", content)},
        )
        file_id = upload_resp.json()["file_id"]

        dl_resp = rw_client.get(
            f"/api/v1/files/{file_id}",
            headers={"Range": "bytes=-4"},
        )
        assert dl_resp.status_code == 206
        assert dl_resp.content == b"cdef"

    def test_download_range_open_end(self, rw_client: TestClient) -> None:
        """bytes=4- should return from byte 4 to the end."""
        content = b"0123456789abcdef"
        upload_resp = rw_client.post(
            "/api/v1/files/upload?category=seed_model",
            files={"file": ("test.osm", content)},
        )
        file_id = upload_resp.json()["file_id"]

        dl_resp = rw_client.get(
            f"/api/v1/files/{file_id}",
            headers={"Range": "bytes=12-"},
        )
        assert dl_resp.status_code == 206
        assert dl_resp.content == b"cdef"

    def test_download_invalid_range(self, rw_client: TestClient) -> None:
        """An unsatisfiable range should return 416."""
        content = b"short"
        upload_resp = rw_client.post(
            "/api/v1/files/upload?category=seed_model",
            files={"file": ("test.osm", content)},
        )
        file_id = upload_resp.json()["file_id"]

        dl_resp = rw_client.get(
            f"/api/v1/files/{file_id}",
            headers={"Range": "bytes=999-"},
        )
        assert dl_resp.status_code == 416

    def test_download_in_read_only_mode(self, ro_client: TestClient) -> None:
        """Download (GET) should work in read-only mode."""
        # First upload in rw mode
        app_rw = create_app(outdir=ro_client.app.state.outdir, read_only=False)
        client_rw = TestClient(app_rw)
        upload_resp = client_rw.post(
            "/api/v1/files/upload?category=seed_model",
            files={"file": ("test.osm", b"{}")},
        )
        file_id = upload_resp.json()["file_id"]

        # Download in read-only mode
        dl_resp = ro_client.get(f"/api/v1/files/{file_id}")
        assert dl_resp.status_code == 200

    def test_download_has_content_disposition(self, rw_client: TestClient) -> None:
        upload_resp = rw_client.post(
            "/api/v1/files/upload?category=seed_model",
            files={"file": ("my_building.osm", b"{}")},
        )
        file_id = upload_resp.json()["file_id"]

        dl_resp = rw_client.get(f"/api/v1/files/{file_id}")
        assert dl_resp.status_code == 200
        assert "attachment" in dl_resp.headers.get("Content-Disposition", "")
        assert "my_building.osm" in dl_resp.headers["Content-Disposition"]


# ---------------------------------------------------------------------------
# Delete tests
# ---------------------------------------------------------------------------


class TestFileDelete:
    """Tests for DELETE /api/v1/files/{file_id}."""

    def test_delete_removes_file(self, rw_client: TestClient) -> None:
        upload_resp = rw_client.post(
            "/api/v1/files/upload?category=seed_model",
            files={"file": ("delme.osm", b"{}")},
        )
        file_id = upload_resp.json()["file_id"]

        del_resp = rw_client.delete(f"/api/v1/files/{file_id}")
        assert del_resp.status_code == 200
        assert del_resp.json()["status"] == "deleted"
        assert del_resp.json()["file_id"] == file_id

        # Verify file is gone from listing
        list_resp = rw_client.get("/api/v1/files")
        assert list_resp.json()["total"] == 0

    def test_delete_nonexistent_id(self, rw_client: TestClient) -> None:
        resp = rw_client.delete("/api/v1/files/nonexistent")
        assert resp.status_code == 404

    def test_delete_requires_read_write(self, ro_client: TestClient) -> None:
        resp = ro_client.delete("/api/v1/files/anything")
        assert resp.status_code == 403

    def test_delete_twice_fails(self, rw_client: TestClient) -> None:
        upload_resp = rw_client.post(
            "/api/v1/files/upload?category=seed_model",
            files={"file": ("once.osm", b"{}")},
        )
        file_id = upload_resp.json()["file_id"]

        del1 = rw_client.delete(f"/api/v1/files/{file_id}")
        assert del1.status_code == 200

        del2 = rw_client.delete(f"/api/v1/files/{file_id}")
        assert del2.status_code == 404

    def test_delete_then_download_fails(self, rw_client: TestClient) -> None:
        upload_resp = rw_client.post(
            "/api/v1/files/upload?category=config",
            files={"file": ("vars.yml", b"x: 1\n")},
        )
        file_id = upload_resp.json()["file_id"]

        rw_client.delete(f"/api/v1/files/{file_id}")

        dl_resp = rw_client.get(f"/api/v1/files/{file_id}")
        assert dl_resp.status_code == 404


# ---------------------------------------------------------------------------
# Persistence tests
# ---------------------------------------------------------------------------


class TestFilePersistence:
    """Tests that file metadata persists across app instances."""

    def test_files_survive_app_restart(self, tmp_outdir: Path) -> None:
        content = b"persistent data"

        # Upload with first app instance
        app1 = create_app(outdir=tmp_outdir, read_only=False)
        client1 = TestClient(app1)
        upload_resp = client1.post(
            "/api/v1/files/upload?category=seed_model",
            files={"file": ("persistent.osm", content)},
        )
        file_id = upload_resp.json()["file_id"]
        assert upload_resp.status_code == 201

        # Verify file exists in new app instance
        app2 = create_app(outdir=tmp_outdir, read_only=False)
        client2 = TestClient(app2)
        list_resp = client2.get("/api/v1/files")
        assert list_resp.json()["total"] == 1
        assert list_resp.json()["files"][0]["file_id"] == file_id

        # Download with second instance
        dl_resp = client2.get(f"/api/v1/files/{file_id}")
        assert dl_resp.status_code == 200
        assert dl_resp.content == content


# ---------------------------------------------------------------------------
# Integration: upload → list → download → delete cycle
# ---------------------------------------------------------------------------


class TestFullCycle:
    """End-to-end upload → list → download → delete cycle."""

    def test_full_lifecycle(self, rw_client: TestClient) -> None:
        # 1. Upload
        content = b"full lifecycle test content"
        upload_resp = rw_client.post(
            "/api/v1/files/upload?category=weather",
            files={"file": ("lifecycle.epw", content)},
        )
        assert upload_resp.status_code == 201
        file_id = upload_resp.json()["file_id"]

        # 2. List — should contain the file
        list_resp = rw_client.get("/api/v1/files")
        assert list_resp.json()["total"] == 1
        assert list_resp.json()["files"][0]["file_id"] == file_id

        # 3. Download — full content
        dl_resp = rw_client.get(f"/api/v1/files/{file_id}")
        assert dl_resp.status_code == 200
        assert dl_resp.content == content

        # 4. Download — range request
        range_resp = rw_client.get(
            f"/api/v1/files/{file_id}",
            headers={"Range": "bytes=0-4"},
        )
        assert range_resp.status_code == 206
        assert range_resp.content == content[:5]

        # 5. Delete
        del_resp = rw_client.delete(f"/api/v1/files/{file_id}")
        assert del_resp.status_code == 200

        # 6. List — should be empty
        list_resp2 = rw_client.get("/api/v1/files")
        assert list_resp2.json()["total"] == 0

        # 7. Download — should 404
        dl_resp2 = rw_client.get(f"/api/v1/files/{file_id}")
        assert dl_resp2.status_code == 404
