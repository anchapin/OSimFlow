"""SQLite-backed cache for OSimFlow campaign runs.

The cache is an explicit, content-hash-based resume layer: every invalidation
rule must be reviewable, testable, and correct. It is intentionally small
(~120 lines) for that reason.

Cache key shape (PRIMARY KEY):
    (step, sample_id, openstudio_version, inputs_sha256, code_sha256,
     container_digest)

Invalidation rules (per gotcha #3 and the analysis in
`.agents/results/result-architecture.md`):
  * Editing a file in `bin/` invalidates the steps that reference it,
    for ALL samples.
  * Changing `--openstudio_version` invalidates `RUN_OPENSTUDIO_SIM`
    entries only (it does not affect LHS, apply, extract, aggregate).
  * Changing `template_sim_package` content invalidates `APPLY_PARAMETERS`
    and `RUN_OPENSTUDIO_SIM` (the modified sim package is derived from it).
  * Changing `variables.yml` invalidates `GENERATE_LHS_SAMPLES` (and
    therefore every downstream step that depends on the sample set).

The DB schema is small enough to inspect with `sqlite3 cache.db ".schema"`.
"""

from __future__ import annotations

__all__ = ["CacheKey", "CacheStats", "SQLiteCache", "sha256_of_dict", "sha256_of_files"]

import contextlib
import dataclasses
import hashlib
import json
import logging
import sqlite3
import threading
import time
from collections.abc import Iterable
from pathlib import Path

log = logging.getLogger("osimflow.cache")


@dataclasses.dataclass
class CacheStats:
    """Cache hit/miss and invalidation statistics."""

    hits: int = 0
    misses: int = 0
    invalidations: int = 0
    total_keys: int = 0


SCHEMA = """
CREATE TABLE IF NOT EXISTS cache_entries (
    step              TEXT NOT NULL,
    sample_id         TEXT NOT NULL,         -- 'ALL' for singleton steps
    openstudio_version TEXT NOT NULL,        -- 'N/A' for non-OS steps
    inputs_sha256     TEXT NOT NULL,
    code_sha256       TEXT NOT NULL,
    container_digest  TEXT NOT NULL,
    generation        INTEGER NOT NULL DEFAULT 0,
    output_path       TEXT NOT NULL,
    started_at        REAL NOT NULL,
    finished_at      REAL NOT NULL,
    exit_code         INTEGER NOT NULL,
    PRIMARY KEY (step, sample_id, openstudio_version, inputs_sha256, code_sha256, container_digest, generation)
);
CREATE INDEX IF NOT EXISTS ix_cache_step ON cache_entries(step);
"""


@dataclasses.dataclass(frozen=True)
class CacheKey:
    """Inputs that, if any one changes, invalidate the cached output."""

    step: str
    sample_id: str
    openstudio_version: str
    inputs_sha256: str
    code_sha256: str
    container_digest: str
    generation: int = 0


def sha256_of_files(paths: Iterable[Path]) -> str:
    """Hash a set of files (path-sorted for determinism)."""
    h = hashlib.sha256()
    for p in sorted(paths, key=str):
        h.update(str(p).encode())
        h.update(b"\0")
        if p.is_file():
            h.update(p.read_bytes())
        else:
            # directory: hash the sorted listing
            for child in sorted(p.rglob("*"), key=str):
                h.update(str(child.relative_to(p)).encode())
                h.update(b"\0")
                if child.is_file():
                    h.update(child.read_bytes())
    return h.hexdigest()


def sha256_of_dict(d: dict[str, object]) -> str:
    """Stable hash of a JSON-serializable dict (sort_keys=True)."""
    blob = json.dumps(d, sort_keys=True, default=str).encode()
    return hashlib.sha256(blob).hexdigest()


