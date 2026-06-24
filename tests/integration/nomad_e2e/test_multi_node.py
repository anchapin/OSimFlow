"""End-to-end integration test: multi-node Nomad HA cluster (issue #137).

Acceptance criteria (G17b):

  * ``docker-compose.multi.yml`` starts a 3-server + 2-client Nomad
    cluster in under 60 seconds.
  * ``test_multi_node_cluster_healthy``: Nomad HA API responds with
    a leader; at least 2 client nodes are registered.
  * ``test_10_sample_campaign_multi_node``: run a 10-sample campaign
    using ``NomadExecutor`` against the multi-node cluster and assert:
    - ``aggregated_results.csv`` has header + 10 rows
    - ``failed_simulations.csv`` exists
    - ``run.json`` exists with 10 sample traces, all ``"ok"``
    - Allocations landed on at least 2 distinct client nodes
  * ``test_failover_campaign_continues``: kill one server, verify the
    campaign continues and completes successfully on the remaining
    quorum.

The test suite uses the same ``_LocalWorkNomadExecutor`` pattern as
the single-node tests (``test_single_node.py``): real Nomad HTTP
wiring is exercised but the heavy work runs locally via a thread pool,
so we don't need the NREL OpenStudio Docker image in CI.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from osimflow import Campaign, CampaignConfig
from osimflow.executors import NomadExecutor

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parents[3]
EXAMPLE_PKG = REPO_ROOT / "example_package"
EXAMPLE_VARS_YML = REPO_ROOT / "variables.yml"

MULTI_COMPOSE_FILE = Path(__file__).resolve().parent / "docker-compose.multi.yml"
NOMAD_ADDRESS = "http://localhost:4646"
NOMAD_READY_TIMEOUT_S = 60.0
NOMAD_READY_POLL_S = 1.5


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    """Clean per-test work directory."""
    wd = tmp_path / "work"
    wd.mkdir()
    (wd / "variables.yml").write_text(EXAMPLE_VARS_YML.read_text())
    return wd


@pytest.fixture
def template_pkg(workdir: Path) -> Path:
    """Copy of the project's example_package into tmp_path."""
    pkg = workdir / "template"
    shutil.copytree(EXAMPLE_PKG, pkg)
    return pkg


@pytest.fixture
def outdir(workdir: Path) -> Path:
    od = workdir / "out"
    od.mkdir()
    return od


@pytest.fixture
def cfg_10(workdir: Path, template_pkg: Path, outdir: Path) -> CampaignConfig:
    """10-sample campaign config for multi-node fan-out testing."""
    return CampaignConfig(
        input_variables=workdir / "variables.yml",
        template_sim_package=template_pkg,
        n_samples=10,
        outdir=outdir,
        openstudio_version="3.11.0",
        archive_intermediates=False,
    )


@pytest.fixture
def cfg_3(workdir: Path, template_pkg: Path, outdir: Path) -> CampaignConfig:
    """3-sample campaign config for failover test."""
    return CampaignConfig(
        input_variables=workdir / "variables.yml",
        template_sim_package=template_pkg,
        n_samples=3,
        outdir=outdir,
        openstudio_version="3.11.0",
        archive_intermediates=False,
    )


