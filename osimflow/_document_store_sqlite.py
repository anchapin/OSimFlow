"""SQLite/JSON1 document-store backend (issue #1890 split of ``osimflow.document_store``)."""

from __future__ import annotations

__all__ = ["SQLiteDocumentStore"]

import contextlib
import json
import logging
import sqlite3
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from ._document_store_base import (
    _FIELD_NAME_RE,
    DocumentStore,
    DocumentStoreError,
    DuplicateDocumentError,
    _validate_filter_key,
    apply_update_operators,
)
from ._sqlite_store import connect as _store_connect
from ._sqlite_store import encode_value as _encode_value

log = logging.getLogger("osimflow.document_store")


def _handle_eq_ne(key: str, op_value: Any) -> tuple[str, list[Any]]:
    """Handle $eq/$ne operators."""
    return f"json_extract(doc, '$.{key}') = ?", [op_value]


def _handle_ne(key: str, op_value: Any) -> tuple[str, list[Any]]:
    """Handle $ne operator."""
    return f"json_extract(doc, '$.{key}') != ?", [op_value]


def _handle_gt(key: str, op_value: Any) -> tuple[str, list[Any]]:
    """Handle $gt operator."""
    col = f"CAST(json_extract(doc, '$.{key}') AS REAL)"
    return f"{col} > ?", [op_value]


def _handle_gte(key: str, op_value: Any) -> tuple[str, list[Any]]:
    """Handle $gte operator."""
    col = f"CAST(json_extract(doc, '$.{key}') AS REAL)"
    return f"{col} >= ?", [op_value]


def _handle_lt(key: str, op_value: Any) -> tuple[str, list[Any]]:
    """Handle $lt operator."""
    col = f"CAST(json_extract(doc, '$.{key}') AS REAL)"
    return f"{col} < ?", [op_value]


def _handle_lte(key: str, op_value: Any) -> tuple[str, list[Any]]:
    """Handle $lte operator."""
    col = f"CAST(json_extract(doc, '$.{key}') AS REAL)"
    return f"{col} <= ?", [op_value]


def _handle_in(key: str, op_value: list[Any]) -> tuple[str, list[Any]]:
    """Handle $in operator."""
    placeholders = ",".join(["?" for _ in op_value])
    return f"json_extract(doc, '$.{key}') IN ({placeholders})", list(op_value)


def _handle_nin(key: str, op_value: list[Any]) -> tuple[str, list[Any]]:
    """Handle $nin operator."""
    placeholders = ",".join(["?" for _ in op_value])
    return f"json_extract(doc, '$.{key}') NOT IN ({placeholders})", list(op_value)


def _handle_exists(key: str, op_value: bool) -> tuple[str, list[Any]]:
    """Handle $exists operator."""
    if op_value:
        return f"json_extract(doc, '$.{key}') IS NOT NULL", []
    return f"json_extract(doc, '$.{key}') IS NULL", []


def _handle_regex(key: str, op_value: str) -> tuple[str, list[Any]]:
    """Handle $regex operator."""
    return f"json_extract(doc, '$.{key}') LIKE ?", [op_value]


# Dispatch table for comparison operators
_COMPARISON_HANDLERS: dict[str, Callable[..., tuple[str, list[Any]]]] = {
    "$eq": _handle_eq_ne,
    "$ne": _handle_ne,
    "$gt": _handle_gt,
    "$gte": _handle_gte,
    "$lt": _handle_lt,
    "$lte": _handle_lte,
    "$in": _handle_in,
    "$nin": _handle_nin,
    "$exists": _handle_exists,
    "$regex": _handle_regex,
}


def _build_where_clause(
    filter_spec: dict[str, Any],
) -> tuple[str, list[Any]]:
    """Build a SQLite WHERE clause from a MongoDB-style filter spec.

    Supports comparison operators: $eq, $ne, $gt, $gte, $lt, $lte,
    $in, $nin, $exists, $regex.

    Parameters
    ----------
    filter_spec
        MongoDB-style filter dictionary.

    Returns
    -------
    tuple[str, list[Any]]
        Tuple of (where_clause, parameter_values).
    """
    conditions: list[str] = []
    params: list[Any] = []

    for key, value in filter_spec.items():
        # Validate every filter key before any other access (issue #1779).
        # Keys are interpolated into the WHERE clause via
        # ``f"json_extract(doc, '$.{key}')"`` — values are bound, but
        # the path is not. Malicious keys like ``"foo'; DROP TABLE
        # documents; --"`` would otherwise yield a fully exploitable
        # SQL injection. ``_validate_filter_key`` also rejects
        # non-string keys (e.g. ``None``) which would otherwise crash
        # the ``startswith`` check below.
        _validate_filter_key(key)
        if key.startswith("$"):
            continue  # Skip logical operators at field level

        if isinstance(value, dict):
            for op, op_value in value.items():
                handler = _COMPARISON_HANDLERS.get(op)
                if handler:
                    cond, vals = handler(key, op_value)
                    conditions.append(cond)
                    params.extend(vals)
                else:
                    log.warning("unknown comparison operator: %s", op)
        else:
            conditions.append(f"json_extract(doc, '$.{key}') = ?")
            params.append(value)

    where_clause = " AND ".join(conditions) if conditions else "1=1"
    return where_clause, params


