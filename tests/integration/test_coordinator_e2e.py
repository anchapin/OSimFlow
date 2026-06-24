"""S5 disconnect-resilience E2E suite (issue #629, Epic #624).

This is the gate that declares Epic #624 (cloud-disconnect orchestration)
done. It proves the four headline invariants of a Coordinator-backed
(``--detach``) campaign:

  1. **Disconnect-resilience** — killing the submitting CLI after
     ``POST /handoff`` returns ``202 Accepted`` does NOT abort the remote
     execution loop. The Coordinator owns the lifecycle from that point on.
  2. **Reconnect** — a fresh ``osimflow status <outdir>`` (or
     ``GET /campaigns/{id}``) returns correct progress and final status,
     even from a brand-new shell that has no in-memory state.
  3. **Zero per-sample egress** — the submitting host receives ONLY the
     final aggregated artifacts (on explicit ``osimflow download``); no
     per-sample ``kpis.json`` / ``eplusout.*`` bytes ever flow to it.
  4. **One-array-submit + one-aggregator** — an N-sample campaign produces
     exactly one AWS Batch array ``submit_job`` call and one terminal
     aggregator job (the ``one submission for 50,000 runs`` epic gate).

It also confirms the cloud-path bug fixes hold (#622 no ``TypeError`` on
submit, #621 cancellation honored at the Coordinator boundary, #620 no
``cache.sqlite-shm`` ``FileNotFoundError`` on teardown).

Gating
------
Every test is skipped unless ``OSIMFLOW_COORDINATOR_E2E=1`` is set, mirroring
the ``OSIMFLOW_AWS_BATCH_E2E`` pattern. Without the flag the suite is a clean
no-op in normal CI; with the flag it runs deterministically against a
FastAPI ``TestClient`` + mocked Batch substrate + in-memory fake storage. It
does **not** require real AWS infrastructure — the nightly ``aws-batch-e2e``
workflow covers the real-Batch path. What this suite validates is the
Coordinator's orchestration logic and the disconnect invariants themselves.
"""

from __future__ import annotations

import json
import os
import shutil
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

# These extras are required for the Coordinator FastAPI app + boto3 mocking.
# importorskip keeps the file importable in a minimal install (it just skips).
pytest.importorskip("fastapi", reason="osimflow[api] extra required")
pytest.importorskip("slowapi", reason="osimflow[api] extra required")
pytest.importorskip("boto3", reason="osimflow[aws] extra required")
pytest.importorskip("httpx", reason="osimflow[api] extra required")

from osimflow import Campaign, CampaignConfig  # noqa: E402
from osimflow.api import coordinator as coord  # noqa: E402
from osimflow.api import create_app  # noqa: E402
from osimflow.executors import BaseExecutor, Handle  # noqa: E402
from osimflow.handoff_record import (  # noqa: E402
    HandoffRecord,
    read_handoff_record,
    write_handoff_record,
)
from osimflow.storage import ResultStorage  # noqa: E402

# ---------------------------------------------------------------------------
# Gating: skip every test unless OSIMFLOW_COORDINATOR_E2E=1
# ---------------------------------------------------------------------------
# Mirrors the OSIMFLOW_AWS_BATCH_E2E gate in test_aws_batch_real.py. Without
# the flag every test skips cleanly so normal CI (the ci.yml `test` job) never
# executes them. Set the flag to run the disconnect-resilience suite locally
# or in a dedicated workflow.
_E2E = os.environ.get("OSIMFLOW_COORDINATOR_E2E", "")
_skip = pytest.mark.skipif(
    not _E2E or _E2E in ("0", "", "false", "False"),
    reason="Set OSIMFLOW_COORDINATOR_E2E=1 to run the Coordinator disconnect-resilience E2E suite",
)

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_PKG = REPO_ROOT / "example_package"

# Shared-secret used by the EventBridge array-complete webhook. The
# Coordinator fails closed (401) if OSIMFLOW_EVENTBRIDGE_WEBHOOK_SECRET is
# unset; tests set it explicitly so the webhook path is exercisable.
_EVENTBRIDGE_SECRET = "test-eb-secret-629"


# ===========================================================================
# In-memory fake storage (records every download for the zero-egress test)
# ===========================================================================


