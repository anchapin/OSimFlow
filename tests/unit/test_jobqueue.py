"""Tests for osimflow.jobqueue — filesystem-based job queue for crash recovery."""

from __future__ import annotations

import time

import pytest

from osimflow.jobqueue import JobQueue


@pytest.fixture
def queue_dir(tmp_path):
    """Provide a clean queue directory."""
    return tmp_path / "queue"


@pytest.fixture
def q(queue_dir):
    """Provide a JobQueue instance."""
    return JobQueue(queue_dir)


class TestJobQueueInit:
    """Directory structure creation."""

    def test_creates_state_subdirectories(self, queue_dir):
        JobQueue(queue_dir)
        from osimflow.jobqueue import STATES

        for state in STATES:
            assert (queue_dir / state).is_dir()

    def test_idempotent_init(self, queue_dir):
        JobQueue(queue_dir)
        JobQueue(queue_dir)  # second call should not raise


class TestEnqueue:
    """enqueue() — write a job file."""

    def test_creates_pending_job_file(self, q, queue_dir):
        path = q.enqueue("job_1", {"step": "SIM", "sample_id": "s0"})
        assert path.exists()
        assert path.parent.name == "pending"

    def test_job_record_shape(self, q):
        q.enqueue("job_1", {"step": "SIM"})
        jobs = q.pending_jobs()
        assert len(jobs) == 1
        rec = jobs[0]
        assert rec["id"] == "job_1"
        assert rec["state"] == "pending"
        assert rec["payload"] == {"step": "SIM"}
        assert isinstance(rec["created_at"], float)
        assert rec["started_at"] is None
        assert rec["completed_at"] is None
        assert rec["error"] is None

    def test_enqueue_idempotent(self, q, queue_dir):
        p1 = q.enqueue("job_1", {"step": "SIM"})
        p2 = q.enqueue("job_1", {"step": "SIM"})
        # Same file returned; no duplicate created.
        assert p1 == p2
        assert len(q.pending_jobs()) == 1

    def test_multiple_jobs(self, q):
        q.enqueue("job_a", {"idx": 0})
        q.enqueue("job_b", {"idx": 1})
        q.enqueue("job_c", {"idx": 2})
        assert len(q.pending_jobs()) == 3

    def test_enqueue_returns_path(self, q):
        path = q.enqueue("my_job", {"x": 1})
        assert path.name == "my_job.json"


class TestDequeue:
    """dequeue() — pick up next pending job."""

    def test_returns_none_when_empty(self, q):
        assert q.dequeue() is None

    def test_moves_to_in_progress(self, q, queue_dir):
        q.enqueue("job_1", {"step": "SIM"})
        rec = q.dequeue()
        assert rec is not None
        assert rec["state"] == "in_progress"
        assert isinstance(rec["started_at"], float)
        # Pending dir should be empty.
        assert len(q.pending_jobs()) == 0
        # In-progress dir should have the job.
        in_progress = q.jobs_by_state("in_progress")
        assert len(in_progress) == 1

    def test_fifo_ordering(self, q):
        q.enqueue("job_a", {"i": 0})
        # Ensure different timestamps (sub-second).
        time.sleep(0.01)
        q.enqueue("job_b", {"i": 1})
        rec = q.dequeue()
        assert rec["id"] == "job_a"

    def test_dequeue_all(self, q):
        for i in range(5):
            q.enqueue(f"job_{i}", {"i": i})
        dequeued = []
        while True:
            rec = q.dequeue()
            if rec is None:
                break
            dequeued.append(rec)
        assert len(dequeued) == 5
        assert len(q.pending_jobs()) == 0


