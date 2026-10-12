"""Unit tests for Coordinator array-completion detection (issue #626, Epic #624).

Covers the four acceptance-criteria scenarios:

1. EventBridge webhook path with full success.
2. EventBridge webhook path with partial failures (still -> aggregating).
3. ``poll_array_job_to_completion`` exponential-backoff polling fallback.
4. Idempotent duplicate webhook (no-op after a poll-driven transition).

Plus signature/source/id-mismatch guards required by the security rule
(AGENTS.md §10): an unset server secret fails closed; a bad secret, a bad
``source``, or a mismatched ``detail.jobId`` are all rejected.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

pytest.importorskip("fastapi", reason="osimflow[api] extra required")
pytest.importorskip("slowapi", reason="osimflow[api] extra required")
pytest.importorskip("boto3", reason="osimflow[aws] extra required")
from fastapi.testclient import TestClient

from osimflow.api import coordinator as coord
from osimflow.api import create_app

WEBHOOK_SECRET = "test-shared-secret-abc123"
ARRAY_JOB_ID = "array-job-12345"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_campaign_store() -> None:
    """Clear the in-memory campaign store before every test."""
    coord._campaigns.clear()
    yield
    coord._campaigns.clear()


@pytest.fixture(autouse=True)
def _webhook_secret_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Configure the server-side EventBridge shared secret for every test."""
    monkeypatch.setenv(coord._EVENTBRIDGE_SECRET_ENV, WEBHOOK_SECRET)


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    """FastAPI TestClient with no user-auth (webhook uses its own signature)."""
    app = create_app(outdir=tmp_path, read_only=False)
    return TestClient(app)


def _seed_campaign(
    *,
    campaign_id: str = "camp-1",
    status: str = "running",
    array_job_id: str = ARRAY_JOB_ID,
) -> dict[str, Any]:
    """Insert a campaign record into the in-memory store and return it."""
    rec: dict[str, Any] = {
        "campaign_id": campaign_id,
        "name": "test",
        "status": status,
        "created_at": 0.0,
        "updated_at": 0.0,
        "n_samples": 3,
        "executor": "aws_batch",
        "openstudio_version": "3.11.0",
        "array_job_id": array_job_id,
        "result_storage_bucket": None,
    }
    coord._campaigns[campaign_id] = rec
    return rec


def _fake_batch_client(jobs_by_call: list[dict[str, Any]]) -> tuple[MagicMock, list[list[str]]]:
    """Build a fake boto3 Batch client that returns ``jobs_by_call`` in order.

    Returns ``(client, describe_calls)`` where ``describe_calls`` records the
    ``jobs`` argument of every ``describe_jobs`` invocation so tests can assert
    the parent array_job_id was queried.
    """
    fake = MagicMock()
    describe_calls: list[list[str]] = []
    queue = list(jobs_by_call)

    def fake_describe_jobs(**kwargs: Any) -> dict[str, Any]:
        describe_calls.append(list(kwargs.get("jobs", [])))  # type: ignore[arg-type]
        if not queue:
            # If the test polls more than expected, keep returning the last job.
            job = jobs_by_call[-1]
        else:
            job = queue.pop(0)
        return {"jobs": [job]}

    fake.describe_jobs.side_effect = fake_describe_jobs
    return fake, describe_calls


def _event_body(
    *,
    job_id: str = ARRAY_JOB_ID,
    source: str = coord._EXPECTED_EVENT_SOURCE,
    detail_type: str = coord._EXPECTED_EVENT_DETAIL_TYPE,
) -> dict[str, Any]:
    """Build a minimal EventBridge Batch Job State Change envelope."""
    return {
        "version": "0",
        "id": "evt-1",
        "detail-type": detail_type,
        "source": source,
        "account": "123456789012",
        "region": "us-east-1",
        "time": "2026-06-19T12:00:00Z",
        "resources": [f"arn:aws:batch:us-east-1:123456789012:job/{job_id}"],
        "detail": {"jobId": job_id, "status": "SUCCEEDED"},
    }


def _full_success_job() -> dict[str, Any]:
    return {
        "jobId": ARRAY_JOB_ID,
        "status": "SUCCEEDED",
        "arrayProperties": {"size": 3},
        "statusSummary": {"SUCCEEDED": 3, "FAILED": 0},
    }


def _partial_failure_job() -> dict[str, Any]:
    return {
        "jobId": ARRAY_JOB_ID,
        "status": "FAILED",
        "arrayProperties": {"size": 3},
        "statusSummary": {"SUCCEEDED": 2, "FAILED": 1},
    }


