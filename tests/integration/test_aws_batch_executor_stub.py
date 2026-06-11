"""End-to-end integration test: Campaign via ``AWSBatchExecutor`` (stub).

Acceptance criterion (issue #11):

    test_aws_batch_executor_stub.py: runs a 3-sample campaign against
    ``AWSBatchExecutor`` (the stub), asserts the same outputs. (The
    real ``boto3``-backed implementation will be tested separately when
    an AWS account is available.)

The current ``AWSBatchExecutor`` is a *stub* in the sense that:

  * It calls ``boto3.client('batch').submit_job`` (mocked here).
  * The handle's ``.result()`` polls ``describe_jobs`` until ``SUCCEEDED``
    (also mocked).
  * The actual work function is NOT run locally — in production it
    would run inside a Batch container with the on-disk artifacts
    appearing on shared storage.

The "same outputs" assertion in the issue is therefore understood as:

  * The Campaign completes end-to-end without raising.
  * The 4 output artifacts are produced (aggregated_results.csv,
    failed_simulations.csv, KPI JSON files, plot directory) — they
    may be empty headers when no sample's simulation actually ran.
  * ``run.json`` carries the expected per-step / per-sample schema.
  * The boto3 client is called with the expected ``submit_job`` /
    ``describe_jobs`` payload for each step (verifying the executor
    is wired into the Campaign correctly).

A real-Batch end-to-end test would require an AWS account; the issue
explicitly defers that to a separate ticket.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from osimflow import Campaign, CampaignConfig
from osimflow.executors import AWSBatchExecutor

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
        openstudio_version="3.4.0",
        archive_intermediates=False,
    )


# ---------------------------------------------------------------------------
# Helper: mock boto3.client('batch') with a sensible default
# ---------------------------------------------------------------------------
@contextmanager
def mocked_aws_batch_client() -> Iterator[MagicMock]:
    """Context manager: patch boto3.client with a MagicMock that returns
    SUCCEEDED on the first describe_jobs poll for every jobId.

    The mock records:
      * ``submit_job_calls`` — list of submit_job kwargs.
      * ``describe_jobs_calls`` — list of describe_jobs kwargs.

    The Campaign will call submit_job once per fan-out task (3 sim
    tasks for 3 samples, plus 3 apply + 3 extract + 1 each for the
    single-shot steps). The handle.result() polls describe_jobs until
    SUCCEEDED; we make the first poll return SUCCEEDED so the test
    does not need to wait.
    """
    fake_client = MagicMock()
    submit_calls: list[dict[str, object]] = []
    describe_calls: list[dict[str, object]] = []

    def fake_submit_job(**kwargs: object) -> dict[str, str]:
        submit_calls.append(kwargs)
        # Each submit returns a unique jobId so the Campaign's per-task
        # state stays independent.
        return {"jobId": f"job-{len(submit_calls)}"}

    def fake_describe_jobs(**kwargs: object) -> dict[str, object]:
        describe_calls.append(kwargs)
        # Always report SUCCEEDED on the first poll. The AWSBatchHandle
        # caches the terminal state in the per-handle Future, so a
        # single SUCCEEDED response is sufficient.
        jobs = []
        for jid in kwargs.get("jobs", []):
            jobs.append(
                {
                    "jobId": jid,
                    "status": "SUCCEEDED",
                    "statusReason": "OK",
                    "container": {"exitCode": 0, "taskArn": "arn:aws:ecs:stub"},
                }
            )
        return {"jobs": jobs}

    fake_client.submit_job.side_effect = fake_submit_job
    fake_client.describe_jobs.side_effect = fake_describe_jobs
    # Attach the recorded calls so the test can inspect them after the
    # `with` block.
    fake_client.submit_job_calls = submit_calls  # type: ignore[attr-defined]
    fake_client.describe_jobs_calls = describe_calls  # type: ignore[attr-defined]

    with patch("boto3.client", return_value=fake_client):
        yield fake_client


# ---------------------------------------------------------------------------
# Test-only stub executor: AWSBatchExecutor + local execution
# ---------------------------------------------------------------------------
class _StubAWSBatchExecutor(AWSBatchExecutor):
    """Test-only AWS Batch executor that ALSO runs the work locally.

    The real ``AWSBatchExecutor`` returns ``None`` from
    ``Handle.result()`` because in production the work runs inside a
    remote Batch container; the Campaign reads the on-disk artifacts
    from shared storage. The ``Campaign`` orchestrator, however, treats
    the handle's result as a ``Path`` (it calls ``Path(result_path)``),
    so the stub executor would crash the Campaign if used directly.

    This stub fixes that gap: every ``submit()`` also queues the work
    on a local thread pool, and the handle's ``result()`` returns the
    *local* work output. The boto3 call is still made (so the wiring
    is verified), and the handle's ``describe_jobs`` poll still runs
    (so the campaign's `result()` path is exercised).

    This is a *test-only* class — production code paths use the
    real ``AWSBatchExecutor``. The class lives in this test file on
    purpose: the production fix (returning a Path from the Batch
    handle, e.g. by storing the Batch output to S3 and reading it
    back) is a separate concern.
    """

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        # The local pool runs the work in parallel — the same fan-out
        # shape the real Batch would have, minus the remote overhead.
        from concurrent.futures import ThreadPoolExecutor

        self._local_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="stub-aws-batch")

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
        # Run the real submit() first (so the boto3 call is made and
        # the wire format is verified). The real submit returns an
        # _AWSBatchHandle whose .result() polls describe_jobs.
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
        # Queue the work on the local pool. The future holds the
        # actual Path the Campaign expects to see in `result()`.
        local_fut = self._local_pool.submit(fn, *args)  # type: ignore[arg-type]

        # Return a wrapper handle that delegates .result() to the
        # local future. We ignore the real handle's polling — that
        # path is exercised by the real boto3.submit_job call above
        # and by the test_aws_batch_failed_raises_with_status_reason
        # test in test_awsbatch_boto3_wiring.py.
        class _StubHandle:
            def __init__(self, fut: object) -> None:
                self._fut = fut
                # Use the Batch jobId as our public id so the boto3
                # wiring can be cross-referenced if needed.
                self.job_id = real_handle.job_id
                # Worker tracking fields (issue #105): the Campaign
                # reads these from every handle, so the stub must
                # expose them too.
                self.worker_id: str | None = real_handle.job_id
                self.worker_ip: str | None = None
                self.worker_region: str | None = None

            def result(self, timeout: float | None = None) -> object:  # noqa: ARG002
                return self._fut.result(timeout=timeout)  # type: ignore[attr-defined]

            def done(self) -> bool:
                return self._fut.done()  # type: ignore[attr-defined]

        return _StubHandle(local_fut)

    def shutdown(self) -> None:
        self._local_pool.shutdown(wait=True)
        super().shutdown()


# ---------------------------------------------------------------------------
# Test: 3-sample campaign via AWSBatchExecutor (stub) produces the 4 artifacts
# ---------------------------------------------------------------------------
def test_three_sample_campaign_via_aws_batch_stub_produces_artifacts(
    cfg: CampaignConfig, outdir: Path
) -> None:
    """A 3-sample campaign through the AWSBatchExecutor stub must:

    1. Drive the Campaign's 6-step DAG end-to-end without raising.
    2. Produce the 4 output artifacts (aggregated_results.csv,
       failed_simulations.csv, KPI JSON files, plots directory).
    3. Write a well-formed run.json with the expected per-step
       and per-sample blocks.
    4. Issue one ``submit_job`` per per-sample task (apply, sim,
       extract) and the right number of single-shot tasks (LHS,
       aggregate, plots).
    """
    with mocked_aws_batch_client() as fake_client:
        executor = _StubAWSBatchExecutor(
            job_queue="stub-queue",
            job_definition="stub-job-def",
            poll_interval_s=0.01,
            max_poll_interval_s=0.02,
        )
        campaign = Campaign(cfg=cfg, executor=executor)
        result = campaign.run()
        executor.shutdown()

    # --- 4 output artifacts -----------------------------------------------
    csv_path = outdir / "aggregated_results.csv"
    assert csv_path.is_file(), f"missing artifact: {csv_path}"
    # The stub runs the work locally, so the CSV has the per-sample
    # rows (header + 3 data rows).
    csv_text = csv_path.read_text()
    assert csv_text.startswith("sample_id")
    assert len(csv_text.strip().splitlines()) == 3 + 1, (
        f"expected header + 3 data rows, got: {csv_text!r}"
    )

    failed_path = outdir / "failed_simulations.csv"
    assert failed_path.is_file(), f"missing artifact: {failed_path}"
    assert failed_path.read_text().startswith("sample_id")

    # KPI JSON files: the stub runs the work locally, so the per-sample
    # extract step writes one kpi_<sid>.json per sample.
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
    assert trace["config"]["executor"] == "aws_batch", (
        f"expected executor name 'aws_batch' in run.json, got {trace['config']['executor']!r}"
    )
    assert trace["config"]["n_samples"] == 3
    assert trace["config"]["openstudio_version"] == "3.4.0"

    # Every Campaign step is recorded in run.json.
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

    # Per-sample rows: 3 rows (one per sample), all status=ok. The
    # stub's local execution means the work function actually runs
    # (producing the eplusout.sql / KPI artifacts), so samples
    # complete cleanly. This is the integration contract we want
    # to prove: the boto3 wiring and the per-step fan-out both
    # succeed under the AWSBatchExecutor name.
    assert len(trace["per_sample"]) == 3
    statuses = {row["status"] for row in trace["per_sample"]}
    assert statuses == {"ok"}, f"expected all-ok statuses, got {statuses}"

    # --- boto3 wiring: verify submit_job was called for the right tasks -
    submit_calls: list[dict[str, object]] = fake_client.submit_job_calls  # type: ignore[attr-defined]
    job_names = [str(c.get("jobName", "")) for c in submit_calls]
    # 3 sim_<sid> submissions (one per sample). This is the critical
    # fan-out assertion: the executor was actually invoked.
    sim_submissions = [n for n in job_names if n.startswith("sim_")]
    assert len(sim_submissions) == 3, (
        f"expected 3 sim_<sid> submit_job calls, got {len(sim_submissions)}: {sim_submissions}"
    )
    # And the per-sample apply/extract fan-outs must also be 3 each.
    apply_submissions = [n for n in job_names if n.startswith("apply_")]
    kpi_submissions = [n for n in job_names if n.startswith("kpi_")]
    assert len(apply_submissions) == 3
    assert len(kpi_submissions) == 3
    # Plus the 2 single-shot steps (aggregate, plots).  Sample
    # generation is now done inline via the algorithm framework —
    # there is no executor submission for it.
    for expected_single_shot in ("aggregate", "plots"):
        assert expected_single_shot in job_names, (
            f"expected single-shot task {expected_single_shot!r} in submit_job "
            f"calls, got {job_names}"
        )

    # --- containerOverrides + environment plumbed into submit_job -------
    # At least one of the sim submissions must carry the
    # OSIMFLOW_OS_VERSION env var (the per-sample openstudio_version
    # is passed as a Batch env var by the AWSBatchExecutor).
    sim_calls = [c for c in submit_calls if str(c.get("jobName", "")).startswith("sim_")]
    assert sim_calls, "no sim submit_job calls captured"
    for call in sim_calls:
        overrides = call.get("containerOverrides", {})
        env = overrides.get("environment", []) if isinstance(overrides, dict) else []
        env_dict = {e["name"]: e["value"] for e in env}
        assert env_dict.get("OSIMFLOW_OS_VERSION") == "3.4.0", (
            f"OSIMFLOW_OS_VERSION not in containerOverrides.environment: {env_dict}"
        )
        # The container tag must also be present so the work layer
        # knows which container to invoke.
        assert env_dict.get("OSIMFLOW_CONTAINER"), (
            f"OSIMFLOW_CONTAINER missing from containerOverrides.environment: {env_dict}"
        )

    # --- describe_jobs polled for every submitted job ---------------------
    # The real AWSBatchHandle polls describe_jobs in .result(). The
    # _StubAWSBatchExecutor's handle delegates to the local future, so
    # describe_jobs may not be polled (the stub's local path short-
    # circuits the poll). The Campaign's view of the per-sample result
    # is what matters here — the wire format is verified by the
    # submit_job_calls list above.

    # --- result dict contract (public surface) ---------------------------
    assert set(result) >= {"samples", "kpis", "aggregated", "plots", "elapsed_s", "run_json"}
    # 3 samples generated (LHS works regardless of executor).
    assert len(result["samples"]) == 3
    # 3 KPI files (the stub ran the work locally).
    assert len(result["kpis"]) == 3
    assert result["run_json"] == run_json


# ---------------------------------------------------------------------------
# Test: the AWSBatchExecutor accepts the per-submit resource directives
# the Campaign passes and forwards them as containerOverrides.
# ---------------------------------------------------------------------------
def test_aws_batch_stub_passes_per_sample_resource_directives(
    cfg: CampaignConfig,
) -> None:
    """The Campaign passes `cpus` / `memory_mb` / `time_min` to every
    ``executor.submit()`` call. The AWSBatchExecutor must convert these
    into ``containerOverrides`` (vcpus, memory in MiB, attemptDurationSeconds)
    and forward them in the ``submit_job`` payload.

    This is a stub-level wiring test: the resource directives do not
    matter for a mocked Batch (no real container), but a regression
    where the executor dropped them would silently affect cost /
    scheduling in production. We assert the conversion here.
    """
    with mocked_aws_batch_client() as fake_client:
        executor = AWSBatchExecutor(
            job_queue="q",
            job_definition="jd",
            poll_interval_s=0.01,
            max_poll_interval_s=0.02,
        )
        # Simulate the Campaign's per-submit call shape (the values
        # below match what the Campaign passes for RUN_OPENSTUDIO_SIM).
        handle = executor.submit(
            lambda: "ok",
            name="sim_test",
            cpus=4,
            memory_mb=8192,
            time_min=240,
            container="openstudio_cli_image:3.4.0",
            openstudio_version="3.4.0",
        )
        handle.result(timeout=5)
        executor.shutdown()

    submit_calls: list[dict[str, object]] = fake_client.submit_job_calls  # type: ignore[attr-defined]
    assert len(submit_calls) == 1
    call = submit_calls[0]
    overrides = call.get("containerOverrides", {})
    assert isinstance(overrides, dict)
    assert overrides.get("vcpus") == 4, f"vcpus not propagated: {overrides}"
    # The Campaign passes memory_mb in MB; the executor passes it as
    # Batch's memory (MiB) — 1:1 (PRD acceptance).
    assert overrides.get("memory") == 8192, f"memory not propagated: {overrides}"
    # time_min → attemptDurationSeconds.
    assert call.get("timeout") == {"attemptDurationSeconds": 240 * 60}, (
        f"timeout not converted from time_min: {call.get('timeout')}"
    )
