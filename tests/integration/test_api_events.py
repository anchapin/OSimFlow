"""Tests for SSE events and campaign stop endpoints (issue #143)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

pytest.importorskip("fastapi", reason="osimflow[api] extra required")
from fastapi.testclient import TestClient

from osimflow.api import create_app
from osimflow.api.events import _event_generator, diff_events


async def _always_false() -> bool:
    return False


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_run_json(
    samples: list[dict] | None = None,
    steps: list[dict] | None = None,
    finished_at: float | None = None,
) -> dict:
    data: dict = {
        "schema_version": 1,
        "campaign_id": "test-campaign-001",
        "started_at": 1000.0,
        "finished_at": finished_at,
        "config_summary": {"executor": "local", "n_samples": 3},
        "steps": steps or [],
        "per_sample": samples or [],
    }
    return data


@pytest.fixture
def outdir(tmp_path: Path) -> Path:
    """Output directory with an initial run.json."""
    (tmp_path / "run.json").write_text(json.dumps(_make_run_json()))
    return tmp_path


@pytest.fixture
def client_rw(outdir: Path) -> TestClient:
    """TestClient with read_only=False (live events + stop enabled)."""
    return TestClient(create_app(outdir=outdir, read_only=False))


@pytest.fixture
def client_ro(outdir: Path) -> TestClient:
    """TestClient with read_only=True (default)."""
    return TestClient(create_app(outdir=outdir, read_only=True))


# ---------------------------------------------------------------------------
# diff_events — pure function unit tests
# ---------------------------------------------------------------------------


class TestDiffEvents:
    """Tests for the diff_events helper function."""

    def test_no_changes(self) -> None:
        """No events when snapshots are identical."""
        snapshot = _make_run_json()
        events = diff_events(snapshot, snapshot)
        assert events == []

    def test_step_completed(self) -> None:
        """Emits step.completed when a new step appears."""
        old = _make_run_json(steps=[])
        new = _make_run_json(
            steps=[
                {"step": "GENERATE_LHS_SAMPLES", "cache": "MISS", "elapsed_s": 0.5, "exit_code": 0}
            ]
        )
        events = diff_events(old, new)
        assert len(events) == 1
        assert events[0]["event"] == "step.completed"
        assert events[0]["data"]["step"] == "GENERATE_LHS_SAMPLES"

    def test_sample_completed(self) -> None:
        """Emits sample.completed when a new sample with terminal status appears."""
        old = _make_run_json(samples=[])
        new = _make_run_json(
            samples=[{"sample_id": "sample_000", "status": "ok", "elapsed_s": 10.0}]
        )
        events = diff_events(old, new)
        assert len(events) == 1
        assert events[0]["event"] == "sample.completed"
        assert events[0]["data"]["sample_id"] == "sample_000"

    def test_sample_started(self) -> None:
        """Emits sample.started when a new sample with non-terminal status appears."""
        old = _make_run_json(samples=[])
        new = _make_run_json(
            samples=[{"sample_id": "sample_000", "status": "running", "elapsed_s": 0.0}]
        )
        events = diff_events(old, new)
        assert len(events) == 1
        assert events[0]["event"] == "sample.started"

    def test_sample_status_change(self) -> None:
        """Emits sample.completed when status changes to terminal."""
        old = _make_run_json(
            samples=[{"sample_id": "sample_000", "status": "running", "elapsed_s": 0.0}]
        )
        new = _make_run_json(
            samples=[{"sample_id": "sample_000", "status": "ok", "elapsed_s": 10.0}]
        )
        events = diff_events(old, new)
        assert len(events) == 1
        assert events[0]["event"] == "sample.completed"

    def test_campaign_completed(self) -> None:
        """Emits campaign.completed when finished_at becomes non-null."""
        old = _make_run_json(finished_at=None)
        new = _make_run_json(finished_at=2000.0)
        events = diff_events(old, new)
        assert len(events) == 1
        assert events[0]["event"] == "campaign.completed"
        assert events[0]["data"]["finished_at"] == 2000.0

    def test_multiple_events(self) -> None:
        """Emits multiple events when several things change at once."""
        old = _make_run_json(steps=[], samples=[], finished_at=None)
        new = _make_run_json(
            steps=[
                {"step": "GENERATE_LHS_SAMPLES", "cache": "MISS", "elapsed_s": 0.5, "exit_code": 0}
            ],
            samples=[{"sample_id": "sample_000", "status": "ok", "elapsed_s": 10.0}],
            finished_at=2000.0,
        )
        events = diff_events(old, new)
        event_types = [e["event"] for e in events]
        assert "step.completed" in event_types
        assert "sample.completed" in event_types
        assert "campaign.completed" in event_types

    def test_failed_sample(self) -> None:
        """Emits sample.completed for failed status."""
        old = _make_run_json(samples=[])
        new = _make_run_json(
            samples=[
                {
                    "sample_id": "sample_001",
                    "status": "failed",
                    "elapsed_s": 5.0,
                    "error_summary": "Severe Error",
                }
            ]
        )
        events = diff_events(old, new)
        assert len(events) == 1
        assert events[0]["event"] == "sample.completed"
        assert events[0]["data"]["status"] == "failed"

    def test_cached_sample(self) -> None:
        """Emits sample.completed for cached status."""
        old = _make_run_json(samples=[])
        new = _make_run_json(
            samples=[{"sample_id": "sample_000", "status": "cached", "elapsed_s": 0.01}]
        )
        events = diff_events(old, new)
        assert len(events) == 1
        assert events[0]["event"] == "sample.completed"

    def test_no_event_for_same_status(self) -> None:
        """No event when status hasn't changed."""
        old = _make_run_json(
            samples=[{"sample_id": "sample_000", "status": "ok", "elapsed_s": 10.0}]
        )
        new = _make_run_json(
            samples=[{"sample_id": "sample_000", "status": "ok", "elapsed_s": 10.0}]
        )
        events = diff_events(old, new)
        assert events == []


