"""N processes × all shared-SQLite stores — verify no OperationalError/FileNotFoundError under contention.

Issue #1564: shared SQLite access primitive in :mod:`osimflow._sqlite_store`.

Replaces the per-module one-off lock tests that previously lived in
``tests/unit/test_cache.py`` (``test_multiprocess_concurrent_close_no_aux_file_race``),
``tests/unit/test_document_store.py`` (``test_concurrent_workers_distinct_ids``,
``test_concurrent_workers_unique_index``), and the per-store thread tests
in ``test_results_db`` / ``test_event_log`` / ``test_registry``.  Those
tests stay in place for now (issue #1564 leaves them as-is for a
follow-up cleanup) — this file adds a single end-to-end storm that
exercises the canonical PRAGMA / WAL / busy_timeout set across every
store that now derives from :mod:`osimflow._sqlite_store`.

Skip gate: ``OSIMFLOW_SKIP_MULTIPROC_SQLITE_STORE_TEST`` (matches the
existing ``OSIMFLOW_SKIP_MULTIPROC_CACHE_TEST`` pattern).
"""

from __future__ import annotations

import multiprocessing as mp_mod
import os
import time
from pathlib import Path

import pytest

from osimflow.cache import CacheKey, SQLiteCache
from osimflow.document_store import SQLiteDocumentStore
from osimflow.registry import CampaignRecord, CampaignRegistry
from osimflow.results_db import ResultsDatabase


def _cache_worker(db_path_str: str, wid: int, n_ops: int) -> None:
    db_path = Path(db_path_str)
    cache = SQLiteCache(db_path)
    try:
        for i in range(n_ops):
            key = CacheKey(
                step="RUN_OPENSTUDIO_SIM",
                sample_id=f"w{wid}-s{i:03d}",
                openstudio_version="3.11.0",
                inputs_sha256=f"i{wid}-{i}",
                code_sha256="c",
                container_digest="d",
            )
            cache.store(key, Path(f"/tmp/c{wid}_{i}"), exit_code=0)
            cache.lookup(key)
    finally:
        cache.close()


def _registry_worker(db_path_str: str, wid: int, n_ops: int) -> None:
    db_path = Path(db_path_str)
    reg = CampaignRegistry(db_path)
    try:
        for i in range(n_ops):
            reg.register(
                f"w{wid}-camp-{i:03d}",
                name=f"campaign-w{wid}-{i}",
                project=f"proj-w{wid}",
                outdir=f"/tmp/w{wid}/{i}",
                status="running",
                algorithm="lhs",
                n_samples=10,
                executor="local",
                openstudio_version="3.11.0",
                config_hash=f"cfg-w{wid}-{i}",
                metadata={"worker": wid, "op": i},
            )
            reg.list_campaigns(limit=5)
    finally:
        # CampaignRegistry opens a fresh connection per call and relies on
        # the OS / GC to release the file handles. Nothing to close.
        pass


def _results_db_worker(db_path_str: str, wid: int, n_ops: int) -> None:
    db_path = Path(db_path_str)
    db = ResultsDatabase(db_path)
    try:
        db.add_campaign(
            f"camp_w{wid}",
            n_samples=n_ops,
            algorithm="lhs",
            openstudio_version="3.11.0",
        )
        for i in range(n_ops):
            db.add_result(
                f"w{wid}-s{i:03d}",
                "eui",
                float(wid * 100 + i),
                unit="kWh/m2/yr",
                campaign_id=f"camp_w{wid}",
            )
        db.query_results(campaign_id=f"camp_w{wid}")
    finally:
        db.close()


def _document_store_worker(db_path_str: str, wid: int, n_ops: int) -> None:
    db_path = Path(db_path_str)
    store = SQLiteDocumentStore(db_path)
    try:
        for i in range(n_ops):
            store.insert_one(
                "kpis",
                {
                    "sample_id": f"w{wid}-s{i:03d}",
                    "eui": float(wid * 100 + i),
                    "cost": float(i),
                    "worker": wid,
                },
            )
        store.find_many("kpis", {"worker": wid}, limit=10)
    finally:
        store.close()


_WORKERS = (
    ("cache", _cache_worker, "cache.sqlite"),
    ("registry", _registry_worker, "registry.db"),
    ("results_db", _results_db_worker, "results.db"),
    ("document_store", _document_store_worker, "docs.sqlite"),
)


