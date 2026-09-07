"""Security tests for measure archive extraction and permission gates.

Covers issues #1625 and #1626:

* **#1625 — tar-slip:** a crafted tar.gz containing a symlink member
  (``d -> <dir outside dest>``) followed by a regular member written
  through the link (``d/evil``) must be rejected *before* extraction
  writes anything outside the destination, and the zip branch must
  enforce per-entry / total decompressed-size caps (zip bombs).
* **#1626 — permission gates:** ``upload_measure``,
  ``patch_uploaded_measure`` and ``delete_uploaded_measure`` must call
  ``require_permission(request, "readwrite")`` so a viewer-role API key
  cannot install executable measure code or delete other users'
  measures (mirrors the issue-#1551 coordinator test pattern).
"""

from __future__ import annotations

import io
import json
import tarfile
import zipfile
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi", reason="osimflow[api] extra required")
pytest.importorskip("slowapi", reason="osimflow[api] extra required")
pytest.importorskip("boto3", reason="osimflow[aws] extra required")
from fastapi.testclient import TestClient

from osimflow.api import create_app, hash_api_key
from osimflow.api import measures as measures_api

UPLOAD_URL = "/api/v1/measures/upload"

RO_KEY = "ro-key-1"
RW_KEY = "rw-key-1"

_USERS: list[dict[str, Any]] = [
    {"key": RO_KEY, "user_id": "reader", "role": "readonly"},
    {"key": RW_KEY, "user_id": "writer", "role": "readwrite"},
]


# ---------------------------------------------------------------------------
# Archive-building helpers
# ---------------------------------------------------------------------------


def _add_measure_stub(tf: tarfile.TarFile, measure_name: str = "TestMeasure") -> None:
    """Add a minimal valid measure.rb so the archive looks like a measure bundle."""
    d = tarfile.TarInfo(f"{measure_name}")
    d.type = tarfile.DIRTYPE
    tf.addfile(d)
    payload = b"# measure stub\n"
    src = tarfile.TarInfo(f"{measure_name}/measure.rb")
    src.size = len(payload)
    tf.addfile(src, io.BytesIO(payload))


def _add_symlink(tf: tarfile.TarFile, name: str, linkname: str) -> None:
    link = tarfile.TarInfo(name)
    link.type = tarfile.SYMTYPE
    link.linkname = linkname
    tf.addfile(link)


def _add_file_through_link(tf: tarfile.TarFile, name: str, payload: bytes = b"pwned\n") -> None:
    evil = tarfile.TarInfo(name)
    evil.size = len(payload)
    tf.addfile(evil, io.BytesIO(payload))


def _write_archive(tmp_path: Path, data: bytes, suffix: str = ".tar.gz") -> Path:
    archive_path = tmp_path / f"archive{suffix}"
    archive_path.write_bytes(data)
    return archive_path


# ---------------------------------------------------------------------------
# #1625 — tar-slip symlink escape
# ---------------------------------------------------------------------------


class TestTarSlipRejected:
    def test_symlink_then_file_through_link_rejected(self, tmp_path: Path) -> None:
        """Symlink ``d -> outside`` + member ``d/evil`` must be rejected pre-write."""
        outside_dir = tmp_path / "outside"
        outside_dir.mkdir()
        dest_dir = tmp_path / "dest"

        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            _add_measure_stub(tf)
            _add_symlink(tf, "TestMeasure/d", str(outside_dir))
            _add_file_through_link(tf, "TestMeasure/d/evil")
        archive_path = _write_archive(tmp_path, buf.getvalue())

        with pytest.raises(ValueError, match="links outside extraction directory"):
            measures_api._extract_measure_archive(archive_path, dest_dir)

        # The escape target must NOT have been written.
        assert not (outside_dir / "evil").exists()
        # ... and neither the evil member nor the link landed in dest_dir.
        assert not (dest_dir / "TestMeasure" / "d").exists()

    def test_relative_symlink_escape_rejected(self, tmp_path: Path) -> None:
        """Relative symlink ``d -> ../../outside`` must also be rejected."""
        outside_dir = tmp_path / "outside"
        outside_dir.mkdir()
        dest_dir = tmp_path / "dest" / "deep" / "nested"

        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            _add_measure_stub(tf)
            _add_symlink(tf, "TestMeasure/d", "../../../../../outside")
            _add_file_through_link(tf, "TestMeasure/d/evil")
        archive_path = _write_archive(tmp_path, buf.getvalue())

        with pytest.raises(ValueError, match="links outside extraction directory"):
            measures_api._extract_measure_archive(archive_path, dest_dir)

        assert not (outside_dir / "evil").exists()

    def test_parent_traversal_member_rejected(self, tmp_path: Path) -> None:
        """A plain ``../`` regular-file member is still rejected (pre-#1625 check kept)."""
        dest_dir = tmp_path / "dest"

        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            _add_measure_stub(tf)
            _add_file_through_link(tf, "../evil")
        archive_path = _write_archive(tmp_path, buf.getvalue())

        with pytest.raises(ValueError, match="escapes extraction directory"):
            measures_api._extract_measure_archive(archive_path, dest_dir)

    def test_benign_tar_gz_still_extracts(self, tmp_path: Path) -> None:
        """A legitimate tar.gz measure bundle extracts and returns its measure dir."""
        dest_dir = tmp_path / "dest"

        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            _add_measure_stub(tf, "GoodMeasure")
        archive_path = _write_archive(tmp_path, buf.getvalue())

        measure_dir = measures_api._extract_measure_archive(archive_path, dest_dir)
        assert measure_dir.name == "GoodMeasure"
        assert (measure_dir / "measure.rb").is_file()