# ---------------------------------------------------------------------------
# SSE endpoint — HTTP-level tests
# ---------------------------------------------------------------------------


class TestSSEEndpoint:
    """Tests for GET /api/v1/events HTTP behaviour."""

    def test_sse_read_only_connects(self, client_ro: TestClient, outdir: Path) -> None:
        """SSE endpoint is available in read-only mode (issue #275)."""
        from unittest.mock import patch

        async def mock_generator(request, poll_interval=1.0, max_iterations=1):
            yield {"event": "campaign.completed", "data": "{}"}
            return

        with patch("osimflow.api.events._event_generator", mock_generator):
            resp = client_ro.get("/api/v1/events", timeout=10)
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Campaign stop
# ---------------------------------------------------------------------------


class TestCampaignStop:
    """Tests for POST /api/v1/campaign/stop."""

    def test_stop_creates_flag_file(self, client_rw: TestClient, outdir: Path) -> None:
        """Stop endpoint creates .stop file and returns correct response."""
        resp = client_rw.post("/api/v1/campaign/stop")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "stopping"

        stop_file = outdir / ".stop"
        assert stop_file.exists()
        content = json.loads(stop_file.read_text())
        assert "requested_at" in content

    def test_stop_returns_403_in_read_only(self, client_ro: TestClient) -> None:
        """Stop endpoint returns 403 in read-only mode."""
        resp = client_ro.post("/api/v1/campaign/stop")
        assert resp.status_code == 403
        assert "read-only" in resp.json()["detail"].lower()

    def test_stop_no_outdir(self) -> None:
        """Stop endpoint returns 503 when no outdir configured."""
        client = TestClient(create_app(outdir=None, read_only=False))
        resp = client.post("/api/v1/campaign/stop")
        assert resp.status_code == 503

    def test_stop_idempotent(self, client_rw: TestClient, outdir: Path) -> None:
        """Multiple stop requests are idempotent."""
        resp1 = client_rw.post("/api/v1/campaign/stop")
        resp2 = client_rw.post("/api/v1/campaign/stop")
        assert resp1.status_code == 200
        assert resp2.status_code == 200
        assert resp1.json()["status"] == "stopping"
        assert resp2.json()["status"] == "stopping"


# ---------------------------------------------------------------------------
# SSE streaming — integration test with finite generator
# ---------------------------------------------------------------------------


