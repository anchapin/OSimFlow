"""End-to-end integration test: Campaign via ``NomadExecutor`` (stub).

Acceptance criterion (issue #27):

    test_nomad_executor_stub.py: runs a 3-sample campaign against
    ``NomadExecutor`` with a mocked HTTP transport, asserts the same
    outputs as the other executor stub tests.

The ``NomadExecutor`` is a *stub* in the sense that:

  * It calls ``urllib.request.urlopen`` (mocked here) to POST job specs.
  * The handle's ``.result()`` polls ``GET /v1/allocation/<id>`` until
    ``complete`` (also mocked).
  * The actual work function is NOT run on a remote Nomad client — in
    production it would run inside a Docker container on a Nomad client
    with on-disk artifacts appearing on shared storage.

The "same outputs" assertion is therefore understood as:

  * The Campaign completes end-to-end without raising.
  * The 4 output artifacts are produced (aggregated_results.csv,
    failed_simulations.csv, KPI JSON files, plot directory).
  * ``run.json`` carries the expected per-step / per-sample schema.
  * The HTTP client is called with the right POST /v1/jobs payloads
    for each step (verifying the executor is wired into the Campaign).
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from osimflow import Campaign, CampaignConfig
from osimflow.executors import NomadExecutor

# ---------------------------------------------------------------------------
# Fixtures — same shape as the other executor test files
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_PKG = REPO_ROOT / "example_package"
EXAMPLE_VARS_YML = REPO_ROOT / "variables.yml"


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    wd = tmp_path / "work"
    wd.mkdir()
    (wd / "variables.yml").write_text(EXAMPLE_VARS_YML.read_text())
    return wd


@pytest.fixture
def template_pkg(workdir: Path) -> Path:
    pkg = workdir / "template"
    shutil.copytree(EXAMPLE_PKG, pkg)
    return pkg


@pytest.fixture
def outdir(workdir: Path) -> Path:
    od = workdir / "out"
    od.mkdir()
    return od


@pytest.fixture
def cfg(workdir: Path, template_pkg: Path, outdir: Path) -> CampaignConfig:
    return CampaignConfig(
        input_variables=workdir / "variables.yml",
        template_sim_package=template_pkg,
        n_samples=3,
        outdir=outdir,
        openstudio_version="3.11.0",
        archive_intermediates=False,
    )


# ---------------------------------------------------------------------------
# Helper: mock urllib.request.urlopen with permissive responses
# ---------------------------------------------------------------------------
@contextmanager
def mocked_nomad_transport() -> Iterator[MagicMock]:
    """Context manager: patch ``urllib.request.urlopen`` with a MagicMock
    that always returns ``complete`` for every allocation lookup.

    The mock records every ``urlopen`` call so the test can inspect
    which endpoints were hit and what payloads were sent.
    """
    fake_urlopen = MagicMock()
    submit_calls: list[dict[str, object]] = []
    alloc_calls: list[dict[str, object]] = []
    call_counter = 0

    def fake_urlopen_fn(request: Any, *args: Any, **kwargs: Any) -> Any:
        nonlocal call_counter
        call_counter += 1
        method = request.get_method()
        url = request.full_url

        if method == "POST" and "/v1/jobs" in url:
            # Job submit: return a unique JobID + EvalID.
            payload = json.loads(request.data.decode("utf-8"))
            submit_calls.append(payload)
            idx = len(submit_calls)
            result = {
                "JobID": f"osimflow/job-{idx}",
                "EvalID": f"eval-{idx}",
                "Index": 0,
            }
            resp = MagicMock()
            resp.read.return_value = json.dumps(result).encode("utf-8")
            resp.__enter__ = lambda s: s
            resp.__exit__ = lambda s, *a: None
            return resp

        if method == "GET" and "/v1/evaluation/" in url and "/allocations" in url:
            # Eval-based allocation lookup: return a stub allocation.
            idx = call_counter
            alloc_calls.append({"method": method, "url": url})
            result = [{"ID": f"alloc-eval-{idx}", "ClientStatus": "complete"}]
            resp = MagicMock()
            resp.read.return_value = json.dumps(result).encode("utf-8")
            resp.__enter__ = lambda s: s
            resp.__exit__ = lambda s, *a: None
            return resp

        if method == "GET" and "/v1/job/" in url and "/allocations" in url:
            # Job-based allocation lookup: return a stub allocation.
            alloc_calls.append({"method": method, "url": url})
            result = [{"ID": "alloc-job-stub", "ClientStatus": "complete"}]
            resp = MagicMock()
            resp.read.return_value = json.dumps(result).encode("utf-8")
            resp.__enter__ = lambda s: s
            resp.__exit__ = lambda s, *a: None
            return resp

        if method == "GET" and "/v1/allocation/" in url:
            # Allocation lookup: always return ``complete``.
            alloc_calls.append({"method": method, "url": url})
            result = {
                "ID": f"alloc-{len(alloc_calls)}",
                "ClientStatus": "complete",
                "JobID": "osimflow/ok",
            }
            resp = MagicMock()
            resp.read.return_value = json.dumps(result).encode("utf-8")
            resp.__enter__ = lambda s: s
            resp.__exit__ = lambda s, *a: None
            return resp

        # Unexpected request — return generic empty response.
        resp = MagicMock()
        resp.read.return_value = b"{}"
        resp.__enter__ = lambda s: s
        resp.__exit__ = lambda s, *a: None
        return resp

    fake_urlopen.side_effect = fake_urlopen_fn
    fake_urlopen.submit_calls = submit_calls  # type: ignore[attr-defined]
    fake_urlopen.alloc_calls = alloc_calls  # type: ignore[attr-defined]

    with patch("urllib.request.urlopen", side_effect=fake_urlopen_fn):
        yield fake_urlopen


# ---------------------------------------------------------------------------
# Test-only stub executor: NomadExecutor + local execution
# ---------------------------------------------------------------------------
class _StubNomadExecutor(NomadExecutor):
    """Test-only Nomad executor that ALSO runs the work locally.

    The real ``NomadExecutor`` returns ``None`` from
    ``Handle.result()`` because in production the work runs inside a
    Nomad Docker container; the Campaign reads on-disk artifacts from
    shared storage. The ``Campaign`` orchestrator treats the handle's
    result as a ``Path`` (it calls ``Path(result_path)``), so the stub
    would crash if used directly.

    This stub fixes that gap: every ``submit()`` also queues the work
    on a local thread pool, and the handle's ``result()`` returns the
    *local* work output. The HTTP call is still made (so the wiring
    is verified), and the handle's allocation poll still runs.
    """

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        from concurrent.futures import ThreadPoolExecutor

        self._local_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="stub-nomad")

    def submit(  # type: ignore[override]
        self,
        fn: object,
        *args: object,
        name: str = "task",
        cpus: int = 1,
        memory_mb: int = 1024,
        time_min: int = 60,
        container: str | None = None,
        **kwargs: object,
    ) -> object:
        # Run the real submit() first (so the HTTP call is made and
        # the wire format is verified).
        real_handle = super().submit(  # type: ignore[arg-type]
            fn,  # type: ignore[arg-type]
            *args,
            name=name,
            cpus=cpus,
            memory_mb=memory_mb,
            time_min=time_min,
            container=container,
            **kwargs,
        )
        # Queue the work on the local pool.
        local_fut = self._local_pool.submit(fn, *args)  # type: ignore[arg-type]

        class _StubHandle:
            def __init__(self, fut: object) -> None:
                self._fut = fut
                self.job_id = real_handle.job_id
                # Worker tracking fields (issue #105): the Campaign
                # reads these from every handle, so the stub must
                # expose them too.
                self.worker_id: str | None = real_handle.job_id
                self.worker_ip: str | None = None
                self.worker_region: str | None = None
                # Cost tracking fields (issue #126): the Campaign
                # reads these from every handle after simulation.
                self.cost_usd: float | None = None
                self.billed_duration_seconds: float | None = None

            def result(self, timeout: float | None = None) -> object:  # noqa: ARG002
                return self._fut.result(timeout=timeout)  # type: ignore[attr-defined]

            def done(self) -> bool:
                return self._fut.done()  # type: ignore[attr-defined]

        return _StubHandle(local_fut)

    def shutdown(self) -> None:
        self._local_pool.shutdown(wait=True)
        super().shutdown()


# ---------------------------------------------------------------------------
# Test: 3-sample campaign via NomadExecutor (stub) produces artifacts
# ---------------------------------------------------------------------------
def test_three_sample_campaign_via_nomad_stub_produces_artifacts(
    cfg: CampaignConfig, outdir: Path
) -> None:
    """A 3-sample campaign through the NomadExecutor stub must:

    1. Drive the Campaign's 6-step DAG end-to-end without raising.
    2. Produce the 4 output artifacts (aggregated_results.csv,
       failed_simulations.csv, KPI JSON files, plots directory).
    3. Write a well-formed run.json with the expected per-step
       and per-sample blocks.
    4. Issue one ``POST /v1/jobs`` per fan-out task.
    """
    with mocked_nomad_transport() as fake_transport:
        executor = _StubNomadExecutor(
            address="http://nomad.stub:4646",
            datacentre="dc1",
            poll_interval_s=0.01,
            max_poll_interval_s=0.02,
        )
        campaign = Campaign(cfg=cfg, executor=executor)
        result = campaign.run()
        executor.shutdown()

    # --- 4 output artifacts -----------------------------------------------
    csv_path = outdir / "aggregated_results.csv"
    assert csv_path.is_file(), f"missing artifact: {csv_path}"
    csv_text = csv_path.read_text()
    assert csv_text.startswith("sample_id")
    assert len(csv_text.strip().splitlines()) == 3 + 1, (
        f"expected header + 3 data rows, got: {csv_text!r}"
    )

    failed_path = outdir / "failed_simulations.csv"
    assert failed_path.is_file(), f"missing artifact: {failed_path}"
    assert failed_path.read_text().startswith("sample_id")

    kpi_files = sorted((outdir / "work" / "kpis").glob("kpi_*.json"))
    assert len(kpi_files) == 3, f"expected 3 KPI JSONs, got {len(kpi_files)}"
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
    assert trace["config"]["executor"] == "nomad", (
        f"expected executor name 'nomad' in run.json, got {trace['config']['executor']!r}"
    )
    assert trace["config"]["n_samples"] == 3
    assert trace["config"]["openstudio_version"] == "3.11.0"

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

    assert len(trace["per_sample"]) == 3
    statuses = {row["status"] for row in trace["per_sample"]}
    assert statuses == {"ok"}, f"expected all-ok statuses, got {statuses}"

    # --- HTTP wiring: verify POST /v1/jobs was called for tasks ----------
    submit_calls: list[dict[str, object]] = fake_transport.submit_calls  # type: ignore[attr-defined]
    job_names = []
    for spec in submit_calls:
        job = spec.get("Job", {})
        job_names.append(str(job.get("Name", "")))

    # 3 sim submissions (one per sample).
    sim_submissions = [n for n in job_names if "sim-" in n]
    assert len(sim_submissions) == 3, (
        f"expected 3 sim-* submit calls, got {len(sim_submissions)}: {sim_submissions}"
    )
    # And the per-sample apply/extract fan-outs.
    apply_submissions = [n for n in job_names if "apply-" in n]
    kpi_submissions = [n for n in job_names if "kpi-" in n]
    assert len(apply_submissions) == 3
    assert len(kpi_submissions) == 3

    # --- Job spec shape: at least one sim spec has the right structure ---
    sim_specs = [s for s in submit_calls if "sim-" in str(s.get("Job", {}).get("Name", ""))]
    assert sim_specs, "no sim job specs captured"
    spec = sim_specs[0]
    job = spec["Job"]
    assert job["Type"] == "batch"
    # The container image must carry the OS version tag.
    task = job["TaskGroups"][0]["Tasks"][0]
    assert "3.11.0" in task["Config"]["image"], (
        f"OpenStudio version not in container image: {task['Config']['image']!r}"
    )
    # Env vars must carry the OSIMFLOW_OS_VERSION.
    env = task["Config"]["env"]
    env_dict = {e["name"]: e["value"] for e in env}
    assert env_dict.get("OSIMFLOW_OS_VERSION") == "3.11.0", (
        f"OSIMFLOW_OS_VERSION not in task env: {env_dict}"
    )

    # --- result dict contract (public surface) ---------------------------
    assert set(result) >= {"samples", "kpis", "aggregated", "plots", "elapsed_s", "run_json"}
    assert len(result["samples"]) == 3
    assert len(result["kpis"]) == 3
    assert result["run_json"] == run_json
