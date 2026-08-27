"""Unit tests for osimflow/document_store.py (issue #389, #1014).

Covers:
- DocumentStoreError hierarchy
- SQLiteDocumentStore: insert_one, find_one, find_many
- Update operators: $set, $unset, $inc, $push, $pull
- Comparison operators: $eq, $ne, $gt, $gte, $lt, $lte, $in, $nin, $exists, $regex
- create_index with unique constraint
- list_collections and count_documents
- delete_one
- Context manager and close
- RedisDocumentStore (issue #1014): same ABC contract as the SQLite
  backend, exercised against ``fakeredis`` so the tests stay
  hermetic.  Includes the multi-process race regression test that
  mirrors the T8.1 reproducer the issue describes.
- ``build_document_store`` factory: legacy ``backend=`` dispatch
  plus the new ``redis_url`` / ``namespace`` dispatch that mirrors
  ``build_cache``.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from osimflow.document_store import (
    DocumentNotFoundError,
    DocumentStoreError,
    DuplicateDocumentError,
    RedisDocumentStore,
    SQLiteDocumentStore,
    _build_where_clause,
    _document_matches,
    apply_update_operators,
    build_document_store,
)

# fakeredis is in the dev optional-dependency set (issue #1014).  Tests
# skip cleanly when the optional dev dep is absent so a fresh CI
# install that has not yet run ``make install`` only loses the
# Redis-specific assertions, not the entire suite.
try:
    import fakeredis  # noqa: F401

    _HAS_FAKEREDIS = True
except ImportError:
    _HAS_FAKEREDIS = False

requires_fakeredis = pytest.mark.skipif(
    not _HAS_FAKEREDIS, reason="fakeredis optional dev dep not installed"
)


class TestBuildWhereClause:
    """Tests for the WHERE clause builder helper."""

    def test_empty_filter(self) -> None:
        clause, params = _build_where_clause({})
        assert clause == "1=1"
        assert params == []

    def test_equality(self) -> None:
        clause, params = _build_where_clause({"sample_id": "s0001"})
        assert clause == "json_extract(doc, '$.sample_id') = ?"
        assert params == ["s0001"]

    def test_multiple_equality(self) -> None:
        clause, params = _build_where_clause({"sample_id": "s0001", "status": "ok"})
        assert "json_extract(doc, '$.sample_id') = ?" in clause
        assert "json_extract(doc, '$.status') = ?" in clause
        assert params == ["s0001", "ok"]

    def test_gt_operator(self) -> None:
        clause, params = _build_where_clause({"eui": {"$gt": 100}})
        assert clause == "CAST(json_extract(doc, '$.eui') AS REAL) > ?"
        assert params == [100]

    def test_gte_operator(self) -> None:
        clause, params = _build_where_clause({"eui": {"$gte": 100}})
        assert clause == "CAST(json_extract(doc, '$.eui') AS REAL) >= ?"
        assert params == [100]

    def test_lt_operator(self) -> None:
        clause, params = _build_where_clause({"eui": {"$lt": 100}})
        assert clause == "CAST(json_extract(doc, '$.eui') AS REAL) < ?"
        assert params == [100]

    def test_lte_operator(self) -> None:
        clause, params = _build_where_clause({"eui": {"$lte": 100}})
        assert clause == "CAST(json_extract(doc, '$.eui') AS REAL) <= ?"
        assert params == [100]

    def test_ne_operator(self) -> None:
        clause, params = _build_where_clause({"status": {"$ne": "failed"}})
        assert clause == "json_extract(doc, '$.status') != ?"
        assert params == ["failed"]

    def test_in_operator(self) -> None:
        clause, params = _build_where_clause({"status": {"$in": ["ok", "cached"]}})
        assert clause == "json_extract(doc, '$.status') IN (?,?)"
        assert params == ["ok", "cached"]

    def test_nin_operator(self) -> None:
        clause, params = _build_where_clause({"status": {"$nin": ["failed", "unknown"]}})
        assert clause == "json_extract(doc, '$.status') NOT IN (?,?)"
        assert params == ["failed", "unknown"]

    def test_exists_true(self) -> None:
        clause, params = _build_where_clause({"eui": {"$exists": True}})
        assert clause == "json_extract(doc, '$.eui') IS NOT NULL"
        assert params == []

    def test_exists_false(self) -> None:
        clause, params = _build_where_clause({"eui": {"$exists": False}})
        assert clause == "json_extract(doc, '$.eui') IS NULL"
        assert params == []

    def test_regex_operator(self) -> None:
        clause, params = _build_where_clause({"name": {"$regex": "%test%"}})
        assert clause == "json_extract(doc, '$.name') LIKE ?"
        assert params == ["%test%"]


class TestSQLiteDocumentStore:
    """Tests for SQLiteDocumentStore implementation."""

    @pytest.fixture
    def store(self, tmp_path: Path) -> SQLiteDocumentStore:
        db_path = tmp_path / "test_docs.db"
        return SQLiteDocumentStore(db_path=db_path)

    def test_insert_one_generates_id(self, store: SQLiteDocumentStore) -> None:
        doc = {"name": "test", "value": 42}
        doc_id = store.insert_one("test_collection", doc)
        assert doc_id is not None
        assert len(doc_id) > 0

    def test_insert_one_with_existing_id(self, store: SQLiteDocumentStore) -> None:
        doc = {"_id": "custom_id", "name": "test"}
        doc_id = store.insert_one("test_collection", doc)
        assert doc_id == "custom_id"

    def test_find_one_returns_document(self, store: SQLiteDocumentStore) -> None:
        doc = {"sample_id": "s0001", "eui": 150.5}
        store.insert_one("kpis", doc)
        found = store.find_one("kpis", {"sample_id": "s0001"})
        assert found is not None
        assert found["sample_id"] == "s0001"
        assert found["eui"] == 150.5

    def test_find_one_returns_none_when_not_found(self, store: SQLiteDocumentStore) -> None:
        found = store.find_one("kpis", {"sample_id": "nonexistent"})
        assert found is None

    def test_find_one_with_no_filter(self, store: SQLiteDocumentStore) -> None:
        doc = {"name": "test"}
        store.insert_one("test_collection", doc)
        found = store.find_one("test_collection")
        assert found is not None
        assert found["name"] == "test"

    def test_find_many_returns_matching(self, store: SQLiteDocumentStore) -> None:
        store.insert_one("kpis", {"sample_id": "s0001", "eui": 150.0})
        store.insert_one("kpis", {"sample_id": "s0002", "eui": 200.0})
        store.insert_one("kpis", {"sample_id": "s0003", "eui": 100.0})
        found = store.find_many("kpis", {"eui": {"$gt": 150}})
        assert len(found) == 1
        assert found[0]["sample_id"] == "s0002"

    def test_find_many_with_limit(self, store: SQLiteDocumentStore) -> None:
        for i in range(10):
            store.insert_one("test", {"index": i})
        found = store.find_many("test", limit=3)
        assert len(found) == 3

    def test_find_many_with_skip(self, store: SQLiteDocumentStore) -> None:
        for i in range(10):
            store.insert_one("test", {"index": i})
        found = store.find_many("test", sort=[("index", 1)], skip=5)
        assert len(found) == 5
        assert found[0]["index"] == 5

    def test_find_many_with_sort_ascending(self, store: SQLiteDocumentStore) -> None:
        store.insert_one("test", {"index": 3})
        store.insert_one("test", {"index": 1})
        store.insert_one("test", {"index": 2})
        found = store.find_many("test", sort=[("index", 1)])
        assert [d["index"] for d in found] == [1, 2, 3]

    def test_find_many_with_sort_descending(self, store: SQLiteDocumentStore) -> None:
        store.insert_one("test", {"index": 3})
        store.insert_one("test", {"index": 1})
        store.insert_one("test", {"index": 2})
        found = store.find_many("test", sort=[("index", -1)])
        assert [d["index"] for d in found] == [3, 2, 1]

    def test_update_one_with_set(self, store: SQLiteDocumentStore) -> None:
        store.insert_one("kpis", {"sample_id": "s0001", "eui": 150.5})
        updated = store.update_one(
            "kpis",
            {"sample_id": "s0001"},
            {"$set": {"eui": 160.0}},
        )
        assert updated is True
        found = store.find_one("kpis", {"sample_id": "s0001"})
        assert found is not None
        assert found["eui"] == 160.0

    def test_update_one_with_unset(self, store: SQLiteDocumentStore) -> None:
        store.insert_one("kpis", {"sample_id": "s0001", "eui": 150.5, "note": "test"})
        updated = store.update_one(
            "kpis",
            {"sample_id": "s0001"},
            {"$unset": {"note": True}},
        )
        assert updated is True
        found = store.find_one("kpis", {"sample_id": "s0001"})
        assert found is not None
        assert "note" not in found

    def test_update_one_with_inc(self, store: SQLiteDocumentStore) -> None:
        store.insert_one("kpis", {"sample_id": "s0001", "count": 10})
        updated = store.update_one(
            "kpis",
            {"sample_id": "s0001"},
            {"$inc": {"count": 5}},
        )
        assert updated is True
        found = store.find_one("kpis", {"sample_id": "s0001"})
        assert found is not None
        assert found["count"] == 15

    def test_update_one_with_push(self, store: SQLiteDocumentStore) -> None:
        store.insert_one("kpis", {"sample_id": "s0001", "tags": []})
        updated = store.update_one(
            "kpis",
            {"sample_id": "s0001"},
            {"$push": {"tags": "new_tag"}},
        )
        assert updated is True
        found = store.find_one("kpis", {"sample_id": "s0001"})
        assert found is not None
        assert "new_tag" in found["tags"]

    def test_update_one_with_pull(self, store: SQLiteDocumentStore) -> None:
        store.insert_one("kpis", {"sample_id": "s0001", "tags": ["a", "b", "c"]})
        updated = store.update_one(
            "kpis",
            {"sample_id": "s0001"},
            {"$pull": {"tags": "b"}},
        )
        assert updated is True
        found = store.find_one("kpis", {"sample_id": "s0001"})
        assert found is not None
        assert found["tags"] == ["a", "c"]

    def test_update_one_returns_false_when_not_found(self, store: SQLiteDocumentStore) -> None:
        updated = store.update_one(
            "kpis",
            {"sample_id": "nonexistent"},
            {"$set": {"eui": 160.0}},
        )
        assert updated is False

    def test_delete_one(self, store: SQLiteDocumentStore) -> None:
        store.insert_one("kpis", {"sample_id": "s0001", "eui": 150.5})
        deleted = store.delete_one("kpis", {"sample_id": "s0001"})
        assert deleted is True
        found = store.find_one("kpis", {"sample_id": "s0001"})
        assert found is None

    def test_delete_one_returns_false_when_not_found(self, store: SQLiteDocumentStore) -> None:
        deleted = store.delete_one("kpis", {"sample_id": "nonexistent"})
        assert deleted is False

    def test_create_index(self, store: SQLiteDocumentStore) -> None:
        store.insert_one("kpis", {"sample_id": "s0001", "eui": 150.5})
        store.create_index("kpis", "sample_id", unique=True)
        # Index creation should not raise

    def test_list_collections(self, store: SQLiteDocumentStore) -> None:
        store.insert_one("collection_a", {"doc": 1})
        store.insert_one("collection_b", {"doc": 2})
        collections = store.list_collections()
        assert "collection_a" in collections
        assert "collection_b" in collections

    def test_count_documents(self, store: SQLiteDocumentStore) -> None:
        store.insert_one("kpis", {"sample_id": "s0001"})
        store.insert_one("kpis", {"sample_id": "s0002"})
        store.insert_one("kpis", {"sample_id": "s0003"})
        count = store.count_documents("kpis")
        assert count == 3

    def test_count_documents_with_filter(self, store: SQLiteDocumentStore) -> None:
        store.insert_one("kpis", {"sample_id": "s0001", "eui": 150.0})
        store.insert_one("kpis", {"sample_id": "s0002", "eui": 200.0})
        store.insert_one("kpis", {"sample_id": "s0003", "eui": 100.0})
        count = store.count_documents("kpis", {"eui": {"$gt": 150}})
        assert count == 1

    def test_context_manager(self, store: SQLiteDocumentStore) -> None:
        with store:
            store.insert_one("test", {"doc": 1})
        # Should not raise on close

    def test_close_idempotent(self, store: SQLiteDocumentStore) -> None:
        store.close()
        store.close()  # Should not raise

    def test_close_does_not_use_truncate_checkpoint(self, tmp_path: Path) -> None:
        """Issue #1006 invariant: ``close()`` MUST NOT execute TRUNCATE.

        A TRUNCATE checkpoint removes the ``-wal``/``-shm`` aux files out
        from under peer ``SQLiteDocumentStore`` instances that still have
        the same ``db_path`` open, crashing the peers' next write with
        ``FileNotFoundError: <db>.sqlite-shm``. This test inspects the
        source to prevent a silent regression: it fails only on an
        executable ``execute(...)`` call that issues
        ``wal_checkpoint(TRUNCATE)``, not on prose mentions in
        docstrings/comments (which explain why we avoid it).
        """
        import re

        source = (Path(__file__).resolve().parents[2] / "osimflow" / "document_store.py").read_text(
            encoding="utf-8"
        )
        executable_truncate = re.findall(
            r'\.execute\(\s*["\']PRAGMA\s*wal_checkpoint\(TRUNCATE\)["\']',
            source,
            flags=re.IGNORECASE,
        )
        assert not executable_truncate, (
            "SQLiteDocumentStore must not execute wal_checkpoint(TRUNCATE) "
            "— it removes the -wal/-shm aux files and crashes peer worker "
            "processes with FileNotFoundError (issue #1006). Use PASSIVE, "
            "matching SQLiteCache.close() (issue #620)."
        )
        assert "wal_checkpoint(PASSIVE)" in source

    def test_close_does_not_remove_wal_aux_files(self, tmp_path: Path) -> None:
        """Issue #1006 regression: ``close()`` must not crash a peer
        ``SQLiteDocumentStore`` instance that is still working against
        the same ``db_path``.

        Before the fix, ``close()`` ran ``PRAGMA wal_checkpoint(TRUNCATE)``
        which removed the auxiliary ``<db>.sqlite-wal`` and
        ``<db>.sqlite-shm`` files out from under the peer store's still-
        open connection. The peer's next ``insert_one``/``find_one``
        then crashed with ``FileNotFoundError`` on the now-missing aux
        file. After the fix, ``close()`` uses ``wal_checkpoint(PASSIVE)``
        which never removes the aux files; the peer store's subsequent
        reads and writes succeed cleanly.

        Reproduces the scenario described in the issue:

        1. Open store A against ``db_path``.
        2. Write through A so the WAL is populated.
        3. Open store B (the peer) against the same ``db_path`` —
           mirrors the multi-worker campaign shape.
        4. ``A.close()`` — must not break B's connection.
        5. ``B.insert_one`` + ``B.find_one`` must succeed without
           raising ``FileNotFoundError`` on the aux files.

        Note: we do not assert on the ``-wal``/``-shm`` files'
        *existence* — SQLite lazily creates those files and may reclaim
        an empty WAL on close regardless of checkpoint policy. What
        matters (and what the old ``TRUNCATE`` behavior broke) is that
        a *peer connection* still using the same DB can keep writing.
        """
        db_path = tmp_path / "test_close_wal_aux.sqlite"
        store_a = SQLiteDocumentStore(db_path=db_path)
        # Pre-populate the WAL so the aux files are in active use.
        store_a.insert_one("kpis", {"sample_id": "from_a", "eui": 100.0})

        # Peer store against the same path — the multi-worker shape
        # from issue #1006.
        store_b = SQLiteDocumentStore(db_path=db_path)

        # Tear down A. With the old TRUNCATE behavior this removed the
        # -wal/-shm files out from under B mid-connection; with PASSIVE
        # the aux files stay in place and B keeps working.
        store_a.close()

        # Peer's next write/read must succeed — the regression manifested
        # as FileNotFoundError on the aux file under the old behavior.
        # We assert the actual data round-trips rather than poking at
        # the aux file paths (those are managed by SQLite).
        store_b.insert_one("kpis", {"sample_id": "from_b", "eui": 200.0})
        found_a = store_b.find_one("kpis", {"sample_id": "from_a"})
        assert found_a is not None
        assert found_a["eui"] == 100.0
        found_b = store_b.find_one("kpis", {"sample_id": "from_b"})
        assert found_b is not None
        assert found_b["eui"] == 200.0
        # count_documents exercises a separate query path; cover it too.
        assert store_b.count_documents("kpis") == 2
        store_b.close()

    def test_insert_one_to_new_collection(self, store: SQLiteDocumentStore) -> None:
        doc = {"name": "test", "value": 42}
        doc_id = store.insert_one("new_collection", doc)
        assert doc_id is not None
        collections = store.list_collections()
        assert "new_collection" in collections


class TestBuildDocumentStore:
    """Tests for the document store factory."""

    def test_sqlite_backend(self, tmp_path: Path) -> None:
        db_path = tmp_path / "test.db"
        store = build_document_store(backend="sqlite", db_path=db_path)
        assert isinstance(store, SQLiteDocumentStore)
        assert store.name == "sqlite"
        store.close()

    def test_unknown_backend_raises(self, tmp_path: Path) -> None:
        db_path = tmp_path / "test.db"
        with pytest.raises(ValueError, match="unknown document_store_backend"):
            build_document_store(backend="unknown", db_path=db_path)


class TestDocumentStoreErrorHierarchy:
    """Tests for the error hierarchy."""

    def test_document_store_error_is_base(self) -> None:
        assert issubclass(DocumentStoreError, Exception)

    def test_document_not_found_error(self) -> None:
        assert issubclass(DocumentNotFoundError, DocumentStoreError)

    def test_duplicate_document_error(self) -> None:
        assert issubclass(DuplicateDocumentError, DocumentStoreError)


# ---------------------------------------------------------------------------
# Issue #1014: RedisDocumentStore + factory + race scenarios
# ---------------------------------------------------------------------------


class TestApplyUpdateOperators:
    """Tests for the module-level update-operator helper.

    Both ``SQLiteDocumentStore`` and ``RedisDocumentStore`` share this
    implementation (issue #1014).  The class-method wrapper on
    ``SQLiteDocumentStore`` delegates here.
    """

    def test_set(self) -> None:
        doc: dict[str, object] = {"a": 1}
        apply_update_operators(doc, {"$set": {"a": 2, "b": 3}})
        assert doc == {"a": 2, "b": 3}

    def test_unset(self) -> None:
        doc: dict[str, object] = {"a": 1, "b": 2}
        apply_update_operators(doc, {"$unset": {"a": True}})
        assert doc == {"b": 2}

    def test_inc(self) -> None:
        doc: dict[str, object] = {"count": 10}
        apply_update_operators(doc, {"$inc": {"count": 5}})
        assert doc == {"count": 15}

    def test_push(self) -> None:
        doc: dict[str, object] = {"tags": ["a"]}
        apply_update_operators(doc, {"$push": {"tags": "b"}})
        assert doc == {"tags": ["a", "b"]}

    def test_pull(self) -> None:
        doc: dict[str, object] = {"tags": ["a", "b", "c"]}
        apply_update_operators(doc, {"$pull": {"tags": "b"}})
        assert doc == {"tags": ["a", "c"]}

    def test_unknown_operator_is_logged_and_skipped(self) -> None:
        doc: dict[str, object] = {"a": 1}
        apply_update_operators(doc, {"$unknown": {"a": 2}})
        assert doc == {"a": 1}


class TestDocumentMatches:
    """Tests for the client-side filter helper used by RedisDocumentStore."""

    def test_empty_filter_matches_anything(self) -> None:
        assert _document_matches({"a": 1}, None) is True
        assert _document_matches({"a": 1}, {}) is True

    def test_eq(self) -> None:
        assert _document_matches({"x": 1}, {"x": 1}) is True
        assert _document_matches({"x": 2}, {"x": 1}) is False

    def test_comparison_ops(self) -> None:
        assert _document_matches({"n": 5}, {"n": {"$gt": 1}}) is True
        assert _document_matches({"n": 5}, {"n": {"$gt": 10}}) is False
        assert _document_matches({"n": 5}, {"n": {"$gte": 5}}) is True
        assert _document_matches({"n": 5}, {"n": {"$lt": 10}}) is True
        assert _document_matches({"n": 5}, {"n": {"$lte": 5}}) is True
        assert _document_matches({"n": 5}, {"n": {"$ne": 6}}) is True

    def test_in_nin(self) -> None:
        assert _document_matches({"x": "a"}, {"x": {"$in": ["a", "b"]}}) is True
        assert _document_matches({"x": "c"}, {"x": {"$in": ["a", "b"]}}) is False
        assert _document_matches({"x": "c"}, {"x": {"$nin": ["a", "b"]}}) is True
        assert _document_matches({"x": "a"}, {"x": {"$nin": ["a", "b"]}}) is False

    def test_exists(self) -> None:
        assert _document_matches({"x": 1}, {"x": {"$exists": True}}) is True
        assert _document_matches({}, {"x": {"$exists": False}}) is True
        assert _document_matches({"x": 1}, {"x": {"$exists": False}}) is False
        assert _document_matches({}, {"x": {"$exists": True}}) is False

    def test_regex(self) -> None:
        assert _document_matches({"name": "abc123"}, {"name": {"$regex": "abc"}}) is True
        assert _document_matches({"name": "xyz"}, {"name": {"$regex": "abc"}}) is False


@requires_fakeredis
class TestRedisDocumentStore:
    """Tests for ``RedisDocumentStore`` (issue #1014)."""

    @pytest.fixture
    def fake_redis(self) -> fakeredis.FakeRedis:  # type: ignore[name-defined]
        return fakeredis.FakeRedis(decode_responses=True)

    @pytest.fixture
    def store(self, fake_redis: fakeredis.FakeRedis) -> RedisDocumentStore:  # type: ignore[name-defined]
        store = RedisDocumentStore(
            redis_url="redis://localhost:6379/0",
            namespace="test-ns",
            db_path=Path("/tmp/documents.sqlite"),
        )
        # Inject fakeredis so the test never opens a real socket.
        store._redis_client = fake_redis
        return store

    def test_name(self, store: RedisDocumentStore) -> None:
        assert store.name == "redis"

    def test_insert_one_generates_id(self, store: RedisDocumentStore) -> None:
        doc_id = store.insert_one("kpis", {"sample_id": "s0001", "eui": 150.4})
        assert doc_id == "doc_1"
        # Counter is shared across the process — second insert uses 2.
        doc_id2 = store.insert_one("kpis", {"sample_id": "s0002", "eui": 200.0})
        assert doc_id2 == "doc_2"

    def test_insert_one_with_existing_id(self, store: RedisDocumentStore) -> None:
        doc_id = store.insert_one("kpis", {"_id": "custom_id", "sample_id": "s0001"})
        assert doc_id == "custom_id"

    def test_find_one_by_id(self, store: RedisDocumentStore) -> None:
        store.insert_one("kpis", {"sample_id": "s0001", "eui": 150.5})
        doc = store.find_one("kpis", {"sample_id": "s0001"})
        assert doc is not None
        assert doc["sample_id"] == "s0001"
        assert doc["eui"] == 150.5

    def test_find_one_returns_none_when_not_found(self, store: RedisDocumentStore) -> None:
        assert store.find_one("kpis", {"sample_id": "nope"}) is None

    def test_find_one_with_no_filter(self, store: RedisDocumentStore) -> None:
        store.insert_one("test", {"name": "foo"})
        doc = store.find_one("test")
        assert doc is not None
        assert doc["name"] == "foo"

    def test_find_many_returns_matching(self, store: RedisDocumentStore) -> None:
        store.insert_one("kpis", {"sample_id": "s0001", "eui": 150.0})
        store.insert_one("kpis", {"sample_id": "s0002", "eui": 200.0})
        store.insert_one("kpis", {"sample_id": "s0003", "eui": 100.0})
        docs = store.find_many("kpis", {"eui": {"$gt": 150}})
        ids = sorted(d["sample_id"] for d in docs)
        assert ids == ["s0002"]

    def test_find_many_with_limit(self, store: RedisDocumentStore) -> None:
        for i in range(10):
            store.insert_one("test", {"index": i})
        docs = store.find_many("test", limit=3)
        assert len(docs) == 3

    def test_find_many_with_skip(self, store: RedisDocumentStore) -> None:
        for i in range(10):
            store.insert_one("test", {"index": i})
        docs = store.find_many("test", sort=[("index", 1)], skip=5)
        assert len(docs) == 5
        assert docs[0]["index"] == 5

    def test_find_many_with_sort(self, store: RedisDocumentStore) -> None:
        for i in [3, 1, 2]:
            store.insert_one("test", {"index": i})
        asc = store.find_many("test", sort=[("index", 1)])
        desc = store.find_many("test", sort=[("index", -1)])
        assert [d["index"] for d in asc] == [1, 2, 3]
        assert [d["index"] for d in desc] == [3, 2, 1]

    def test_update_one_with_set(self, store: RedisDocumentStore) -> None:
        store.insert_one("kpis", {"sample_id": "s0001", "eui": 150.5})
        assert store.update_one("kpis", {"sample_id": "s0001"}, {"$set": {"eui": 160.0}}) is True
        doc = store.find_one("kpis", {"sample_id": "s0001"})
        assert doc is not None
        assert doc["eui"] == 160.0

    def test_update_one_with_unset(self, store: RedisDocumentStore) -> None:
        store.insert_one("kpis", {"sample_id": "s0001", "eui": 150.5, "note": "test"})
        assert store.update_one("kpis", {"sample_id": "s0001"}, {"$unset": {"note": True}}) is True
        doc = store.find_one("kpis", {"sample_id": "s0001"})
        assert doc is not None
        assert "note" not in doc

    def test_update_one_with_inc(self, store: RedisDocumentStore) -> None:
        store.insert_one("kpis", {"sample_id": "s0001", "count": 10})
        assert store.update_one("kpis", {"sample_id": "s0001"}, {"$inc": {"count": 5}}) is True
        doc = store.find_one("kpis", {"sample_id": "s0001"})
        assert doc is not None
        assert doc["count"] == 15

    def test_update_one_with_push(self, store: RedisDocumentStore) -> None:
        store.insert_one("kpis", {"sample_id": "s0001", "tags": []})
        assert store.update_one("kpis", {"sample_id": "s0001"}, {"$push": {"tags": "new"}}) is True
        doc = store.find_one("kpis", {"sample_id": "s0001"})
        assert doc is not None
        assert "new" in doc["tags"]

    def test_update_one_with_pull(self, store: RedisDocumentStore) -> None:
        store.insert_one("kpis", {"sample_id": "s0001", "tags": ["a", "b", "c"]})
        assert store.update_one("kpis", {"sample_id": "s0001"}, {"$pull": {"tags": "b"}}) is True
        doc = store.find_one("kpis", {"sample_id": "s0001"})
        assert doc is not None
        assert doc["tags"] == ["a", "c"]

    def test_update_one_returns_false_when_not_found(self, store: RedisDocumentStore) -> None:
        assert store.update_one("kpis", {"sample_id": "nope"}, {"$set": {"eui": 1.0}}) is False

    def test_delete_one(self, store: RedisDocumentStore) -> None:
        store.insert_one("kpis", {"sample_id": "s0001", "eui": 150.5})
        assert store.delete_one("kpis", {"sample_id": "s0001"}) is True
        assert store.find_one("kpis", {"sample_id": "s0001"}) is None

    def test_delete_one_returns_false_when_not_found(self, store: RedisDocumentStore) -> None:
        assert store.delete_one("kpis", {"sample_id": "nope"}) is False

    def test_create_index_unique(self, store: RedisDocumentStore) -> None:
        store.insert_one("kpis", {"sample_id": "s0001", "eui": 150.5})
        store.create_index("kpis", "sample_id", unique=True)
        with pytest.raises(DuplicateDocumentError):
            store.insert_one("kpis", {"sample_id": "s0001", "eui": 999.0})

    def test_list_collections(self, store: RedisDocumentStore) -> None:
        store.insert_one("a", {"v": 1})
        store.insert_one("b", {"v": 2})
        assert sorted(store.list_collections()) == ["a", "b"]

    def test_count_documents(self, store: RedisDocumentStore) -> None:
        for i in range(5):
            store.insert_one("kpis", {"sample_id": f"s{i:04d}"})
        assert store.count_documents("kpis") == 5

    def test_count_documents_with_filter(self, store: RedisDocumentStore) -> None:
        store.insert_one("kpis", {"sample_id": "s0001", "eui": 150.0})
        store.insert_one("kpis", {"sample_id": "s0002", "eui": 200.0})
        store.insert_one("kpis", {"sample_id": "s0003", "eui": 100.0})
        assert store.count_documents("kpis", {"eui": {"$gt": 120}}) == 2

    def test_close_is_idempotent(self, store: RedisDocumentStore) -> None:
        store.close()
        store.close()  # must not raise

    def test_context_manager(self, store: RedisDocumentStore) -> None:
        with store:
            store.insert_one("test", {"v": 1})

    def test_lru_hits_avoid_redis(self, store: RedisDocumentStore) -> None:
        """The LRU absorbs repeated reads so a common read path does not
        hit Redis on every call (issue #1014)."""
        store.insert_one("kpis", {"sample_id": "s0001", "eui": 150.5})
        # First read primes the LRU.
        store.find_one("kpis", {"sample_id": "s0001"})
        cached = store._lru_get("kpis", "doc_1")
        assert cached is not None
        assert cached["eui"] == 150.5

    def test_lru_eviction_when_full(self, fake_redis: fakeredis.FakeRedis) -> None:  # type: ignore[name-defined]
        store = RedisDocumentStore(
            redis_url="redis://localhost:6379/0",
            namespace="evict",
            lru_max_entries=2,
        )
        store._redis_client = fake_redis
        store.insert_one("c", {"_id": "a", "v": 1})
        store.insert_one("c", {"_id": "b", "v": 2})
        store.insert_one("c", {"_id": "c", "v": 3})
        # Touch some entries to warm the LRU.
        store.find_one("c", {"_id": "a"})
        store.find_one("c", {"_id": "b"})
        store.find_one("c", {"_id": "c"})
        # LRU is bounded.
        assert len(store._lru) <= 2


@requires_fakeredis
class TestRedisDocumentStoreRaceScenario:
    """Regression test for the T8.1-style multi-process race.

    Mirrors the issue #1014 acceptance criterion: N concurrent
    processes writing distinct IDs to the same ``DocumentStore`` must
    produce a single consistent DB view.  Threads stand in for
    processes here; the in-process RedisDocumentStore + shared
    fakeredis client simulates the cross-process shared state.
    """

    def test_concurrent_workers_distinct_ids(self) -> None:
        # type: ignore[name-defined]
        import fakeredis

        shared = fakeredis.FakeRedis(decode_responses=True)
        namespace = "campaign-outdir-deadbeef"
        n_workers = 8
        ops_per_worker = 25
        errors: list[Exception] = []
        lock = threading.Lock()

        def worker(worker_id: int) -> None:
            try:
                s = RedisDocumentStore(
                    redis_url="redis://fake",
                    namespace=namespace,
                )
                s._redis_client = shared
                for i in range(ops_per_worker):
                    doc_id = f"w{worker_id}_doc_{i}"
                    s.insert_one(
                        "kpis",
                        {"_id": doc_id, "worker": worker_id, "index": i},
                    )
                # Verify every doc we wrote is independently readable.
                for i in range(ops_per_worker):
                    doc_id = f"w{worker_id}_doc_{i}"
                    doc = s.find_one("kpis", {"_id": doc_id})
                    if doc is None:
                        with lock:
                            errors.append(
                                AssertionError(f"worker {worker_id} missing doc {doc_id}")
                            )
                s.close()
            except Exception as e:  # noqa: BLE001
                with lock:
                    errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_workers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # All workers must have completed without errors.
        assert not errors, f"concurrent errors: {errors}"

        # Independent reader sees the union of distinct ids.
        reader = RedisDocumentStore(
            redis_url="redis://fake",
            namespace=namespace,
        )
        reader._redis_client = shared
        try:
            docs = reader.find_many("kpis")
            ids = [d["_id"] for d in docs]
            assert len(ids) == n_workers * ops_per_worker, (
                f"expected {n_workers * ops_per_worker} docs, got {len(ids)}"
            )
            assert len(set(ids)) == len(ids), "duplicate doc_ids (collision)"
            # The auto-incremented _id space must also be unique.
            nonce_ids = [d["_id"] for d in docs if d["_id"].startswith("doc_")]
            assert len(set(nonce_ids)) == len(nonce_ids), (
                "auto-increment counter collided across workers"
            )
        finally:
            reader.close()

    def test_concurrent_workers_unique_index(self) -> None:
        # type: ignore[name-defined]
        """Concurrent inserts that violate a unique index raise
        DuplicateDocumentError on the offending worker only — the
        other workers still see a consistent state."""
        import fakeredis

        shared = fakeredis.FakeRedis(decode_responses=True)
        namespace = "race-unique"
        n_workers = 4
        # All workers try to insert the same sample_id; one wins,
        # three raise DuplicateDocumentError.
        errors: list[Exception] = []
        success: list[int] = []
        lock = threading.Lock()

        def worker(worker_id: int) -> None:
            try:
                s = RedisDocumentStore(
                    redis_url="redis://fake",
                    namespace=namespace,
                )
                s._redis_client = shared
                s.create_index("kpis", "sample_id", unique=True)
                s.insert_one(
                    "kpis",
                    {"sample_id": "s0001", "worker": worker_id},
                )
                with lock:
                    success.append(worker_id)
                s.close()
            except DuplicateDocumentError:
                pass  # expected for all but one worker
            except Exception as e:  # noqa: BLE001
                with lock:
                    errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_workers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, errors
        assert len(success) == 1, f"expected exactly 1 winner, got {len(success)}"


@requires_fakeredis
class TestBuildDocumentStoreRedisDispatch:
    """Factory tests for the ``redis_url`` dispatch path (issue #1014)."""

    def test_redis_url_dispatches_to_redis(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # type: ignore[name-defined]
        import fakeredis

        # Patch the lazy redis-sync import so build_document_store
        # never tries to open a real socket.
        fake = fakeredis.FakeRedis(decode_responses=True)
        fake_module = type("FakeModule", (), {"from_url": staticmethod(lambda *a, **k: fake)})
        monkeypatch.setattr("osimflow.document_store._get_redis_sync", lambda: fake_module)
        store = build_document_store(
            db_path=tmp_path / "documents.sqlite",
            redis_url="redis://localhost:6379/0",
            namespace="test-ns",
        )
        assert isinstance(store, RedisDocumentStore)
        assert store.name == "redis"
        store.close()

    def test_redis_url_without_namespace_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="requires a namespace"):
            build_document_store(
                db_path=tmp_path / "documents.sqlite",
                redis_url="redis://localhost:6379/0",
            )

    def test_redis_url_none_returns_sqlite(self, tmp_path: Path) -> None:
        store = build_document_store(
            db_path=tmp_path / "documents.sqlite",
            redis_url=None,
        )
        assert isinstance(store, SQLiteDocumentStore)
        store.close()

    def test_no_args_returns_sqlite(self, tmp_path: Path) -> None:
        store = build_document_store(db_path=tmp_path / "documents.sqlite")
        assert isinstance(store, SQLiteDocumentStore)
        store.close()

    def test_legacy_backend_redis(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # type: ignore[name-defined]
        import fakeredis

        fake = fakeredis.FakeRedis(decode_responses=True)
        fake_module = type("FakeModule", (), {"from_url": staticmethod(lambda *a, **k: fake)})
        monkeypatch.setattr("osimflow.document_store._get_redis_sync", lambda: fake_module)
        store = build_document_store(
            backend="redis",
            db_path=tmp_path / "documents.sqlite",
            redis_url="redis://localhost:6379/0",
            namespace="legacy",
        )
        assert isinstance(store, RedisDocumentStore)
        store.close()

    def test_legacy_backend_redis_without_redis_url_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="requires redis_url"):
            build_document_store(
                backend="redis",
                db_path=tmp_path / "documents.sqlite",
            )


@requires_fakeredis
class TestRedisDocumentStoreErrorPaths:
    """Error-path tests for RedisDocumentStore (issue #1220).

    AGENTS.md §8 gotcha #15 requires RedisDocumentStore to "fail loud"
    on Redis outages: every operation raises ``DocumentStoreError``,
    not silently degrade to local-only state.

    These tests simulate Redis failures at two levels:
    1. Circuit-breaker open (fail-fast after repeated failures).
    2. Individual Redis operations raising exceptions.
    """

    @pytest.fixture
    def store(self) -> RedisDocumentStore:
        import fakeredis

        fake = fakeredis.FakeRedis(decode_responses=True)
        store = RedisDocumentStore(
            redis_url="redis://localhost:6379/0",
            namespace="error-path-test",
            db_path=Path("/tmp/documents.sqlite"),
        )
        store._redis_client = fake
        return store

    def test_insert_one_raises_when_circuit_open(self, store: RedisDocumentStore) -> None:
        for _ in range(store._breaker.failure_threshold):
            store._breaker.record_failure()
        assert store._breaker.state == "open"
        with pytest.raises(DocumentStoreError, match="Redis unavailable"):
            store.insert_one("kpis", {"sample_id": "s0001"})

    def test_find_one_raises_when_circuit_open(self, store: RedisDocumentStore) -> None:
        for _ in range(store._breaker.failure_threshold):
            store._breaker.record_failure()
        assert store._breaker.state == "open"
        with pytest.raises(DocumentStoreError, match="Redis unavailable"):
            store.find_one("kpis", {"sample_id": "s0001"})

    def test_find_many_raises_when_circuit_open(self, store: RedisDocumentStore) -> None:
        for _ in range(store._breaker.failure_threshold):
            store._breaker.record_failure()
        assert store._breaker.state == "open"
        with pytest.raises(DocumentStoreError, match="Redis unavailable"):
            store.find_many("kpis", {})

    def test_update_one_raises_when_circuit_open(self, store: RedisDocumentStore) -> None:
        for _ in range(store._breaker.failure_threshold):
            store._breaker.record_failure()
        assert store._breaker.state == "open"
        with pytest.raises(DocumentStoreError, match="Redis unavailable"):
            store.update_one("kpis", {"sample_id": "s0001"}, {"$set": {"eui": 1.0}})

    def test_delete_one_raises_when_circuit_open(self, store: RedisDocumentStore) -> None:
        for _ in range(store._breaker.failure_threshold):
            store._breaker.record_failure()
        assert store._breaker.state == "open"
        with pytest.raises(DocumentStoreError, match="Redis unavailable"):
            store.delete_one("kpis", {"sample_id": "s0001"})

    def test_create_index_raises_when_circuit_open(self, store: RedisDocumentStore) -> None:
        for _ in range(store._breaker.failure_threshold):
            store._breaker.record_failure()
        assert store._breaker.state == "open"
        with pytest.raises(DocumentStoreError, match="Redis unavailable"):
            store.create_index("kpis", "sample_id")

    def test_list_collections_raises_when_circuit_open(self, store: RedisDocumentStore) -> None:
        for _ in range(store._breaker.failure_threshold):
            store._breaker.record_failure()
        assert store._breaker.state == "open"
        with pytest.raises(DocumentStoreError, match="Redis unavailable"):
            store.list_collections()

    def test_count_documents_raises_when_circuit_open(self, store: RedisDocumentStore) -> None:
        for _ in range(store._breaker.failure_threshold):
            store._breaker.record_failure()
        assert store._breaker.state == "open"
        with pytest.raises(DocumentStoreError, match="Redis unavailable"):
            store.count_documents("kpis")

    def test_insert_one_redis_failure_raises_document_store_error(
        self, store: RedisDocumentStore
    ) -> None:
        from unittest.mock import patch

        with patch.object(store._redis_client, "hset", side_effect=ConnectionError("Connection refused")):
            with pytest.raises(DocumentStoreError, match="insert_one failed"):
                store.insert_one("kpis", {"sample_id": "s0001", "eui": 150.5})

    def test_update_one_redis_failure_raises_document_store_error(
        self, store: RedisDocumentStore
    ) -> None:
        from unittest.mock import patch

        store.insert_one("kpis", {"_id": "doc_x", "sample_id": "s0001", "eui": 150.5})
        with patch.object(store._redis_client, "hset", side_effect=ConnectionError("Connection refused")):
            with pytest.raises(DocumentStoreError, match="update_one failed"):
                store.update_one("kpis", {"_id": "doc_x"}, {"$set": {"eui": 160.0}})

    def test_find_one_redis_failure_raises_document_store_error(
        self, store: RedisDocumentStore
    ) -> None:
        from unittest.mock import patch

        with patch.object(store._redis_client, "hget", side_effect=ConnectionError("Connection refused")):
            with pytest.raises(DocumentStoreError):
                store.find_one("kpis", {"_id": "doc_1"})

    def test_find_many_redis_failure_raises_document_store_error(
        self, store: RedisDocumentStore
    ) -> None:
        from unittest.mock import patch

        with patch.object(store._redis_client, "hgetall", side_effect=ConnectionError("Connection refused")):
            with pytest.raises(DocumentStoreError):
                store.find_many("kpis", {})

    def test_delete_one_redis_failure_raises_document_store_error(
        self, store: RedisDocumentStore
    ) -> None:
        from unittest.mock import patch

        store.insert_one("kpis", {"_id": "doc_del", "sample_id": "s0001", "eui": 150.5})
        with patch.object(store._redis_client, "hdel", side_effect=ConnectionError("Connection refused")):
            with pytest.raises(DocumentStoreError):
                store.delete_one("kpis", {"_id": "doc_del"})

    def test_list_collections_redis_failure_raises_document_store_error(
        self, store: RedisDocumentStore
    ) -> None:
        from unittest.mock import patch

        with patch.object(store._redis_client, "smembers", side_effect=ConnectionError("Connection refused")):
            with pytest.raises(DocumentStoreError):
                store.list_collections()

    def test_count_documents_redis_failure_raises_document_store_error(
        self, store: RedisDocumentStore
    ) -> None:
        from unittest.mock import patch

        with patch.object(store._redis_client, "hgetall", side_effect=ConnectionError("Connection refused")):
            with pytest.raises(DocumentStoreError):
                store.count_documents("kpis")
