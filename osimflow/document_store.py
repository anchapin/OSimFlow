"""Document store backends for OSimFlow campaigns (issue #389).

Provides a MongoDB-equivalent interface for storing flexible JSON documents
using SQLite with JSON1 extension as the underlying engine. This enables
document-based storage without requiring MongoDB itself, which is important
for HPC environments where MongoDB may not be available.

Architecture
------------
``DocumentStore`` is the abstract base class defining the MongoDB-like
interface. ``SQLiteDocumentStore`` is the concrete implementation using
SQLite JSON1 functions for document storage and querying.

Document collections are stored as rows in SQLite tables, with the full
JSON document stored in a single TEXT column. Indexes are created as
SQLite indexes on JSON fields using JSON1 extraction functions.

Example
-------
>>> from osimflow.document_store import SQLiteDocumentStore
>>> store = SQLiteDocumentStore(db_path=Path("campaign_data.db"))
>>> store.insert_one("kpis", {"sample_id": "s0001", "eui": 150.5, "cost": 1200})
>>> doc = store.find_one("kpis", {"sample_id": "s0001"})
>>> docs = store.find_many("kpis", {"eui": {"$gt": 100}})
>>> store.update_one("kpis", {"sample_id": "s0001"}, {"$set": {"eui": 160.0}})
>>> store.create_index("kpis", "sample_id", unique=True)
"""

from __future__ import annotations

import contextlib
import json
import logging
import sqlite3
import threading
from abc import ABC, abstractmethod
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypedDict, cast

if TYPE_CHECKING:
    pass


log = logging.getLogger("osimflow.document_store")


class DocumentStoreError(Exception):
    """Base exception for document store errors."""


class DocumentNotFoundError(DocumentStoreError):
    """Raised when a document is not found in the store."""


class DuplicateDocumentError(DocumentStoreError):
    """Raised when attempting to insert a duplicate document with unique index."""


# ---------------------------------------------------------------------------
# Query filter helpers
# ---------------------------------------------------------------------------


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
# Abstract base class
# ---------------------------------------------------------------------------


