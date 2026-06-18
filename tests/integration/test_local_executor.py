"""End-to-end integration test: Campaign via ``LocalExecutor``.

Acceptance criterion (issue #11):

    test_local_executor.py: runs a 3-sample campaign against the
    example package on the ``LocalExecutor``, asserts all 4 output
    artifacts are produced and ``run.json`` ``summary`` is correct.

The four output artifacts are:

  * ``aggregated_results.csv``     — per-sample KPI summary
  * ``failed_simulations.csv``     — sample_id + error summary for failures
  * KPI JSONs (``work/kpis/kpi_<sid>.json``)
  * plot files / ``plots/`` directory

In addition, ``run.json`` is verified to carry the expected schema
(``summary``, per-step timings, per-sample status) so a future
regression in the Campaign's monitoring surface is caught here.

These tests are *integration* (end-to-end) and exercise the full DAG
via the public surface — no internal mocking. The stub ``bin/*.py``
scripts do the actual work; the assertion surface is the on-disk
artifact.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import yaml

from osimflow import Campaign, CampaignConfig
from osimflow.apply_params import _build_mappings, preflight_check
from osimflow.executors import LocalExecutor

# Mark as slow: runs a real 3-sample campaign (30-60s). Excluded by `make test-fast`.
pytestmark = pytest.mark.slow

# ---------------------------------------------------------------------------
# Fixtures: a copy of the example_package in tmp_path so the test does not
# pollute the canonical example. The variables.yml is also copied and
# rewritten in tmp_path to keep the test hermetic.
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_PKG = REPO_ROOT / "example_package"


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    """Clean per-test work directory."""
    wd = tmp_path / "work"
    wd.mkdir()
    (wd / "variables.yml").write_text(
        "algorithm: lhs\n"
        "variables:\n"
        "  - name: wwr\n"
        "    distribution: uniform\n"
        "    min: 0.2\n"
        "    max: 0.6\n"
        "    measure_argument: SetEnvelopePerformance.wwr\n"
    )
    return wd


@pytest.fixture
def template_pkg(workdir: Path) -> Path:
    """Copy of the project's example_package into tmp_path.

    The apply step mutates the template in place (per the
    `apply_params_to_model.py` contract), so we MUST copy rather than
    reference — otherwise the test would mutate the project's
    canonical example_package and pollute sibling tests.
    """
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
    """3-sample campaign config matching the issue's acceptance criterion."""
    return CampaignConfig(
        input_variables=workdir / "variables.yml",
        template_sim_package=template_pkg,
        n_samples=3,
        outdir=outdir,
        openstudio_version="3.11.0",
        archive_intermediates=False,
    )


@pytest.fixture
def campaign(cfg: CampaignConfig) -> Campaign:
    return Campaign(cfg=cfg, executor=LocalExecutor(max_workers=3))