@pytest.mark.timeout(180)
def test_concurrent_writes_across_all_stores(tmp_path: Path) -> None:
    """N processes × all shared-SQLite stores — verify no OperationalError /
    FileNotFoundError and that each store's invariants hold.

    The fixture builds four shared databases and spawns N worker
    processes for each.  Every worker hammers its store with insert +
    query pairs; the storm is the closest reproducible proxy for the
    ``--redis-url`` distributed cache and HPC Slurm fan-out scenarios
    that the original per-module tests targeted.

    Each store's invariant after the storm:

    * **cache** — ``stats()['total']`` equals N*n_ops (last-write-wins
      keyed by sample_id, so each row is unique).
    * **registry** — ``list_campaigns()`` returns N*n_ops campaigns
      with the expected ``id`` / ``metadata`` round-trip.
    * **results_db** — ``n_results()`` equals N*n_ops (one per
      ``add_result`` call, no duplicates since ``sample_id`` includes
      the worker id).
    * **document_store** — ``count_documents('kpis')`` equals N*n_ops.
    """
    if os.environ.get("OSIMFLOW_SKIP_MULTIPROC_SQLITE_STORE_TEST"):
        pytest.skip("OSIMFLOW_SKIP_MULTIPROC_SQLITE_STORE_TEST set")

    n_workers = 4
    n_ops = 20

    ctx = mp_mod.get_context("spawn")
    procs: list[mp_mod.context.SpawnProcess] = []

    started = time.time()
    for kind, fn, fname in _WORKERS:
        db_path = str(tmp_path / fname)
        # Open each store once in the parent so the schema is in place
        # before the workers race — mirrors how a long-running campaign
        # would have already created the file when a peer joins.
        if kind == "cache":
            SQLiteCache(Path(db_path)).close()
        elif kind == "registry":
            CampaignRegistry(Path(db_path))
        elif kind == "results_db":
            ResultsDatabase(Path(db_path)).close()
        elif kind == "document_store":
            SQLiteDocumentStore(Path(db_path)).close()

        for wid in range(n_workers):
            procs.append(
                ctx.Process(
                    target=fn,
                    args=(db_path, wid, n_ops),
                    name=f"osim1564-{kind}-w{wid}",
                )
            )

    for p in procs:
        p.start()

    try:
        for p in procs:
            p.join(timeout=90)
            if p.is_alive():
                p.terminate()
                p.join(timeout=5)
                pytest.fail(
                    f"multiprocess sqlite worker {p.name!r} hung — "
                    "possible WAL/checkpoint deadlock (issue #1564 / "
                    "regression of issues #620, #993, #1006)"
                )
            assert p.exitcode == 0, (
                f"worker {p.name!r} exited with code {p.exitcode}; "
                "see traceback on stderr above. This reproduces the "
                "OperationalError / FileNotFoundError class of bug "
                "the shared primitive (issue #1564) is meant to make "
                "impossible across the refactored stores."
            )
    finally:
        for p in procs:
            if p.is_alive():
                p.kill()
                p.join(timeout=2)

    elapsed = time.time() - started
    print(f"\n[test_concurrent_writes_across_all_stores] elapsed={elapsed:.2f}s")

    # Per-store invariants
    expected = n_workers * n_ops

    cache = SQLiteCache(tmp_path / "cache.sqlite")
    try:
        cache_total = cache.stats()["total"]
        assert cache_total == expected, (
            f"cache total: expected {expected} (last-write-wins keyed by "
            f"sample_id), got {cache_total}"
        )
    finally:
        cache.close()

    reg = CampaignRegistry(tmp_path / "registry.db")
    reg_rows: list[CampaignRecord] = reg.list_campaigns()
    assert len(reg_rows) == expected, f"registry rows: expected {expected}, got {len(reg_rows)}"
    # Spot-check a round-tripped metadata blob exercises encode_value +
    # decode_value from the shared primitive.
    spot_record: CampaignRecord | None = next(
        iter([r for r in reg_rows if r.id == "w0-camp-005"]), None
    )
    assert spot_record is not None, "missing w0-camp-005 from registry storm"
    assert spot_record.metadata == {"worker": 0, "op": 5}, (
        f"registry metadata round-trip broken: {spot_record.metadata}"
    )

    rdb = ResultsDatabase(tmp_path / "results.db")
    try:
        rdb_total = rdb.n_results()
        assert rdb_total == expected, f"results_db total: expected {expected}, got {rdb_total}"
    finally:
        rdb.close()

    doc_store = SQLiteDocumentStore(tmp_path / "docs.sqlite")
    try:
        docs_total = doc_store.count_documents("kpis")
        assert docs_total == expected, (
            f"document_store 'kpis' count: expected {expected}, got {docs_total}"
        )
        # Spot-check JSON round-trip from the shared encode_value /
        # decode_value helpers.
        spot = doc_store.find_one("kpis", {"sample_id": "w2-s017"})
        assert spot is not None and spot["worker"] == 2 and spot["eui"] == 217.0, (
            f"document_store JSON round-trip broken: {spot}"
        )
    finally:
        doc_store.close()
