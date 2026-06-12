"""Tests for sample/results/failures/pareto API endpoints (issue #147)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="osimflow[api] extra required")
from fastapi.testclient import TestClient

from osimflow.api import create_app


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SAMPLES_DATA: list[dict] = [
    {
        "sample_id": "sample_000",
        "status": "ok",
        "elapsed_s": 10.0,
        "sim_exit_code": 0,
        "worker_id": "local",
    },
    {
        "sample_id": "sample_001",
        "status": "failed",
        "elapsed_s": 5.0,
        "sim_exit_code": 1,
        "error_summary": "Severe Error in model",
        "worker_id": "local",
    },
    {
        "sample_id": "sample_002",
        "status": "ok",
        "elapsed_s": 12.0,
        "sim_exit_code": 0,
        "worker_id": "local",
    },
]


def _make_run_json(
    samples: list[dict] | None = None,
    extra: dict | None = None,
) -> dict:
    """Build a minimal run.json dict."""
    data: dict = {
        "schema_version": 1,
        "campaign_id": "test-campaign-001",
        "started_at": 1000.0,
        "finished_at": 2000.0,
        "config_summary": {"executor": "local", "n_samples": 3},
        "steps": [
            {"step": "GENERATE_LHS_SAMPLES", "cache": "MISS", "elapsed_s": 0.5, "exit_code": 0},
        ],
        "per_sample": samples if samples is not None else SAMPLES_DATA,
    }
    if extra:
        data.update(extra)
    return data


@pytest.fixture
def outdir_with_samples(tmp_path: Path) -> Path:
    """Output dir with run.json containing 3 samples."""
    (tmp_path / "run.json").write_text(json.dumps(_make_run_json()))
    return tmp_path


@pytest.fixture
def outdir_with_kpi(tmp_path: Path) -> Path:
    """Output dir with run.json + a KPI file for sample_000."""
    (tmp_path / "run.json").write_text(json.dumps(_make_run_json()))
    sim_dir = tmp_path / "work" / "sim" / "sample_000"
    sim_dir.mkdir(parents=True)
    kpi_data = {"eui_kwh_m2_yr": 120.5, "total_energy_kwh": 50000.0}
    (sim_dir / "kpi.json").write_text(json.dumps(kpi_data))
    return tmp_path


@pytest.fixture
def outdir_with_logs(tmp_path: Path) -> Path:
    """Output dir with run.json + stdout/stderr logs for sample_000."""
    (tmp_path / "run.json").write_text(json.dumps(_make_run_json()))
    sim_dir = tmp_path / "work" / "sim" / "sample_000"
    sim_dir.mkdir(parents=True)
    (sim_dir / "stdout.log").write_text("sim output")
    (sim_dir / "stderr.log").write_text("sim errors")
    return tmp_path


@pytest.fixture
def outdir_with_csv(tmp_path: Path) -> Path:
    """Output dir with aggregated_results.csv and failed_simulations.csv."""
    (tmp_path / "run.json").write_text(json.dumps(_make_run_json()))
    (tmp_path / "aggregated_results.csv").write_text(
        "sample_id,eui,area\nsample_000,120.5,500.0\nsample_002,98.3,480.0\n"
    )
    (tmp_path / "failed_simulations.csv").write_text(
        "sample_id,error_summary\nsample_001,Severe Error in model\n"
    )
    return tmp_path


@pytest.fixture
def outdir_with_pareto(tmp_path: Path) -> Path:
    """Output dir with pareto generation files."""
    (tmp_path / "run.json").write_text(json.dumps(_make_run_json()))
    pareto_dir = tmp_path / "pareto"
    pareto_dir.mkdir()
    gen0 = {
        "objective_names": ["eui", "cost"],
        "maximize": [False, False],
        "solutions": [
            {"sample_id": "s0", "objectives": {"eui": 100, "cost": 5000}, "parameters": {}, "generation": 0},
        ],
    }
    gen1 = {
        "objective_names": ["eui", "cost"],
        "maximize": [False, False],
        "solutions": [
            {"sample_id": "s2", "objectives": {"eui": 90, "cost": 4500}, "parameters": {}, "generation": 1},
        ],
    }
    (pareto_dir / "gen_0.json").write_text(json.dumps(gen0))
    (pareto_dir / "gen_1.json").write_text(json.dumps(gen1))
    return tmp_path


@pytest.fixture
def client_factory():
    """Factory fixture to create a TestClient from a given outdir."""

    def _factory(outdir: Path) -> TestClient:
        return TestClient(create_app(outdir=outdir))

    return _factory


# ---------------------------------------------------------------------------
# GET /api/v1/samples (paginated)
# ---------------------------------------------------------------------------


class TestGetSamples:
    """Tests for the paginated samples list endpoint."""

    def test_default_pagination(self, outdir_with_samples: Path) -> None:
        client = TestClient(create_app(outdir=outdir_with_samples))
        resp = client.get("/api/v1/samples")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 3
        assert data["page"] == 1
        assert data["per_page"] == 50
        assert len(data["samples"]) == 3
        assert data["samples"][0]["sample_id"] == "sample_000"

    def test_custom_pagination(self, outdir_with_samples: Path) -> None:
        client = TestClient(create_app(outdir=outdir_with_samples))
        resp = client.get("/api/v1/samples?page=1&per_page=2")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 3
        assert len(data["samples"]) == 2
        assert data["page"] == 1
        assert data["per_page"] == 2

    def test_second_page(self, outdir_with_samples: Path) -> None:
        client = TestClient(create_app(outdir=outdir_with_samples))
        resp = client.get("/api/v1/samples?page=2&per_page=2")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 3
        assert len(data["samples"]) == 1
        assert data["samples"][0]["sample_id"] == "sample_002"
        assert data["page"] == 2

    def test_empty_samples(self, tmp_path: Path) -> None:
        (tmp_path / "run.json").write_text(json.dumps(_make_run_json(samples=[])))
        client = TestClient(create_app(outdir=tmp_path))
        resp = client.get("/api/v1/samples")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 0
        assert data["samples"] == []

    def test_no_run_json(self, tmp_path: Path) -> None:
        client = TestClient(create_app(outdir=tmp_path))
        resp = client.get("/api/v1/samples")
        assert resp.status_code == 404

    def test_no_outdir(self) -> None:
        client = TestClient(create_app(outdir=None))
        resp = client.get("/api/v1/samples")
        assert resp.status_code == 503


# ---------------------------------------------------------------------------
# GET /api/v1/samples/{sid}
# ---------------------------------------------------------------------------


class TestGetSampleDetail:
    """Tests for single sample detail endpoint."""

    def test_sample_found(self, outdir_with_samples: Path) -> None:
        client = TestClient(create_app(outdir=outdir_with_samples))
        resp = client.get("/api/v1/samples/sample_000")
        assert resp.status_code == 200
        data = resp.json()
        assert data["sample_id"] == "sample_000"
        assert data["status"] == "ok"

    def test_sample_not_found(self, outdir_with_samples: Path) -> None:
        client = TestClient(create_app(outdir=outdir_with_samples))
        resp = client.get("/api/v1/samples/nonexistent")
        assert resp.status_code == 404

    def test_sample_with_kpi(self, outdir_with_kpi: Path) -> None:
        client = TestClient(create_app(outdir=outdir_with_kpi))
        resp = client.get("/api/v1/samples/sample_000")
        assert resp.status_code == 200
        data = resp.json()
        assert data["kpis"] is not None
        assert data["kpis"]["eui_kwh_m2_yr"] == 120.5

    def test_sample_no_kpi_file(self, outdir_with_samples: Path) -> None:
        client = TestClient(create_app(outdir=outdir_with_samples))
        resp = client.get("/api/v1/samples/sample_000")
        assert resp.status_code == 200
        data = resp.json()
        assert data["kpis"] is None

    def test_sample_with_logs(self, outdir_with_logs: Path) -> None:
        client = TestClient(create_app(outdir=outdir_with_logs))
        resp = client.get("/api/v1/samples/sample_000")
        assert resp.status_code == 200
        data = resp.json()
        assert "stdout.log" in data["log_files"]
        assert "stderr.log" in data["log_files"]

    def test_sample_failed(self, outdir_with_samples: Path) -> None:
        client = TestClient(create_app(outdir=outdir_with_samples))
        resp = client.get("/api/v1/samples/sample_001")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "failed"
        assert data["error_summary"] == "Severe Error in model"


# ---------------------------------------------------------------------------
# GET /api/v1/results
# ---------------------------------------------------------------------------


class TestGetResults:
    """Tests for the aggregated results endpoint."""

    def test_results_with_data(self, outdir_with_csv: Path) -> None:
        client = TestClient(create_app(outdir=outdir_with_csv))
        resp = client.get("/api/v1/results")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) == 2
        assert data[0]["sample_id"] == "sample_000"
        assert data[0]["eui"] == 120.5

    def test_results_not_found(self, outdir_with_samples: Path) -> None:
        client = TestClient(create_app(outdir=outdir_with_samples))
        resp = client.get("/api/v1/results")
        assert resp.status_code == 404

    def test_results_no_outdir(self) -> None:
        client = TestClient(create_app(outdir=None))
        resp = client.get("/api/v1/results")
        assert resp.status_code == 503


# ---------------------------------------------------------------------------
# GET /api/v1/failures
# ---------------------------------------------------------------------------


class TestGetFailures:
    """Tests for the failed simulations endpoint."""

    def test_failures_with_data(self, outdir_with_csv: Path) -> None:
        client = TestClient(create_app(outdir=outdir_with_csv))
        resp = client.get("/api/v1/failures")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) == 1
        assert data[0]["sample_id"] == "sample_001"
        assert data[0]["error_summary"] == "Severe Error in model"

    def test_failures_not_found(self, outdir_with_samples: Path) -> None:
        client = TestClient(create_app(outdir=outdir_with_samples))
        resp = client.get("/api/v1/failures")
        assert resp.status_code == 404

    def test_failures_no_outdir(self) -> None:
        client = TestClient(create_app(outdir=None))
        resp = client.get("/api/v1/failures")
        assert resp.status_code == 503


# ---------------------------------------------------------------------------
# GET /api/v1/pareto
# ---------------------------------------------------------------------------


class TestGetPareto:
    """Tests for the pareto front endpoint."""

    def test_pareto_with_data(self, outdir_with_pareto: Path) -> None:
        client = TestClient(create_app(outdir=outdir_with_pareto))
        resp = client.get("/api/v1/pareto")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_generations"] == 2
        assert len(data["generations"]) == 2
        assert data["generations"][0]["_file"] == "gen_0.json"
        assert data["generations"][1]["_file"] == "gen_1.json"

    def test_pareto_no_data(self, outdir_with_samples: Path) -> None:
        client = TestClient(create_app(outdir=outdir_with_samples))
        resp = client.get("/api/v1/pareto")
        assert resp.status_code == 404

    def test_pareto_empty_dir(self, tmp_path: Path) -> None:
        (tmp_path / "run.json").write_text(json.dumps(_make_run_json()))
        (tmp_path / "pareto").mkdir()
        client = TestClient(create_app(outdir=tmp_path))
        resp = client.get("/api/v1/pareto")
        assert resp.status_code == 404

    def test_pareto_no_outdir(self) -> None:
        client = TestClient(create_app(outdir=None))
        resp = client.get("/api/v1/pareto")
        assert resp.status_code == 503