# ---------------------------------------------------------------------------
# Test: 3-sample campaign via LocalExecutor produces all 4 output artifacts
# ---------------------------------------------------------------------------
def test_three_sample_campaign_via_local_executor_produces_all_artifacts(
    campaign: Campaign, outdir: Path, workdir: Path
) -> None:
    """A 3-sample campaign through the LocalExecutor must produce the four
    output artifacts listed in the issue, plus the per-campaign
    run.json monitoring trace. The KPIs must align with the per-sample
    status recorded in run.json, and the example_package's pre-flight
    check (PRD §1.4) must succeed for the canonical variables.yml.
    """
    # --- Pre-flight check: variables.yml must map to example_package --
    # The PRD §1.4 pre-flight check fails the apply step if any
    # declared variable is unmapped. We assert the mapping is
    # complete so a future regression in either variables.yml or
    # example_package is caught here rather than at the apply step.
    declared = {
        v["name"] for v in yaml.safe_load((workdir / "variables.yml").read_text())["variables"]
    }
    mappings = _build_mappings(workdir / "template")
    missing = declared - set(mappings.keys())
    assert not missing, (
        f"variables {missing} declared in variables.yml but missing from "
        f"example_package — pre-flight should have failed. "
        f"Declared: {declared}, available: {set(mappings.keys())}"
    )

    # --- Run the campaign ---------------------------------------------
    result = campaign.run()

    # --- 4 output artifacts -------------------------------------------
    # 1) aggregated_results.csv
    csv_path = outdir / "aggregated_results.csv"
    assert csv_path.is_file(), f"missing artifact: {csv_path}"
    csv_text = csv_path.read_text()
    assert csv_text.startswith("sample_id"), (
        f"aggregated_results.csv missing header; got: {csv_text[:200]!r}"
    )
    # One CSV row per sample (header + 3 data rows).
    assert len(csv_text.strip().splitlines()) == 3 + 1

    # 2) failed_simulations.csv (may have only the header when nothing failed)
    failed_path = outdir / "failed_simulations.csv"
    assert failed_path.is_file(), f"missing artifact: {failed_path}"
    assert failed_path.read_text().startswith("sample_id")

    # 3) KPI JSONs: one per sample, under work/kpis/
    kpi_files = list((outdir / "work" / "kpis").glob("kpi_*.json"))
    assert len(kpi_files) == 3, f"expected 3 KPI JSONs, got {len(kpi_files)}"
    for kpi in kpi_files:
        data = json.loads(kpi.read_text())
        assert "sample_id" in data
        assert "kpis" in data

    # 4) plot files: directory must exist; the stub generator may emit zero
    # files, but the directory is what the campaign's `step_generate_plots`
    # creates via `osimflow.work.generate_plots`.
    plots_dir = outdir / "plots"
    assert plots_dir.is_dir(), f"missing plot directory: {plots_dir}"

    # --- run.json monitoring trace ------------------------------------
    run_json = outdir / "run.json"
    assert run_json.is_file(), f"missing run.json: {run_json}"
    trace = json.loads(run_json.read_text())
    assert trace["schema_version"] == 1
    assert trace["config"]["executor"] == "local"
    assert trace["config"]["n_samples"] == 3
    assert trace["config"]["openstudio_version"] == "3.11.0"
    # `summary` is the high-level result. Some Campaign versions emit
    # a structured summary; we accept either shape (the structural
    # fields are what we depend on).
    assert "summary" in trace
    assert trace["summary"]["n_samples"] == 3
    assert trace["summary"]["n_succeeded"] == 3
    assert trace["summary"]["n_failed"] == 0
    # Every step we drive should be recorded.
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

    # --- Per-sample rows + KPI alignment ------------------------------
    # The kpi files returned in the result dict must match the per-
    # sample status recorded in run.json. A regression in either
    # side (the cache loop or the trace writer) is caught here.
    per_sample = {row["sample_id"]: row for row in trace["per_sample"]}
    assert len(per_sample) == 3
    assert len(result["kpis"]) == 3
    # Every sample is OK in the happy path.
    statuses = {row["status"] for row in trace["per_sample"]}
    assert statuses == {"ok"}, f"expected all-ok statuses, got {statuses}"
    # Every returned kpi file must reference a sample that completed.
    for kpi_path in result["kpis"]:
        data = json.loads(kpi_path.read_text())
        sid = data["sample_id"]
        assert sid in per_sample, f"kpi sample_id {sid!r} not in run.json per_sample"
        assert per_sample[sid]["status"] == "ok"
    # eplusout_sql must be a string (JSON-serializable), not a Path
    # object (regression for the "isinstance(x, str)" cast that
    # dropped Path values).
    for row in trace["per_sample"]:
        assert isinstance(row["eplusout_sql"], str)

    # --- result dict contract (the public surface used by callers) ---
    assert set(result) >= {"samples", "kpis", "aggregated", "plots", "elapsed_s", "run_json"}
    assert len(result["samples"]) == 3
    assert Path(result["aggregated"]["csv"]).is_file()
    assert Path(result["aggregated"]["failed"]).is_file()
    assert result["run_json"] == run_json
    assert isinstance(result["elapsed_s"], float)
    assert result["elapsed_s"] > 0.0


# ---------------------------------------------------------------------------
# Test: BYOS end-to-end via CampaignConfig.custom_apply_script (issue #36)
# ---------------------------------------------------------------------------
def test_byos_apply_script_consumed_by_campaign(
    workdir: Path, template_pkg: Path, outdir: Path
) -> None:
    """CampaignConfig.custom_apply_script must be consumed by the Campaign
    when no explicit apply_fn is passed. The BYOS script writes a sentinel
    file so we can verify it was actually called.

    This test verifies the single-canonical-path fix from issue #36:
    there is exactly one BYOS loader (osimflow.byos.load_user_function),
    and the Campaign falls back to it when apply_fn is None.
    """
    # Write a BYOS script with the canonical function name.
    byos_script = workdir / "custom_apply.py"
    byos_script.write_text(
        """\
import json
from pathlib import Path

def apply_parameters(template: Path, parameters: dict, sample_id: str, out: Path) -> Path:
    out_dir = out / sample_id
    out_dir.mkdir(parents=True, exist_ok=True)
    # Write a sentinel so the test can verify we ran.
    (out_dir / "byos_ran.txt").write_text(f"sid={sample_id} params={parameters}")
    # Also write a minimal model.osm for the sim stub.
    (out_dir / "model.osm").write_text(json.dumps({"attributes": {}}))
    return out_dir
"""
    )

    cfg = CampaignConfig(
        input_variables=workdir / "variables.yml",
        template_sim_package=template_pkg,
        n_samples=3,
        outdir=outdir,
        openstudio_version="3.11.0",
        archive_intermediates=False,
        custom_apply_script=byos_script,
    )
    # Campaign constructed WITHOUT apply_fn — it must load from cfg.
    campaign = Campaign(cfg=cfg, executor=LocalExecutor(max_workers=3))
    campaign.run()

    # The sentinel must exist in every sample's apply output.
    for sample_dir in outdir.glob("work/apply/*/*"):
        sentinel = sample_dir / "byos_ran.txt"
        assert sentinel.is_file(), f"BYOS sentinel missing in {sample_dir}"
        text = sentinel.read_text()
        assert "sid=" in text
        assert "params=" in text

    # The campaign still produces all expected artifacts.
    assert (outdir / "aggregated_results.csv").is_file()
    assert (outdir / "failed_simulations.csv").is_file()
    assert (outdir / "run.json").is_file()
    trace = json.loads((outdir / "run.json").read_text())
    # custom_apply_script should appear in the trace config summary.
    assert trace["config"]["custom_apply_script"] == str(byos_script)
