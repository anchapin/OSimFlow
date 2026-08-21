"""Integration tests for the S4 Notification backends (issue #628).

Covers the headline criterion #6: a succeeded campaign triggers exactly
ONE ``send()`` call on the configured backend. Exercises the full
aggregation → auto-notify flow end-to-end through the FastAPI app with
a fake :class:`ResultStorage` (so no real S3 is hit) and a recording
:class:`NotifyBackend` injected in place of the real channel.

Also covers:

* The explicit ``POST /campaigns/{id}/notify`` endpoint dispatches via
  the right backend and returns ``status=sent``.
* A failed ``send()`` is logged but never reverts the campaign status
  (criterion #4 — best-effort).
* The campaign.succeeded payload carries ``download_url`` + ``expires_in_seconds``.
* A campaign with no configured channel completes silently (auto-notify
  is skipped, not an error).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi", reason="osimflow[api] extra required")
pytest.importorskip("slowapi", reason="osimflow[api] extra required")
pytest.importorskip("boto3", reason="osimflow[aws] extra required")
from fastapi.testclient import TestClient

from osimflow.api import coordinator as coord
from osimflow.api import create_app
from osimflow.notify import NotifyBackend, NullNotifyBackend
from osimflow.storage import ResultStorage

CAMPAIGN_ID = "01J0NOTIFY1"


# ---------------------------------------------------------------------------
# Fakes (mirrors tests/integration/test_coordinator_aggregator.py)
# ---------------------------------------------------------------------------


class _FakeObjectStorage(ResultStorage):
    """In-memory object store honouring the :class:`ResultStorage` ABC."""

    name = "fake"

    def __init__(self) -> None:
        self._objects: dict[str, bytes] = {}

    def put_text(self, remote_path: str, text: str) -> None:
        self._objects[remote_path] = text.encode("utf-8")

    @property
    def objects(self) -> dict[str, bytes]:
        return dict(self._objects)

    def upload_file(self, local_path: Path, remote_path: str) -> None:
        self._objects[remote_path] = local_path.read_bytes()

    def download_file(self, remote_path: str, local_path: Path) -> None:
        if remote_path not in self._objects:
            raise FileNotFoundError(f"fake-storage: missing {remote_path}")
        local_path.parent.mkdir(parents=True, exist_ok=True)
        local_path.write_bytes(self._objects[remote_path])

    def list_results(self, prefix: str = "") -> list[str]:
        return sorted(k for k in self._objects if k.startswith(prefix))


class _RecordingBackend(NotifyBackend):
    """Backend that records every ``send()`` for assertion."""

    def __init__(self, name: str = "recording") -> None:
        self.name = name
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.raise_on_send: Exception | None = None

    def send(self, event: str, payload: dict[str, Any]) -> None:
        self.calls.append((event, payload))
        if self.raise_on_send is not None:
            raise self.raise_on_send


def _kpis(sample_id: str, **kpis: float) -> str:
    return json.dumps({"sample_id": sample_id, "openstudio_version": None, "kpis": kpis})


def _manifest(
    sample_id: str,
    index: int,
    *,
    status: str,
    kpis_key: str | None,
    first_severe: str | None = None,
    exit_code: int = 0,
) -> str:
    return json.dumps(
        {
            "campaign_id": CAMPAIGN_ID,
            "sample_id": sample_id,
            "index": index,
            "status": status,
            "kpis_key": kpis_key,
            "exit_code": exit_code,
            "first_severe_error": first_severe,
            "finished_at": "2026-06-20T12:00:00Z",
        }
    )


def _seed_two_ok_samples(store: _FakeObjectStorage) -> None:
    """Populate *store* with two ok sample manifests + KPI files."""
    k0 = f"{CAMPAIGN_ID}/samples/s0001/kpis.json"
    store.put_text(k0, _kpis("s0001", eui=120.0, total_energy=48000.0))
    store.put_text(
        f"{CAMPAIGN_ID}/samples/s0001/_manifest.json",
        _manifest("s0001", 0, status="ok", kpis_key=k0),
    )
    k1 = f"{CAMPAIGN_ID}/samples/s0002/kpis.json"
    store.put_text(k1, _kpis("s0002", eui=150.0, total_energy=60000.0))
    store.put_text(
        f"{CAMPAIGN_ID}/samples/s0002/_manifest.json",
        _manifest("s0002", 1, status="ok", kpis_key=k1),
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_campaign_store() -> Any:
    """Clear the in-memory campaign + idempotency stores around every test."""
    coord._campaigns.clear()
    coord._idempotency_keys.clear()
    yield
    coord._campaigns.clear()
    coord._idempotency_keys.clear()


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    """FastAPI TestClient with write access (no API key configured)."""
    app = create_app(outdir=tmp_path, read_only=False)
    return TestClient(app)


def _seed_campaign(
    *,
    sns_topic_arn: str | None = None,
    notification_email: str | None = None,
    webhook_url: str | None = None,
    bucket: str = "test-bucket",
    status: str = "aggregating",
) -> dict[str, Any]:
    rec: dict[str, Any] = {
        "campaign_id": CAMPAIGN_ID,
        "name": "notify-test",
        "status": status,
        "created_at": 0.0,
        "updated_at": 0.0,
        "n_samples": 2,
        "executor": "aws_batch",
        "openstudio_version": "3.11.0",
        "array_job_id": "array-1",
        "result_storage_bucket": bucket,
        "result_status": "unavailable",
        "aggregated_results_key": None,
        "notification_email": notification_email,
        "sns_topic_arn": sns_topic_arn,
        "webhook_url": webhook_url,
        "payload": {
            "result_storage_backend": "s3",
            "result_storage_bucket": bucket,
            "algorithm": "lhs",
        },
    }
    coord._campaigns[CAMPAIGN_ID] = rec
    return rec


# ===========================================================================
# Aggregation auto-notify (issue #628 criterion #6)
# ===========================================================================


def test_aggregation_fires_exactly_one_send_for_single_configured_channel(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A succeeded campaign triggers exactly ONE send() (criterion #6)."""
    store = _FakeObjectStorage()
    _seed_two_ok_samples(store)
    _seed_campaign(sns_topic_arn="arn:aws:sns:us-east-1:123456789012:osimflow")
    monkeypatch.setattr(coord, "_storage_from_campaign", lambda _rec: store)

    fake = _RecordingBackend(name="sns-fake")
    # Inject the fake as the SNS backend the auto-fire loop constructs.
    monkeypatch.setattr(coord, "SNSNotifyBackend", lambda **_kw: fake)

    resp = client.post(f"/api/v1/coordinator/campaigns/{CAMPAIGN_ID}/aggregate")

    assert resp.status_code == 202, resp.text
    assert resp.json()["status"] == "complete"

    # Criterion #6: exactly ONE send() call on the configured channel.
    assert len(fake.calls) == 1
    event, payload = fake.calls[0]
    assert event == "campaign.succeeded"
    # campaign.succeeded payload contract.
    assert payload["campaign_id"] == CAMPAIGN_ID
    assert "download_url" in payload
    assert payload["expires_in_seconds"] == 3600
    # Aggregation produced the key on the record before notify fired.
    assert payload["aggregated_results_key"] == f"{CAMPAIGN_ID}/_aggregated/aggregated_results.csv"


