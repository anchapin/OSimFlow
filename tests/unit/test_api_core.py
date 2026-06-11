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
