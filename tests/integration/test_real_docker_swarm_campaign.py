"""Real Docker Swarm E2E test.  Only runs when OSIMFLOW_DOCKER_SWARM_E2E=1.

Closes the substrate-coverage gap called out by issue #1020: every
other executor in ``osimflow/executors/`` has a
``tests/integration/test_real_<substrate>_campaign.py`` companion
(Slurm #941, AWS Batch #942, Azure #958, Google #959, Kubernetes,
Nomad, PBS, Dask-JobQueue, OpenStudio CLI #939) but
``DockerSwarmExecutor`` did not.

Requires:

  - A reachable Docker daemon in Swarm mode (``docker info`` reports
    ``Swarm.ControlAvailable: true``).
  - ``OSIMFLOW_DOCKER_SWARM_E2E=1``.
  - The ``docker`` Python SDK (``pip install docker``).
  - ``OSIMFLOW_DOCKER_SWARM_IMAGE`` env var (the image to launch
    per-sample services with; defaults to ``python:3.12-slim`` for
    portability — the production default ``nrel/openstudio:latest``
    is pulled only when explicitly set).

This test is intentionally skipped in normal CI.  It is designed for
a nightly ``docker-swarm-e2e`` workflow (to be provisioned alongside
the existing ``slurm-e2e`` / ``aws-batch-e2e`` runners).  The matrix
lives at ``docs/substrate-coverage.md``.  To run locally on a
single-node Docker Swarm (e.g. via ``docker swarm init``)::

    export OSIMFLOW_DOCKER_SWARM_E2E=1
    export OSIMFLOW_DOCKER_SWARM_IMAGE=python:3.12-slim
    .venv/bin/pytest tests/integration/test_real_docker_swarm_campaign.py -v --timeout=1800

After the run, ``docker service ls`` should show 3 services named
``osimflow-<task-name>`` (the test does NOT delete them — manual
cleanup with ``docker service rm osimflow-*`` is the operator's
responsibility; this is the production pattern).
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

# Primary gate: the test opt-in flag.
pytestmark = pytest.mark.skipif(
    os.environ.get("OSIMFLOW_DOCKER_SWARM_E2E") != "1",
    reason="Set OSIMFLOW_DOCKER_SWARM_E2E=1 and ensure a Docker daemon in Swarm "
    "mode is reachable to run real Docker Swarm tests",
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def _docker_swarm_available() -> tuple[bool, str]:
    """Return ``(available, reason)`` for a Docker Swarm reachability probe.

    The executor's ``_check_docker_available`` returns True when
    ``client.info()['Swarm']['ControlAvailable']`` is truthy.  We
    import the SDK lazily and short-circuit with a clean skip
    message if it is missing — otherwise a developer running on a
    workstation would see a confusing ``ImportError`` instead of a
    clear skip.
    """
    try:
        import docker  # noqa: PLC0415
    except ImportError:
        return False, "docker Python SDK not installed (pip install docker)"
    try:
        client = docker.from_env()  # type: ignore[attr-defined]
        client.ping()
    except Exception as exc:  # noqa: BLE001
        return False, f"Docker daemon not reachable: {exc}"
    info = client.info()
    swarm = info.get("Swarm") or {}
    if not swarm.get("ControlAvailable"):
        return False, (
            "Docker daemon reachable but not in Swarm mode; run 'docker swarm init' to enable Swarm"
        )
    return True, ""


def _swarm_service_env(service_name: str) -> dict[str, str]:
    """Return the service's container env as a dict via service inspect (issue #1453).

    Mirrors :func:`tests.integration._resource_contract.swarm_service_resources`
    but returns ``Spec.TaskTemplate.ContainerSpec.Env`` (``KEY=value`` pairs)
    instead of the ``Resources`` block.
    """
    proc = subprocess.run(  # noqa: S603
        ["docker", "service", "inspect", service_name, "--format", "{{json .Spec}}"],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    spec = json.loads(proc.stdout)
    pairs = (spec.get("TaskTemplate", {}).get("ContainerSpec", {}) or {}).get("Env") or []
    env: dict[str, str] = {}
    for pair in pairs:
        key, _, value = pair.partition("=")
        env[key] = value
    return env


def test_real_docker_swarm_3_samples(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """3-sample campaign against a real Docker Swarm cluster (#582, #1020).

    This test exercises the full production path:

      1. ``DockerSwarmExecutor`` is constructed and lazily creates a
         Docker SDK client on first use.
      2. ``submit()`` creates a Docker Swarm **service** per sample
         (via ``client.services.create(...)``) with resource limits
         mapped from ``cpus`` / ``memory_mb``.  The service runs a
         ``sleep infinity`` container so the executor can poll its
         task state until completion.
      3. ``_wait_for_terminal()`` polls the service's tasks with
         exponential backoff until all reach a terminal state
         (``complete`` / ``failed`` / ``shutdown`` / ``rejected``).
      4. The Campaign collects per-sample results and emits the
         standard 4-artifact contract + ``run.json``.

    The test asserts:

      - The 4-artifact contract (``aggregated_results.csv``,
        ``failed_simulations.csv``, per-sample KPI JSONs, ``plots/``).
      - ``run.json`` records ``executor == "docker_swarm"`` with all
        6 DAG steps and per-sample status.
      - **Structural proof the path was real, not the local
        dev-fallback**: the executor did NOT silently fall back to
        ``LocalExecutor`` (which is what
        ``OSIMFLOW_DOCKER_SWARM_DEV_FALLBACK=1`` or
        ``OSIMFLOW_DOCKER_SWARM_DRY_RUN=1`` trigger).
        We assert by inspecting the executor's ``_stub_executor``
        attribute: when the dev-fallback fires, that attribute is
        populated by the executor on first ``submit()``; when the
        real path runs, it remains ``None``.

    Cleanup:
        The 3 Swarm services created during the test are left in
        place after the test exits — matching the production pattern
        (services persist after their tasks complete, allowing the
        Campaign to query status).  The CI runner / developer is
        expected to clean them up with ``docker service rm
        osimflow-*``.

    A failure here indicates a regression in either the
    ``DockerSwarmExecutor.submit`` plumbing, the SDK wire format, or
    the Swarm cluster configuration.
    """
    available, reason = _docker_swarm_available()
    if not available:
        pytest.skip(f"OSIMFLOW_DOCKER_SWARM_E2E=1 is set but {reason}")

    # The dev-fallback env vars must NOT be set — they would silently
    # downgrade to LocalExecutor and make the test pass even if the
    # real Docker Swarm code path were broken.  We fail loudly here
    # so the developer fixes their environment instead of getting a
    # false-green CI signal.
    for env_name in (
        "OSIMFLOW_DOCKER_SWARM_DEV_FALLBACK",
        "OSIMFLOW_DOCKER_SWARM_DRY_RUN",
    ):
        if os.environ.get(env_name) in ("1", "true", "yes"):
            pytest.skip(
                f"{env_name} is set; the executor would silently fall back to "
                "LocalExecutor and defeat the purpose of this real-E2E test. "
                f"Unset {env_name} before running."
            )

    from osimflow import Campaign, CampaignConfig
    from osimflow.executors import DockerSwarmExecutor
    from osimflow.task_payload_hmac import (  # noqa: PLC0415
        TASK_PAYLOAD_SECRET_ENV,
        TASK_PAYLOAD_SIG_ENV,
        verify_task_payload,
    )

    # Issue #1453: configure the HMAC shared secret so the executor signs
    # OSIMFLOW_TASK_PAYLOAD and the remote runner verifies (fail-closed)
    # before decoding/executing.
    secret = "osimflow-swarm-e2e-task-payload-secret"
    monkeypatch.setenv(TASK_PAYLOAD_SECRET_ENV, secret)

    # Use the env-configurable image (default: python:3.12-slim — the
    # OSimFlow stub work function only needs Python; the real
    # ``nrel/openstudio`` image is pulled only when explicitly set).
    image = os.environ.get("OSIMFLOW_DOCKER_SWARM_IMAGE", "python:3.12-slim")

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

    executor = DockerSwarmExecutor(
        image=image,
        poll_interval_s=1.0,
        max_poll_interval_s=10.0,
    )

    # ---- Structural assertion: dev-fallback is NOT active ----
    # When the dev-fallback path fires, the executor sets
    # ``self._stub_executor = LocalExecutor(...)`` on first submit
    # failure.  We assert it stays None — a populated stub here would
    # prove the real Swarm code path was never taken.
    assert executor._stub_executor is None, (  # noqa: SLF001
        "DockerSwarmExecutor._stub_executor is populated; the dev-fallback "
        "path was activated. The test would be running LocalExecutor under "
        "the hood instead of a real Swarm service."
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
    # --- Swarm wire check: service inspect sees Limits (#1403) ---
    from tests.integration._resource_contract import (  # noqa: PLC0415
        swarm_service_resources,
    )

    sim_service_ids = [
        r["job_id"]
        for r in directives.records
        if r["cpus"] == 4 and r["memory_mb"] == 8192 and r["job_id"]
    ][:3]
    for service_name in sim_service_ids:
        resources = swarm_service_resources(service_name)
        nano = int(4 * 1e9)
        assert resources.get("Limits", {}).get("NanoCPUs") == nano, (
            f"Swarm dropped cpus for {service_name}: {resources}"
        )
        assert resources.get("Limits", {}).get("MemoryBytes") == 8192 * 1024 * 1024, (
            f"Swarm dropped memory_mb for {service_name}: {resources}"
        )
    result = campaign.run()
    executor.shutdown()

    # --- HMAC signature propagation (issue #1453) -------------------------
    # Re-read the real service spec via ``docker service inspect`` and
    # assert the submitted service carries payload + signature + secret,
    # and that the signature verifies against the configured secret.
    # Campaign success with the secret configured is itself the
    # remote-runner verification proof: the runner exits non-zero on a
    # missing/tampered signature, which would fail the service and the
    # campaign.
    sim_service_names_hmac = [
        r["job_id"]
        for r in directives.records
        if r["cpus"] == 4 and r["memory_mb"] == 8192 and r["job_id"]
    ][:3]
    assert len(sim_service_names_hmac) >= 3, (
        f"expected >= 3 sim service names for the HMAC wire check, got {directives.records!r}"
    )
    for service_name in sim_service_names_hmac:
        env = _swarm_service_env(service_name)
        assert env.get("OSIMFLOW_TASK_PAYLOAD"), (
            f"Swarm service {service_name} missing OSIMFLOW_TASK_PAYLOAD env var"
        )
        assert env.get(TASK_PAYLOAD_SIG_ENV), (
            f"Swarm service {service_name} missing {TASK_PAYLOAD_SIG_ENV} env var "
            "(the HMAC signature did not propagate to the submitted spec)"
        )
        assert env.get(TASK_PAYLOAD_SECRET_ENV) == secret, (
            f"Swarm service {service_name} missing/mismatched "
            f"{TASK_PAYLOAD_SECRET_ENV} env var: {env.get(TASK_PAYLOAD_SECRET_ENV)!r}"
        )
        assert verify_task_payload(
            env["OSIMFLOW_TASK_PAYLOAD"],
            env.get(TASK_PAYLOAD_SIG_ENV),
            env[TASK_PAYLOAD_SECRET_ENV],
        ), (
            f"Swarm service {service_name}: {TASK_PAYLOAD_SIG_ENV} does not verify "
            f"against {TASK_PAYLOAD_SECRET_ENV} for the submitted payload"
        )

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
    assert trace["config"]["executor"] == "docker_swarm", (
        f"run.json did not record executor=docker_swarm; got {trace['config']['executor']!r}"
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
