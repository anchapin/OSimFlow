"""Real Azure Batch E2E test.  Only runs when OSIMFLOW_AZURE_BATCH_E2E=1.

Requires:
  - Azure credentials (OIDC via ``azure/login`` in CI, or
    ``DefaultAzureCredential`` locally via ``az login`` /
    AZURE_TENANT_ID / AZURE_CLIENT_ID / AZURE_CLIENT_SECRET env vars)
  - OSIMFLOW_AZURE_BATCH_ACCOUNT_NAME env var (Batch account name)
  - OSIMFLOW_AZURE_BATCH_ACCOUNT_URL env var (e.g. https://<acct>.eastus.batch.azure.com)
  - OSIMFLOW_AZURE_BATCH_POOL_ID env var (Batch pool id with the
    ``nrel/openstudio`` container image available)
  - OSIMFLOW_AZURE_BATCH_LOCATION env var (Azure region, e.g. eastus)

This test is intentionally skipped in normal CI.  It is designed for the
nightly ``azure-batch-e2e`` workflow
(``.github/workflows/azure-batch-e2e.yml``) which authenticates via
Azure OIDC (``azure/login``) and runs against a real Azure Batch pool.
To run locally::

    export OSIMFLOW_AZURE_BATCH_E2E=1
    export OSIMFLOW_AZURE_BATCH_ACCOUNT_NAME=mybatchacct
    export OSIMFLOW_AZURE_BATCH_ACCOUNT_URL=https://mybatchacct.eastus.batch.azure.com
    export OSIMFLOW_AZURE_BATCH_POOL_ID=osimflow-pool
    export OSIMFLOW_AZURE_BATCH_LOCATION=eastus
    az login
    .venv/bin/pytest tests/integration/test_azure_batch_real.py -v --timeout=1800
"""

import json
import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("OSIMFLOW_AZURE_BATCH_E2E") != "1",
    reason="Set OSIMFLOW_AZURE_BATCH_E2E=1 and configure Azure Batch vars "
    "to run real Azure Batch tests",
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_real_azure_batch_3_samples(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """3-sample campaign against real Azure Batch.

    This test exercises the full production path:

      1. ``AzureBatchExecutor`` submits real Batch tasks via the
         ``azure-batch`` SDK.
      2. Each task runs inside a container on the Batch pool (the work
         function is the OSimFlow stub that ships in the container image).
      3. The executor polls ``job.get`` until the task reaches a terminal
         state.
      4. The Campaign collects per-sample results from shared storage.

    The test asserts the same 4-artifact contract as the local executor
    test (``test_local_executor.py``), plus the per-campaign ``run.json``
    monitoring trace.  A failure here indicates a regression in either
    the executor wiring, the container image, or the Batch pool
    configuration.
    """
    import shutil

    from osimflow import Campaign, CampaignConfig
    from osimflow.executors import AzureBatchExecutor
    from osimflow.task_payload_hmac import (  # noqa: PLC0415
        TASK_PAYLOAD_SECRET_ENV,
        TASK_PAYLOAD_SIG_ENV,
        verify_task_payload,
    )

    account_name = os.environ["OSIMFLOW_AZURE_BATCH_ACCOUNT_NAME"]
    account_url = os.environ["OSIMFLOW_AZURE_BATCH_ACCOUNT_URL"]
    pool_id = os.environ["OSIMFLOW_AZURE_BATCH_POOL_ID"]
    location = os.environ["OSIMFLOW_AZURE_BATCH_LOCATION"]

    # Issue #1453: configure the HMAC shared secret so the executor signs
    # OSIMFLOW_TASK_PAYLOAD and the remote runner verifies (fail-closed)
    # before decoding/executing.
    secret = "osimflow-azure-e2e-task-payload-secret"
    monkeypatch.setenv(TASK_PAYLOAD_SECRET_ENV, secret)

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

    executor = AzureBatchExecutor(
        account_name=account_name,
        account_url=account_url,
        pool_id=pool_id,
        location=location,
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

    # --- HMAC signature propagation (issue #1453) -------------------------
    # Re-read the real Batch task via the azure-batch SDK (the same
    # probe style as ``tests/integration/_resource_contract.py``) and
    # assert the submitted task carries payload + signature + secret,
    # and that the signature verifies against the configured secret.
    # A successful task execution is the remote-runner verification
    # proof: with the shared secret configured, ``osimflow.remote_runner``
    # exits non-zero on a missing/tampered signature, which fails the
    # task.
    sim_job_ids_hmac = [
        r["job_id"]
        for r in directives.records
        if r["cpus"] == 4 and r["memory_mb"] == 8192 and r["job_id"]
    ][:3]
    assert len(sim_job_ids_hmac) >= 3, (
        f"expected >= 3 sim job ids for the HMAC wire check, got {directives.records!r}"
    )
    batch_client = executor._client  # noqa: SLF001 — populated during campaign.run()
    for job_id in sim_job_ids_hmac:
        task = batch_client.task.get(account_name, job_id, job_id)
        env = {e.name: e.value for e in (task.environment_settings or [])}
        assert env.get("OSIMFLOW_TASK_PAYLOAD"), (
            f"Azure Batch task {job_id} missing OSIMFLOW_TASK_PAYLOAD env var"
        )
        assert env.get(TASK_PAYLOAD_SIG_ENV), (
            f"Azure Batch task {job_id} missing {TASK_PAYLOAD_SIG_ENV} env var "
            "(the HMAC signature did not propagate to the submitted spec)"
        )
        assert env.get(TASK_PAYLOAD_SECRET_ENV) == secret, (
            f"Azure Batch task {job_id} missing/mismatched {TASK_PAYLOAD_SECRET_ENV} "
            f"env var: {env.get(TASK_PAYLOAD_SECRET_ENV)!r}"
        )
        assert verify_task_payload(
            env["OSIMFLOW_TASK_PAYLOAD"],
            env.get(TASK_PAYLOAD_SIG_ENV),
            env[TASK_PAYLOAD_SECRET_ENV],
        ), (
            f"Azure Batch task {job_id}: {TASK_PAYLOAD_SIG_ENV} does not verify "
            f"against {TASK_PAYLOAD_SECRET_ENV} for the submitted payload"
        )
        assert task.state == "completed", (
            f"Azure Batch task {job_id} did not complete: {task.state!r}"
        )
        assert task.execution_info is not None and task.execution_info.result == "success", (
            f"Azure Batch task {job_id} did not succeed; the remote runner may "
            "have failed HMAC verification (execution_info: "
            f"{task.execution_info!r})"
        )

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
    assert trace["config"]["executor"] == "azure_batch"
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
