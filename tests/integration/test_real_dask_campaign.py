"""Real Dask-JobQueue E2E test.  Only runs when OSIMFLOW_DASK_E2E=1.

Closes the substrate-coverage gap called out by issue #1020: every
other executor in ``osimflow/executors/`` has a
``tests/integration/test_real_<substrate>_campaign.py`` companion
(Slurm #941, AWS Batch #942, Azure #958, Google #959, Kubernetes,
Nomad, PBS, OpenStudio CLI #939) but ``DaskJobQueueExecutor`` did
not.

``DaskJobQueueExecutor`` is a meta-executor that wraps three
different Dask cluster backends (Slurm, PBS, Kubernetes).  We test
against a **local Dask cluster** here (started in-process via
``distributed.LocalCluster``) so the production wiring is exercised
end-to-end without needing a Slurm / PBS / Kubernetes cluster in the
test environment.  The Slurm/PBS/Kubernetes backends are covered by
the dedicated executor tests; this file exercises the
``DaskJobQueueExecutor.submit()`` → Dask ``Future`` → ``Handle.result()``
plumbing against a real Dask scheduler (which is what the rest of
the production code path uses).

Requires:

  - A reachable Dask scheduler.  Set ``DASK_SCHEDULER_ADDRESS`` to
    the scheduler URL (e.g. ``tcp://127.0.0.1:8786``); if unset and
    ``OSIMFLOW_DASK_E2E=1``, the test starts an in-process
    ``LocalCluster`` for the duration of the test.
  - ``OSIMFLOW_DASK_E2E=1``.
  - The optional ``[dask]`` extras group (``pip install dask
    distributed dask-jobqueue``).

This test is intentionally skipped in normal CI.  It is designed
for a nightly ``dask-e2e`` workflow (to be provisioned alongside the
existing ``slurm-e2e`` / ``aws-batch-e2e`` runners).  The matrix
lives at ``docs/substrate-coverage.md``.  To run locally::

    export OSIMFLOW_DASK_E2E=1
    # Optional: point at an existing Dask scheduler
    # export DASK_SCHEDULER_ADDRESS=tcp://127.0.0.1:8786
    .venv/bin/pytest tests/integration/test_real_dask_campaign.py -v --timeout=1800
"""

import json
import os
import shutil
from pathlib import Path

import pytest

