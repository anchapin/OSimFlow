"""Tests for osimflow.distributed_jobqueue — Redis-backed distributed job queue."""

from __future__ import annotations

import threading
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from osimflow.circuit_breaker import CircuitBreaker
from osimflow.distributed_jobqueue import (
    DistributedJobQueue,
    build_job_queue,
)
from osimflow.jobqueue import JobQueue


@pytest.fixture
def queue_dir(tmp_path: Path) -> Path:
    """Provide a clean queue directory."""
    return tmp_path / "queue"


@pytest.fixture
def mock_redis():
    """Mock redis.asyncio module via ``_get_redis_asyncio``.

    Patches the ``_get_redis_asyncio`` accessor (not the bare dict) so the
    mock is actually returned to callers; patching the dict with a
    ``MagicMock`` causes ``mock["module"]`` to short-circuit to an
    auto-generated child mock and silently break publish assertions.
    """
    with patch("osimflow.distributed_jobqueue._get_redis_asyncio") as get_module:
        mock_client = MagicMock()
        mock_client.publish = AsyncMock(return_value=1)
        mock_client.aclose = AsyncMock()
        mock_module = MagicMock()
        mock_module.from_url = MagicMock(return_value=mock_client)
        get_module.return_value = mock_module
        yield mock_client


class TestBuildJobQueue:
    """build_job_queue factory function."""

    def test_returns_plain_jobqueue_when_redis_url_is_none(self, queue_dir: Path) -> None:
        """When redis_url is None, a plain JobQueue is returned."""
        queue = build_job_queue(
            queue_dir=queue_dir,
            redis_url=None,
            campaign_id="test-campaign",
        )
        assert isinstance(queue, JobQueue)
        assert not isinstance(queue, DistributedJobQueue)

    def test_returns_distributed_jobqueue_when_redis_url_set(self, queue_dir: Path) -> None:
        """When redis_url is set, a DistributedJobQueue is returned."""
        queue = build_job_queue(
            queue_dir=queue_dir,
            redis_url="redis://localhost:6379/0",
            campaign_id="test-campaign",
        )
        assert isinstance(queue, DistributedJobQueue)
        assert not isinstance(queue, JobQueue)


class TestDistributedJobQueueInit:
    """Initialization and directory structure."""

    def test_creates_state_subdirectories(self, queue_dir: Path) -> None:
        """DistributedJobQueue creates state subdirectories via local JobQueue."""
        DistributedJobQueue(
            queue_dir=queue_dir,
            redis_url="redis://localhost:6379/0",
            campaign_id="test-campaign",
        )
        from osimflow.jobqueue import STATES

        for state in STATES:
            assert (queue_dir / state).is_dir()


class TestDistributedJobQueueEnqueue:
    """enqueue() broadcasts to Redis."""

    def test_enqueue_returns_path(self, queue_dir: Path) -> None:
        """enqueue() returns a path like the local JobQueue."""
        dq = DistributedJobQueue(
            queue_dir=queue_dir,
            redis_url="redis://localhost:6379/0",
            campaign_id="test-campaign",
        )
        path = dq.enqueue("job_1", {"step": "SIM"})
        assert path.exists()
        assert path.name == "job_1.json"

    def test_enqueue_idempotent(self, queue_dir: Path) -> None:
        """enqueue() is idempotent like the local JobQueue."""
        dq = DistributedJobQueue(
            queue_dir=queue_dir,
            redis_url="redis://localhost:6379/0",
            campaign_id="test-campaign",
        )
        p1 = dq.enqueue("job_1", {"step": "SIM"})
        p2 = dq.enqueue("job_1", {"step": "SIM"})
        assert p1 == p2
        assert len(dq.pending_jobs()) == 1


class TestDistributedJobQueueMarkCompleted:
    """mark_completed() broadcasts to Redis."""

    def test_marks_completed_from_pending(self, queue_dir: Path) -> None:
        """mark_completed() moves job to completed."""
        dq = DistributedJobQueue(
            queue_dir=queue_dir,
            redis_url="redis://localhost:6379/0",
            campaign_id="test-campaign",
        )
        dq.enqueue("job_1", {"step": "SIM"})
        dq.mark_completed("job_1")
        completed = dq.jobs_by_state("completed")
        assert len(completed) == 1
        assert completed[0]["state"] == "completed"

    def test_silent_when_not_found(self, queue_dir: Path) -> None:
        """Non-existent job is silently skipped."""
        dq = DistributedJobQueue(
            queue_dir=queue_dir,
            redis_url="redis://localhost:6379/0",
            campaign_id="test-campaign",
        )
        dq.mark_completed("nonexistent")  # Should not raise