class TestMarkCompleted:
    """mark_completed() — move job to completed."""

    def test_marks_completed_from_in_progress(self, q, queue_dir):
        q.enqueue("job_1", {})
        q.dequeue()
        q.mark_completed("job_1")
        completed = q.jobs_by_state("completed")
        assert len(completed) == 1
        assert completed[0]["state"] == "completed"
        assert isinstance(completed[0]["completed_at"], float)

    def test_marks_completed_from_pending(self, q, queue_dir):
        """Integration scenario: Campaign enqueues but doesn't dequeue."""
        q.enqueue("job_1", {"step": "SIM"})
        # No dequeue — mark_completed should find it in pending.
        q.mark_completed("job_1")
        completed = q.jobs_by_state("completed")
        assert len(completed) == 1
        assert completed[0]["state"] == "completed"

    def test_silent_when_not_found(self, q):
        """Non-existent job is silently skipped (crash-recovery safe)."""
        # Should not raise — the cache may have already handled it.
        q.mark_completed("nonexistent")


class TestMarkFailed:
    """mark_failed() — move job to failed."""

    def test_marks_failed_with_error(self, q):
        q.enqueue("job_1", {})
        q.dequeue()
        q.mark_failed("job_1", "Sim crashed: OOM")
        failed = q.jobs_by_state("failed")
        assert len(failed) == 1
        assert failed[0]["state"] == "failed"
        assert failed[0]["error"] == "Sim crashed: OOM"
        assert isinstance(failed[0]["completed_at"], float)

    def test_marks_failed_from_pending(self, q):
        """Integration scenario: fail a job that was never dequeued."""
        q.enqueue("job_1", {})
        q.mark_failed("job_1", "error msg")
        failed = q.jobs_by_state("failed")
        assert len(failed) == 1

    def test_silent_when_not_found(self, q):
        q.mark_failed("nonexistent", "nope")


class TestRecover:
    """recover() — reset in-flight jobs after crash."""

    def test_no_in_progress_returns_empty(self, q):
        recovered = q.recover()
        assert recovered == []

    def test_resets_in_progress_to_pending(self, q, queue_dir):
        q.enqueue("job_1", {"step": "SIM"})
        q.enqueue("job_2", {"step": "SIM"})
        q.dequeue()  # job_1 -> in_progress
        q.dequeue()  # job_2 -> in_progress
        assert len(q.jobs_by_state("in_progress")) == 2

        recovered = q.recover()
        assert len(recovered) == 2
        # All reset to pending.
        assert len(q.pending_jobs()) == 2
        assert len(q.jobs_by_state("in_progress")) == 0
        # started_at cleared.
        for rec in recovered:
            assert rec["started_at"] is None
            assert rec["state"] == "pending"

    def test_preserves_completed_jobs(self, q):
        q.enqueue("job_1", {})
        q.dequeue()
        q.mark_completed("job_1")
        q.recover()
        # Completed job stays completed.
        assert len(q.jobs_by_state("completed")) == 1
        assert len(q.pending_jobs()) == 0

    def test_preserves_failed_jobs(self, q):
        q.enqueue("job_1", {})
        q.dequeue()
        q.mark_failed("job_1", "error")
        q.recover()
        assert len(q.jobs_by_state("failed")) == 1
        assert len(q.pending_jobs()) == 0

    def test_mixed_states(self, q):
        q.enqueue("j1", {})
        q.enqueue("j2", {})
        q.enqueue("j3", {})
        q.dequeue()  # j1 -> in_progress
        q.dequeue()  # j2 -> in_progress
        # j3 still pending
        q.recover()
        # j1 and j2 reset to pending; j3 was already pending.
        assert len(q.pending_jobs()) == 3
        assert len(q.jobs_by_state("in_progress")) == 0


class TestJobCount:
    """job_count() — per-state counts."""

    def test_empty_queue(self, q):
        counts = q.job_count()
        assert counts == {"pending": 0, "in_progress": 0, "completed": 0, "failed": 0}

    def test_mixed_states(self, q):
        q.enqueue("j1", {})
        q.enqueue("j2", {})
        q.enqueue("j3", {})
        q.dequeue()  # j1 -> in_progress
        q.dequeue()  # j2 -> in_progress
        q.mark_completed("j1")
        q.mark_failed("j2", "err")
        # j3 still pending
        counts = q.job_count()
        assert counts["pending"] == 1
        assert counts["completed"] == 1
        assert counts["failed"] == 1
        assert counts["in_progress"] == 0


