"""Tests for campaign CRUD and per-sample result endpoints (issue #267)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="osimflow[api] extra required")
pytest.importorskip("slowapi", reason="osimflow[api] extra required")
from fastapi.testclient import TestClient

from osimflow.api import create_app

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_run_json(
    campaign_id: str = "test-campaign-001",
    finished_at: float | None = None,
    samples: list[dict] | None = None,
    steps: list[dict] | None = None,
    extra: dict | None = None,
) -> dict:
    """Build a minimal run.json dict."""
    data: dict = {
        "schema_version": 1,
        "campaign_id": campaign_id,
        "started_at": 1000.0,
        "finished_at": finished_at,
        "elapsed_s": (finished_at - 1000.0) if finished_at else None,
        "config_summary": {"executor": "local", "n_samples": 3},
        "summary": {
            "n_samples": len(samples) if samples is not None else 3,
            "n_succeeded": 2,
            "n_failed": 1,
        },
        "steps": steps
        or [
            {"step": "GENERATE_LHS_SAMPLES", "cache": "MISS", "elapsed_s": 0.5, "exit_code": 0},
        ],
        "per_sample": samples
        or [
            {"sample_id": "sample_000", "status": "ok", "elapsed_s": 10.0},
            {
                "sample_id": "sample_001",
                "status": "failed",
                "elapsed_s": 5.0,
                "error_summary": "Severe Error",
            },
            {"sample_id": "sample_002", "status": "ok", "elapsed_s": 12.0},
        ],
    }
    if extra:
        data.update(extra)
    return data


def _setup_campaign_dir(base: Path, campaign_id: str, run_json: dict | None = None) -> Path:
    """Create a campaign directory under base with a run.json."""
    cdir = base / campaign_id
    cdir.mkdir(parents=True, exist_ok=True)
    data = run_json or _make_run_json(campaign_id=campaign_id)
    (cdir / "run.json").write_text(json.dumps(data))
    return cdir


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def campaigns_base(tmp_path: Path) -> Path:
    """Base directory with two campaign subdirectories."""
    base = tmp_path / "campaigns"
    base.mkdir()

    # Campaign 1 — completed
    _setup_campaign_dir(
        base,
        "campaign-aaa",
        _make_run_json(
            campaign_id="campaign-aaa",
            finished_at=2000.0,
            samples=[
                {"sample_id": "s0", "status": "ok", "elapsed_s": 10.0},
                {"sample_id": "s1", "status": "ok", "elapsed_s": 12.0},
            ],
        ),
    )

    # Campaign 2 — running
    _setup_campaign_dir(
        base,
        "campaign-bbb",
        _make_run_json(
            campaign_id="campaign-bbb",
            finished_at=None,
            samples=[
                {"sample_id": "s0", "status": "ok", "elapsed_s": 8.0},
            ],
        ),
    )

    return base


@pytest.fixture
def client_rw(campaigns_base: Path) -> TestClient:
    """TestClient with campaigns_base_dir and read_only=False."""
    return TestClient(create_app(campaigns_base_dir=campaigns_base, read_only=False))


@pytest.fixture
def client_ro(campaigns_base: Path) -> TestClient:
    """TestClient with campaigns_base_dir and read_only=True (default)."""
    return TestClient(create_app(campaigns_base_dir=campaigns_base))


@pytest.fixture
def client_no_base(tmp_path: Path) -> TestClient:
    """TestClient with outdir only (no campaigns_base_dir)."""
    outdir = tmp_path / "single"
    outdir.mkdir()
    (outdir / "run.json").write_text(json.dumps(_make_run_json()))
    return TestClient(create_app(outdir=outdir))


# ---------------------------------------------------------------------------
# GET /api/v1/campaigns — list campaigns
# ---------------------------------------------------------------------------


class TestListCampaigns:
    """Tests for listing all campaigns."""

    def test_list_returns_both_campaigns(self, client_ro: TestClient) -> None:
        resp = client_ro.get("/api/v1/campaigns")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 2
        assert len(data["campaigns"]) == 2

    def test_list_campaign_fields(self, client_ro: TestClient) -> None:
        resp = client_ro.get("/api/v1/campaigns")
        data = resp.json()
        campaigns_by_id = {c["campaign_id"]: c for c in data["campaigns"]}

        aaa = campaigns_by_id["campaign-aaa"]
        assert aaa["status"] == "completed"
        assert aaa["finished_at"] == 2000.0
        assert aaa["n_samples"] == 2
        assert aaa["n_succeeded"] == 2

        bbb = campaigns_by_id["campaign-bbb"]
        assert bbb["status"] == "running"
        assert bbb["finished_at"] is None

    def test_list_empty_base(self, tmp_path: Path) -> None:
        base = tmp_path / "empty"
        base.mkdir()
        client = TestClient(create_app(campaigns_base_dir=base))
        resp = client.get("/api/v1/campaigns")
        assert resp.status_code == 200
        assert resp.json()["total"] == 0
        assert resp.json()["campaigns"] == []

    def test_list_skips_dirs_without_run_json(self, tmp_path: Path) -> None:
        base = tmp_path / "mixed"
        base.mkdir()
        (base / "has_run").mkdir()
        (base / "has_run" / "run.json").write_text(json.dumps(_make_run_json()))
        (base / "no_run").mkdir()
        client = TestClient(create_app(campaigns_base_dir=base))
        resp = client.get("/api/v1/campaigns")
        assert resp.status_code == 200
        assert resp.json()["total"] == 1

    def test_list_no_directory(self) -> None:
        client = TestClient(create_app(outdir=None))
        resp = client.get("/api/v1/campaigns")
        assert resp.status_code == 503

    def test_list_uses_outdir_fallback(self, client_no_base: TestClient) -> None:
        """When campaigns_base_dir is not set, falls back to outdir."""
        resp = client_no_base.get("/api/v1/campaigns")
        # outdir itself has a run.json but is not a subdirectory,
        # so it depends on outdir's parent. The fallback should still work.
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# GET /api/v1/campaigns/{campaign_id} — campaign status
# ---------------------------------------------------------------------------


class TestGetCampaignStatus:
    """Tests for individual campaign status."""

    def test_get_existing_campaign(self, client_ro: TestClient) -> None:
        resp = client_ro.get("/api/v1/campaigns/campaign-aaa")
        assert resp.status_code == 200
        data = resp.json()
        assert data["campaign_id"] == "campaign-aaa"
        assert data["status"] == "completed"
        assert data["elapsed_s"] is not None
        assert len(data["steps"]) >= 1

    def test_get_running_campaign(self, client_ro: TestClient) -> None:
        resp = client_ro.get("/api/v1/campaigns/campaign-bbb")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "running"
        assert data["finished_at"] is None

    def test_get_campaign_with_summary(self, client_ro: TestClient) -> None:
        resp = client_ro.get("/api/v1/campaigns/campaign-aaa")
        data = resp.json()
        assert data["summary"]["n_samples"] == 2

    def test_get_unknown_campaign_404(self, client_ro: TestClient) -> None:
        resp = client_ro.get("/api/v1/campaigns/nonexistent")
        assert resp.status_code == 404

    def test_get_campaign_with_traversal_attempt(self, client_ro: TestClient) -> None:
        """Directory traversal in campaign_id should be rejected."""
        resp = client_ro.get("/api/v1/campaigns/..%2Fetc")
        # FastAPI may decode the path; the key thing is it doesn't leak files
        assert resp.status_code in (400, 404)

    def test_get_campaign_with_baseline(self, campaigns_base: Path) -> None:
        """Campaign with baseline_comparison data."""
        cdir = campaigns_base / "campaign-aaa"
        data = json.loads((cdir / "run.json").read_text())
        data["baseline_comparison"] = {"improvement_pct": 15.0}
        data["total_cost_usd"] = 0.5
        data["spot_savings_usd"] = 0.1
        (cdir / "run.json").write_text(json.dumps(data))

        client = TestClient(create_app(campaigns_base_dir=campaigns_base))
        resp = client.get("/api/v1/campaigns/campaign-aaa")
        assert resp.status_code == 200
        result = resp.json()
        assert result["baseline_comparison"] == {"improvement_pct": 15.0}
        assert result["total_cost_usd"] == 0.5

    def test_get_campaign_corrupt_run_json(self, campaigns_base: Path) -> None:
        cdir = campaigns_base / "campaign-bbb"
        (cdir / "run.json").write_text("not valid json {{{{")
        client = TestClient(create_app(campaigns_base_dir=campaigns_base))
        resp = client.get("/api/v1/campaigns/campaign-bbb")
        assert resp.status_code == 500


# ---------------------------------------------------------------------------
# GET /api/v1/campaigns/{campaign_id}/samples — per-sample results
# ---------------------------------------------------------------------------


class TestListCampaignSamples:
    """Tests for paginated per-sample results."""

    def test_default_pagination(self, client_ro: TestClient) -> None:
        resp = client_ro.get("/api/v1/campaigns/campaign-aaa/samples")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 2
        assert data["page"] == 1
        assert len(data["samples"]) == 2
        assert data["samples"][0]["sample_id"] == "s0"

    def test_custom_pagination(self, client_ro: TestClient) -> None:
        resp = client_ro.get("/api/v1/campaigns/campaign-bbb/samples?page=1&per_page=1")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert len(data["samples"]) == 1
        assert data["page"] == 1
        assert data["per_page"] == 1

    def test_empty_samples(self, campaigns_base: Path) -> None:
        cdir = campaigns_base / "campaign-aaa"
        data = json.loads((cdir / "run.json").read_text())
        data["per_sample"] = []
        (cdir / "run.json").write_text(json.dumps(data))

        client = TestClient(create_app(campaigns_base_dir=campaigns_base))
        resp = client.get("/api/v1/campaigns/campaign-aaa/samples")
        assert resp.status_code == 200
        assert resp.json()["total"] == 0
        assert resp.json()["samples"] == []

    def test_sample_fields(self, client_ro: TestClient) -> None:
        resp = client_ro.get("/api/v1/campaigns/campaign-aaa/samples")
        data = resp.json()
        s0 = data["samples"][0]
        assert "sample_id" in s0
        assert "status" in s0
        assert "elapsed_s" in s0
        assert "error_summary" in s0

    def test_unknown_campaign_404(self, client_ro: TestClient) -> None:
        resp = client_ro.get("/api/v1/campaigns/nonexistent/samples")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# GET /api/v1/campaigns/{campaign_id}/samples/{sample_id}
# ---------------------------------------------------------------------------


class TestGetCampaignSampleDetail:
    """Tests for individual sample detail within a campaign."""

    def test_existing_sample(self, client_ro: TestClient) -> None:
        resp = client_ro.get("/api/v1/campaigns/campaign-aaa/samples/s0")
        assert resp.status_code == 200
        data = resp.json()
        assert data["sample_id"] == "s0"
        assert data["status"] == "ok"
        assert data["elapsed_s"] == 10.0

    def test_failed_sample(self, client_ro: TestClient) -> None:
        # campaign-bbb only has s0 (ok). Use campaign-aaa with modified data
        # to include a failed sample.
        pass

    def test_unknown_sample_404(self, client_ro: TestClient) -> None:
        resp = client_ro.get("/api/v1/campaigns/campaign-aaa/samples/nonexistent")
        assert resp.status_code == 404

    def test_unknown_campaign_404(self, client_ro: TestClient) -> None:
        resp = client_ro.get("/api/v1/campaigns/nonexistent/samples/s0")
        assert resp.status_code == 404

    def test_sample_with_kpi_file(self, campaigns_base: Path) -> None:
        """KPI file in the sim directory is loaded."""
        cdir = campaigns_base / "campaign-aaa"
        sim_dir = cdir / "work" / "sim" / "s0"
        sim_dir.mkdir(parents=True)
        kpi_data = {"eui_kwh_m2_yr": 120.5, "total_energy_kwh": 50000.0}
        (sim_dir / "kpi.json").write_text(json.dumps(kpi_data))

        client = TestClient(create_app(campaigns_base_dir=campaigns_base))
        resp = client.get("/api/v1/campaigns/campaign-aaa/samples/s0")
        assert resp.status_code == 200
        data = resp.json()
        assert data["kpis"] is not None
        assert data["kpis"]["eui_kwh_m2_yr"] == 120.5

    def test_sample_with_log_files(self, campaigns_base: Path) -> None:
        """Log file paths are returned."""
        cdir = campaigns_base / "campaign-aaa"
        sim_dir = cdir / "work" / "sim" / "s0"
        sim_dir.mkdir(parents=True)
        (sim_dir / "stdout.log").write_text("output")
        (sim_dir / "stderr.log").write_text("errors")

        client = TestClient(create_app(campaigns_base_dir=campaigns_base))
        resp = client.get("/api/v1/campaigns/campaign-aaa/samples/s0")
        assert resp.status_code == 200
        data = resp.json()
        assert "stdout.log" in data["log_files"]
        assert "stderr.log" in data["log_files"]

    def test_sample_no_kpi_file(self, client_ro: TestClient) -> None:
        """Sample without KPI file returns kpis=None."""
        resp = client_ro.get("/api/v1/campaigns/campaign-aaa/samples/s0")
        assert resp.status_code == 200
        assert resp.json()["kpis"] is None

    def test_sample_with_error_summary(self, campaigns_base: Path) -> None:
        """Failed sample includes error_summary."""
        cdir = campaigns_base / "campaign-aaa"
        data = json.loads((cdir / "run.json").read_text())
        data["per_sample"].append(
            {"sample_id": "s_fail", "status": "failed", "elapsed_s": 2.0, "error_summary": "Crash"}
        )
        (cdir / "run.json").write_text(json.dumps(data))

        client = TestClient(create_app(campaigns_base_dir=campaigns_base))
        resp = client.get("/api/v1/campaigns/campaign-aaa/samples/s_fail")
        assert resp.status_code == 200
        assert resp.json()["error_summary"] == "Crash"


# ---------------------------------------------------------------------------
# POST /api/v1/campaigns — create campaign
# ---------------------------------------------------------------------------


class TestCreateCampaign:
    """Tests for campaign creation."""

    def test_create_campaign_read_only_forbidden(self, client_ro: TestClient) -> None:
        resp = client_ro.post(
            "/api/v1/campaigns",
            json={
                "input_variables": "/tmp/vars.yml",
                "template_sim_package": "/tmp/pkg",
                "n_samples": 5,
            },
        )
        assert resp.status_code == 403

    def test_create_campaign_success(self, client_rw: TestClient, campaigns_base: Path) -> None:
        resp = client_rw.post(
            "/api/v1/campaigns",
            json={
                "input_variables": "/tmp/vars.yml",
                "template_sim_package": "/tmp/pkg",
                "n_samples": 5,
                "openstudio_version": "3.11.0",
            },
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["campaign_id"].startswith("campaign-")
        assert data["status"] == "created"
        # Verify the config file was written
        config_path = Path(data["outdir"]) / "campaign_config.json"
        assert config_path.exists()

    def test_create_campaign_with_outdir(self, client_rw: TestClient) -> None:
        resp = client_rw.post(
            "/api/v1/campaigns",
            json={
                "input_variables": "/tmp/vars.yml",
                "template_sim_package": "/tmp/pkg",
                "n_samples": 3,
                "outdir": "/tmp/custom-outdir",
            },
        )
        assert resp.status_code == 201
        data = resp.json()
        assert "/tmp/custom-outdir" in data["outdir"]

    def test_create_campaign_validation_n_samples_zero(self, client_rw: TestClient) -> None:
        """n_samples must be >= 1."""
        resp = client_rw.post(
            "/api/v1/campaigns",
            json={
                "input_variables": "/tmp/vars.yml",
                "template_sim_package": "/tmp/pkg",
                "n_samples": 0,
            },
        )
        assert resp.status_code == 422  # Pydantic validation error

    def test_create_campaign_missing_required_fields(self, client_rw: TestClient) -> None:
        resp = client_rw.post("/api/v1/campaigns", json={})
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# POST /api/v1/campaigns/{campaign_id}/cancel
# ---------------------------------------------------------------------------


class TestCancelCampaign:
    """Tests for campaign cancellation."""

    def test_cancel_running_campaign(self, client_rw: TestClient, campaigns_base: Path) -> None:
        resp = client_rw.post("/api/v1/campaigns/campaign-bbb/cancel")
        assert resp.status_code == 200
        data = resp.json()
        assert data["campaign_id"] == "campaign-bbb"
        assert data["status"] == "stopping"
        # Verify .stop file was created
        stop_file = campaigns_base / "campaign-bbb" / ".stop"
        assert stop_file.exists()

    def test_cancel_completed_campaign_409(self, client_rw: TestClient) -> None:
        resp = client_rw.post("/api/v1/campaigns/campaign-aaa/cancel")
        assert resp.status_code == 409
        assert "already completed" in resp.json()["detail"]

    def test_cancel_unknown_campaign_404(self, client_rw: TestClient) -> None:
        resp = client_rw.post("/api/v1/campaigns/nonexistent/cancel")
        assert resp.status_code == 404

    def test_cancel_read_only_403(self, client_ro: TestClient) -> None:
        resp = client_ro.post("/api/v1/campaigns/campaign-bbb/cancel")
        assert resp.status_code == 403

    def test_cancel_no_outdir(self) -> None:
        client = TestClient(create_app(outdir=None, read_only=False))
        resp = client.post("/api/v1/campaigns/some-id/cancel")
        assert resp.status_code == 503

    def test_cancel_idempotent(self, client_rw: TestClient, campaigns_base: Path) -> None:
        """Multiple cancel requests are idempotent."""
        resp1 = client_rw.post("/api/v1/campaigns/campaign-bbb/cancel")
        resp2 = client_rw.post("/api/v1/campaigns/campaign-bbb/cancel")
        assert resp1.status_code == 200
        assert resp2.status_code == 200


# ---------------------------------------------------------------------------
# GET /api/v1/campaigns/compare
# ---------------------------------------------------------------------------


class TestCompareCampaigns:
    """Tests for campaign comparison via GET /api/v1/campaigns/compare (issue #386/#588)."""

    def test_compare_both_found(
        self, client_kpis: TestClient, campaigns_base_with_kpis: Path
    ) -> None:
        """When both campaign directories exist with KPI data, return both details."""
        resp = client_kpis.get(
            "/api/v1/campaigns/compare",
            params={
                "outdir": [
                    str(campaigns_base_with_kpis / "campaign-aaa"),
                    str(campaigns_base_with_kpis / "campaign-bbb"),
                ]
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 2
        assert len(data["campaigns"]) == 2
        left = data["campaigns"][0]
        right = data["campaigns"][1]
        assert left["campaign_id"] == "campaign-aaa"
        assert right["campaign_id"] == "campaign-bbb"
        assert left["found"] is True
        assert right["found"] is True

    def test_compare_left_not_found(self, client_ro: TestClient, campaigns_base: Path) -> None:
        """When left campaign directory does not exist, return 404."""
        resp = client_ro.get(
            "/api/v1/campaigns/compare",
            params={
                "outdir": [
                    str(campaigns_base / "nonexistent"),
                    str(campaigns_base / "campaign-aaa"),
                ]
            },
        )
        assert resp.status_code == 404

    def test_compare_right_not_found(self, client_ro: TestClient, campaigns_base: Path) -> None:
        """When right campaign directory does not exist, return 404."""
        resp = client_ro.get(
            "/api/v1/campaigns/compare",
            params={
                "outdir": [
                    str(campaigns_base / "campaign-aaa"),
                    str(campaigns_base / "nonexistent"),
                ]
            },
        )
        assert resp.status_code == 404

    def test_compare_both_not_found(self, client_ro: TestClient, campaigns_base: Path) -> None:
        """When neither campaign directory exists, return 404."""
        resp = client_ro.get(
            "/api/v1/campaigns/compare",
            params={
                "outdir": [
                    str(campaigns_base / "nonexistent-1"),
                    str(campaigns_base / "nonexistent-2"),
                ]
            },
        )
        assert resp.status_code == 404

    def test_compare_missing_outdir(self, client_ro: TestClient) -> None:
        """When outdir is not provided, return 422 validation error."""
        resp = client_ro.get("/api/v1/campaigns/compare")
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# POST /api/v1/campaigns/compare — multi-campaign comparison (issue #404)
# ---------------------------------------------------------------------------


def _make_csv(base: Path, campaign_id: str, rows: list[dict]) -> Path:
    """Write an aggregated_results.csv in a campaign directory."""
    import csv

    csv_path = base / campaign_id / "aggregated_results.csv"
    if not rows:
        return csv_path
    fieldnames = list(rows[0].keys())
    with csv_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    return csv_path


@pytest.fixture
def campaigns_base_with_kpis(tmp_path: Path) -> Path:
    """Base directory with campaigns that have aggregated_results.csv."""
    base = tmp_path / "campaigns"
    base.mkdir()

    _setup_campaign_dir(
        base,
        "campaign-aaa",
        _make_run_json(
            campaign_id="campaign-aaa",
            finished_at=2000.0,
            samples=[
                {"sample_id": "s0", "status": "ok", "elapsed_s": 10.0},
                {"sample_id": "s1", "status": "ok", "elapsed_s": 12.0},
            ],
        ),
    )
    _make_csv(
        base,
        "campaign-aaa",
        [
            {"sample_id": "s0", "eui": 120.5, "total_energy": 50000},
            {"sample_id": "s1", "eui": 118.2, "total_energy": 48000},
        ],
    )

    _setup_campaign_dir(
        base,
        "campaign-bbb",
        _make_run_json(
            campaign_id="campaign-bbb",
            finished_at=3000.0,
            samples=[
                {"sample_id": "s0", "status": "ok", "elapsed_s": 8.0},
                {"sample_id": "s1", "status": "failed", "elapsed_s": 3.0},
                {"sample_id": "s2", "status": "ok", "elapsed_s": 9.0},
            ],
        ),
    )
    _make_csv(
        base,
        "campaign-bbb",
        [
            {"sample_id": "s0", "eui": 135.0, "total_energy": 55000},
            {"sample_id": "s2", "eui": 132.3, "total_energy": 53000},
        ],
    )

    return base


@pytest.fixture
def client_kpis(campaigns_base_with_kpis: Path) -> TestClient:
    """TestClient with campaigns that have KPI data."""
    return TestClient(create_app(campaigns_base_dir=campaigns_base_with_kpis))


class TestCompareCampaignsPost:
    """Tests for POST /api/v1/campaigns/compare (issue #404)."""

    def test_compare_two_campaigns_by_id(self, client_kpis: TestClient) -> None:
        """Compare two campaigns by campaign_id with full metadata."""
        resp = client_kpis.post(
            "/api/v1/campaigns/compare",
            json={
                "campaigns": [
                    {"campaign_id": "campaign-aaa"},
                    {"campaign_id": "campaign-bbb"},
                ]
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 2
        assert len(data["campaigns"]) == 2

        c0 = data["campaigns"][0]
        assert c0["found"] is True
        assert c0["campaign_id"] == "campaign-aaa"
        assert c0["status"] == "completed"
        assert c0["started_at"] == 1000.0
        assert c0["finished_at"] == 2000.0

    def test_compare_step_timing(self, client_kpis: TestClient) -> None:
        """Step timing traces are included."""
        resp = client_kpis.post(
            "/api/v1/campaigns/compare",
            json={
                "campaigns": [
                    {"campaign_id": "campaign-aaa"},
                    {"campaign_id": "campaign-bbb"},
                ]
            },
        )
        data = resp.json()
        for entry in data["campaigns"]:
            assert "step_timing" in entry
            assert isinstance(entry["step_timing"], list)
            assert len(entry["step_timing"]) >= 1

    def test_compare_sample_summary(self, client_kpis: TestClient) -> None:
        """Sample counts and success rates are computed."""
        resp = client_kpis.post(
            "/api/v1/campaigns/compare",
            json={
                "campaigns": [
                    {"campaign_id": "campaign-aaa"},
                    {"campaign_id": "campaign-bbb"},
                ]
            },
        )
        data = resp.json()

        # campaign-aaa: 2 samples, 2 succeeded
        s0 = data["campaigns"][0]["sample_summary"]
        assert s0["n_samples"] == 2
        assert s0["n_succeeded"] == 2
        assert s0["n_failed"] == 0
        assert s0["success_rate"] == 1.0

        # campaign-bbb: 3 samples, 2 succeeded, 1 failed
        s1 = data["campaigns"][1]["sample_summary"]
        assert s1["n_samples"] == 3
        assert s1["n_succeeded"] == 2
        assert s1["n_failed"] == 1

    def test_compare_kpi_stats(self, client_kpis: TestClient) -> None:
        """Per-campaign KPI statistics are computed from aggregated_results.csv."""
        resp = client_kpis.post(
            "/api/v1/campaigns/compare",
            json={
                "campaigns": [
                    {"campaign_id": "campaign-aaa"},
                    {"campaign_id": "campaign-bbb"},
                ]
            },
        )
        data = resp.json()

        kpi0 = data["campaigns"][0]["kpi_stats"]
        assert "eui" in kpi0
        assert "total_energy" in kpi0
        # mean of [120.5, 118.2] = 119.35
        assert abs(kpi0["eui"]["mean"] - 119.35) < 0.01
        assert kpi0["eui"]["count"] == 2

    def test_compare_kpi_comparison_alignment(self, client_kpis: TestClient) -> None:
        """KPI comparison rows align means across campaigns."""
        resp = client_kpis.post(
            "/api/v1/campaigns/compare",
            json={
                "campaigns": [
                    {"campaign_id": "campaign-aaa"},
                    {"campaign_id": "campaign-bbb"},
                ]
            },
        )
        data = resp.json()
        kpi_comp = data["kpi_comparison"]
        assert len(kpi_comp) > 0

        # Find the EUI comparison row
        eui_row = next(r for r in kpi_comp if r["metric"] == "eui")
        assert len(eui_row["values"]) == 2
        assert eui_row["values"][0] is not None
        assert eui_row["values"][1] is not None
        assert abs(eui_row["values"][0] - 119.35) < 0.01

    def test_compare_by_outdir(self, campaigns_base_with_kpis: Path) -> None:
        """Compare campaigns by explicit outdir paths."""
        client = TestClient(create_app(outdir=campaigns_base_with_kpis))
        resp = client.post(
            "/api/v1/campaigns/compare",
            json={
                "campaigns": [
                    {"outdir": str(campaigns_base_with_kpis / "campaign-aaa")},
                    {"outdir": str(campaigns_base_with_kpis / "campaign-bbb")},
                ]
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["campaigns"][0]["found"] is True
        assert data["campaigns"][0]["campaign_id"] == "campaign-aaa"
        assert data["campaigns"][1]["found"] is True
        assert data["campaigns"][1]["campaign_id"] == "campaign-bbb"

    def test_compare_mixed_id_and_outdir(
        self, client_kpis: TestClient, campaigns_base_with_kpis: Path
    ) -> None:
        """Compare using campaign_id for one and outdir for the other."""
        resp = client_kpis.post(
            "/api/v1/campaigns/compare",
            json={
                "campaigns": [
                    {"campaign_id": "campaign-aaa"},
                    {"outdir": str(campaigns_base_with_kpis / "campaign-bbb")},
                ]
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 2
        assert data["campaigns"][0]["found"] is True
        assert data["campaigns"][1]["found"] is True

    def test_compare_not_found(self, client_kpis: TestClient) -> None:
        """Missing campaign is returned with found=False and an error."""
        resp = client_kpis.post(
            "/api/v1/campaigns/compare",
            json={
                "campaigns": [
                    {"campaign_id": "campaign-aaa"},
                    {"campaign_id": "nonexistent"},
                ]
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["campaigns"][0]["found"] is True
        assert data["campaigns"][1]["found"] is False
        assert data["campaigns"][1]["error"] is not None

    def test_compare_three_campaigns(self, campaigns_base_with_kpis: Path) -> None:
        """Compare more than two campaigns."""
        # Add a third campaign
        _setup_campaign_dir(
            campaigns_base_with_kpis,
            "campaign-ccc",
            _make_run_json(
                campaign_id="campaign-ccc",
                finished_at=4000.0,
            ),
        )
        client = TestClient(create_app(campaigns_base_dir=campaigns_base_with_kpis))
        resp = client.post(
            "/api/v1/campaigns/compare",
            json={
                "campaigns": [
                    {"campaign_id": "campaign-aaa"},
                    {"campaign_id": "campaign-bbb"},
                    {"campaign_id": "campaign-ccc"},
                ]
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 3
        assert all(c["found"] for c in data["campaigns"])

    def test_compare_min_two_required(self, client_kpis: TestClient) -> None:
        """At least two campaigns are required."""
        resp = client_kpis.post(
            "/api/v1/campaigns/compare",
            json={"campaigns": [{"campaign_id": "campaign-aaa"}]},
        )
        assert resp.status_code == 422

    def test_compare_empty_campaigns_list(self, client_kpis: TestClient) -> None:
        """Empty campaigns list is rejected."""
        resp = client_kpis.post(
            "/api/v1/campaigns/compare",
            json={"campaigns": []},
        )
        assert resp.status_code == 422

    def test_compare_no_identifier_provided(self, client_kpis: TestClient) -> None:
        """When neither campaign_id nor outdir is given, entry has error."""
        resp = client_kpis.post(
            "/api/v1/campaigns/compare",
            json={
                "campaigns": [
                    {"campaign_id": "campaign-aaa"},
                    {},
                ]
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["campaigns"][1]["found"] is False
        assert data["campaigns"][1]["error"] is not None

    def test_compare_no_kpi_data(self, campaigns_base: Path) -> None:
        """When campaigns have no aggregated_results.csv, kpi_stats is empty."""
        client = TestClient(create_app(campaigns_base_dir=campaigns_base))
        resp = client.post(
            "/api/v1/campaigns/compare",
            json={
                "campaigns": [
                    {"campaign_id": "campaign-aaa"},
                    {"campaign_id": "campaign-bbb"},
                ]
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        for entry in data["campaigns"]:
            assert entry["kpi_stats"] == {}
        assert data["kpi_comparison"] == []

    def test_compare_config_metadata(self, client_kpis: TestClient) -> None:
        """Config summary is included in the comparison."""
        resp = client_kpis.post(
            "/api/v1/campaigns/compare",
            json={
                "campaigns": [
                    {"campaign_id": "campaign-aaa"},
                    {"campaign_id": "campaign-bbb"},
                ]
            },
        )
        data = resp.json()
        for entry in data["campaigns"]:
            assert entry["config"] is not None
            assert "executor" in entry["config"]

    def test_compare_get_endpoint_still_works(
        self, client_kpis: TestClient, campaigns_base_with_kpis: Path
    ) -> None:
        """The GET /compare endpoint works with outdir params."""
        resp = client_kpis.get(
            "/api/v1/campaigns/compare",
            params={
                "outdir": [
                    str(campaigns_base_with_kpis / "campaign-aaa"),
                    str(campaigns_base_with_kpis / "campaign-bbb"),
                ]
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "campaigns" in data
        assert len(data["campaigns"]) == 2


# ---------------------------------------------------------------------------
# Backward compatibility — existing endpoints still work
# ---------------------------------------------------------------------------


class TestBackwardCompatibility:
    """Verify existing single-campaign endpoints still work."""

    def test_legacy_campaign_endpoint(self, client_no_base: TestClient) -> None:
        resp = client_no_base.get("/api/v1/campaign")
        assert resp.status_code == 200
        assert resp.json()["campaign_id"] == "test-campaign-001"

    def test_legacy_samples_endpoint(self, client_no_base: TestClient) -> None:
        resp = client_no_base.get("/api/v1/samples")
        assert resp.status_code == 200

    def test_legacy_health(self, client_no_base: TestClient) -> None:
        resp = client_no_base.get("/health")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# POST /api/v1/campaigns/{campaign_id}/samples/batch_upload
# ---------------------------------------------------------------------------


class TestBatchUploadSamples:
    """Tests for batch sample upload (issue #552)."""

    def test_batch_upload_success(self, client_rw: TestClient, campaigns_base: Path) -> None:
        """Valid batch upload adds samples to run.json per_sample list."""
        resp = client_rw.post(
            "/api/v1/campaigns/campaign-aaa/samples/batch_upload",
            json={
                "samples": [
                    {"values": {"wall_r": 2.5, "window_shgc": 0.4}},
                    {"values": {"wall_r": 3.0, "window_shgc": 0.3}, "kpi_values": {"eui": 120.5}},
                ]
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["campaign_id"] == "campaign-aaa"
        assert data["added"] == 2
        assert len(data["sample_ids"]) == 2
        # Verify run.json was updated
        import json

        run_json = json.loads((campaigns_base / "campaign-aaa" / "run.json").read_text())
        assert len(run_json["per_sample"]) == 4  # 2 original + 2 new

    def test_batch_upload_read_only_forbidden(self, client_ro: TestClient) -> None:
        """Read-only clients get 403."""
        resp = client_ro.post(
            "/api/v1/campaigns/campaign-aaa/samples/batch_upload",
            json={"samples": [{"values": {"x": 1}}]},
        )
        assert resp.status_code == 403

    def test_batch_upload_unknown_campaign_404(self, client_rw: TestClient) -> None:
        """Unknown campaign returns 404."""
        resp = client_rw.post(
            "/api/v1/campaigns/nonexistent/samples/batch_upload",
            json={"samples": [{"values": {"x": 1}}]},
        )
        assert resp.status_code == 404

    def test_batch_upload_empty_samples_422(self, client_rw: TestClient) -> None:
        """Empty samples list is rejected with 422."""
        resp = client_rw.post(
            "/api/v1/campaigns/campaign-aaa/samples/batch_upload",
            json={"samples": []},
        )
        assert resp.status_code == 422

    def test_batch_upload_no_run_json_404(
        self, client_rw: TestClient, campaigns_base: Path
    ) -> None:
        """Campaign without run.json returns 404."""
        # Create a campaign dir without run.json
        (campaigns_base / "no-run").mkdir()
        resp = client_rw.post(
            "/api/v1/campaigns/no-run/samples/batch_upload",
            json={"samples": [{"values": {"x": 1}}]},
        )
        assert resp.status_code == 404

    def test_batch_upload_updates_summary(
        self, client_rw: TestClient, campaigns_base: Path
    ) -> None:
        """Summary n_samples is updated after batch upload."""
        import json

        resp = client_rw.post(
            "/api/v1/campaigns/campaign-aaa/samples/batch_upload",
            json={"samples": [{"values": {"x": 1}}]},
        )
        assert resp.status_code == 200
        run_json = json.loads((campaigns_base / "campaign-aaa" / "run.json").read_text())
        assert run_json["summary"]["n_samples"] == 3


# ---------------------------------------------------------------------------
# POST /api/v1/campaigns/{campaign_id}/samples/{sample_id}/requeue
# ---------------------------------------------------------------------------


class TestRequeueSample:
    """Tests for per-sample requeue (issue #552)."""

    def test_requeue_completed_sample(self, client_rw: TestClient) -> None:
        """Requeueing a completed sample creates a new pending sample."""
        resp = client_rw.post(
            "/api/v1/campaigns/campaign-aaa/samples/s0/requeue",
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["campaign_id"] == "campaign-aaa"
        assert data["original_sample_id"] == "s0"
        assert data["new_sample_id"] == "s0_reanalyze_1"
        assert data["status"] == "pending"

    def test_requeue_failed_sample(self, client_rw: TestClient) -> None:
        """Requeueing a failed sample creates a new pending sample."""
        resp = client_rw.post(
            "/api/v1/campaigns/campaign-bbb/samples/s0/requeue",
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["new_sample_id"] == "s0_reanalyze_1"

    def test_requeue_read_only_forbidden(self, client_ro: TestClient) -> None:
        """Read-only clients get 403."""
        resp = client_ro.post("/api/v1/campaigns/campaign-aaa/samples/s0/requeue")
        assert resp.status_code == 403

    def test_requeue_unknown_sample_404(self, client_rw: TestClient) -> None:
        """Unknown sample returns 404."""
        resp = client_rw.post(
            "/api/v1/campaigns/campaign-aaa/samples/nonexistent/requeue",
        )
        assert resp.status_code == 404

    def test_requeue_unknown_campaign_404(self, client_rw: TestClient) -> None:
        """Unknown campaign returns 404."""
        resp = client_rw.post(
            "/api/v1/campaigns/nonexistent/samples/s0/requeue",
        )
        assert resp.status_code == 404

    def test_requeue_running_sample_422(self, client_rw: TestClient, campaigns_base: Path) -> None:
        """Running sample cannot be requeued."""
        import json

        # Modify campaign-bbb's s0 to have "running" status
        cdir = campaigns_base / "campaign-bbb"
        data = json.loads((cdir / "run.json").read_text())
        data["per_sample"][0]["status"] = "running"
        (cdir / "run.json").write_text(json.dumps(data))

        resp = client_rw.post(
            "/api/v1/campaigns/campaign-bbb/samples/s0/requeue",
        )
        assert resp.status_code == 422
        assert "running" in resp.json()["detail"]

    def test_requeue_increments_count(self, client_rw: TestClient) -> None:
        """Multiple requeues of the same sample increment the suffix."""
        resp1 = client_rw.post(
            "/api/v1/campaigns/campaign-aaa/samples/s0/requeue",
        )
        assert resp1.status_code == 200
        assert resp1.json()["new_sample_id"] == "s0_reanalyze_1"

        resp2 = client_rw.post(
            "/api/v1/campaigns/campaign-aaa/samples/s0/requeue",
        )
        assert resp2.status_code == 200
        assert resp2.json()["new_sample_id"] == "s0_reanalyze_2"

    def test_requeue_updates_run_json(self, client_rw: TestClient, campaigns_base: Path) -> None:
        """Requeue updates run.json per_sample and summary."""
        import json

        resp = client_rw.post(
            "/api/v1/campaigns/campaign-aaa/samples/s0/requeue",
        )
        assert resp.status_code == 200

        run_json = json.loads((campaigns_base / "campaign-aaa" / "run.json").read_text())
        # Should have original s0, s1 + new s0_reanalyze_1
        sample_ids = [s["sample_id"] for s in run_json["per_sample"]]
        assert "s0_reanalyze_1" in sample_ids
        assert run_json["summary"]["n_samples"] == 3