# ---------------------------------------------------------------------------
# SQLite implementation
# ---------------------------------------------------------------------------


class SQLiteDocumentStore(DocumentStore):
    """SQLite-backed document store using JSON1 extension.

    Provides MongoDB-equivalent document storage using SQLite as the
    underlying engine. Documents are stored as JSON strings in a
    single TEXT column, with indexes created using JSON1 extraction
    functions.

    This implementation is suitable for single-node campaigns and
    provides a persistent document store without requiring MongoDB.

    Thread safety
    -------------
    The implementation uses a single connection with WAL mode
    for thread-safe concurrent access, similar to SQLiteCache.
    """

    name = "sqlite"

    def __init__(self, db_path: Path) -> None:
        """Initialize the SQLite document store.

        Parameters
        ----------
        db_path
            Path to the SQLite database file. Parent directories
            are created if needed.
        """
        self.db_path = db_path
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.Lock()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()
        log.info("document store opened at %s", db_path)

    def _init_db(self) -> None:
        """Initialize the database schema.

        Issue #1564: connection setup (WAL + busy_timeout + row_factory +
        timeout + check_same_thread=False) now lives in
        :func:`osimflow._sqlite_store.connect`.  ``_connect`` below is
        kept for the subclass public ``connection`` property; it is a
        thin alias around the shared primitive.
        """
        conn = self._connect()
        conn.close()

    def _connect(self) -> sqlite3.Connection:
        """Create a new database connection.

        Issue #1564: shared SQLite access primitive — WAL, busy_timeout,
        synchronous=NORMAL, locking_mode=NORMAL, check_same_thread=False,
        timeout=10.0, and row_factory=sqlite3.Row now live in
        :mod:`osimflow._sqlite_store`.
        """
        return _store_connect(self.db_path)

    def _ensure_conn(self) -> sqlite3.Connection:
        """Return the persistent connection, creating it if needed."""
        if self._conn is None:
            self._conn = self._connect()
        return self._conn

    @property
    def connection(self) -> sqlite3.Connection:
        """Return the persistent connection (lazy initialization)."""
        return self._ensure_conn()

    def close(self) -> None:
        """Close the database connection.

        Uses ``PRAGMA wal_checkpoint(PASSIVE)`` — the SQLite default. A
        PASSIVE checkpoint copies as much WAL content as it can back into
        the main DB **without blocking** and **without removing** the
        auxiliary ``-wal``/``-shm`` files. Those files are left in place
        for SQLite to manage cooperatively across every process that
        shares the document store. This is what makes ``close()`` safe
        to call when peer ``SQLiteDocumentStore`` instances (other
        workers in the same campaign) still have the DB open (issue
        #1006): the previous ``wal_checkpoint(TRUNCATE)`` removed the
        aux files out from under peers and crashed them with
        ``FileNotFoundError: <db>.sqlite-shm``. The fix mirrors the one
        already applied to ``SQLiteCache.close()`` (issue #620).
        """
        if self._conn is None:
            return
        try:
            self._conn.commit()
            self._conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
            self._conn.execute("PRAGMA optimize")
        except sqlite3.Error:
            log.debug("document store checkpoint on close failed", exc_info=True)
        with self._lock:
            with contextlib.suppress(Exception):
                self._conn.close()
            self._conn = None
        log.debug("document store closed at %s", self.db_path)

    def __enter__(self) -> SQLiteDocumentStore:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def __del__(self) -> None:
        self.close()

    def _collection_table(self, collection: str) -> str:
        """Return the SQLite table name for a collection."""
        # Escape special characters and validate collection name
        safe_name = "".join(c if c.isalnum() else "_" for c in collection)
        return f"doc_{safe_name}"

    def _ensure_collection(self, collection: str) -> None:
        """Ensure the collection table exists."""
        table = self._collection_table(collection)
        with self._lock:
            c = self.connection
            c.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {table} (
                    _id INTEGER PRIMARY KEY AUTOINCREMENT,
                    doc TEXT NOT NULL
                )
                """
            )

    def insert_one(self, collection: str, document: dict[str, Any]) -> str:
        """Insert a single document into a collection."""
        self._ensure_collection(collection)
        table = self._collection_table(collection)

        # Generate _id if not present
        doc = dict(document)
        if "_id" not in doc:
            with self._lock:
                row = self.connection.execute(f"SELECT MAX(_id) as max_id FROM {table}").fetchone()
                next_id = (row["max_id"] if row["max_id"] is not None else 0) + 1
            doc["_id"] = f"doc_{next_id}"

        # Issue #1564: shared JSON encode helper — sort_keys=True +
        # default=str is the canonical OSimFlow encoding for documents.
        doc_json = _encode_value(doc)

        with self._lock:
            c = self.connection
            try:
                cursor = c.execute(
                    f"INSERT INTO {table} (doc) VALUES (?)",
                    (doc_json,),
                )
                doc_id = doc.get("_id", str(cursor.lastrowid))
                log.debug(
                    "insert_one: collection=%s doc_id=%s",
                    collection,
                    doc_id,
                )
                return str(doc_id)
            except sqlite3.IntegrityError as exc:
                if "UNIQUE" in str(exc):
                    raise DuplicateDocumentError(
                        f"duplicate document in collection {collection}"
                    ) from exc
                raise DocumentStoreError(f"insert_one failed for collection {collection}") from exc

    def find_one(
        self,
        collection: str,
        filter_spec: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Find a single document matching the filter."""
        self._ensure_collection(collection)
        table = self._collection_table(collection)
        if filter_spec is None:
            filter_spec = {}

        where_clause, params = _build_where_clause(filter_spec)
        query = f"SELECT doc FROM {table} WHERE {where_clause} LIMIT 1"

        with self._lock:
            c = self.connection
            row = c.execute(query, params).fetchone()

        if row is None:
            return None

        try:
            doc = cast(dict[str, Any], json.loads(row["doc"]))
        except (json.JSONDecodeError, TypeError) as exc:
            log.warning(
                "find_one: corrupted doc in collection=%s filter=%s: %s",
                collection,
                filter_spec,
                exc,
            )
            return None
        log.debug("find_one: collection=%s filter=%s -> found", collection, filter_spec)
        return doc

    def find_many(
        self,
        collection: str,
        filter_spec: dict[str, Any] | None = None,
        *,
        limit: int = 0,
        skip: int = 0,
        sort: list[tuple[str, int]] | None = None,
    ) -> list[dict[str, Any]]:
        """Find documents matching the filter."""
        self._ensure_collection(collection)
        table = self._collection_table(collection)
        if filter_spec is None:
            filter_spec = {}

        where_clause, params = _build_where_clause(filter_spec)

        # Build ORDER BY clause
        order_by = ""
        if sort:
            order_parts = []
            for field, direction in sort:
                if not isinstance(field, str) or not _FIELD_NAME_RE.match(field):
                    raise ValueError(
                        f"Invalid sort field {field!r}: must be alphanumeric with optional dots/underscores"
                    )
                dir_str = "ASC" if direction > 0 else "DESC"
                order_parts.append(f"json_extract(doc, '$.{field}') {dir_str}")
            order_by = f" ORDER BY {', '.join(order_parts)}"

        # Build LIMIT/OFFSET clause
        # SQLite requires LIMIT when using OFFSET, so if skip > 0 but limit is 0,
        # we use a very large number as the limit (essentially "no limit")
        large_limit = 10**9
        limit_clause = (
            f" LIMIT {large_limit}"
            if limit == 0 and skip > 0
            else (f" LIMIT {limit}" if limit > 0 else "")
        )
        offset_clause = f" OFFSET {skip}" if skip > 0 else ""

        query = (
            f"SELECT doc FROM {table} WHERE {where_clause}{order_by}{limit_clause}{offset_clause}"
        )

        with self._lock:
            c = self.connection
            rows = c.execute(query, params).fetchall()

        docs = []
        for row in rows:
            try:
                docs.append(json.loads(row["doc"]))
            except (json.JSONDecodeError, TypeError) as exc:
                log.warning(
                    "find_many: skipped corrupted doc in collection=%s: %s", collection, exc
                )
        log.debug(
            "find_many: collection=%s filter=%s limit=%d skip=%d -> %d docs",
            collection,
            filter_spec,
            limit,
            skip,
            len(docs),
        )
        return docs

    # -----------------------------------------------------------------------
    # Update operators are implemented at module scope
    # (``_op_set`` / ``_op_unset`` / ``_op_inc`` / ``_op_push`` / ``_op_pull``
    # and ``_apply_update_operators``) so both ``SQLiteDocumentStore`` and
    # ``RedisDocumentStore`` can share the same MongoDB-style update
    # semantics without duplication. The previous class-method dispatch
    # table is preserved as a thin class-level wrapper for backward
    # compatibility (issue #1014).
    # -----------------------------------------------------------------------

    def _apply_update_operators(
        self,
        doc: dict[str, Any],
        update: dict[str, Any],
    ) -> None:
        """Apply MongoDB-style update operators to a document.

        Backed by the module-level ``_apply_update_operators`` so it shares
        its dispatch table with ``RedisDocumentStore``. Issue #1014.
        """
        apply_update_operators(doc, update)

    def update_one(
        self,
        collection: str,
        filter_spec: dict[str, Any],
        update: dict[str, Any],
    ) -> bool:
        """Update a single document matching the filter."""
        self._ensure_collection(collection)
        table = self._collection_table(collection)
        where_clause, params = _build_where_clause(filter_spec)

        # Find the document first
        doc = self.find_one(collection, filter_spec)
        if doc is None:
            return False

        # Apply update operators
        apply_update_operators(doc, update)

        # Write the updated document back.  Issue #1564: shared JSON
        # encode helper — sort_keys=True + default=str is canonical.
        doc_json = _encode_value(doc)
        with self._lock:
            c = self.connection
            c.execute(
                f"UPDATE {table} SET doc = ? WHERE {where_clause}",
                [doc_json] + params,
            )

        log.debug("update_one: collection=%s filter=%s", collection, filter_spec)
        return True

    def delete_one(
        self,
        collection: str,
        filter_spec: dict[str, Any],
    ) -> bool:
        """Delete a single document matching the filter."""
        self._ensure_collection(collection)
        table = self._collection_table(collection)
        where_clause, params = _build_where_clause(filter_spec)

        with self._lock:
            c = self.connection
            cursor = c.execute(
                f"DELETE FROM {table} WHERE rowid = (SELECT rowid FROM {table} WHERE {where_clause} LIMIT 1)",
                params,
            )

        deleted = cursor.rowcount > 0
        log.debug(
            "delete_one: collection=%s filter=%s -> deleted=%s",
            collection,
            filter_spec,
            deleted,
        )
        return deleted

    def create_index(
        self,
        collection: str,
        field: str,
        *,
        unique: bool = False,
    ) -> None:
        """Create an index on a JSON field in a collection."""
        if not isinstance(field, str) or not _FIELD_NAME_RE.match(field):
            raise ValueError(
                f"Invalid field {field!r}: must be alphanumeric with optional dots/underscores"
            )
        self._ensure_collection(collection)
        table = self._collection_table(collection)
        index_name = f"idx_{table}_{field.replace('.', '_')}"

        unique_str = "UNIQUE" if unique else ""

        # Create expression-based index on JSON field
        expr = f"json_extract(doc, '$.{field}')"
        with self._lock:
            c = self.connection
            c.execute(f"CREATE {unique_str} INDEX IF NOT EXISTS {index_name} ON {table} ({expr})")

        log.debug(
            "create_index: collection=%s field=%s unique=%s",
            collection,
            field,
            unique,
        )

    def list_collections(self) -> list[str]:
        """List all collection names."""
        with self._lock:
            c = self.connection
            rows = c.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'doc_%' ORDER BY name"
            ).fetchall()

        collections = []
        for row in rows:
            table_name = row["name"]
            if table_name.startswith("doc_"):
                collections.append(table_name[4:])  # Strip 'doc_' prefix
            else:
                collections.append(table_name)

        return sorted(collections)

    def count_documents(
        self,
        collection: str,
        filter_spec: dict[str, Any] | None = None,
    ) -> int:
        """Count documents matching the filter."""
        self._ensure_collection(collection)
        table = self._collection_table(collection)
        if filter_spec is None:
            filter_spec = {}

        where_clause, params = _build_where_clause(filter_spec)
        query = f"SELECT COUNT(*) as cnt FROM {table} WHERE {where_clause}"

        with self._lock:
            c = self.connection
            row = c.execute(query, params).fetchone()

        return row["cnt"] if row else 0


# ---------------------------------------------------------------------------
# Redis implementation (issue #1014)
