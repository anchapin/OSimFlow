"""Tests for results_viewer scatter_matrix and radar endpoints (issue #584)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from osimflow.api import create_app

pytest.importorskip("fastapi", reason="osimflow[api] extra required")


def _make_run_json(
    campaign_id: str = "test-campaign-001",
    samples: list[dict] | None = None,
) -> dict:
    data: dict = {
        "schema_version": 1,
        "campaign_id": campaign_id,
        "started_at": 1000.0,
        "finished_at": 2000.0,
        "elapsed_s": 1000.0,
        "config_summary": {"executor": "local", "n_samples": 3},
        "steps": [
            {"step": "GENERATE_LHS_SAMPLES", "cache": "MISS", "elapsed_s": 0.5, "exit_code": 0},
        ],
        "per_sample": samples
        or [
            {"sample_id": "s0", "status": "ok", "elapsed_s": 10.0},
            {"sample_id": "s1", "status": "ok", "elapsed_s": 12.0},
            {"sample_id": "s2", "status": "ok", "elapsed_s": 11.0},
        ],
    }
    return data


def _make_kpi_csv(base: Path, campaign_id: str) -> Path:
    csv_path = base / campaign_id / "aggregated_results.csv"
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    csv_path.write_text(
        "sample_id,wall_r,window_u,window_shgc,eui_kwh_m2_yr,cost_annual,peak_kw\n"
        "s0,2.5,0.3,0.4,120.5,8500,45.2\n"
        "s1,3.0,0.25,0.35,118.2,8200,43.1\n"
        "s2,2.0,0.35,0.45,135.0,9100,50.0\n"
    )
    return csv_path


@pytest.fixture
def rv_campaign_dir(tmp_path: Path) -> Path:
    base = tmp_path / "campaigns"
    base.mkdir()
    campaign_id = "rv-campaign-001"
    cdir = base / campaign_id
    cdir.mkdir(parents=True)
    (cdir / "run.json").write_text(json.dumps(_make_run_json(campaign_id=campaign_id)))
    _make_kpi_csv(base, campaign_id)
    return base


@pytest.fixture
def rv_client(rv_campaign_dir: Path) -> pytest.TestClient:
    from fastapi.testclient import TestClient

    return TestClient(create_app(campaigns_base_dir=rv_campaign_dir, results_viewer=True))


# ---------------------------------------------------------------------------
# GET /results/{campaign_id}/scatter_matrix
# ---------------------------------------------------------------------------


class TestScatterMatrixEndpoint:
    def test_returns_scatter_matrix_data(self, rv_client: pytest.TestClient) -> None:
        resp = rv_client.get("/results/rv-campaign-001/scatter_matrix")
        assert resp.status_code == 200
        d = resp.json()
        assert "variables" in d
        assert "kpis" in d
        assert "traces" in d
        assert "n_samples" in d
        assert d["n_samples"] == 3

    def test_variables_are_lhs_columns(self, rv_client: pytest.TestClient) -> None:
        resp = rv_client.get("/results/rv-campaign-001/scatter_matrix")
        assert resp.status_code == 200
        d = resp.json()
        assert "wall_r" in d["variables"]
        assert "window_u" in d["variables"]

    def test_kpis_are_inferred(self, rv_client: pytest.TestClient) -> None:
        resp = rv_client.get("/results/rv-campaign-001/scatter_matrix")
        assert resp.status_code == 200
        d = resp.json()
        assert "eui_kwh_m2_yr" in d["kpis"]
        assert "cost_annual" in d["kpis"]
        assert "peak_kw" in d["kpis"]

    def test_traces_have_xy_and_correlation(self, rv_client: pytest.TestClient) -> None:
        resp = rv_client.get("/results/rv-campaign-001/scatter_matrix")
        assert resp.status_code == 200
        d = resp.json()
        assert len(d["traces"]) > 0
        for t in d["traces"]:
            assert "x" in t
            assert "y" in t
            assert "correlation" in t
            assert "variable" in t
            assert "kpi" in t
            assert len(t["x"]) == 3
            assert len(t["y"]) == 3

    def test_404_for_unknown_campaign(self, rv_client: pytest.TestClient) -> None:
        resp = rv_client.get("/results/nonexistent/scatter_matrix")
        assert resp.status_code == 404

    def test_404_when_no_results_csv(
        self, rv_client: pytest.TestClient, rv_campaign_dir: Path
    ) -> None:
        (rv_campaign_dir / "no-results").mkdir()
        (rv_campaign_dir / "no-results" / "run.json").write_text(
            json.dumps(_make_run_json(campaign_id="no-results"))
        )
        resp = rv_client.get("/results/no-results/scatter_matrix")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# GET /results/{campaign_id}/radar
# ---------------------------------------------------------------------------


class TestRadarEndpoint:
    def test_returns_radar_data(self, rv_client: pytest.TestClient) -> None:
        resp = rv_client.get("/results/rv-campaign-001/radar")
        assert resp.status_code == 200
        d = resp.json()
        assert "kpis" in d
        assert "samples" in d
        assert "ranges" in d

    def test_samples_have_values_and_raw(self, rv_client: pytest.TestClient) -> None:
        resp = rv_client.get("/results/rv-campaign-001/radar")
        assert resp.status_code == 200
        d = resp.json()
        assert len(d["samples"]) == 3
        for s in d["samples"]:
            assert "sample_id" in s
            assert "values" in s
            assert "raw" in s
            assert len(s["values"]) == len(d["kpis"])
            assert len(s["raw"]) == len(d["kpis"])

    def test_values_are_normalised_0_to_1(self, rv_client: pytest.TestClient) -> None:
        resp = rv_client.get("/results/rv-campaign-001/radar")
        assert resp.status_code == 200
        d = resp.json()
        for s in d["samples"]:
            for v in s["values"]:
                assert 0.0 <= v <= 1.0

    def test_ranges_match_kpis(self, rv_client: pytest.TestClient) -> None:
        resp = rv_client.get("/results/rv-campaign-001/radar")
        assert resp.status_code == 200
        d = resp.json()
        assert len(d["ranges"]) == len(d["kpis"])
        for r in d["ranges"]:
            assert len(r) == 2
            assert r[0] <= r[1]

    def test_max_kpis_query_param(self, rv_client: pytest.TestClient) -> None:
        resp = rv_client.get("/results/rv-campaign-001/radar?max_kpis=2")
        assert resp.status_code == 422

        resp_ok = rv_client.get("/results/rv-campaign-001/radar?max_kpis=3")
        assert resp_ok.status_code == 200
        d = resp_ok.json()
        assert len(d["kpis"]) <= 3

    def test_404_for_unknown_campaign(self, rv_client: pytest.TestClient) -> None:
        resp = rv_client.get("/results/nonexistent/radar")
        assert resp.status_code == 404

    def test_404_when_too_few_kpis(
        self, rv_client: pytest.TestClient, rv_campaign_dir: Path
    ) -> None:
        (rv_campaign_dir / "few-kpis").mkdir()
        (rv_campaign_dir / "few-kpis" / "run.json").write_text(
            json.dumps(_make_run_json(campaign_id="few-kpis"))
        )
        few_kpi_csv = rv_campaign_dir / "few-kpis" / "aggregated_results.csv"
        few_kpi_csv.write_text("sample_id,single_kpi\ns0,100\ns1,110\n")
        resp = rv_client.get("/results/few-kpis/radar")
        assert resp.status_code == 404
        assert "3 KPIs" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# GET /results/{campaign_id} — available_plots includes new chart types
# ---------------------------------------------------------------------------


class TestAvailablePlotsUpdated:
    def test_available_plots_has_scatter_matrix(self, rv_client: pytest.TestClient) -> None:
        resp = rv_client.get("/results/rv-campaign-001")
        assert resp.status_code == 200
        d = resp.json()
        assert "scatter_matrix" in d["available_plots"]
        assert d["available_plots"]["scatter_matrix"] is True

    def test_available_plots_has_radar(self, rv_client: pytest.TestClient) -> None:
        resp = rv_client.get("/results/rv-campaign-001")
        assert resp.status_code == 200
        d = resp.json()
        assert "radar" in d["available_plots"]
        assert d["available_plots"]["radar"] is True

    def test_available_plots_has_parallel_coordinates(self, rv_client: pytest.TestClient) -> None:
        resp = rv_client.get("/results/rv-campaign-001")
        assert resp.status_code == 200
        d = resp.json()
        assert "parallel_coordinates" in d["available_plots"]
        assert d["available_plots"]["parallel_coordinates"] is True

    def test_scatter_matrix_false_when_insufficient_columns(
        self, rv_client: pytest.TestClient, rv_campaign_dir: Path
    ) -> None:
        (rv_campaign_dir / "single-var").mkdir()
        (rv_campaign_dir / "single-var" / "run.json").write_text(
            json.dumps(_make_run_json(campaign_id="single-var"))
        )
        single_var_csv = rv_campaign_dir / "single-var" / "aggregated_results.csv"
        single_var_csv.write_text("sample_id,wall_r,eui_kwh_m2_yr\ns0,2.5,120\n")
        resp = rv_client.get("/results/single-var")
        assert resp.status_code == 200
        d = resp.json()
        assert d["available_plots"]["scatter_matrix"] is False
