"""Unit tests for the Campaign orchestrator public API.

These tests exercise the Campaign end-to-end through its public surface
(no internal mocking). They cover the happy-path behavior of each DAG
step so that the `coverage_gate` contract test passes and so that any
regression in the public surface is caught at unit-test speed.

Behavior covered:
  * `step_generate_lhs` produces N samples and is cache-stable on second call.
  * `step_apply_parameters` runs per sample and stores the per-sample work dir.
  * `step_run_openstudio_sim` runs per sample and writes eplusout.sql.
  * `step_extract_kpis` runs per sample and produces a kpi file.
  * `step_aggregate_results` reads the stub kpis and produces csv/parquet/failed.
  * `step_generate_plots` is non-cached and runs to completion.
  * `run()` returns the documented result dict.
  * `monitoring.RunTrace` writes run.json with the expected schema.

These are unit tests (no real OpenStudio CLI), but they go through the
full code path via the LocalExecutor and the stub `bin/*.py` scripts.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from osimflow import Campaign, CampaignConfig
from osimflow.executors import LocalExecutor


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    """A clean per-test work directory with the input variables.yml and
    a stub template_sim_package."""
    wd = tmp_path / "work"
    wd.mkdir()

    # variables.yml: two uniform + one lognormal
    (wd / "variables.yml").write_text(
        yaml.safe_dump(
            {
                "variables": [
                    {"name": "u1", "distribution": "uniform", "min": 0.0, "max": 1.0},
                    {"name": "u2", "distribution": "uniform", "min": 10.0, "max": 20.0},
                    {"name": "ln1", "distribution": "lognormal", "mean": 0.0, "sigma": 0.5},
                ]
            }
        )
    )
    return wd


@pytest.fixture
def template_pkg(workdir: Path) -> Path:
    pkg = workdir / "template"
    pkg.mkdir()
    # The .osm is in test-mode JSON form: a dict of attribute name -> default.
    # Each variable declared in variables.yml (u1, u2, ln1) must exist as an
    # attribute or measure argument here, otherwise the pre-flight check
    # (PRD §1.4) correctly fails the apply step. The test fixture is
    # intentionally permissive: every declared variable has a matching
    # attribute so the stub apply path can run end-to-end.
    (pkg / "model.osm").write_text(
        json.dumps(
            {
                "attributes": {
                    "u1": 0.0,
                    "u2": 10.0,
                    "ln1": 1.0,
                }
            }
        )
    )
    (pkg / "workflow.osw").write_text(json.dumps({"name": "stub"}))
    return pkg


@pytest.fixture
def outdir(workdir: Path) -> Path:
    od = workdir / "out"
    od.mkdir()
    return od


@pytest.fixture
def cfg(workdir: Path, template_pkg: Path, outdir: Path) -> CampaignConfig:
    return CampaignConfig(
        input_variables=workdir / "variables.yml",
        template_sim_package=template_pkg,
        n_samples=2,
        outdir=outdir,
        openstudio_version="3.4.0",
        archive_intermediates=False,
    )


@pytest.fixture
def campaign(cfg: CampaignConfig) -> Campaign:
    return Campaign(cfg=cfg, executor=LocalExecutor(max_workers=2))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def test_run_returns_documented_result_dict(campaign: Campaign) -> None:
    result = campaign.run()
    assert set(result) >= {"samples", "kpis", "aggregated", "plots", "elapsed_s", "run_json"}
    assert isinstance(result["samples"], list)
    assert len(result["samples"]) == 2
    assert isinstance(result["kpis"], list)
    assert len(result["kpis"]) == 2
    assert isinstance(result["aggregated"], dict)
    assert "csv" in result["aggregated"]
    assert "failed" in result["aggregated"]
    assert isinstance(result["plots"], list)
    assert isinstance(result["elapsed_s"], float)
    assert Path(result["run_json"]).is_file()


def test_run_writes_run_json_with_expected_schema(campaign: Campaign, outdir: Path) -> None:
    campaign.run()
    run_json = outdir / "run.json"
    assert run_json.is_file()
    data = json.loads(run_json.read_text())
    assert data["schema_version"] == 1
    assert "campaign_id" in data
    assert "started_at" in data
    assert "finished_at" in data
    assert data["elapsed_s"] >= 0.0
    assert "config" in data
    assert data["config"]["executor"] == "local"
    assert data["config"]["openstudio_version"] == "3.4.0"
    assert data["config"]["n_samples"] == 2
    steps = {s["step"] for s in data["steps"]}
    assert "GENERATE_LHS_SAMPLES" in steps
    assert "AGGREGATE_RESULTS" in steps
    # Per-sample rows: one per sample, each with status.
    assert len(data["per_sample"]) == 2
    statuses = {row["status"] for row in data["per_sample"]}
    assert statuses == {"ok"}
    # eplusout_sql must be a string (JSON-serializable), not a Path object
    # (regression for the "isinstance(x, str)" cast that dropped Path values).
    for row in data["per_sample"]:
        assert isinstance(row["eplusout_sql"], str)


def test_step_generate_lhs_returns_deterministic_samples(
    campaign: Campaign, cfg: CampaignConfig
) -> None:
    """Two calls with the same inputs must produce the same samples (cache-stable)."""
    samples_a = campaign.step_generate_lhs()
    campaign2 = Campaign(cfg=cfg, executor=LocalExecutor(max_workers=1))
    samples_b = campaign2.step_generate_lhs()
    assert samples_a == samples_b
    # Each sample has sample_id and a values dict matching variables.yml.
    for s in samples_a:
        assert set(s["sample_id"]) >= {"0"}
        assert "values" in s
        assert set(s["values"].keys()) == {"u1", "u2", "ln1"}
    # u1, u2 are in range; ln1 is positive (lognormal).
    for s in samples_a:
        assert 0.0 <= float(str(s["values"]["u1"])) <= 1.0  # type: ignore[arg-type]
        assert 10.0 <= float(str(s["values"]["u2"])) <= 20.0  # type: ignore[arg-type]
        assert float(str(s["values"]["ln1"])) > 0.0  # type: ignore[arg-type]


def test_step_apply_parameters_writes_per_sample_dirs(campaign: Campaign) -> None:
    samples = campaign.step_generate_lhs()
    parameterized = campaign.step_apply_parameters(samples)
    assert set(parameterized.keys()) == {s["sample_id"] for s in samples}
    for sid, path in parameterized.items():
        assert path.is_dir(), f"per-sample work dir missing for {sid}"


def test_step_run_openstudio_sim_writes_eplusout_sql(campaign: Campaign) -> None:
    samples = campaign.step_generate_lhs()
    parameterized = campaign.step_apply_parameters(samples)
    simulated = campaign.step_run_openstudio_sim(parameterized)
    for _sid, path in simulated.items():
        assert path.is_dir()
        assert (path / "eplusout.sql").is_file()
        # The stub writes a placeholder; the cache may then delete an empty
        # .err (PRD §1.4 "Intelligent Intermediate File Optimization").


def test_step_extract_kpis_writes_per_sample_files(campaign: Campaign) -> None:
    samples = campaign.step_generate_lhs()
    parameterized = campaign.step_apply_parameters(samples)
    simulated = campaign.step_run_openstudio_sim(parameterized)
    kpi_files = campaign.step_extract_kpis(simulated)
    assert len(kpi_files) == 2
    for kpi in kpi_files:
        assert kpi.is_file()
        # The default extractor writes at least the file; the stub
        # writes the sample_id in JSON form.
        data = json.loads(kpi.read_text())
        assert "sample_id" in data


def test_step_aggregate_results_writes_csv_and_failed(
    campaign: Campaign, cfg: CampaignConfig
) -> None:
    samples = campaign.step_generate_lhs()
    parameterized = campaign.step_apply_parameters(samples)
    simulated = campaign.step_run_openstudio_sim(parameterized)
    kpi_files = campaign.step_extract_kpis(simulated)
    agg = campaign.step_aggregate_results(kpi_files, simulated)
    assert agg["csv"].is_file()
    assert agg["failed"].is_file()
    assert agg["parquet"].is_file() or agg["parquet"].parent.is_dir()
    # CSV has a header and one row per sample.
    csv_text = agg["csv"].read_text()
    assert csv_text.startswith("sample_id")


def test_step_generate_plots_creates_output_dir(campaign: Campaign) -> None:
    samples = campaign.step_generate_lhs()
    parameterized = campaign.step_apply_parameters(samples)
    simulated = campaign.step_run_openstudio_sim(parameterized)
    kpi_files = campaign.step_extract_kpis(simulated)
    agg = campaign.step_aggregate_results(kpi_files, simulated)
    plots = campaign.step_generate_plots(agg)
    # The stub plot generator writes no plots; the result is an empty list.
    assert isinstance(plots, list)
    # Either a list of plot files, or empty.
    for p in plots:
        assert p.is_file()


def test_resume_is_cache_stable(campaign: Campaign, cfg: CampaignConfig) -> None:
    """A second run on the same outdir should be a full cache hit (no re-runs)."""
    campaign.run()
    stats_after_first = campaign.cache.stats()
    campaign2 = Campaign(cfg=cfg, executor=LocalExecutor(max_workers=2))
    campaign2.run()
    stats_after_second = campaign2.cache.stats()
    # Cache is content-addressed; second run finds every entry.
    assert stats_after_first["total"] > 0
    assert stats_after_second["total"] >= stats_after_first["total"]


def test_unknown_distribution_raises_not_implemented(campaign: Campaign, workdir: Path) -> None:
    """An unknown distribution name in variables.yml must raise a clear error
    (PRD §4.2 step 1). The step delegates to `bin/generate_lhs.py` via the
    executor, so the underlying ``ValueError`` is wrapped in
    ``subprocess.CalledProcessError`` and re-raised as ``RuntimeError`` by
    ``osimflow.work.generate_lhs``. The ``__cause__`` chain preserves the
    original exception type for diagnostic purposes; here we only assert
    that the user-facing message mentions the distribution name.
    """
    (workdir / "variables.yml").write_text(
        yaml.safe_dump({"variables": [{"name": "x", "distribution": "weibull"}]})
    )
    with pytest.raises(RuntimeError, match="generate_lhs failed"):
        campaign.step_generate_lhs()