class SQLiteCache:
    """The campaign's resume cache. Append-only on hits, INSERT OR REPLACE on misses.

    The cache uses a single persistent connection in WAL mode for the
    lifetime of the instance. Within a single process, this avoids the
    race condition where concurrent connections fighting over the same
    ``.sqlite-wal`` and ``.sqlite-shm`` auxiliary files cause
    ``FileNotFoundError`` during pytest-xdist parallel test teardown.

    **Cross-process safety (issue #620).** In a multi-worker campaign
    each worker is a separate process with its own ``SQLiteCache``
    instance pointed at the same ``db_path``. The connection PRAGMAs
    below (``WAL`` + ``synchronous=NORMAL`` + ``busy_timeout`` +
    ``locking_mode=NORMAL``) are the SQLite-recommended set for that
    shared-database shape, and ``close()`` uses a ``PASSIVE`` checkpoint
    that **never removes** the ``-wal``/``-shm`` aux files. Removing
    them out from under peer processes during campaign cancellation
    was the root cause of ``FileNotFoundError: cache.sqlite-shm``.

    The cache is itself a context manager — use it with ``with`` to ensure
    the WAL is checkpointed and the connection is closed when done::

        with SQLiteCache(db_path) as cache:
            cache.store(key, path, exit_code=0)

    Or call ``close()`` explicitly::

        cache = SQLiteCache(db_path)
        try:
            cache.store(key, path, exit_code=0)
        finally:
            cache.close()
    """

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._conn: sqlite3.Connection | None = None
        # RLock (reentrant): the operation methods (store/lookup/...)
        # hold this lock and then access ``self.connection``, which in
        # turn acquires the lock again inside ``_ensure_conn``. A plain
        # Lock would deadlock on that re-entrancy (issue #620).
        self._lock = threading.RLock()
        self._stats = CacheStats()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()
        log.info("cache opened at %s", db_path)

    def _init_db(self) -> None:
        # Use the persistent connection (lazily opened) rather than a
        # transient open/close. Every extra open/close churns the WAL
        # aux files (-wal/-shm) and contributes to multi-process races
        # during campaign cancellation (issue #620).
        conn = self._ensure_conn()
        conn.executescript(SCHEMA)
        conn.commit()

    def _connect(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.db_path, timeout=10.0, check_same_thread=False)
        # WAL + this PRAGMA combination is the SQLite-recommended set
        # for a database shared across multiple processes (issue #620):
        #   * journal_mode=WAL    — readers never block the writer and
        #                           the writer never blocks readers.
        #   * synchronous=NORMAL  — safe with WAL (much faster than FULL)
        #                           and durable across OS crashes.
        #   * busy_timeout=5000   — writers wait up to 5s for a lock
        #                           held by a peer process instead of
        #                           raising ``database is locked``.
        #   * locking_mode=NORMAL — the default, stated explicitly so a
        #                           future edit can never silently flip
        #                           to EXCLUSIVE and starve peer workers.
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        c.execute("PRAGMA busy_timeout=5000")
        c.execute("PRAGMA locking_mode=NORMAL")
        c.row_factory = sqlite3.Row
        return c

    def _ensure_conn(self) -> sqlite3.Connection:
        # RLock makes this safe to call from inside the operation methods
        # (which already hold the lock). The lock guards both the
        # initial-open race (two threads must not open two connections
        # and leak one) and the close-vs-operate race (close sets
        # ``_conn = None`` under the same lock — issue #620).
        with self._lock:
            if self._conn is None:
                self._conn = self._connect()
            return self._conn

    @property
    def connection(self) -> sqlite3.Connection:
        """Return the persistent connection (lazy initialization)."""
        return self._ensure_conn()

    def close(self) -> None:
        """Best-effort WAL checkpoint and connection teardown.

        Uses ``PRAGMA wal_checkpoint(PASSIVE)`` — the SQLite default. A
        PASSIVE checkpoint copies as much WAL content as it can back into
        the main DB **without blocking** and **without removing** the
        auxiliary ``-wal``/``-shm`` files. Those files are left in place
        for SQLite to manage cooperatively across every process that
        shares the cache. This is what makes ``close()`` safe to call
        during multi-worker campaign cancellation while peer processes
        still have the cache open (issue #620): the previous
        ``wal_checkpoint(TRUNCATE)`` removed the aux files out from under
        peers and crashed them with ``FileNotFoundError: cache.sqlite-shm``.

        The method is:

        * **thread-safe** — guarded by ``_lock`` so a concurrent
          ``store``/``lookup`` cannot observe a half-closed connection.
        * **idempotent** — calling it on an already-closed cache is a
          no-op. Subsequent operations re-open the connection lazily.
        * **non-raising** — ``sqlite3.OperationalError`` and
          ``FileNotFoundError`` raised during the best-effort checkpoint
          are logged and swallowed so a ``finally:`` block can call this
          without masking the campaign's original status.
        """
        with self._lock:
            conn = self._conn
            if conn is None:
                return
            # Drop the reference under the lock so any concurrent
            # operation that is already mid-flight (holding the lock)
            # finishes with its locally-captured connection reference;
            # the next operation will re-open lazily.
            self._conn = None
        try:
            # Commit any pending transaction before checkpointing. Without
            # this the WAL checkpoint may report that uncommitted data is
            # still in the WAL.
            conn.commit()
            # PASSIVE checkpoint: writes back as much WAL content as
            # possible without blocking peers and WITHOUT removing the
            # aux files. The old TRUNCATE checkpoint was the bug — it
            # removed -wal/-shm out from under peer processes during
            # campaign cancellation (issue #620).
            conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
            conn.execute("PRAGMA optimize")
        except sqlite3.OperationalError:
            # e.g. "database is locked" because a peer is mid-write.
            # Best-effort shutdown: log and fall through to close().
            log.warning(
                "cache checkpoint failed at %s (best-effort close)",
                self.db_path,
                exc_info=True,
            )
        except FileNotFoundError:
            # A peer process removed a transient aux file between our
            # open and our checkpoint. Non-fatal: SQLite recreates the
            # aux files on the next access.
            log.warning(
                "cache aux file missing during close at %s (likely a peer checkpoint; non-fatal)",
                self.db_path,
                exc_info=True,
            )
        finally:
            with contextlib.suppress(Exception):
                conn.close()
            log.debug("cache closed at %s", self.db_path)

    def __enter__(self) -> SQLiteCache:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def __del__(self) -> None:
        self.close()

    def lookup(self, key: CacheKey) -> Path | None:
        """Return the cached output path if this exact key is present and successful."""
        with self._lock:
            c = self.connection
            row = c.execute(
                """SELECT output_path, exit_code FROM cache_entries
               WHERE step=? AND sample_id=? AND openstudio_version=?
                 AND inputs_sha256=? AND code_sha256=? AND container_digest=?
                 AND generation=?""",
                (
                    key.step,
                    key.sample_id,
                    key.openstudio_version,
                    key.inputs_sha256,
                    key.code_sha256,
                    key.container_digest,
                    key.generation,
                ),
            ).fetchone()
        if row is None:
            self._stats.misses += 1
            return None
        if row["exit_code"] != 0:
            self._stats.misses += 1
            return None
        out = Path(row["output_path"])
        if not out.exists():
            # Stale cache entry: the output was deleted out from under us.
            log.warning("cache hit but output missing on disk: %s", out)
            self._stats.misses += 1
            return None
        self._stats.hits += 1
        log.info("cache HIT  step=%s sample=%s -> %s", key.step, key.sample_id, out)
        return out

    def store(self, key: CacheKey, output_path: Path, exit_code: int) -> None:
        with self._lock:
            c = self.connection
            c.execute(
                """INSERT OR REPLACE INTO cache_entries
                   (step, sample_id, openstudio_version, inputs_sha256,
                    code_sha256, container_digest, generation, output_path,
                    started_at, finished_at, exit_code)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    key.step,
                    key.sample_id,
                    key.openstudio_version,
                    key.inputs_sha256,
                    key.code_sha256,
                    key.container_digest,
                    key.generation,
                    str(output_path),
                    time.time(),
                    time.time(),
                    exit_code,
                ),
            )
        log.info(
            "cache STORE step=%s sample=%s exit=%d -> %s",
            key.step,
            key.sample_id,
            exit_code,
            output_path,
        )

    def invalidate_step(self, step: str) -> int:
        """Drop every entry for a given step. Used by --openstudio_version bumps."""
        with self._lock:
            c = self.connection
            cur = c.execute("DELETE FROM cache_entries WHERE step=?", (step,))
            n = cur.rowcount
        self._stats.invalidations += n
        log.warning("cache INVALIDATE step=%s (%d rows)", step, n)
        return n

    def invalidate_sample(self, step: str, sample_id: str) -> int:
        with self._lock:
            c = self.connection
            cur = c.execute(
                "DELETE FROM cache_entries WHERE step=? AND sample_id=?",
                (step, sample_id),
            )
            n = cur.rowcount
        self._stats.invalidations += n
        log.info("cache INVALIDATE step=%s sample=%s (%d rows)", step, sample_id, n)
        return n

    def stats(self) -> dict[str, object]:
        with self._lock:
            c = self.connection
            n_total = c.execute("SELECT COUNT(*) FROM cache_entries").fetchone()[0]
            by_step = dict(
                c.execute("SELECT step, COUNT(*) FROM cache_entries GROUP BY step").fetchall()
            )
        return {"total": n_total, "by_step": by_step}

    def get_stats(self) -> CacheStats:
        """Return current cache statistics."""
        with self._lock:
            c = self.connection
            n_total = c.execute("SELECT COUNT(*) FROM cache_entries").fetchone()[0]
        return CacheStats(
            hits=self._stats.hits,
            misses=self._stats.misses,
            invalidations=self._stats.invalidations,
            total_keys=n_total,
        )

    def get_cache_hit_rate(self) -> float:
        """Return cache hit rate as a float between 0.0 and 1.0."""
        total = self._stats.hits + self._stats.misses
        if total == 0:
            return 0.0
        return self._stats.hits / total