# ---------------------------------------------------------------------------
# #1625 — zip-bomb decompressed-size caps
# ---------------------------------------------------------------------------


class TestZipBombRejected:
    def test_high_ratio_entry_exceeding_per_entry_cap_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A high-compression-ratio entry past the per-entry cap is rejected."""
        monkeypatch.setattr(measures_api, "_MAX_ARCHIVE_ENTRY_BYTES", 4 * 1024)
        dest_dir = tmp_path / "dest"

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("BombMeasure/measure.rb", "# stub\n")
            # 64 KiB of zeros deflates to <100 bytes (~900:1 ratio)
            zf.writestr("BombMeasure/resources/blob.bin", b"\0" * (64 * 1024))
        archive_path = _write_archive(tmp_path, buf.getvalue(), suffix=".zip")

        with pytest.raises(ValueError, match="per-entry cap"):
            measures_api._extract_measure_archive(archive_path, dest_dir)

    def test_total_decompressed_size_cap_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Entries individually under the cap but over it in total are rejected."""
        monkeypatch.setattr(measures_api, "_MAX_ARCHIVE_ENTRY_BYTES", 4 * 1024)
        monkeypatch.setattr(measures_api, "_MAX_ARCHIVE_TOTAL_BYTES", 6 * 1024)
        dest_dir = tmp_path / "dest"

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("BombMeasure/measure.rb", "# stub\n")
            zf.writestr("BombMeasure/resources/blob1.bin", b"\0" * (4 * 1024))
            zf.writestr("BombMeasure/resources/blob2.bin", b"\0" * (4 * 1024))
        archive_path = _write_archive(tmp_path, buf.getvalue(), suffix=".zip")

        with pytest.raises(ValueError, match="total cap"):
            measures_api._extract_measure_archive(archive_path, dest_dir)

    def test_default_caps_are_sane(self) -> None:
        """Module-level caps exist and are ordered (guards against typos)."""
        assert measures_api._MAX_ARCHIVE_ENTRY_BYTES == 512 * 1024 * 1024
        assert measures_api._MAX_ARCHIVE_TOTAL_BYTES == 2 * 1024 * 1024 * 1024
        assert measures_api._MAX_ARCHIVE_TOTAL_BYTES >= measures_api._MAX_ARCHIVE_ENTRY_BYTES


# ---------------------------------------------------------------------------
# #1626 — permission gates on measures upload/patch/delete
# ---------------------------------------------------------------------------