class TestSSEStreaming:
    """Integration tests for SSE streaming using finite iteration cap."""

    def test_sse_streams_step_event(self, outdir: Path) -> None:
        """SSE endpoint emits step.completed event when run.json changes."""
        # Pre-update run.json before starting the generator
        updated = _make_run_json(
            steps=[
                {"step": "GENERATE_LHS_SAMPLES", "cache": "MISS", "elapsed_s": 0.5, "exit_code": 0}
            ]
        )
        (outdir / "run.json").write_text(json.dumps(updated))

        # Create a mock request with app.state
        mock_request = MagicMock()
        mock_request.app.state.outdir = outdir
        mock_request.is_disconnected = _always_false

        # Collect events from the generator with a small iteration cap
        collected: list[dict[str, Any]] = []
        import asyncio

        async def _collect() -> None:
            async for evt in _event_generator(
                mock_request,
                poll_interval=0.01,
                heartbeat_interval=9999.0,  # disable heartbeat for test
                max_iterations=3,
            ):
                collected.append(evt)

        asyncio.run(_collect())

        # Should have emitted the step.completed event
        event_types = [e.get("event") for e in collected]
        assert "step.completed" in event_types

    def test_sse_streams_campaign_completed(self, outdir: Path) -> None:
        """SSE endpoint emits campaign.completed when finished_at is set."""
        updated = _make_run_json(finished_at=2000.0)
        (outdir / "run.json").write_text(json.dumps(updated))

        mock_request = MagicMock()
        mock_request.app.state.outdir = outdir
        mock_request.is_disconnected = _always_false

        collected: list[dict[str, Any]] = []

        async def _collect() -> None:
            async for evt in _event_generator(
                mock_request,
                poll_interval=0.01,
                heartbeat_interval=9999.0,
                max_iterations=3,
            ):
                collected.append(evt)

        asyncio.run(_collect())

        event_types = [e.get("event") for e in collected]
        assert "campaign.completed" in event_types

    def test_sse_streams_sample_events(self, outdir: Path) -> None:
        """SSE endpoint emits sample.completed when samples appear."""
        updated = _make_run_json(
            samples=[
                {"sample_id": "sample_000", "status": "ok", "elapsed_s": 10.0},
                {"sample_id": "sample_001", "status": "failed", "elapsed_s": 5.0},
            ]
        )
        (outdir / "run.json").write_text(json.dumps(updated))

        mock_request = MagicMock()
        mock_request.app.state.outdir = outdir
        mock_request.is_disconnected = _always_false

        collected: list[dict[str, Any]] = []

        async def _collect() -> None:
            async for evt in _event_generator(
                mock_request,
                poll_interval=0.01,
                heartbeat_interval=9999.0,
                max_iterations=3,
            ):
                collected.append(evt)

        asyncio.run(_collect())

        event_types = [e.get("event") for e in collected]
        assert "sample.completed" in event_types
        # Should have two sample.completed events
        sample_completed = [e for e in collected if e.get("event") == "sample.completed"]
        assert len(sample_completed) == 2

    def test_sse_no_outdir_yields_error(self) -> None:
        """SSE generator yields error event when outdir is None."""
        mock_request = MagicMock()
        mock_request.app.state.outdir = None

        collected: list[dict[str, Any]] = []

        async def _collect() -> None:
            async for evt in _event_generator(mock_request, max_iterations=1):
                collected.append(evt)

        asyncio.run(_collect())

        assert len(collected) == 1
        assert collected[0]["event"] == "error"

    def test_sse_heartbeat(self, outdir: Path) -> None:
        """SSE generator sends heartbeat ping after interval."""
        # run.json has already been loaded, no changes — heartbeat should fire
        mock_request = MagicMock()
        mock_request.app.state.outdir = outdir
        mock_request.is_disconnected = _always_false

        collected: list[dict[str, Any]] = []

        async def _collect() -> None:
            async for evt in _event_generator(
                mock_request,
                poll_interval=0.01,
                heartbeat_interval=0.0,  # trigger heartbeat immediately
                max_iterations=3,
            ):
                collected.append(evt)

        asyncio.run(_collect())

        event_types = [e.get("event") for e in collected]
        assert "ping" in event_types
