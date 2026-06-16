"""Tests for osimflow.distributed_jobqueue — Redis-backed distributed job queue."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

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
    """Mock redis.asyncio module."""
    with patch("osimflow.distributed_jobqueue._redis_asyncio_module") as mock:
        mock_client = MagicMock()
        mock_client.publish = MagicMock(return_value=1)
        mock_client.aclose = MagicMock()
        mock_module = MagicMock()
        mock_module.from_url = MagicMock(return_value=mock_client)
        mock["module"] = mock_module
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
        from unittest.mock import AsyncMock, patch

        dq = DistributedJobQueue(
            queue_dir=queue_dir,
            redis_url="redis://localhost:6379/0",
            campaign_id="test-backoff",
        )

        async def mock_get_message(timeout: float = 1.0, ignore_subscribe_messages: bool = True):
            raise ConnectionError("Redis connection lost")

        mock_pubsub = AsyncMock()
        mock_pubsub.get_message = mock_get_message
        mock_pubsub.subscribe = AsyncMock()

        async def mock_pubsub_context():
            return mock_pubsub

        mock_client_instance = AsyncMock()
        mock_client_instance.pubsub = mock_pubsub_context
        mock_client_instance.aclose = AsyncMock()

        sleep_delays: list[float] = []

        async def mock_sleep(delay: float) -> None:
            sleep_delays.append(delay)

        mock_ra = MagicMock()
        mock_ra.from_url.return_value = mock_client_instance

        with patch("osimflow.distributed_jobqueue._get_redis_asyncio", return_value=mock_ra):
            with patch("asyncio.sleep", mock_sleep):
                dq.enqueue("job_1", {"step": "SIM"})
                import time

                time.sleep(0.3)
                dq.close()

        assert len(sleep_delays) >= 2
        assert sleep_delays[0] <= 2.0
        assert sleep_delays[1] <= 4.0
        assert sleep_delays[1] >= sleep_delays[0]