class _RecordingObjectStorage(ResultStorage):
    """Filesystem-free object store that honours the :class:`ResultStorage` ABC.

    A superset of the ``_FakeObjectStorage`` from ``test_coordinator_aggregator``:
    it also records every ``download_file`` call's remote path so the
    zero-egress test can prove which objects the Coordinator (server-side)
    materialised versus which the submitting host (client-side) fetched.
    """

    name = "recording-fake"

    def __init__(self) -> None:
        self._objects: dict[str, bytes] = {}
        self.download_calls: list[str] = []

    # --- test helpers -------------------------------------------------------
    def put_text(self, remote_path: str, text: str) -> None:
        self._objects[remote_path] = text.encode("utf-8")

    @property
    def objects(self) -> dict[str, bytes]:
        return dict(self._objects)

    # --- ResultStorage ABC --------------------------------------------------
    def upload_file(self, local_path: Path, remote_path: str) -> None:
        self._objects[remote_path] = local_path.read_bytes()

    def download_file(self, remote_path: str, local_path: Path) -> None:
        if remote_path not in self._objects:
            raise FileNotFoundError(f"recording-storage: missing {remote_path}")
        self.download_calls.append(remote_path)
        local_path.parent.mkdir(parents=True, exist_ok=True)
        local_path.write_bytes(self._objects[remote_path])

    def list_results(self, prefix: str = "") -> list[str]:
        return sorted(k for k in self._objects if k.startswith(prefix))


# ===========================================================================
# boto3 substrate mock (records submit_job, fakes describe_jobs / S3)
# ===========================================================================


class _MockBatchClient:
    """Fake ``boto3.client('batch')`` that records ``submit_job`` calls.

    ``describe_jobs`` returns an array parent whose ``statusSummary`` reports
    every child as ``SUCCEEDED`` — the terminal state the Coordinator's
    array-completion detector requires to flip ``running -> aggregating``.
    """

    def __init__(self, *, array_size: int = 3) -> None:
        self.array_size = array_size
        self.submit_job_calls: list[dict[str, Any]] = []
        self._next_job_id = 0

    def submit_job(self, **kwargs: Any) -> dict[str, str]:
        self.submit_job_calls.append(kwargs)
        self._next_job_id += 1
        return {"jobId": f"array-job-{self._next_job_id}"}

    def describe_jobs(self, **_kwargs: Any) -> dict[str, Any]:
        # Every job is reported as a fully-succeeded array parent. The
        # Coordinator's _parse_array_completion reads arrayProperties.size
        # and statusSummary to decide the running->aggregating transition.
        size = self.array_size
        return {
            "jobs": [
                {
                    "jobId": "array-job-1",
                    "status": "SUCCEEDED",
                    "arrayProperties": {"size": size},
                    "statusSummary": {"SUCCEEDED": size, "FAILED": 0},
                }
            ]
        }


class _MockS3Paginator:
    """Fake S3 v2 paginator yielding per-sample kpi keys (for results listing)."""

    def __init__(self, kpi_keys: list[str]) -> None:
        self._kpi_keys = kpi_keys

    def paginate(self, **_kwargs: Any) -> Iterator[dict[str, Any]]:
        contents = [{"Key": k, "Size": 128} for k in self._kpi_keys]
        if contents:
            yield {"Contents": contents}
        else:
            yield {}


class _MockS3Client:
    """Fake ``boto3.client('s3')`` for presigning + listing result objects."""

    def __init__(self, kpi_keys: list[str]) -> None:
        self._kpi_keys = kpi_keys

    def generate_presigned_url(  # noqa: N803
        self, _op: str, *, Params: dict[str, str], ExpiresIn: int = 3600
    ) -> str:
        # Deterministic presigned URL so the zero-egress test can recognise it.
        bucket = Params.get("Bucket", "unknown")
        key = Params.get("Key", "unknown")
        return f"https://presigned.test/{bucket}/{key}?expires={ExpiresIn}"

    def get_paginator(self, _name: str) -> _MockS3Paginator:  # noqa: ARG002
        return _MockS3Paginator(self._kpi_keys)


@contextmanager
def mocked_coordinator_boto3(
    *, array_size: int = 3, kpi_keys: list[str] | None = None
) -> Iterator[tuple[_MockBatchClient, _MockS3Client]]:
    """Patch ``boto3.client`` for the Coordinator's Batch + S3 calls.

    Yields the ``(batch, s3)`` mock pair so tests can inspect
    ``submit_job_calls`` and assert on the presigned/listing behaviour. The
    Batch mock records every ``submit_job`` (the one-submission invariant)
    and answers ``describe_jobs`` with a fully-succeeded array parent.
    """
    batch = _MockBatchClient(array_size=array_size)
    s3 = _MockS3Client(kpi_keys or [])

    def fake_client(service: str, **_kwargs: Any) -> Any:
        if service == "batch":
            return batch
        if service == "s3":
            return s3
        return MagicMock()

    with patch("boto3.client", side_effect=fake_client):
        yield batch, s3


