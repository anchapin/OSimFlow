"""Real Kubernetes E2E test.  Only runs when OSIMFLOW_KUBERNETES_E2E=1.

Requires:
  - A reachable Kubernetes cluster (managed EKS/GKE/AKS, self-hosted, or a
    local ``kind``/``minikube`` cluster).
  - A kubeconfig readable via the ``KUBECONFIG`` env var or the default
    ``~/.kube/config`` path. ``KubernetesExecutor`` sources credentials from
    ``config.load_kube_config()`` (falling back to in-cluster config) and does
    **not** accept explicit credentials.
  - OSIMFLOW_KUBERNETES_NAMESPACE env var (the namespace to submit Jobs into;
    defaults to ``default`` if unset).

This test is intentionally skipped in normal CI.  It is designed for the
nightly ``kubernetes-e2e`` workflow
(``.github/workflows/kubernetes-e2e.yml``) which authenticates by writing a
kubeconfig from the ``KUBECONFIG`` repository secret to ``~/.kube/config`` on
the runner and runs against a real Kubernetes cluster.  To run locally::

    export OSIMFLOW_KUBERNETES_E2E=1
    export OSIMFLOW_KUBERNETES_NAMESPACE=default
    export KUBECONFIG=~/.kube/config   # or rely on the default path
    .venv/bin/pytest tests/integration/test_kubernetes_executor_real.py -v --timeout=1800
"""

import json
import os
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _kubeconfig_reachable() -> bool:
    """Return True if a kubeconfig is available to the Kubernetes client.

    ``KubernetesExecutor._get_client`` tries ``config.load_kube_config()``
    (which honours ``KUBECONFIG`` or ``~/.kube/config``) and then falls back to
    ``config.load_incluster_config()``.  We cannot cheaply prove the in-cluster
    path here without importing the SDK, so the guard only checks the
    kubeconfig locations — the common case for runner/developer machines.
    """
    kubeconfig = os.environ.get("KUBECONFIG")
    if kubeconfig:
        # KUBECONFIG may be a colon-separated list (Kubernetes convention).
        first = kubeconfig.split(os.pathsep)[0]
        if first and Path(first).exists():
            return True
    if (Path.home() / ".kube" / "config").exists():
        return True
    return False


pytestmark = pytest.mark.skipif(
    os.environ.get("OSIMFLOW_KUBERNETES_E2E") != "1" or not _kubeconfig_reachable(),
    reason="Set OSIMFLOW_KUBERNETES_E2E=1 and provide kubeconfig "
    "(KUBECONFIG or ~/.kube/config) to run real Kubernetes tests",
)


def test_real_kubernetes_3_samples(tmp_path: Path) -> None:
    """3-sample campaign against a real Kubernetes cluster.

    This test exercises the full production path:

      1. ``KubernetesExecutor`` submits real ``batch/v1`` Jobs via the
         ``kubernetes`` Python client (one Job per sample).
      2. Each Job maps the resource directives (``cpus``/``memory_mb``) to
         ``V1ResourceRequirements`` requests+limits and ``time_min`` to
         ``active_deadline_seconds``.
      3. The executor polls ``list_namespaced_pod`` until the pod reaches a
         terminal phase (``Succeeded``/``Failed``), tolerating scheduling
         latency via exponential backoff.
      4. The Campaign collects per-sample results from shared storage.

    The test asserts the same 4-artifact contract as the local executor
    test (``test_local_executor.py``), plus the per-campaign ``run.json``
    monitoring trace.  A failure here indicates a regression in either the
    executor wiring, the container image, or the cluster configuration.
    """
    import shutil

    from osimflow import Campaign, CampaignConfig
    from osimflow.executors import KubernetesExecutor

    namespace = os.environ.get("OSIMFLOW_KUBERNETES_NAMESPACE", "default")

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

    # Tighter polling than the defaults (5s/60s) so the test stays tolerant of
    # pod-scheduling latency on busy/shared clusters without dragging the run.
    executor = KubernetesExecutor(
        namespace=namespace,
        poll_interval_s=2.0,
        max_poll_interval_s=30.0,
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
    assert trace["config"]["executor"] == "kubernetes"
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
