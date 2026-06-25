"""Real Google Cloud Batch E2E test.  Only runs when OSIMFLOW_GOOGLE_BATCH_E2E=1.

Requires:
  - Google Cloud credentials (Workload Identity Federation via
    ``google-github-actions/auth`` in CI, or Application Default
    Credentials via ``gcloud auth application-default login`` locally)
  - OSIMFLOW_GOOGLE_BATCH_PROJECT_ID env var (GCP project id)
  - OSIMFLOW_GOOGLE_BATCH_REGION env var (e.g. us-central1)
  - OSIMFLOW_GOOGLE_BATCH_SERVICE_ACCOUNT env var (Batch service account email)

This test is intentionally skipped in normal CI.  It is designed for the
nightly ``google-batch-e2e`` workflow
(``.github/workflows/google-batch-e2e.yml``) which authenticates via
Workload Identity Federation and runs against a real Google Cloud Batch
environment.  To run locally::

    export OSIMFLOW_GOOGLE_BATCH_E2E=1
    export OSIMFLOW_GOOGLE_BATCH_PROJECT_ID=my-project
    export OSIMFLOW_GOOGLE_BATCH_REGION=us-central1
    export OSIMFLOW_GOOGLE_BATCH_SERVICE_ACCOUNT=batch@my-project.iam.gserviceaccount.com
    .venv/bin/pytest tests/integration/test_google_batch_real.py -v --timeout=1800
"""

import json
import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("OSIMFLOW_GOOGLE_BATCH_E2E") != "1",
    reason="Set OSIMFLOW_GOOGLE_BATCH_E2E=1 and configure Google Cloud vars "
    "to run real Google Batch tests",
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_real_google_batch_3_samples(tmp_path: Path) -> None:
    """3-sample campaign against real Google Cloud Batch.

    This test exercises the full production path:

      1. ``GoogleBatchExecutor`` submits real Cloud Batch jobs via the
         ``google-cloud-batch`` SDK.
      2. Each job runs inside a container on the Cloud Batch compute
         environment (the work function is the OSimFlow stub that
         ships in the container image).
      3. The executor polls ``get_job`` until SUCCEEDED.
      4. The Campaign collects per-sample results from shared storage.

    The test asserts the same 4-artifact contract as the local executor
    test (``test_local_executor.py``), plus the per-campaign ``run.json``
    monitoring trace.  A failure here indicates a regression in either
    the executor wiring, the container image, or the Cloud Batch
    infrastructure.
    """
    import shutil

    from osimflow import Campaign, CampaignConfig
    from osimflow.executors import GoogleBatchExecutor

    project_id = os.environ["OSIMFLOW_GOOGLE_BATCH_PROJECT_ID"]
    region = os.environ["OSIMFLOW_GOOGLE_BATCH_REGION"]
    service_account = os.environ["OSIMFLOW_GOOGLE_BATCH_SERVICE_ACCOUNT"]

    # Set up hermetic test fixtures (same pattern as other executor tests).
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

    executor = GoogleBatchExecutor(
        project_id=project_id,
        region=region,
        batch_service_account=service_account,
    )

    campaign = Campaign(cfg=cfg, executor=executor)
    result = campaign.run()
    executor.shutdown()

    # --- 4 output artifacts ---
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
    assert trace["config"]["executor"] == "google_batch"
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