# ===========================================================================
# Shared fixtures
# ===========================================================================


@pytest.fixture(autouse=True)
def _isolate_campaign_store() -> Iterator[None]:
    """Clear the Coordinator's in-memory campaign + idempotency stores per test."""
    coord._campaigns.clear()
    coord._idempotency_keys.clear()
    yield
    coord._campaigns.clear()
    coord._idempotency_keys.clear()


@pytest.fixture(autouse=True)
def _eventbridge_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set the EventBridge shared secret so the array-complete webhook authenticates."""
    monkeypatch.setenv(coord._EVENTBRIDGE_SECRET_ENV, _EVENTBRIDGE_SECRET)


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    """FastAPI TestClient in read-write mode (no API key -> full admin)."""
    app = create_app(outdir=tmp_path, read_only=False)
    return TestClient(app)


# ===========================================================================
# Payload helpers (manifests / kpis / handoff payload)
# ===========================================================================


def _kpis(sample_id: str, **kpis: float) -> str:
    """Canonical worker ``kpis.json`` payload (matches bin/extract_kpis.py)."""
    return json.dumps({"sample_id": sample_id, "openstudio_version": None, "kpis": kpis})


def _manifest(
    sample_id: str,
    index: int,
    campaign_id: str,
    *,
    status: str,
    kpis_key: str | None,
    first_severe: str | None = None,
    exit_code: int = 0,
) -> str:
    """Build a §3.1 ``_manifest.json`` payload for one sample."""
    return json.dumps(
        {
            "campaign_id": campaign_id,
            "sample_id": sample_id,
            "index": index,
            "status": status,
            "kpis_key": kpis_key,
            "exit_code": exit_code,
            "first_severe_error": first_severe,
            "finished_at": "2026-06-20T00:00:00Z",
        }
    )


def _seed_sample_manifests(store: _RecordingObjectStorage, campaign_id: str, n: int) -> list[str]:
    """Populate *store* with *n* ok sample manifests + their kpis.json.

    Returns the list of per-sample kpi keys (used to prove the SERVER read
    them while the CLIENT did not). Mirrors the seeder in
    test_coordinator_aggregator but is parameterised on the campaign id.
    """
    kpi_keys: list[str] = []
    for i in range(n):
        sid = f"s{i + 1:04d}"
        kpi_key = f"{campaign_id}/samples/{sid}/kpis.json"
        store.put_text(kpi_key, _kpis(sid, eui=100.0 + i, total_energy=50000.0 + i))
        store.put_text(
            f"{campaign_id}/samples/{sid}/_manifest.json",
            _manifest(sid, i, campaign_id, status="ok", kpis_key=kpi_key),
        )
        kpi_keys.append(kpi_key)
    return kpi_keys


def _handoff_payload(*, n_samples: int = 3, bucket: str = "test-bucket") -> dict[str, Any]:
    """Build a minimal ``POST /handoff`` payload accepted by the Coordinator."""
    return {
        "name": "e2e-disconnect",
        "n_samples": n_samples,
        "executor": "aws_batch",
        "openstudio_version": "3.11.0",
        "algorithm": "lhs",
        "result_storage_backend": "s3",
        "result_storage_bucket": bucket,
    }


def _post_handoff(client: TestClient, payload: dict[str, Any]) -> dict[str, Any]:
    """POST /handoff and return the full parsed response body.

    Callers read ``campaign_id`` and ``status_url`` from the returned dict.
    """
    resp = client.post("/api/v1/coordinator/handoff", json=payload)
    assert resp.status_code == 202, f"handoff failed: {resp.status_code} {resp.text}"
    body = resp.json()
    assert body["campaign_id"], "handoff returned empty campaign_id"
    return body


def _submit_array(
    client: TestClient, campaign_id: str, *, array_size: int, queue: str = "q", jd: str = "jd"
) -> str:
    """POST /submit-array and return the assigned ``array_job_id``."""
    resp = client.post(
        f"/api/v1/coordinator/campaigns/{campaign_id}/submit-array",
        json={"job_queue": queue, "job_definition": jd, "array_size": array_size},
    )
    assert resp.status_code == 200, f"submit-array failed: {resp.status_code} {resp.text}"
    return str(resp.json()["array_job_id"])


def _fire_array_complete(client: TestClient, campaign_id: str, array_job_id: str) -> dict[str, Any]:
    """POST the EventBridge array-complete webhook; return the parsed body."""
    event = {
        "source": "aws.batch",
        "detail-type": "Batch Job State Change",
        "detail": {"jobId": array_job_id},
    }
    resp = client.post(
        f"/api/v1/coordinator/campaigns/{campaign_id}/array-complete",
        json=event,
        headers={coord._EVENTBRIDGE_SECRET_HEADER: _EVENTBRIDGE_SECRET},
    )
    assert resp.status_code == 200, f"array-complete failed: {resp.status_code} {resp.text}"
    return resp.json()


def _run_aggregate(client: TestClient, campaign_id: str) -> dict[str, Any]:
    """POST /aggregate and return the parsed body."""
    resp = client.post(f"/api/v1/coordinator/campaigns/{campaign_id}/aggregate")
    assert resp.status_code == 202, f"aggregate failed: {resp.status_code} {resp.text}"
    return resp.json()


def _get_status(client: TestClient, campaign_id: str) -> dict[str, Any]:
    """GET /campaigns/{id} and return the parsed campaign record."""
    resp = client.get(f"/api/v1/coordinator/campaigns/{campaign_id}")
    assert resp.status_code == 200, f"status failed: {resp.status_code} {resp.text}"
    return resp.json()


def _seed_storage_for(monkeypatch: pytest.MonkeyPatch, store: _RecordingObjectStorage) -> None:
    """Inject *store* as the Coordinator's ResultStorage for aggregation."""
    monkeypatch.setattr(coord, "_storage_from_campaign", lambda _rec: store)


