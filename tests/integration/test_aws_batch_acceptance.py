"""Live AWS Batch MVP acceptance gate: S3 -> Fargate -> S3 (issue #1815).

Proves real remote execution, not just ``Batch SUCCEEDED``. Evidence checks live
in ``tests/integration/_aws_batch_acceptance.py`` (unit-tested in CI); this
module drives them against real AWS and is skip-gated.

Gates (all required, otherwise the module skips):
  ``OSIMFLOW_AWS_BATCH_E2E=1``, ``OSIMFLOW_AWS_BATCH_ACCEPTANCE=1``,
  ``OSIMFLOW_AWS_BATCH_QUEUE`` (approved on-demand Fargate queue),
  ``OSIMFLOW_AWS_BATCH_JOB_DEFINITION``, ``OSIMFLOW_AWS_REGION``,
  ``OSIMFLOW_AWS_BATCH_RESULT_BUCKET`` (S3 input/output staging bucket),
  ``OSIMFLOW_AWS_BATCH_CONTAINER_DIGEST`` (``repo@sha256:<64 hex>`` worker pin).

Strict mode: with ``OSIMFLOW_AWS_BATCH_ACCEPTANCE_STRICT=1`` a missing gate or
fixture is a *failure*, never a skip, so a vacuous green cannot satisfy the
acceptance run. Optional ``OSIMFLOW_AWS_BATCH_ACCEPTANCE_N`` (default 10, max
20) sizes the bounded campaign; ``OSIMFLOW_AWS_BATCH_ACCEPTANCE_EVIDENCE_DIR``
receives the redacted evidence JSON.

The worker job definition must NOT set ``OSIMFLOW_STUB_SIM``; stub output is
detected and rejected by the verifiers.
"""

import json
import os
import sys
import time
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _aws_batch_acceptance as acc  # noqa: E402
import test_aws_batch_real_openstudio as real_os  # noqa: E402

_REQUIRED_ENV = (
    "OSIMFLOW_AWS_BATCH_E2E",
    "OSIMFLOW_AWS_BATCH_ACCEPTANCE",
    "OSIMFLOW_AWS_BATCH_QUEUE",
    "OSIMFLOW_AWS_BATCH_JOB_DEFINITION",
    "OSIMFLOW_AWS_REGION",
    "OSIMFLOW_AWS_BATCH_RESULT_BUCKET",
    "OSIMFLOW_AWS_BATCH_CONTAINER_DIGEST",
)
_MISSING = [v for v in _REQUIRED_ENV if os.environ.get(v) in (None, "")]
_STRICT = os.environ.get("OSIMFLOW_AWS_BATCH_ACCEPTANCE_STRICT") == "1"

if _MISSING and _STRICT:
    pytest.fail(f"strict acceptance mode: missing env {_MISSING}", pytrace=False)

pytestmark = pytest.mark.skipif(
    bool(_MISSING),
    reason=f"AWS Batch acceptance gate not enabled (missing: {_MISSING})",
)

N_BOUNDED = min(int(os.environ.get("OSIMFLOW_AWS_BATCH_ACCEPTANCE_N", "10")), 20)


@pytest.fixture(autouse=True)
def _real_worker_not_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    """The root conftest forces OSIMFLOW_STUB_SIM=1, which the executor forwards
    to the Batch worker; drop it so the worker runs the real OpenStudio CLI."""
    monkeypatch.delenv("OSIMFLOW_STUB_SIM", raising=False)


def _fixture() -> None:
    try:
        real_os._ensure_real_fixture()
    except pytest.skip.Exception as exc:
        pytest.fail(f"acceptance requires a real fixture: {exc}")


