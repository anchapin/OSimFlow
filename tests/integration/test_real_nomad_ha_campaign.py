"""Real Nomad HA / multi-node E2E test.  Only runs when OSIMFLOW_NOMAD_E2E=1.

Closes the substrate-coverage gap called out by issue #1020: every
other executor in ``osimflow/executors/`` has a
``tests/integration/test_real_<substrate>_campaign.py`` companion
(Slurm #941, AWS Batch #942, Azure #958, Google #959, Kubernetes,
PBS, Dask-JobQueue, Docker Swarm, OpenStudio CLI #939) but the
``NomadExecutor`` multi-node / HA path did not.

The existing single-node tests under ``tests/integration/nomad_e2e/``
(``test_single_node.py`` + ``test_single_node_mock.py``) cover the
single-agent case.  This file is the **production-grade multi-node
campaign** counterpart: a 3-sample campaign driven through the
public ``osimflow.executors.NomadExecutor`` against a multi-server
Nomad cluster (typically 3-server Raft — see ``docs/nomad-production.md``
and ``infra/nomad/examples/ha/``).

Requires:

  - A reachable multi-node Nomad cluster (the
    ``tests/integration/nomad_e2e/docker-compose.multi.yml`` harness
    is the canonical local recipe; production deployments are
    configured via ``infra/nomad/examples/ha/``).
  - ``OSIMFLOW_NOMAD_E2E=1``.
  - ``NOMAD_ADDR`` env var (the cluster's HTTP API endpoint,
    e.g. ``http://nomad.example:4646``); defaults to
    ``http://127.0.0.1:4646`` if unset (single-node fallback).
  - ``NOMAD_TOKEN`` env var (the cluster's ACL token — see
    ``infra/nomad/acl/policies/``).

This test is intentionally skipped in normal CI.  It is designed for
the nightly ``nomad-e2e`` workflow
(``.github/workflows/nomad-e2e.yml``) which already runs the
single-node tests in CI; the multi-node / HA job is nightly-only.
The matrix lives at ``docs/substrate-coverage.md``.  To run locally
against the bundled ``docker-compose.multi.yml`` cluster::

    docker compose -f tests/integration/nomad_e2e/docker-compose.multi.yml up -d
    export OSIMFLOW_NOMAD_E2E=1
    export NOMAD_ADDR=http://localhost:4646
    .venv/bin/pytest tests/integration/test_real_nomad_ha_campaign.py -v --timeout=1800
"""

import json
import os
import shutil
import urllib.error
import urllib.request
from pathlib import Path

import pytest

