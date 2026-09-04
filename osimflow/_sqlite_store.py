"""Shared SQLite access primitive (issue #1564).

Single source of truth for:

* SQLite connection setup — WAL + ``busy_timeout`` + ``synchronous=NORMAL``
  + ``locking_mode=NORMAL`` + ``check_same_thread=False`` + ``row_factory``
  + ``timeout=10.0`` (the SQLite-recommended set for a database shared
  across multiple processes / threads — issues #620, #993, #1006, #1340).
* Per-pid path scheme (issue #993) — concurrent campaigns coordinate on
  Redis-backed shared state and use a pid-suffixed local SQLite file, so
  two processes never lock the same database.
* JSON encoding helpers (``encode_value`` / ``decode_value``) — used by
  :mod:`osimflow.registry`, :mod:`osimflow.document_store`,
  :mod:`osimflow.event_log`.
* Retry-with-exponential-backoff on :class:`sqlite3.OperationalError` —
  covers "database is locked" during peer-checkpoint contention.
* ``transaction`` context manager for explicit commit/rollback.

Each store owns its own persistent-connection / threading-lock /
cache-specific PRAGMA tweaks (e.g. ``cache_size=-65536``, ``mmap_size``,
``temp_store`` from issue #1016). This module owns the canonical
*shared* surface so the WAL/busy/retry pattern lives in one place.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import logging
import os
import sqlite3
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, TypeVar

T = TypeVar("T")

log = logging.getLogger("osimflow._sqlite_store")

__all__ = [
    "StoreConfig",
    "connect",
    "decode_value",
    "encode_value",
    "per_pid_path",
    "transaction",
    "with_retries",
]


@dataclasses.dataclass(frozen=True)
class StoreConfig:
    """Configuration for :func:`connect` and :func:`with_retries`.

    Attributes
    ----------
    path
        SQLite database file path.  The connection is opened against
        ``per_pid_path(path)`` when ``pid_isolation=True``.
    busy_timeout_ms
        ``PRAGMA busy_timeout`` in milliseconds (default 5000).  Writers
        wait this long for a peer-held lock before raising
        ``database is locked``.
    max_retries
        Default retry attempts for :func:`with_retries` (default 5).
    initial_backoff_ms
        Initial backoff for :func:`with_retries` (default 50 ms,
        doubles each attempt — 50/100/200/400/800 ms for the default 5
        attempts).
    """

    path: Path
    busy_timeout_ms: int = 5000
    max_retries: int = 5
    initial_backoff_ms: int = 50


def per_pid_path(base: Path) -> Path:
    """Return the pid-suffixed sibling of *base* (issue #993).

    ``cache.sqlite`` -> ``cache.p<pid>.sqlite``.  The suffix is preserved
    so artifact-manifest categorisation (which keys on suffix) still
    classifies the file.  Every process gets its own file, so concurrent
    campaign processes never lock the same SQLite database — the fix
    for the T8.1 SQLite-lock reproducer.

    Used by :class:`osimflow.distributed_cache.DistributedCache` for its
    process-private local SQLite layer.
    """
    return base.with_name(f"{base.stem}.p{os.getpid()}{base.suffix}")


def connect(
    path: Path,
    *,
    config: StoreConfig | None = None,
    pid_isolation: bool = False,
) -> sqlite3.Connection:
    """Open a SQLite connection with the canonical OSimFlow PRAGMA set.

    PRAGMAs applied:

    * ``journal_mode=WAL`` — readers never block the writer and vice versa.
    * ``synchronous=NORMAL`` — safe with WAL, much faster than FULL (issue
      #1340).  Durable across OS crashes.
    * ``busy_timeout=5000`` — writers wait up to 5 s for a peer-held lock
      instead of raising ``database is locked`` immediately (issue #620).
    * ``locking_mode=NORMAL`` — default; stated explicitly so a future
      edit cannot silently flip to EXCLUSIVE and starve peer workers.
    * ``check_same_thread=False`` + ``timeout=10.0`` — safe across the
      fan-out thread pool without starving a slow writer.
    * ``row_factory=sqlite3.Row`` — column access by name everywhere.

    ``journal_mode`` is per-database, not per-connection, so once one
    connection sets WAL the file keeps it across reconnects.  Stores
    that need extra cache-specific PRAGMAs (``cache_size=-65536``,
    ``mmap_size=268435456``, ``temp_store=MEMORY`` — issue #1016) set
    them after this call returns.

    Parameters
    ----------
    path
        Database file path.
    config
        Optional :class:`StoreConfig` overriding busy_timeout_ms etc.
    pid_isolation
        When ``True`` the connection is opened against the per-pid
        sibling of *path* (see :func:`per_pid_path`).  Used by
        :class:`osimflow.distributed_cache.DistributedCache` so its
        process-private local SQLite layer never contends on the
        shared DB file (issue #993).
    """
    cfg = config or StoreConfig(path=path)
    actual = per_pid_path(path) if pid_isolation else path
    conn = sqlite3.connect(actual, timeout=10.0, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute(f"PRAGMA busy_timeout={cfg.busy_timeout_ms}")
    conn.execute("PRAGMA locking_mode=NORMAL")
    conn.row_factory = sqlite3.Row
    return conn


def encode_value(
    value: Any,
    *,
    sort_keys: bool = True,
    default: Callable[[Any], Any] = str,
) -> str:
    """JSON-encode *value* with the OSimFlow canonical settings.

    Mirrors the per-store ``json.dumps(..., default=str)`` calls
    scattered across :mod:`osimflow.registry`,
    :mod:`osimflow.document_store`, and :mod:`osimflow.event_log`.
    Centralised here so future encoding changes (e.g. adding
    ``ensure_ascii=False`` for unicode-heavy campaigns) live in one
    place.

    Parameters
    ----------
    value
        JSON-serializable Python object.
    sort_keys
        When ``True`` (default) keys are sorted — gives a stable hash
        for cache / registry rows that include JSON-encoded blobs.
    default
        Callable passed to ``json.dumps`` for non-serializable values.
    """
    return json.dumps(value, sort_keys=sort_keys, default=default)


def decode_value(blob: str | None, default: Any = None) -> Any:
    """JSON-decode *blob* with safe fallback on missing or corrupt input.

    Returns *default* when *blob* is empty / ``None`` or fails to parse
    as JSON.  Mirrors the per-store
    ``try: json.loads(...) except (JSONDecodeError, TypeError): default = {}``
    pattern previously open-coded in :class:`osimflow.registry.CampaignRecord`.
    """
    if not blob:
        return default
    try:
        return json.loads(blob)
    except (json.JSONDecodeError, TypeError):
        return default


def with_retries[T](
    operation: Callable[[], T],
    *,
    max_retries: int = 5,
    initial_backoff_ms: int = 50,
    logger: logging.Logger | None = None,
) -> T:
    """Run *operation*; retry with exponential backoff on ``OperationalError``.

    Covers the canonical transient errors a shared SQLite database can
    surface during a campaign: ``database is locked`` when a peer holds
    the writer lock for longer than ``busy_timeout`` (because the peer
    was paused / mid-checkpoint), ``disk I/O error`` during checkpoint
    contention, etc.

    Exponential backoff doubles each attempt starting at
    ``initial_backoff_ms``.  After ``max_retries`` attempts the final
    :class:`sqlite3.OperationalError` is re-raised so the caller's
    error-handling path can run.
    """
    _log = logger if logger is not None else log
    delay_s = initial_backoff_ms / 1000.0
    for attempt in range(max_retries):
        try:
            return operation()
        except sqlite3.OperationalError as exc:
            if attempt >= max_retries - 1:
                raise
            _log.debug(
                "sqlite retry attempt=%d/%d err=%s backoff_ms=%.0f",
                attempt + 1,
                max_retries,
                exc,
                delay_s * 1000,
            )
            time.sleep(delay_s)
            delay_s *= 2
    raise RuntimeError("unreachable — covered by raise above")  # pragma: no cover


@contextlib.contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Commit on clean exit, rollback on exception.

    Use when a store wants explicit transaction semantics outside the
    ``sqlite3.Connection`` built-in context manager (which uses
    implicit transactions whose boundaries are tricky across
    multi-statement blocks).
    """
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
