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

import hashlib
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from .cache import CacheKey, CacheStats, SQLiteCache
from .circuit_breaker import CircuitBreaker

if TYPE_CHECKING:
    import redis as redis_sync
    import redis.asyncio as redis_async


# ---------------------------------------------------------------------------
# Security validation (issue #1277)
# ---------------------------------------------------------------------------

_NONLOCALHOST_BLOCKLIST = ("localhost", "127.0.0.1", "::1", "0.0.0.0")


def _validate_redis_url(redis_url: str, require_auth: bool = False) -> None:
    """Validate that a Redis URL meets the minimum security baseline.

    When ``redis_url`` points to a non-localhost host, the connection must
    either use ``rediss://`` (TLS) or embed credentials
    (``redis://user:pass@host:port``).  The ``require_auth=True`` flag
    allows operators who configure authentication externally (e.g. via
    ``AUTH`` environment variable consumed by the Redis server, not the
    client) to explicitly opt out of the URL-credential check.

    Raises
    ------
    ValueError
        When a non-localhost URL lacks both TLS and embedded credentials
        and ``require_auth`` is False.
    """
    parsed = urlparse(redis_url)
    host = parsed.hostname or ""

    # Single-node localhost is always fine (no network exposure).
    if host in _NONLOCALHOST_BLOCKLIST:
        return

    has_tls = parsed.scheme == "rediss"
    has_creds = bool(parsed.username and parsed.password)

    if has_tls or has_creds or require_auth:
        return

    raise ValueError(
        f"insecure Redis URL (issue #1277): host {host!r} is not localhost "
        f"but the URL uses {parsed.scheme!r} without embedded credentials. "
        f"Non-localhost Redis requires either:\n"
        f"  (a) TLS: rediss://user:pass@{host}:PORT\n"
        f"  (b) credentials in URL: redis://user:pass@{host}:PORT\n"
        f"  (c) --require-redis-auth (set this if Redis auth is handled "
        f"externally, e.g. via an AUTH file or environment variable)."
    )

log = logging.getLogger("osimflow.distributed_cache")

# Lazy import holders — replaced in tests via patch().
_redis_asyncio_module: dict[str, Any] = {}
_redis_sync_module: dict[str, Any] = {}


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


def _private_db_path(db_path: Path) -> Path:
    """Return the process-private variant of ``db_path``.

    ``cache.sqlite`` -> ``cache.p<pid>.sqlite``.  The ``.sqlite`` suffix is
    preserved so artifact-manifest categorisation (which keys on suffix)
    still classifies the file as cache.  Every process gets its own file,
    so concurrent campaign processes never lock the same SQLite database
    (issue #993 / T8.1 reproducer).
    """
    return db_path.with_name(f"{db_path.stem}.p{os.getpid()}{db_path.suffix}")


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
        """
        self.requested_db_path = db_path
        self._local = SQLiteCache(_private_db_path(db_path))
        self._redis_url = redis_url
        self._campaign_id = campaign_id
        self._channel = f"osimflow:cache:invalidate:{campaign_id}"
        self._shared_key = f"osimflow:cache:entries:{campaign_id}"
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
        """Process a received invalidation message against the local cache."""
        action = payload.get("action")
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
        """Publish an invalidation message to Redis (async, non-blocking)."""
        import asyncio  # noqa: PLC0415

        async def _pub() -> None:
            if not self._breaker.allow():
                log.debug("DistributedCache: circuit open, skipping invalidation publish")
                return
            try:
                client = self._get_redis()
                await client.publish(self._channel, json.dumps(payload))
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
        log.debug("DistributedCache closed for campaign=%s", self._campaign_id)

    def __enter__(self) -> DistributedCache:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def __del__(self) -> None:
        self.close()


def build_cache(
    db_path: Path,
    redis_url: str | None,
    campaign_id: str,
    *,
    require_auth: bool = False,
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
    _validate_redis_url(redis_url, require_auth)
    return DistributedCache(
        db_path=db_path,
        redis_url=redis_url,
        campaign_id=campaign_id,
    )
