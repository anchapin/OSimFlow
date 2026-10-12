"""Shared document-store primitives: errors, update/match helpers and the
``DocumentStore`` ABC (issue #1890 split of ``osimflow.document_store``)."""

from __future__ import annotations

__all__ = [
    "DocumentNotFoundError",
    "DocumentStore",
    "DocumentStoreError",
    "DuplicateDocumentError",
]

import logging
import re
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any

from .errors import OSimFlowError

log = logging.getLogger("osimflow.document_store")

# Whitelist pattern for JSON field names used in ORDER BY and index expressions.
# Only alphanumeric characters, dots (for nested keys), and underscores are allowed.
# This prevents SQL injection via malicious field names (issue #1269).
_FIELD_NAME_RE = re.compile(r"^[A-Za-z0-9._]+$")
# Stricter pattern for filter keys interpolated into WHERE-clause fragments.
# Must start with a letter or underscore (not a digit — matches typical
# JSON key naming), and only contains alphanumerics, dots, and underscores.
# Used to validate keys against SQL injection attempts containing `'`,
# `;`, `--`, ` OR 1=1 `, etc. (issue #1779).
_FILTER_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")


def _validate_filter_key(key: str) -> None:
    """Reject filter keys that look like SQL injection attempts (issue #1779).

    ``_build_where_clause`` interpolates every filter key into a
    ``json_extract(doc, '$.{key}')`` fragment via f-string — the values
    are parameterized, but the path is not. Library users (or REST
    callers routing through ``osimflow/api/*``) passing
    ``{"foo'; DROP TABLE documents; --": 1}`` get a fully exploitable
    SQL injection; even the more constrained ``{"$regex": {"x' OR 1=1
    --": "y"}}`` breaks out of the LIKE-quoted literal.

    This validator enforces the same shape ``_collection_table`` uses
    for collection names plus the additional leading-character rule
    typical of JSON keys.

    Raises
    ------
    DocumentStoreError
        When *key* contains anything outside ``[A-Za-z0-9_.]`` or does
        not start with a letter / underscore.
    """
    if not isinstance(key, str) or not _FILTER_KEY_RE.match(key):
        raise DocumentStoreError(f"invalid filter key {key!r}: must match {_FILTER_KEY_RE.pattern}")


class DocumentStoreError(OSimFlowError):
    """Base exception for document store errors."""


class DocumentNotFoundError(DocumentStoreError):
    """Raised when a document is not found in the store."""


class DuplicateDocumentError(DocumentStoreError):
    """Raised when attempting to insert a duplicate document with unique index."""


# ---------------------------------------------------------------------------
# Query filter helpers
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Update operator helpers (shared by SQLite and Redis backends)
# ---------------------------------------------------------------------------


def _op_set(doc: dict[str, Any], op_value: dict[str, Any]) -> None:
    """Handle $set operator."""
    doc.update(op_value)


def _op_unset(doc: dict[str, Any], op_value: dict[str, list[str]]) -> None:
    """Handle $unset operator."""
    for key in op_value:
        doc.pop(key, None)


def _op_inc(doc: dict[str, Any], op_value: dict[str, float]) -> None:
    """Handle $inc operator."""
    for key, inc_value in op_value.items():
        current = doc.get(key, 0)
        if isinstance(current, (int, float)) and isinstance(inc_value, (int, float)):
            doc[key] = current + inc_value


def _op_push(doc: dict[str, Any], op_value: dict[str, Any]) -> None:
    """Handle $push operator."""
    for key, value in op_value.items():
        if key not in doc:
            doc[key] = []
        if isinstance(doc[key], list):
            doc[key].append(value)


def _op_pull(doc: dict[str, Any], op_value: dict[str, Any]) -> None:
    """Handle $pull operator."""
    for key, value in op_value.items():
        if key in doc and isinstance(doc[key], list):
            doc[key] = [item for item in doc[key] if item != value]


# Dispatch table for update operators (shared by both backends)
_UPDATE_OPS: dict[str, Callable[..., None]] = {
    "$set": _op_set,
    "$unset": _op_unset,
    "$inc": _op_inc,
    "$push": _op_push,
    "$pull": _op_pull,
}


def apply_update_operators(doc: dict[str, Any], update: dict[str, Any]) -> None:
    """Apply MongoDB-style update operators to a document in place.

    Shared by ``SQLiteDocumentStore`` and ``RedisDocumentStore`` so both
    backends implement the same update semantics ($set, $unset, $inc,
    $push, $pull). Unknown operators are logged and skipped (matches the
    pre-#1014 behaviour of the SQLite backend).

    Parameters
    ----------
    doc
        The document to mutate (modified in place).
    update
        MongoDB-style update specification.
    """
    for op, op_value in update.items():
        handler = _UPDATE_OPS.get(op)
        if handler is not None:
            handler(doc, op_value)
        else:
            log.warning("unknown update operator: %s", op)


# ---------------------------------------------------------------------------
# Document-matching helpers (shared by SQLite and Redis backends)
# ---------------------------------------------------------------------------


def _document_matches(doc: dict[str, Any], filter_spec: dict[str, Any]) -> bool:
    """Evaluate a MongoDB-style filter against an in-memory document.

    Used by the Redis backend to filter documents client-side after the
    full collection is fetched. The SQLite backend uses the equivalent
    SQL ``_build_where_clause`` for server-side filtering. The two
    backends must agree on semantics for all supported operators
    (``$eq``, ``$ne``, ``$gt``, ``$gte``, ``$lt``, ``$lte``, ``$in``,
    ``$nin``, ``$exists``, ``$regex``); this mirrors
    ``_build_where_clause`` so the same filter spec works on both
    backends (issue #1014).

    Parameters
    ----------
    doc
        The document to evaluate (in-memory dict).
    filter_spec
        MongoDB-style filter dictionary. ``None`` / empty matches any
        document.

    Returns
    -------
    bool
        ``True`` if *doc* matches *filter_spec*.
    """
    if not filter_spec:
        return True
    for key, value in filter_spec.items():
        if key.startswith("$"):
            continue  # Skip logical operators at field level
        current = doc.get(key)
        if isinstance(value, dict):
            for op, op_value in value.items():
                if op == "$eq":
                    if current != op_value:
                        return False
                elif op == "$ne":
                    if current == op_value:
                        return False
                elif op == "$gt":
                    if not (current is not None and current > op_value):
                        return False
                elif op == "$gte":
                    if not (current is not None and current >= op_value):
                        return False
                elif op == "$lt":
                    if not (current is not None and current < op_value):
                        return False
                elif op == "$lte":
                    if not (current is not None and current <= op_value):
                        return False
                elif op == "$in":
                    if current not in op_value:
                        return False
                elif op == "$nin":
                    if current in op_value:
                        return False
                elif op == "$exists":
                    exists = current is not None
                    if bool(op_value) != exists:
                        return False
                elif op == "$regex":
                    # Literal SQL LIKE pattern; current is converted to
                    # str to match SQLite's ``json_extract`` text CAST.
                    if current is None or op_value not in str(current):
                        return False
                else:
                    log.warning("unknown comparison operator: %s", op)
        elif current != value:
            return False
    return True


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
