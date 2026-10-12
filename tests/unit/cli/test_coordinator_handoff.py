"""Unit tests for Coordinator handoff idempotency + status-URL (issue #630).

Covers the S6 acceptance criteria:

* A duplicate handoff for the same ``Idempotency-Key`` returns the *original*
  ``campaign_id`` instead of creating a second campaign.
* The endpoint returns HTTP ``202 Accepted`` with an absolute ``status_url``.
* Distinct keys create distinct campaigns; omitting the header preserves the
  legacy "new campaign every time" behaviour.
* Read-only mode rejects handoff (403).
* ``status_url`` resolution lets the CLI reconnect.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="osimflow[api] extra required")
pytest.importorskip("slowapi", reason="osimflow[api] extra required")
from fastapi.testclient import TestClient

from osimflow.api import coordinator as coord
from osimflow.api import create_app
from osimflow.handoff_record import IDEMPOTENCY_KEY_HEADER

HANDOFF_URL = "/api/v1/coordinator/handoff"


def _payload(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "name": "test-campaign",
        "n_samples": 4,
        "executor": "aws_batch",
        "openstudio_version": "3.11.0",
    }
    base.update(overrides)
    return base


@pytest.fixture(autouse=True)
def _isolate_campaign_store() -> None:
    """Clear both in-memory stores before/after every test."""
    coord._campaigns.clear()
    coord._idempotency_keys.clear()
    yield
    coord._campaigns.clear()
    coord._idempotency_keys.clear()


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    """Read-write TestClient (handoff requires the ``write`` permission)."""
    app = create_app(outdir=tmp_path, read_only=False)
    return TestClient(app)


@pytest.fixture
def readonly_client(tmp_path: Path) -> TestClient:
    """Read-only TestClient (no API key -> handoff forbidden)."""
    app = create_app(outdir=tmp_path, read_only=True)
    return TestClient(app)


class TestHandoffIdempotency:
    def test_handoff_returns_202_with_status_url(self, client: TestClient) -> None:
        resp = client.post(HANDOFF_URL, json=_payload(), headers={IDEMPOTENCY_KEY_HEADER: "k-1"})
        assert resp.status_code == 202
        body = resp.json()
        assert body["status"] == "pending"
        cid = body["campaign_id"]
        # status_url is absolute and points at this campaign's status endpoint.
        assert body["status_url"] == (f"http://testserver/api/v1/coordinator/campaigns/{cid}")
        assert cid in body["status_url"]

    def test_duplicate_idempotency_key_returns_same_campaign_id(self, client: TestClient) -> None:
        headers = {IDEMPOTENCY_KEY_HEADER: "dup-key-123"}
        first = client.post(HANDOFF_URL, json=_payload(), headers=headers).json()
        second = client.post(
            HANDOFF_URL, json=_payload(name="different-name"), headers=headers
        ).json()

        # Core acceptance criterion: same key -> same campaign_id, not a second
        # campaign. Even though the second payload differs, the key wins.
        assert second["campaign_id"] == first["campaign_id"]
        assert second["status_url"] == first["status_url"]
        # And only one campaign exists in the store.
        assert len(coord._campaigns) == 1
        assert len(coord._idempotency_keys) == 1
        assert coord._idempotency_keys["dup-key-123"] == first["campaign_id"]

    def test_distinct_keys_create_distinct_campaigns(self, client: TestClient) -> None:
        a = client.post(HANDOFF_URL, json=_payload(), headers={IDEMPOTENCY_KEY_HEADER: "k-a"})
        b = client.post(HANDOFF_URL, json=_payload(), headers={IDEMPOTENCY_KEY_HEADER: "k-b"})
        assert a.status_code == 202 and b.status_code == 202
        assert a.json()["campaign_id"] != b.json()["campaign_id"]
        assert len(coord._campaigns) == 2

    def test_no_idempotency_key_always_creates_new(self, client: TestClient) -> None:
        """Omitting the header preserves the legacy non-idempotent behaviour."""
        a = client.post(HANDOFF_URL, json=_payload())
        b = client.post(HANDOFF_URL, json=_payload())
        assert a.json()["campaign_id"] != b.json()["campaign_id"]
        assert len(coord._campaigns) == 2
        assert coord._idempotency_keys == {}

    def test_replay_message_indicates_idempotent_noop(self, client: TestClient) -> None:
        headers = {IDEMPOTENCY_KEY_HEADER: "replay-key"}
        client.post(HANDOFF_URL, json=_payload(), headers=headers)
        replay = client.post(HANDOFF_URL, json=_payload(), headers=headers).json()
        assert "idempotent replay" in replay["message"]


class TestHandoffPermissions:
    def test_read_only_mode_rejects_handoff(self, readonly_client: TestClient) -> None:
        resp = readonly_client.post(
            HANDOFF_URL, json=_payload(), headers={IDEMPOTENCY_KEY_HEADER: "k"}
        )
        assert resp.status_code == 403
        # No campaign or idempotency entry created on rejection.
        assert coord._campaigns == {}
        assert coord._idempotency_keys == {}


class TestStatusUrlResolution:
    def test_status_url_is_pollable_and_returns_campaign(self, client: TestClient) -> None:
        """The status_url the CLI persists must resolve to the live campaign."""
        handed = client.post(
            HANDOFF_URL, json=_payload(), headers={IDEMPOTENCY_KEY_HEADER: "k"}
        ).json()
        status_url = handed["status_url"].replace("http://testserver", "")

        live = client.get(status_url)
        assert live.status_code == 200
        live_body = live.json()
        assert live_body["campaign_id"] == handed["campaign_id"]
        assert live_body["status"] == "pending"
        assert live_body["n_samples"] == 4

    def test_status_url_for_unknown_campaign_is_404(self, client: TestClient) -> None:
        live = client.get("/api/v1/coordinator/campaigns/does-not-exist")
        assert live.status_code == 404
