"""Distributed cache for multi-node campaigns (issue #330).

Provides cross-node cache invalidation via Redis pub/sub so that
Slurm workers or AWS Batch jobs can share a coherent cache view.

Architecture
------------
SQLiteCache is the local persistence layer (single-node).  DistributedCache
adds a Redis pub/sub broadcast on every ``invalidate_*`` call so that all
workers invalidate their local SQLite cache when any one worker invalidates.

When ``redis_url`` is not configured, ``build_cache`` returns a plain
SQLiteCache — the single-node behaviour is unchanged.

Redis channel naming
--------------------
``osimflow:cache:invalidate:<campaign_id>``

The campaign_id is the run's unique identifier (from run.json), which
keeps multiple concurrent campaigns isolated.

Message format (JSON)
-------------------
Invalidate step::

    {"action": "invalidate_step", "step": "RUN_OPENSTUDIO_SIM"}

Invalidate sample::

    {"action": "invalidate_sample", "step": "APPLY_PARAMETERS", "sample_id": "s0001"}

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

import json
import logging
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .cache import CacheKey, SQLiteCache

if TYPE_CHECKING:
    import redis.asyncio as redis_async

log = logging.getLogger("osimflow.distributed_cache")

# Lazy import holder — replaced in tests via patch().
_redis_asyncio_module: dict[str, Any] = {}


def _get_redis_asyncio() -> Any:
    """Import and return the redis.asyncio module (lazy, cached)."""
    if not _redis_asyncio_module:
        import redis.asyncio as ra
        _redis_asyncio_module["module"] = ra
    return _redis_asyncio_module["module"]


class DistributedCache:
    """SQLiteCache wrapper with Redis pub/sub for cross-node invalidation.

    This class is a drop-in replacement for ``SQLiteCache``.  All cache
    operations are local (SQLite); invalidation events are broadcast via
    Redis so that all workers in a multi-node campaign see a coherent
    cache state.

    Usage::

        cache = DistributedCache(
            db_path=Path("outdir/work/cache.sqlite"),
            redis_url="redis://localhost:6379/0",
            campaign_id="2025-01-01T12-00-00",
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
            Path to the local SQLite database file.  Passed directly to
            ``SQLiteCache``.
        redis_url
            Redis connection URL, e.g. ``redis://localhost:6379/0``.
            Supports ``rediss://`` for TLS.  May contain user:pass for
            AUTH.  ``None`` falls back to a plain ``SQLiteCache``.
        campaign_id
            Unique campaign identifier.  Used as the Redis pub/sub
            channel name suffix so concurrent campaigns are isolated.
        """
        self._local = SQLiteCache(db_path)
        self._redis_url = redis_url
        self._campaign_id = campaign_id
        self._channel = f"osimflow:cache:invalidate:{campaign_id}"

        # Lazily-created async Redis client and subscriber thread.
        self._redis_client: redis_async.Redis | None = None
        self._subscriber_thread: threading.Thread | None = None
        self._stop_subscriber = threading.Event()
        self._sub_lock = threading.Lock()

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
    # Subscriber thread
    # ------------------------------------------------------------------
    def _start_subscriber(self) -> None:
        """Start the background subscriber thread (idempotent)."""
        if self._subscriber_thread is not None:
            return

        def _run() -> None:
            import asyncio

            redis_async = _get_redis_asyncio()

            async def _main() -> None:
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
                    log.warning(
                        "DistributedCache subscriber error (campaign=%s): %s — re-connecting",
                        self._campaign_id,
                        exc,
                    )
                finally:
                    await client.aclose()
                    log.info(
                        "DistributedCache subscriber stopped for campaign=%s",
                        self._campaign_id,
                    )

            asyncio.run(_main())

        t = threading.Thread(target=_run, name=f"osimflow-subscriber-{self._campaign_id}", daemon=True)
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
        import asyncio

        async def _pub() -> None:
            try:
                client = self._get_redis()
                await client.publish(self._channel, json.dumps(payload))
            except Exception as exc:
                log.warning(
                    "DistributedCache: failed to publish invalidation for campaign=%s: %s",
                    self._campaign_id,
                    exc,
                )

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
        """Return the cached output path if this exact key is present and successful."""
        return self._local.lookup(key)

    def store(self, key: CacheKey, output_path: Path, exit_code: int) -> None:
        """Store a cache entry locally (no Redis broadcast for store)."""
        self._local.store(key, output_path, exit_code)

    def invalidate_step(self, step: str) -> int:
        """Drop every entry for a given step locally and broadcast to Redis."""
        # Ensure subscriber is running so we receive our own broadcasts
        # (for consistency when multiple workers share the same campaign).
        with self._sub_lock:
            if self._subscriber_thread is None:
                self._start_subscriber()
        n = self._local.invalidate_step(step)
        self._publish({"action": "invalidate_step", "step": step})
        return n

    def invalidate_sample(self, step: str, sample_id: str) -> int:
        """Drop a specific (step, sample) entry locally and broadcast to Redis."""
        with self._sub_lock:
            if self._subscriber_thread is None:
                self._start_subscriber()
        n = self._local.invalidate_sample(step, sample_id)
        self._publish({"action": "invalidate_sample", "step": step, "sample_id": sample_id})
        return n

    def stats(self) -> dict[str, Any]:
        """Return cache statistics from the local SQLite cache."""
        return self._local.stats()

    def close(self) -> None:
        """Stop the subscriber thread and close the local SQLite cache."""
        # Signal the subscriber to stop.
        self._stop_subscriber.set()
        if self._subscriber_thread is not None:
            self._subscriber_thread.join(timeout=5.0)
            self._subscriber_thread = None

        # Close the Redis client.
        if self._redis_client is not None:
            import asyncio

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
) -> SQLiteCache | DistributedCache:
    """Factory: build the appropriate cache from configuration.

    When ``redis_url`` is ``None``, returns a plain ``SQLiteCache`` (single-node
    behaviour).  When a Redis URL is provided, returns a ``DistributedCache``
    that broadcasts invalidation events to all workers in a multi-node campaign.

    Parameters
    ----------
    db_path
        Path to the local SQLite database file.
    redis_url
        Redis connection URL (e.g. ``redis://localhost:6379/0``).
        ``None`` disables distributed cache.
    campaign_id
        Unique campaign identifier.  Used as the Redis pub/sub channel
        suffix so concurrent campaigns are isolated.

    Returns
    -------
    SQLiteCache | DistributedCache
        The concrete cache instance.
    """
    if redis_url is None:
        return SQLiteCache(db_path)
    return DistributedCache(
        db_path=db_path,
        redis_url=redis_url,
        campaign_id=campaign_id,
    )
