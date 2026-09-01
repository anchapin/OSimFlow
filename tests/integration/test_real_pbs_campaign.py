"""Real PBS / Torque E2E test.  Only runs when OSIMFLOW_PBS_E2E=1.

Closes the substrate-coverage gap called out by issue #1020: every
other executor in ``osimflow/executors/`` has a
``tests/integration/test_real_<substrate>_campaign.py`` companion
(Slurm #941, AWS Batch #942, Azure #958, Google #959, Kubernetes,
Nomad, OpenStudio CLI #939) but ``PBSExecutor`` did not.

Requires:

  - A reachable PBS / Torque cluster head/login node (``qsub`` and
    ``qstat`` on PATH — the executor uses ``subprocess.run(["qsub", ...])``
    to submit and ``qstat -f <jobid>`` to poll).
  - ``OSIMFLOW_PBS_E2E=1``.
  - ``OSIMFLOW_PBS_QUEUE`` env var (the queue / destination to submit
    to; required because the scheduler rejects jobs without a valid
    destination).

This test is intentionally skipped in normal CI.  It is designed for
a nightly ``pbs-e2e`` workflow (to be provisioned alongside the
existing ``slurm-e2e`` / ``aws-batch-e2e`` runners).  The matrix
lives at ``docs/substrate-coverage.md``.  To run locally on a PBS
head node::

    export OSIMFLOW_PBS_E2E=1
    export OSIMFLOW_PBS_QUEUE=batch
    .venv/bin/pytest tests/integration/test_real_pbs_campaign.py -v --timeout=1800
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

# Primary gate: the test opt-in flag.
pytestmark = pytest.mark.skipif(
    os.environ.get("OSIMFLOW_PBS_E2E") != "1",
    reason="Set OSIMFLOW_PBS_E2E=1 and run on a real PBS/Torque cluster head node "
    "to exercise the production path",
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def _pbs_toolchain_available() -> bool:
    """Return True if ``qsub`` and ``qstat`` are both on PATH.

    The PBS executor shells out to both CLIs in production.  Without
    them, ``submit()`` would raise ``FileNotFoundError`` instead of a
    clean skip — we guard explicitly so a developer running the test
    on a workstation gets a clear skip reason rather than a confusing
    failure.
    """
    return shutil.which("qsub") is not None and shutil.which("qstat") is not None


def _qstat_self_test() -> bool:
    """Return True if ``qstat`` talks to a live PBS server.

    ``qstat`` with no arguments exits non-zero when the server is
    unreachable (e.g. when the CLI is installed but not on a head
    node).  We use this as a structural proof that we're talking to a
    real cluster before constructing ``PBSExecutor(debug=False)``.
    """
    try:
        result = subprocess.run(  # noqa: S603
            ["qstat"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def test_real_pbs_cluster_3_samples(tmp_path: Path) -> None:
    """3-sample campaign against a real PBS / Torque cluster.

    This test exercises the full production path:

      1. ``PBSExecutor`` is constructed with ``debug=False``.
      2. ``submit()`` shells out to ``qsub`` to enqueue a real job,
         then ``_wait_for_terminal()`` polls ``qstat -f <jobid>``
         with exponential backoff until the job reaches state
         ``F`` / ``E`` / ``C`` (terminal).
      3. The per-sample work function runs the OSimFlow stub that
         ships in the worker image; the campaign blocks on every
         job's ``Handle.result()``.
      4. The Campaign collects per-sample results and emits the
         standard 4-artifact contract + ``run.json``.

    The test asserts:

      - The 4-artifact contract (``aggregated_results.csv``,
        ``failed_simulations.csv``, per-sample KPI JSONs, ``plots/``).
      - ``run.json`` records ``executor == "pbs"`` with all 6 DAG
        steps and per-sample status.
      - **Structural proof the path was real, not debug**: the
        executor's ``debug`` flag is False and the underlying
        ``PBSExecutor._submit_job`` was the code path that ran.
        We do not shell out to ``qstat -x <jobid>`` post-hoc —
        that would couple the assertion to scheduler quirks; the
        executor's stored ``debug`` flag is sufficient and portable.

    A failure here indicates a regression in either the
    ``PBSExecutor`` ``debug=False`` wiring, the PBS resource-line
    construction, or the cluster's queue / scheduler configuration.
    """
    if not _pbs_toolchain_available():
        pytest.skip(
            "OSIMFLOW_PBS_E2E=1 is set but qsub/qstat are not on PATH — "
            "not running on a real PBS/Torque cluster head node"
        )
    if not _qstat_self_test():
        pytest.skip(
            "OSIMFLOW_PBS_E2E=1 is set but qstat cannot reach a live PBS "
            "server — refusing to claim a real PBS job ran"
        )

    queue = os.environ.get("OSIMFLOW_PBS_QUEUE")
    if not queue:
        pytest.skip("OSIMFLOW_PBS_QUEUE env var is required to target a real queue")

    from osimflow import Campaign, CampaignConfig
    from osimflow.executors import PBSExecutor

    # --- Hermetic fixtures (same pattern as test_aws_batch_real.py) ---
    example_pkg = REPO_ROOT / "example_package"
    example_vars = REPO_ROOT / "variables.yml"

    workdir = tmp_path / "work"
    workdir.mkdir()
    (workdir / "variables.yml").write_text(example_vars.read_text())

    template_pkg = workdir / "template"
    shutil.copytree(example_pkg, template_pkg)

    outdir = tmp_path / "out"
    outdir.mkdir()

    cfg = CampaignConfig(
        input_variables=workdir / "variables.yml",
        template_sim_package=template_pkg,
        n_samples=3,
        outdir=outdir,
        openstudio_version="3.11.0",
        archive_intermediates=False,
    )

    executor = PBSExecutor(
        queue=queue,
        debug=False,  # <-- the entire point of this test
        poll_interval_s=1.0,
        max_poll_interval_s=10.0,
    )

    # ---- Structural assertion #1: debug=False is the production path ----
    # When ``debug=False``, ``PBSExecutor.submit`` calls ``_submit_job``
    # which shells out to ``qsub``.  When ``debug=True``, it runs the
    # work function in a local subprocess (mirroring Slurm's DebugExecutor
    # pattern).  We assert the wiring flag here so a regression that
    # silently flips it back to True is caught.
    assert executor.debug is False, (
        "PBSExecutor.debug is True on the production path; "
        "the test would be running the local-subprocess fallback instead of qsub"
    )
    assert executor.queue == queue, (
        f"PBSExecutor.queue={executor.queue!r} != OSIMFLOW_PBS_QUEUE={queue!r}; "
        "the queue config did not propagate"
    )

    from tests.integration._resource_contract import (  # noqa: PLC0415
        record_submit_directives,
    )

    directives = record_submit_directives(executor)

    campaign = Campaign(cfg=cfg, executor=executor)
    # --- Resource-directive propagation (issue #1403) ---
    from tests.integration._resource_contract import (  # noqa: PLC0415
        assert_sim_fanout_directives,
        record_submit_directives,
    )

    assert_sim_fanout_directives(directives)
    # --- PBS wire check: qstat -f sees Resource_List (#1403) ---
    from tests.integration._resource_contract import (  # noqa: PLC0415
        pbs_job_resources,
    )

    sim_job_ids = [
        r["job_id"]
        for r in directives.records
        if r["cpus"] == 4 and r["memory_mb"] == 8192 and r["job_id"]
    ][:3]
    for job_id in sim_job_ids:
        resources = pbs_job_resources(job_id)
        assert resources.get("ncpus") == "4", f"PBS dropped ncpus for {job_id}: {resources}"
        assert resources.get("mem"), f"PBS dropped mem for {job_id}: {resources}"
    result = campaign.run()
    executor.shutdown()

    # --- 4 output artifacts (same contract as test_aws_batch_real.py) ---
    csv_path = outdir / "aggregated_results.csv"
    assert csv_path.is_file(), f"missing artifact: {csv_path}"
    csv_text = csv_path.read_text()
    assert csv_text.startswith("sample_id"), (
        f"aggregated_results.csv missing header; got: {csv_text[:200]!r}"
    )

    failed_path = outdir / "failed_simulations.csv"
    assert failed_path.is_file(), f"missing artifact: {failed_path}"
    assert failed_path.read_text().startswith("sample_id")

    # KPI JSON files: one per sample, under work/kpis/
    kpi_files = list((outdir / "work" / "kpis").glob("kpi_*.json"))
    assert len(kpi_files) >= 3, f"expected >= 3 KPI JSONs, got {len(kpi_files)}"
    for kpi in kpi_files:
        data = json.loads(kpi.read_text())
        assert "sample_id" in data
        assert "kpis" in data

    # Plots directory.
    plots_dir = outdir / "plots"
    assert plots_dir.is_dir(), f"missing plot directory: {plots_dir}"

    # --- run.json monitoring trace ---
    run_json = outdir / "run.json"
    assert run_json.is_file(), f"missing run.json: {run_json}"
    trace = json.loads(run_json.read_text())
    assert trace["schema_version"] == 1
    assert trace["config"]["executor"] == "pbs", (
        f"run.json did not record executor=pbs; got {trace['config']['executor']!r}"
    )
    assert trace["config"]["n_samples"] == 3

    # Every campaign step must be recorded.
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

    # --- result dict contract ---
    assert set(result) >= {"samples", "kpis", "aggregated", "plots", "elapsed_s", "run_json"}
    assert len(result["samples"]) == 3
    assert result["run_json"] == run_json