class TestDistributedJobQueueMarkFailed:
    """mark_failed() broadcasts to Redis."""

    def test_marks_failed_with_error(self, queue_dir: Path) -> None:
        """mark_failed() moves job to failed with error message."""
        dq = DistributedJobQueue(
            queue_dir=queue_dir,
            redis_url="redis://localhost:6379/0",
            campaign_id="test-campaign",
        )
        dq.enqueue("job_1", {})
        dq.mark_failed("job_1", "Sim crashed: OOM")
        failed = dq.jobs_by_state("failed")
        assert len(failed) == 1
        assert failed[0]["state"] == "failed"
        assert failed[0]["error"] == "Sim crashed: OOM"


class TestDistributedJobQueueRecover:
    """recover() broadcasts to Redis."""

    def test_resets_in_progress_to_pending(self, queue_dir: Path) -> None:
        """recover() resets in-flight jobs."""
        dq = DistributedJobQueue(
            queue_dir=queue_dir,
            redis_url="redis://localhost:6379/0",
            campaign_id="test-campaign",
        )
        dq.enqueue("job_1", {"step": "SIM"})
        dq.enqueue("job_2", {"step": "SIM"})
        dq.dequeue()  # job_1 -> in_progress
        dq.dequeue()  # job_2 -> in_progress
        assert len(dq.jobs_by_state("in_progress")) == 2

        recovered = dq.recover()
        assert len(recovered) == 2
        assert len(dq.pending_jobs()) == 2
        assert len(dq.jobs_by_state("in_progress")) == 0


class TestDistributedJobQueueQuery:
    """Query methods are local-only."""

    def test_pending_jobs(self, queue_dir: Path) -> None:
        """pending_jobs() returns local jobs."""
        dq = DistributedJobQueue(
            queue_dir=queue_dir,
            redis_url="redis://localhost:6379/0",
            campaign_id="test-campaign",
        )
        dq.enqueue("job_1", {"step": "SIM"})
        dq.enqueue("job_2", {"step": "SIM"})
        assert len(dq.pending_jobs()) == 2

    def test_job_count(self, queue_dir: Path) -> None:
        """job_count() returns local counts."""
        dq = DistributedJobQueue(
            queue_dir=queue_dir,
            redis_url="redis://localhost:6379/0",
            campaign_id="test-campaign",
        )
        dq.enqueue("job_1", {})
        dq.enqueue("job_2", {})
        counts = dq.job_count()
        assert counts["pending"] == 2
        assert counts["completed"] == 0

    def test_has_pending(self, queue_dir: Path) -> None:
        """has_pending() returns local state."""
        dq = DistributedJobQueue(
            queue_dir=queue_dir,
            redis_url="redis://localhost:6379/0",
            campaign_id="test-campaign",
        )
        assert dq.has_pending() is False
        dq.enqueue("job_1", {})
        assert dq.has_pending() is True


class TestDistributedJobQueueContextManager:
    """Context manager protocol."""

    def test_context_manager(self, queue_dir: Path) -> None:
        """Can use DistributedJobQueue as a context manager."""
        dq = DistributedJobQueue(
            queue_dir=queue_dir,
            redis_url="redis://localhost:6379/0",
            campaign_id="test-campaign",
        )
        with dq as q:
            assert q is dq
            q.enqueue("job_1", {"step": "SIM"})
        # After exiting, has_pending should still work (close was called)
        assert dq.has_pending() is True


class TestDistributedJobQueueAutoRecovery:
    """Test auto-recovery of the Redis subscriber thread (issue #443)."""

    def test_subscriber_reconnect_delay_doubles_on_error(
        self, queue_dir: Path, mock_redis: MagicMock
    ) -> None:
        """Reconnect delay doubles after each error, capped at 60s."""
        from unittest.mock import patch

        dq = DistributedJobQueue(
            queue_dir=queue_dir,
            redis_url="redis://localhost:6379/0",
            campaign_id="test-backoff",
            sample_ids={"test_backoff_0"},
        )

        async def mock_get_message(timeout: float = 1.0, ignore_subscribe_messages: bool = True):
            raise ConnectionError("Redis connection lost")

        mock_pubsub = AsyncMock()
        mock_pubsub.get_message = mock_get_message
        mock_pubsub.subscribe = AsyncMock()

        mock_client_instance = AsyncMock()
        mock_client_instance.pubsub = MagicMock(return_value=mock_pubsub)
        mock_pubsub.__aenter__ = AsyncMock(return_value=mock_pubsub)
        mock_pubsub.__aexit__ = AsyncMock(return_value=None)
        mock_client_instance.aclose = AsyncMock()

        sleep_delays: list[float] = []
        two_sleeps = threading.Event()

        async def mock_sleep(delay: float) -> None:
            sleep_delays.append(delay)
            if len(sleep_delays) >= 2:
                two_sleeps.set()

        mock_ra = MagicMock()
        mock_ra.from_url.return_value = mock_client_instance

        with patch("osimflow.distributed_jobqueue._get_redis_asyncio", return_value=mock_ra):
            with patch("asyncio.sleep", mock_sleep):
                dq.enqueue("job_1", {"step": "SIM"})
                # Deterministic per issue #1544: synchronize on the mocked
                # asyncio.sleep calls instead of racing the subscriber thread
                # with a wall-clock sleep. The timeout is a failure bound,
                # not a timing assumption.
                assert two_sleeps.wait(timeout=10.0), (
                    "subscriber never reached the second reconnect backoff"
                )
                dq.close()

        assert len(sleep_delays) >= 2
        assert sleep_delays[0] <= 2.0
        assert sleep_delays[1] <= 4.0
        assert sleep_delays[1] >= sleep_delays[0]


