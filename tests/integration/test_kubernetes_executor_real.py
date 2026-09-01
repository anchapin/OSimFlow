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

Since issue #996 each Job runs the ephemeral-runner pattern: the container
command is ``python -m osimflow.remote_runner`` (or an explicit
``remote_command`` override), the task payload travels in the
``OSIMFLOW_TASK_PAYLOAD`` env var, and result-transport configuration in the
``OSIMFLOW_RESULT_*`` env vars. Consequently the **full-campaign test below
requires worker images that ship the ``osimflow`` package** (both the Python
image used for apply/KPI/aggregate/plots steps — override with
``OSIMFLOW_PYTHON_CONTAINER_IMAGE`` — and the ``nrel/openstudio`` image used
for sim steps; see ``docs/kubernetes-deployment.md``). The dedicated
remote-command tests only need the public ``python:3.12-slim`` image and run
on any cluster.

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
from typing import Any

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


def _list_osimflow_jobs(namespace: str) -> list[Any]:
    """Return the OSimFlow Job manifests currently in *namespace*."""
    from kubernetes import client as k8s_client

    batch = k8s_client.BatchV1Api()
    jobs = batch.list_namespaced_job(namespace=namespace)
    return [job for job in jobs.items if job.metadata.name.startswith("osimflow-")]


def test_real_kubernetes_job_executes_remote_command() -> None:
    """A Job must execute real work driven by the executor contract (#996).

    Uses the public ``python:3.12-slim`` image plus a ``remote_command``
    override to prove end-to-end that:

      1. the command override is honored (``/bin/sh -c <cmd>``),
      2. the ``OSIMFLOW_TASK_PAYLOAD`` env var reaches the container and
         decodes to the Nomad-compatible task payload,
      3. a zero exit code drives the Job to ``Succeeded`` and the handle
         resolves without raising.
    """
    from osimflow.executors import KubernetesExecutor

    namespace = os.environ.get("OSIMFLOW_KUBERNETES_NAMESPACE", "default")
    remote_command = (
        'python -c "import json,os,sys; '
        "p=os.environ.get('OSIMFLOW_TASK_PAYLOAD'); "
        "sys.exit(0 if p and json.loads(p).get('name')=='cmd-check' else 4)\""
    )
    executor = KubernetesExecutor(
        namespace=namespace,
        poll_interval_s=2.0,
        max_poll_interval_s=15.0,
    )
    handle = executor.submit(
        lambda: None,
        name="cmd-check",
        container="python:3.12-slim",
        remote_command=remote_command,
        cpus=1,
        memory_mb=512,
        time_min=5,
    )
    # A non-zero exit (payload missing/wrong) fails the test via RuntimeError.
    assert handle.result() is None
    executor.shutdown()


def test_real_kubernetes_job_failure_reason_surfaces() -> None:
    """A non-zero pod exit code must surface in the handle error (#996)."""
    from osimflow.executors import KubernetesExecutor

    namespace = os.environ.get("OSIMFLOW_KUBERNETES_NAMESPACE", "default")
    executor = KubernetesExecutor(
        namespace=namespace,
        poll_interval_s=2.0,
        max_poll_interval_s=15.0,
    )
    handle = executor.submit(
        lambda: None,
        name="fail-check",
        container="python:3.12-slim",
        remote_command="python -c 'import sys; sys.exit(3)'",
        cpus=1,
        memory_mb=512,
        time_min=5,
    )
    with pytest.raises(RuntimeError, match="exit code 3"):
        handle.result()
    executor.shutdown()