def _make_ruby_measure_zip(measure_name: str = "TestRubyMeasure") -> bytes:
    """Build a valid Ruby measure zip for upload testing."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{measure_name}/measure.rb", f"# Class: {measure_name}\n# stub\n")
    return buf.getvalue()


def _keys_file(tmp_path: Path) -> Path:
    """Write a 0600 multi-user keys file (hashed at rest, issue #1552)."""
    users = [
        {**{k: v for k, v in u.items() if k != "key"}, "key_sha256": hash_api_key(u["key"])}
        for u in _USERS
    ]
    path = tmp_path / "api_keys.json"
    path.write_text(json.dumps({"users": users}))
    path.chmod(0o600)
    return path


@pytest.fixture
def multiuser_client(tmp_path: Path) -> TestClient:
    """Multi-user key store with readonly/readwrite roles (read_only=False)."""
    return TestClient(
        create_app(outdir=tmp_path, api_keys_file=_keys_file(tmp_path), read_only=False)
    )


def _upload(client: TestClient, key: str, zip_bytes: bytes | None = None) -> Any:
    return client.post(
        UPLOAD_URL,
        files={
            "file": ("m.zip", io.BytesIO(zip_bytes or _make_ruby_measure_zip()), "application/zip")
        },
        headers={"X-API-Key": key},
    )


class TestMeasuresPermissionGates:
    def test_readonly_role_forbidden_on_upload(self, multiuser_client: TestClient) -> None:
        resp = _upload(multiuser_client, RO_KEY)
        assert resp.status_code == 403
        assert "readwrite" in resp.json()["detail"].lower()

    def test_readwrite_role_can_upload(self, multiuser_client: TestClient) -> None:
        resp = _upload(multiuser_client, RW_KEY)
        assert resp.status_code == 200
        assert resp.json()["name"] == "TestRubyMeasure"

    def test_missing_key_rejected_401(self, multiuser_client: TestClient) -> None:
        assert _upload(multiuser_client, "no-key-supplied").status_code == 401

    def test_forbidden_before_404_or_503(self, tmp_path: Path) -> None:
        """403 fires before registry lookup — authz is the first gate."""
        client = TestClient(
            create_app(outdir=tmp_path, api_keys_file=_keys_file(tmp_path), read_only=False)
        )
        ro = {"X-API-Key": RO_KEY}
        # PATCH/DELETE a nonexistent measure id: readonly sees 403, not 404.
        assert (
            client.patch(
                "/api/v1/measures/by-id/00000000-0000-0000-0000-000000000000",
                json={"taxonomy": "X"},
                headers=ro,
            ).status_code
            == 403
        )
        assert (
            client.delete(
                "/api/v1/measures/by-id/00000000-0000-0000-0000-000000000000",
                headers=ro,
            ).status_code
            == 403
        )

    def test_readonly_forbidden_on_patch_and_delete(self, multiuser_client: TestClient) -> None:
        uploaded = _upload(multiuser_client, RW_KEY)
        assert uploaded.status_code == 200
        mid = uploaded.json()["measure_id"]
        ro = {"X-API-Key": RO_KEY}

        resp = multiuser_client.patch(
            f"/api/v1/measures/by-id/{mid}", json={"taxonomy": "Evil"}, headers=ro
        )
        assert resp.status_code == 403

        resp = multiuser_client.delete(f"/api/v1/measures/by-id/{mid}", headers=ro)
        assert resp.status_code == 403
        # The measure survived the forbidden delete.
        assert multiuser_client.get(f"/api/v1/measures/by-id/{mid}", headers=ro).status_code == 200

    def test_readwrite_can_patch_and_delete(self, multiuser_client: TestClient) -> None:
        uploaded = _upload(multiuser_client, RW_KEY)
        mid = uploaded.json()["measure_id"]
        rw = {"X-API-Key": RW_KEY}

        resp = multiuser_client.patch(
            f"/api/v1/measures/by-id/{mid}", json={"taxonomy": "A.B"}, headers=rw
        )
        assert resp.status_code == 200
        assert resp.json()["taxonomy"] == "A.B"

        assert (
            multiuser_client.delete(f"/api/v1/measures/by-id/{mid}", headers=rw).status_code == 200
        )

    def test_noauth_read_only_mode_denies_upload(self, tmp_path: Path) -> None:
        """No key store + server read_only=True: writes denied (pre-#1626 fallback)."""
        client = TestClient(create_app(outdir=tmp_path, read_only=True))
        assert _upload(client, "ignored").status_code == 403

    def test_noauth_read_write_mode_allows_upload(self, tmp_path: Path) -> None:
        client = TestClient(create_app(outdir=tmp_path, read_only=False))
        assert _upload(client, "ignored").status_code == 200

    def test_evil_tar_upload_rejected_400(self, tmp_path: Path) -> None:
        """The tar-slip archive is rejected at the API layer with a 400."""
        outside_dir = tmp_path / "outside"
        outside_dir.mkdir()

        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            _add_measure_stub(tf)
            _add_symlink(tf, "TestMeasure/d", str(outside_dir))
            _add_file_through_link(tf, "TestMeasure/d/evil")

        client = TestClient(create_app(outdir=tmp_path, read_only=False))
        resp = client.post(
            UPLOAD_URL,
            files={"file": ("evil.tar.gz", io.BytesIO(buf.getvalue()), "application/gzip")},
        )
        assert resp.status_code == 400
        assert "links outside extraction directory" in resp.json()["detail"]
        assert not (outside_dir / "evil").exists()
