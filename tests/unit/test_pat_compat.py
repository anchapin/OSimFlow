"""Tests for the PAT compatibility API shim layer (issue #265)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="osimflow[api] extra required")
pytest.importorskip("slowapi", reason="osimflow[api] extra required")

from fastapi.testclient import TestClient

from osimflow.api import create_app

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def campaign_outdir(tmp_path: Path) -> Path:
    """Create a campaign output directory with a populated run.json."""
    run_json = {
        "schema_version": 1,
        "campaign_id": "pat-test-campaign",
        "started_at": 1000.0,
        "finished_at": 2000.0,
        "config_summary": {
            "executor": "local",
            "n_samples": 3,
            "source": "pat_compat",
        },
        "summary": {"n_samples": 3, "n_succeeded": 2, "n_failed": 1},
        "steps": [
            {"step": "GENERATE_LHS_SAMPLES", "cache": "MISS", "elapsed_s": 0.5, "exit_code": 0},
        ],
        "per_sample": [
            {
                "sample_id": "sample_000",
                "status": "ok",
                "elapsed_s": 100.0,
            },
            {
                "sample_id": "sample_001",
                "status": "ok",
                "elapsed_s": 120.0,
                "kpis": {"eui_kwh_m2_yr": 125.3},
            },
            {
                "sample_id": "sample_002",
                "status": "failed",
                "elapsed_s": 5.0,
                "error_summary": "Severe Error: something went wrong",
            },
        ],
    }
    campaign_dir = tmp_path / "pat-test-campaign"
    campaign_dir.mkdir()
    (campaign_dir / "run.json").write_text(json.dumps(run_json))
    return tmp_path


@pytest.fixture
def client(campaign_outdir: Path) -> TestClient:
    """Create a TestClient with campaigns_base_dir pointing at the test campaigns."""
    app = create_app(campaigns_base_dir=campaign_outdir, read_only=False)
    return TestClient(app)


@pytest.fixture
def read_only_client(campaign_outdir: Path) -> TestClient:
    """Create a TestClient in read-only mode."""
    app = create_app(campaigns_base_dir=campaign_outdir, read_only=True)
    return TestClient(app)


# ---------------------------------------------------------------------------
# GET status endpoint
# ---------------------------------------------------------------------------


class TestGetPATStatus:
    """Tests for GET /api/v1/pat/analyses/{id}/status."""

    def test_status_returns_pat_format(self, client: TestClient) -> None:
        resp = client.get("/api/v1/pat/analyses/pat-test-campaign/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["analysis_id"] == "pat-test-campaign"
        assert data["status"] == "completed"
        assert data["started_at"] == 1000.0
        assert data["finished_at"] == 2000.0

    def test_status_data_point_counts(self, client: TestClient) -> None:
        resp = client.get("/api/v1/pat/analyses/pat-test-campaign/status")
        data = resp.json()
        dp = data["data_points"]
        assert dp["total"] == 3
        assert dp["completed"] == 2
        assert dp["failed"] == 1
        assert dp["pending"] == 0

    def test_status_unknown_campaign(self, client: TestClient) -> None:
        resp = client.get("/api/v1/pat/analyses/nonexistent/status")
        assert resp.status_code == 404

    def test_status_running_campaign(self, campaign_outdir: Path) -> None:
        """A campaign with started_at but no finished_at shows 'running'."""
        campaign_dir = campaign_outdir / "running-campaign"
        campaign_dir.mkdir()
        run_json = {
            "campaign_id": "running-campaign",
            "started_at": 1000.0,
            "finished_at": None,
            "per_sample": [
                {"sample_id": "s0", "status": "ok"},
                {"sample_id": "s1", "status": "pending"},
            ],
        }
        (campaign_dir / "run.json").write_text(json.dumps(run_json))

        app = create_app(campaigns_base_dir=campaign_outdir, read_only=False)
        client = TestClient(app)
        resp = client.get("/api/v1/pat/analyses/running-campaign/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "running"
        assert data["data_points"]["pending"] == 1

    def test_status_not_started_campaign(self, campaign_outdir: Path) -> None:
        """A campaign with no started_at shows 'not_started'."""
        campaign_dir = campaign_outdir / "notstarted"
        campaign_dir.mkdir()
        run_json = {
            "campaign_id": "notstarted",
            "started_at": None,
            "finished_at": None,
            "per_sample": [],
        }
        (campaign_dir / "run.json").write_text(json.dumps(run_json))

        app = create_app(campaigns_base_dir=campaign_outdir, read_only=False)
        client = TestClient(app)
        resp = client.get("/api/v1/pat/analyses/notstarted/status")
        assert resp.status_code == 200
        assert resp.json()["status"] == "not_started"


# ---------------------------------------------------------------------------
# GET data_points endpoint
# ---------------------------------------------------------------------------


class TestGetPATDataPoints:
    """Tests for GET /api/v1/pat/analyses/{id}/data_points."""

    def test_data_points_returns_all_samples(self, client: TestClient) -> None:
        resp = client.get("/api/v1/pat/analyses/pat-test-campaign/data_points")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 3
        assert len(data["data_points"]) == 3

    def test_data_point_field_mapping(self, client: TestClient) -> None:
        resp = client.get("/api/v1/pat/analyses/pat-test-campaign/data_points")
        data = resp.json()
        dp = data["data_points"]

        # First sample: ok, no KPIs
        assert dp[0]["data_point_id"] == "sample_000"
        assert dp[0]["status"] == "ok"
        assert dp[0]["elapsed_s"] == 100.0

        # Second sample: ok, with KPIs
        assert dp[1]["data_point_id"] == "sample_001"
        assert dp[1]["results"] == {"eui_kwh_m2_yr": 125.3}

        # Third sample: failed with error
        assert dp[2]["data_point_id"] == "sample_002"
        assert dp[2]["status"] == "failed"
        assert dp[2]["error_summary"] == "Severe Error: something went wrong"

    def test_data_points_unknown_campaign(self, client: TestClient) -> None:
        resp = client.get("/api/v1/pat/analyses/nonexistent/data_points")
        assert resp.status_code == 404

    def test_data_points_empty_campaign(self, campaign_outdir: Path) -> None:
        campaign_dir = campaign_outdir / "empty-campaign"
        campaign_dir.mkdir()
        run_json = {
            "campaign_id": "empty-campaign",
            "per_sample": [],
        }
        (campaign_dir / "run.json").write_text(json.dumps(run_json))

        app = create_app(campaigns_base_dir=campaign_outdir, read_only=False)
        client = TestClient(app)
        resp = client.get("/api/v1/pat/analyses/empty-campaign/data_points")
        assert resp.status_code == 200
        assert resp.json()["total"] == 0
        assert resp.json()["data_points"] == []


# ---------------------------------------------------------------------------
# POST analyses endpoint
# ---------------------------------------------------------------------------


class TestCreatePATAnalysis:
    """Tests for POST /api/v1/pat/analyses."""

    def test_create_requires_write_mode(self, read_only_client: TestClient) -> None:
        resp = read_only_client.post(
            "/api/v1/pat/analyses",
            json={
                "osa_path": "/tmp/test.osa",
                "template_sim_package": "/tmp/pkg",
            },
        )
        assert resp.status_code == 403

    def test_create_requires_osa_path_or_analysis(self, client: TestClient) -> None:
        resp = client.post(
            "/api/v1/pat/analyses",
            json={
                "template_sim_package": "/tmp/pkg",
            },
        )
        assert resp.status_code == 422

    def test_create_requires_valid_template_sim_package(
        self,
        campaign_outdir: Path,
    ) -> None:
        app = create_app(campaigns_base_dir=campaign_outdir, read_only=False)
        client = TestClient(app)
        resp = client.post(
            "/api/v1/pat/analyses",
            json={
                "analysis": {
                    "problem": {
                        "algorithm": {"type": "lhs", "number_of_samples": 5},
                        "variables": [],
                    },
                },
                "template_sim_package": "/nonexistent/path",
            },
        )
        assert resp.status_code == 422

    def test_create_with_inline_analysis(
        self,
        campaign_outdir: Path,
    ) -> None:
        """Create an analysis from inline JSON (no OSA file)."""
        # Create a valid template_sim_package
        tsp = campaign_outdir / "template_pkg"
        tsp.mkdir()

        app = create_app(campaigns_base_dir=campaign_outdir, read_only=False)
        client = TestClient(app)
        resp = client.post(
            "/api/v1/pat/analyses",
            json={
                "analysis": {
                    "problem": {
                        "algorithm": {"type": "lhs", "number_of_samples": 5},
                        "variables": [
                            {
                                "name": "test_var",
                                "variable_type": "variable",
                                "distribution": {
                                    "type": "uniform",
                                    "minimum": 0.0,
                                    "maximum": 1.0,
                                },
                            },
                        ],
                    },
                },
                "template_sim_package": str(tsp),
                "n_samples": 5,
                "auto_start": False,
            },
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["analysis_id"].startswith("pat-")
        assert data["status"] == "created"
        assert data["osimflow_campaign_id"] == data["analysis_id"]

        # Verify variables.yml was created
        analysis_id = data["analysis_id"]
        campaign_dir = campaign_outdir / analysis_id
        assert (campaign_dir / "variables.yml").exists()
        assert (campaign_dir / "campaign_config.json").exists()

    def test_create_with_osa_path(
        self,
        campaign_outdir: Path,
    ) -> None:
        """Create an analysis from a .osa file path."""
        import json as json_mod
        import zipfile

        # Create a minimal .osa file
        analysis_data = {
            "analysis": {
                "problem": {
                    "algorithm": {"type": "lhs", "number_of_samples": 10},
                    "variables": [
                        {
                            "name": "r_value",
                            "variable_type": "variable",
                            "distribution": {
                                "type": "uniform",
                                "minimum": 5.0,
                                "maximum": 30.0,
                            },
                        },
                    ],
                },
            },
        }
        osa_path = campaign_outdir / "test.osa"
        with zipfile.ZipFile(osa_path, "w") as zf:
            zf.writestr("analysis.json", json_mod.dumps(analysis_data, indent=2))

        tsp = campaign_outdir / "template_pkg"
        tsp.mkdir()

        app = create_app(campaigns_base_dir=campaign_outdir, read_only=False)
        client = TestClient(app)
        resp = client.post(
            "/api/v1/pat/analyses",
            json={
                "osa_path": str(osa_path),
                "template_sim_package": str(tsp),
                "n_samples": 10,
                "auto_start": False,
            },
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["status"] == "created"

    def test_create_with_nonexistent_osa_path(
        self,
        campaign_outdir: Path,
    ) -> None:
        tsp = campaign_outdir / "template_pkg"
        tsp.mkdir()

        app = create_app(campaigns_base_dir=campaign_outdir, read_only=False)
        client = TestClient(app)
        resp = client.post(
            "/api/v1/pat/analyses",
            json={
                "osa_path": "/nonexistent/file.osa",
                "template_sim_package": str(tsp),
            },
        )
        assert resp.status_code == 422

    def test_create_auto_start_sets_running(
        self,
        campaign_outdir: Path,
    ) -> None:
        """auto_start=True should set status to 'running'."""
        tsp = campaign_outdir / "template_pkg"
        tsp.mkdir()

        app = create_app(campaigns_base_dir=campaign_outdir, read_only=False)
        client = TestClient(app)
        resp = client.post(
            "/api/v1/pat/analyses",
            json={
                "analysis": {
                    "problem": {
                        "algorithm": {"type": "lhs", "number_of_samples": 3},
                        "variables": [
                            {
                                "name": "x",
                                "distribution": {"type": "uniform", "minimum": 0, "maximum": 1},
                            },
                        ],
                    },
                },
                "template_sim_package": str(tsp),
                "n_samples": 3,
                "auto_start": True,
            },
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["status"] == "running"

        # Verify run.json was created
        analysis_id = data["analysis_id"]
        campaign_dir = campaign_outdir / analysis_id
        assert (campaign_dir / "run.json").exists()


# ---------------------------------------------------------------------------
# Integration: create → status → data_points
# ---------------------------------------------------------------------------


class TestPATWorkflowIntegration:
    """End-to-end test of the PAT analysis workflow."""

    def test_create_then_poll(self, campaign_outdir: Path) -> None:
        """Create an analysis and then poll its status and data points."""
        tsp = campaign_outdir / "template_pkg"
        tsp.mkdir()

        app = create_app(campaigns_base_dir=campaign_outdir, read_only=False)
        client = TestClient(app)

        # Create
        resp = client.post(
            "/api/v1/pat/analyses",
            json={
                "analysis": {
                    "problem": {
                        "algorithm": {"type": "lhs", "number_of_samples": 2},
                        "variables": [
                            {
                                "name": "var_a",
                                "distribution": {"type": "uniform", "minimum": 0, "maximum": 10},
                            },
                        ],
                    },
                },
                "template_sim_package": str(tsp),
                "n_samples": 2,
                "auto_start": False,
            },
        )
        assert resp.status_code == 201
        analysis_id = resp.json()["analysis_id"]

        # Poll status
        resp = client.get(f"/api/v1/pat/analyses/{analysis_id}/status")
        assert resp.status_code == 404  # No run.json yet (not started)

        # Write a minimal run.json to simulate a running campaign
        campaign_dir = campaign_outdir / analysis_id
        run_data = {
            "campaign_id": analysis_id,
            "started_at": 1000.0,
            "finished_at": None,
            "per_sample": [
                {"sample_id": "s0", "status": "ok", "elapsed_s": 50.0},
            ],
        }
        (campaign_dir / "run.json").write_text(json.dumps(run_data))

        # Poll status again
        resp = client.get(f"/api/v1/pat/analyses/{analysis_id}/status")
        assert resp.status_code == 200
        assert resp.json()["status"] == "running"
        assert resp.json()["data_points"]["total"] == 1

        # Get data points
        resp = client.get(f"/api/v1/pat/analyses/{analysis_id}/data_points")
        assert resp.status_code == 200
        assert resp.json()["total"] == 1
        assert resp.json()["data_points"][0]["data_point_id"] == "s0"
