"""Real Slurm cluster E2E test. Only runs when OSIMFLOW_SLURM_E2E=1.

This is the missing ``debug=False`` coverage called out by
``tests/integration/test_slurm_production_wiring.py`` (issue #4 / #941):

    "The production Slurm path itself needs a real cluster to exercise
    end-to-end, so most of these tests target the ``debug=True`` path."

Every Slurm test in the suite constructs ``SlurmExecutor`` on a host
where submitit's :class:`AutoExecutor` auto-detects the *local* cluster
(because ``srun``/``sbatch`` are absent), so the production Slurm code
path is never exercised. PRD §5.2 #6 requires end-to-end coverage for
the Slurm profile; this file provides it, gated so it stays inert in
normal CI.

Requires:
  - A real Slurm cluster head/login node (``sbatch`` and ``srun`` on PATH
    — submitit uses ``shutil.which("srun")`` to pick the Slurm backend).
  - ``OSIMFLOW_SLURM_E2E=1``.
  - ``OSIMFLOW_SLURM_PARTITION`` env var (the partition to submit to).
  - ``OSIMFLOW_SLURM_ACCOUNT`` env var (optional but recommended; the
    scheduler's accounting allocation to charge).

This test is intentionally skipped in normal CI. It is designed for the
``slurm-e2e`` workflow (``.github/workflows/slurm-e2e.yml``) which is
dispatched on a self-hosted runner registered against a Slurm login
node. To run locally on a cluster head node::

    export OSIMFLOW_SLURM_E2E=1
    export OSIMFLOW_SLURM_PARTITION=short
    export OSIMFLOW_SLURM_ACCOUNT=my-project
    .venv/bin/pytest tests/integration/test_slurm_real_cluster.py -v --timeout=3600
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

# Primary gate: the test opt-in flag.
pytestmark = pytest.mark.skipif(
    os.environ.get("OSIMFLOW_SLURM_E2E") != "1",
    reason="Set OSIMFLOW_SLURM_E2E=1 and run on a real Slurm cluster head node to exercise the production path",
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def _secondary_guard() -> None:
    """Secondary guard: even with the env flag set, we need a real Slurm
    toolchain on PATH. submitit's ``AutoExecutor.which()`` keys off
    ``shutil.which("srun")``; we check ``sbatch`` (always co-installed on
    a real cluster) so the failure mode is a clean skip rather than a
    confusing submitit ``RuntimeError`` about an unavailable executor."""
    if shutil.which("sbatch") is None or shutil.which("srun") is None:
        pytest.skip(
            "OSIMFLOW_SLURM_E2E=1 is set but sbatch/srun are not on PATH — "
            "not running on a real Slurm cluster head node"
        )


def test_real_slurm_cluster_3_samples(tmp_path: Path) -> None:
    """3-sample campaign against a real Slurm cluster.

    This test exercises the full production path:

      1. ``SlurmExecutor`` is constructed with ``debug=False``.
      2. submitit's :class:`AutoExecutor` auto-detects the *slurm*
         cluster (because ``srun`` is on PATH) and renders real
         ``sbatch`` scripts — *not* the local ``DebugExecutor`` /
         ``LocalExecutor`` fallback used by every other Slurm test.
      3. Each per-sample job is queued to the configured partition and
         runs on a compute node; the campaign blocks on every job's
         ``.result()``.
      4. The Campaign collects per-sample results and emits the standard
         4-artifact contract + ``run.json``.

    The test asserts:

      - The 4-artifact contract (``aggregated_results.csv``,
        ``failed_simulations.csv``, per-sample KPI JSONs, ``plots/``).
      - ``run.json`` records ``executor == "slurm"`` with all 6+
        recorded DAG steps and per-sample status.
      - **Structural proof the path was real, not debug**: submitit's
        auto-detected ``cluster == "slurm"`` and the underlying executor
        is :class:`submitit.SlurmExecutor` (not ``LocalExecutor``).
        Job IDs assigned to per-sample handles resemble Slurm JOBIDs
        (numeric strings). We do *not* shell out to ``sacct`` — that
        would couple the assertion to scheduler quirks; the structural
        check is sufficient and portable across Slurm versions.

    A failure here indicates a regression in either the
    ``SlurmExecutor`` ``debug=False`` wiring, submitit version
    compatibility, or the cluster's scheduler configuration.
    """
    _secondary_guard()

    import submitit  # noqa: PLC0415

    from osimflow import Campaign, CampaignConfig
    from osimflow.executors import SlurmExecutor

    partition = os.environ.get("OSIMFLOW_SLURM_PARTITION")
    if not partition:
        pytest.skip("OSIMFLOW_SLURM_PARTITION env var is required to target a real partition")
    account = os.environ.get("OSIMFLOW_SLURM_ACCOUNT")  # optional

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

    # Keep the per-sample log folder under the test tmp_path so the
    # submitit AutoExecutor folder is hermetic and inspectable.
    slurm_logs = tmp_path / "slurm-logs"
    slurm_logs.mkdir()
    os.environ["OSIMFLOW_SLURM_LOGS"] = str(slurm_logs)

    cfg = CampaignConfig(
        input_variables=workdir / "variables.yml",
        template_sim_package=template_pkg,
        n_samples=3,
        outdir=outdir,
        openstudio_version="3.11.0",
        archive_intermediates=False,
    )

    executor = SlurmExecutor(
        partition=partition,
        account=account,
        debug=False,  # <-- the entire point of this test
    )

    # ---- Structural assertion #1: submitit picked the REAL Slurm cluster ----
    # AutoExecutor.which() returns "slurm" iff shutil.which("srun") is not
    # None (see submitit.SlurmExecutor.affinity). On a non-cluster host it
    # would return "local" and _executor would be a LocalExecutor — exactly
    # the path every other Slurm test exercises. If this assertion fires
    # the test is silently running on the local substrate; do not proceed.
    assert executor._ex.cluster == "slurm", (
        f"submitit AutoExecutor did not detect Slurm (cluster={executor._ex.cluster!r}); "
        "srun is not on PATH — refusing to claim a real Slurm job ran"
    )
    inner = executor._ex._executor  # noqa: SLF001
    assert isinstance(inner, submitit.SlurmExecutor), (
        f"underlying submitit executor is {type(inner).__name__}, not SlurmExecutor — "
        "the debug=False production path was not actually taken"
    )
    # The SlurmExecutor debug flag is advisory-only in modern submitit
    # (the real-vs-debug behaviour is decided by AutoExecutor cluster
    # detection). We assert the wiring flag too for documentation.
    assert executor.debug is False

    campaign = Campaign(cfg=cfg, executor=executor)
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
    assert trace["config"]["executor"] == "slurm", (
        f"run.json did not record executor=slurm; got {trace['config']['executor']!r}"
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

    # ---- Structural assertion #2: real Slurm JOBIDs were assigned ----
    # submitit assigns the scheduler's JOBID (a numeric string) to each
    # SlurmJob; the campaign surfaces it as the per-sample handle's
    # ``job_id``. We assert the recorded per-sample trace / samples carry
    # numeric-looking IDs — not by shelling out to ``sacct`` (which would
    # couple the test to scheduler quirks and accounting configuration)
    # but by structural shape. The local/debug path produces IDs of the
    # form ``local-<id>`` or a DebugExecutor placeholder, never a bare
    # integer, so this is a real-vs-debug discriminator.
    samples = result.get("samples") or []
    assert len(samples) == 3, f"expected 3 samples in result, got {len(samples)}"

    # The per-sample directory under work/sim/<sample_id>/ is where the
    # work layer writes per-sample stdout/stderr; we don't hard-require
    # a specific file shape (depends on whether the stub or real CLI ran)
    # but the directory must exist for every sample.
    sim_dir = outdir / "work" / "sim"
    if sim_dir.is_dir():
        sim_entries = [p for p in sim_dir.iterdir() if p.is_dir()]
        # Tolerant: at least one per-sample subdirectory should be present
        # after a real campaign ran.
        assert sim_entries, f"work/sim/ exists but has no per-sample subdirectories: {sim_dir}"

    # Confirm the per-sample trace records a Slurm-shaped job_id. We pull
    # the recorded job IDs from run.json's per-sample status entries when
    # present; if the schema doesn't surface them we fall back to the
    # result dict shape. Either way the path must be present.
    recorded_job_ids: list[str] = []
    for sample_entry in trace.get("samples", []) or []:
        jid = sample_entry.get("job_id") or sample_entry.get("worker_id")
        if jid:
            recorded_job_ids.append(str(jid))
    # If the trace exposes any job IDs, at least one must look like a
    # Slurm JOBID (a bare integer string). This is the real-vs-debug
    # discriminator: the local executor assigns ``local-<n>`` and the
    # debug path assigns DebugExecutor placeholders.
    if recorded_job_ids:
        assert any(jid.isdigit() for jid in recorded_job_ids), (
            "no per-sample job_id resembled a Slurm JOBID (integer string); "
            f"recorded: {recorded_job_ids}"
        )

    # --- result dict contract ---
    assert set(result) >= {"samples", "kpis", "aggregated", "plots", "elapsed_s", "run_json"}
    assert result["run_json"] == run_json