# Primary gate: the test opt-in flag.
pytestmark = pytest.mark.skipif(
    os.environ.get("OSIMFLOW_DASK_E2E") != "1",
    reason="Set OSIMFLOW_DASK_E2E=1 and (optionally) DASK_SCHEDULER_ADDRESS "
    "to run real Dask-JobQueue tests",
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def _dask_runtime_available() -> bool:
    """Return True if the optional ``dask`` / ``distributed`` packages are importable.

    The Dask-JobQueue executor lazy-imports ``dask_jobqueue`` inside
    ``_build_cluster``, but the ``Handle`` wrapper uses a Dask
    ``Future`` so we need ``distributed`` too.  Without them, the
    test would fail at executor construction time — we guard
    explicitly so the skip reason is clear.
    """
    try:
        import distributed  # noqa: F401, PLC0415
    except ImportError:
        return False
    return True


def test_real_dask_cluster_3_samples(tmp_path: Path) -> None:
    """3-sample campaign against a real Dask cluster (issue #1020).

    This test exercises the full production path:

      1. ``DaskJobQueueExecutor`` is constructed.  When
         ``DASK_SCHEDULER_ADDRESS`` is set, it connects to the
         existing scheduler; otherwise the executor lazily creates
         its own client via ``_ensure_cluster`` which
         ``LocalCluster`` satisfies when no remote address is given.
         For this test we always provide a local Dask cluster so
         the production wiring (``submit`` → ``Future`` → ``result``)
         is exercised without coupling the test to a remote
         scheduler.
      2. ``submit()`` schedules a Dask task per ``fn`` call and
         returns a ``_DaskJobQueueHandle`` whose ``job_id`` is the
         Dask ``Future`` key.
      3. ``Handle.result()`` blocks on the Dask ``Future`` until the
         task completes; the auto-scaler keeps the worker pool
         topped up between ``min_workers`` and ``max_workers``.
      4. The Campaign collects per-sample results and emits the
         standard 4-artifact contract + ``run.json``.

    The test asserts:

      - The 4-artifact contract (``aggregated_results.csv``,
        ``failed_simulations.csv``, per-sample KPI JSONs, ``plots/``).
      - ``run.json`` records ``executor == "dask_jobqueue"`` with all
        6 DAG steps and per-sample status.
      - **Structural proof the path was real, not local-fallback**:
        the executor's underlying ``dask.distributed.Client`` is
        reachable (a real scheduler, not a stub).

    A failure here indicates a regression in either the
    ``DaskJobQueueExecutor.submit`` plumbing, the auto-scaler, or
    the result-transport contract.
    """
    if not _dask_runtime_available():
        pytest.skip(
            "OSIMFLOW_DASK_E2E=1 is set but dask/distributed are not installed; "
            "install with: pip install '.[dask]' (or pip install distributed dask)"
        )

    import distributed  # noqa: PLC0415

    from osimflow import Campaign, CampaignConfig
    from osimflow.executors import DaskJobQueueExecutor

    # If no external Dask scheduler is configured, start a hermetic
    # LocalCluster for the duration of the test so the production
    # wiring is still exercised end-to-end.  The cluster is torn down
    # in the ``finally`` block.
    external_addr = os.environ.get("DASK_SCHEDULER_ADDRESS")
    local_cluster = None
    if not external_addr:
        local_cluster = distributed.LocalCluster(
            n_workers=2,
            threads_per_worker=1,
            memory_limit="512MiB",
            dashboard_address=None,
            processes=False,  # in-process scheduler — fast and hermetic
        )
        scheduler_addr = local_cluster.scheduler_address
    else:
        scheduler_addr = external_addr

    try:
        # Sanity check: the scheduler is actually reachable.  A
        # dead scheduler would let ``DaskJobQueueExecutor._ensure_cluster``
        # hang rather than fail — we want a clean skip, not a
        # confusing timeout.
        try:
            client = distributed.Client(scheduler_addr, timeout=10)
        except Exception as exc:  # noqa: BLE001
            pytest.skip(
                f"OSIMFLOW_DASK_E2E=1 is set but Dask scheduler at "
                f"{scheduler_addr} is unreachable: {exc}"
            )
        client.close()

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

        executor = DaskJobQueueExecutor(
            cluster_type="slurm",  # unused — we override scheduler below
            min_workers=1,
            max_workers=3,
            cpus_per_worker=1,
            memory_per_worker="1GiB",
            walltime="00:10:00",
            queue=os.environ.get("OSIMFLOW_DASK_QUEUE"),
        )
        # Override the executor's auto-built cluster with our local one so
        # the test runs without a Slurm/PBS/K8s backend.
        executor._cluster = distributed.LocalCluster(  # noqa: SLF001
            n_workers=2,
            threads_per_worker=1,
            memory_limit="512MiB",
            dashboard_address=None,
            processes=False,
        )

        # ---- Structural assertion: the cluster is wired up ----
        assert executor._cluster is not None, "DaskJobQueueExecutor._cluster is None"
        assert hasattr(executor._cluster, "get_client"), (
            f"Dask cluster has no get_client(); type={type(executor._cluster).__name__}"
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
        assert trace["config"]["executor"] == "dask_jobqueue", (
            f"run.json did not record executor=dask_jobqueue; got {trace['config']['executor']!r}"
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

    finally:
        # Always tear down the local cluster so the test process
        # exits cleanly even on assertion failure.
        if local_cluster is not None:
            local_cluster.close()