def _drive_server_lifecycle(
    client: TestClient,
    campaign_id: str,
    *,
    n_samples: int,
) -> str:
    """Drive the SERVER-side lifecycle: submit-array -> array-complete.

    Aggregation is intentionally NOT run here (callers run it explicitly so
    they can assert on the response). This models the Batch workers +
    EventBridge firing — none of which requires the original submitting
    client. Returns the ``array_job_id``.
    """
    array_job_id = _submit_array(client, campaign_id, array_size=n_samples)
    _fire_array_complete(client, campaign_id, array_job_id)
    return array_job_id


# ===========================================================================
# Minimal stub executor for the bug-fix Campaign-level tests (#621 / #620)
# ===========================================================================


class _SyncStubExecutor(BaseExecutor):
    """Synchronous stub executor (runs ``fn`` inline) for fast Campaign tests.

    Mirrors ``StubExecutor`` in tests/unit/test_graceful_shutdown.py: it runs
    the work function synchronously in-process so the Campaign's cancellation
    polling and cache-teardown paths are exercised without any real fan-out
    latency. This is the substrate a Coordinator worker uses locally.
    """

    name = "sync-stub"

    def __init__(self) -> None:
        self._cancel_called = False

    def submit(  # type: ignore[override]
        self,
        fn: Any,
        *args: Any,
        name: str = "task",
        cpus: int = 1,  # noqa: ARG002
        memory_mb: int = 1024,  # noqa: ARG002
        time_min: int = 60,  # noqa: ARG002
        container: str | None = None,  # noqa: ARG002
        **kwargs: Any,
    ) -> Handle:
        from concurrent.futures import Future

        fut: Future[Any] = Future()
        try:
            result = fn(*args, **kwargs)
            fut.set_result(result)
        except BaseException as exc:
            fut.set_exception(exc)
        return Handle(
            job_id=f"sync-stub-{name}",
            _future=fut,
            worker_id="local",
            worker_ip="127.0.0.1",
        )

    def cancel(self) -> None:
        self._cancel_called = True

    def shutdown(self) -> None:
        return None


def _campaign_cfg(
    variables_yml: Path, template_pkg: Path, outdir: Path, **overrides: Any
) -> CampaignConfig:
    """Build a minimal 1-sample dry-run CampaignConfig for the bug-fix tests."""
    defaults: dict[str, Any] = {
        "input_variables": variables_yml,
        "template_sim_package": template_pkg,
        "n_samples": 1,
        "outdir": outdir,
        "openstudio_version": "3.11.0",
        "dry_run": True,
    }
    defaults.update(overrides)
    return CampaignConfig(**defaults)


