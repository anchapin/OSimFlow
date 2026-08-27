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

Per-sample channel design (issue #1219)
---------------------------------------
Rather than broadcasting every state change to every worker (which creates
an O(N) fan-out bottleneck at campaign scale), each job is published to a
per-sample channel derived from its ``job_id``.  Workers are configured with
the set of sample IDs they own; the subscriber only processes messages
for those samples, dropping all others at the application layer without
touching the local queue.

Channel naming
~~~~~~~~~~~~~~
``osimflow:jobqueue:<campaign_id>:<sample_id>``

where ``sample_id`` is the sample portion of the ``job_id`` extracted via
:meth:`_extract_sample_id`.  For example, ``sample_0_RUN_OPENSTUDIO_SIM``
yields channel ``osimflow:jobqueue:<campaign_id>:sample_0``.

Worker subscription
~~~~~~~~~~~~~~~~~~~
Each worker subscribes to a glob pattern per owned sample:
``osimflow:jobqueue:<campaign_id>:sample_{N}_*``.  This guarantees a worker
only ever receives messages for its assigned samples.  The coordinator
(which has no ``sample_ids``) publishes to all channels but does not
subscribe — it only broadcasts state changes.

The ``recover`` action is local-only: each node calls ``recover()`` on its
own local JobQueue at startup to pick up any in-flight jobs from a previous
crash.  No cross-node broadcast is required because every node's local queue
is independent.

Message format (JSON)
--------------------
Enqueue::

    {"action": "enqueue", "job_id": "sample_0_RUN_OPENSTUDIO_SIM",
     "sample_id": "sample_0", "payload": {...}}

Mark completed::

    {"action": "mark_completed", "job_id": "sample_0_RUN_OPENSTUDIO_SIM",
     "sample_id": "sample_0"}

Mark failed::

    {"action": "mark_failed", "job_id": "sample_0_RUN_OPENSTUDIO_SIM",
     "sample_id": "sample_0", "error": "..."}

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

__all__ = ["DistributedJobQueue", "build_job_queue"]

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


_CHANNEL_PREFIX = "osimflow:jobqueue"


def _extract_sample_id(job_id: str) -> str:
    """Extract the sample_id prefix from a job_id.

    Job IDs follow the format ``sample_{index}_{step}`` (e.g.
    ``sample_0_RUN_OPENSTUDIO_SIM`` or ``sample_42_sim``).  This function
    returns the sample portion — everything up to and including the index —
    so that all steps for the same sample share a channel.

    Examples
    --------
    >>> _extract_sample_id("sample_0_RUN_OPENSTUDIO_SIM")
    'sample_0'
    >>> _extract_sample_id("sample_42_sim")
    'sample_42'
    """
    parts = job_id.split("_")
    if len(parts) >= 2:
        return f"{parts[0]}_{parts[1]}"
    return job_id


class DistributedJobQueue:
    """JobQueue wrapper with Redis pub/sub for cross-node job coordination.

    This class is a drop-in replacement for ``JobQueue``.  All job queue
    operations are local (filesystem); state-change events are broadcast via
    per-sample Redis channels so that workers only receive messages for their
    assigned samples (issue #1219).

    Usage (coordinator — publishes all, subscribes to none)::

        queue = DistributedJobQueue(
            queue_dir=Path("outdir/work/queue"),
            redis_url="redis://localhost:6379/0",
            campaign_id="2025-01-01T12-00-00",
        )
        queue.enqueue("sample_0_SIM", {"sample_id": "s0", "step": "SIM"})
        queue.mark_completed("sample_0_SIM")
        # Or simply: queue.close() when done.

    Usage (worker — subscribes only to its assigned samples)::

        queue = DistributedJobQueue(
            queue_dir=Path("outdir/work/queue"),
            redis_url="redis://localhost:6379/0",
            campaign_id="2025-01-01T12-00-00",
            sample_ids={"sample_0", "sample_1", "sample_2"},
        )
        # The subscriber filters messages, applying only those for sample_0/1/2
        # to the local queue.  All other messages are dropped silently.

    Per-sample channels
    -------------------
    Each job is published to ``osimflow:jobqueue:<campaign_id>:<sample_id>``.
    Workers configured with ``sample_ids`` subscribe to a glob pattern per
    sample (``osimflow:jobqueue:<campaign_id>:<sample_id>_*``) and filter
    messages at the application layer.  This eliminates the O(N) fan-out
    bottleneck for large campaigns (issue #1219).

    Subscriber management
    --------------------
    A background thread runs a non-blocking Redis subscriber that calls
    ``self._local.*`` for each received message whose ``sample_id`` is in
    the configured ``sample_ids`` set.  The subscriber is started lazily on
    the first state-changing call and stopped when ``close()`` is called.
    """

    def __init__(
        self,
        queue_dir: Path,
        redis_url: str,
        campaign_id: str,
        *,
        sample_ids: set[str] | None = None,
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
        sample_ids
            Set of sample IDs this worker is responsible for (e.g.
            ``{"sample_0", "sample_1", "sample_2"}``).  The subscriber
            filters incoming messages, applying only those whose
            ``sample_id`` is in this set to the local queue.  When
            ``None`` (coordinator mode), the subscriber is disabled and
            all state-change operations are broadcast to all channels
            so that *other* nodes can filter on their side.
        """
        self._local = JobQueue(queue_dir)
        self._redis_url = redis_url
        self._campaign_id = campaign_id
        self._sample_ids: set[str] = sample_ids if sample_ids is not None else set()
        self._channel_prefix = f"{_CHANNEL_PREFIX}:{campaign_id}"

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
    # Subscriber thread with auto-recovery (issue #443)
    # ------------------------------------------------------------------
    def _start_subscriber(self) -> None:
        """Start the background subscriber thread with auto-recovery (idempotent)."""
        if self._subscriber_thread is not None:
            return

        if not self._sample_ids:
            return

        def _run() -> None:
            import asyncio  # noqa: PLC0415

            redis_async = _get_redis_asyncio()

            async def _main() -> None:
                reconnect_delay = 1.0
                max_reconnect_delay = 60.0
                channels = [f"{self._channel_prefix}:{sid}_*" for sid in self._sample_ids]

                while not self._stop_subscriber.is_set():
                    client = redis_async.from_url(
                        self._redis_url,
                        encoding="utf-8",
                        decode_responses=True,
                    )
                    try:
                        log.info(
                            "DistributedJobQueue subscriber started for campaign=%s channels=%s sample_ids=%s",
                            self._campaign_id,
                            channels,
                            self._sample_ids,
                        )
                        async with client.pubsub() as pubsub:
                            await pubsub.psubscribe(*channels)
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
                                        "DistributedJobQueue: received non-JSON message: %r",
                                        data,
                                    )
                                    continue
                                self._handle_action(payload)
                    except Exception as exc:
                        if self._stop_subscriber.is_set():
                            break
                        log.warning(
                            "DistributedJobQueue subscriber error (campaign=%s): %s — reconnecting in %.1fs",
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
        """Process a received action message against the local queue.

        Messages whose ``sample_id`` is not in the worker's ``sample_ids`` set
        are dropped silently at the application layer (issue #1219).  This
        avoids the local queue being polluted with jobs for other samples.
        """
        action = payload.get("action")
        sample_id = payload.get("sample_id", "")

        if self._sample_ids and sample_id not in self._sample_ids:
            return

        try:
            if action == "enqueue":
                job_id = payload.get("job_id")
                job_payload = payload.get("payload", {})
                priority = payload.get("priority", 0)
                if job_id:
                    self._local.enqueue(job_id, job_payload, priority=priority)
                    log.info(
                        "DistributedJobQueue: received enqueue job_id=%s priority=%d sample_id=%s",
                        job_id,
                        priority,
                        sample_id,
                    )
            elif action == "mark_completed":
                job_id = payload.get("job_id")
                if job_id:
                    self._local.mark_completed(job_id)
                    log.info(
                        "DistributedJobQueue: received mark_completed job_id=%s sample_id=%s",
                        job_id,
                        sample_id,
                    )
            elif action == "mark_failed":
                job_id = payload.get("job_id")
                error = payload.get("error", "")
                if job_id:
                    self._local.mark_failed(job_id, error)
                    log.info(
                        "DistributedJobQueue: received mark_failed job_id=%s sample_id=%s",
                        job_id,
                        sample_id,
                    )
            elif action == "recover":
                recovered = self._local.recover()
                log.info(
                    "DistributedJobQueue: received recover (%d jobs reset) sample_id=%s",
                    len(recovered),
                    sample_id,
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

    def _publish(self, payload: dict[str, Any], channel: str | None = None) -> None:
        """Publish an action message to a Redis channel (async, non-blocking).

        Parameters
        ----------
        payload
            The JSON-serializable message body.  ``sample_id`` is added
            automatically from the associated ``job_id``.
        channel
            Optional explicit channel name.  When ``None``, derives the
            per-sample channel from the payload's ``sample_id`` field.
        """
        import asyncio  # noqa: PLC0415

        sample_id = payload.get("sample_id", "")
        target_channel = channel or f"{self._channel_prefix}:{sample_id}_"

        async def _pub() -> None:
            try:
                client = self._get_redis()
                await client.publish(target_channel, json.dumps(payload))
            except Exception as exc:
                log.warning(
                    "DistributedJobQueue: failed to publish action for campaign=%s channel=%s: %s",
                    self._campaign_id,
                    target_channel,
                    exc,
                )

        try:
            asyncio.get_running_loop()
            asyncio.create_task(_pub())
        except RuntimeError:

            def _run() -> None:
                asyncio.run(_pub())

            t = threading.Thread(target=_run, daemon=True)
            t.start()

    # ------------------------------------------------------------------
    # Public queue interface (same as JobQueue)
    # ------------------------------------------------------------------
    def enqueue(self, job_id: str, payload: dict[str, Any], priority: int = 0) -> Path:
        """Enqueue a job locally and broadcast to its per-sample Redis channel."""
        with self._sub_lock:
            if self._subscriber_thread is None:
                self._start_subscriber()
        sample_id = _extract_sample_id(job_id)
        result = self._local.enqueue(job_id, payload, priority=priority)
        self._publish(
            {
                "action": "enqueue",
                "job_id": job_id,
                "sample_id": sample_id,
                "payload": payload,
                "priority": priority,
            }
        )
        return result

    def dequeue(self) -> dict[str, Any] | None:
        """Dequeue a job locally (no Redis broadcast for dequeue).

        Note: dequeue is a local-only operation in this implementation.
        Each worker dequeues independently from its local queue.
        """
        return self._local.dequeue()

    def mark_completed(self, job_id: str) -> None:
        """Mark a job completed locally and broadcast to its per-sample Redis channel."""
        with self._sub_lock:
            if self._subscriber_thread is None:
                self._start_subscriber()
        sample_id = _extract_sample_id(job_id)
        self._local.mark_completed(job_id)
        self._publish({"action": "mark_completed", "job_id": job_id, "sample_id": sample_id})

    def mark_failed(self, job_id: str, error: str) -> None:
        """Mark a job failed locally and broadcast to its per-sample Redis channel."""
        with self._sub_lock:
            if self._subscriber_thread is None:
                self._start_subscriber()
        sample_id = _extract_sample_id(job_id)
        self._local.mark_failed(job_id, error)
        self._publish(
            {"action": "mark_failed", "job_id": job_id, "sample_id": sample_id, "error": error}
        )

    def pending_jobs(self) -> list[dict[str, Any]]:
        """List all pending jobs (local only)."""
        return self._local.pending_jobs()

    def recover(self) -> list[dict[str, Any]]:
        """Recover in-flight jobs from the local queue (local-only, no Redis broadcast).

        Each node independently recovers its own local JobQueue at startup.
        No cross-node broadcast is required because every node's local queue
        is independent.
        """
        with self._sub_lock:
            if self._subscriber_thread is None:
                self._start_subscriber()
        return self._local.recover()

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
    *,
    sample_ids: set[str] | None = None,
) -> JobQueue | DistributedJobQueue:
    """Factory: build the appropriate job queue from configuration.

    When ``redis_url`` is ``None``, returns a plain ``JobQueue`` (single-process
    behaviour).  When a Redis URL is provided, returns a ``DistributedJobQueue``
    that broadcasts job state changes to per-sample Redis channels so that
    workers only receive messages for their assigned samples (issue #1219).

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
    sample_ids
        Set of sample IDs this worker owns.  Passed to
        :class:`DistributedJobQueue` so the subscriber filters incoming
        messages to only those whose ``sample_id`` is in this set.
        Omit or pass ``None`` for coordinator mode (publishes all,
        subscribes to none).

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
        sample_ids=sample_ids,
    )