class TestDistributedJobQueuePublishThreadBoundedness:
    """Thread pool boundedness for _publish (issue #1326)."""

    def test_publish_uses_bounded_thread_pool(self, queue_dir: Path) -> None:
        """Multiple publishes reuse the same ThreadPoolExecutor (max_workers=8)."""
        dq = DistributedJobQueue(
            queue_dir=queue_dir,
            redis_url="redis://localhost:6379/0",
            campaign_id="test-thread-bounded",
        )
        # Trigger several publishes in a context where there is no running loop.
        for i in range(20):
            dq.enqueue(f"job_{i}", {"step": "SIM"})
        # The executor should exist and have max_workers=8.
        assert dq._publish_executor is not None
        assert dq._publish_executor._max_workers == 8  # type: ignore[attr-defined]
        dq.close()

    def test_close_shuts_down_publish_executor(self, queue_dir: Path) -> None:
        """close() shuts down the publish executor."""
        dq = DistributedJobQueue(
            queue_dir=queue_dir,
            redis_url="redis://localhost:6379/0",
            campaign_id="test-close-executor",
        )
        dq.enqueue("job_1", {"step": "SIM"})
        assert dq._publish_executor is not None
        dq.close()
        assert dq._publish_executor is None


class TestDistributedJobQueueCircuitBreaker:
    """CircuitBreaker wiring for _publish (issue #1397).

    Mirrors the data-plane breaker pattern used in ``DistributedCache`` and
    ``RedisDocumentStore`` (issue #1111): after repeated consecutive Redis
    failures, the publish path is short-circuited so a persistent outage
    cannot burn the 5 s socket timeout on every job state transition.
    """

    def _wait_for_executor(self, dq: DistributedJobQueue) -> None:
        """Block until all submitted _pub coroutines complete.

        ``_publish`` is fire-and-forget (issue #1326), so the test must wait
        for the executor's in-flight work to finish before asserting on the
        breaker's recorded outcome.  ``shutdown(wait=True)`` blocks until
        every queued task has run; ``cancel_futures=True`` ensures we drain
        only what was submitted (no new work accepted after this point).
        """
        with dq._pub_lock:  # type: ignore[attr-defined]
            executor = dq._publish_executor
            if executor is None:
                return
            executor.shutdown(wait=True, cancel_futures=True)
            dq._publish_executor = None

    def test_breaker_constructed_with_campaign_id(self, queue_dir: Path) -> None:
        """``_breaker`` is constructed eagerly and named after the campaign (issue #1397)."""
        dq = DistributedJobQueue(
            queue_dir=queue_dir,
            redis_url="redis://localhost:6379/0",
            campaign_id="issue-1397-test",
        )
        assert isinstance(dq._breaker, CircuitBreaker)
        assert dq._breaker.name == "jobqueue:issue-1397-test"
        assert dq._breaker.state == "closed"
        assert dq.breaker_state == "closed"

    def test_publish_records_failure_then_success(
        self, queue_dir: Path, mock_redis: MagicMock
    ) -> None:
        """ConnectionError on first publish → record_failure; success on retry → record_success.

        The breaker should stay closed across a transient failure, with
        ``consecutive_failures`` climbing to 1 then back to 0 after the
        successful retry.
        """
        success_calls = threading.Event()
        call_count = [0]

        async def publish_side_effect(*args: object, **kwargs: object) -> int:
            call_count[0] += 1
            if call_count[0] == 1:
                raise ConnectionError("redis down")
            success_calls.set()
            return 1

        # First call raises ConnectionError; subsequent calls succeed.
        mock_redis.publish = AsyncMock(side_effect=publish_side_effect)

        dq = DistributedJobQueue(
            queue_dir=queue_dir,
            redis_url="redis://localhost:6379/0",
            campaign_id="test-mixed",
            sample_ids=set(),  # avoid subscriber thread; pure publish path
        )

        # First enqueue: publishes once → ConnectionError → record_failure.
        dq.enqueue("sample_0_job_1", {"step": "SIM"})
        # Second enqueue: publishes once → success → record_success.
        dq.enqueue("sample_0_job_2", {"step": "SIM"})

        # Wait for the second publish attempt to complete (success).
        assert success_calls.wait(timeout=5.0)
        self._wait_for_executor(dq)

        # After the failure + the success, the counter is back to 0
        # and the circuit is closed.
        assert dq._breaker.consecutive_failures == 0
        assert dq._breaker.state == "closed"
        # Both publishes were attempted (failure + success).
        assert mock_redis.publish.call_count == 2

        dq.close()

    def test_persistent_outage_opens_breaker_and_short_circuits(
        self, queue_dir: Path, mock_redis: MagicMock
    ) -> None:
        """Always-failing publish opens the breaker; subsequent enqueues short-circuit locally.

        This is the regression test for issue #1397: under a persistent
        Redis outage, ``enqueue`` must short-circuit to local-only state
        without burning socket timeouts on every state transition.
        """
        # Always raise — simulates a Redis outage with the 5 s socket
        # timeout burned on each attempt.
        mock_redis.publish = AsyncMock(side_effect=ConnectionError("redis down"))

        dq = DistributedJobQueue(
            queue_dir=queue_dir,
            redis_url="redis://localhost:6379/0",
            campaign_id="test-persistent-outage",
            sample_ids=set(),  # no subscriber thread; pure publish path
        )

        # Trigger enough publishes to drive the breaker open (default threshold is 5).
        for i in range(dq._breaker.failure_threshold + 2):
            dq.enqueue(f"job_{i}", {"step": "SIM"})

        self._wait_for_executor(dq)
        assert dq._breaker.state == "open"
        calls_during_outage = mock_redis.publish.call_count

        # After the breaker is open, additional enqueues must short-circuit
        # before reaching the Redis client — i.e. ``publish.call_count``
        # must not keep growing.  Each enqueue still records the job
        # locally so the campaign keeps running in local-only mode.
        for i in range(20):
            dq.enqueue(f"job_post_open_{i}", {"step": "SIM"})

        self._wait_for_executor(dq)
        # Breaker stays open — no half-open transition within the test window.
        assert dq._breaker.state == "open"
        # ``client.publish`` must not have been called again after the
        # breaker opened; the short-circuit path skips the async work.
        assert mock_redis.publish.call_count == calls_during_outage

        # Local state is fully consistent: every enqueue landed on disk
        # even though the publish was short-circuited.
        pending = dq.pending_jobs()
        assert len(pending) == (dq._breaker.failure_threshold + 2 + 20)
        assert all(job["id"].startswith(("job_", "job_post_open_")) for job in pending)

        dq.close()

    def test_enqueue_returns_local_only_state_when_breaker_open(
        self, queue_dir: Path, mock_redis: MagicMock
    ) -> None:
        """With the breaker already open, ``enqueue`` returns local-only state.

        Mirrors the acceptance criterion for issue #1397: state transitions
        never burn socket timeouts when the breaker is open.
        """
        dq = DistributedJobQueue(
            queue_dir=queue_dir,
            redis_url="redis://localhost:6379/0",
            campaign_id="test-short-circuit",
            sample_ids=set(),
        )

        # Force the breaker open without ever calling publish.
        for _ in range(dq._breaker.failure_threshold):
            dq._breaker.record_failure()
        assert dq._breaker.state == "open"

        mock_redis.publish.reset_mock()

        # ``enqueue`` should return a local path even though the breaker
        # is open; the Redis publish path must be skipped entirely.
        result = dq.enqueue("sample_0_job_short_circuit", {"step": "SIM"})
        assert result.exists()
        assert result.name == "sample_0_job_short_circuit.json"

        # ``mark_completed`` / ``mark_failed`` must also short-circuit
        # without touching the Redis client.
        dq.mark_completed("sample_0_job_short_circuit")
        dq.enqueue("sample_0_job_failed", {})
        dq.mark_failed("sample_0_job_failed", "boom")

        # Give any in-flight work a chance to (incorrectly) complete.
        self._wait_for_executor(dq)

        # No Redis publish attempts were dispatched while the breaker
        # was open — local-only behaviour is preserved.
        assert mock_redis.publish.call_count == 0
        # Local state is consistent with the operations performed.
        assert len(dq.jobs_by_state("completed")) == 1
        assert len(dq.jobs_by_state("failed")) == 1
        # Breaker stays open.
        assert dq._breaker.state == "open"

        dq.close()
