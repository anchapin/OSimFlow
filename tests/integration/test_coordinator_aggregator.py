"""Integration tests for the S3 Aggregator Task (issue #627, Epic #624).

Covers all seven acceptance criteria:

1. ``POST /api/v1/coordinator/campaigns/{id}/aggregate`` -> ``202`` with
   ``aggregator_job_id``.
2. ``aggregated_results.csv`` column contract matches the local
   ``bin/aggregate_results.py`` path (``sample_id`` lead, then KPI columns).
3. ``failed_simulations.csv`` carries exactly the first ``  * Severe`` line
   per failed manifest (PRD §6 #4) — no full ``.err`` dumps.
4. A Pareto-front JSON is produced when ``--algorithm`` is ``nsga2``/``pso``.
5. Robustness: an ok manifest whose ``kpis.json`` is missing is logged +
   counted as failed, never a crash.
6. Final artifacts land at ``{campaign_id}/_aggregated/`` and the campaign
   status flips ``aggregating -> complete``.
7. The headline scenario: 3 fake manifests (2 ok + 1 failed) -> 2-row CSV +
   correct failed summary.

The pure aggregation core (:func:`osimflow.aggregation.compile_aggregation`)
is exercised directly for the column/robustness contracts, and the HTTP
endpoint is exercised end-to-end through a filesystem-backed fake
:class:`~osimflow.storage.ResultStorage` so the list/download/upload/transition
path is covered too.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi", reason="osimflow[api] extra required")
pytest.importorskip("slowapi", reason="osimflow[api] extra required")
pytest.importorskip("boto3", reason="osimflow[aws] extra required")
from fastapi.testclient import TestClient

from osimflow.aggregation import (
    FAILED_SIMULATIONS_COLUMNS,
    compile_aggregation,
    parse_manifest,
)
from osimflow.api import coordinator as coord
from osimflow.api import create_app
from osimflow.storage import ResultStorage

CAMPAIGN_ID = "01J0ABCDEFGH"


# ---------------------------------------------------------------------------
# Filesystem-backed fake storage (lets the endpoint do real list/get/put)
# ---------------------------------------------------------------------------


class _FakeObjectStorage(ResultStorage):
    """In-memory object store that honours the :class:`ResultStorage` ABC.

    Objects are held as raw bytes keyed by their remote path.  ``upload_file``
    stages through the caller's local file (matching the real backends) and
    ``download_file`` materialises bytes to the caller's local path, so the
    endpoint's temp-file staging logic is exercised faithfully.
    """

    name = "fake"

    def __init__(self) -> None:
        self._objects: dict[str, bytes] = {}

    # --- helpers for the test -------------------------------------------------
    def put_text(self, remote_path: str, text: str) -> None:
        self._objects[remote_path] = text.encode("utf-8")

    @property
    def objects(self) -> dict[str, bytes]:
        return dict(self._objects)

    # --- ResultStorage ABC ----------------------------------------------------
    def upload_file(self, local_path: Path, remote_path: str) -> None:
        self._objects[remote_path] = local_path.read_bytes()

    def download_file(self, remote_path: str, local_path: Path) -> None:
        if remote_path not in self._objects:
            raise FileNotFoundError(f"fake-storage: missing {remote_path}")
        local_path.parent.mkdir(parents=True, exist_ok=True)
        local_path.write_bytes(self._objects[remote_path])

    def list_results(self, prefix: str = "") -> list[str]:
        return sorted(k for k in self._objects if k.startswith(prefix))


def _kpis(sample_id: str, **kpis: float) -> str:
    """Build a canonical worker ``kpis.json`` payload (matches extract_kpis.py)."""
    return json.dumps({"sample_id": sample_id, "openstudio_version": None, "kpis": kpis})


def _manifest(
    sample_id: str,
    index: int,
    *,
    status: str,
    kpis_key: str | None,
    first_severe: str | None = None,
    exit_code: int = 0,
) -> str:
    """Build a §3.1 ``_manifest.json`` payload."""
    return json.dumps(
        {
            "campaign_id": CAMPAIGN_ID,
            "sample_id": sample_id,
            "index": index,
            "status": status,
            "kpis_key": kpis_key,
            "exit_code": exit_code,
            "first_severe_error": first_severe,
            "finished_at": "2026-06-19T12:00:00Z",
        }
    )


def _seed_three_samples(store: _FakeObjectStorage) -> None:
    """Populate *store* with the headline 3-sample scenario (2 ok + 1 failed)."""
    # Sample 0 — ok, low EUI.
    k0 = f"{CAMPAIGN_ID}/samples/s0001/kpis.json"
    store.put_text(k0, _kpis("s0001", eui=120.0, total_energy=48000.0))
    store.put_text(
        f"{CAMPAIGN_ID}/samples/s0001/_manifest.json",
        _manifest("s0001", 0, status="ok", kpis_key=k0, exit_code=0),
    )
    # Sample 1 — ok, high EUI (dominated by s0001 on both objectives).
    k1 = f"{CAMPAIGN_ID}/samples/s0002/kpis.json"
    store.put_text(k1, _kpis("s0002", eui=150.0, total_energy=60000.0))
    store.put_text(
        f"{CAMPAIGN_ID}/samples/s0002/_manifest.json",
        _manifest("s0002", 1, status="ok", kpis_key=k1, exit_code=0),
    )
    # Sample 2 — failed; first Severe line captured in the manifest.
    store.put_text(
        f"{CAMPAIGN_ID}/samples/s0003/_manifest.json",
        _manifest(
            "s0003",
            2,
            status="failed",
            kpis_key=None,
            first_severe="  * Severe ~ HVAC sizing failed for plant loop",
            exit_code=1,
        ),
    )


# ===========================================================================
# Pure-function tests (compile_aggregation)
# ===========================================================================


def _fake_fetcher(kpis_by_key: dict[str, str]) -> Any:
    """Build a kpi_fetcher backed by an in-memory {key: json_str} map."""

    def fetch(key: str) -> dict[str, Any] | None:
        raw = kpis_by_key.get(key)
        if raw is None:
            return None
        return json.loads(raw)

    return fetch


def test_parse_manifest_tolerates_iso_finished_at() -> None:
    """A non-numeric finished_at coerces to None (metadata-only field)."""
    m = parse_manifest(
        {
            "sample_id": "s1",
            "index": "3",
            "status": "OK",
            "kpis_key": "k.json",
            "exit_code": "0",
            "first_severe_error": None,
            "finished_at": "2026-06-19T12:00:00Z",
        }
    )
    assert m.sample_id == "s1"
    assert m.index == 3
    assert m.status == "OK"
    assert m.kpis_key == "k.json"
    assert m.finished_at is None


def test_compile_aggregation_headline_scenario() -> None:
    """2 ok + 1 failed -> 2-row aggregated CSV + 1-row failed CSV (criteria #2,3,7)."""
    manifests = [
        parse_manifest(json.loads(_manifest("s0001", 0, status="ok", kpis_key="k1"))),
        parse_manifest(json.loads(_manifest("s0002", 1, status="ok", kpis_key="k2"))),
        parse_manifest(
            json.loads(
                _manifest(
                    "s0003",
                    2,
                    status="failed",
                    kpis_key=None,
                    first_severe="  * Severe ~ plant loop not converged",
                    exit_code=1,
                )
            )
        ),
    ]
    kpis = {
        "k1": _kpis("s0001", eui=120.0, total_energy=48000.0),
        "k2": _kpis("s0002", eui=150.0, total_energy=60000.0),
    }
    result = compile_aggregation(manifests, _fake_fetcher(kpis), algorithm="lhs")

    # Criterion #2: aggregated_results.csv matches the local column contract.
    lines = result.aggregated_results_csv.strip().splitlines()
    assert lines[0] == "sample_id,eui,total_energy", lines[0]
    assert len(lines) == 1 + 2  # header + 2 ok rows (failed excluded)
    assert lines[1].startswith("s0001,")
    assert lines[2].startswith("s0002,")

    # Criterion #3: failed_simulations.csv — first Severe line, canonical columns.
    flines = result.failed_simulations_csv.strip().splitlines()
    assert flines[0] == ",".join(FAILED_SIMULATIONS_COLUMNS)
    assert len(flines) == 2  # header + 1 failure
    # The Severe line is verbatim in error_summary (no full .err dump).
    assert "plant loop not converged" in flines[1]
    assert "s0003" == flines[1].split(",")[0]

    assert result.ok_count == 2
    assert result.failed_count == 1
    assert result.total_count == 3
    # Single-objective algorithm -> no Pareto front.
    assert result.pareto_json is None


def test_compile_aggregation_criterion5_missing_kpis_for_ok_manifest() -> None:
    """An ok manifest with an unrecoverable kpis.json is counted as failed (criterion #5)."""
    manifests = [
        parse_manifest(json.loads(_manifest("s0001", 0, status="ok", kpis_key="k1"))),
        # ok claim but kpis.json absent from storage.
        parse_manifest(json.loads(_manifest("s0002", 1, status="ok", kpis_key="missing"))),
    ]
    kpis = {"k1": _kpis("s0001", eui=120.0)}
    result = compile_aggregation(manifests, _fake_fetcher(kpis))

    assert result.ok_count == 1
    assert result.failed_count == 1
    assert result.degraded_ok_samples == ["s0002"]
    # The degraded sample lands in failed_simulations.csv with a clear summary.
    assert "kpis.json missing" in result.failed_simulations_csv
    # And NOT in aggregated_results.csv.
    assert "s0002" not in result.aggregated_results_csv


def test_compile_aggregation_kpi_fetcher_exception_is_not_fatal() -> None:
    """A raising fetcher is caught + logged (criterion #5 robustness)."""

    def raising_fetch(key: str) -> dict[str, Any] | None:
        raise RuntimeError("transient storage outage")

    manifests = [parse_manifest(json.loads(_manifest("s1", 0, status="ok", kpis_key="k1")))]
    result = compile_aggregation(manifests, raising_fetch)
    assert result.ok_count == 0
    assert result.failed_count == 1
    assert result.degraded_ok_samples == ["s1"]


def test_compile_aggregation_pareto_front_for_multi_objective() -> None:
    """nsga2 -> a Pareto front JSON with the non-dominated solutions (criterion #4)."""
    manifests = [
        parse_manifest(json.loads(_manifest("dom", 0, status="ok", kpis_key="k_dom"))),
        parse_manifest(json.loads(_manifest("nd1", 1, status="ok", kpis_key="k_nd1"))),
        parse_manifest(json.loads(_manifest("nd2", 2, status="ok", kpis_key="k_nd2"))),
    ]
    # nd1/nd2 trade off; "dom" is worse on both -> excluded from the front.
    kpis = {
        "k_dom": _kpis("dom", eui=200.0, cost=200.0),
        "k_nd1": _kpis("nd1", eui=100.0, cost=150.0),
        "k_nd2": _kpis("nd2", eui=150.0, cost=100.0),
    }
    result = compile_aggregation(
        manifests,
        _fake_fetcher(kpis),
        algorithm="nsga2",
        kpi_objectives={"eui": "minimize", "cost": "minimize"},
    )
    assert result.pareto_json is not None
    front = json.loads(result.pareto_json)
    front_ids = {s["sample_id"] for s in front["solutions"]}
    assert "dom" not in front_ids
    assert {"nd1", "nd2"} == front_ids


def test_compile_aggregation_empty_manifests_yields_header_only_csvs() -> None:
    """No manifests -> header-only CSVs (matches the local empty path)."""
    result = compile_aggregation([], lambda _: None)
    assert result.aggregated_results_csv == "sample_id\n"
    assert result.failed_simulations_csv == ",".join(FAILED_SIMULATIONS_COLUMNS) + "\n"
    assert result.ok_count == 0


def test_compile_aggregation_all_failed_yields_empty_aggregate() -> None:
    """All-failed campaign: aggregated_results.csv is header-only, failures populated."""
    manifests = [
        parse_manifest(
            json.loads(
                _manifest("s1", 0, status="failed", kpis_key=None, first_severe="  * Severe X")
            )
        )
    ]
    result = compile_aggregation(manifests, lambda _: None)
    assert result.aggregated_results_csv == "sample_id\n"
    assert "s1" in result.failed_simulations_csv


# ===========================================================================
# HTTP endpoint tests (POST /aggregate)
# ===========================================================================


@pytest.fixture(autouse=True)
def _isolate_campaign_store() -> None:
    """Clear the in-memory campaign + idempotency stores around every test."""
    coord._campaigns.clear()
    coord._idempotency_keys.clear()
    yield
    coord._campaigns.clear()
    coord._idempotency_keys.clear()


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    """FastAPI TestClient with write access (no API key configured)."""
    app = create_app(outdir=tmp_path, read_only=False)
    return TestClient(app)


def _seed_campaign(
    *,
    campaign_id: str = CAMPAIGN_ID,
    status: str = "aggregating",
    bucket: str = "test-bucket",
    algorithm: str = "lhs",
) -> dict[str, Any]:
    rec: dict[str, Any] = {
        "campaign_id": campaign_id,
        "name": "agg-test",
        "status": status,
        "created_at": 0.0,
        "updated_at": 0.0,
        "n_samples": 3,
        "executor": "aws_batch",
        "openstudio_version": "3.11.0",
        "array_job_id": "array-1",
        "result_storage_bucket": bucket,
        "result_status": "unavailable",
        "aggregated_results_key": None,
        "payload": {
            "result_storage_backend": "s3",
            "result_storage_bucket": bucket,
            "algorithm": algorithm,
        },
    }
    coord._campaigns[campaign_id] = rec
    return rec


def test_aggregate_endpoint_headline_3_samples(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: 3 fake manifests -> 202, artifacts under _aggregated/, status flips."""
    store = _FakeObjectStorage()
    _seed_three_samples(store)
    _seed_campaign()
    monkeypatch.setattr(coord, "_storage_from_campaign", lambda _rec: store)

    resp = client.post(f"/api/v1/coordinator/campaigns/{CAMPAIGN_ID}/aggregate")

    assert resp.status_code == 202, resp.text
    body = resp.json()
    # Criterion #1.
    assert body["aggregator_job_id"] == f"{CAMPAIGN_ID}-aggregator"
    assert body["status"] == "complete"
    assert body["ok_count"] == 2
    assert body["failed_count"] == 1
    assert body["total_count"] == 3
    # Criterion #6: artifacts land under {campaign_id}/_aggregated/.
    assert body["aggregated_results_key"] == f"{CAMPAIGN_ID}/_aggregated/aggregated_results.csv"
    assert body["failed_simulations_key"] == f"{CAMPAIGN_ID}/_aggregated/failed_simulations.csv"

    # Status flipped atomically on the record.
    rec = coord._campaigns[CAMPAIGN_ID]
    assert rec["status"] == "complete"
    assert rec["result_status"] == "available"

    # The aggregated CSV written to the fake store matches the column contract.
    agg = store.objects[body["aggregated_results_key"]].decode("utf-8")
    assert agg.splitlines()[0] == "sample_id,eui,total_energy"
    assert len(agg.splitlines()) == 3  # header + 2 ok rows
    # The failed CSV carries the first Severe line verbatim.
    fail = store.objects[body["failed_simulations_key"]].decode("utf-8")
    assert "HVAC sizing failed for plant loop" in fail
    assert fail.splitlines()[0] == ",".join(FAILED_SIMULATIONS_COLUMNS)


def test_aggregate_endpoint_criterion5_missing_kpis(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An ok manifest with no kpis.json in storage -> counted as failed, no 5xx."""
    store = _FakeObjectStorage()
    # s0001 ok + kpis present; s0002 claims ok but kpis.json never uploaded.
    k0 = f"{CAMPAIGN_ID}/samples/s0001/kpis.json"
    store.put_text(k0, _kpis("s0001", eui=100.0))
    store.put_text(
        f"{CAMPAIGN_ID}/samples/s0001/_manifest.json",
        _manifest("s0001", 0, status="ok", kpis_key=k0),
    )
    store.put_text(
        f"{CAMPAIGN_ID}/samples/s0002/_manifest.json",
        _manifest("s0002", 1, status="ok", kpis_key=f"{CAMPAIGN_ID}/samples/s0002/kpis.json"),
    )
    _seed_campaign()
    monkeypatch.setattr(coord, "_storage_from_campaign", lambda _rec: store)

    resp = client.post(f"/api/v1/coordinator/campaigns/{CAMPAIGN_ID}/aggregate")

    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["ok_count"] == 1
    assert body["failed_count"] == 1  # s0002 downgraded, not crashed
    fail = store.objects[body["failed_simulations_key"]].decode("utf-8")
    assert "s0002" in fail
    assert "kpis.json missing" in fail


def test_aggregate_endpoint_writes_pareto_for_nsga2(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Multi-objective algorithm -> pareto_front.json written (criterion #4)."""
    store = _FakeObjectStorage()
    k1 = f"{CAMPAIGN_ID}/samples/s0001/kpis.json"
    k2 = f"{CAMPAIGN_ID}/samples/s0002/kpis.json"
    store.put_text(k1, _kpis("s0001", eui=100.0, cost=150.0))
    store.put_text(k2, _kpis("s0002", eui=150.0, cost=100.0))
    store.put_text(
        f"{CAMPAIGN_ID}/samples/s0001/_manifest.json",
        _manifest("s0001", 0, status="ok", kpis_key=k1),
    )
    store.put_text(
        f"{CAMPAIGN_ID}/samples/s0002/_manifest.json",
        _manifest("s0002", 1, status="ok", kpis_key=k2),
    )
    _seed_campaign(algorithm="nsga2")
    monkeypatch.setattr(coord, "_storage_from_campaign", lambda _rec: store)

    resp = client.post(f"/api/v1/coordinator/campaigns/{CAMPAIGN_ID}/aggregate")

    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["pareto_front_key"] == f"{CAMPAIGN_ID}/_aggregated/pareto_front.json"
    pareto = json.loads(store.objects[body["pareto_front_key"]])
    front_ids = {s["sample_id"] for s in pareto["solutions"]}
    assert front_ids == {"s0001", "s0002"}


def test_aggregate_endpoint_409_when_already_complete(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second call after a successful aggregation is rejected (idempotency guard)."""
    store = _FakeObjectStorage()
    _seed_three_samples(store)
    _seed_campaign()
    monkeypatch.setattr(coord, "_storage_from_campaign", lambda _rec: store)

    first = client.post(f"/api/v1/coordinator/campaigns/{CAMPAIGN_ID}/aggregate")
    assert first.status_code == 202

    second = client.post(f"/api/v1/coordinator/campaigns/{CAMPAIGN_ID}/aggregate")
    assert second.status_code == 409


def test_aggregate_endpoint_409_when_not_yet_aggregating(
    client: TestClient,
) -> None:
    """Calling aggregate before array-complete (status still 'running') -> 409."""
    _seed_campaign(status="running")
    resp = client.post(f"/api/v1/coordinator/campaigns/{CAMPAIGN_ID}/aggregate")
    assert resp.status_code == 409
    assert "aggregating" in resp.json()["detail"]


def test_aggregate_endpoint_409_when_no_manifests(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Aggregating campaign with zero manifests -> 409 (nothing to aggregate)."""
    store = _FakeObjectStorage()  # empty
    _seed_campaign()
    monkeypatch.setattr(coord, "_storage_from_campaign", lambda _rec: store)

    resp = client.post(f"/api/v1/coordinator/campaigns/{CAMPAIGN_ID}/aggregate")
    assert resp.status_code == 409
    assert "manifest" in resp.json()["detail"].lower()


def test_aggregate_endpoint_409_when_no_bucket_configured(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Campaign with no result_storage_bucket -> 409 (cannot aggregate)."""
    _seed_campaign()
    # Simulate the storage factory declining to build a backend.
    monkeypatch.setattr(coord, "_storage_from_campaign", lambda _rec: None)

    resp = client.post(f"/api/v1/coordinator/campaigns/{CAMPAIGN_ID}/aggregate")
    assert resp.status_code == 409


def test_aggregate_endpoint_404_unknown_campaign(client: TestClient) -> None:
    """An unknown campaign_id -> 404 (consistent with the other endpoints)."""
    resp = client.post("/api/v1/coordinator/campaigns/does-not-exist/aggregate")
    assert resp.status_code == 404
