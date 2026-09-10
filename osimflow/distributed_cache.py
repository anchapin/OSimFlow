"""Distributed cache for multi-node campaigns (issue #330, #993).

Provides cross-node cache coordination via Redis so that Slurm workers
or AWS Batch jobs sharing a campaign see a coherent cache view.

Architecture
------------
``SQLiteCache`` is the single-node persistence layer (issue #993 / T8.2:
SQLite is kept as the default for single-node local mode).  When a
``redis_url`` is configured, ``DistributedCache`` adds two Redis-backed
coordination layers on top of a *process-private* local SQLite file:

1. **Shared entry store** (issue #993, T8.2).  Cache entries that must be
   shared across nodes/processes are written to a Redis hash
   (``osimflow:cache:entries:<namespace>``) in addition to the local
   SQLite file.  ``lookup`` falls back to the shared store on a local
   miss and backfills the local file, so every process converges on the
   same view of completed work without contending on a single SQLite
   database.

2. **Invalidation broadcast** (issue #330).  Every ``invalidate_*`` call
   publishes a pub/sub message so all workers drop the affected entries
   from their local SQLite caches; the shared Redis hash fields are
   deleted directly.

Because each process uses a pid-suffixed local SQLite file
(``<stem>.p<pid>.sqlite``), two concurrent campaign processes
coordinating on the same state never open — and never lock — the same
SQLite file.  This is the fix for the T8.1 SQLite lock reproducer
(fluxion#1790 / OSimFlow#993).

When ``redis_url`` is not configured, ``build_cache`` returns a plain
``SQLiteCache`` — the single-node behaviour is unchanged.

Redis key naming
----------------
Shared entry store (hash)::

    osimflow:cache:entries:<namespace>

Invalidation channel (pub/sub)::

    osimflow:cache:invalidate:<campaign_id>

The namespace is a stable identifier for the campaign's shared state
(see ``campaign_state_namespace``): two processes or nodes targeting
the same ``outdir`` share one namespace, while concurrent campaigns on
different outdirs stay isolated.

Security
--------
Redis credentials are carried in the URL (user:pass@host:port/db).
TLS is supported via the ``rediss://`` scheme.  No credentials are
hardcoded anywhere in the config.

Example URLs::

    redis://localhost:6379/0              # local, no auth
    redis://user:pass@redis.example.com:6379/0  # AUTH
    rediss://user:pass@redis.example.com:6379/0  # AUTH + TLS
"""

from __future__ import annotations

__all__ = ["DistributedCache", "build_cache", "campaign_state_namespace"]

import contextlib
import hashlib
import json
import logging
import os
import ssl
import threading
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from ._sqlite_store import per_pid_path
from .cache import CacheKey, CacheStats, SQLiteCache
from .circuit_breaker import CircuitBreaker

if TYPE_CHECKING:
    import redis as redis_sync
    import redis.asyncio as redis_async


# ---------------------------------------------------------------------------
# Security validation (issue #1277)
# ---------------------------------------------------------------------------

_NONLOCALHOST_BLOCKLIST = ("localhost", "127.0.0.1", "::1", "0.0.0.0")


def validate_redis_url(redis_url: str, require_auth: bool = False) -> None:
    """Validate that a Redis URL meets the minimum security baseline (public).

    The four Redis-backed planes (``build_cache``,
    ``build_document_store``, ``build_job_queue``, and the API rate-limit
    store built by ``osimflow.api.create_app``) all funnel through this
    helper so that the fail-closed posture documented in ADR-0004 and
    issue #1321 stays a single source of truth — a library consumer
    using ``osimflow.build_job_queue`` directly is held to the same
    baseline as the CLI flag, with no opt-out beyond the documented
    ``require_auth`` escape hatch.

    Policy
    ------
    * **Loopback hosts are exempt** — ``localhost``, ``127.0.0.1``,
      ``::1``, and ``0.0.0.0`` never traverse a real network, so TLS and
      embedded credentials are not required for those URLs.  This matches
      the loopback exemptions in ``_validate_storage_endpoint``
      (issue #1386) and ``_validate_coordinator_url`` (issue #1550).
    * **Non-loopback must use TLS** (``rediss://``) — issue #1321.
    * **Non-loopback must carry embedded credentials** unless
      ``require_auth=True`` — issue #1277.  Set ``require_auth=True`` when
      authentication is handled externally (e.g. via an ``AUTH`` file
      consumed by the Redis server, not the client).

    Raises
    ------
    ValueError
        When a non-localhost URL lacks TLS, regardless of ``require_auth``.
        When a non-localhost URL lacks embedded credentials and
        ``require_auth`` is False.
    """
    parsed = urlparse(redis_url)
    host = parsed.hostname or ""

    # Single-node localhost is always fine (no network exposure).
    if host in _NONLOCALHOST_BLOCKLIST:
        return

    has_tls = parsed.scheme == "rediss"
    has_creds = bool(parsed.username and parsed.password)

    # TLS is always required for non-localhost (issue #1321).
    if not has_tls:
        raise ValueError(
            f"insecure Redis URL (issue #1321): host {host!r} is not localhost "
            f"but the URL uses {parsed.scheme!r} without TLS. "
            f"Non-localhost Redis requires TLS (rediss://). "
            f"Set --require-redis-auth only if TLS is handled externally."
        )

    # Credentials are optional when require_auth=True (external auth mechanism).
    if not has_creds and not require_auth:
        raise ValueError(
            f"insecure Redis URL (issue #1277): host {host!r} is not localhost "
            f"but the URL has no embedded credentials. "
            f"Non-localhost Redis requires either:\n"
            f"  (a) credentials in URL: rediss://user:pass@{host}:PORT\n"
            f"  (b) --require-redis-auth (set this if Redis auth is handled "
            f"externally, e.g. via an AUTH file or environment variable)."
        )


