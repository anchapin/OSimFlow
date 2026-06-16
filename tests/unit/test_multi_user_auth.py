"""Tests for multi-user authentication and authorization (issue #395)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="osimflow[api] extra required")
pytest.importorskip("slowapi", reason="osimflow[api] extra required")
from fastapi.testclient import TestClient

from osimflow.api import (
    _ADMIN,
    _READONLY,
    _READWRITE,
    APIKeyUser,
    MultiUserAPIKeyStore,
    create_app,
)


@pytest.fixture
def tmp_outdir(tmp_path: Path) -> Path:
    """Create a temporary output directory with a sample run.json."""
    run_json = {
        "schema_version": 1,
        "campaign_id": "test-campaign-001",
        "started_at": 1000.0,
        "finished_at": 2000.0,
        "config_summary": {"executor": "local", "n_samples": 5},
        "steps": [],
        "per_sample": [],
    }
    (tmp_path / "run.json").write_text(json.dumps(run_json))
    return tmp_path


class TestMultiUserAPIKeyStore:
    """Tests for MultiUserAPIKeyStore."""

    def test_from_single_key(self) -> None:
        store = MultiUserAPIKeyStore.from_single_key("secret-key")
        assert store.single_key == "secret-key"
        assert store.users == []

    def test_from_users(self) -> None:
        users = [
            {"key": "key1", "user_id": "alice", "role": _ADMIN},
            {"key": "key2", "user_id": "bob", "role": _READONLY},
        ]
        store = MultiUserAPIKeyStore.from_users(users)
        assert store.single_key is None
        assert len(store.users) == 2

    def test_validate_single_key_valid(self) -> None:
        store = MultiUserAPIKeyStore.from_single_key("secret-key")
        user = store.validate("secret-key")
        assert user is not None
        assert user.user_id == "default"
        assert user.role == _ADMIN

    def test_validate_single_key_invalid(self) -> None:
        store = MultiUserAPIKeyStore.from_single_key("secret-key")
        user = store.validate("wrong-key")
        assert user is None

    def test_validate_single_key_none(self) -> None:
        store = MultiUserAPIKeyStore.from_single_key("secret-key")
        user = store.validate(None)
        assert user is None

    def test_validate_multi_user_valid_admin(self) -> None:
        users = [
            {"key": "admin-key", "user_id": "alice", "role": _ADMIN},
            {"key": "readonly-key", "user_id": "bob", "role": _READONLY},
        ]
        store = MultiUserAPIKeyStore.from_users(users)
        user = store.validate("admin-key")
        assert user is not None
        assert user.user_id == "alice"
        assert user.role == _ADMIN
        assert user.has_permission(_ADMIN)
        assert user.has_permission(_READWRITE)
        assert user.has_permission(_READONLY)

    def test_validate_multi_user_valid_readonly(self) -> None:
        users = [
            {"key": "admin-key", "user_id": "alice", "role": _ADMIN},
            {"key": "readonly-key", "user_id": "bob", "role": _READONLY},
        ]
        store = MultiUserAPIKeyStore.from_users(users)
        user = store.validate("readonly-key")
        assert user is not None
        assert user.user_id == "bob"
        assert user.role == _READONLY
        assert user.has_permission(_READONLY)
        assert not user.has_permission(_READWRITE)
        assert not user.has_permission(_ADMIN)

    def test_validate_multi_user_invalid_key(self) -> None:
        users = [
            {"key": "admin-key", "user_id": "alice", "role": _ADMIN},
        ]
        store = MultiUserAPIKeyStore.from_users(users)
        user = store.validate("wrong-key")
        assert user is None

    def test_validate_multi_user_default_role(self) -> None:
        """Users without explicit role get readonly."""
        users = [
            {"key": "no-role-key", "user_id": "charlie"},
        ]
        store = MultiUserAPIKeyStore.from_users(users)
        user = store.validate("no-role-key")
        assert user is not None
        assert user.role == _READONLY


class TestAPIKeyUser:
    """Tests for APIKeyUser."""

    def test_admin_has_all_permissions(self) -> None:
        user = APIKeyUser(key="key", user_id="alice", role=_ADMIN)
        assert user.has_permission(_ADMIN)
        assert user.has_permission(_READWRITE)
        assert user.has_permission(_READONLY)

    def test_readwrite_has_subset_permissions(self) -> None:
        user = APIKeyUser(key="key", user_id="bob", role=_READWRITE)
        assert not user.has_permission(_ADMIN)
        assert user.has_permission(_READWRITE)
        assert user.has_permission(_READONLY)

    def test_readonly_has_minimal_permissions(self) -> None:
        user = APIKeyUser(key="key", user_id="charlie", role=_READONLY)
        assert not user.has_permission(_ADMIN)
        assert not user.has_permission(_READWRITE)
        assert user.has_permission(_READONLY)


class TestMultiUserAuth:
    """Tests for multi-user authentication via API."""

    def test_multi_user_api_keys_file(self, tmp_outdir: Path) -> None:
        """Test loading API keys from a file."""
        keys_file = tmp_outdir / "api_keys.json"
        keys_file.write_text(
            json.dumps(
                {
                    "users": [
                        {"key": "admin-key", "user_id": "alice", "role": "admin"},
                        {"key": "readonly-key", "user_id": "bob", "role": "readonly"},
                    ]
                }
            )
        )
        app = create_app(outdir=tmp_outdir, api_keys_file=keys_file)
        client = TestClient(app)

        # Admin key should work for everything
        resp = client.get("/api/v1/campaign", headers={"X-API-Key": "admin-key"})
        assert resp.status_code == 200

        # Readonly key should also work for reads
        resp = client.get("/api/v1/campaign", headers={"X-API-Key": "readonly-key"})
        assert resp.status_code == 200

        # Wrong key should fail
        resp = client.get("/api/v1/campaign", headers={"X-API-Key": "wrong-key"})
        assert resp.status_code == 401

    def test_single_key_backward_compat(self, tmp_outdir: Path) -> None:
        """Test that single API key mode still works."""
        app = create_app(outdir=tmp_outdir, api_key="single-secret")
        client = TestClient(app)

        # Valid key works
        resp = client.get("/api/v1/campaign", headers={"X-API-Key": "single-secret"})
        assert resp.status_code == 200

        # Invalid key fails
        resp = client.get("/api/v1/campaign", headers={"X-API-Key": "wrong"})
        assert resp.status_code == 401

    def test_api_key_file_missing_users(self, tmp_path: Path) -> None:
        """Test that empty users list raises error."""
        keys_file = tmp_path / "empty_keys.json"
        keys_file.write_text(json.dumps({"users": []}))
        with pytest.raises(ValueError, match="No users found"):
            create_app(outdir=tmp_path, api_keys_file=keys_file)

    def test_api_key_file_invalid_json(self, tmp_path: Path) -> None:
        """Test that invalid JSON raises error."""
        keys_file = tmp_path / "invalid_keys.json"
        keys_file.write_text("not valid json")
        with pytest.raises(ValueError, match="Invalid api_keys_file"):
            create_app(outdir=tmp_path, api_keys_file=keys_file)


class TestPermissionLevels:
    """Tests for permission level constants."""

    def test_permission_constants_exist(self) -> None:
        assert _ADMIN == "admin"
        assert _READWRITE == "readwrite"
        assert _READONLY == "readonly"

    def test_permission_hierarchy(self) -> None:
        """Admin > readwrite > readonly."""
        admin = APIKeyUser(key="k", user_id="a", role=_ADMIN)
        readwrite = APIKeyUser(key="k", user_id="a", role=_READWRITE)
        readonly = APIKeyUser(key="k", user_id="a", role=_READONLY)

        # Admin can do everything
        assert admin.has_permission(_ADMIN)
        assert admin.has_permission(_READWRITE)
        assert admin.has_permission(_READONLY)

        # Readwrite can do readwrite and readonly, but not admin
        assert not readwrite.has_permission(_ADMIN)
        assert readwrite.has_permission(_READWRITE)
        assert readwrite.has_permission(_READONLY)

        # Readonly can only do readonly
        assert not readonly.has_permission(_ADMIN)
        assert not readonly.has_permission(_READWRITE)
        assert readonly.has_permission(_READONLY)


class TestRBACWriteOperations:
    """Tests for RBAC enforcement on write operations (issue #442, #395)."""

    def _make_keys_file(self, tmp_path: Path, users: list[dict[str, str]]) -> Path:
        """Create an API keys file with the given users."""
        keys_file = tmp_path / "api_keys.json"
        keys_file.write_text(json.dumps({"users": users}))
        return keys_file

    def test_admin_can_create_variables(self, tmp_outdir: Path, tmp_path: Path) -> None:
        """Admin users can create variables via the API."""
        keys_file = self._make_keys_file(
            tmp_path,
            [
                {"key": "admin-key", "user_id": "alice", "role": _ADMIN},
            ],
        )
        app = create_app(outdir=tmp_outdir, api_keys_file=keys_file)
        client = TestClient(app)

        resp = client.post(
            "/api/v1/variables",
            json={"name": "wwr", "distribution": "uniform", "min": 0.1, "max": 0.6},
            headers={"X-API-Key": "admin-key"},
        )
        assert resp.status_code == 201, f"Admin should be able to create variables: {resp.json()}"

    def test_readwrite_can_create_variables(self, tmp_outdir: Path, tmp_path: Path) -> None:
        """Readwrite users can create variables via the API."""
        keys_file = self._make_keys_file(
            tmp_path,
            [
                {"key": "rw-key", "user_id": "bob", "role": _READWRITE},
            ],
        )
        app = create_app(outdir=tmp_outdir, api_keys_file=keys_file)
        client = TestClient(app)

        resp = client.post(
            "/api/v1/variables",
            json={"name": "wwr", "distribution": "uniform", "min": 0.1, "max": 0.6},
            headers={"X-API-Key": "rw-key"},
        )
        assert resp.status_code == 201, f"Readwrite should be able to create variables: {resp.json()}"

    def test_readonly_cannot_create_variables(self, tmp_outdir: Path, tmp_path: Path) -> None:
        """Readonly users get 403 when creating variables."""
        keys_file = self._make_keys_file(
            tmp_path,
            [
                {"key": "readonly-key", "user_id": "charlie", "role": _READONLY},
            ],
        )
        app = create_app(outdir=tmp_outdir, api_keys_file=keys_file)
        client = TestClient(app)

        resp = client.post(
            "/api/v1/variables",
            json={"name": "wwr", "distribution": "uniform", "min": 0.1, "max": 0.6},
            headers={"X-API-Key": "readonly-key"},
        )
        assert resp.status_code == 403
        assert "readwrite" in resp.json()["detail"].lower()

    def test_readonly_can_read_variables(self, tmp_outdir: Path, tmp_path: Path) -> None:
        """Readonly users can read variables."""
        keys_file = self._make_keys_file(
            tmp_path,
            [
                {"key": "readonly-key", "user_id": "charlie", "role": _READONLY},
            ],
        )
        app = create_app(outdir=tmp_outdir, api_keys_file=keys_file)
        client = TestClient(app)

        resp = client.get("/api/v1/variables", headers={"X-API-Key": "readonly-key"})
        assert resp.status_code == 200

    def test_readonly_cannot_delete_variables(self, tmp_outdir: Path, tmp_path: Path) -> None:
        """Readonly users get 403 when deleting variables."""
        keys_file = self._make_keys_file(
            tmp_path,
            [
                {"key": "readonly-key", "user_id": "charlie", "role": _READONLY},
            ],
        )
        app = create_app(outdir=tmp_outdir, api_keys_file=keys_file)
        client = TestClient(app)

        resp = client.delete("/api/v1/variables/wwr", headers={"X-API-Key": "readonly-key"})
        assert resp.status_code == 403
        assert "readwrite" in resp.json()["detail"].lower()

    def test_readonly_cannot_upload_files(self, tmp_outdir: Path, tmp_path: Path) -> None:
        """Readonly users get 403 when uploading files."""
        keys_file = self._make_keys_file(
            tmp_path,
            [
                {"key": "readonly-key", "user_id": "charlie", "role": _READONLY},
            ],
        )
        app = create_app(outdir=tmp_outdir, api_keys_file=keys_file)
        client = TestClient(app)

        resp = client.post(
            "/api/v1/files/upload?category=seed_model",
            headers={"X-API-Key": "readonly-key"},
            files={"file": ("model.osm", b"seed model content", "application/json")},
        )
        assert resp.status_code == 403
        assert "readwrite" in resp.json()["detail"].lower()

    def test_readonly_cannot_delete_files(self, tmp_outdir: Path, tmp_path: Path) -> None:
        """Readonly users get 403 when deleting files."""
        keys_file = self._make_keys_file(
            tmp_path,
            [
                {"key": "readonly-key", "user_id": "charlie", "role": _READONLY},
            ],
        )
        app = create_app(outdir=tmp_outdir, api_keys_file=keys_file)
        client = TestClient(app)

        resp = client.delete("/api/v1/files/some-file-id", headers={"X-API-Key": "readonly-key"})
        assert resp.status_code == 403
        assert "readwrite" in resp.json()["detail"].lower()

    def test_readonly_cannot_create_pat_analysis(self, tmp_outdir: Path, tmp_path: Path) -> None:
        """Readonly users get 403 when creating PAT analyses."""
        keys_file = self._make_keys_file(
            tmp_path,
            [
                {"key": "readonly-key", "user_id": "charlie", "role": _READONLY},
            ],
        )
        app = create_app(outdir=tmp_outdir, api_keys_file=keys_file)
        client = TestClient(app)

        resp = client.post(
            "/api/v1/pat/analyses",
            json={"osa_path": "/some/path.osa", "template_sim_package": "/tmp/pkg"},
            headers={"X-API-Key": "readonly-key"},
        )
        assert resp.status_code == 403
        assert "readwrite" in resp.json()["detail"].lower()

    def test_admin_can_create_pat_analysis(self, tmp_outdir: Path, tmp_path: Path) -> None:
        """Admin users can create PAT analyses."""
        keys_file = self._make_keys_file(
            tmp_path,
            [
                {"key": "admin-key", "user_id": "alice", "role": _ADMIN},
            ],
        )
        app = create_app(outdir=tmp_outdir, api_keys_file=keys_file)
        client = TestClient(app)

        resp = client.post(
            "/api/v1/pat/analyses",
            json={"osa_path": "/some/path.osa", "template_sim_package": "/tmp/pkg"},
            headers={"X-API-Key": "admin-key"},
        )
        assert resp.status_code in (201, 400, 422), f"Admin should be able to call PAT endpoint: {resp.json()}"

    def test_single_key_admin_can_write(self, tmp_outdir: Path) -> None:
        """Single API key mode grants admin access for writes."""
        app = create_app(outdir=tmp_outdir, api_key="single-secret", read_only=False)
        client = TestClient(app)

        resp = client.post(
            "/api/v1/variables",
            json={"name": "wwr", "distribution": "uniform", "min": 0.1, "max": 0.6},
            headers={"X-API-Key": "single-secret"},
        )
        assert resp.status_code == 201, f"Single key admin should write: {resp.json()}"

    def test_no_auth_no_writes(self, tmp_outdir: Path) -> None:
        """When no auth is configured, writes should be denied (read_only=True by default)."""
        app = create_app(outdir=tmp_outdir, api_key=None, read_only=True)
        client = TestClient(app)

        resp = client.post(
            "/api/v1/variables",
            json={"name": "wwr", "distribution": "uniform", "min": 0.1, "max": 0.6},
        )
        assert resp.status_code == 403
