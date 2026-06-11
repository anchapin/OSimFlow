"""End-to-end integration test: Campaign via ``SlurmExecutor(debug=True)``.

Acceptance criterion (issue #11):

    test_slurm_executor_debug.py: runs a 3-sample campaign against
    ``SlurmExecutor(debug=True)``, asserts the same outputs. (The
    ``debug=True`` path uses ``submitit.DebugExecutor`` which runs
    locally — no real Slurm cluster needed in CI.)

The point of routing through the SlurmExecutor is to verify the full
submitit plumbing (AutoExecutor + update_parameters + per-submit
overrides + closure wiring) actually drives the same Campaign
orchestrator to the same outputs as the LocalExecutor. If the
``debug=True`` path silently dropped the work or short-circuited the
cache key, this test would fail.

A real-cluster production test is intentionally out of scope (the
``SlurmExecutor`` production issue covers that path).
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from osimflow import Campaign, CampaignConfig
from osimflow.executors import SlurmExecutor

# ---------------------------------------------------------------------------
# Fixtures — same shape as test_local_executor.py; deliberately local so
# the test is hermetic (no shared conftest state, no cache pollution
# between executor variants).
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_PKG = REPO_ROOT / "example_package"
EXAMPLE_VARS_YML = REPO_ROOT / "variables.yml"


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    wd = tmp_path / "work"
    wd.mkdir()
    (wd / "variables.yml").write_text(EXAMPLE_VARS_YML.read_text())
    return wd


@pytest.fixture
def template_pkg(workdir: Path) -> Path:
    pkg = workdir / "template"
    shutil.copytree(EXAMPLE_PKG, pkg)
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
        n_samples=3,
        outdir=outdir,
        openstudio_version="3.11.0",
        archive_intermediates=False,
    )


@pytest.fixture
def slurm_executor(tmp_path: Path) -> SlurmExecutor:
    """SlurmExecutor in debug mode (no real Slurm).

    The `OSIMFLOW_SLURM_LOGS` env var points submitit's debug log
    directory into tmp_path so the test does not pollute `/tmp`
    between runs.
    """
    slurm_logs = tmp_path / "slurm-debug-logs"
    slurm_logs.mkdir()
    prev = os.environ.get("OSIMFLOW_SLURM_LOGS")
    os.environ["OSIMFLOW_SLURM_LOGS"] = str(slurm_logs)
    try:
        # debug=True is the default; pass it explicitly so the
        # intent is visible at the call site.
        ex = SlurmExecutor(partition="short", cpus_per_task=2, mem_gb=4, time_h=1, debug=True)
    finally:
        if prev is not None:
            os.environ["OSIMFLOW_SLURM_LOGS"] = prev
    yield ex
    ex.shutdown()


# ---------------------------------------------------------------------------
# Test: 3-sample campaign via SlurmExecutor(debug=True) produces all 4 artifacts
# ---------------------------------------------------------------------------
def test_three_sample_campaign_via_slurm_debug_executor_produces_all_artifacts(
    cfg: CampaignConfig, outdir: Path, slurm_executor: SlurmExecutor
) -> None:
    """A 3-sample campaign through SlurmExecutor(debug=True) must
    produce the same four output artifacts as the LocalExecutor path,
    and the per-sample trace must confirm 3 simulations completed
    via the submitit debug path.

    The `debug=True` setting routes through `submitit.DebugExecutor`,
    which runs the work locally while still emitting the exact
    `sbatch` script that would have been submitted. The Campaign
    itself does not know it is being run on a "real" Slurm substrate
    — it submits to whatever the configured BaseExecutor accepts.
    """
    campaign = Campaign(cfg=cfg, executor=slurm_executor)
    result = campaign.run()

    # --- 4 output artifacts -------------------------------------------
    csv_path = outdir / "aggregated_results.csv"
    assert csv_path.is_file(), f"missing artifact: {csv_path}"
    csv_text = csv_path.read_text()
    assert csv_text.startswith("sample_id")
    assert len(csv_text.strip().splitlines()) == 3 + 1

    failed_path = outdir / "failed_simulations.csv"
    assert failed_path.is_file(), f"missing artifact: {failed_path}"
    assert failed_path.read_text().startswith("sample_id")

    kpi_files = list((outdir / "work" / "kpis").glob("kpi_*.json"))
    assert len(kpi_files) == 3, f"expected 3 KPI JSONs, got {len(kpi_files)}"
    for kpi in kpi_files:
        data = json.loads(kpi.read_text())
        assert "sample_id" in data
        assert "kpis" in data

    plots_dir = outdir / "plots"
    assert plots_dir.is_dir(), f"missing plot directory: {plots_dir}"

    # --- run.json monitoring trace ------------------------------------
    run_json = outdir / "run.json"
    assert run_json.is_file(), f"missing run.json: {run_json}"
    trace = json.loads(run_json.read_text())
    assert trace["schema_version"] == 1
    # The Campaign records the executor *name*; for SlurmExecutor
    # the name is "slurm" regardless of debug vs. real mode.
    assert trace["config"]["executor"] == "slurm", (
        f"expected executor name 'slurm' in run.json, got {trace['config']['executor']!r}"
    )
    assert trace["config"]["n_samples"] == 3
    assert trace["config"]["openstudio_version"] == "3.11.0"
    assert trace["summary"]["n_samples"] == 3
    assert trace["summary"]["n_succeeded"] == 3
    assert trace["summary"]["n_failed"] == 0

    # Per-sample status: every sample completed via the submitit
    # debug path.
    statuses = {row["status"] for row in trace["per_sample"]}
    assert statuses == {"ok"}, f"expected all-ok statuses, got {statuses}"

    # Step coverage: every Campaign step is recorded.
    step_names = {s["step"] for s in trace["steps"]}
    for required in (
        "GENERATE_LHS_SAMPLES",
        "APPLY_PARAMETERS",
        "RUN_OPENSTUDIO_SIM",
        "EXTRACT_KPIS",
        "AGGREGATE_RESULTS",
        "GENERATE_BASIC_PLOTS",
    ):
        assert required in step_names, f"step {required} missing from run.json"

    # --- The SlurmExecutor actually drove 3 fan-out tasks (one per
    # sample). The Campaign's per-sample trace is the ground truth:
    # 3 per-sample rows, 3 distinct sample_ids, 3 eplusout.sql
    # files. This avoids monkey-patching submitit's internals (which
    # would couple the test to submitit's private API).
    assert len(trace["per_sample"]) == 3
    kpi_sample_ids = sorted(json.loads(p.read_text())["sample_id"] for p in result["kpis"])
    assert len(kpi_sample_ids) == 3
    assert kpi_sample_ids[0] != kpi_sample_ids[1] != kpi_sample_ids[2], (
        f"expected 3 distinct sample_ids, got {kpi_sample_ids}"
    )
    sql_files = list((outdir / "work" / "sim").rglob("eplusout.sql"))
    assert len(sql_files) == 3, f"expected 3 eplusout.sql files, got {len(sql_files)}: {sql_files}"

    # --- result dict contract -----------------------------------------
    assert set(result) >= {"samples", "kpis", "aggregated", "plots", "elapsed_s", "run_json"}
    assert len(result["samples"]) == 3
    assert len(result["kpis"]) == 3
    assert Path(result["aggregated"]["csv"]).is_file()
    assert Path(result["aggregated"]["failed"]).is_file()
    assert result["run_json"] == run_json