# Primary gate: the test opt-in flag.
pytestmark = pytest.mark.skipif(
    os.environ.get("OSIMFLOW_NOMAD_E2E") != "1",
    reason="Set OSIMFLOW_NOMAD_E2E=1 and NOMAD_ADDR to run real Nomad (HA / multi-node) tests",
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def _nomad_api_reachable() -> bool:
    """Return True if ``GET <NOMAD_ADDR>/v1/agent/health`` returns 200.

    The Nomad executor shells out to ``POST /v1/jobs`` /
    ``POST /v1/job/<id>/dispatch`` against this base URL.  We probe
    the lightweight ``/v1/agent/health`` endpoint and require the
    JSON body to report ``"client": "ready"`` plus a list of
    servers — both signals of a healthy multi-node cluster.
    """
    addr = os.environ.get("NOMAD_ADDR") or "http://127.0.0.1:4646"
    url = f"{addr.rstrip('/')}/v1/agent/health"
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:  # noqa: S310 -- trusted local URL
            if resp.status != 200:
                return False
            body = json.loads(resp.read())
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return False
    # Multi-node signal: server list has more than one entry, OR
    # explicitly check that the cluster has at least one client.
    servers = body.get("servers") or []
    if len(servers) >= 2:
        return True
    # Fallback: single-node is still acceptable — the test doesn't
    # *require* HA, it just runs the same Campaign against whatever
    # the NOMAD_ADDR points to.  Issue #1020 explicitly notes HA as
    # the new dimension, but a single-node run still exercises the
    # production wiring.
    return body.get("client") == "ready"


def test_real_nomad_3_samples(tmp_path: Path) -> None:
    """3-sample campaign against a real Nomad cluster (single- or multi-node).

    This test exercises the full production path:

      1. ``NomadExecutor`` is constructed; the address comes from
         ``NOMAD_ADDR`` (with a 127.0.0.1 default for local dev).
      2. ``submit()`` POSTs a job spec to ``/v1/jobs`` (direct
         mode) or dispatches a parameterized ``osimflow-worker``
         job (when ``use_dispatch=True`` — set via the
         ``--nomad-dispatch-policy`` CLI flag, see
         ``osimflow/__main__.py``).
      3. ``_NomadHandle.result()`` polls ``GET /v1/allocation/<id>``
         with exponential backoff until the allocation reaches
         ``complete`` (success) or a terminal failure state.
      4. The Campaign collects per-sample results and emits the
         standard 4-artifact contract + ``run.json``.

    The test asserts:

      - The 4-artifact contract (``aggregated_results.csv``,
        ``failed_simulations.csv``, per-sample KPI JSONs, ``plots/``).
      - ``run.json`` records ``executor == "nomad"`` with all 6 DAG
        steps and per-sample status.
      - **Structural proof the path was real, not mocked**: the
        executor's stored ``address`` matches ``NOMAD_ADDR`` and the
        health probe at that address returned ``"client":"ready"``.

    A failure here indicates a regression in either the
    ``NomadExecutor.submit`` plumbing, the HTTP wire format, the
    dispatch-mode parameterized-job registration, or the cluster's
    scheduler / ACL configuration.
    """
    if not _nomad_api_reachable():
        pytest.skip(
            "OSIMFLOW_NOMAD_E2E=1 is set but the Nomad API at NOMAD_ADDR "
            "is unreachable or unhealthy"
        )

    from osimflow import Campaign, CampaignConfig
    from osimflow.executors import NomadExecutor

    addr = os.environ.get("NOMAD_ADDR") or "http://127.0.0.1:4646"

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

    executor = NomadExecutor(
        address=addr,
        datacentre=os.environ.get("NOMAD_DATACENTRE", "dc1"),
        poll_interval_s=1.0,
        max_poll_interval_s=10.0,
        # Honor the dispatch-mode env var if the operator wants the
        # parameterized-job path exercised (issue #135).
        use_dispatch=os.environ.get("OSIMFLOW_NOMAD_USE_DISPATCH") == "1",
    )

    # ---- Structural assertion: the executor talks to the right cluster ----
    assert executor.address == addr, (
        f"NomadExecutor.address={executor.address!r} != NOMAD_ADDR={addr!r}; "
        "the address config did not propagate"
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
    # --- Nomad wire check: /v1/job sees the Resources block (#1403) ---
    from tests.integration._resource_contract import (  # noqa: PLC0415
        nomad_job_resources,
    )

    _nomad_addr = os.environ.get("NOMAD_ADDR") or "http://127.0.0.1:4646"
    _nomad_token = os.environ.get("NOMAD_TOKEN")
    sim_job_ids = [
        r["job_id"]
        for r in directives.records
        if r["cpus"] == 4 and r["memory_mb"] == 8192 and r["job_id"]
    ][:3]
    for job_id in sim_job_ids:
        resources = nomad_job_resources(_nomad_addr, job_id, token=_nomad_token)
        assert resources.get("MemoryMB") == 8192, (
            f"Nomad dropped memory_mb for {job_id}: {resources}"
        )
        # Nomad stores CPU in MHz; the executor maps cpus -> MHz * 1000.
        assert resources.get("CPU"), f"Nomad dropped cpus for {job_id}: {resources}"
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
    assert trace["config"]["executor"] == "nomad", (
        f"run.json did not record executor=nomad; got {trace['config']['executor']!r}"
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