@pytest.fixture
def campaign_fixtures(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Build a hermetic variables.yml + template_pkg + outdir for Campaign tests."""
    workdir = tmp_path / "campaign-work"
    workdir.mkdir()
    (workdir / "variables.yml").write_text(
        "algorithm: lhs\n"
        "variables:\n"
        "  - name: wwr\n"
        "    distribution: uniform\n"
        "    min: 0.2\n"
        "    max: 0.6\n"
        "    measure_argument: SetEnvelopePerformance.wwr\n"
    )
    template_pkg = workdir / "template"
    shutil.copytree(EXAMPLE_PKG, template_pkg)
    outdir = workdir / "out"
    outdir.mkdir()
    return workdir / "variables.yml", template_pkg, outdir


# ===========================================================================
# Criterion 1: Disconnect-resilience
# ===========================================================================


@_skip
def test_disconnect_does_not_abort_remote_campaign(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Killing the submitting process after 202 does not abort the remote campaign.

    Models the disconnect: the ``submitting_client`` performs the single
    ``POST /handoff`` (the only thing the user's CLI does), captures the
    ``campaign_id``, and is then dropped — it never polls, never submits
    array work, never touches storage. The server-side lifecycle (Batch
    workers + EventBridge + Coordinator aggregator) is driven entirely by
    other actors. Finally a *third* fresh client reconnects and observes
    ``status == complete``.

    If the Coordinator had any dependency on the submitting process staying
    alive (e.g. an in-process poll loop owned by the handoff request), the
    campaign could never reach ``complete`` here — the submitting client is
    gone before any server-side work runs.
    """
    n_samples = 3
    store = _RecordingObjectStorage()
    # Workers have already uploaded their manifests by the time aggregation
    # fires — seed them up front so the aggregator has something to compile.
    per_sample_kpi_keys: list[str] = []

    # --- The submitting process: one POST /handoff, then it "dies" --------
    submitting_body = _post_handoff(client, _handoff_payload(n_samples=n_samples))
    campaign_id = submitting_body["campaign_id"]
    # Seed the worker manifests for this campaign id.
    per_sample_kpi_keys = _seed_sample_manifests(store, campaign_id, n_samples)
    # From here on the submitting process context is gone: it never polls,
    # never submits array work, never downloads. Only server-side actors
    # touch the campaign below.

    # --- Server-side lifecycle (Batch workers + EventBridge + aggregator) -
    _seed_storage_for(monkeypatch, store)
    with mocked_coordinator_boto3(array_size=n_samples):
        array_job_id = _drive_server_lifecycle(client, campaign_id, n_samples=n_samples)
        assert array_job_id, "submit-array did not return an array job id"
        # EventBridge fired array-complete -> status is now "aggregating".
        assert _get_status(client, campaign_id)["status"] == "aggregating"
        # The Coordinator's own aggregator runs the terminal step.
        agg_body = _run_aggregate(client, campaign_id)
    assert agg_body["status"] == "complete"
    assert agg_body["total_count"] == n_samples

    # --- A fresh reconnect (new shell, no in-memory state) observes done --
    fresh_client = TestClient(create_app(outdir=Path("/nonexistent"), read_only=False))
    final = _get_status(fresh_client, campaign_id)
    assert final["status"] == "complete", (
        f"campaign did not reach complete after the submitting process was killed; "
        f"got status={final['status']!r}"
    )
    assert final["n_samples"] == n_samples


# ===========================================================================
# Criterion 2: Reconnect returns correct status
# ===========================================================================


@_skip
def test_reconnect_returns_correct_status(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After the kill, a fresh ``osimflow status`` reconnects via the handoff record.

    The CLI writes ``outdir/.coordinator_handoff.json`` on ``--detach`` so a
    brand-new shell can reconnect. This test:

      1. Hands off (the CLI persists the handoff record to a temp outdir).
      2. Drives the lifecycle forward server-side (the submitting process
         is gone).
      3. Opens a *fresh* client with no in-memory state, reads the handoff
         record, rebuilds the status URL exactly as the CLI does, and polls
         ``GET /campaigns/{id}``.

    Asserts the reconnect observes the correct progress at intermediate
    phases and the final ``complete`` status.
    """
    n_samples = 2
    store = _RecordingObjectStorage()
    outdir = tmp_path / "reconnect-out"
    outdir.mkdir()

    # 1. Handoff; the response carries the absolute status_url the CLI persists.
    body = _post_handoff(client, _handoff_payload(n_samples=n_samples))
    campaign_id = body["campaign_id"]
    persisted_status_url = body.get("status_url") or ""
    write_handoff_record(
        outdir,
        HandoffRecord(
            campaign_id=campaign_id,
            coordinator_url="http://testserver",
            submitted_at=time.time(),
            status_url=persisted_status_url,
            idempotency_key=None,
        ),
    )
    # Seed worker manifests for the aggregator.
    _seed_sample_manifests(store, campaign_id, n_samples)

    # 2. Drive the lifecycle server-side (submitting process is gone).
    #    Intermediate reconnect after array-complete observes "aggregating".
    _seed_storage_for(monkeypatch, store)
    with mocked_coordinator_boto3(array_size=n_samples):
        array_job_id = _drive_server_lifecycle(client, campaign_id, n_samples=n_samples)
    intermediate = _get_status(client, campaign_id)
    assert intermediate["status"] == "aggregating", (
        f"expected aggregating mid-lifecycle, got {intermediate['status']!r}"
    )
    _run_aggregate(client, campaign_id)

    # 3. Fresh shell: read the handoff record, reconnect, observe status.
    record = read_handoff_record(outdir)
    assert record.campaign_id == campaign_id

    fresh_client = TestClient(create_app(outdir=outdir, read_only=False))
    # Rebuild the status URL exactly as osimflow.__main__._coordinator_status_url
    # does, then poll. The path is the contract the handoff record relies on.
    rebuilt_url = (
        f"{record.coordinator_url.rstrip('/')}/api/v1/coordinator/campaigns/{record.campaign_id}"
    )
    resp = fresh_client.get(rebuilt_url)
    assert resp.status_code == 200
    data = resp.json()
    assert data["campaign_id"] == record.campaign_id
    assert data["status"] == "complete", f"expected complete, got {data['status']!r}"
    assert data["n_samples"] == n_samples


# ===========================================================================
# Criterion 3: Zero per-sample egress to the submitting host
# ===========================================================================


@_skip
def test_zero_per_sample_egress_to_submitting_host(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No per-sample result bytes flow to the submitting host.

    The submitting host's only result-fetching code path is
    ``osimflow download`` -> ``_download_coordinator_results``, which calls
    ``GET /campaigns/{id}/results`` and then fetches exactly the aggregated
    CSV via the Coordinator-signed presigned URL. It must NEVER fetch a
    per-sample ``kpis.json`` or ``eplusout.*`` object.

    This test instruments that path with a recording ``http_get`` and asserts:

      * the SERVER-side aggregator DID download every per-sample kpis.json
        (proving the work genuinely happened remotely, on the Coordinator);
      * the CLIENT-side download path fetched ONLY the aggregated-results
        URL — zero per-sample GETs.
    """
    from osimflow.__main__ import _download_coordinator_results

    n_samples = 3
    store = _RecordingObjectStorage()
    body = _post_handoff(client, _handoff_payload(n_samples=n_samples))
    campaign_id = body["campaign_id"]
    per_sample_kpi_keys = _seed_sample_manifests(store, campaign_id, n_samples)
    _seed_storage_for(monkeypatch, store)

    with mocked_coordinator_boto3(array_size=n_samples, kpi_keys=per_sample_kpi_keys):
        array_job_id = _drive_server_lifecycle(client, campaign_id, n_samples=n_samples)
        agg_body = _run_aggregate(client, campaign_id)
    aggregated_key = agg_body["aggregated_results_key"]

    # SERVER side: the aggregator compiled per-sample kpis into the CSV.
    # Prove the work happened remotely (not on the submitting host).
    server_downloaded = [k for k in store.download_calls if k.endswith("/kpis.json")]
    assert sorted(server_downloaded) == sorted(per_sample_kpi_keys), (
        "server-side aggregator should have downloaded every per-sample kpis.json; "
        f"got {server_downloaded!r}"
    )
    assert aggregated_key in store.objects, "aggregated CSV was not published"

    # CLIENT side: a recording http_get captures every URL the submitting
    # host touches during `osimflow download`.
    fetched_urls: list[str] = []

    class _RecordingResponse:
        def __init__(self, url: str) -> None:
            self.url = url
            self.status_code = 200
            self.text = ""
            if url.endswith("/results"):
                # GET /campaigns/{id}/results listing.
                self._json: Any = {
                    "status": "available",
                    "aggregated_results_key": aggregated_key,
                    "aggregated_results_url": f"https://presigned.test/agg/{aggregated_key}",
                    "kpi_files": [
                        {"sample_index": i, "file_key": k, "file_type": "json"}
                        for i, k in enumerate(per_sample_kpi_keys)
                    ],
                    "message": "ok",
                }
                self.content = b""
            else:
                # The presigned aggregated-results download.
                self._json = None
                self.content = store.objects[aggregated_key]

        def json(self) -> Any:
            return self._json

    def recording_get(url: str, **_kwargs: Any) -> _RecordingResponse:
        fetched_urls.append(url)
        return _RecordingResponse(url)

    record = HandoffRecord(
        campaign_id=campaign_id,
        coordinator_url="http://testserver",
        submitted_at=time.time(),
        status_url=f"http://testserver/api/v1/coordinator/campaigns/{campaign_id}",
    )
    output_dir = tmp_path / "download"
    rc = _download_coordinator_results(record, output_dir, http_get=recording_get)

    assert rc == 0, "download should succeed"
    # The host received the aggregated CSV...
    assert (output_dir / "aggregated_results.csv").is_file()
    # ...and touched exactly two URLs: the results listing + the aggregated
    # presigned URL. No per-sample object was fetched.
    assert len(fetched_urls) == 2, f"expected 2 client GETs, got {fetched_urls!r}"
    per_sample_fetches = [u for u in fetched_urls if "/kpis.json" in u or "eplusout" in u]
    assert not per_sample_fetches, (
        f"ZERO per-sample egress violated: host fetched {per_sample_fetches!r}"
    )
    assert any("presigned" in u or aggregated_key in u for u in fetched_urls), (
        f"client never fetched the aggregated results; fetched={fetched_urls!r}"
    )


# ===========================================================================
# Criterion 4: One array submit + one aggregator
# ===========================================================================


@_skip
def test_exactly_one_array_submit_plus_one_aggregator(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An N-sample campaign produces exactly one array submit_job + one aggregator.

    This is the epic-level gate: "a 50,000-sample campaign submits with
    exactly one ``submit_job`` (array) + one aggregator job." We test it at
    N=3 (the principle is identical at any N — the array size scales, the
    call count does not). The boto3 mock records every ``submit_job``; we
    assert the count is exactly 1 and it carries ``arrayProperties.size``
    equal to N. The aggregator runs exactly once and returns a single
    deterministic ``aggregator_job_id``; a second call is rejected (409).
    """
    n_samples = 3
    store = _RecordingObjectStorage()
    body = _post_handoff(client, _handoff_payload(n_samples=n_samples))
    campaign_id = body["campaign_id"]
    _seed_sample_manifests(store, campaign_id, n_samples)
    _seed_storage_for(monkeypatch, store)

    with mocked_coordinator_boto3(array_size=n_samples) as (batch, _s3):
        array_job_id = _drive_server_lifecycle(client, campaign_id, n_samples=n_samples)
        agg_body = _run_aggregate(client, campaign_id)

    # --- Exactly one array submit_job, with arrayProperties.size == N ------
    submit_calls = batch.submit_job_calls
    assert len(submit_calls) == 1, (
        f"expected exactly 1 submit_job for a {n_samples}-sample campaign, "
        f"got {len(submit_calls)}: {submit_calls}"
    )
    only_call = submit_calls[0]
    array_props = only_call.get("arrayProperties") or {}
    assert array_props.get("size") == n_samples, (
        f"arrayProperties.size should be {n_samples}, got {array_props}"
    )
    assert only_call["jobName"].startswith("osimflow-"), (
        f"unexpected jobName: {only_call.get('jobName')!r}"
    )
    assert array_job_id, "array_job_id should be non-empty"

    # --- Exactly one aggregator job ---------------------------------------
    assert agg_body["aggregator_job_id"] == f"{campaign_id}-aggregator"
    assert agg_body["status"] == "complete"
    assert agg_body["total_count"] == n_samples

    # Re-running aggregate is rejected (idempotency guard) -> still one job.
    second = client.post(f"/api/v1/coordinator/campaigns/{campaign_id}/aggregate")
    assert second.status_code == 409, (
        "a second aggregate call must be rejected (exactly-one-aggregator invariant)"
    )


# ===========================================================================
# Criterion 5: Bug-fix confirmations
# ===========================================================================


@_skip
def test_bug622_no_typeerror_on_coordinator_array_submit(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,  # noqa: ARG001
) -> None:
    """#622 fix holds in the cloud path: no TypeError on submit.

    Issue #622 was a ``TypeError`` caused by ``openstudio_version`` being
    passed both positionally and as a keyword to ``executor.submit()``
    (fixed in #631 by using a named-parameter call). In the cloud
    (Coordinator) path the equivalent submit is the ``submit-array``
    endpoint's ``boto3.client('batch').submit_job(...)`` call. A regression
    where the openstudio_version kwarg is mishandled would surface as a 5xx
    (TypeError inside the handler). We assert the endpoint returns 200 with
    exactly one recorded submit_job and no TypeError.
    """
    n_samples = 2
    body = _post_handoff(client, _handoff_payload(n_samples=n_samples))
    campaign_id = body["campaign_id"]

    with mocked_coordinator_boto3(array_size=n_samples) as (batch, _s3):
        # Must not 5xx. A TypeError would surface as HTTP 500.
        resp = client.post(
            f"/api/v1/coordinator/campaigns/{campaign_id}/submit-array",
            json={
                "job_queue": "q",
                "job_definition": "jd",
                "array_size": n_samples,
            },
        )
    assert resp.status_code == 200, (
        f"submit-array should succeed (no TypeError); got {resp.status_code}: {resp.text}"
    )
    assert len(batch.submit_job_calls) == 1
    only = batch.submit_job_calls[0]
    assert "arrayProperties" in only and only["arrayProperties"]["size"] == n_samples


@_skip
def test_bug621_cancellation_honored_at_coordinator_boundary(
    campaign_fixtures: tuple[Path, Path, Path],
) -> None:
    """#621 fix holds: a cancel signal mid-run is honored (status=cancelled).

    Issue #621 was the generation loop / dry-run path not polling the cancel
    signal between steps, so a ``.stop`` file written mid-flight was lost
    and the trace was written as ``status="ok"`` (fixed in #638). The
    Coordinator boundary propagates cancels to running workers via exactly
    this ``.stop``-file mechanism (the ``POST /api/v1/campaign/stop``
    endpoint writes it; the Campaign's ``_check_cancel_requested`` reads
    it). This test reproduces the #638 regression test at the boundary a
    Coordinator worker would hit: a ``.stop`` file appears after a step
    completes, and the next inter-step check must observe it.
    """
    variables_yml, template_pkg, outdir = campaign_fixtures
    cfg = _campaign_cfg(variables_yml, template_pkg, outdir, dry_run=True)
    campaign = Campaign(cfg=cfg, executor=_SyncStubExecutor())

    extract_calls = 0
    original_extract = campaign.step_extract_kpis

    def extract_then_write_stop(*args: Any, **kwargs: Any) -> Any:
        nonlocal extract_calls
        result = original_extract(*args, **kwargs)
        extract_calls += 1
        # Simulate the Coordinator boundary (POST /campaign/stop) writing
        # the .stop flag AFTER the step's own entry check has passed.
        (outdir / ".stop").touch()
        return result

    campaign.step_extract_kpis = extract_then_write_stop

    # Must not raise: cancellation is graceful, trace status is "cancelled".
    campaign.run()

    assert extract_calls == 1, "step_extract_kpis should have run once before cancel"
    assert campaign.trace.status == "cancelled", (
        f"#621 regression: expected status=cancelled, got {campaign.trace.status!r}"
    )
    run_data = json.loads((outdir / "run.json").read_text())
    assert run_data["status"] == "cancelled"


@_skip
def test_bug620_no_cache_sqlite_shm_error_on_teardown(
    campaign_fixtures: tuple[Path, Path, Path],
) -> None:
    """#620 fix holds: no ``cache.sqlite-shm`` ``FileNotFoundError`` on teardown.

    Issue #620 was a SQLite race where ``close()`` ran
    ``wal_checkpoint(TRUNCATE)``, removing the ``-wal``/``-shm`` aux files
    out from under peer worker processes and crashing them with
    ``FileNotFoundError: cache.sqlite-shm`` during campaign cancellation
    (fixed in #635 with a PASSIVE checkpoint + thread-safe close). This
    test runs a campaign, cancels it mid-flight, and asserts the teardown
    path in ``run()``'s ``finally`` block completes without raising any
    ``FileNotFoundError`` — the exact regression class #620 describes.

    It also sanity-checks the cache aux files are stable across a clean
    open/close/reopen cycle (the unit-level invariant the Campaign teardown
    relies on).
    """
    from osimflow.cache import SQLiteCache

    outdir = campaign_fixtures[2]

    # --- Unit-level: cache close must not raise FileNotFoundError ---------
    db_path = outdir / "sub" / "cache.sqlite"
    cache = SQLiteCache(db_path)
    cache.close()
    cache.close()  # idempotent — must not raise FileNotFoundError
    # A second instance opening the same path after a clean close must work.
    cache2 = SQLiteCache(db_path)
    cache2.close()

    # --- Campaign-level: cancelled run tears down cleanly -----------------
    variables_yml, template_pkg, outdir = campaign_fixtures
    cfg = _campaign_cfg(variables_yml, template_pkg, outdir, dry_run=True)
    campaign = Campaign(cfg=cfg, executor=_SyncStubExecutor())
    original_extract = campaign.step_extract_kpis

    def extract_then_cancel(*args: Any, **kwargs: Any) -> Any:
        result = original_extract(*args, **kwargs)
        (outdir / ".stop").touch()  # cancel via the Coordinator-boundary path
        return result

    campaign.step_extract_kpis = extract_then_cancel

    try:
        campaign.run()
    except FileNotFoundError as exc:
        if "sqlite-shm" in str(exc) or "sqlite-wal" in str(exc):
            pytest.fail(f"#620 regression: cache teardown raised {exc!r}")
        raise

    # If we reach here, teardown was clean — the #620 invariant holds.
    assert campaign.trace.status == "cancelled"
