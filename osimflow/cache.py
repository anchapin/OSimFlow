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

import contextlib
import dataclasses
import hashlib
import json
import logging
import shutil
import sqlite3
import subprocess
import threading
import time
from collections.abc import Iterable
from pathlib import Path

__all__ = [
    "CacheEntry",
    "CacheKey",
    "CacheStats",
    "SQLiteCache",
    "sha256_of_dict",
    "sha256_of_files",
]

#: Threshold above which ``SQLiteCache.lookup_many`` switches from a tuple-IN
#: query to a temporary-table JOIN. Below this we keep the SQL string compact
#: and the parameter list easy to inspect; above it we trade a CREATE/INSERT/
#: SELECT/DROP round-trip for an INSERT-bound batch that scales linearly.
_LOOKUP_MANY_IN_THRESHOLD = 100

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


@dataclasses.dataclass(frozen=True)
class CacheEntry:
    """A successful cache hit: the cached output path plus run metadata.

    Returned by :meth:`SQLiteCache.lookup_many`. ``lookup`` historically returns
    just ``Path`` — :class:`CacheEntry` is the batch-friendly equivalent that
    preserves the per-row ``exit_code`` / timing fields from the ``cache_entries``
    schema for callers that need them (e.g. observability backends, audits).
    """

    output_path: Path
    started_at: float
    finished_at: float
    exit_code: int


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


# Timeout for ``docker inspect``. 5s is long enough for a healthy daemon
# on a warm cache and short enough to keep campaign init snappy when
# docker is hung or absent. Issue #1023.
_DOCKER_INSPECT_TIMEOUT_S: float = 5.0


def _resolve_image_digest(
    tag: str,
    *,
    docker_path: str | None = None,
) -> str:
    """Resolve the canonical content-addressable digest for an image ``tag``.

    Tries ``docker inspect --format='{{index .RepoDigests 0}}' <tag>`` and
    returns the resulting reference — typically ``<repo>@sha256:<hex>`` —
    when a docker daemon is reachable, the image has been pulled, and the
    call completes within ``_DOCKER_INSPECT_TIMEOUT_S``.

    Falls back to ``sha256:<sha256(tag)>`` when any of the following
    hold: ``docker`` is not on PATH, the daemon is down, the image has
    not been pulled (RepoDigests is empty), the call exceeds the
    timeout, or the call returns non-zero. The fallback is
    deterministic per tag and content-addressable across machines —
    the cache key still varies when the label changes, but cannot
    detect an image rebuild under the same tag (which is the documented
    trade-off of offline operation).

    Issue #1023: the cache key used to store the mutable tag string
    (``docker.io/nrel/openstudio:3.11.0``) instead of the digest. Two
    rebuilds of the same tag produced identical cache keys, so a
    silently stale simulation could be served from cache. Resolving at
    config/campaign-init time means every cache lookup agrees on the
    digest regardless of subsequent republishing.
    """
    docker = docker_path or shutil.which("docker")
    if docker and tag:
        try:
            cp = subprocess.run(  # noqa: S603 — argv-controlled, no shell
                [docker, "inspect", "--format={{index .RepoDigests 0}}", tag],
                capture_output=True,
                text=True,
                timeout=_DOCKER_INSPECT_TIMEOUT_S,
                check=False,
            )
        except (subprocess.SubprocessError, OSError, TimeoutError):
            cp = None
        if cp is not None and cp.returncode == 0:
            ref = (cp.stdout or "").strip()
            # Sanity check: docker's RepoDigests format is ``<repo>@<digest>``
            # (the digest may be sha256:... or, for OCI, sha512:...). Empty
            # output means the image is not pulled locally. We require the
            # ``@`` separator before accepting the value as a digest.
            if "@" in ref:
                return ref
    # Offline / no-docker / image-not-pulled fallback. The label is hashed
    # so the cache key still varies per tag and the result is reproducible
    # across machines that lack docker.
    return f"sha256:{hashlib.sha256(tag.encode('utf-8')).hexdigest()}"


def _container_digest_for(label: str) -> str:
    """Cache-row ``container_digest`` value for an image ``label``.

    Format: ``<label>@<digest>`` where ``digest`` is either the resolved
    RepoDigests reference from docker (e.g. ``nrel/openstudio@sha256:<hex>``)
    or, when resolution fails, ``sha256:<sha256(label)>`` as a deterministic
    fallback.

    The combined form preserves the human-readable label (so ``run.json``,
    the campaign registry, and ad-hoc ``sqlite3`` queries can still
    identify the image at a glance) while embedding the content-
    addressable digest so cache invalidation works when an image is
    rebuilt under the same tag.

    Backward-compatibility (issue #1023): an old cache row that stored
    just the label (``docker.io/nrel/openstudio:3.11.0``) no longer
    matches the new ``<label>@<digest>`` form and is treated as a cache
    miss. The DB schema is unchanged — no migration is required.
    """
    if not label:
        # Defensive: empty labels would otherwise produce ``@sha256:<...>``
        # and risk colliding with a real ``@sha256:e3b0...`` fallback.
        return "unresolved"
    return f"{label}@{_resolve_image_digest(label)}"