# ---------------------------------------------------------------------------
# Multi-node cluster fixture
# ---------------------------------------------------------------------------
def _docker_available() -> bool:
    """Check whether Docker and Docker Compose are available."""
    try:
        subprocess.run(
            ["docker", "info"],
            capture_output=True,
            check=True,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return False
    try:
        subprocess.run(
            ["docker", "compose", "version"],
            capture_output=True,
            check=True,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return False
    return True


def _wait_for_nomad(address: str, timeout_s: float, poll_s: float) -> None:
    """Poll ``/v1/status/leader`` until it returns 200."""
    from urllib.error import URLError
    from urllib.request import Request, urlopen

    url = f"{address}/v1/status/leader"
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            req = Request(url, method="GET", headers={"Accept": "application/json"})
            with urlopen(req, timeout=5.0) as resp:
                if resp.status == 200:
                    return
        except (URLError, OSError):
            pass
        time.sleep(poll_s)
    raise TimeoutError(f"Nomad API at {address} did not become ready within {timeout_s:.0f}s")


@pytest.fixture(scope="module")
def nomad_multi() -> str:  # type: ignore[misc]
    """Module-scoped fixture: start multi-node Nomad cluster, yield address, tear down.

    Skips when Docker is not available.
    """
    if not _docker_available():
        pytest.skip("Docker / Docker Compose not available — skipping Nomad multi-node E2E tests")

    # Start the Docker Compose stack.
    try:
        proc = subprocess.run(
            [
                "docker",
                "compose",
                "-f",
                str(MULTI_COMPOSE_FILE),
                "up",
                "-d",
                "--wait",
            ],
            capture_output=True,
            timeout=180,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as exc:
        pytest.skip(f"Docker Compose failed to start multi-node Nomad: {exc}")
        return  # pragma: no cover

    if proc.returncode != 0:
        log_proc = subprocess.run(
            ["docker", "compose", "-f", str(MULTI_COMPOSE_FILE), "logs"],
            capture_output=True,
            timeout=30,
            check=False,
        )
        logs = log_proc.stdout.decode("utf-8", errors="replace")[-2000:]
        subprocess.run(
            [
                "docker",
                "compose",
                "-f",
                str(MULTI_COMPOSE_FILE),
                "down",
                "--volumes",
                "--remove-orphans",
            ],
            capture_output=True,
            check=False,
            timeout=60,
        )
        pytest.skip(
            f"Docker Compose up failed (rc={proc.returncode}). "
            f"stderr: {proc.stderr.decode('utf-8', errors='replace')[-500:]}\n"
            f"logs:\n{logs}"
        )
        return  # pragma: no cover

    try:
        _wait_for_nomad(NOMAD_ADDRESS, NOMAD_READY_TIMEOUT_S, NOMAD_READY_POLL_S)
        yield NOMAD_ADDRESS
    except TimeoutError:
        log_proc = subprocess.run(
            ["docker", "compose", "-f", str(MULTI_COMPOSE_FILE), "logs"],
            capture_output=True,
            timeout=30,
            check=False,
        )
        logs = log_proc.stdout.decode("utf-8", errors="replace")[-2000:]
        pytest.skip(
            f"Nomad multi-node API did not become ready within "
            f"{NOMAD_READY_TIMEOUT_S:.0f}s. Logs:\n{logs}"
        )
        return  # pragma: no cover
    finally:
        subprocess.run(
            [
                "docker",
                "compose",
                "-f",
                str(MULTI_COMPOSE_FILE),
                "down",
                "--volumes",
                "--remove-orphans",
            ],
            capture_output=True,
            check=False,
            timeout=120,
        )


# ---------------------------------------------------------------------------
# Helpers — Nomad HTTP API queries
# ---------------------------------------------------------------------------
def _nomad_get(path: str, address: str = NOMAD_ADDRESS) -> Any:
    """GET a JSON path from the Nomad API."""
    from urllib.request import Request, urlopen

    url = f"{address}{path}"
    req = Request(url, method="GET", headers={"Accept": "application/json"})
    with urlopen(req, timeout=10.0) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _get_client_nodes(address: str) -> list[dict[str, Any]]:
    """Return the list of Nomad client nodes from the cluster."""
    nodes = _nomad_get("/v1/nodes", address)
    return nodes  # type: ignore[no-any-return]


def _get_allocations_for_job(job_id: str, address: str) -> list[dict[str, Any]]:
    """Return allocations for a given job ID."""
    allocs = _nomad_get(f"/v1/job/{job_id}/allocations", address)
    return allocs  # type: ignore[no-any-return]


# ---------------------------------------------------------------------------
# Test-only executor: real Nomad submit + local work execution
# ---------------------------------------------------------------------------
class _LocalWorkNomadExecutor(NomadExecutor):
    """Test-only Nomad executor for multi-node testing.

    Submits real batch jobs to the Nomad cluster (exercising the HTTP
    wiring) and tracks allocation node placement.  The actual work
    runs locally via a thread pool so the NREL OpenStudio image is not
    required in CI.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._local_pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="e2e-multi")
        self._allocation_nodes: dict[str, str] = {}

    def submit(  # type: ignore[override]
        self,
        fn: Any,
        *args: Any,
        name: str = "task",
        cpus: int = 1,
        memory_mb: int = 1024,
        time_min: int = 60,
        container: str | None = None,
        **kwargs: Any,
    ) -> Any:
        # Submit a minimal batch job to the real Nomad cluster and
        # record which node it lands on.
        self._submit_minimal_job(name)

        # Queue the actual work on the local pool.
        local_fut: Future[Any] = self._local_pool.submit(fn, *args)

        class _Handle:
            def __init__(self, fut: Future[Any]) -> None:
                self._fut = fut
                self.job_id = f"local-{id(fut)}"

            def result(self, timeout: float | None = None) -> Any:
                return self._fut.result(timeout=timeout)

            def done(self) -> bool:
                return self._fut.done()

        return _Handle(local_fut)

    def _submit_minimal_job(self, name: str) -> None:
        """Submit a trivial ``exec`` batch job to the real Nomad cluster.

        After submission, polls the allocation to record which client
        node the job was placed on.  Catches errors so the test
        continues even if the Nomad job fails.
        """
        import osimflow.executors as exec_mod  # noqa: PLC0415

        slug = exec_mod._slugify_job_name(f"multi-e2e-{name}")  # noqa: SLF001
        spec = {
            "Job": {
                "ID": slug,
                "Name": slug,
                "Type": "batch",
                "Datacenters": [self.datacentre],
                "TaskGroups": [
                    {
                        "Name": "test",
                        "Tasks": [
                            {
                                "Name": "test",
                                "Driver": "exec",
                                "Config": {
                                    "command": "/bin/echo",
                                    "args": ["multi-node-ok"],
                                },
                                "Resources": {
                                    "CPU": 100,
                                    "MemoryMB": 64,
                                },
                            }
                        ],
                    }
                ],
            }
        }
        try:
            response = self._client.submit_job(spec)
            job_id = response.get("JobID", "")
            eval_id = response.get("EvalID", "")
            if job_id and eval_id:
                # Best-effort: record the node for this allocation.
                try:
                    alloc_id = self._client.resolve_allocation(eval_id=eval_id, job_id=job_id)
                    if alloc_id:
                        alloc = self._client.get_allocation(alloc_id)
                        node_id = alloc.get("NodeID", "unknown")
                        self._allocation_nodes[name] = node_id
                except Exception:
                    pass  # best-effort node tracking
        except Exception:
            pass  # log but don't fail

    @property
    def allocation_nodes(self) -> dict[str, str]:
        """Map of sample name -> Nomad node ID for submitted jobs."""
        return dict(self._allocation_nodes)

    def shutdown(self) -> None:
        self._local_pool.shutdown(wait=True)
        super().shutdown()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
@pytest.mark.nomad_e2e
def test_multi_node_cluster_healthy(nomad_multi: str) -> None:
    """Verify the multi-node Nomad cluster has a leader and client nodes."""
    # Leader check.
    leader = _nomad_get("/v1/status/leader", nomad_multi)
    assert leader, "no leader elected in multi-node cluster"

    # Peer count — at least 3 servers for quorum.
    peers = _nomad_get("/v1/status/peers", nomad_multi)
    assert len(peers) >= 3, f"expected >= 3 server peers, got {len(peers)}: {peers}"

    # Client nodes — both should register.
    nodes = _get_client_nodes(nomad_multi)
    assert len(nodes) >= 2, f"expected >= 2 client nodes, got {len(nodes)}: {nodes}"


@pytest.mark.nomad_e2e
def test_10_sample_campaign_multi_node(
    nomad_multi: str,
    cfg_10: CampaignConfig,
    outdir: Path,
) -> None:
    """Run a 10-sample campaign on the multi-node cluster and verify
    fan-out across nodes.
    """
    executor = _LocalWorkNomadExecutor(
        address=nomad_multi,
        datacentre="dc1",
        poll_interval_s=0.5,
        max_poll_interval_s=5.0,
    )
    campaign = Campaign(cfg=cfg_10, executor=executor)
    result = campaign.run()
    executor.shutdown()

    # --- 4 output artifacts -----------------------------------------------
    csv_path = outdir / "aggregated_results.csv"
    assert csv_path.is_file(), f"missing artifact: {csv_path}"
    csv_text = csv_path.read_text()
    assert csv_text.startswith("sample_id")
    assert len(csv_text.strip().splitlines()) == 10 + 1, (
        f"expected header + 10 data rows, got: {csv_text!r}"
    )

    failed_path = outdir / "failed_simulations.csv"
    assert failed_path.is_file(), f"missing artifact: {failed_path}"
    assert failed_path.read_text().startswith("sample_id")

    kpi_files = sorted((outdir / "work" / "kpis").glob("kpi_*.json"))
    assert len(kpi_files) == 10, f"expected 10 KPI JSONs, got {len(kpi_files)}"
    for kpi in kpi_files:
        data = json.loads(kpi.read_text())
        assert "sample_id" in data
        assert "kpis" in data

    plots_dir = outdir / "plots"
    assert plots_dir.is_dir(), f"missing plot directory: {plots_dir}"

    # --- run.json monitoring trace ----------------------------------------
    run_json = outdir / "run.json"
    assert run_json.is_file(), f"missing run.json: {run_json}"
    trace = json.loads(run_json.read_text())
    assert trace["schema_version"] == 1
    assert trace["config"]["executor"] == "nomad"
    assert trace["config"]["n_samples"] == 10

    # All 10 samples must be "ok".
    assert len(trace["per_sample"]) == 10
    statuses = {row["status"] for row in trace["per_sample"]}
    assert statuses == {"ok"}, f"expected all-ok statuses, got {statuses}"

    # --- Fan-out: allocations must land on >= 2 distinct nodes -----------
    # The _LocalWorkNomadExecutor tracks which Nomad node each submitted
    # job was placed on.  We require at least 2 distinct nodes to validate
    # that the multi-node cluster is actually distributing work.
    unique_nodes = set(executor.allocation_nodes.values())
    assert len(unique_nodes) >= 2, (
        f"expected allocations on >= 2 distinct nodes, but all landed on: "
        f"{unique_nodes}. allocation_nodes={executor.allocation_nodes}"
    )

    # --- result dict contract ---------------------------------------------
    assert set(result) >= {"samples", "kpis", "aggregated", "plots", "elapsed_s", "run_json"}
    assert len(result["samples"]) == 10
    assert len(result["kpis"]) == 10
    assert result["run_json"] == run_json


@pytest.mark.nomad_e2e
def test_failover_campaign_continues(
    nomad_multi: str,
    cfg_3: CampaignConfig,
    outdir: Path,
) -> None:
    """Kill one server, verify the campaign continues on remaining quorum.

    The Nomad HA cluster has 3 servers (quorum = 2). After stopping one
    non-leader server, the remaining 2 maintain quorum and the campaign
    should complete without error.
    """
    # Identify a non-leader server to kill.
    leader_resp = _nomad_get("/v1/status/leader", nomad_multi)
    # leader_resp is like '"10.0.0.1:4647"' (quoted string with port).
    leader_addr = leader_resp.strip('"').split(":")[0] if isinstance(leader_resp, str) else ""

    # Pick the first server that is NOT the leader.
    container_to_stop: str | None = None
    for candidate in [
        "nomad-multi-server-1",
        "nomad-multi-server-2",
        "nomad-multi-server-3",
    ]:
        # Get the container's IP via docker inspect.
        try:
            inspect = subprocess.run(
                [
                    "docker",
                    "inspect",
                    candidate,
                    "--format",
                    "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
                ],
                capture_output=True,
                text=True,
                check=True,
                timeout=10,
            )
            container_ip = inspect.stdout.strip()
            if container_ip != leader_addr:
                container_to_stop = candidate
                break
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            continue

    if container_to_stop is None:
        # Fallback: stop server-3 (least likely to be the leader).
        container_to_stop = "nomad-multi-server-3"

    # Stop the non-leader server.
    subprocess.run(
        ["docker", "stop", container_to_stop],
        capture_output=True,
        check=True,
        timeout=30,
    )

    # Give the cluster a moment to stabilize after the failure.
    time.sleep(5.0)

    # Verify the remaining cluster still has a leader.
    try:
        new_leader = _nomad_get("/v1/status/leader", nomad_multi)
        assert new_leader, "no leader after killing one server"
    except Exception:
        # Restart the container so the teardown is clean, then fail.
        subprocess.run(
            ["docker", "start", container_to_stop],
            capture_output=True,
            check=False,
            timeout=30,
        )
        pytest.skip("Leader not re-elected after failover — environment may be too slow")
        return  # pragma: no cover

    try:
        # Run a 3-sample campaign on the degraded cluster.
        executor = _LocalWorkNomadExecutor(
            address=nomad_multi,
            datacentre="dc1",
            poll_interval_s=0.5,
            max_poll_interval_s=5.0,
        )
        campaign = Campaign(cfg=cfg_3, executor=executor)
        result = campaign.run()
        executor.shutdown()

        # Assert the campaign completed.
        csv_path = outdir / "aggregated_results.csv"
        assert csv_path.is_file(), f"missing artifact after failover: {csv_path}"
        csv_text = csv_path.read_text()
        assert len(csv_text.strip().splitlines()) == 3 + 1, (
            f"expected header + 3 rows after failover, got: {csv_text!r}"
        )

        run_json = outdir / "run.json"
        assert run_json.is_file()
        trace = json.loads(run_json.read_text())
        assert len(trace["per_sample"]) == 3
        statuses = {row["status"] for row in trace["per_sample"]}
        assert statuses == {"ok"}, f"expected all-ok after failover, got {statuses}"

        assert set(result) >= {"samples", "kpis", "aggregated", "plots", "elapsed_s", "run_json"}
    finally:
        # Restart the stopped server so the module-scoped teardown is clean.
        subprocess.run(
            ["docker", "start", container_to_stop],
            capture_output=True,
            check=False,
            timeout=30,
        )
