"""Unit tests for osimflow/document_store.py (issue #389).

Covers:
- DocumentStoreError hierarchy
- SQLiteDocumentStore: insert_one, find_one, find_many
- Update operators: $set, $unset, $inc, $push, $pull
- Comparison operators: $eq, $ne, $gt, $gte, $lt, $lte, $in, $nin, $exists, $regex
- create_index with unique constraint
- list_collections and count_documents
- delete_one
- Context manager and close
"""

from __future__ import annotations

from pathlib import Path

import pytest

from osimflow.document_store import (
    DocumentNotFoundError,
    DocumentStoreError,
    DuplicateDocumentError,
    SQLiteDocumentStore,
    _build_where_clause,
    build_document_store,
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
