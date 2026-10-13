"""Document store backends for OSimFlow campaigns (issue #389, #1014).

Provides a MongoDB-equivalent interface for storing flexible JSON documents
using SQLite with JSON1 extension as the underlying engine. This enables
document-based storage without requiring MongoDB itself, which is important
for HPC environments where MongoDB may not be available.

Architecture
------------
``DocumentStore`` is the abstract base class defining the MongoDB-like
interface. Two concrete backends are shipped:

* ``SQLiteDocumentStore`` — single-node persistence using SQLite's
  JSON1 functions for document storage and querying. The default
  when no distributed coordinator is configured.
* ``RedisDocumentStore`` — Redis-backed shared store for distributed
  campaigns (issue #1014). Coordinates document state across
  processes / nodes via Redis hashes, eliminating the T8.1-style
  SQLite lock-reproducer that distributed workers used to hit when
  sharing a single ``SQLiteDocumentStore`` file.  Mirrors the
  factory / Redis-key-naming pattern from ``osimflow/distributed_cache.py``
  (issue #993): ``build_document_store`` returns a
  ``RedisDocumentStore`` when ``redis_url`` is configured, falling
  back to ``SQLiteDocumentStore`` for single-node mode.

``SQLiteDocumentStore`` stores documents as JSON strings in a single
TEXT column, with indexes created using JSON1 extraction functions.
``RedisDocumentStore`` stores documents as JSON strings in a Redis
hash per collection and filters / counts client-side. The query
operators exposed by the ``DocumentStore`` ABC are the same for both
backends (``$eq``, ``$ne``, ``$gt``, ``$gte``, ``$lt``, ``$lte``,
``$in``, ``$nin``, ``$exists``, ``$regex`` for filters; ``$set``,
``$unset``, ``$inc``, ``$push``, ``$pull`` for updates).

Redis key naming
----------------
Per-collection document hash::

    osimflow:docs:coll:<namespace>:<collection>  ->  { doc_id: json_doc }

Per-collection ``_id`` counter (auto-increment)::

    osimflow:docs:counter:<namespace>:<collection>  ->  integer

Known collection set (for ``list_collections``)::

    osimflow:docs:collections:<namespace>  ->  set of collection names

Index metadata (used for unique-index enforcement on the Redis side)::

    osimflow:docs:idx:<namespace>:<collection>:<field>  ->  { field_value: doc_id }

The namespace is a stable identifier for the campaign's shared state
(see ``distributed_cache.campaign_state_namespace``); two processes
targeting the same campaign share one namespace, while concurrent
campaigns on different outdirs stay isolated.

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

__all__ = [
    "DocumentNotFoundError",
    "DocumentStore",
    "DocumentStoreError",
    "DuplicateDocumentError",
    "RedisDocumentStore",
    "SQLiteDocumentStore",
    "_build_where_clause",  # historical re-export (issue #1890)
    "_document_matches",  # historical re-export (issue #1890)
    "apply_update_operators",  # historical re-export (issue #1890)
    "build_document_store",
]

import contextlib
import json
import logging
import ssl
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any, TypedDict, cast

from ._document_store_base import (
    DocumentNotFoundError,
    DocumentStore,
    DocumentStoreError,
    DuplicateDocumentError,
    _document_matches,
    apply_update_operators,
)
from ._document_store_sqlite import SQLiteDocumentStore, _build_where_clause
from ._sqlite_store import encode_value as _encode_value
from .circuit_breaker import CircuitBreaker, CircuitOpenError
from .distributed_cache import validate_redis_url

log = logging.getLogger("osimflow.document_store")

# ---------------------------------------------------------------------------

# Lazy import holder — replaced in tests via patch().  Mirrors the pattern
# used in ``osimflow/distributed_cache.py`` so the same Redis client
# construction strategy is used for caches, job queues, and document
# stores (issue #1014).
_redis_sync_module: dict[str, Any] = {}


def _get_redis_sync() -> Any:
    """Import and return the sync ``redis`` module (lazy, cached)."""
    if not _redis_sync_module:
        import redis as rs  # noqa: PLC0415

        _redis_sync_module["module"] = rs
    return _redis_sync_module["module"]


class _BreakerClient:
    """Proxy recording every Redis operation outcome into a breaker.

    Wraps the sync Redis client so ``DistributedCache``-style call sites
    do not each need bespoke breaker plumbing (issue #1111). While the
    circuit is open, calls fail fast with :class:`DocumentStoreError`
    instead of waiting out another 5 s socket timeout.
    """

    def __init__(self, client: Any, breaker: CircuitBreaker) -> None:
        self._client = client
        self._breaker = breaker

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._client, name)
        if not callable(attr):
            return attr

        def wrapped(*args: Any, **kwargs: Any) -> Any:
            try:
                self._breaker.check()
            except CircuitOpenError as exc:
                raise DocumentStoreError(f"Redis unavailable ({exc})") from exc
            try:
                result = attr(*args, **kwargs)
            except Exception:
                self._breaker.record_failure()
                raise
            self._breaker.record_success()
            return result

        return wrapped


class RedisDocumentStore(DocumentStore):
    """Redis-backed document store for distributed campaigns (issue #1014).

    Drop-in replacement for ``SQLiteDocumentStore`` that keeps the
    authoritative state in Redis instead of a single shared SQLite file.
    This is the fix for the T8.1-style SQLITE_LOCKED reproducer that
    workers in a multi-node / multi-process campaign would hit when
    they all targeted the same ``<outdir>/.../documents.sqlite``
    (the same anti-pattern that issue #993 fixed for the cache).

    The implementation mirrors the architecture of ``DistributedCache``
    (issue #993): one shared Redis hash per collection, JSON-encoded
    document bodies, and a small per-process in-memory LRU that absorbs
    repeated reads without a Redis round-trip.  Filtering and counting
    happen client-side because the ``DocumentStore`` ABC exposes a
    rich MongoDB-style query language that does not map cleanly to
    Redis without an external search module (RediSearch).  Per-campaign
    KPI / metadata collections are typically bounded in size, so the
    client-side cost is negligible compared with the original SQLite
    lock reproducer.

    Redis key naming
    ----------------
    Document hash per collection::

        osimflow:docs:coll:<namespace>:<collection>  ->  { doc_id: json_doc }

    Per-collection ``_id`` counter (auto-increment)::

        osimflow:docs:counter:<namespace>:<collection>  ->  integer

    Known collection set::

        osimflow:docs:collections:<namespace>  ->  set of collection names

    Unique-index enforcement (field value -> doc_id)::

        osimflow:docs:idx:<namespace>:<collection>:<field>  ->  { value: doc_id }

    Usage
    -----
    ``build_document_store`` instantiates this class when ``redis_url``
    is configured::

        from osimflow.document_store import build_document_store
        store = build_document_store(
            db_path=Path("outdir/work/documents.sqlite"),
            redis_url="redis://localhost:6379/0",
            namespace="campaign-outdir-abcd1234",
        )

    Failure mode
    ------------
    If Redis is unreachable, every operation logs a warning and raises
    ``DocumentStoreError`` — the Redis backend is strictly a *shared*
    backend, and a Redis outage must not silently fall back to local
    state because the workers would diverge.  (Compare with
    ``DistributedCache``, which degrades to local-only because the cache
    is a soft hint — the document store is the source of truth, so we
    fail loud.)

    Caveat: this implementation is *not* a full MongoDB replacement —
    the per-process LRU is advisory, transactions are per-document, and
    the query language is limited to the operators the ABC already
    documents.  See ``docs/mongodb-storage.md`` for the broader
    roadmap.
    """

    name = "redis"

    def __init__(
        self,
        redis_url: str,
        namespace: str,
        db_path: Path | None = None,
        *,
        lru_max_entries: int = 1024,
        connection_pool_max_connections: int = 50,
        redis_ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        """Initialize the Redis document store.

        Parameters
        ----------
        redis_url
            Redis connection URL, e.g. ``redis://localhost:6379/0``.
            Supports ``rediss://`` for TLS.  May contain user:pass for
            AUTH.
        namespace
            Stable Redis namespace for the campaign's shared state.
            Pass ``campaign_state_namespace(outdir)`` (or any other
            stable identifier) so two processes targeting the same
            outdir share one Redis view while concurrent campaigns on
            different outdirs stay isolated.
        db_path
            Optional path accepted for parity with ``SQLiteDocumentStore``
            — ``RedisDocumentStore`` does not maintain a local SQLite
            file, so this is ignored except for surfacing it in logs.
        lru_max_entries
            Maximum number of ``(collection, doc_id)`` entries to keep
            in the in-process LRU.  Defaults to 1024; tune to bound
            memory for very read-heavy workloads.
        connection_pool_max_connections
            Maximum number of connections in the shared Redis connection
            pool.  Defaults to 50.  Each ``RedisDocumentStore`` instance
            maintains its own pool so concurrent campaigns stay isolated.
            Issue #1343.
        redis_ssl_context
            Optional ``ssl.SSLContext`` to use for the Redis connection.
            When provided, it is passed to
            ``redis.ConnectionPool.from_url()`` as the ``ssl`` argument,
            enabling custom CA bundle or disabled verification for
            air-gapped deployments (issue #1327).
        """
        self._redis_url = redis_url
        self._redis_ssl_context = redis_ssl_context
        self.namespace = namespace
        self.requested_db_path = db_path
        self._lru_max_entries = int(lru_max_entries)
        self._connection_pool_max_connections = int(connection_pool_max_connections)
        self._redis_client: Any = None
        self._pool: Any = None
        # Circuit breaker (issue #1111): after repeated consecutive Redis
        # failures, fail fast (DocumentStoreError) instead of burning the
        # 5 s socket timeout on every operation until Redis recovers.
        self._breaker = CircuitBreaker(name=f"docs:{namespace}")
        self._lru: OrderedDict[tuple[str, str], dict[str, Any]] = OrderedDict()
        self._lru_lock = threading.Lock()
        # Per-instance index registry.  Keyed by ``(collection, field)``
        # to the Redis unique-index key unique to this namespace —
        # keeps the per-process state namespaced so two
        # ``RedisDocumentStore`` instances in different namespaces
        # (e.g. across tests) don't surface each other's indexes
        # during insert / update.  The authoritative uniqueness
        # primitive is still the Redis side table; the local set is
        # only consulted to know which fields to check.
        self._indexes: set[tuple[str, str, str, bool]] = set()
        log.info(
            "Redis document store opened namespace=%s lru_max_entries=%d",
            namespace,
            self._lru_max_entries,
        )

    # ------------------------------------------------------------------
    # Redis client (lazy, thread-safe)
    # ------------------------------------------------------------------
    def _get_client(self) -> Any:
        """Lazily create the sync Redis client used for all data ops.

        Uses an explicit ``ConnectionPool`` shared across all operations
        so connections are reused instead of opened/closed per call
        (issue #1343).  Socket timeouts bound every call so a hung Redis
        does not stall the campaign (mirrors the same timeouts in
        ``DistributedCache._get_sync_client``).  The returned client is
        wrapped in a recording proxy that feeds every operation's outcome
        into the circuit breaker; while the breaker is open, operations
        fail fast with :class:`DocumentStoreError` instead of waiting for
        another socket timeout (issue #1111).
        """
        # Fail fast while the circuit is open (issue #1111): do not even
        # attempt client construction or a socket round-trip.
        try:
            self._breaker.check()
        except CircuitOpenError as exc:
            raise DocumentStoreError(f"Redis unavailable ({exc})") from exc
        if self._redis_client is None:
            redis_sync = _get_redis_sync()
            try:
                if self._pool is None:
                    self._pool = redis_sync.ConnectionPool.from_url(
                        self._redis_url,
                        decode_responses=True,
                        socket_timeout=5.0,
                        socket_connect_timeout=5.0,
                        max_connections=self._connection_pool_max_connections,
                        ssl=self._redis_ssl_context,
                    )
                raw_client = redis_sync.Redis(connection_pool=self._pool)
            except Exception:
                # Construction itself failed (bad URL, immediate connection
                # error): count it toward the circuit (issue #1111).
                self._breaker.record_failure()
                raise
            self._redis_client = _BreakerClient(raw_client, self._breaker)
        return self._redis_client

    # ------------------------------------------------------------------
    # Key helpers
    # ------------------------------------------------------------------
    def _coll_key(self, collection: str) -> str:
        """Return the Redis hash key holding documents for *collection*."""
        return f"osimflow:docs:coll:{self.namespace}:{collection}"

    def _counter_key(self, collection: str) -> str:
        """Return the Redis counter key for auto-incrementing ``_id`` values."""
        return f"osimflow:docs:counter:{self.namespace}:{collection}"

    def _collections_key(self) -> str:
        """Return the Redis set key holding the known collection names."""
        return f"osimflow:docs:collections:{self.namespace}"

    def _index_key(self, collection: str, field: str) -> str:
        """Return the Redis hash key for unique-index enforcement."""
        return f"osimflow:docs:idx:{self.namespace}:{collection}:{field}"

    # ------------------------------------------------------------------
    # In-process LRU
    # ------------------------------------------------------------------
    def _lru_get(self, collection: str, doc_id: str) -> dict[str, Any] | None:
        """Return a cached document, or ``None`` on miss."""
        with self._lru_lock:
            return self._lru.get((collection, doc_id))

    def _lru_put(self, collection: str, doc_id: str, doc: dict[str, Any]) -> None:
        """Insert a document into the LRU, evicting the oldest entry if full."""
        key = (collection, doc_id)
        with self._lru_lock:
            if key in self._lru:
                # Move to the end (most-recently used).
                self._lru.move_to_end(key)
                self._lru[key] = doc
                return
            self._lru[key] = doc
            if len(self._lru) > self._lru_max_entries:
                self._lru.popitem(last=False)

    def _lru_invalidate(self, collection: str | None = None) -> None:
        """Drop cached entries for *collection* (or all, if ``None``).

        Called after every write / delete so the next read fetches
        fresh state from Redis.  A full eviction is the safe default
        because the operation is cheap and the LRU is a soft hint.
        """
        with self._lru_lock:
            if collection is None:
                self._lru.clear()
                return
            to_drop = [k for k in self._lru if k[0] == collection]
            for k in to_drop:
                self._lru.pop(k, None)

    # ------------------------------------------------------------------
    # Read / write helpers
    # ------------------------------------------------------------------
    def _load_collection(self, collection: str) -> dict[str, dict[str, Any]]:
        """Load every document in *collection* from Redis (LRU-unaware).

        Returns a dict mapping ``_id`` -> document.  Used by the
        client-side filter path.  The LRU absorbs repeated single-doc
        reads so subsequent ``find_one`` calls on the same doc do
        not hit Redis.
        """
        client = self._get_client()
        try:
            raw = client.hgetall(self._coll_key(collection))
        except Exception as exc:
            raise DocumentStoreError(
                f"_load_collection failed for collection {collection!r}: {exc}"
            ) from exc
        out: dict[str, dict[str, Any]] = {}
        for doc_id, doc_json in raw.items():
            try:
                doc = cast(dict[str, Any], json.loads(doc_json))
            except (json.JSONDecodeError, TypeError) as exc:
                log.warning(
                    "RedisDocumentStore: skipped corrupted doc collection=%s id=%s: %s",
                    collection,
                    doc_id,
                    exc,
                )
                continue
            out[doc_id] = doc
        return out

    def _next_id(self, collection: str) -> str:
        """Return the next auto-generated ``_id`` for *collection*."""
        client = self._get_client()
        # ``INCR`` is atomic — concurrent processes get distinct ids
        # without a shared lock.  This is the multi-process equivalent
        # of the SQLite ``MAX(_id) + 1`` path.
        n = client.incr(self._counter_key(collection))
        return f"doc_{n}"

    def _ensure_collection_tracked(self, collection: str) -> None:
        """Add *collection* to the known-collections set (idempotent)."""
        client = self._get_client()
        client.sadd(self._collections_key(), collection)

    def _check_unique_index(
        self,
        collection: str,
        field: str,
        value: Any,
        exclude_doc_id: str | None = None,
    ) -> None:
        """Raise ``DuplicateDocumentError`` if a unique index is violated.

        Reads the per-field Redis hash that maps ``field_value`` to
        ``doc_id`` and checks for collisions.  ``exclude_doc_id`` allows
        updates against the same document to pass (matched by doc_id).
        """
        idx_key = self._index_key(collection, field)
        try:
            existing = self._get_client().hget(idx_key, str(value))
        except Exception as exc:  # noqa: BLE001 - redis-py raises varied errors; logged and the unique-index pre-check is skipped (HSETNX claim still enforces uniqueness)
            log.warning(
                "RedisDocumentStore: unique-index check failed collection=%s field=%s: %s",
                collection,
                field,
                exc,
            )
            return
        if existing is not None and existing != exclude_doc_id:
            raise DuplicateDocumentError(
                f"duplicate value for unique-indexed field {field!r} in collection {collection!r}"
            )

    def _claim_unique_index_slot(
        self,
        collection: str,
        field: str,
        value: Any,
        doc_id: str,
    ) -> bool:
        """Atomically claim the ``field_value`` slot for *doc_id*.

        Uses Redis ``HSETNX`` (set-if-not-exists) so two concurrent
        workers cannot both succeed in claiming the same unique index
        slot.  Returns ``True`` if the slot was claimed, ``False`` if
        another doc already owns it.  This is the multi-process
        equivalent of the SQLite ``UNIQUE`` index violation; without
        the atomicity, two workers each running their own
        ``create_index`` would each see an empty index and both
        succeed (issue #1014).
        """
        idx_key = self._index_key(collection, field)
        try:
            claimed = self._get_client().hsetnx(idx_key, str(value), doc_id)
        except Exception as exc:  # noqa: BLE001 - redis-py raises varied errors on HSETNX; logged and falls back to a non-atomic check
            log.warning(
                "RedisDocumentStore: atomic index claim failed collection=%s field=%s: %s",
                collection,
                field,
                exc,
            )
            # Fall back to a non-atomic check; the worst case is a
            # duplicate, which raises DuplicateDocumentError via the
            # follow-up _check_unique_index call.
            return True
        return bool(claimed)

    def _release_unique_index_slot(
        self,
        collection: str,
        field: str,
        value: Any,
        doc_id: str,
    ) -> None:
        """Release the unique-index slot for *doc_id* if it still owns it.

        Used by the insert / update rollback paths so a failed write
        does not leave an orphaned index entry that would block the
        next insert (issue #1014).
        """
        idx_key = self._index_key(collection, field)
        try:
            client = self._get_client()
            existing = client.hget(idx_key, str(value))
            if existing == doc_id:
                client.hdel(idx_key, str(value))
        except Exception as exc:  # noqa: BLE001 - releasing an index slot is cleanup; redis error is logged and the stale slot is tolerated
            log.warning(
                "RedisDocumentStore: index slot release failed collection=%s field=%s: %s",
                collection,
                field,
                exc,
            )

    # ------------------------------------------------------------------
    # Index registry (per-instance; per-namespace)
    # ------------------------------------------------------------------
    def _iter_indexes(self) -> list[tuple[tuple[str, str, str], bool]]:
        """Yield ``((collection, field, key), unique)`` for every recorded index.

        The returned key is the per-namespace Redis index key, so this
        iterator only surfaces indexes that belong to *this* instance's
        namespace (issue #1014).  The authoritative uniqueness primitive
        remains the Redis side table — the local set is only consulted
        to know which fields to enforce on insert / update.
        """
        return [(idx[:3], idx[3]) for idx in self._indexes]

    def insert_one(self, collection: str, document: dict[str, Any]) -> str:
        """Insert a single document into *collection*."""
        client = self._get_client()
        self._ensure_collection_tracked(collection)

        doc = dict(document)
        if "_id" not in doc:
            doc["_id"] = self._next_id(collection)

        # Atomically claim any unique-index slots before writing the
        # document.  ``HSETNX`` returns 0 if the slot is already taken,
        # in which case we raise ``DuplicateDocumentError`` without
        # touching the document hash.  This is the multi-process
        # equivalent of the SQLite UNIQUE index violation (issue #1014).
        for (coll, field, _), unique in list(self._iter_indexes()):
            if coll != collection or not unique:
                continue
            value = doc.get(field)
            if value is None:
                continue
            if not self._claim_unique_index_slot(collection, field, value, str(doc["_id"])):
                raise DuplicateDocumentError(
                    f"duplicate value for unique-indexed field {field!r} "
                    f"in collection {collection!r}"
                )

        # Issue #1564: shared JSON encode helper.
        doc_json = _encode_value(doc)
        try:
            client.hset(self._coll_key(collection), str(doc["_id"]), doc_json)
        except Exception as exc:
            # Roll back any unique-index slots we just claimed so the
            # index stays consistent with the document hash.
            for (coll, field, _), unique in list(self._iter_indexes()):
                if coll != collection or not unique:
                    continue
                value = doc.get(field)
                if value is None:
                    continue
                self._release_unique_index_slot(collection, field, value, str(doc["_id"]))
            raise DocumentStoreError(
                f"insert_one failed for collection {collection!r}: {exc}"
            ) from exc

        self._lru_invalidate(collection)
        log.debug(
            "insert_one: collection=%s doc_id=%s",
            collection,
            doc["_id"],
        )
        return str(doc["_id"])

    def find_one(
        self,
        collection: str,
        filter_spec: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Find a single document matching the filter."""
        if (
            filter_spec is not None
            and set(filter_spec.keys()) == {"_id"}
            and isinstance(filter_spec["_id"], str)
        ):
            # Fast path: look up by _id through the LRU first.
            target_id = filter_spec["_id"]
            cached = self._lru_get(collection, target_id)
            if cached is not None:
                return cached
            try:
                raw = self._get_client().hget(self._coll_key(collection), target_id)
            except Exception as exc:
                raise DocumentStoreError(
                    f"find_one failed for collection {collection!r}: {exc}"
                ) from exc
            if raw is None:
                return None
            try:
                doc = cast(dict[str, Any], json.loads(raw))
            except (json.JSONDecodeError, TypeError) as exc:
                log.warning(
                    "RedisDocumentStore: corrupted doc collection=%s id=%s: %s",
                    collection,
                    target_id,
                    exc,
                )
                return None
            self._lru_put(collection, target_id, doc)
            return doc

        # General path: full collection scan with client-side filter.
        for doc_id, doc in self._load_collection(collection).items():
            if _document_matches(doc, filter_spec or {}):
                self._lru_put(collection, doc_id, doc)
                return doc
        return None

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
        docs = list(self._load_collection(collection).values())
        spec: dict[str, Any] = filter_spec or {}
        matched = [d for d in docs if _document_matches(d, spec)]

        if sort:
            for field, direction in reversed(sort):

                def _sort_key(d: dict[str, Any], f: str = field) -> str:
                    return str(d.get(f) or "")

                matched.sort(
                    key=_sort_key,
                    reverse=(direction < 0),
                )

        if skip:
            matched = matched[skip:]
        if limit:
            matched = matched[:limit]

        # Warm the LRU for the matched docs so the next hit is local.
        for d in matched:
            doc_id = str(d.get("_id", ""))
            if doc_id:
                self._lru_put(collection, doc_id, d)

        log.debug(
            "find_many: collection=%s filter=%s limit=%d skip=%d -> %d docs",
            collection,
            filter_spec,
            limit,
            skip,
            len(matched),
        )
        return matched

    def update_one(
        self,
        collection: str,
        filter_spec: dict[str, Any],
        update: dict[str, Any],
    ) -> bool:
        """Update a single document matching the filter."""
        client = self._get_client()
        coll_key = self._coll_key(collection)

        # Locate the matching document by _id fast path, otherwise scan.
        target_id: str | None = None
        if (
            isinstance(filter_spec, dict)
            and set(filter_spec.keys()) == {"_id"}
            and isinstance(filter_spec["_id"], str)
        ):
            target_id = filter_spec["_id"]
            raw = client.hget(coll_key, target_id)
            if raw is None:
                return False
            doc = json.loads(raw)
        else:
            for doc_id, doc in self._load_collection(collection).items():
                if _document_matches(doc, filter_spec):
                    target_id = doc_id
                    break
            if target_id is None:
                return False

        # Capture pre-update values for every unique-indexed field so
        # we can correctly transition the index slots: an update that
        # changes ``sample_id`` from "A" to "B" must release "A" and
        # claim "B" atomically (issue #1014).
        old_values: dict[str, Any] = {}
        for (coll, field, _), unique in self._iter_indexes():
            if coll == collection and unique:
                old_values[field] = doc.get(field)

        # Apply update operators in place.
        apply_update_operators(doc, update)

        # Re-claim unique-index slots for any field whose value
        # changed.  If the new value's slot is already held by a
        # different doc, raise ``DuplicateDocumentError`` and roll
        # back any partial claims.
        new_claims: list[tuple[str, str, str]] = []  # (field, value, doc_id)
        old_claims_released: list[tuple[str, Any, str]] = []  # (field, value, doc_id)
        try:
            for (coll, field, _), unique in list(self._iter_indexes()):
                if coll != collection or not unique:
                    continue
                new_value = doc.get(field)
                old_value = old_values.get(field)
                if new_value == old_value:
                    continue
                if old_value is not None:
                    # Release the old slot first so HSETNX can claim
                    # the new one unconditionally.
                    self._release_unique_index_slot(collection, field, old_value, target_id)
                    old_claims_released.append((field, old_value, target_id))
                if new_value is None:
                    # $unset: nothing to claim.
                    continue
                if not self._claim_unique_index_slot(collection, field, new_value, target_id):
                    raise DuplicateDocumentError(
                        f"duplicate value for unique-indexed field {field!r} "
                        f"in collection {collection!r}"
                    )
                new_claims.append((field, str(new_value), target_id))
        except DuplicateDocumentError:
            # Roll back partial claims on failure.
            for field, value, doc_id in new_claims:
                self._release_unique_index_slot(collection, field, value, doc_id)
            for field, value, doc_id in old_claims_released:
                self._claim_unique_index_slot(collection, field, value, doc_id)
            raise

        # Issue #1564: shared JSON encode helper.
        doc_json = _encode_value(doc)
        try:
            client.hset(coll_key, target_id, doc_json)
        except Exception as exc:
            # Roll back any unique-index claims we made.
            for field, value, doc_id in new_claims:
                self._release_unique_index_slot(collection, field, value, doc_id)
            for field, value, doc_id in old_claims_released:
                self._claim_unique_index_slot(collection, field, value, doc_id)
            raise DocumentStoreError(
                f"update_one failed for collection {collection!r}: {exc}"
            ) from exc

        self._lru_invalidate(collection)
        log.debug("update_one: collection=%s filter=%s", collection, filter_spec)
        return True

    def delete_one(
        self,
        collection: str,
        filter_spec: dict[str, Any],
    ) -> bool:
        """Delete a single document matching the filter."""
        client = self._get_client()
        coll_key = self._coll_key(collection)

        target_id: str | None = None
        if (
            isinstance(filter_spec, dict)
            and set(filter_spec.keys()) == {"_id"}
            and isinstance(filter_spec["_id"], str)
        ):
            target_id = filter_spec["_id"]
        else:
            for doc_id, doc in self._load_collection(collection).items():
                if _document_matches(doc, filter_spec):
                    target_id = doc_id
                    break

        if target_id is None:
            return False

        try:
            client.hdel(coll_key, target_id)
        except Exception as exc:
            raise DocumentStoreError(
                f"delete_one failed for collection {collection!r}: {exc}"
            ) from exc

        # Drop any unique-index entries that pointed at this doc.
        for (coll, _field, key), _unique in self._iter_indexes():
            if coll != collection:
                continue
            try:
                index_entries = client.hgetall(key)
            except Exception as exc:  # noqa: BLE001 - index scan during delete is cleanup; redis error logged and that index skipped
                log.warning(
                    "RedisDocumentStore: failed to load index entries for collection=%s field=%s: %s",
                    collection,
                    key,
                    exc,
                )
                continue
            for value, d in index_entries.items():
                if d == target_id:
                    try:
                        client.hdel(key, value)
                    except Exception as exc:  # noqa: BLE001 - index entry removal is cleanup; redis error logged and remaining entries still processed
                        log.warning(
                            "RedisDocumentStore: failed to delete index entry collection=%s field=%s value=%s: %s",
                            collection,
                            key,
                            value,
                            exc,
                        )

        self._lru_invalidate(collection)
        log.debug(
            "delete_one: collection=%s filter=%s -> deleted",
            collection,
            filter_spec,
        )
        return True

    def create_index(
        self,
        collection: str,
        field: str,
        *,
        unique: bool = False,
    ) -> None:
        """Create an index on a JSON field in a collection.

        For the Redis implementation, indexes are stored as Redis
        hashes ``field_value -> doc_id`` so the uniqueness check can
        happen atomically on insert / update.  Reads still load the
        full collection client-side (no server-side filter).
        """
        key = self._index_key(collection, field)
        # Pre-populate the index from existing docs so ``create_index``
        # is safe to call after data has been inserted.
        client = self._get_client()
        client.sadd(self._collections_key(), collection)
        for doc_id, doc in self._load_collection(collection).items():
            value = doc.get(field)
            if value is None:
                continue
            if unique:
                self._check_unique_index(collection, field, value, exclude_doc_id=doc_id)
            client.hset(key, str(value), doc_id)
        self._indexes.add((collection, field, key, unique))
        log.debug(
            "create_index: collection=%s field=%s unique=%s",
            collection,
            field,
            unique,
        )

    def list_collections(self) -> list[str]:
        """List all collection names."""
        try:
            members = self._get_client().smembers(self._collections_key())
        except Exception as exc:
            raise DocumentStoreError(f"list_collections failed: {exc}") from exc
        return sorted(members)

    def count_documents(
        self,
        collection: str,
        filter_spec: dict[str, Any] | None = None,
    ) -> int:
        """Count documents matching the filter."""
        docs = list(self._load_collection(collection).values())
        if not filter_spec:
            return len(docs)
        return sum(1 for d in docs if _document_matches(d, filter_spec))

    @property
    def breaker_state(self) -> str:
        """Current circuit breaker state (issue #1310)."""
        return self._breaker.state

    def close(self) -> None:
        """Close the Redis client, connection pool, and drop the in-process LRU."""
        self._lru_invalidate()
        if self._redis_client is not None:
            try:
                self._redis_client.close()
            except Exception as exc:  # noqa: BLE001 - closing a possibly-broken redis client; failure logged during shutdown
                log.warning(
                    "RedisDocumentStore: error closing client namespace=%s: %s",
                    self.namespace,
                    exc,
                )
            self._redis_client = None
        if self._pool is not None:
            try:
                self._pool.disconnect()
            except Exception as exc:  # noqa: BLE001 - disconnecting a possibly-broken redis pool; failure logged during shutdown
                log.warning(
                    "RedisDocumentStore: error closing pool namespace=%s: %s",
                    self.namespace,
                    exc,
                )
            self._pool = None
        log.debug("RedisDocumentStore closed namespace=%s", self.namespace)

    def __enter__(self) -> RedisDocumentStore:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def __del__(self) -> None:
        with contextlib.suppress(Exception):
            self.close()


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


class DocumentStoreConfig(TypedDict, total=False):
    """Configuration for building a document store."""

    backend: str
    db_path: Path
    redis_url: str
    namespace: str


def _build_document_store_redis(
    redis_url: str,
    namespace: str,
    db_path: Path,
    *,
    require_auth: bool = False,
    redis_ssl_context: ssl.SSLContext | None = None,
) -> DocumentStore:
    """Return a ``RedisDocumentStore`` for distributed campaigns (issue #1014).

    Security baseline (issue #1277, #1321): delegates to the shared public
    :func:`osimflow.distributed_cache.validate_redis_url` so the four
    Redis-backed planes stay a single source of truth.
    """
    validate_redis_url(redis_url, require_auth)
    return RedisDocumentStore(
        redis_url=redis_url,
        namespace=namespace,
        db_path=db_path,
        redis_ssl_context=redis_ssl_context,
    )


def build_document_store(
    db_path: Path,
    redis_url: str | None = None,
    namespace: str | None = None,
    *,
    backend: str | None = None,
    require_auth: bool = False,
    redis_ssl_context: ssl.SSLContext | None = None,
) -> DocumentStore:
    """Factory: build the appropriate ``DocumentStore`` from configuration.

    When *redis_url* is ``None`` (or only the legacy *backend* kwarg is
    supplied), returns a plain ``SQLiteDocumentStore`` at *db_path* —
    the single-node default, unchanged (issue #993 keeps SQLite for
    single-node local mode). When a Redis URL is provided *and*
    *namespace* is set, returns a ``RedisDocumentStore`` whose shared
    document state lives in Redis hashes — concurrent processes
    coordinating on the same campaign never contend on a single
    SQLite database (the T8.1 reproducer fixed for the cache by issue
    #993, now extended to the document store by issue #1014).

    Parameters
    ----------
    db_path
        Path to the local SQLite database file.  Used directly by the
        single-node ``SQLiteDocumentStore``; the ``RedisDocumentStore``
        accepts it for parity / logging but does not maintain a local
        SQLite file (the authoritative state lives in Redis).
    redis_url
        Redis connection URL (e.g. ``redis://localhost:6379/0``).
        ``None`` disables the distributed document store.
    namespace
        Stable Redis namespace for the campaign's shared state (see
        ``distributed_cache.campaign_state_namespace``).  Required
        when *redis_url* is set.
    backend
        Legacy explicit selector.  ``backend="sqlite"`` is identical
        to ``redis_url=None``; ``backend="redis"`` requires
        ``redis_url`` + ``namespace``.  Raises ``ValueError`` for
        unknown values.
    require_auth
        When True, skips the URL-level credential check.  Set this when
        Redis authentication is handled externally (e.g. via an ``AUTH``
        file consumed by the Redis server, not the client).  Issue #1277.
    redis_ssl_context
        Optional ``ssl.SSLContext`` to pass to
        ``redis.ConnectionPool.from_url()`` for custom CA bundles or
        disabled verification (issue #1327).

    Returns
    -------
    DocumentStore
        The concrete document store instance.

    Raises
    ------
    ValueError
        When ``backend`` is not ``"sqlite"`` or ``"redis"``, or when
        ``backend="redis"`` is requested without ``redis_url`` and
        ``namespace``, or when a non-localhost Redis URL lacks TLS and
        credentials (issue #1277).
    """
    if backend is not None:
        if backend == "sqlite":
            return SQLiteDocumentStore(db_path=db_path)
        if backend == "redis":
            if redis_url is None or namespace is None:
                raise ValueError(
                    "build_document_store: backend='redis' requires redis_url and namespace"
                )
            return _build_document_store_redis(
                redis_url,
                namespace,
                db_path,
                require_auth=require_auth,
                redis_ssl_context=redis_ssl_context,
            )
        raise ValueError(f"unknown document_store_backend: {backend!r}")

    if redis_url is None:
        return SQLiteDocumentStore(db_path=db_path)
    if namespace is None:
        raise ValueError(
            "build_document_store: redis_url requires a namespace "
            "(use campaign_state_namespace(outdir) to derive one).",
        )
    return _build_document_store_redis(
        redis_url,
        namespace,
        db_path,
        require_auth=require_auth,
        redis_ssl_context=redis_ssl_context,
    )