log = logging.getLogger("osimflow.distributed_cache")

# Lazy import holders — replaced in tests via patch().
_redis_asyncio_module: dict[str, Any] = {}
_redis_sync_module: dict[str, Any] = {}


# ---------------------------------------------------------------------------
# Stale pid-private file sweep (issue #1691)
# ---------------------------------------------------------------------------


def _pid_alive(pid: int) -> bool:
    """Return True if *pid* is alive on this host (issue #1691).

    Uses ``os.kill(pid, 0)`` — the POSIX-standard "no-op signal" probe
    that returns the same errors as a real signal but never sends one:

    * ``ProcessLookupError`` (POSIX ``ESRCH``) — no such process. The
      pid is dead; safe to sweep.
    * ``PermissionError`` (``EPERM``) — process exists but is owned by
      another user. Conservatively treated as alive: we cannot verify
      ownership of the sibling file, so we leave it alone rather than
      risk deleting an active peer's database.
    * Any other ``OSError`` (network FS hiccup, transient /proc
      unmounted, ...) — also conservatively alive. We refuse to sweep
      unless we can positively prove the pid is dead.

    Returns True for the *current* process's pid without any syscall
    (the caller's own pid is trivially alive by construction).
    """
    if pid == os.getpid():
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but not ours; leave its file alone
    except OSError:
        # Conservatively skip — we can't prove the pid is dead, so we
        # leave the file in place rather than risk deleting an active
        # peer's database.
        return True
    return True


def _parse_pid_from_sibling(sibling: Path, *, stem: str, suffix: str) -> int | None:
    """Extract the pid from a ``<stem>.p<pid>.<suffix>`` filename.

    Returns ``None`` for filenames that don't match the expected shape
    (defensive: never sweep a file whose pid we can't parse). Used by
    :meth:`DistributedCache._sweep_stale_pid_siblings` (issue #1691).
    """
    name = sibling.name
    prefix = f"{stem}.p"
    if not name.startswith(prefix) or not name.endswith(suffix):
        return None
    pid_str = name[len(prefix) : -len(suffix)]
    if not pid_str:
        return None
    try:
        pid = int(pid_str)
    except ValueError:
        return None
    # ``os.getpid()`` is always > 0; a non-positive integer can never
    # be a real pid even though ``int("0")`` parses cleanly.
    if pid <= 0:
        return None
    return pid


# ---------------------------------------------------------------------------
# Redis client (lazy, thread-safe)
# ---------------------------------------------------------------------------


def _get_redis_asyncio() -> Any:
    """Import and return the redis.asyncio module (lazy, cached)."""
    if not _redis_asyncio_module:
        import redis.asyncio as ra  # noqa: PLC0415

        _redis_asyncio_module["module"] = ra
    return _redis_asyncio_module["module"]


def _get_redis_sync() -> Any:
    """Import and return the sync ``redis`` module (lazy, cached).

    The synchronous client is used for the shared entry store data plane
    (HSET/HGET/HDEL) because the campaign code that calls
    ``store``/``lookup`` is itself synchronous (issue #993).  The async
    client remains reserved for the pub/sub subscriber loop.
    """
    if not _redis_sync_module:
        import redis as rs  # noqa: PLC0415

        _redis_sync_module["module"] = rs
    return _redis_sync_module["module"]


def campaign_state_namespace(outdir: Path) -> str:
    """Return a stable Redis namespace for a campaign's shared state.

    Two campaign processes (or nodes on a shared filesystem) targeting the
    same ``outdir`` are coordinating on the same campaign state, so they
    must share one Redis namespace.  A per-run timestamp id would give each
    process a *different* namespace and defeat sharing.  Hashing the
    resolved ``outdir`` keeps same-outdir processes together while
    isolating concurrent campaigns on different outdirs (issue #993).
    """
    digest = hashlib.sha256(str(outdir.resolve()).encode()).hexdigest()[:16]
    return f"outdir-{digest}"


