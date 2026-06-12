"""Tests for osimflow/api/ core endpoints (issue #138)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="osimflow[api] extra required")
from fastapi.testclient import TestClient

from osimflow.api import create_app


@pytest.fixture
def tmp_outdir(tmp_path: Path) -> Path:
    """Create a temporary output directory with a sample run.json."""
    run_json = {
        "schema_version": 1,
        "campaign_id": "test-campaign-001",
        "started_at": 1000.0,
        "finished_at": 2000.0,
        "config_summary": {"executor": "local", "n_samples": 5},
        "steps": [
            {"step": "GENERATE_LHS_SAMPLES", "cache": "MISS", "elapsed_s": 0.5, "exit_code": 0},
            {"step": "RUN_OPENSTUDIO_SIM", "cache": "MISS", "elapsed_s": 100.0, "exit_code": 0},
        ],
        "per_sample": [],
    }
    (tmp_path / "run.json").write_text(json.dumps(run_json))
    return tmp_path


@pytest.fixture
def client(tmp_outdir: Path) -> TestClient:
    app = create_app(outdir=tmp_outdir)
    return TestClient(app)


def test_health(client: TestClient) -> None:
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "alive"


def test_ready(client: TestClient) -> None:
    resp = client.get("/ready")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ready"


def test_campaign(client: TestClient) -> None:
    resp = client.get("/api/v1/campaign")
    assert resp.status_code == 200
    data = resp.json()
    assert data["campaign_id"] == "test-campaign-001"
    assert data["config_summary"]["executor"] == "local"


def test_steps(client: TestClient) -> None:
    resp = client.get("/api/v1/steps")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_steps"] == 2
    assert data["steps"][0]["step"] == "GENERATE_LHS_SAMPLES"


def test_no_outdir() -> None:
    app = create_app(outdir=None)
    client = TestClient(app)
    resp = client.get("/api/v1/campaign")
    assert resp.status_code == 503


def test_no_run_json(tmp_path: Path) -> None:
    app = create_app(outdir=tmp_path)
    client = TestClient(app)
    resp = client.get("/api/v1/campaign")
    assert resp.status_code == 404


class TestCreateApp:
    """Tests for the create_app factory."""

    def test_returns_fastapi_app(self) -> None:
        from fastapi import FastAPI

        app = create_app()
        assert isinstance(app, FastAPI)

    def test_app_title(self) -> None:
        app = create_app()
        assert app.title == "OSimFlow API"

    def test_read_only_default(self) -> None:
        app = create_app()
        assert app.state.read_only is True

    def test_read_only_false(self) -> None:
        app = create_app(read_only=False)
        assert app.state.read_only is False


class TestReadyEndpoint:
    """Tests for /ready readiness probe edge cases."""

    def test_ready_no_outdir(self) -> None:
        app = create_app(outdir=None)
        client = TestClient(app)
        resp = client.get("/ready")
        assert resp.status_code == 200
        assert resp.json()["status"] == "not_ready"

    def test_ready_no_run_json(self, tmp_path: Path) -> None:
        app = create_app(outdir=tmp_path)
        client = TestClient(app)
        resp = client.get("/ready")
        assert resp.status_code == 200
        assert resp.json()["status"] == "not_ready"

    def test_ready_with_run_json(self, tmp_outdir: Path) -> None:
        app = create_app(outdir=tmp_outdir)
        client = TestClient(app)
        resp = client.get("/ready")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ready"
        assert data["campaign_id"] == "test-campaign-001"


class TestCampaignEndpoint:
    """Tests for /api/v1/campaign endpoint."""

    def test_campaign_returns_baseline_comparison(self, tmp_outdir: Path) -> None:
        run_data = json.loads((tmp_outdir / "run.json").read_text())
        run_data["baseline_comparison"] = {"improvement_pct": 15.0}
        (tmp_outdir / "run.json").write_text(json.dumps(run_data))

        app = create_app(outdir=tmp_outdir)
        client = TestClient(app)
        resp = client.get("/api/v1/campaign")
        assert resp.status_code == 200
        assert resp.json()["baseline_comparison"] == {"improvement_pct": 15.0}

    def test_campaign_missing_fields(self, tmp_path: Path) -> None:
        (tmp_path / "run.json").write_text(json.dumps({"campaign_id": "minimal"}))
        app = create_app(outdir=tmp_path)
        client = TestClient(app)
        resp = client.get("/api/v1/campaign")
        data = resp.json()
        assert data["campaign_id"] == "minimal"
        assert data["config_summary"] == {}
        assert data["started_at"] is None
        assert data["finished_at"] is None


class TestStepsEndpoint:
    """Tests for /api/v1/steps endpoint."""

    def test_steps_empty(self, tmp_path: Path) -> None:
        (tmp_path / "run.json").write_text(json.dumps({"steps": []}))
        app = create_app(outdir=tmp_path)
        client = TestClient(app)
        resp = client.get("/api/v1/steps")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_steps"] == 0
        assert data["steps"] == []

    def test_steps_missing_key(self, tmp_path: Path) -> None:
        (tmp_path / "run.json").write_text(json.dumps({}))
        app = create_app(outdir=tmp_path)
        client = TestClient(app)
        resp = client.get("/api/v1/steps")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_steps"] == 0


class TestUnknownRoutes:
    """Tests for unknown route handling."""

    def test_unknown_route_returns_404(self, tmp_outdir: Path) -> None:
        app = create_app(outdir=tmp_outdir)
        client = TestClient(app)
        resp = client.get("/api/v1/nonexistent")
        assert resp.status_code == 404

    def test_unknown_root_route(self, tmp_outdir: Path) -> None:
        app = create_app(outdir=tmp_outdir)
        client = TestClient(app)
        resp = client.get("/unknown")
        assert resp.status_code == 404
