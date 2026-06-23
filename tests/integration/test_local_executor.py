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

.. note::
    ``skip_preflight=True`` is set on all campaign configs because the
    stub ``OSIMFLOW_STUB_SIM=1`` mode is used in CI, which still
    exercises preflight but the preflight validation checks (weather
    files, geometry, measure entry points) are skipped in stub mode.
    When OpenStudio CLI is available on the host, preflight would run
    the real CLI against the template — which fails on the stub
    ``example_package/model.osm`` (a JSON stub, not a real OpenStudio
    model).  Always use ``skip_preflight=True`` in integration tests that
    use stub simulation.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import yaml

from osimflow import Campaign, CampaignConfig
from osimflow.apply_params import _build_mappings
from osimflow.executors import LocalExecutor

pytestmark = pytest.mark.slow

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_PKG = REPO_ROOT / "example_package"


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
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
        skip_preflight=True,
    )


@pytest.fixture
def campaign(cfg: CampaignConfig) -> Campaign:
    return Campaign(cfg=cfg, executor=LocalExecutor(max_workers=3))


def test_three_sample_campaign_via_local_executor_produces_all_artifacts(
    campaign: Campaign, outdir: Path, workdir: Path
) -> None:
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

    result = campaign.run()

    csv_path = outdir / "aggregated_results.csv"
    assert csv_path.is_file(), f"missing artifact: {csv_path}"
    csv_text = csv_path.read_text()
    assert csv_text.startswith("sample_id"), (
        f"aggregated_results.csv missing header; got: {csv_text[:200]!r}"
    )
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

    run_json = outdir / "run.json"
    assert run_json.is_file(), f"missing run.json: {run_json}"
    trace = json.loads(run_json.read_text())
    assert trace["schema_version"] == 1
    assert trace["config"]["executor"] == "local"
    assert trace["config"]["n_samples"] == 3
    assert trace["config"]["openstudio_version"] == "3.11.0"
    assert "summary" in trace
    assert trace["summary"]["n_samples"] == 3
    assert trace["summary"]["n_succeeded"] == 3
    assert trace["summary"]["n_failed"] == 0
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

    per_sample = {row["sample_id"]: row for row in trace["per_sample"]}
    assert len(per_sample) == 3
    assert len(result["kpis"]) == 3
    statuses = {row["status"] for row in trace["per_sample"]}
    assert statuses == {"ok"}, f"expected all-ok statuses, got {statuses}"
    for kpi_path in result["kpis"]:
        data = json.loads(kpi_path.read_text())
        sid = data["sample_id"]
        assert sid in per_sample, f"kpi sample_id {sid!r} not in run.json per_sample"
        assert per_sample[sid]["status"] == "ok"
    for row in trace["per_sample"]:
        assert isinstance(row["eplusout_sql"], str)

    assert set(result) >= {"samples", "kpis", "aggregated", "plots", "elapsed_s", "run_json"}
    assert len(result["samples"]) == 3
    assert Path(result["aggregated"]["csv"]).is_file()
    assert Path(result["aggregated"]["failed"]).is_file()
    assert result["run_json"] == run_json
    assert isinstance(result["elapsed_s"], float)
    assert result["elapsed_s"] > 0.0


def test_byos_apply_script_consumed_by_campaign(
    workdir: Path, template_pkg: Path, outdir: Path
) -> None:
    byos_script = workdir / "custom_apply.py"
    byos_script.write_text(
        """\
import json
from pathlib import Path

def apply_parameters(sim_dir: Path, variables: dict) -> Path:
    sim_dir.mkdir(parents=True, exist_ok=True)
    (sim_dir / "byos_ran.txt").write_text(f"params={variables}")
    (sim_dir / "model.osm").write_text(json.dumps({"attributes": {}}))
    return sim_dir
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
        skip_preflight=True,
    )
    campaign = Campaign(cfg=cfg, executor=LocalExecutor(max_workers=3))
    campaign.run()

    for sample_dir in outdir.glob("work/apply/*"):
        sentinel = sample_dir / "byos_ran.txt"
        assert sentinel.is_file(), f"BYOS sentinel missing in {sample_dir}"
        text = sentinel.read_text()
        assert "params=" in text

    assert (outdir / "aggregated_results.csv").is_file()
    assert (outdir / "failed_simulations.csv").is_file()
    assert (outdir / "run.json").is_file()
    trace = json.loads((outdir / "run.json").read_text())
    assert trace["config"]["custom_apply_script"] == str(byos_script)