# Issue #1564: pid-private path scheme now lives in
# ``osimflow._sqlite_store.per_pid_path``. The class-level method below
# (``_private_db_path``) is preserved as a thin alias for any third-party
# caller that imports it directly (issue #993 originally added it as a
# module-level helper before issue #1564).
_private_db_path = per_pid_path


def _field_for_key(key: CacheKey) -> str:
    """Encode a CacheKey as a Redis hash field (pipe-delimited)."""
    return "|".join(
        (
            key.step,
            key.sample_id,
            key.openstudio_version,
            key.inputs_sha256,
            key.code_sha256,
            key.container_digest,
            str(key.generation),
        )
    )


class DistributedCache:
    """SQLiteCache wrapper with Redis-backed shared state (issues #330, #993).

    This class is a drop-in replacement for ``SQLiteCache``:

    * ``store`` writes the entry to a Redis **shared entry store** (a hash
      keyed by the campaign's stable namespace) *and* to a process-private
      local SQLite file, so every process/node coordinating on the same
      campaign sees completed work without contending on one SQLite
      database (issue #993 / T8.2).
    * ``lookup`` checks the local SQLite file first (fast path) and falls
      back to the shared store, backfilling the local file on a shared hit.
    * ``invalidate_step`` / ``invalidate_sample`` delete from both the
      shared store and the local file, and broadcast a pub/sub message so
      peers drop their local copies (issue #330).

    If Redis is unreachable, every shared-store operation logs a warning
    and degrades to local-only behaviour — a Redis outage never fails the
    campaign.

    Usage::

        cache = DistributedCache(
            db_path=Path("outdir/work/cache.sqlite"),
            redis_url="redis://localhost:6379/0",
            campaign_id="outdir-1a2b3c4d5e6f7890",
        )
        with cache:
            cache.store(key, output_path, exit_code=0)
            cached = cache.lookup(key)
        # Or simply: cache.close() when done.

    Invalidation broadcast
    ----------------------
    Every call to ``invalidate_step`` or ``invalidate_sample`` publishes a
    JSON message to the Redis channel.  Other workers subscribed to the
    same channel receive the message and call their local
    ``SQLiteCache.invalidate_*``, keeping their local cache in sync.

    Subscriber management
    --------------------
    A background thread runs a blocking Redis subscriber that calls
    ``self._local.invalidate_*`` for each received message.  The subscriber
    is started lazily on the first ``invalidate_*`` call and stopped when
    ``close()`` is called.
    """

    def __init__(
        self,
        db_path: Path,
        redis_url: str,
        campaign_id: str,
        *,
        redis_ssl_context: ssl.SSLContext | None = None,
        unlink_pid_file_on_close: bool = False,
    ) -> None:
        """Initialize the distributed cache.

        Parameters
        ----------
        db_path
            Requested path for the local SQLite database file.  The actual
            per-process file used is a pid-suffixed sibling (see
            ``_private_db_path``) so concurrent processes never lock the
            same database; the shared state lives in Redis.
        redis_url
            Redis connection URL, e.g. ``redis://localhost:6379/0``.
            Supports ``rediss://`` for TLS.  May contain user:pass for
            AUTH.  ``None`` falls back to a plain ``SQLiteCache``.
        campaign_id
            Campaign identifier used for both the shared entry store key
            and the pub/sub channel name.  Pass
            ``campaign_state_namespace(outdir)`` so all processes targeting
            the same ``outdir`` share one namespace.
        redis_ssl_context
            Optional ``ssl.SSLContext`` to use for the Redis connection.
            When provided, it is passed to ``redis.from_url()`` as the
            ``ssl`` argument, enabling custom CA bundle or disabled
            verification for air-gapped deployments (issue #1327).
        unlink_pid_file_on_close
            When ``True`` (``osimflow`` does *not* enable this by default
            to preserve the SQLiteCache-compatible contract that
            ``close()`` leaves the local file in place for lazy re-open),
            :meth:`close` unlinks the pid-private SQLite file (and the
            WAL aux files) at the end of a graceful shutdown so the
            campaign outdir does not leak the multi-megabyte local file.
            The startup sweep (:meth:`_sweep_stale_pid_siblings`) runs
            unconditionally regardless of this flag, so the SIGKILL path
            is always bounded by the next campaign's sweep — the
            ``unlink_pid_file_on_close`` flag only tightens the
            graceful-exit path. Issue #1691.
        """
        self.requested_db_path = db_path
        # Issue #1691: sweep stale pid-private SQLite siblings from prior
        # SIGKILL'd / OOM'd / crashed-otherwise campaigns before opening
        # our own. Each sweep happens before ``SQLiteCache`` opens the
        # current process's file, so the current pid is never on disk
        # yet and is therefore not subject to removal. Runs
        # unconditionally — this is the issue's primary acceptance
        # criterion (covers the SIGKILL restart-by-replay path).
        self._sweep_stale_pid_siblings()
        self._local = SQLiteCache(_private_db_path(db_path))
        self._redis_url = redis_url
        self._campaign_id = campaign_id
        self._redis_ssl_context = redis_ssl_context
        self._unlink_pid_file_on_close = unlink_pid_file_on_close
        self._channel = f"osimflow:cache:invalidate:{campaign_id}"
        self._shared_key = f"osimflow:cache:entries:{campaign_id}"
        # Stable per-process identifier used to drop self-published
        # invalidation broadcasts on the subscriber thread (issue #1563,
        # test ``test_per_sample_fanout_invalidation``). Without this
        # filter the publisher's own broadcast can race past the local
        # rebuild and have the subscriber re-delete the freshly-stored
        # entry.
        self._instance_id = uuid.uuid4().hex
        # Circuit breaker (issue #1111): after repeated consecutive Redis
        # failures, skip the shared data plane entirely for a cooldown
        # period instead of burning a 5 s socket timeout on every op.
        self._breaker = CircuitBreaker(name=f"cache:{campaign_id}")

        # Lazily-created async + sync Redis clients and subscriber thread.
        self._redis_client: redis_async.Redis | None = None
        self._sync_client: redis_sync.Redis | None = None
        self._subscriber_thread: threading.Thread | None = None
        self._stop_subscriber = threading.Event()
        self._sub_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Pid-private file lifecycle (issue #1691)
    # ------------------------------------------------------------------
    def _pid_private_db_path(self) -> Path:
        """Return the path to this process's pid-private local SQLite file.

        Computed from ``requested_db_path`` via :func:`per_pid_path`. Used
        by :meth:`_delete_pid_private_files` to clean up after a graceful
        ``close()``. Exposed as a method (not a stored attribute) so the
        file location stays derived from the single source of truth and
        can't drift from :func:`per_pid_path`'s naming scheme (issue #1691).
        """
        return per_pid_path(self.requested_db_path)

    def _sweep_stale_pid_siblings(self, *, ttl_seconds: float = 0.0) -> int:
        """Unlink ``<stem>.p<pid>.<suffix>`` siblings whose pid is dead (issue #1691).

        Each ``DistributedCache`` opens a pid-suffixed sibling of the
        requested SQLite path (see :func:`per_pid_path`) so concurrent
        campaigns coordinating on the same outdir never lock one
        database. Nothing previously unlinked those files: a clean
        ``close()`` left the local SQLite in place (the Redis layer owns
        the canonical state, so removing the file was considered
        disposable), and a SIGKILL'd process obviously could not run
        ``close()`` at all. Each campaign restart therefore minted a new
        multi-megabyte SQLite file in the outdir forever, bloating
        ``artifact_manifest.json`` (which keys on suffix to classify the
        file as cache).

        This sweep runs on construction, before the *current* process's
        file is opened, so the live pid is never subject to removal:

        1. Glob the requested-db parent for ``<stem>.p*.sqlite`` siblings.
        2. Skip the current pid (trivially alive by construction).
        3. Skip any sibling whose pid is unparseable (defensive: never
           delete a file whose identity we can't confirm).
        4. Probe each remaining pid with ``os.kill(pid, 0)`` via
           :func:`_pid_alive`. A dead pid is the primary sweep trigger;
           the optional ``ttl_seconds`` adds a hard age cap that catches
           the (rare) pid-reuse window before the kernel reuses the
           number for a new process whose file we must not touch.

        Logs the removed-file count at INFO. Returns the number of files
        unlinked (used by tests).

        Parameters
        ----------
        ttl_seconds
            Optional age cap in seconds; non-positive disables it.
            ``0`` (the default) sweeps only by dead-pid check. A future
            CLI flag could thread this through ``build_cache`` for
            operators that want a hard bound independent of pid status.
        """
        parent = self.requested_db_path.parent
        stem = self.requested_db_path.stem
        suffix = self.requested_db_path.suffix
        if not parent.exists():
            return 0
        # Snapshot the glob: the local ``SQLiteCache.__init__`` call below
        # is about to *create* the current pid's file, but we deliberately
        # ran the sweep first so it isn't in the result yet.
        siblings = sorted(parent.glob(f"{stem}.p*.sqlite"))
        if not siblings:
            return 0
        current_pid = os.getpid()
        # ``st_mtime`` is wall-clock based (seconds since epoch), so the
        # TTL comparison must also use ``time.time()`` — ``monotonic()``
        # is undefined relative to ``st_mtime`` and the difference would
        # be a meaningless epoch offset.
        now = time.time() if ttl_seconds > 0 else 0.0
        removed = 0
        for sibling in siblings:
            pid = _parse_pid_from_sibling(sibling, stem=stem, suffix=suffix)
            if pid is None or pid == current_pid:
                continue
            if _pid_alive(pid):
                # Live pid: only sweep if the file is older than the TTL
                # (rare — would mean pid reuse on the same outdir; the
                # operator should investigate).
                if ttl_seconds > 0:
                    try:
                        age_s = now - sibling.stat().st_mtime
                    except OSError:
                        continue
                    if age_s < ttl_seconds:
                        continue
                else:
                    continue
            try:
                sibling.unlink()
            except FileNotFoundError:
                # Raced with a peer sweep — the file is already gone, no-op.
                continue
            except OSError as exc:
                log.warning(
                    "DistributedCache: failed to unlink stale pid sibling %s (pid=%d): %s",
                    sibling,
                    pid,
                    exc,
                )
                continue
            removed += 1
        if removed:
            log.info(
                "DistributedCache: swept %d stale pid-private SQLite file(s) for %s",
                removed,
                self.requested_db_path,
            )
        return removed

    def _delete_pid_private_files(self) -> None:
        """Unlink this process's pid-private SQLite file (and WAL aux).

        Best-effort: called from :meth:`close` so a graceful shutdown
        removes the multi-megabyte local cache file the SIGKILL path
        previously leaked. WAL aux files (``-wal`` and ``-shm``) are
        produced by SQLite in WAL mode (see :func:`osimflow._sqlite_store.connect`)
        and are unlinked alongside the main file so the outdir is clean.

        Idempotent (a missing file is silently skipped). The current
        process has already closed its SQLite connection via
        ``self._local.close()`` before this runs, so there is no risk
        of unlinking an open database.
        """
        db_path = self._pid_private_db_path()
        for candidate in (
            db_path,
            db_path.with_suffix(db_path.suffix + "-wal"),
            db_path.with_suffix(db_path.suffix + "-shm"),
        ):
            try:
                candidate.unlink()
            except FileNotFoundError:
                continue
            except OSError as exc:
                log.warning(
                    "DistributedCache: failed to unlink pid-private file %s: %s",
                    candidate,
                    exc,
                )

    # ------------------------------------------------------------------
    # Sync Redis client for the shared entry store (lazy, thread-safe)
    # ------------------------------------------------------------------
    def _get_sync_client(self) -> Any:
        """Lazily create the sync Redis client used by the shared store.

        Socket timeouts bound every call so a hung Redis degrades to
        local-only behaviour within seconds instead of stalling the
        campaign.
        """
        if self._sync_client is None:
            redis_sync = _get_redis_sync()
            self._sync_client = redis_sync.from_url(
                self._redis_url,
                decode_responses=True,
                socket_timeout=5.0,
                socket_connect_timeout=5.0,
                ssl=self._redis_ssl_context,
            )
        return self._sync_client

    # ------------------------------------------------------------------
    # Shared entry store data plane (issue #993, T8.2)
    # ------------------------------------------------------------------
    def _shared_store(self, key: CacheKey, output_path: Path, exit_code: int) -> None:
        """Write one entry to the Redis shared store (never raises)."""
        if not self._breaker.allow():
            # Circuit open (issue #1111): skip silently — the campaign is
            # already operating local-only until the cooldown elapses.
            log.debug("DistributedCache: circuit open, skipping shared store")
            return
        entry = json.dumps(
            {
                "output_path": str(output_path),
                "exit_code": exit_code,
                "finished_at": time.time(),
            }
        )
        try:
            self._get_sync_client().hset(self._shared_key, _field_for_key(key), entry)
        except Exception as exc:
            self._breaker.record_failure()
            log.warning(
                "DistributedCache: failed to share entry campaign=%s key=%s: %s"
                " — continuing local-only (circuit failures: %d)",
                self._campaign_id,
                _field_for_key(key),
                exc,
                self._breaker.consecutive_failures,
            )
        else:
            self._breaker.record_success()

    def _decode_shared_entry(self, raw: str | None, key: CacheKey) -> Path | None:
        """Decode a shared-store entry into an output path, or None."""
        if raw is None:
            return None
        try:
            entry = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            log.warning(
                "DistributedCache: corrupt shared entry for key=%s — treating as miss",
                _field_for_key(key),
            )
            return None
        if int(entry.get("exit_code", -1)) != 0:
            return None
        out = Path(str(entry["output_path"]))
        if not out.exists():
            # Stale shared entry: the output file was deleted (e.g. the
            # producing node's scratch dir went away).
            log.warning("shared cache hit but output missing on disk: %s", out)
            return None
        return out

    def _shared_lookup(self, key: CacheKey) -> Path | None:
        """Look up one entry in the Redis shared store (never raises)."""
        if not self._breaker.allow():
            log.debug("DistributedCache: circuit open, skipping shared lookup")
            return None
        try:
            raw = self._get_sync_client().hget(self._shared_key, _field_for_key(key))
        except Exception as exc:
            self._breaker.record_failure()
            log.warning(
                "DistributedCache: shared lookup failed campaign=%s: %s — continuing local-only"
                " (circuit failures: %d)",
                self._campaign_id,
                exc,
                self._breaker.consecutive_failures,
            )
            return None
        self._breaker.record_success()
        return self._decode_shared_entry(raw, key)

    def _shared_invalidate(self, pattern: str) -> None:
        """Delete every shared-store hash field matching ``pattern`` (never raises)."""
        if not self._breaker.allow():
            log.debug("DistributedCache: circuit open, skipping shared invalidate")
            return
        try:
            client = self._get_sync_client()
            fields = [field for field, _ in client.hscan_iter(self._shared_key, match=pattern)]
            if fields:
                client.hdel(self._shared_key, *fields)
        except Exception as exc:
            self._breaker.record_failure()
            log.warning(
                "DistributedCache: shared invalidate failed campaign=%s pattern=%s: %s"
                " — continuing local-only (circuit failures: %d)",
                self._campaign_id,
                pattern,
                exc,
                self._breaker.consecutive_failures,
            )
        else:
            self._breaker.record_success()

    # ------------------------------------------------------------------
    # Redis client (lazy, thread-safe)
    # ------------------------------------------------------------------
    def _get_redis(self) -> Any:
        """Lazily create the async Redis client."""
        if self._redis_client is None:
            redis_async = _get_redis_asyncio()
            self._redis_client = redis_async.from_url(
                self._redis_url,
                encoding="utf-8",
                decode_responses=True,
                ssl=self._redis_ssl_context,
            )
        return self._redis_client

    # ------------------------------------------------------------------
    # Subscriber thread with auto-recovery (issue #443)
    # ------------------------------------------------------------------
    def _start_subscriber(self) -> None:
        """Start the background subscriber thread with auto-recovery (idempotent)."""
        if self._subscriber_thread is not None:
            return

        def _run() -> None:
            import asyncio  # noqa: PLC0415

            redis_async = _get_redis_asyncio()

            async def _main() -> None:
                reconnect_delay = 1.0
                max_reconnect_delay = 60.0

                while not self._stop_subscriber.is_set():
                    client = redis_async.from_url(
                        self._redis_url,
                        encoding="utf-8",
                        decode_responses=True,
                        ssl=self._redis_ssl_context,
                    )
                    try:
                        log.info(
                            "DistributedCache subscriber started for campaign=%s channel=%s",
                            self._campaign_id,
                            self._channel,
                        )
                        async with client.pubsub() as pubsub:
                            await pubsub.subscribe(self._channel)
                            # Reset reconnect delay on successful subscription.
                            reconnect_delay = 1.0
                            while not self._stop_subscriber.is_set():
                                msg = await pubsub.get_message(
                                    timeout=1.0,
                                    ignore_subscribe_messages=True,
                                )
                                if msg is None:
                                    continue
                                data = msg.get("data")
                                if data is None:
                                    continue
                                try:
                                    payload = json.loads(data)
                                except (json.JSONDecodeError, TypeError):
                                    log.warning(
                                        "DistributedCache: received non-JSON message: %r",
                                        data,
                                    )
                                    continue
                                self._handle_invalidation(payload)
                    except Exception as exc:
                        if self._stop_subscriber.is_set():
                            break
                        log.warning(
                            "DistributedCache subscriber error (campaign=%s): %s — reconnecting in %.1fs",
                            self._campaign_id,
                            exc,
                            reconnect_delay,
                        )
                        await client.aclose()
                        await asyncio.sleep(reconnect_delay)
                        reconnect_delay = min(reconnect_delay * 2, max_reconnect_delay)
                        continue
                    finally:
                        if not self._stop_subscriber.is_set():
                            await client.aclose()
                        log.info(
                            "DistributedCache subscriber stopped for campaign=%s",
                            self._campaign_id,
                        )

            asyncio.run(_main())

        t = threading.Thread(
            target=_run, name=f"osimflow-cache-subscriber-{self._campaign_id}", daemon=True
        )
        t.start()
        self._subscriber_thread = t

    def _handle_invalidation(self, payload: dict[str, Any]) -> None:
        """Process a received invalidation message against the local cache.

        Self-published broadcasts are dropped: every ``invalidate_*`` call
        applies the local DELETE synchronously *before* publishing, so the
        subscriber side of this same process must not re-apply it on
        arrival — otherwise the subscriber can race past a subsequent
        local rebuild and delete a freshly-stored entry (issue #1563,
        ``test_per_sample_fanout_invalidation``).
        """
        action = payload.get("action")
        originator = payload.get("instance_id")
        if originator is not None and originator == self._instance_id:
            log.debug(
                "DistributedCache: dropping self-published invalidation action=%s (instance_id=%s)",
                action,
                originator,
            )
            return
        try:
            if action == "invalidate_step":
                step = payload.get("step")
                if step:
                    n = self._local.invalidate_step(step)
                    log.info(
                        "DistributedCache: received invalidate_step step=%s (%d rows)",
                        step,
                        n,
                    )
            elif action == "invalidate_sample":
                step = payload.get("step")
                sample_id = payload.get("sample_id")
                if step and sample_id:
                    n = self._local.invalidate_sample(step, sample_id)
                    log.info(
                        "DistributedCache: received invalidate_sample step=%s sample=%s (%d rows)",
                        step,
                        sample_id,
                        n,
                    )
            else:
                log.warning(
                    "DistributedCache: unknown action %r in invalidation message",
                    action,
                )
        except Exception as exc:
            log.warning(
                "DistributedCache: error handling invalidation payload=%s: %s",
                payload,
                exc,
            )

    def _publish(self, payload: dict[str, Any]) -> None:
        """Publish an invalidation message to Redis (async, non-blocking).

        Every published payload carries this instance's ``instance_id`` so
        the subscriber thread can drop the publisher's own broadcast on
        arrival (the local SQLite side of the invalidation has already
        been applied synchronously before publishing; re-applying it
        asynchronously would race past a subsequent local rebuild and
        delete freshly-stored entries — issue #1563).
        """
        enriched = dict(payload)
        enriched["instance_id"] = self._instance_id
        import asyncio  # noqa: PLC0415

        async def _pub() -> None:
            if not self._breaker.allow():
                log.debug("DistributedCache: circuit open, skipping invalidation publish")
                return
            try:
                client = self._get_redis()
                await client.publish(self._channel, json.dumps(enriched))
            except Exception as exc:
                self._breaker.record_failure()
                log.warning(
                    "DistributedCache: failed to publish invalidation for campaign=%s: %s"
                    " (circuit failures: %d)",
                    self._campaign_id,
                    exc,
                    self._breaker.consecutive_failures,
                )
            else:
                self._breaker.record_success()

        try:
            asyncio.get_running_loop()
            # Already in an async context — create a task (non-blocking).
            asyncio.create_task(_pub())
        except RuntimeError:
            # Sync context — run the coroutine in a background thread.
            def _run() -> None:
                asyncio.run(_pub())

            t = threading.Thread(target=_run, daemon=True)
            t.start()

    # ------------------------------------------------------------------
    # Public cache interface (same as SQLiteCache)
    # ------------------------------------------------------------------
    def lookup(self, key: CacheKey) -> Path | None:
        """Return the cached output path if this exact key is present and successful.

        Checks the process-local SQLite file first (fast path), then the
        Redis shared store (issue #993).  A shared hit is backfilled into
        the local file so subsequent lookups are local-only.
        """
        local = self._local.lookup(key)
        if local is not None:
            return local
        shared = self._shared_lookup(key)
        if shared is None:
            return None
        # Backfill the local file so the next lookup for this key is a
        # local fast-path hit, and keep CacheStats honest (the local
        # lookup above registered a miss; the shared store served it).
        self._local.store(key, shared, exit_code=0)
        self._local.note_external_hit()
        log.info(
            "shared cache HIT  step=%s sample=%s -> %s",
            key.step,
            key.sample_id,
            shared,
        )
        return shared

    def store(self, key: CacheKey, output_path: Path, exit_code: int) -> None:
        """Store a cache entry locally and in the Redis shared store."""
        self._local.store(key, output_path, exit_code)
        self._shared_store(key, output_path, exit_code)

    def invalidate_step(self, step: str) -> int:
        """Drop every entry for a given step locally + in Redis, and broadcast."""
        # Ensure subscriber is running so we receive our own broadcasts
        # (for consistency when multiple workers share the same campaign).
        with self._sub_lock:
            if self._subscriber_thread is None:
                self._start_subscriber()
        n = self._local.invalidate_step(step)
        self._shared_invalidate(f"{step}|*")
        self._publish({"action": "invalidate_step", "step": step})
        return n

    def invalidate_sample(self, step: str, sample_id: str) -> int:
        """Drop a specific (step, sample) entry locally + in Redis, and broadcast."""
        with self._sub_lock:
            if self._subscriber_thread is None:
                self._start_subscriber()
        n = self._local.invalidate_sample(step, sample_id)
        self._shared_invalidate(f"{step}|{sample_id}|*")
        self._publish({"action": "invalidate_sample", "step": step, "sample_id": sample_id})
        return n

    def stats(self) -> dict[str, Any]:
        """Return cache statistics from the local SQLite cache."""
        return self._local.stats()

    def get_stats(self) -> CacheStats:
        """Return ``CacheStats`` for this process's local view of the cache.

        Delegates to the local SQLite layer. Stats reflect the entries
        this process stored or looked up (shared hits are backfilled
        locally first, so they are counted here too). Part of the
        ``SQLiteCache`` drop-in contract used by ``Campaign.warm_cache``.
        """
        return self._local.get_stats()

    @property
    def breaker_state(self) -> str:
        """Current circuit breaker state (issue #1310)."""
        return self._breaker.state

    def close(self) -> None:
        """Stop the subscriber thread, close Redis clients, close the local cache."""
        # Signal the subscriber to stop.
        self._stop_subscriber.set()
        if self._subscriber_thread is not None:
            self._subscriber_thread.join(timeout=5.0)
            self._subscriber_thread = None

        # Close the sync shared-store client.
        if self._sync_client is not None:
            try:
                self._sync_client.close()
            except Exception as exc:
                log.warning(
                    "DistributedCache: error closing sync client for campaign=%s: %s",
                    self._campaign_id,
                    exc,
                )
            self._sync_client = None

        # Close the async pub/sub client.
        if self._redis_client is not None:
            import asyncio  # noqa: PLC0415

            async def _close() -> None:
                await self._redis_client.aclose()  # type: ignore[union-attr]

            try:
                asyncio.get_running_loop()
                asyncio.create_task(_close())
            except RuntimeError:
                asyncio.run(_close())
            self._redis_client = None

        # Close the local SQLite cache.
        self._local.close()
        # Issue #1691: when the operator opted in to strict cleanup,
        # unlink the pid-private SQLite file (and its WAL aux) so a
        # clean shutdown does not leak the multi-megabyte local file
        # into the campaign outdir. Best-effort: a peer sweep racing
        # us, a missing file, or a permission error is logged and
        # swallowed — the Redis layer is the source of truth and the
        # file is recoverable from Redis via the backfill path. When
        # ``unlink_pid_file_on_close`` is False (the default) the file
        # is preserved across ``close()`` so post-close ``lookup()``
        # etc. transparently re-open it via ``SQLiteCache``'s lazy
        # reconnect — matches the historical SQLiteCache contract.
        if self._unlink_pid_file_on_close:
            self._delete_pid_private_files()
        log.debug("DistributedCache closed for campaign=%s", self._campaign_id)

    def __enter__(self) -> DistributedCache:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def __del__(self) -> None:
        # Defensive: during interpreter shutdown ``sys.meta_path`` is
        # already torn down (returns ``None``), and ``pathlib.Path``
        # operations in :meth:`_pid_private_db_path` raise
        # ``ImportError`` in that state. The file unlink is best-effort
        # — the next campaign's startup sweep will collect whatever
        # ``__del__`` couldn't — so swallow every exception and let the
        # interpreter exit cleanly (issue #1691).
        with contextlib.suppress(BaseException):
            self.close()


def build_cache(
    db_path: Path,
    redis_url: str | None,
    campaign_id: str,
    *,
    require_auth: bool = False,
    redis_ssl_context: ssl.SSLContext | None = None,
) -> SQLiteCache | DistributedCache:
    """Factory: build the appropriate cache from configuration.

    When ``redis_url`` is ``None``, returns a plain ``SQLiteCache`` at
    ``db_path`` — the single-node default, unchanged (issue #993 keeps
    SQLite for single-node local mode).  When a Redis URL is provided,
    returns a ``DistributedCache`` whose shared cache entries live in a
    Redis hash under ``campaign_id`` and whose local SQLite file is
    process-private, so concurrent processes coordinating on the same
    campaign never contend on one SQLite database.

    Parameters
    ----------
    db_path
        Path to the local SQLite database file.  Used directly by the
        single-node ``SQLiteCache``; the ``DistributedCache`` derives a
        pid-suffixed sibling for its process-private local layer.
    redis_url
        Redis connection URL (e.g. ``redis://localhost:6379/0``).
        ``None`` disables the distributed cache.
    campaign_id
        Stable campaign namespace (see ``campaign_state_namespace``).
        Used for both the shared entry store key and the pub/sub channel
        so concurrent campaigns are isolated.
    require_auth
        When True, skips the URL-level credential check.  Set this when
        Redis authentication is handled externally (e.g. via an ``AUTH``
        file consumed by the Redis server, not the client).  Issue #1277.
    redis_ssl_context
        Optional ``ssl.SSLContext`` to pass to ``redis.from_url()`` for
        custom CA bundles or disabled verification (issue #1327).

    Returns
    -------
    SQLiteCache | DistributedCache
        The concrete cache instance.

    Raises
    ------
    ValueError
        When a non-localhost Redis URL lacks both TLS (``rediss://``)
        and embedded credentials and ``require_auth`` is False (issue #1277).
    """
    if redis_url is None:
        return SQLiteCache(db_path)
    validate_redis_url(redis_url, require_auth)
    return DistributedCache(
        db_path=db_path,
        redis_url=redis_url,
        campaign_id=campaign_id,
        redis_ssl_context=redis_ssl_context,
    )