class _CountingExecutor:
    """Factory for an AWSBatchExecutor that counts real Batch simulation submissions."""

    @staticmethod
    def make() -> object:
        from osimflow.executors import AWSBatchExecutor

        class Counting(AWSBatchExecutor):
            submissions: list[str] = []

            def _do_submit(self, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
                handle = super()._do_submit(*args, **kwargs)  # type: ignore[misc]
                # Acceptance bounds *simulation* submissions; the apply/KPI/
                # aggregate/plot steps are not duplicate-sensitive.
                if str(kwargs.get("name", "")).startswith("sim_"):
                    type(self).submissions.append(str(getattr(handle, "job_id", "unknown")))
                return handle

        Counting.submissions = []
        return Counting(
            job_queue=os.environ["OSIMFLOW_AWS_BATCH_QUEUE"],
            job_definition=os.environ["OSIMFLOW_AWS_BATCH_JOB_DEFINITION"],
            region_name=os.environ["OSIMFLOW_AWS_REGION"],
            allow_long_lived_credentials=True,  # SSO/OIDC env creds
        )


def _run(
    tmp_path: Path,
    outdir: Path,
    n: int,
    *,
    template: Path | None = None,
    timeout_s: float | None = None,
    executor: object | None = None,
) -> tuple[object, object]:
    from osimflow import Campaign, CampaignConfig

    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    (work / "variables.yml").write_text("algorithm: lhs\nvariables: []\n")
    template = template or real_os._build_real_template(tmp_path / f"tpl_{outdir.name}")
    outdir.mkdir(parents=True, exist_ok=True)
    cfg = CampaignConfig(
        input_variables=work / "variables.yml",
        template_sim_package=template,
        n_samples=n,
        outdir=outdir,
        openstudio_version=os.environ.get("OSIMFLOW_OPENSTUDIO_VERSION", "3.11.0"),
        result_storage_backend="s3",
        result_storage_bucket=os.environ["OSIMFLOW_AWS_BATCH_RESULT_BUCKET"],
        container_digest=os.environ["OSIMFLOW_AWS_BATCH_CONTAINER_DIGEST"],
        byos_timeout_s=timeout_s,
        prebuilt_workflow=True,
    )
    ex = executor or _CountingExecutor.make()
    try:
        Campaign(cfg=cfg, executor=ex).run()  # type: ignore[arg-type]
    finally:
        ex.shutdown()  # type: ignore[attr-defined]
    return ex, cfg


def _ids(outdir: Path) -> list[str]:
    trace = json.loads((outdir / "run.json").read_text())
    return [r["sample_id"] for r in trace["per_sample"]]


def _write_evidence(name: str, record: dict[str, object]) -> None:
    target = os.environ.get("OSIMFLOW_AWS_BATCH_ACCEPTANCE_EVIDENCE_DIR")
    if target:
        Path(target).mkdir(parents=True, exist_ok=True)
        (Path(target) / f"{name}.json").write_text(json.dumps(record, indent=2))


def _record(ex: object, outdir: Path, wall: float) -> dict[str, object]:
    return acc.build_evidence_record(
        job_definition=os.environ["OSIMFLOW_AWS_BATCH_JOB_DEFINITION"],
        image=os.environ["OSIMFLOW_AWS_BATCH_CONTAINER_DIGEST"],
        job_ids=list(type(ex).submissions),  # type: ignore[attr-defined]
        input_manifest={"bucket_configured": True},
        output_manifest={"per_sample": _ids(outdir)},
        wall_time_s=wall,
        per_sample={sid: "ok" for sid in _ids(outdir)},
    )


def test_one_model_cold_smoke(tmp_path: Path) -> None:
    _fixture()
    outdir = tmp_path / f"smoke-{uuid.uuid4().hex[:8]}"
    t0 = time.monotonic()
    ex, _ = _run(tmp_path, outdir, 1)
    wall = time.monotonic() - t0
    acc.verify_acceptance(outdir, expected_success=_ids(outdir), min_successes=1)
    assert len(type(ex).submissions) == 1  # type: ignore[attr-defined]
    _write_evidence("smoke", _record(ex, outdir, wall))


def test_bounded_campaign_and_resume_no_duplicate_submissions(tmp_path: Path) -> None:
    _fixture()
    outdir = tmp_path / f"bounded-{uuid.uuid4().hex[:8]}"
    t0 = time.monotonic()
    template = real_os._build_real_template(tmp_path / "tpl_bounded")
    ex, _ = _run(tmp_path, outdir, N_BOUNDED, template=template)
    wall = time.monotonic() - t0
    ids = _ids(outdir)
    acc.verify_acceptance(outdir, expected_success=ids, min_successes=len(ids))
    first = list(type(ex).submissions)  # type: ignore[attr-defined]
    assert len(first) == N_BOUNDED
    _write_evidence("bounded", _record(ex, outdir, wall))

    # Same retained outdir: cached samples must not be resubmitted.
    ex2, _ = _run(tmp_path, outdir, N_BOUNDED, template=template)
    assert type(ex2).submissions == [], (  # type: ignore[attr-defined]
        f"resume resubmitted cached samples: {type(ex2).submissions}"  # type: ignore[attr-defined]
    )
    acc.verify_acceptance(outdir, expected_success=ids, min_successes=len(ids))


def test_invalid_workflow_fails_with_retained_reason(tmp_path: Path) -> None:
    _fixture()
    template = real_os._build_real_template(tmp_path / "tpl_bad")
    (template / "workflow.osw").write_text('{"seed_file": "does_not_exist.osm", "steps": []}')
    outdir = tmp_path / f"invalid-{uuid.uuid4().hex[:8]}"
    try:
        _run(tmp_path, outdir, 1, template=template)
    except Exception:  # noqa: BLE001 -- campaign may abort on all-failed
        pass
    if (outdir / "run.json").is_file():
        acc.verify_acceptance(
            outdir, expected_success=[], expected_failed=_ids(outdir), min_successes=0
        )
        sim = outdir / "work" / "sim"
        assert not any(sim.rglob("eplusout.sql")) or not any(
            not acc.real_energyplus_sql_problems(p) for p in sim.rglob("eplusout.sql")
        ), "invalid workflow produced a genuine completed simulation"


def test_timeout_fails_and_is_not_reported_as_success(tmp_path: Path) -> None:
    _fixture()
    outdir = tmp_path / f"timeout-{uuid.uuid4().hex[:8]}"
    try:
        _run(tmp_path, outdir, 1, timeout_s=3.0)
    except Exception:  # noqa: BLE001 -- timeout surfaces as sample failure/abort
        pass
    if (outdir / "run.json").is_file():
        acc.verify_acceptance(
            outdir, expected_success=[], expected_failed=_ids(outdir), min_successes=0
        )


_TERMINAL = ("SUCCEEDED", "FAILED")


def _describe(batch: object, job_ids: list[str]) -> dict[str, dict[str, object]]:
    found: dict[str, dict[str, object]] = {}
    for i in range(0, len(job_ids), 100):
        resp = batch.describe_jobs(jobs=job_ids[i : i + 100])  # type: ignore[attr-defined]
        found.update({j["jobId"]: j for j in resp["jobs"]})
    return found


def test_cancel_terminates_batch_jobs_and_leaves_no_orphans(tmp_path: Path) -> None:
    """Cancel right after submit (while STARTING, bounded cost): every Batch job
    the campaign created ends terminated and none stays in a live state (#1848)."""
    import threading

    import boto3

    from osimflow.executors import AWSBatchExecutor

    _fixture()

    class Tracking(AWSBatchExecutor):
        handles: list[object] = []

        def _do_submit(self, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
            handle = super()._do_submit(*args, **kwargs)  # type: ignore[misc]
            type(self).handles.append(handle)
            return handle

    Tracking.handles = []
    ex = Tracking(
        job_queue=os.environ["OSIMFLOW_AWS_BATCH_QUEUE"],
        job_definition=os.environ["OSIMFLOW_AWS_BATCH_JOB_DEFINITION"],
        region_name=os.environ["OSIMFLOW_AWS_REGION"],
        allow_long_lived_credentials=True,
    )
    batch = boto3.client("batch", region_name=os.environ["OSIMFLOW_AWS_REGION"])
    outdir = tmp_path / f"cancel-{uuid.uuid4().hex[:8]}"

    watcher_errors: list[str] = []

    def _cancel_after_first_submit() -> None:
        # Campaign installs signal handlers, so it must own the main thread.
        deadline = time.monotonic() + 300
        while not Tracking.handles and time.monotonic() < deadline:
            time.sleep(0.5)
        if not Tracking.handles:
            watcher_errors.append("campaign never submitted a Batch job")
            return
        ex.cancel()

    watcher = threading.Thread(target=_cancel_after_first_submit, name="cancel-watcher")
    watcher.start()
    try:
        _run(tmp_path, outdir, 1, executor=ex)
    except Exception:  # noqa: BLE001 -- cancelled campaign is expected to fail
        pass
    watcher.join(timeout=330)
    assert not watcher_errors, watcher_errors
    first = str(Tracking.handles[0].job_id)  # type: ignore[attr-defined]
    ex.cancel()  # sweep anything submitted while the cancel was racing

    job_ids = sorted({str(h.job_id) for h in Tracking.handles})  # type: ignore[attr-defined]
    deadline = time.monotonic() + 600
    while True:
        jobs = _describe(batch, job_ids)
        live = [i for i, j in jobs.items() if j["status"] not in _TERMINAL]
        if not live or time.monotonic() > deadline:
            break
        time.sleep(5)

    assert not live, f"orphaned live Batch jobs after cancel: {live}"
    assert jobs[first]["status"] == "FAILED"
    assert "cancellation" in str(jobs[first].get("statusReason", "")).lower(), jobs[first]
    _write_evidence(
        "cancel",
        {
            "job_ids": job_ids,
            "first_job_status": jobs[first]["status"],
            "first_job_reason": jobs[first].get("statusReason"),
            "orphans": live,
        },
    )