def _row_to_cache_dict(
    rows: list[sqlite3.Row],
) -> dict[CacheKey, tuple[str, float, float, int]]:
    """Map :class:`sqlite3.Row` rows from a cache SELECT into a dict keyed by :class:`CacheKey`.

    Shared between :meth:`SQLiteCache._lookup_many_in` and
    :meth:`SQLiteCache._lookup_many_temp_table` so the row-shape mapping lives
    in one place.
    """
    return {
        CacheKey(
            step=row["step"],
            sample_id=row["sample_id"],
            openstudio_version=row["openstudio_version"],
            inputs_sha256=row["inputs_sha256"],
            code_sha256=row["code_sha256"],
            container_digest=row["container_digest"],
            generation=row["generation"],
        ): (
            row["output_path"],
            row["started_at"],
            row["finished_at"],
            row["exit_code"],
        )
        for row in rows
    }


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
        #   * cache_size=-65536    — 64 MiB negative-paged cache working set
        #                           for multi-GB caches (issue #1016).
        #   * mmap_size=268435456 — 256 MiB memory-mapped I/O window so
        #                           reads beyond the page cache skip the
        #                           read() syscall path.
        #   * temp_store=MEMORY   — keep ORDER BY / GROUP BY spill buffers
        #                           in RAM instead of spilling to disk.
        c.execute("PRAGMA cache_size=-65536")
        c.execute("PRAGMA mmap_size=268435456")
        c.execute("PRAGMA temp_store=MEMORY")
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

    def lookup_many(self, keys: list[CacheKey]) -> dict[CacheKey, CacheEntry | None]:
        """Batch lookup: return a :class:`CacheEntry` per hit, ``None`` per miss.

        Collapses N round-trips (one ``SELECT`` per :meth:`lookup` call) into a
        single batched read so the per-step cache-check loop in
        ``Campaign.step_*`` can resolve hundreds of sample keys with one
        SQLite query.

        Returns a dict keyed by every **distinct** input :class:`CacheKey` (dict
        semantics naturally collapse duplicates). Each value is a
        :class:`CacheEntry` on a hit, ``None`` on a miss — defined as the union
        of "no row", "row present but ``exit_code != 0``" and "row points at a
        path that no longer exists on disk". This matches the contract of
        :meth:`lookup` so callers can swap one for the other without semantic
        drift. The ``CacheStats`` hit/miss counters are updated exactly once
        per distinct key, again mirroring per-call :meth:`lookup` semantics.

        Threshold-based SQL strategy:

        * ``len(keys) <= _LOOKUP_MANY_IN_THRESHOLD`` (100): a single
          ``SELECT ... WHERE (key_cols) IN ((?,?,?,?,?,?,?), ...)`` query.
          Compact SQL, easy to inspect in traces.
        * ``len(keys) > _LOOKUP_MANY_IN_THRESHOLD``: ``CREATE TEMP TABLE`` +
          ``INSERT`` (via ``executemany``) + ``LEFT JOIN ... USING (...)``
          + ``DROP TABLE``. Issues one ``SELECT`` regardless of N; the
          ``INSERT`` is the only cost that scales with N and it remains a
          single ``executemany`` round-trip.

        No new locks beyond the existing ``_lock``.
        """
        if not keys:
            return {}

        # Preserve insertion order while deduping so identical keys in the
        # input list are not double-counted in hit/miss bookkeeping.
        seen: set[CacheKey] = set()
        unique_keys: list[CacheKey] = []
        for k in keys:
            if k not in seen:
                seen.add(k)
                unique_keys.append(k)

        with self._lock:
            c = self.connection
            if len(unique_keys) <= _LOOKUP_MANY_IN_THRESHOLD:
                raw_hits = self._lookup_many_in(c, unique_keys)
            else:
                raw_hits = self._lookup_many_temp_table(c, unique_keys)

        result: dict[CacheKey, CacheEntry | None] = {}
        for key in unique_keys:
            row = raw_hits.get(key)
            if row is None:
                self._stats.misses += 1
                result[key] = None
                continue
            output_path_str, started_at, finished_at, exit_code = row
            if exit_code != 0:
                self._stats.misses += 1
                result[key] = None
                continue
            out = Path(output_path_str)
            if not out.exists():
                # Stale cache entry: the output was deleted out from under us.
                log.warning("cache hit but output missing on disk: %s", out)
                self._stats.misses += 1
                result[key] = None
                continue
            result[key] = CacheEntry(
                output_path=out,
                started_at=started_at,
                finished_at=finished_at,
                exit_code=exit_code,
            )
            self._stats.hits += 1
            log.info("cache HIT  step=%s sample=%s -> %s", key.step, key.sample_id, out)
        # ``keys`` may contain duplicates that dedupe in the dict. Mirror the
        # requested set: every requested key maps to the same value.
        return {k: result[k] for k in keys}

    def _lookup_many_in(
        self,
        c: sqlite3.Connection,
        keys: list[CacheKey],
    ) -> dict[CacheKey, tuple[str, float, float, int]]:
        """Single-query tuple-IN read. Helper for :meth:`lookup_many` (small N)."""
        key_cols = (
            "(step, sample_id, openstudio_version, inputs_sha256, "
            "code_sha256, container_digest, generation)"
        )
        tuple_ph = "(" + ",".join(["?"] * 7) + ")"
        placeholders = ",".join([tuple_ph] * len(keys))
        sql = (
            "SELECT step, sample_id, openstudio_version, inputs_sha256, "
            "code_sha256, container_digest, generation, "
            "output_path, started_at, finished_at, exit_code "
            f"FROM cache_entries WHERE {key_cols} IN ({placeholders})"
        )
        params: list[object] = []
        for k in keys:
            params.extend(
                [
                    k.step,
                    k.sample_id,
                    k.openstudio_version,
                    k.inputs_sha256,
                    k.code_sha256,
                    k.container_digest,
                    k.generation,
                ]
            )
        rows = c.execute(sql, params).fetchall()
        return _row_to_cache_dict(rows)

    def _lookup_many_temp_table(
        self,
        c: sqlite3.Connection,
        keys: list[CacheKey],
    ) -> dict[CacheKey, tuple[str, float, float, int]]:
        """CREATE TEMP TABLE + bulk INSERT + JOIN + DROP. Helper for :meth:`lookup_many` (large N)."""
        sql_create = (
            "CREATE TEMP TABLE IF NOT EXISTS _osimflow_lookup_many_keys ("
            "  step TEXT, sample_id TEXT, openstudio_version TEXT,"
            "  inputs_sha256 TEXT, code_sha256 TEXT, container_digest TEXT,"
            "  generation INTEGER, PRIMARY KEY (step, sample_id, "
            "    openstudio_version, inputs_sha256, code_sha256, "
            "    container_digest, generation)"
            ")"
        )
        sql_drop = "DROP TABLE _osimflow_lookup_many_keys"
        sql_insert = (
            "INSERT OR IGNORE INTO _osimflow_lookup_many_keys "
            "(step, sample_id, openstudio_version, inputs_sha256, "
            "code_sha256, container_digest, generation) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)"
        )
        sql_select = (
            "SELECT k.step, k.sample_id, k.openstudio_version, k.inputs_sha256, "
            "k.code_sha256, k.container_digest, k.generation, "
            "e.output_path, e.started_at, e.finished_at, e.exit_code "
            "FROM _osimflow_lookup_many_keys k "
            "LEFT JOIN cache_entries e USING (step, sample_id, "
            "  openstudio_version, inputs_sha256, code_sha256, "
            "  container_digest, generation) "
            "WHERE e.output_path IS NOT NULL"
        )
        c.execute(sql_create)
        try:
            c.executemany(
                sql_insert,
                [
                    (
                        k.step,
                        k.sample_id,
                        k.openstudio_version,
                        k.inputs_sha256,
                        k.code_sha256,
                        k.container_digest,
                        k.generation,
                    )
                    for k in keys
                ],
            )
            rows = c.execute(sql_select).fetchall()
        finally:
            # Always drop the temp table — even on exception — so a stale
            # temp table never leaks into a later batch on this connection.
            c.execute(sql_drop)
        return _row_to_cache_dict(rows)

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

    def note_external_hit(self) -> None:
        """Reclassify the most recent lookup miss as a hit served elsewhere.

        Used by ``DistributedCache`` (issue #993): when a lookup misses the
        local SQLite layer but hits the Redis-backed shared store, the
        wrapper calls this so ``CacheStats`` reflects the campaign's true
        hit rate instead of double-counting the local miss.
        """
        self._stats.misses = max(0, self._stats.misses - 1)
        self._stats.hits += 1