def test_aggregation_auto_notify_failure_does_not_revert_status(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A backend failure cannot flip a succeeded campaign back (criterion #4)."""
    store = _FakeObjectStorage()
    _seed_two_ok_samples(store)
    _seed_campaign(notification_email="ops@example.com")
    monkeypatch.setattr(coord, "_storage_from_campaign", lambda _rec: store)

    fake = _RecordingBackend(name="email-fake")
    fake.raise_on_send = RuntimeError("SMTP exploded")
    monkeypatch.setattr(coord, "EmailNotifyBackend", lambda **_kw: fake)

    resp = client.post(f"/api/v1/coordinator/campaigns/{CAMPAIGN_ID}/aggregate")

    # Aggregation still succeeds (HTTP 202, status=complete).
    assert resp.status_code == 202, resp.text
    assert resp.json()["status"] == "complete"
    # The notify attempt was made exactly once.
    assert len(fake.calls) == 1
    # The campaign record remains "complete" — the failure did not revert.
    assert coord._campaigns[CAMPAIGN_ID]["status"] == "complete"


def test_aggregation_auto_notify_skipped_when_no_channel_configured(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No channels configured → no send() call, but aggregation still succeeds."""
    store = _FakeObjectStorage()
    _seed_two_ok_samples(store)
    # No sns_topic_arn / notification_email / webhook_url on the record.
    _seed_campaign()
    monkeypatch.setattr(coord, "_storage_from_campaign", lambda _rec: store)

    resp = client.post(f"/api/v1/coordinator/campaigns/{CAMPAIGN_ID}/aggregate")

    assert resp.status_code == 202
    assert resp.json()["status"] == "complete"


def test_aggregation_auto_notify_fans_out_to_all_configured_channels(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Multiple configured channels → one send() per channel (fan-out)."""
    store = _FakeObjectStorage()
    _seed_two_ok_samples(store)
    _seed_campaign(
        sns_topic_arn="arn:aws:sns:us-east-1:123456789012:t",
        notification_email="ops@example.com",
        webhook_url="https://hooks.example.com/x",
    )
    monkeypatch.setattr(coord, "_storage_from_campaign", lambda _rec: store)

    sns_fake = _RecordingBackend(name="sns")
    email_fake = _RecordingBackend(name="email")
    webhook_fake = _RecordingBackend(name="webhook")
    monkeypatch.setattr(coord, "SNSNotifyBackend", lambda **_kw: sns_fake)
    monkeypatch.setattr(coord, "EmailNotifyBackend", lambda **_kw: email_fake)
    monkeypatch.setattr(coord, "WebhookNotifyBackend", lambda **_kw: webhook_fake)

    resp = client.post(f"/api/v1/coordinator/campaigns/{CAMPAIGN_ID}/aggregate")

    assert resp.status_code == 202
    # Each configured backend got exactly one call.
    assert len(sns_fake.calls) == 1
    assert len(email_fake.calls) == 1
    assert len(webhook_fake.calls) == 1
    # All carry the same event tag + presigned URL payload shape.
    for backend in (sns_fake, email_fake, webhook_fake):
        event, payload = backend.calls[0]
        assert event == "campaign.succeeded"
        assert payload["campaign_id"] == CAMPAIGN_ID


# ===========================================================================
# Explicit POST /campaigns/{id}/notify endpoint
# ===========================================================================


def test_notify_endpoint_dispatches_via_selected_backend(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST /notify picks the requested backend and returns status=sent."""
    _seed_campaign(
        sns_topic_arn="arn:aws:sns:us-east-1:123456789012:t",
        status="complete",
    )
    # Pretend aggregation already produced the artifacts.
    coord._campaigns[CAMPAIGN_ID]["aggregated_results_key"] = (
        f"{CAMPAIGN_ID}/_aggregated/aggregated_results.csv"
    )

    fake = _RecordingBackend()
    monkeypatch.setattr(coord, "build_notify_backend", lambda **_kw: fake)
    # Presigning hits S3 — stub it out so the test makes no network calls.
    monkeypatch.setattr(coord, "_presign_aggregated_get", lambda *a, **k: "https://signed/x.csv")

    resp = client.post(
        f"/api/v1/coordinator/campaigns/{CAMPAIGN_ID}/notify",
        json={"notification_type": "sns", "expires_in_seconds": 7200},
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "sent"
    assert body["notification_type"] == "sns"

    assert len(fake.calls) == 1
    event, payload = fake.calls[0]
    assert event == "campaign.succeeded"
    assert payload["download_url"] == "https://signed/x.csv"
    assert payload["expires_in_seconds"] == 7200


def test_notify_endpoint_returns_skipped_when_no_channel(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A campaign with no configured backend → status=skipped (not an error)."""
    _seed_campaign(status="complete")
    monkeypatch.setattr(coord, "_presign_aggregated_get", lambda *a, **k: None)

    resp = client.post(
        f"/api/v1/coordinator/campaigns/{CAMPAIGN_ID}/notify",
        json={"notification_type": "sns"},
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "skipped"
    assert isinstance(NullNotifyBackend(), NotifyBackend)  # sanity


def test_notify_endpoint_404_unknown_campaign(client: TestClient) -> None:
    """An unknown campaign_id → 404 (consistent with the other endpoints)."""
    resp = client.post(
        "/api/v1/coordinator/campaigns/does-not-exist/notify",
        json={"notification_type": "sns"},
    )
    assert resp.status_code == 404


def test_notify_endpoint_failed_status_when_backend_leaks_exception(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even if a buggy backend raises, the endpoint returns 200 + status=failed.

    The dispatch wrapper catches the leak so the campaign status is never
    affected (criterion #4 — belt-and-braces).
    """
    _seed_campaign(notification_email="ops@example.com", status="complete")
    monkeypatch.setattr(coord, "_presign_aggregated_get", lambda *a, **k: None)

    # build_notify_backend normally returns the right type; replace it with
    # a backend whose send() raises (violating the contract — the wrapper
    # must still cope).
    leaking = _RecordingBackend()
    leaking.raise_on_send = RuntimeError("leaked")
    monkeypatch.setattr(coord, "build_notify_backend", lambda **_kw: leaking)

    resp = client.post(
        f"/api/v1/coordinator/campaigns/{CAMPAIGN_ID}/notify",
        json={"notification_type": "email"},
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "failed"
    assert "leaked" in body["message"]
    # Campaign status is untouched.
    assert coord._campaigns[CAMPAIGN_ID]["status"] == "complete"


# ===========================================================================
# Handoff persists webhook_url (issue #628 plumbing)
# ===========================================================================


def test_handoff_persists_webhook_url_on_record(client: TestClient) -> None:
    """A handoff carrying webhook_url lands on the campaign record."""
    resp = client.post(
        "/api/v1/coordinator/handoff",
        json={
            "name": "with-webhook",
            "n_samples": 1,
            "executor": "aws_batch",
            "openstudio_version": "3.11.0",
            "webhook_url": "https://hooks.example.com/osimflow",
        },
    )
    assert resp.status_code == 202, resp.text
    campaign_id = resp.json()["campaign_id"]
    assert coord._campaigns[campaign_id]["webhook_url"] == "https://hooks.example.com/osimflow"
