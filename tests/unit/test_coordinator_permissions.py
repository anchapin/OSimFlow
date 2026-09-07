"""Coordinator endpoint permission enforcement tests (issue #1551).

The pre-#1551 coordinator routes either discarded the result of
``get_user_permission(request, "read")`` (an invalid level —
``_PERMISSION_LEVELS`` is ``readonly/readwrite/admin``) or enforced the
equally invalid ``"write"`` level, so in multi-user mode every caller was
denied on write endpoints while read endpoints enforced nothing.  Every
route now calls :func:`osimflow.api.auth.require_permission`, a raising
helper that is self-sufficient even when the router is mounted without
the global ``APIKeyMiddleware``.

Coverage required by the issue's acceptance criteria:

* **(a) No-auth mode** — guarded endpoints respond normally when no key
  store is configured (reads pass even under ``read_only=True``; writes
  keep honouring the server-level ``read_only`` flag exactly as before).
* **(b) Readonly role** — a readonly-role user hitting a read-write
  coordinator endpoint receives 403.
* **(c) Readwrite role** — a readwrite-role user hitting a read-write
  endpoint succeeds (202/200).
* **(d) Missing/invalid key** — 401 when a key store is configured but
  no (valid) key is presented, including when the router is mounted
  under an app *without* the auth middleware (the latent-bypass
  scenario from the issue).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi", reason="osimflow[api] extra required")
pytest.importorskip("slowapi", reason="osimflow[api] extra required")
pytest.importorskip("boto3", reason="osimflow[aws] extra required")
from fastapi import FastAPI
from fastapi.testclient import TestClient

from osimflow.api import MultiUserAPIKeyStore, create_app, hash_api_key, require_permission
from osimflow.api import coordinator as coord

CAMPAIGNS_URL = "/api/v1/coordinator/campaigns"
HANDOFF_URL = "/api/v1/coordinator/handoff"

RO_KEY = "ro-key-1"
RW_KEY = "rw-key-1"
ADM_KEY = "adm-key-1"

_USERS = [
    {"key": RO_KEY, "user_id": "reader", "role": "readonly"},
    {"key": RW_KEY, "user_id": "writer", "role": "readwrite"},
    {"key": ADM_KEY, "user_id": "boss", "role": "admin"},
]


def _handoff_payload(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "name": "perm-test-campaign",
        "n_samples": 2,
        "executor": "aws_batch",
        "openstudio_version": "3.11.0",
    }
    base.update(overrides)
    return base


def _seed_campaign(campaign_id: str = "camp-perm-1") -> dict[str, Any]:
    """Insert a campaign record with two samples into the in-memory store."""
    rec: dict[str, Any] = {
        "campaign_id": campaign_id,
        "name": "perm-test",
        "status": "pending",
        "created_at": 0.0,
        "updated_at": 0.0,
        "created_by": "test",
        "n_samples": 2,
        "executor": "aws_batch",
        "openstudio_version": "3.11.0",
        "samples": [{"wwr": 0.4}, {"wwr": 0.6}],
        "result_storage_bucket": None,
        "result_status": "unavailable",
        "array_job_id": None,
    }
    coord._campaigns[campaign_id] = rec
    return rec


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


@pytest.fixture(autouse=True)
def _isolate_campaign_store() -> None:
    """Clear both in-memory stores before/after every test."""
    coord._campaigns.clear()
    coord._idempotency_keys.clear()
    yield
    coord._campaigns.clear()
    coord._idempotency_keys.clear()


@pytest.fixture
def noauth_readonly_client(tmp_path: Path) -> TestClient:
    """No key store configured; server-level read_only=True (create_app default)."""
    return TestClient(create_app(outdir=tmp_path, read_only=True))


@pytest.fixture
def noauth_readwrite_client(tmp_path: Path) -> TestClient:
    """No key store configured; server-level read_only=False."""
    return TestClient(create_app(outdir=tmp_path, read_only=False))


@pytest.fixture
def multiuser_client(tmp_path: Path) -> TestClient:
    """Multi-user key store with readonly/readwrite/admin roles (read_only=False)."""
    return TestClient(
        create_app(outdir=tmp_path, api_keys_file=_keys_file(tmp_path), read_only=False)
    )


@pytest.fixture
def singlekey_client(tmp_path: Path) -> TestClient:
    """Single-key auth; server read_only=True (reads allowed, writes denied)."""
    return TestClient(create_app(outdir=tmp_path, api_key="single-secret", read_only=True))


def _bare_app(*, key_store: MultiUserAPIKeyStore | None, read_only: bool = False) -> FastAPI:
    """Mount ONLY the coordinator router — no APIKeyMiddleware.

    This is the latent-bypass scenario from the issue: enforcement must
    not depend on the global middleware being present.
    """
    app = FastAPI()
    app.include_router(coord.coordinator_router)
    app.state.read_only = read_only
    app.state.api_key_store = key_store
    return app


# ---------------------------------------------------------------------------
# (a) No-auth mode — behaviour preserved
# ---------------------------------------------------------------------------


class TestNoAuthMode:
    def test_reads_pass_even_when_read_only(
        self, noauth_readonly_client: TestClient, noauth_readwrite_client: TestClient
    ) -> None:
        _seed_campaign()
        for client in (noauth_readonly_client, noauth_readwrite_client):
            assert client.get(CAMPAIGNS_URL).status_code == 200
            assert client.get(f"{CAMPAIGNS_URL}/camp-perm-1").status_code == 200
            assert client.get(f"{CAMPAIGNS_URL}/camp-perm-1/samples").status_code == 200
            assert client.get(f"{CAMPAIGNS_URL}/camp-perm-1/samples/0").status_code == 200
            assert client.get(f"{CAMPAIGNS_URL}/camp-perm-1/results").status_code == 200

    def test_write_denied_when_read_only(self, noauth_readonly_client: TestClient) -> None:
        """read_only=True keeps rejecting handoff (pre-#1551 fallback preserved)."""
        resp = noauth_readonly_client.post(HANDOFF_URL, json=_handoff_payload())
        assert resp.status_code == 403

    def test_write_allowed_when_read_write(self, noauth_readwrite_client: TestClient) -> None:
        resp = noauth_readwrite_client.post(HANDOFF_URL, json=_handoff_payload())
        assert resp.status_code == 202


# ---------------------------------------------------------------------------
# Multi-user mode — real per-role enforcement (the #1551 fix)
# ---------------------------------------------------------------------------


class TestMultiUserMode:
    def test_readonly_role_can_read(self, multiuser_client: TestClient) -> None:
        _seed_campaign()
        resp = multiuser_client.get(CAMPAIGNS_URL, headers={"X-API-Key": RO_KEY})
        assert resp.status_code == 200
        assert len(resp.json()) == 1

    def test_readonly_role_forbidden_on_write(self, multiuser_client: TestClient) -> None:
        resp = multiuser_client.post(
            HANDOFF_URL, json=_handoff_payload(), headers={"X-API-Key": RO_KEY}
        )
        assert resp.status_code == 403

    def test_readonly_role_forbidden_on_aggregate_before_404(
        self, multiuser_client: TestClient
    ) -> None:
        """403 fires before the campaign lookup — authz is the first gate."""
        url = f"{CAMPAIGNS_URL}/does-not-exist/aggregate"
        assert multiuser_client.post(url, headers={"X-API-Key": RO_KEY}).status_code == 403
        # A readwrite caller passes the gate and reaches the 404/409 paths.
        assert multiuser_client.post(url, headers={"X-API-Key": RW_KEY}).status_code == 404

    def test_readonly_role_forbidden_on_admin_endpoints(self, multiuser_client: TestClient) -> None:
        _seed_campaign()
        ro = {"X-API-Key": RO_KEY}
        rw = {"X-API-Key": RW_KEY}
        # PATCH status, POST submit-array, GET poll-array, POST notify: admin-only.
        assert (
            multiuser_client.patch(
                f"{CAMPAIGNS_URL}/camp-perm-1/status?status=running", headers=ro
            ).status_code
            == 403
        )
        assert (
            multiuser_client.post(
                f"{CAMPAIGNS_URL}/camp-perm-1/submit-array",
                json={"job_queue": "q", "job_definition": "jd", "array_size": 2},
                headers=ro,
            ).status_code
            == 403
        )
        assert (
            multiuser_client.get(f"{CAMPAIGNS_URL}/camp-perm-1/poll-array", headers=ro).status_code
            == 403
        )
        assert (
            multiuser_client.post(
                f"{CAMPAIGNS_URL}/camp-perm-1/notify", json={}, headers=ro
            ).status_code
            == 403
        )
        # readwrite is still below admin on these endpoints.
        assert (
            multiuser_client.patch(
                f"{CAMPAIGNS_URL}/camp-perm-1/status?status=running", headers=rw
            ).status_code
            == 403
        )

    def test_readwrite_role_can_handoff(self, multiuser_client: TestClient) -> None:
        resp = multiuser_client.post(
            HANDOFF_URL, json=_handoff_payload(), headers={"X-API-Key": RW_KEY}
        )
        assert resp.status_code == 202

    def test_admin_role_can_handoff_and_patch_status(self, multiuser_client: TestClient) -> None:
        adm = {"X-API-Key": ADM_KEY}
        created = multiuser_client.post(HANDOFF_URL, json=_handoff_payload(), headers=adm)
        assert created.status_code == 202
        campaign_id = created.json()["campaign_id"]
        patched = multiuser_client.patch(
            f"{CAMPAIGNS_URL}/{campaign_id}/status?status=running", headers=adm
        )
        assert patched.status_code == 200
        assert patched.json()["status"] == "running"

    def test_missing_key_rejected_401(self, multiuser_client: TestClient) -> None:
        assert multiuser_client.get(CAMPAIGNS_URL).status_code == 401

    def test_invalid_key_rejected_401(self, multiuser_client: TestClient) -> None:
        resp = multiuser_client.get(CAMPAIGNS_URL, headers={"X-API-Key": "wrong-key"})
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Single-key mode — reads allowed, writes follow server read_only
# ---------------------------------------------------------------------------


class TestSingleKeyMode:
    def test_reads_allowed_in_read_only_mode(self, singlekey_client: TestClient) -> None:
        _seed_campaign()
        resp = singlekey_client.get(CAMPAIGNS_URL, headers={"X-API-Key": "single-secret"})
        assert resp.status_code == 200

    def test_write_denied_in_read_only_mode(self, singlekey_client: TestClient) -> None:
        resp = singlekey_client.post(
            HANDOFF_URL, json=_handoff_payload(), headers={"X-API-Key": "single-secret"}
        )
        assert resp.status_code == 403

    def test_missing_key_rejected_401(self, singlekey_client: TestClient) -> None:
        assert singlekey_client.get(CAMPAIGNS_URL).status_code == 401


# ---------------------------------------------------------------------------
# (d) Defense-in-depth — router mounted WITHOUT the auth middleware
# ---------------------------------------------------------------------------


class TestMountedWithoutMiddleware:
    """The authz point must hold even without APIKeyMiddleware (issue #1551)."""

    def test_no_key_401_without_middleware(self) -> None:
        client = TestClient(_bare_app(key_store=MultiUserAPIKeyStore.from_users(_USERS)))
        assert client.get(CAMPAIGNS_URL).status_code == 401
        assert client.post(HANDOFF_URL, json=_handoff_payload()).status_code == 401

    def test_invalid_key_401_without_middleware(self) -> None:
        client = TestClient(_bare_app(key_store=MultiUserAPIKeyStore.from_users(_USERS)))
        resp = client.get(CAMPAIGNS_URL, headers={"X-API-Key": "bogus"})
        assert resp.status_code == 401

    def test_readwrite_role_enforced_without_middleware(self) -> None:
        client = TestClient(_bare_app(key_store=MultiUserAPIKeyStore.from_users(_USERS)))
        assert (
            client.post(
                HANDOFF_URL, json=_handoff_payload(), headers={"X-API-Key": RO_KEY}
            ).status_code
            == 403
        )
        assert (
            client.post(
                HANDOFF_URL, json=_handoff_payload(), headers={"X-API-Key": RW_KEY}
            ).status_code
            == 202
        )

    def test_no_key_store_allows_reads_without_middleware(self) -> None:
        """No store + no middleware (local dev mounting): reads still pass."""
        client = TestClient(_bare_app(key_store=None, read_only=True))
        assert client.get(CAMPAIGNS_URL).status_code == 200

    def test_query_param_key_rejected_401_without_middleware(self) -> None:
        client = TestClient(_bare_app(key_store=MultiUserAPIKeyStore.from_users(_USERS)))
        resp = client.get(f"{CAMPAIGNS_URL}?api_key={RO_KEY}")
        assert resp.status_code == 401
        assert "X-API-Key" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# Helper-level guard — invalid levels fail loudly (the #1551 root cause)
# ---------------------------------------------------------------------------


class TestRequirePermissionHelper:
    def test_invalid_level_raises_value_error(self) -> None:
        from fastapi import Request

        request = Request(scope={"type": "http"})
        with pytest.raises(ValueError, match="Invalid permission level 'read'"):
            require_permission(request, "read")
        with pytest.raises(ValueError, match="Invalid permission level 'write'"):
            require_permission(request, "write")
