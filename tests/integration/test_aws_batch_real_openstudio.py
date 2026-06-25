"""Real-``openstudio.cli``-in-``nrel/openstudio``-container AWS Batch E2E.

This is the *real* production cloud path from PRD §5.2 #6 (issue #942). Unlike
``tests/integration/test_aws_batch_real.py``, whose docstring states "the work
function is the OSimFlow stub that ships in the container image," this test
submits jobs to a **real AWS Batch** compute environment whose Batch job
definition uses the ``nrel/openstudio:<openstudio_version>`` container image —
which ships the real ``openstudio.cli`` — and asserts a genuine EnergyPlus
``eplusout.sql`` (valid SQLite with EnergyPlus tables) is produced per sample.

What this test proves that the stub-in-container test cannot
----------------------------------------------------------------
A regression in any of the following would surface here and only here:

  * the ``nrel/openstudio`` container image (missing/wrong CLI, broken
    EnergyPlus, missing shared libs),
  * the task IAM role's S3 write permissions (results never land in shared
    storage),
  * the Batch result-retrieval wiring (per-sample outputs lost between the
    container and the orchestrator), or
  * the ``run_openstudio_sim`` work function's real-CLI branch
    (``openstudio.cli run -w workflow.osw``) when ``OSIMFLOW_STUB_SIM`` is
    unset.

Requirements (all must hold for the test to run, otherwise it skips)
--------------------------------------------------------------------
  1. ``OSIMFLOW_AWS_BATCH_E2E=1`` (the same gate as the stub-in-container test).
  2. ``OSIMFLOW_AWS_BATCH_REAL_OPENSTUDIO=1`` (a *distinct* gate so the nightly
     can opt into this heavier real-OS variant without affecting the cheaper
     stub-in-container smoke).
  3. ``OSIMFLOW_AWS_BATCH_QUEUE``, ``OSIMFLOW_AWS_BATCH_JOB_DEFINITION``, and
     ``OSIMFLOW_AWS_REGION`` env vars naming the real Batch infrastructure.
  4. The Batch job definition named by ``OSIMFLOW_AWS_BATCH_JOB_DEFINITION``
     must use a container image of ``nrel/openstudio:<openstudio_version>``
     (or an ECR-mirrored equivalent). The image must ship the real
     ``openstudio.cli``.
  5. ``OSIMFLOW_STUB_SIM`` must be **unset** inside the container so the work
     function's real-CLI branch runs (``openstudio.cli run``). The nightly
     job deliberately does not set it.
  6. A real, simulation-capable ``.osm`` + ``.epw`` fixture must be present in
     ``example_package/``. The test invokes
     ``scripts/fetch_example_fixture.py`` to materialise it if only the JSON
     placeholder is committed; if the download fails it skips gracefully.

This test is intentionally **inert in normal CI** (no AWS credentials on the
PR runner): it reports as skipped (``s``), never as an error. It is driven by
the ``aws-batch-real-openstudio-e2e`` job in the nightly
``aws-batch-e2e.yml`` workflow (issue #942), which authenticates via OIDC.

Cost is bounded: the campaign is capped at ``N_SAMPLES`` (2) samples.

To run locally (requires real AWS Batch access + a registered
``nrel/openstudio`` job definition)::

    python scripts/fetch_example_fixture.py
    export OSIMFLOW_AWS_BATCH_E2E=1
    export OSIMFLOW_AWS_BATCH_REAL_OPENSTUDIO=1
    export OSIMFLOW_AWS_BATCH_QUEUE=my-queue
    export OSIMFLOW_AWS_BATCH_JOB_DEFINITION=osimflow-openstudio-real
    export OSIMFLOW_AWS_REGION=us-east-1
    .venv/bin/pytest tests/integration/test_aws_batch_real_openstudio.py -v --timeout=3600
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
# Skip gate — the AWS Batch E2E gate + the distinct real-openstudio gate +
# the three Batch-infrastructure env vars.
# ---------------------------------------------------------------------------
_REQUIRED_ENV = (
    "OSIMFLOW_AWS_BATCH_E2E",
    "OSIMFLOW_AWS_BATCH_REAL_OPENSTUDIO",
    "OSIMFLOW_AWS_BATCH_QUEUE",
    "OSIMFLOW_AWS_BATCH_JOB_DEFINITION",
    "OSIMFLOW_AWS_REGION",
)

_MISSING = [v for v in _REQUIRED_ENV if os.environ.get(v) in (None, "")]

pytestmark = pytest.mark.skipif(
    bool(_MISSING),
    reason=(
        "Set OSIMFLOW_AWS_BATCH_E2E=1 + OSIMFLOW_AWS_BATCH_REAL_OPENSTUDIO=1 plus "
        "OSIMFLOW_AWS_BATCH_QUEUE, OSIMFLOW_AWS_BATCH_JOB_DEFINITION, and "
        "OSIMFLOW_AWS_REGION to run the real-openstudio AWS Batch E2E "
        f"(missing: {_MISSING})"
    ),
)

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_PACKAGE = REPO_ROOT / "example_package"
MODEL_OSM = EXAMPLE_PACKAGE / "model.osm"
FETCH_SCRIPT = REPO_ROOT / "scripts" / "fetch_example_fixture.py"

# Bounded to 2 samples to control real AWS spend (issue #942 acceptance:
# "weekly cadence bounds cost (<=2 samples)").
N_SAMPLES = 2


# ---------------------------------------------------------------------------
# Fixture helpers (mirror the proven #939 real-openstudio pattern).
# ---------------------------------------------------------------------------
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
    fails (e.g. no network) so the test degrades gracefully.
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
# Mirrors the validated shim from tests/integration/test_real_openstudio_campaign.py.
_BYOS_APPLY_TEMPLATE = '''\
"""BYOS apply shim for the real-openstudio AWS Batch E2E (issue #942).

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


# ---------------------------------------------------------------------------
# eplusout.sql validation helpers (issue #942 acceptance: real SQLite w/ E+ tables).
# ---------------------------------------------------------------------------
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


def _sql_in_tree(root: Path) -> list[Path]:
    """Return all ``eplusout.sql`` files under *root* (robust to nested run dirs)."""
    if not root.is_dir():
        return []
    return [p for p in root.rglob("eplusout.sql") if p.is_file()]


def test_real_openstudio_in_aws_batch_container(tmp_path: Path) -> None:
    """Run a 2-sample real-``openstudio.cli`` campaign against real AWS Batch.

    The Batch job definition's container image must be
    ``nrel/openstudio:<openstudio_version>`` (or an ECR mirror), which ships the
    real CLI. With ``OSIMFLOW_STUB_SIM`` unset inside the container, the work
    function invokes ``openstudio.cli run -w workflow.osw`` for real.

    Asserts (issue #942 acceptance criteria):

      * the 4 canonical output artifacts (``aggregated_results.csv``,
        ``failed_simulations.csv``, KPI JSONs, plots dir),
      * ``run.json`` records ``executor=aws_batch`` with ``RUN_OPENSTUDIO_SIM``
        present at a non-HIT cache label (cold run),
      * **at least one sample produced a real ``eplusout.sql``** (valid SQLite
        with EnergyPlus tables) — the structural proof that real OpenStudio ran
        in the container rather than the stub, and
      * the result-dict contract.
    """
    # Late imports keep the module importable on hosts without osimflow deps
    # installed (the skip gate already prevents execution).
    from osimflow import Campaign, CampaignConfig
    from osimflow.executors import AWSBatchExecutor

    # --- prerequisites -----------------------------------------------------
    _ensure_real_fixture()

    queue = os.environ["OSIMFLOW_AWS_BATCH_QUEUE"]
    job_def = os.environ["OSIMFLOW_AWS_BATCH_JOB_DEFINITION"]
    region = os.environ["OSIMFLOW_AWS_REGION"]
    version = os.environ.get("OSIMFLOW_OPENSTUDIO_VERSION", "3.11.0")

    # --- hermetic fixtures -------------------------------------------------
    workdir = tmp_path / "work"
    workdir.mkdir()
    # Empty variables list: LHS still emits N_SAMPLES samples (with empty value
    # dicts), which keeps the run deterministic and avoids the bindings-dependent
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
        n_samples=N_SAMPLES,
        outdir=outdir,
        openstudio_version=version,
        archive_intermediates=False,
        custom_apply_script=byos_script,
    )

    executor = AWSBatchExecutor(
        job_queue=queue,
        job_definition=job_def,
        region_name=region,
    )

    campaign = Campaign(cfg=cfg, executor=executor)
    result = campaign.run()
    executor.shutdown()

    # --- 4 canonical output artifacts -------------------------------------
    csv_path = outdir / "aggregated_results.csv"
    assert csv_path.is_file(), f"missing artifact: {csv_path}"
    csv_text = csv_path.read_text()
    assert csv_text.startswith("sample_id"), (
        f"aggregated_results.csv missing header; got: {csv_text[:200]!r}"
    )
    assert len(csv_text.strip().splitlines()) >= N_SAMPLES + 1, (
        f"expected >= {N_SAMPLES} sample rows in aggregated_results.csv; got:\n{csv_text}"
    )

    failed_path = outdir / "failed_simulations.csv"
    assert failed_path.is_file(), f"missing artifact: {failed_path}"
    assert failed_path.read_text().startswith("sample_id")

    # KPI JSON files: one per sample, under work/kpis/
    kpi_files = list((outdir / "work" / "kpis").glob("kpi_*.json"))
    assert len(kpi_files) >= N_SAMPLES, f"expected >= {N_SAMPLES} KPI JSONs, got {len(kpi_files)}"
    for kpi in kpi_files:
        data = json.loads(kpi.read_text())
        assert "sample_id" in data
        assert "kpis" in data

    # Plots directory.
    plots_dir = outdir / "plots"
    assert plots_dir.is_dir(), f"missing plot directory: {plots_dir}"

    # --- run.json: executor=aws_batch, RUN_OPENSTUDIO_SIM cold (cache != HIT) -
    run_json = outdir / "run.json"
    assert run_json.is_file(), f"missing run.json: {run_json}"
    trace = json.loads(run_json.read_text())
    assert trace["schema_version"] == 1
    assert trace["config"]["executor"] == "aws_batch"
    assert trace["config"]["n_samples"] == N_SAMPLES

    steps = {s["step"]: s for s in trace["steps"]}
    for required in (
        "GENERATE_LHS_SAMPLES",
        "APPLY_PARAMETERS",
        "RUN_OPENSTUDIO_SIM",
        "EXTRACT_KPIS",
        "AGGREGATE_RESULTS",
        "GENERATE_BASIC_PLOTS",
    ):
        assert required in steps, f"DAG step {required} missing from run.json steps"
    # On a cold run RUN_OPENSTUDIO_SIM must not be a cache HIT — a real Batch
    # job ran and produced real simulation output.
    sim_step = steps["RUN_OPENSTUDIO_SIM"]
    assert sim_step.get("cache") != "HIT", (
        "RUN_OPENSTUDIO_SIM recorded cache=HIT on a cold real-OS Batch run; "
        "expected MISS/SKIPPED — no real Batch sim job ran"
    )

    # --- REAL eplusout.sql (not the placeholder stub) ---------------------
    # At least one sample must have produced a genuine EnergyPlus SQLite
    # database. This is the structural proof that real openstudio.cli ran
    # inside the nrel/openstudio container (the stub only writes a placeholder
    # text file). Search the sim dir + the apply step's nested run dirs.
    per_sample = {row["sample_id"]: row for row in trace.get("per_sample", [])}
    assert len(per_sample) == N_SAMPLES, (
        f"run.json per_sample has {len(per_sample)} rows, expected {N_SAMPLES}"
    )

    sim_root = outdir / "work" / "sim"
    real_sql_found = False
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
        "any sample — the real openstudio.cli did not run inside the "
        "nrel/openstudio container, or its output was lost in transit from "
        "Batch. Check that the Batch job definition uses the "
        "nrel/openstudio:<version> image and that OSIMFLOW_STUB_SIM is unset."
    )

    # --- result dict contract ---------------------------------------------
    assert set(result) >= {"samples", "kpis", "aggregated", "plots", "elapsed_s", "run_json"}
    assert len(result["samples"]) == N_SAMPLES
    assert result["run_json"] == run_json