class TestHasPending:
    """has_pending() — quick check."""

    def test_false_when_empty(self, q):
        assert q.has_pending() is False

    def test_true_after_enqueue(self, q):
        q.enqueue("j1", {})
        assert q.has_pending() is True

    def test_false_after_dequeue(self, q):
        q.enqueue("j1", {})
        q.dequeue()
        assert q.has_pending() is False


class TestCorruptFile:
    """Graceful handling of corrupt job files."""

    def test_list_skips_corrupt_file(self, q, queue_dir):
        pending_dir = queue_dir / "pending"
        # Write a corrupt file.
        (pending_dir / "bad.json").write_text("not valid json {{{")
        q.enqueue("good", {"ok": True})
        jobs = q.pending_jobs()
        assert len(jobs) == 1
        assert jobs[0]["id"] == "good"


class TestPayloadPreservation:
    """Ensure arbitrary payloads survive a full lifecycle."""

    def test_complex_payload_round_trip(self, q):
        payload = {
            "step": "RUN_OPENSTUDIO_SIM",
            "sample_id": "sample_42",
            "generation": 2,
            "params": {"window_ratio": 0.45, "r_value": 3.2},
            "paths": ["/tmp/a", "/tmp/b"],
            "nested": {"a": {"b": 1}},
        }
        q.enqueue("job_42", payload)
        rec = q.dequeue()
        assert rec["payload"] == payload

        q.mark_completed("job_42")
        completed = q.jobs_by_state("completed")
        assert completed[0]["payload"] == payload

    def test_recover_preserves_payload(self, q):
        payload = {"step": "SIM", "sample_id": "s0", "extra": [1, 2, 3]}
        q.enqueue("job_1", payload)
        q.dequeue()
        recovered = q.recover()
        assert len(recovered) == 1
        assert recovered[0]["payload"] == payload


class TestPriority:
    """Priority support for job dequeue ordering."""

    def test_default_priority_is_zero(self, q):
        """Jobs without explicit priority default to 0."""
        q.enqueue("job_1", {"step": "SIM"})
        rec = q.dequeue()
        assert rec is not None
        assert rec["priority"] == 0

    def test_higher_priority_dequeued_first(self, q):
        """Jobs with higher priority values are dequeued first."""
        q.enqueue("low", {"step": "SIM"}, priority=1)
        time.sleep(0.01)
        q.enqueue("high", {"step": "SIM"}, priority=10)
        time.sleep(0.01)
        q.enqueue("medium", {"step": "SIM"}, priority=5)
        rec = q.dequeue()
        assert rec is not None
        assert rec["id"] == "high"

    def test_equal_priority_fifo(self, q):
        """Jobs with equal priority follow FIFO ordering by created_at."""
        q.enqueue("first", {"step": "SIM"}, priority=5)
        time.sleep(0.01)
        q.enqueue("second", {"step": "SIM"}, priority=5)
        rec = q.dequeue()
        assert rec is not None
        assert rec["id"] == "first"
        rec = q.dequeue()
        assert rec is not None
        assert rec["id"] == "second"

    def test_priority_in_job_record(self, q):
        """Priority is preserved in the job record."""
        q.enqueue("job_1", {"step": "SIM"}, priority=42)
        jobs = q.pending_jobs()
        assert len(jobs) == 1
        assert jobs[0]["priority"] == 42

    def test_priority_negative(self, q):
        """Negative priority values are supported (lower than default)."""
        q.enqueue("lowest", {"step": "SIM"}, priority=-5)
        q.enqueue("normal", {"step": "SIM"}, priority=0)
        q.enqueue("high", {"step": "SIM"}, priority=10)
        rec = q.dequeue()
        assert rec is not None
        assert rec["id"] == "high"
        rec = q.dequeue()
        assert rec is not None
        assert rec["id"] == "normal"
        rec = q.dequeue()
        assert rec is not None
        assert rec["id"] == "lowest"