class DocumentStore(ABC):
    """Abstract base class for document store backends.

    Provides a MongoDB-equivalent interface for storing and querying
    flexible JSON documents. Concrete backends must implement the
    core CRUD methods.
    """

    name: str

    @abstractmethod
    def insert_one(self, collection: str, document: dict[str, Any]) -> str:
        """Insert a single document into a collection.

        Parameters
        ----------
        collection
            Collection name.
        document
            JSON-serializable document to insert.

        Returns
        -------
        str
            The document ID (from the document or generated).

        Raises
        ------
        DuplicateDocumentError
            When a unique index violation occurs.
        DocumentStoreError
            When insertion fails for other reasons.
        """

    @abstractmethod
    def find_one(
        self,
        collection: str,
        filter_spec: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Find a single document matching the filter.

        Parameters
        ----------
        collection
            Collection name.
        filter_spec
            MongoDB-style filter dictionary. None returns any document.

        Returns
        -------
        dict[str, Any] | None
            The matching document, or None if not found.
        """

    @abstractmethod
    def find_many(
        self,
        collection: str,
        filter_spec: dict[str, Any] | None = None,
        *,
        limit: int = 0,
        skip: int = 0,
        sort: list[tuple[str, int]] | None = None,
    ) -> list[dict[str, Any]]:
        """Find documents matching the filter.

        Parameters
        ----------
        collection
            Collection name.
        filter_spec
            MongoDB-style filter dictionary. None returns all documents.
        limit
            Maximum number of documents to return (0 = unlimited).
        skip
            Number of documents to skip (for pagination).
        sort
            Sort specification as list of (field, direction) tuples.
            Direction: 1 for ascending, -1 for descending.

        Returns
        -------
        list[dict[str, Any]]
            List of matching documents.
        """

    @abstractmethod
    def update_one(
        self,
        collection: str,
        filter_spec: dict[str, Any],
        update: dict[str, Any],
    ) -> bool:
        """Update a single document matching the filter.

        Parameters
        ----------
        collection
            Collection name.
        filter_spec
            MongoDB-style filter dictionary.
        update
            MongoDB-style update specification. Supports $set, $unset,
            $inc, $push, $pull.

        Returns
        -------
        bool
            True if a document was updated, False if no match found.

        Raises
        ------
        DocumentStoreError
            When the update fails.
        """

    @abstractmethod
    def delete_one(
        self,
        collection: str,
        filter_spec: dict[str, Any],
    ) -> bool:
        """Delete a single document matching the filter.

        Parameters
        ----------
        collection
            Collection name.
        filter_spec
            MongoDB-style filter dictionary.

        Returns
        -------
        bool
            True if a document was deleted, False if no match found.
        """

    @abstractmethod
    def create_index(
        self,
        collection: str,
        field: str,
        *,
        unique: bool = False,
    ) -> None:
        """Create an index on a JSON field in a collection.

        Parameters
        ----------
        collection
            Collection name.
        field
            JSON field path to index (e.g., "sample_id" or "kpi.eui").
        unique
            If True, enforce uniqueness constraint.
        """

    @abstractmethod
    def list_collections(self) -> list[str]:
        """List all collection names.

        Returns
        -------
        list[str]
            List of collection names.
        """

    @abstractmethod
    def count_documents(
        self,
        collection: str,
        filter_spec: dict[str, Any] | None = None,
    ) -> int:
        """Count documents matching the filter.

        Parameters
        ----------
        collection
            Collection name.
        filter_spec
            MongoDB-style filter dictionary. None counts all.

        Returns
        -------
        int
            Number of matching documents.
        """

    @abstractmethod
    def close(self) -> None:
        """Close the document store and release resources."""


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
        """Initialize the database schema."""
        conn = self._connect()
        # Enable JSON1 extension (built into SQLite 3.38+)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.close()

    def _connect(self) -> sqlite3.Connection:
        """Create a new database connection."""
        conn = sqlite3.connect(self.db_path, timeout=10.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

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
        """Close the database connection."""
        if self._conn is None:
            return
        try:
            self._conn.commit()
            self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self._conn.execute("PRAGMA optimize")
        except Exception:
            pass
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

        doc_json = json.dumps(doc, sort_keys=True, default=str)

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

        doc = cast(dict[str, Any], json.loads(row["doc"]))
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

        docs = [json.loads(row["doc"]) for row in rows]
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
    # Update operator helpers
    # -----------------------------------------------------------------------

    def _op_set(self, doc: dict[str, Any], op_value: dict[str, Any]) -> None:
        """Handle $set operator."""
        doc.update(op_value)

    def _op_unset(self, doc: dict[str, Any], op_value: dict[str, list[str]]) -> None:
        """Handle $unset operator."""
        for key in op_value:
            doc.pop(key, None)

    def _op_inc(self, doc: dict[str, Any], op_value: dict[str, float]) -> None:
        """Handle $inc operator."""
        for key, inc_value in op_value.items():
            current = doc.get(key, 0)
            if isinstance(current, (int, float)) and isinstance(inc_value, (int, float)):
                doc[key] = current + inc_value

    def _op_push(self, doc: dict[str, Any], op_value: dict[str, Any]) -> None:
        """Handle $push operator."""
        for key, value in op_value.items():
            if key not in doc:
                doc[key] = []
            if isinstance(doc[key], list):
                doc[key].append(value)

    def _op_pull(self, doc: dict[str, Any], op_value: dict[str, Any]) -> None:
        """Handle $pull operator."""
        for key, value in op_value.items():
            if key in doc and isinstance(doc[key], list):
                doc[key] = [item for item in doc[key] if item != value]

    # Dispatch table for update operators
    _UPDATE_OPS: dict[str, Callable[..., None]] = {}

    def _apply_update_operators(
        self,
        doc: dict[str, Any],
        update: dict[str, Any],
    ) -> None:
        """Apply MongoDB-style update operators to a document."""
        if not self._UPDATE_OPS:
            # Initialize dispatch table on first use
            SQLiteDocumentStore._UPDATE_OPS = {
                "$set": SQLiteDocumentStore._op_set,
                "$unset": SQLiteDocumentStore._op_unset,
                "$inc": SQLiteDocumentStore._op_inc,
                "$push": SQLiteDocumentStore._op_push,
                "$pull": SQLiteDocumentStore._op_pull,
            }

        for op, op_value in update.items():
            handler = self._UPDATE_OPS.get(op)
            if handler:
                handler(self, doc, op_value)
            else:
                log.warning("unknown update operator: %s", op)

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
        self._apply_update_operators(doc, update)

        # Write the updated document back
        doc_json = json.dumps(doc, sort_keys=True, default=str)
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
# Factory
# ---------------------------------------------------------------------------


class DocumentStoreConfig(TypedDict, total=False):
    """Configuration for building a document store."""

    backend: str
    db_path: Path


def build_document_store(
    backend: str,
    db_path: Path,
) -> DocumentStore:
    """Factory: build the correct DocumentStore from a backend name.

    Parameters
    ----------
    backend
        One of ``"sqlite"``.
    db_path
        Path to the database file.

    Returns
    -------
    DocumentStore
        The concrete document store instance.

    Raises
    ------
    ValueError
        When *backend* is not one of the supported values.
    """
    if backend == "sqlite":
        return SQLiteDocumentStore(db_path=db_path)
    raise ValueError(f"unknown document_store_backend: {backend!r}")