def test_real_kubernetes_3_samples(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """3-sample campaign against a real Kubernetes cluster.

    This test exercises the full production path:

      1. ``KubernetesExecutor`` submits real ``batch/v1`` Jobs via the
         ``kubernetes`` Python client (one Job per sample).
      2. Each Job maps the resource directives (``cpus``/``memory_mb``) to
         ``V1ResourceRequirements`` requests+limits and ``time_min`` to
         ``active_deadline_seconds``.
      3. Each Job runs the ephemeral runner (``python -m
         osimflow.remote_runner``) with the task payload in
         ``OSIMFLOW_TASK_PAYLOAD`` (issue #996) — the worker images must
         therefore ship the ``osimflow`` package.
      4. The executor signs the payload with the HMAC shared secret
         configured by this test (issue #1453): the submitted Job must
         carry ``OSIMFLOW_TASK_PAYLOAD`` + ``OSIMFLOW_TASK_PAYLOAD_SIG``
         + ``OSIMFLOW_TASK_PAYLOAD_SECRET`` and the signature must
         verify with the configured secret.
      5. The executor polls ``list_namespaced_pod`` until the pod reaches a
         terminal phase (``Succeeded``/``Failed``), tolerating scheduling
         latency via exponential backoff.
      6. The Campaign collects per-sample results from shared storage.

    The test asserts the same 4-artifact contract as the local executor
    test (``test_local_executor.py``), plus the per-campaign ``run.json``
    monitoring trace.  A failure here indicates a regression in either the
    executor wiring, the container image, or the cluster configuration.
    """
    import shutil

    from osimflow import Campaign, CampaignConfig
    from osimflow.executors import KubernetesExecutor
    from osimflow.task_payload_hmac import (  # noqa: PLC0415
        TASK_PAYLOAD_SECRET_ENV,
        TASK_PAYLOAD_SIG_ENV,
        verify_task_payload,
    )

    namespace = os.environ.get("OSIMFLOW_KUBERNETES_NAMESPACE", "default")

    # Issue #1453: configure the HMAC shared secret so the executor signs
    # OSIMFLOW_TASK_PAYLOAD and the remote runner verifies (fail-closed)
    # before decoding/executing.
    secret = "osimflow-k8s-e2e-task-payload-secret"
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

    # --- ephemeral-runner wiring on the real cluster (issue #996) --------
    # Every submitted Job must run the remote runner (not `sleep infinity`)
    # and carry the serialized task payload.
    jobs = _list_osimflow_jobs(namespace)
    assert jobs, "no osimflow-* Jobs found in namespace after campaign run"
    sim_jobs = [j for j in jobs if "sim-" in (j.metadata.name or "")]
    assert len(sim_jobs) >= 3, f"expected >= 3 sim Jobs, got {[j.metadata.name for j in jobs]}"
    for job in sim_jobs[:3]:
        container = job.spec.template.spec.containers[0]
        assert container.command == [
            "python",
            "-m",
            "osimflow.remote_runner",
        ], f"Job {job.metadata.name} command is not the remote runner"
        env = {e.name: e.value for e in (container.env or [])}
        assert "OSIMFLOW_TASK_PAYLOAD" in env, (
            f"Job {job.metadata.name} missing OSIMFLOW_TASK_PAYLOAD env var"
        )
        payload = json.loads(env["OSIMFLOW_TASK_PAYLOAD"])
        assert payload["schema_version"] == 1
        assert payload["step"] == "sim"

        # --- HMAC signature propagation (issue #1453) --------------------
        # The signed job must carry payload + signature + secret, and the
        # signature must verify against the configured secret.
        assert TASK_PAYLOAD_SIG_ENV in env, (
            f"Job {job.metadata.name} missing {TASK_PAYLOAD_SIG_ENV} env var "
            "(the HMAC signature did not propagate to the submitted spec)"
        )
        assert env.get(TASK_PAYLOAD_SECRET_ENV) == secret, (
            f"Job {job.metadata.name} missing/mismatched {TASK_PAYLOAD_SECRET_ENV} "
            f"env var: {env.get(TASK_PAYLOAD_SECRET_ENV)!r}"
        )
        assert verify_task_payload(
            env["OSIMFLOW_TASK_PAYLOAD"],
            env.get(TASK_PAYLOAD_SIG_ENV),
            env[TASK_PAYLOAD_SECRET_ENV],
        ), (
            f"Job {job.metadata.name}: {TASK_PAYLOAD_SIG_ENV} does not verify "
            f"against {TASK_PAYLOAD_SECRET_ENV} for the submitted payload"
        )
        # Remote-runner verification succeeded (issue #1453): with the
        # shared secret configured, ``osimflow.remote_runner`` exits
        # non-zero on any missing/tampered signature, which would fail
        # the Job — a Succeeded sim Job is the recorded proof.
        assert (job.status.succeeded or 0) >= 1, (
            f"Job {job.metadata.name} did not succeed; the remote runner may "
            "have failed HMAC verification "
            f"(status: succeeded={job.status.succeeded!r})"
        )
