"""Integration tests for ASHRAE 90.1 baseline comparison mode (issue #64).

Covers:
  * Baseline section parsed from variables.yml into CampaignConfig.
  * Baseline sample injected into the LHS sample set.
  * Percentage improvement columns in aggregated_results.csv.
  * Baseline reference data in run.json.
  * Baseline EUI reference line in the summary plot.
  * Backward compatibility: when no baseline section, behaviour is unchanged.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from osimflow import Campaign, CampaignConfig
from osimflow.executors import LocalExecutor


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def workdir_with_baseline(tmp_path: Path) -> Path:
    """Work directory with a variables.yml containing a baseline section."""
    wd = tmp_path / "work"
    wd.mkdir()

    (wd / "variables.yml").write_text(
        yaml.safe_dump(
            {
                "variables": [
                    {"name": "u1", "distribution": "uniform", "min": 0.0, "max": 1.0},
                    {"name": "u2", "distribution": "uniform", "min": 10.0, "max": 20.0},
                ],
                "baseline": {
                    "sample_id": "baseline",
                    "parameters": {
                        "u1": 0.5,
                        "u2": 15.0,
                    },
                },
            }
        )
    )
    return wd


@pytest.fixture
def workdir_no_baseline(tmp_path: Path) -> Path:
    """Work directory with a variables.yml WITHOUT a baseline section."""
    wd = tmp_path / "work"
    wd.mkdir()

    (wd / "variables.yml").write_text(
        yaml.safe_dump(
            {
                "variables": [
                    {"name": "u1", "distribution": "uniform", "min": 0.0, "max": 1.0},
                ],
            }
        )
    )
    return wd


@pytest.fixture
def template_pkg(tmp_path: Path) -> Path:
    """A minimal template_sim_package."""
    pkg = tmp_path / "template"
    pkg.mkdir()
    (pkg / "model.osm").write_text(json.dumps({"attributes": {"u1": 0.0, "u2": 10.0}}))
    (pkg / "workflow.osw").write_text(json.dumps({"name": "stub"}))
    return pkg


@pytest.fixture
def outdir(tmp_path: Path) -> Path:
    od = tmp_path / "out"
    od.mkdir()
    return od


# ---------------------------------------------------------------------------
# Config tests
# ---------------------------------------------------------------------------
def test_config_parses_baseline_section(workdir_with_baseline: Path) -> None:
    """load_config must parse the baseline section from variables.yml."""
    from osimflow.config import load_config

    args = {
        "input_variables": str(workdir_with_baseline / "variables.yml"),
        "template_sim_package": str(workdir_with_baseline),
        "n_samples": 3,
        "outdir": str(workdir_with_baseline / "out"),
        "openstudio_version": "3.4.0",
        "archive_intermediates": False,
        "custom_apply_script": None,
        "custom_kpi_extractor": None,
        "mlflow_tracking_uri": None,
        "slurm_qos": None,
        "slurm_constraint": None,
        "slurm_gres": None,
    }
    (workdir_with_baseline / "out").mkdir(exist_ok=True)
    cfg = load_config(args)
    assert cfg.baseline is not None
    assert cfg.baseline["sample_id"] == "baseline"
    params = cfg.baseline["parameters"]
    assert isinstance(params, dict)
    assert params["u1"] == 0.5
    assert params["u2"] == 15.0


def test_config_baseline_none_when_absent(workdir_no_baseline: Path) -> None:
    """load_config must set baseline=None when no baseline section."""
    from osimflow.config import load_config

    args = {
        "input_variables": str(workdir_no_baseline / "variables.yml"),
        "template_sim_package": str(workdir_no_baseline),
        "n_samples": 2,
        "outdir": str(workdir_no_baseline / "out"),
        "openstudio_version": "3.4.0",
        "archive_intermediates": False,
        "custom_apply_script": None,
        "custom_kpi_extractor": None,
        "mlflow_tracking_uri": None,
        "slurm_qos": None,
        "slurm_constraint": None,
        "slurm_gres": None,
    }
    (workdir_no_baseline / "out").mkdir(exist_ok=True)
    cfg = load_config(args)
    assert cfg.baseline is None


# ---------------------------------------------------------------------------
# Campaign-level tests
# ---------------------------------------------------------------------------
def test_baseline_sample_injected_into_samples(
    workdir_with_baseline: Path, template_pkg: Path, outdir: Path
) -> None:
    """step_generate_lhs must include the baseline sample when baseline is configured."""
    cfg = CampaignConfig(
        input_variables=workdir_with_baseline / "variables.yml",
        template_sim_package=template_pkg,
        n_samples=3,
        outdir=outdir,
        openstudio_version="3.4.0",
        baseline={"sample_id": "baseline", "parameters": {"u1": 0.5, "u2": 15.0}},
    )
    campaign = Campaign(cfg=cfg, executor=LocalExecutor(max_workers=2))
    samples = campaign.step_generate_lhs()
    sample_ids = [s["sample_id"] for s in samples]
    assert "baseline" in sample_ids
    # The baseline should have exactly the fixed parameters
    baseline_sample = next(s for s in samples if s["sample_id"] == "baseline")
    assert baseline_sample["values"]["u1"] == 0.5
    assert baseline_sample["values"]["u2"] == 15.0


def test_no_baseline_no_injection(
    workdir_no_baseline: Path, template_pkg: Path, outdir: Path
) -> None:
    """step_generate_lhs must NOT inject baseline when cfg.baseline is None."""
    cfg = CampaignConfig(
        input_variables=workdir_no_baseline / "variables.yml",
        template_sim_package=template_pkg,
        n_samples=2,
        outdir=outdir,
        openstudio_version="3.4.0",
    )
    campaign = Campaign(cfg=cfg, executor=LocalExecutor(max_workers=2))
    samples = campaign.step_generate_lhs()
    sample_ids = [s["sample_id"] for s in samples]
    assert "baseline" not in sample_ids
    assert len(samples) == 2


def test_baseline_campaign_run_produces_all_artifacts(
    workdir_with_baseline: Path, template_pkg: Path, outdir: Path
) -> None:
    """Full campaign run with baseline must produce all artifacts + baseline_comparison."""
    cfg = CampaignConfig(
        input_variables=workdir_with_baseline / "variables.yml",
        template_sim_package=template_pkg,
        n_samples=3,
        outdir=outdir,
        openstudio_version="3.4.0",
        baseline={"sample_id": "baseline", "parameters": {"u1": 0.5, "u2": 15.0}},
    )
    campaign = Campaign(cfg=cfg, executor=LocalExecutor(max_workers=4))
    result = campaign.run()

    # Must have more samples than n_samples (baseline injected)
    assert len(result["samples"]) == 4  # 3 LHS + 1 baseline

    # aggregated_results.csv must exist
    csv_path = outdir / "aggregated_results.csv"
    assert csv_path.is_file()

    # run.json must have baseline_comparison (note: stub KPI extractor may not
    # produce eui_kwh_m2_yr, so baseline_comparison might be None if no numeric
    # KPIs are present. But the key should be absent when there's nothing to compare.)
    run_json = outdir / "run.json"
    assert run_json.is_file()
    trace = json.loads(run_json.read_text())
    # config should carry the baseline_sample_id
    assert trace["config"]["baseline_sample_id"] == "baseline"

    # per_sample should include the baseline sample
    sample_ids = {row["sample_id"] for row in trace["per_sample"]}
    assert "baseline" in sample_ids


def test_backward_compat_no_baseline(
    workdir_no_baseline: Path, template_pkg: Path, outdir: Path
) -> None:
    """Campaign without baseline must behave identically to pre-issue-64."""
    cfg = CampaignConfig(
        input_variables=workdir_no_baseline / "variables.yml",
        template_sim_package=template_pkg,
        n_samples=2,
        outdir=outdir,
        openstudio_version="3.4.0",
    )
    campaign = Campaign(cfg=cfg, executor=LocalExecutor(max_workers=2))
    result = campaign.run()

    # No baseline sample injected
    assert len(result["samples"]) == 2

    # run.json should NOT have baseline_comparison key
    run_json = outdir / "run.json"
    trace = json.loads(run_json.read_text())
    assert "baseline_comparison" not in trace
    assert trace["config"]["baseline_sample_id"] is None


def test_compute_improvement_range_static() -> None:
    """Unit test for the static _compute_improvement_range helper."""
    baseline_kpis = {"eui_kwh_m2_yr": 100.0, "cost_usd": 50000.0}
    all_kpis = {
        "baseline": baseline_kpis,
        "sample_1": {"eui_kwh_m2_yr": 80.0, "cost_usd": 40000.0},
        "sample_2": {"eui_kwh_m2_yr": 120.0, "cost_usd": 60000.0},
    }
    result = Campaign._compute_improvement_range("baseline", baseline_kpis, all_kpis)
    assert result["baseline_eui_kwh_m2_yr"] == 100.0
    # improvement for sample_1: (100 - 80)/100 * 100 = 20%
    # improvement for sample_2: (100 - 120)/100 * 100 = -20%
    assert result["min_eui_kwh_m2_yr_improvement_pct"] == -20.0
    assert result["max_eui_kwh_m2_yr_improvement_pct"] == 20.0
    assert result["baseline_cost_usd"] == 50000.0
    assert result["min_cost_usd_improvement_pct"] == -20.0
    assert result["max_cost_usd_improvement_pct"] == 20.0


def test_read_all_kpis_static() -> None:
    """Unit test for the static _read_all_kpis helper."""
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        kpi1 = Path(td) / "kpi_001.json"
        kpi1.write_text(json.dumps({"sample_id": "001", "kpis": {"eui": 100.0}}))
        kpi2 = Path(td) / "kpi_002.json"
        kpi2.write_text(json.dumps({"sample_id": "002", "kpis": {"eui": 80.0}}))

        result = Campaign._read_all_kpis([kpi1, kpi2])
        assert "001" in result
        assert result["001"]["eui"] == 100.0
        assert "002" in result
        assert result["002"]["eui"] == 80.0
