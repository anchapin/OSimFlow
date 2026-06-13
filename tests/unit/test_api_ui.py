"""Tests for osimflow/api/ui.py (issue #337)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="osimflow[api] extra required")
from fastapi.testclient import TestClient

from osimflow.api import create_app


@pytest.fixture
def tmp_outdir(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture
def client(tmp_outdir: Path) -> TestClient:
    app = create_app(outdir=tmp_outdir, ui_enabled=True)
    return TestClient(app)


# ---------------------------------------------------------------------------
# GET /ui/  — campaign setup page
# ---------------------------------------------------------------------------


def test_ui_index_returns_html(client: TestClient) -> None:
    resp = client.get("/ui/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers.get("content-type", "")


def test_ui_index_contains_osimflow(client: TestClient) -> None:
    resp = client.get("/ui/")
    assert resp.status_code == 200
    assert "OSimFlow" in resp.text


# ---------------------------------------------------------------------------
# GET /ui/api/campaigns  — list all campaigns
# ---------------------------------------------------------------------------


def test_list_campaigns_empty(client: TestClient) -> None:
    resp = client.get("/ui/api/campaigns")
    assert resp.status_code == 200
    assert resp.json() == []


# ---------------------------------------------------------------------------
# POST /ui/api/setup  — create and start a campaign
# ---------------------------------------------------------------------------


def test_setup_requires_variables_yaml(client: TestClient) -> None:
    resp = client.post(
        "/ui/api/setup",
        json={
            "n_samples": 5,
            "executor": "local",
            "openstudio_version": "3.11.0",
            "algorithm": "lhs",
            "template_sim_package": str(client.app.state.outdir / "template"),
            "outdir": str(client.app.state.outdir / "results"),
            "input_variables_yaml": "",
        },
    )
    assert resp.status_code == 400
    assert "input_variables_yaml" in resp.json()["detail"]


def test_setup_requires_template_sim_package(client: TestClient) -> None:
    resp = client.post(
        "/ui/api/setup",
        json={
            "n_samples": 5,
            "executor": "local",
            "openstudio_version": "3.11.0",
            "algorithm": "lhs",
            "template_sim_package": "",
            "outdir": str(client.app.state.outdir / "results"),
            "input_variables_yaml": "variables:\n  - name: wall_area\n    distribution: uniform\n    min: 100\n    max: 500",
        },
    )
    assert resp.status_code == 400
    assert "template_sim_package" in resp.json()["detail"]


def test_setup_requires_outdir(client: TestClient) -> None:
    resp = client.post(
        "/ui/api/setup",
        json={
            "n_samples": 5,
            "executor": "local",
            "openstudio_version": "3.11.0",
            "algorithm": "lhs",
            "template_sim_package": str(client.app.state.outdir / "template"),
            "outdir": "",
            "input_variables_yaml": "variables:\n  - name: wall_area\n    distribution: uniform\n    min: 100\n    max: 500",
        },
    )
    assert resp.status_code == 400
    assert "outdir" in resp.json()["detail"]


def test_setup_creates_campaign(tmp_outdir: Path) -> None:
    app = create_app(outdir=tmp_outdir, ui_enabled=True)
    client = TestClient(app)

    template_dir = tmp_outdir / "template"
    template_dir.mkdir()
    (template_dir / "workflow.osw").write_text("{}")

    resp = client.post(
        "/ui/api/setup",
        json={
            "n_samples": 3,
            "executor": "local",
            "openstudio_version": "3.11.0",
            "algorithm": "lhs",
            "template_sim_package": str(template_dir),
            "outdir": str(tmp_outdir / "results"),
            "input_variables_yaml": "variables:\n  - name: wall_area\n    distribution: uniform\n    min: 100\n    max: 500",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "campaign_id" in data
    assert data["status"] == "running"
    assert "outdir" in data


def test_setup_writes_variables_yml(tmp_outdir: Path) -> None:
    app = create_app(outdir=tmp_outdir, ui_enabled=True)
    client = TestClient(app)

    template_dir = tmp_outdir / "template"
    template_dir.mkdir()
    (template_dir / "workflow.osw").write_text("{}")

    yaml_content = "variables:\n  - name: wall_area\n    distribution: uniform\n    min: 100\n    max: 500"
    results_dir = tmp_outdir / "results"

    resp = client.post(
        "/ui/api/setup",
        json={
            "n_samples": 3,
            "executor": "local",
            "openstudio_version": "3.11.0",
            "algorithm": "lhs",
            "template_sim_package": str(template_dir),
            "outdir": str(results_dir),
            "input_variables_yaml": yaml_content,
        },
    )
    assert resp.status_code == 200
    assert (results_dir / "variables.yml").exists()


# ---------------------------------------------------------------------------
# GET /ui/api/campaigns/<id>  — get campaign status
# ---------------------------------------------------------------------------


def test_get_campaign_status_not_found(client: TestClient) -> None:
    resp = client.get("/ui/api/campaigns/nonexistent")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# GET /ui/api/campaigns/<id>/results  — get aggregated results
# ---------------------------------------------------------------------------


def test_get_campaign_results_not_found(client: TestClient) -> None:
    resp = client.get("/ui/api/campaigns/nonexistent/results")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /ui/api/campaigns/<id>/stop  — stop a running campaign
# ---------------------------------------------------------------------------


def test_stop_campaign_not_found(client: TestClient) -> None:
    resp = client.post("/ui/api/campaigns/nonexistent/stop")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Verify UI endpoints are NOT available when ui_enabled=False
# ---------------------------------------------------------------------------


def test_ui_disabled_when_not_enabled(tmp_outdir: Path) -> None:
    app = create_app(outdir=tmp_outdir, ui_enabled=False)
    client = TestClient(app)

    resp = client.get("/ui/")
    assert resp.status_code == 404
