"""Full-Campaign real-``openstudio.cli`` E2E test exercising all 7 DAG steps.

This is the local/docker real-CLI profile from PRD §5.2 #6 (issue #939). Unlike
``tests/unit/test_run_openstudio_sim.py::TestRealOpenStudioE2E``, which only
exercises the ``run_openstudio_sim`` work function in isolation, this test drives
``Campaign.run()`` end-to-end with a *real* ``openstudio.cli`` through the full
7-step DAG::

    GENERATE_LHS_SAMPLES → PREFLIGHT_RUN_MODEL → APPLY_PARAMETERS →
    RUN_OPENSTUDIO_SIM → EXTRACT_KPIS → AGGREGATE_RESULTS → GENERATE_BASIC_PLOTS

Requirements (all must hold for the test to run, otherwise it skips):

  1. ``OSIMFLOW_RUN_REAL_OPENSTUDIO=1`` is set in the environment (the knob
     documented in AGENTS.md §8 gotcha #11).
  2. ``openstudio.cli`` (or ``openstudio``) is on ``PATH`` — i.e. the NREL
     OpenStudio CLI is installed (natively, or inside the ``nrel/openstudio``
     container).
  3. A real, simulation-capable example fixture is present in
     ``example_package/``. If the committed JSON placeholder is still in place,
     the test invokes ``scripts/fetch_example_fixture.py`` to materialise the
     real ``.osm`` + ``.epw``. If that download fails (e.g. no network), the
     test skips with a clear reason rather than erroring.

This test is intentionally **inert in normal CI** (no ``openstudio.cli`` on the
PR runner): it reports as skipped (``s``), never as an error. It is driven by
the nightly ``openstudio-cli-e2e`` workflow
(``.github/workflows/openstudio-cli-e2e.yml``), which installs the OpenStudio
CLI, fetches the real fixture, sets ``OSIMFLOW_RUN_REAL_OPENSTUDIO=1``, and runs
this module.

To run locally (requires a real OpenStudio install)::

    python scripts/fetch_example_fixture.py
    export OSIMFLOW_RUN_REAL_OPENSTUDIO=1
    .venv/bin/pytest tests/integration/test_real_openstudio_campaign.py -v --timeout=3600

Why a BYOS apply shim + empty ``variables.yml``?
    The default ``default_apply_parameters`` mutates ``model.osm`` via the
    OpenStudio *Python bindings* (``import openstudio``), which are not always
    installed alongside the CLI. To exercise the real **CLI** wiring
    (``openstudio.cli run -w workflow.osw``) without depending on the Python
    bindings, the test supplies a BYOS ``apply_parameters`` that invokes the CLI
    directly. Per the production design (issue #248), the CLI run during
    APPLY_PARAMETERS produces ``eplusout.sql`` in the package's ``run/``
    directory; the subsequent RUN_OPENSTUDIO_SIM step then reuses that output
    via ``_reuse_existing_simulation_output`` and copies it into the per-sample
    ``sim`` directory for KPI extraction. An empty ``variables`` list keeps LHS
    deterministic (3 identical seed-model runs) and bypasses the
    bindings-dependent pre-flight parameter mapping, while still exercising the
    LHS generation, cache, and fan-out machinery.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Skip gate: real CLI knob + openstudio.cli/openstudio on PATH.
# ---------------------------------------------------------------------------
_CLI_NAMES = ("openstudio.cli", "openstudio")


def _openstudio_cli_on_path() -> bool:
    """True if ``openstudio.cli`` or ``openstudio`` is resolvable on PATH."""
    return any(shutil.which(name) is not None for name in _CLI_NAMES)


_run_real = os.environ.get("OSIMFLOW_RUN_REAL_OPENSTUDIO") == "1"

pytestmark = pytest.mark.skipif(
    not (_run_real and _openstudio_cli_on_path()),
    reason=(
        "Set OSIMFLOW_RUN_REAL_OPENSTUDIO=1 and install openstudio.cli/openstudio "
        "on PATH to run the full-Campaign real-CLI E2E"
    ),
)

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_PACKAGE = REPO_ROOT / "example_package"
MODEL_OSM = EXAMPLE_PACKAGE / "model.osm"
FETCH_SCRIPT = REPO_ROOT / "scripts" / "fetch_example_fixture.py"

# The 7 DAG steps that must all appear in run.json on a cold run.
_REQUIRED_STEPS = (
    "GENERATE_LHS_SAMPLES",
    "PREFLIGHT_RUN_MODEL",
    "APPLY_PARAMETERS",
    "RUN_OPENSTUDIO_SIM",
    "EXTRACT_KPIS",
    "AGGREGATE_RESULTS",
    "GENERATE_BASIC_PLOTS",
)


def _is_real_osm(path: Path) -> bool:
    """True iff *path* is a real OpenStudio model (contains ``OS:Version``)."""
    if not path.is_file() or path.stat().st_size == 0:
        return False
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for chunk in iter(lambda: fh.read(65536), ""):
                if "OS:Version" in chunk:
                    return True
    except OSError:
        return False
    return False


def _ensure_real_fixture() -> Path:
    """Ensure a real ``.osm`` + ``.epw`` are present in ``example_package/``.

    If the committed JSON placeholder is still in place, invoke
    ``scripts/fetch_example_fixture.py`` to download the real fixture. Returns
    the path to the real ``model.osm``. Raises ``pytest.skip`` if the download
    fails (e.g. no network in the sandbox) so the test degrades gracefully.
    """
    if _is_real_osm(MODEL_OSM):
        return MODEL_OSM
    if not FETCH_SCRIPT.is_file():
        pytest.skip(
            f"scripts/fetch_example_fixture.py not found at {FETCH_SCRIPT}; "
            "cannot materialise a real OpenStudio fixture"
        )
    try:
        subprocess.run(  # noqa: S603 -- trusted in-tree script
            [sys.executable, str(FETCH_SCRIPT)],
            cwd=str(REPO_ROOT),
            check=True,
            capture_output=True,
            text=True,
            timeout=300,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        pytest.skip(
            f"failed to fetch the real OpenStudio fixture via "
            f"scripts/fetch_example_fixture.py: {exc}"
        )
    if not _is_real_osm(MODEL_OSM):
        pytest.skip("real fixture still absent after fetch attempt")
    return MODEL_OSM


# BYOS apply shim: invokes the real ``openstudio.cli run`` so the workflow is
# exercised against the real CLI (production path, issue #248) without depending
# on the OpenStudio Python bindings. Rendered to a file under tmp_path so the
# Campaign can load it via the standard BYOS discovery (custom_apply_script).
_BYOS_APPLY_TEMPLATE = '''\
"""BYOS apply shim for the real-openstudio-cli E2E (issue #939).

Invokes ``openstudio.cli run -w workflow.osw`` directly so the full measure +
EnergyPlus pipeline runs through the real CLI, producing ``eplusout.sql`` in the
package's ``run/`` directory. The campaign's RUN_OPENSTUDIO_SIM step then reuses
that output. This mirrors the production ``_apply_parameters_via_cli`` design
(issue #248) while avoiding a hard dependency on the OpenStudio Python bindings.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


def apply_parameters(sim_dir: Path, variables: dict) -> Path:  # noqa: ARG001
    workflow = sim_dir / "workflow.osw"
    if not workflow.is_file():
        raise FileNotFoundError(f"workflow.osw not found in {sim_dir}")
    cmd_name = "openstudio.cli" if shutil.which("openstudio.cli") else "openstudio"
    subprocess.run(  # noqa: S603
        [cmd_name, "run", "-w", str(workflow)],
        cwd=str(sim_dir),
        check=True,
    )
    return sim_dir
'''


def _build_real_template(tmp_path: Path) -> Path:
    """Build a real-CLI-capable template package under *tmp_path*.

    Copies ``example_package/`` (with the real ``.osm`` + ``.epw``) and rewrites
    ``workflow.osw`` to a minimal seed-only workflow with the weather file set,
    so ``openstudio.cli run`` simulates the seed model directly.
    """
    template = tmp_path / "template"
    shutil.copytree(EXAMPLE_PACKAGE, template)

    # Locate the fetched weather file name (gitignored, materialised by the
    # fetch script). Fall back to the canonical NREL Golden filename.
    epw_files = list(template.glob("*.epw"))
    weather_name = epw_files[0].name if epw_files else "USA_CO_Golden-NREL.724666_TMY3.epw"

    # Seed-only workflow: no measure steps, weather file set. This lets
    # openstudio.cli forward-translate the seed OSM and run EnergyPlus without
    # requiring any bundled measures.
    (template / "workflow.osw").write_text(
        json.dumps(
            {
                "seed_file": "model.osm",
                "weather_file": weather_name,
                "measure_paths": [],
                "steps": [],
            },
            indent=2,
        )
    )
    return template


def test_real_openstudio_campaign_runs_all_7_dag_steps(tmp_path: Path) -> None:
    """Drive ``Campaign.run()`` with a real ``openstudio.cli`` over 3 samples.

    Asserts (issue #939 acceptance criteria):
      * a real ``eplusout.sql`` (valid SQLite with EnergyPlus tables) is produced,
      * ``aggregated_results.csv`` has the ``sample_id`` header + 3 rows,
      * ``run.json`` records all 7 DAG steps with a non-HIT cache label (cold run),
      * per-sample ``stdout.log`` / ``stderr.log`` are populated, and
      * the 4 canonical output artifacts are present.
    """
    # Late imports keep the module importable on hosts without osimflow deps
    # installed (the skip gate already prevents execution).
    from osimflow import Campaign, CampaignConfig
    from osimflow.executors import LocalExecutor

    # --- prerequisites -----------------------------------------------------
    _ensure_real_fixture()
    assert _openstudio_cli_on_path(), "openstudio CLI disappeared from PATH mid-test"

    version = os.environ.get("OSIMFLOW_OPENSTUDIO_VERSION", "3.11.0")

    # --- hermetic fixtures -------------------------------------------------
    workdir = tmp_path / "work"
    workdir.mkdir()
    # Empty variables list: LHS still emits 3 samples (with empty value dicts),
    # which keeps the run deterministic and avoids the bindings-dependent
    # pre-flight parameter mapping while exercising every DAG step.
    (workdir / "variables.yml").write_text("algorithm: lhs\nvariables: []\n")

    template_pkg = _build_real_template(tmp_path)

    byos_script = workdir / "byos_apply.py"
    byos_script.write_text(_BYOS_APPLY_TEMPLATE)

    outdir = tmp_path / "out"
    outdir.mkdir()

    cfg = CampaignConfig(
        input_variables=workdir / "variables.yml",
        template_sim_package=template_pkg,
        n_samples=3,
        outdir=outdir,
        openstudio_version=version,
        archive_intermediates=False,
        custom_apply_script=byos_script,
        # Do NOT skip preflight: the real CLI must exercise PREFLIGHT_RUN_MODEL
        # (DAG step 2) against the real seed model.
        skip_preflight=False,
    )

    campaign = Campaign(cfg=cfg, executor=LocalExecutor(max_workers=3))
    result = campaign.run()

    # --- 4 canonical output artifacts -------------------------------------
    csv_path = outdir / "aggregated_results.csv"
    assert csv_path.is_file(), f"missing artifact: {csv_path}"
    csv_text = csv_path.read_text()
    assert csv_text.startswith("sample_id"), (
        f"aggregated_results.csv missing header; got: {csv_text[:200]!r}"
    )
    assert len(csv_text.strip().splitlines()) >= 3 + 1, (
        f"expected >= 3 sample rows in aggregated_results.csv; got:\n{csv_text}"
    )

    failed_path = outdir / "failed_simulations.csv"
    assert failed_path.is_file(), f"missing artifact: {failed_path}"
    assert failed_path.read_text().startswith("sample_id")

    kpi_files = list((outdir / "work" / "kpis").glob("kpi_*.json"))
    assert len(kpi_files) >= 3, f"expected >= 3 KPI JSONs, got {len(kpi_files)}"
    for kpi in kpi_files:
        data = json.loads(kpi.read_text())
        assert "sample_id" in data
        assert "kpis" in data

    plots_dir = outdir / "plots"
    assert plots_dir.is_dir(), f"missing plot directory: {plots_dir}"

    # --- run.json: all 7 DAG steps, cold run (cache not HIT) ---------------
    run_json = outdir / "run.json"
    assert run_json.is_file(), f"missing run.json: {run_json}"
    trace = json.loads(run_json.read_text())
    assert trace["schema_version"] == 1
    assert trace["config"]["executor"] == "local"
    assert trace["config"]["n_samples"] == 3

    steps = {s["step"]: s for s in trace["steps"]}
    for required in _REQUIRED_STEPS:
        assert required in steps, f"DAG step {required} missing from run.json steps"
        # On a cold run no step should be a cache HIT.
        cache_label = steps[required].get("cache")
        assert cache_label != "HIT", (
            f"step {required} recorded cache=HIT on a cold run; expected MISS/SKIPPED"
        )

    # --- per-sample status + log capture (issue #6) -----------------------
    per_sample = {row["sample_id"]: row for row in trace["per_sample"]}
    assert len(per_sample) == 3
    statuses = {row["status"] for row in trace["per_sample"]}
    assert statuses == {"ok"}, f"expected all samples ok, got statuses={statuses}"

    sim_root = outdir / "work" / "sim"
    real_sql_found = False
    for sid in per_sample:
        row = per_sample[sid]
        assert row["status"] == "ok"
        # Per-sample stdout/stderr logs (issue #6) must be populated.
        stdout_log = sim_root / sid / "stdout.log"
        stderr_log = sim_root / sid / "stderr.log"
        assert stdout_log.is_file(), f"missing stdout.log for sample {sid}"
        assert stderr_log.is_file(), f"missing stderr.log for sample {sid}"
        assert stdout_path_nonempty(stdout_log), (
            f"stdout.log for sample {sid} is empty — real CLI produced no output"
        )
        # Each sample must reference an eplusout.sql path.
        sql_ref = row.get("eplusout_sql")
        assert isinstance(sql_ref, str) and sql_ref, f"sample {sid} has no eplusout_sql in run.json"

    # --- real eplusout.sql (not the placeholder stub) ---------------------
    # At least one sample must have produced a genuine EnergyPlus SQLite
    # database. The CLI writes it into the package run/ dir and the campaign
    # copies it into the per-sample sim dir; search both to be robust.
    for sid in per_sample:
        sql_candidates = [
            sim_root / sid / "eplusout.sql",
            *_sql_in_tree(outdir / "work" / "apply" / sid),
        ]
        for candidate in sql_candidates:
            if candidate.is_file() and _is_real_energyplus_sql(candidate):
                real_sql_found = True
                break
        if real_sql_found:
            break

    assert real_sql_found, (
        "no real eplusout.sql (SQLite with EnergyPlus tables) was produced for "
        "any sample — the real openstudio.cli did not run or produced a placeholder"
    )

    # --- result dict contract ---------------------------------------------
    assert set(result) >= {"samples", "kpis", "aggregated", "plots", "elapsed_s", "run_json"}
    assert len(result["samples"]) == 3
    assert result["run_json"] == run_json


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def stdout_path_nonempty(path: Path) -> bool:
    """True if *path* exists and has non-whitespace content."""
    return path.is_file() and bool(path.read_text(encoding="utf-8", errors="replace").strip())


def _sql_in_tree(root: Path) -> list[Path]:
    """Return all ``eplusout.sql`` files under *root* (robust to nested run dirs)."""
    if not root.is_dir():
        return []
    return [p for p in root.rglob("eplusout.sql") if p.is_file()]


def _is_real_energyplus_sql(path: Path) -> bool:
    """True iff *path* is a valid SQLite db with at least one EnergyPlus table.

    Rejects the stub placeholder (``-- placeholder sql``) and any non-SQLite
    file. Checks for the canonical EnergyPlus tables (TabularDataWithStrings,
    ReportData, Errors) per issue #246.
    """
    text = path.read_text(encoding="utf-8", errors="replace")
    if not text or text.lstrip().startswith("--"):
        return False
    try:
        with sqlite3.connect(str(path)) as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name IN "
                "('TabularDataWithStrings', 'ReportData', 'Errors')"
            )
            tables = {row[0] for row in cur.fetchall()}
    except sqlite3.DatabaseError:
        return False
    return len(tables) > 0