def _incomplete_job() -> dict[str, Any]:
    return {
        "jobId": ARRAY_JOB_ID,
        "status": "RUNNING",
        "arrayProperties": {"size": 3},
        "statusSummary": {"SUCCEEDED": 1, "RUNNING": 2},
    }


# ---------------------------------------------------------------------------
# (1) Webhook path — full success
# ---------------------------------------------------------------------------


def test_webhook_full_success_transitions_to_aggregating(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """All children SUCCEEDED -> campaign flips running -> aggregating."""
    rec = _seed_campaign(status="running")
    fake, describe_calls = _fake_batch_client([_full_success_job()])
    monkeypatch.setattr(coord, "boto3", MagicMock(client=lambda *a, **k: fake))

    resp = client.post(
        "/api/v1/coordinator/campaigns/camp-1/array-complete",
        json=_event_body(),
        headers={coord._EVENTBRIDGE_SECRET_HEADER: WEBHOOK_SECRET},
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "aggregating"
    assert body["transitioned"] is True
    assert body["succeeded"] == 3
    assert body["failed"] == 0
    assert body["total"] == 3

    # Campaign record advanced and recorded the split for the aggregator.
    assert rec["status"] == "aggregating"
    assert rec["array_completion"] == {
        "succeeded": 3,
        "failed": 0,
        "total": 3,
        "completed_at": rec["array_completion"]["completed_at"],
        "source": "eventbridge",
    }
    # The handler re-queried the stored parent array_job_id (source of truth).
    assert describe_calls == [[ARRAY_JOB_ID]]


# ---------------------------------------------------------------------------
# (2) Webhook path — partial failure (still -> aggregating, split recorded)
# ---------------------------------------------------------------------------


def test_webhook_partial_failure_still_transitions(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Some children FAILED but all terminal -> aggregating, split recorded."""
    rec = _seed_campaign(status="running")
    fake, _ = _fake_batch_client([_partial_failure_job()])
    monkeypatch.setattr(coord, "boto3", MagicMock(client=lambda *a, **k: fake))

    resp = client.post(
        "/api/v1/coordinator/campaigns/camp-1/array-complete",
        json=_event_body(),
        headers={coord._EVENTBRIDGE_SECRET_HEADER: WEBHOOK_SECRET},
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    # Partial failure is "complete (not succeeded)" -> aggregating, not failed.
    assert body["status"] == "aggregating"
    assert body["transitioned"] is True
    assert body["succeeded"] == 2
    assert body["failed"] == 1
    assert body["total"] == 3
    assert rec["status"] == "aggregating"
    assert rec["array_completion"]["failed"] == 1
    assert rec["array_completion"]["succeeded"] == 2


# ---------------------------------------------------------------------------
# (3) Polling fallback — exponential backoff until complete
# ---------------------------------------------------------------------------


def test_polling_fallback_transitions_on_completion() -> None:
    """poll_array_job_to_completion backs off 5s->60s and transitions when done."""
    rec = _seed_campaign(status="running")
    fake, describe_calls = _fake_batch_client([_incomplete_job(), _full_success_job()])

    # Fake async sleep records the backoff schedule (should be 5 then 10).
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    response = asyncio.run(
        coord.poll_array_job_to_completion(
            "camp-1",
            initial_delay=5.0,
            max_delay=60.0,
            max_attempts=10,
            batch_client=fake,
            sleep=fake_sleep,
        )
    )

    assert response.transitioned is True
    assert response.status == "aggregating"
    assert response.succeeded == 3
    assert response.total == 3
    # Two polls: first incomplete (sleep 5s), second complete (no sleep after).
    assert len(describe_calls) == 2
    assert slept == [5.0]  # exponential backoff: first wait is the initial delay
    assert rec["status"] == "aggregating"
    assert rec["array_completion"]["source"] == "poll"


def test_polling_backoff_caps_at_max_delay() -> None:
    """Backoff doubles each incomplete poll up to the configured cap."""
    _seed_campaign(status="running")
    # Three incomplete polls, then complete.
    fake, _ = _fake_batch_client(
        [_incomplete_job(), _incomplete_job(), _incomplete_job(), _full_success_job()]
    )
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    response = asyncio.run(
        coord.poll_array_job_to_completion(
            "camp-1",
            initial_delay=5.0,
            max_delay=8.0,  # cap below the natural 5 -> 10 doubling
            max_attempts=20,
            batch_client=fake,
            sleep=fake_sleep,
        )
    )
    assert response.transitioned is True
    # Schedule: 5, 10 -> capped 8, 16 -> capped 8. Only sleeps *between* polls,
    # so three incomplete polls produce three waits then the 4th poll completes.
    assert slept == [5.0, 8.0, 8.0]


# ---------------------------------------------------------------------------
# (4) Idempotent duplicate webhook (after a poll-driven transition)
# ---------------------------------------------------------------------------


def test_late_webhook_after_poll_is_idempotent_noop(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A webhook arriving after the poller advanced the campaign is a no-op."""
    # Poller fires first: running -> aggregating.
    rec = _seed_campaign(status="running")
    poll_fake, _ = _fake_batch_client([_full_success_job()])
    poll_response = asyncio.run(
        coord.poll_array_job_to_completion("camp-1", batch_client=poll_fake, sleep=_no_sleep)
    )
    assert poll_response.transitioned is True
    assert rec["status"] == "aggregating"
    completed_at = rec["array_completion"]["completed_at"]

    # Late webhook now arrives. It must NOT re-transition or overwrite the split.
    web_fake, _ = _fake_batch_client([_full_success_job()])
    monkeypatch.setattr(coord, "boto3", MagicMock(client=lambda *a, **k: web_fake))
    resp = client.post(
        "/api/v1/coordinator/campaigns/camp-1/array-complete",
        json=_event_body(),
        headers={coord._EVENTBRIDGE_SECRET_HEADER: WEBHOOK_SECRET},
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["transitioned"] is False
    assert body["status"] == "already_aggregating"
    assert body["succeeded"] == 3
    # The poll-driven split is preserved (not overwritten by the webhook).
    assert rec["array_completion"]["source"] == "poll"
    assert rec["array_completion"]["completed_at"] == completed_at


def test_duplicate_webhook_back_to_back_is_idempotent(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two identical webhooks back-to-back: first transitions, second no-ops."""
    rec = _seed_campaign(status="running")
    fake = MagicMock()

    def two_then_complete(**_kwargs: Any) -> dict[str, Any]:
        return {"jobs": [_full_success_job()]}

    fake.describe_jobs.side_effect = two_then_complete
    monkeypatch.setattr(coord, "boto3", MagicMock(client=lambda *a, **k: fake))

    first = client.post(
        "/api/v1/coordinator/campaigns/camp-1/array-complete",
        json=_event_body(),
        headers={coord._EVENTBRIDGE_SECRET_HEADER: WEBHOOK_SECRET},
    )
    second = client.post(
        "/api/v1/coordinator/campaigns/camp-1/array-complete",
        json=_event_body(),
        headers={coord._EVENTBRIDGE_SECRET_HEADER: WEBHOOK_SECRET},
    )
    assert first.json()["transitioned"] is True
    assert first.json()["status"] == "aggregating"
    assert second.status_code == 200
    assert second.json()["transitioned"] is False
    assert second.json()["status"] == "already_aggregating"
    assert rec["status"] == "aggregating"


async def _no_sleep(_seconds: float) -> None:  # noqa: RUF029 - async signature contract
    """Instant async sleep used to drive the poller without real delays."""
    return None


# ---------------------------------------------------------------------------
# Security / validation guards (AGENTS.md §10)
# ---------------------------------------------------------------------------


def test_webhook_rejects_missing_secret_header(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_campaign(status="running")
    fake, _ = _fake_batch_client([_full_success_job()])
    monkeypatch.setattr(coord, "boto3", MagicMock(client=lambda *a, **k: fake))
    resp = client.post("/api/v1/coordinator/campaigns/camp-1/array-complete", json=_event_body())
    assert resp.status_code == 401


def test_webhook_rejects_wrong_secret(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_campaign(status="running")
    fake, _ = _fake_batch_client([_full_success_job()])
    monkeypatch.setattr(coord, "boto3", MagicMock(client=lambda *a, **k: fake))
    resp = client.post(
        "/api/v1/coordinator/campaigns/camp-1/array-complete",
        json=_event_body(),
        headers={coord._EVENTBRIDGE_SECRET_HEADER: "wrong-secret"},
    )
    assert resp.status_code == 401


def test_webhook_unconfigured_secret_fails_closed(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the server secret env var is unset, the endpoint rejects (401)."""
    monkeypatch.delenv(coord._EVENTBRIDGE_SECRET_ENV, raising=False)
    _seed_campaign(status="running")
    resp = client.post(
        "/api/v1/coordinator/campaigns/camp-1/array-complete",
        json=_event_body(),
        headers={coord._EVENTBRIDGE_SECRET_HEADER: WEBHOOK_SECRET},
    )
    assert resp.status_code == 401


def test_webhook_rejects_wrong_source(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_campaign(status="running")
    fake, _ = _fake_batch_client([_full_success_job()])
    monkeypatch.setattr(coord, "boto3", MagicMock(client=lambda *a, **k: fake))
    resp = client.post(
        "/api/v1/coordinator/campaigns/camp-1/array-complete",
        json=_event_body(source="aws.ec2"),
        headers={coord._EVENTBRIDGE_SECRET_HEADER: WEBHOOK_SECRET},
    )
    assert resp.status_code == 400


def test_webhook_rejects_mismatched_job_id(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_campaign(status="running", array_job_id=ARRAY_JOB_ID)
    fake, _ = _fake_batch_client([_full_success_job()])
    monkeypatch.setattr(coord, "boto3", MagicMock(client=lambda *a, **k: fake))
    resp = client.post(
        "/api/v1/coordinator/campaigns/camp-1/array-complete",
        json=_event_body(job_id="some-unrelated-job-id"),
        headers={coord._EVENTBRIDGE_SECRET_HEADER: WEBHOOK_SECRET},
    )
    assert resp.status_code == 409


def test_webhook_accepts_child_job_id(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """EventBridge may report a child jobId (<parent>:<index>) — accept it."""
    rec = _seed_campaign(status="running", array_job_id=ARRAY_JOB_ID)
    fake, _ = _fake_batch_client([_full_success_job()])
    monkeypatch.setattr(coord, "boto3", MagicMock(client=lambda *a, **k: fake))
    resp = client.post(
        "/api/v1/coordinator/campaigns/camp-1/array-complete",
        json=_event_body(job_id=f"{ARRAY_JOB_ID}:2"),
        headers={coord._EVENTBRIDGE_SECRET_HEADER: WEBHOOK_SECRET},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["transitioned"] is True
    assert rec["status"] == "aggregating"


def test_webhook_404_unknown_campaign(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    fake, _ = _fake_batch_client([_full_success_job()])
    monkeypatch.setattr(coord, "boto3", MagicMock(client=lambda *a, **k: fake))
    resp = client.post(
        "/api/v1/coordinator/campaigns/does-not-exist/array-complete",
        json=_event_body(),
        headers={coord._EVENTBRIDGE_SECRET_HEADER: WEBHOOK_SECRET},
    )
    assert resp.status_code == 404


def test_webhook_pending_children_does_not_transition(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If children are still running, status stays running (200, pending)."""
    rec = _seed_campaign(status="running")
    fake, _ = _fake_batch_client([_incomplete_job()])
    monkeypatch.setattr(coord, "boto3", MagicMock(client=lambda *a, **k: fake))
    resp = client.post(
        "/api/v1/coordinator/campaigns/camp-1/array-complete",
        json=_event_body(),
        headers={coord._EVENTBRIDGE_SECRET_HEADER: WEBHOOK_SECRET},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "pending"
    assert body["transitioned"] is False
    assert rec["status"] == "running"  # unchanged
    assert "array_completion" not in rec


# ---------------------------------------------------------------------------
# Shared-helper unit tests (no HTTP)
# ---------------------------------------------------------------------------


def test_parse_array_completion_requires_size_match() -> None:
    """complete is True only when SUCCEEDED + FAILED >= arrayProperties.size."""
    complete = coord._parse_array_completion(
        {"arrayProperties": {"size": 4}, "statusSummary": {"SUCCEEDED": 3, "FAILED": 1}}
    )
    assert complete.complete is True
    assert complete.succeeded == 3
    assert complete.failed == 1
    assert complete.total == 4
    assert complete.pending == 0

    incomplete = coord._parse_array_completion(
        {
            "arrayProperties": {"size": 4},
            "statusSummary": {"SUCCEEDED": 3, "FAILED": 0, "RUNNING": 1},
        }
    )
    assert incomplete.complete is False
    assert incomplete.pending == 1


def test_parse_array_completion_falls_back_to_job_summary_shape() -> None:
    """Older/alternate shape nests statusSummary under jobSummary."""
    job = {
        "arrayProperties": {"size": 2},
        "jobSummary": {"statusSummary": {"SUCCEEDED": 2}},
    }
    completion = coord._parse_array_completion(job)
    assert completion.complete is True
    assert completion.succeeded == 2


if __name__ == "__main__":
    import sys

    sys.exit(asyncio.run(pytest.main([__file__, "-v"])))  # type: ignore[func-returns-value]
