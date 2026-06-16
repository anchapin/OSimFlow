"""Distributed job queue for multi-node campaigns (issue #393).

Provides cross-node job coordination via Redis pub/sub so that
Slurm workers or AWS Batch jobs can share a coherent job queue view.

Architecture
------------
JobQueue is the local persistence layer (single-process crash recovery).
DistributedJobQueue adds a Redis pub/sub broadcast on every state-changing
operation so that all workers see a coherent job state.

When ``redis_url`` is not configured, ``build_job_queue`` returns a plain
JobQueue — the single-process behaviour is unchanged.

Redis channel naming
--------------------
``osimflow:jobqueue:<campaign_id>``

The campaign_id is the run's unique identifier (from run.json), which
keeps multiple concurrent campaigns isolated.

Message format (JSON)
--------------------
Enqueue::

    {"action": "enqueue", "job_id": "sample_0_RUN_OPENSTUDIO_SIM", "payload": {...}}

Dequeue::

    {"action": "dequeue", "job_id": "sample_0_RUN_OPENSTUDIO_SIM"}

Mark completed::

    {"action": "mark_completed", "job_id": "sample_0_RUN_OPENSTUDIO_SIM"}

Mark failed::

    {"action": "mark_failed", "job_id": "sample_0_RUN_OPENSTUDIO_SIM", "error": "..."}

Recover::

    {"action": "recover"}

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

from .jobqueue import JobQueue

if TYPE_CHECKING:
    import redis.asyncio as redis_async

log = logging.getLogger("osimflow.distributed_jobqueue")

# Lazy import holder — replaced in tests via patch().
_redis_asyncio_module: dict[str, Any] = {}


def _get_redis_asyncio() -> Any:
    """Import and return the redis.asyncio module (lazy, cached)."""
    if not _redis_asyncio_module:
        import redis.asyncio as ra  # noqa: PLC0415

        _redis_asyncio_module["module"] = ra
    return _redis_asyncio_module["module"]


class DistributedJobQueue:
    """JobQueue wrapper with Redis pub/sub for cross-node job coordination.

    This class is a drop-in replacement for ``JobQueue``.  All job queue
    operations are local (filesystem); state-change events are broadcast via
    Redis so that all workers in a multi-node campaign see a coherent
    job state.

    Usage::

        queue = DistributedJobQueue(
            queue_dir=Path("outdir/work/queue"),
            redis_url="redis://localhost:6379/0",
            campaign_id="2025-01-01T12-00-00",
        )
        queue.enqueue("sample_0_SIM", {"sample_id": "s0", "step": "SIM"})
        queue.mark_completed("sample_0_SIM")
        # Or simply: queue.close() when done.

    Coordination broadcast
    ----------------------
    Every state-changing operation (enqueue, mark_completed, mark_failed,
    recover) publishes a JSON message to the Redis channel.  Other workers
    subscribed to the same channel receive the message and apply the same
    operation to their local JobQueue, keeping their local queue in sync.

    Subscriber management
    --------------------
    A background thread runs a blocking Redis subscriber that calls
    ``self._local.*`` for each received message.  The subscriber is started
    lazily on the first state-changing call and stopped when ``close()`` is
    called.
    """

    def __init__(
        self,
        queue_dir: Path,
        redis_url: str,
        campaign_id: str,
    ) -> None:
        """Initialize the distributed job queue.

        Parameters
        ----------
        queue_dir
            Path to the queue root directory.  Passed directly to
            ``JobQueue``.  Typically ``{outdir}/work/queue``.
        redis_url
            Redis connection URL, e.g. ``redis://localhost:6379/0``.
            Supports ``rediss://`` for TLS.  May contain user:pass for
            AUTH.  ``None`` falls back to a plain ``JobQueue``.
        campaign_id
            Unique campaign identifier.  Used as the Redis pub/sub
            channel name suffix so concurrent campaigns are isolated.
        """
        self._local = JobQueue(queue_dir)
        self._redis_url = redis_url
        self._campaign_id = campaign_id
        self._channel = f"osimflow:jobqueue:{campaign_id}"

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
            import asyncio  # noqa: PLC0415

            redis_async = _get_redis_asyncio()

            async def _main() -> None:
                client = redis_async.from_url(
                    self._redis_url,
                    encoding="utf-8",
                    decode_responses=True,
                )
                try:
                    log.info(
                        "DistributedJobQueue subscriber started for campaign=%s channel=%s",
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
                                    "DistributedJobQueue: received non-JSON message: %r",
                                    data,
                                )
                                continue
                            self._handle_action(payload)
                except Exception as exc:
                    log.warning(
                        "DistributedJobQueue subscriber error (campaign=%s): %s — re-connecting",
                        self._campaign_id,
                        exc,
                    )
                finally:
                    await client.aclose()
                    log.info(
                        "DistributedJobQueue subscriber stopped for campaign=%s",
                        self._campaign_id,
                    )

            asyncio.run(_main())

        t = threading.Thread(
            target=_run, name=f"osimflow-jobqueue-subscriber-{self._campaign_id}", daemon=True
        )
        t.start()
        self._subscriber_thread = t

    def _handle_action(self, payload: dict[str, Any]) -> None:
        """Process a received action message against the local queue."""
        action = payload.get("action")
        try:
            if action == "enqueue":
                job_id = payload.get("job_id")
                job_payload = payload.get("payload", {})
                priority = payload.get("priority", 0)
                if job_id:
                    self._local.enqueue(job_id, job_payload, priority=priority)
                    log.info(
                        "DistributedJobQueue: received enqueue job_id=%s priority=%d",
                        job_id,
                        priority,
                    )
            elif action == "mark_completed":
                job_id = payload.get("job_id")
                if job_id:
                    self._local.mark_completed(job_id)
                    log.info(
                        "DistributedJobQueue: received mark_completed job_id=%s",
                        job_id,
                    )
            elif action == "mark_failed":
                job_id = payload.get("job_id")
                error = payload.get("error", "")
                if job_id:
                    self._local.mark_failed(job_id, error)
                    log.info(
                        "DistributedJobQueue: received mark_failed job_id=%s",
                        job_id,
                    )
            elif action == "recover":
                recovered = self._local.recover()
                log.info(
                    "DistributedJobQueue: received recover (%d jobs reset)",
                    len(recovered),
                )
            else:
                log.warning(
                    "DistributedJobQueue: unknown action %r in message",
                    action,
                )
        except Exception as exc:
            log.warning(
                "DistributedJobQueue: error handling message payload=%s: %s",
                payload,
                exc,
            )

    def _publish(self, payload: dict[str, Any]) -> None:
        """Publish an action message to Redis (async, non-blocking)."""
        import asyncio  # noqa: PLC0415

        async def _pub() -> None:
            try:
                client = self._get_redis()
                await client.publish(self._channel, json.dumps(payload))
            except Exception as exc:
                log.warning(
                    "DistributedJobQueue: failed to publish action for campaign=%s: %s",
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
    # Public queue interface (same as JobQueue)
    # ------------------------------------------------------------------
    def enqueue(self, job_id: str, payload: dict[str, Any], priority: int = 0) -> Path:
        """Enqueue a job locally and broadcast to Redis."""
        with self._sub_lock:
            if self._subscriber_thread is None:
                self._start_subscriber()
        result = self._local.enqueue(job_id, payload, priority=priority)
        self._publish(
            {"action": "enqueue", "job_id": job_id, "payload": payload, "priority": priority}
        )
        return result

    def dequeue(self) -> dict[str, Any] | None:
        """Dequeue a job locally (no Redis broadcast for dequeue).

        Note: dequeue is a local-only operation in this implementation.
        Each worker dequeues independently from its local queue.
        """
        return self._local.dequeue()

    def mark_completed(self, job_id: str) -> None:
        """Mark a job completed locally and broadcast to Redis."""
        with self._sub_lock:
            if self._subscriber_thread is None:
                self._start_subscriber()
        self._local.mark_completed(job_id)
        self._publish({"action": "mark_completed", "job_id": job_id})

    def mark_failed(self, job_id: str, error: str) -> None:
        """Mark a job failed locally and broadcast to Redis."""
        with self._sub_lock:
            if self._subscriber_thread is None:
                self._start_subscriber()
        self._local.mark_failed(job_id, error)
        self._publish({"action": "mark_failed", "job_id": job_id, "error": error})

    def pending_jobs(self) -> list[dict[str, Any]]:
        """List all pending jobs (local only)."""
        return self._local.pending_jobs()

    def recover(self) -> list[dict[str, Any]]:
        """Recover in-flight jobs locally and broadcast to Redis."""
        with self._sub_lock:
            if self._subscriber_thread is None:
                self._start_subscriber()
        result = self._local.recover()
        self._publish({"action": "recover"})
        return result

    def jobs_by_state(self, state: str) -> list[dict[str, Any]]:
        """List all jobs in a given state (local only)."""
        return self._local.jobs_by_state(state)

    def job_count(self) -> dict[str, int]:
        """Return a count of jobs in each state (local only)."""
        return self._local.job_count()

    def has_pending(self) -> bool:
        """Return ``True`` if there are any pending jobs (local only)."""
        return self._local.has_pending()

    def close(self) -> None:
        """Stop the subscriber thread and close the local queue."""
        # Signal the subscriber to stop.
        self._stop_subscriber.set()
        if self._subscriber_thread is not None:
            self._subscriber_thread.join(timeout=5.0)
            self._subscriber_thread = None

        # Close the Redis client.
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

        log.debug("DistributedJobQueue closed for campaign=%s", self._campaign_id)

    def __enter__(self) -> DistributedJobQueue:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def __del__(self) -> None:
        self.close()


def build_job_queue(
    queue_dir: Path,
    redis_url: str | None,
    campaign_id: str,
) -> JobQueue | DistributedJobQueue:
    """Factory: build the appropriate job queue from configuration.

    When ``redis_url`` is ``None``, returns a plain ``JobQueue`` (single-process
    behaviour).  When a Redis URL is provided, returns a ``DistributedJobQueue``
    that broadcasts job state changes to all workers in a multi-node campaign.

    Parameters
    ----------
    queue_dir
        Path to the queue root directory.  Passed directly to ``JobQueue``.
        Typically ``{outdir}/work/queue``.
    redis_url
        Redis connection URL (e.g. ``redis://localhost:6379/0``).
        ``None`` disables distributed job queue.
    campaign_id
        Unique campaign identifier.  Used as the Redis pub/sub channel
        suffix so concurrent campaigns are isolated.

    Returns
    -------
    JobQueue | DistributedJobQueue
        The concrete queue instance.
    """
    if redis_url is None:
        return JobQueue(queue_dir)
    return DistributedJobQueue(
        queue_dir=queue_dir,
        redis_url=redis_url,
        campaign_id=campaign_id,
    )
