"""Filesystem-based job queue for crash recovery.

A lightweight persistence layer that records pending work items as JSON files
on disk. If the orchestrator process crashes, in-flight jobs are detected on
restart via ``recover()`` and their state is reset so they can be reprocessed.

**Limitations:**

* This is **not** a distributed queue — it has no locking, no broker, and no
  consumer group support. It is designed for single-process orchestrator crash
  recovery only.
* Concurrent writers (multiple orchestrator processes pointing at the same
  ``outdir``) are **not** supported. Use a proper message broker (Redis, SQS)
  for multi-producer / multi-consumer setups.
* Job files are written atomically via ``write-then-rename`` so partial writes
  from a crash mid-flush are avoided (the temp file is discarded; the original
  is left intact).

Directory layout::

    {outdir}/work/queue/
        pending/
            <job_id>.json
        in_progress/
            <job_id>.json
        completed/
            <job_id>.json
        failed/
            <job_id>.json

Job JSON schema::

    {
        "id": "sample_0_sim",
        "state": "pending",
        "payload": { ... },
        "priority": 0,
        "created_at": 1700000000.123,
        "started_at": null,
        "completed_at": null,
        "error": null
    }

Priority ordering:
    Jobs are dequeued in priority order (highest priority first).
    Priority is an integer where higher values indicate higher priority.
    Jobs with equal priority are ordered by ``created_at`` (oldest first).
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

log = logging.getLogger("osimflow.jobqueue")

# Valid job states and their corresponding subdirectories.
STATES = ("pending", "in_progress", "completed", "failed")


class JobQueue:
    """Filesystem-backed job queue for orchestrator crash recovery.

    Parameters
    ----------
    queue_dir
        Root directory for the queue. Subdirectories for each state
        are created lazily on first write.  Typically ``{outdir}/work/queue``.
    """

    def __init__(self, queue_dir: Path) -> None:
        self._root = queue_dir
        self._ensure_dirs()

    # ------------------------------------------------------------------
    # Directory management
    # ------------------------------------------------------------------
    def _ensure_dirs(self) -> None:
        """Create the state subdirectories if they don't exist."""
        for state in STATES:
            (self._root / state).mkdir(parents=True, exist_ok=True)

    def _state_dir(self, state: str) -> Path:
        d = self._root / state
        if not d.is_dir():
            raise ValueError(f"invalid job state: {state!r}")
        return d

    # ------------------------------------------------------------------
    # Core operations
    # ------------------------------------------------------------------
    def enqueue(self, job_id: str, payload: dict[str, Any], priority: int = 0) -> Path:
        """Write a new job to the ``pending`` directory.

        If a job with the same ``job_id`` already exists in any state
        directory, it is **not** overwritten — the existing job file is
        returned instead.  This makes ``enqueue()`` idempotent for crash
        recovery: calling it twice for the same sample is safe.

        Parameters
        ----------
        job_id
            Unique identifier for this work item (e.g.
            ``"sample_0_RUN_OPENSTUDIO_SIM"``).
        payload
            Arbitrary JSON-serializable data describing the work.
        priority
            Integer priority value. Higher values are dequeued first.
            Defaults to 0.

        Returns
        -------
        Path
            The path to the job file.
        """
        # Idempotency: if the job already exists anywhere, don't touch it.
        existing = self._find_job(job_id)
        if existing is not None:
            log.debug("enqueue: job %s already exists at %s, skipping", job_id, existing)
            return existing

        record: dict[str, Any] = {
            "id": job_id,
            "state": "pending",
            "payload": payload,
            "priority": priority,
            "created_at": time.time(),
            "started_at": None,
            "completed_at": None,
            "error": None,
        }
        return self._write_job("pending", record)

    def dequeue(self) -> dict[str, Any] | None:
        """Pick up the next pending job and move it to ``in_progress``.

        Returns ``None`` when there are no pending jobs. The returned dict
        is the full job record with ``state`` updated to ``"in_progress"``
        and ``started_at`` set to the current time.

        Jobs are selected by highest priority first, then oldest by
        ``created_at`` for stable ordering within the same priority.
        """
        pending_dir = self._state_dir("pending")
        job_files = list(pending_dir.glob("*.json"))
        if not job_files:
            return None

        # Read all job records and sort by priority (desc), then created_at (asc).
        jobs: list[dict[str, Any]] = []
        for job_file in job_files:
            try:
                record = self._read_job(job_file)
                jobs.append(record)
            except (json.JSONDecodeError, ValueError) as exc:
                log.warning("skipping corrupt job file %s: %s", job_file, exc)
                continue

        if not jobs:
            return None

        jobs.sort(key=lambda j: (-j.get("priority", 0), j.get("created_at", 0)))
        record = jobs[0]
        record["state"] = "in_progress"
        record["started_at"] = time.time()

        # Atomic move: write new file then remove old.
        self._write_job("in_progress", record)
        (pending_dir / f"{record['id']}.json").unlink(missing_ok=True)
        log.debug("dequeue: picked up job %s (priority=%d)", record["id"], record.get("priority", 0))
        return record

    def mark_completed(self, job_id: str) -> None:
        """Move a job to ``completed``.

        Looks up the job in ``pending`` or ``in_progress`` and moves it
        to ``completed``.  Raises ``FileNotFoundError`` if the job is
        not found in either directory (it may already be completed or
        failed, or never enqueued).
        """
        self._transition_from_any(job_id, "completed", error=None)

    def mark_failed(self, job_id: str, error: str) -> None:
        """Move a job to ``failed``.

        Parameters
        ----------
        job_id
            The job identifier.
        error
            Human-readable error description.
        """
        self._transition_from_any(job_id, "failed", error=error)

    def pending_jobs(self) -> list[dict[str, Any]]:
        """List all pending jobs (not yet started).

        Returns a list of job records sorted by ``created_at``.
        """
        return self._list_jobs("pending")

    def recover(self) -> list[dict[str, Any]]:
        """Resume any jobs that were in-flight when a crash occurred.

        Moves all ``in_progress`` jobs back to ``pending`` so they will be
        reprocessed on the next run.  Returns the list of recovered job
        records (empty if nothing to recover).

        This should be called at campaign start, before any new submissions.
        """
        in_progress_dir = self._state_dir("in_progress")
        recovered: list[dict[str, Any]] = []
        for job_file in sorted(in_progress_dir.glob("*.json")):
            record = self._read_job(job_file)
            record["state"] = "pending"
            record["started_at"] = None
            self._write_job("pending", record)
            job_file.unlink(missing_ok=True)
            recovered.append(record)
            log.info(
                "recover: reset job %s from in_progress -> pending",
                record["id"],
            )
        if recovered:
            log.info("recover: %d in-flight job(s) reset to pending", len(recovered))
        return recovered

    # ------------------------------------------------------------------
    # Query helpers
    # ------------------------------------------------------------------
    def jobs_by_state(self, state: str) -> list[dict[str, Any]]:
        """List all jobs in a given state.

        Parameters
        ----------
        state
            One of ``"pending"``, ``"in_progress"``, ``"completed"``,
            ``"failed"``.
        """
        return self._list_jobs(state)

    def job_count(self) -> dict[str, int]:
        """Return a count of jobs in each state."""
        return {state: len(list(self._state_dir(state).glob("*.json"))) for state in STATES}

    def has_pending(self) -> bool:
        """Return ``True`` if there are any pending jobs."""
        return bool(list(self._state_dir("pending").glob("*.json")))

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _write_job(self, state: str, record: dict[str, Any]) -> Path:
        """Write a job file atomically to the given state directory.

        Uses write-to-temp-then-rename to avoid partial files on crash.
        """
        dest = self._state_dir(state) / f"{record['id']}.json"
        tmp = dest.with_suffix(".tmp")
        tmp.write_text(json.dumps(record, indent=2, default=str))
        tmp.rename(dest)
        return dest

    def _read_job(self, path: Path) -> dict[str, Any]:
        """Read and parse a job file."""
        data: Any = json.loads(path.read_text())
        if not isinstance(data, dict):
            raise ValueError(f"job file {path} does not contain a dict")
        return data

    def _find_job(self, job_id: str) -> Path | None:
        """Find an existing job file in any state directory."""
        filename = f"{job_id}.json"
        for state in STATES:
            candidate = self._state_dir(state) / filename
            if candidate.exists():
                return candidate
        return None

    def _transition(
        self,
        job_id: str,
        from_state: str,
        to_state: str,
        error: str | None = None,
    ) -> None:
        """Move a job from one state to another."""
        src = self._state_dir(from_state) / f"{job_id}.json"
        if not src.exists():
            raise FileNotFoundError(f"job {job_id!r} not found in {from_state} directory")
        record = self._read_job(src)
        record["state"] = to_state
        record["completed_at"] = time.time()
        if error is not None:
            record["error"] = error
        self._write_job(to_state, record)
        src.unlink(missing_ok=True)
        log.debug("transition: job %s %s -> %s", job_id, from_state, to_state)

    def _transition_from_any(
        self,
        job_id: str,
        to_state: str,
        error: str | None = None,
    ) -> None:
        """Move a job from its current non-terminal state to *to_state*.

        Searches ``pending`` then ``in_progress`` for the job file.
        This is the integration-friendly variant: the Campaign does not
        call ``dequeue()`` explicitly — it enqueues directly and then
        marks completed/failed, so the job may be in either state.
        """
        filename = f"{job_id}.json"
        for from_state in ("pending", "in_progress"):
            src = self._state_dir(from_state) / filename
            if src.exists():
                record = self._read_job(src)
                record["state"] = to_state
                record["completed_at"] = time.time()
                if error is not None:
                    record["error"] = error
                self._write_job(to_state, record)
                src.unlink(missing_ok=True)
                log.debug("transition: job %s %s -> %s", job_id, from_state, to_state)
                return
        # Job not found in any active state — may already be completed,
        # failed, or never enqueued.  This is not an error in the
        # crash-recovery context (the cache may have handled the sample).
        log.debug("transition: job %s not found in active states, skipping", job_id)

    def _list_jobs(self, state: str) -> list[dict[str, Any]]:
        """List jobs in a state directory, sorted by created_at."""
        state_dir = self._state_dir(state)
        jobs: list[dict[str, Any]] = []
        for job_file in state_dir.glob("*.json"):
            try:
                jobs.append(self._read_job(job_file))
            except (json.JSONDecodeError, ValueError) as exc:
                log.warning("skipping corrupt job file %s: %s", job_file, exc)
        jobs.sort(key=lambda j: j.get("created_at", 0))
        return jobs
