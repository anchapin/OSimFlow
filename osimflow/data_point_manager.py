"""Data point lifecycle management for OSimFlow campaigns (issues #418, #419, #420).

Provides a :class:`DataPointManager` that tracks the full lifecycle of individual
simulation data points: creation, status updates, priority, reanalysis, and
result merging.

This module underpins three gap-analysis issues:

* **#419** — Data Point Lifecycle Management (CRUD for samples)
* **#420** — Data Point Reanalysis (re-run individual completed samples)
* **#418** — Automatic Data Point Merging (combine results from multiple runs)

The manager persists state in ``{outdir}/.osimflow/data_points.json`` so it
survives process restarts and is shared across campaign runs that share an
``--outdir``.
"""

from __future__ import annotations

__all__ = ["DataPoint", "DataPointManager", "DataPointStatus"]

import fasteners
import json
import os
import time
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

log = __import__("logging").getLogger("osimflow.data_point_manager")


class DataPointStatus(StrEnum):
    """Lifecycle state of a single data point."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    MERGED = "merged"  # consumed by a merge operation


@dataclass
class DataPoint:
    """Immutable-ish record of a single simulation data point.

    Attributes
    ----------
    seed_model
        Path to a per-sample seed model override. When set, this replaces
        the campaign-level ``template_sim_package`` for this sample only.
        Supports multi-archetype studies where each sample uses a different
        building model (GAP-009).
    weather_file
        Path to a per-sample weather file override. When set, this replaces
        the campaign-level weather file for this sample only. Supports
        multi-climate-zone studies where each sample uses a different EPW
        file (GAP-009).
    """

    sample_id: str
    status: DataPointStatus = DataPointStatus.PENDING
    priority: int = 0  # higher = more urgent
    work_dir: Path | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    completed_at: float | None = None
    error_summary: str | None = None
    # Merge metadata
    merged_into: str | None = None  # sample_id this was merged into
    merged_from: list[str] = field(default_factory=list)  # sample_ids merged into this one
    # Reanalysis metadata
    reanalyze_count: int = 0
    original_sample_id: str | None = None  # if this is a reanalysis of another
    # Per-sample override paths (GAP-009)
    seed_model: str | None = None  # absolute path to per-sample seed model
    weather_file: str | None = None  # absolute path to per-sample weather file

    def to_dict(self) -> dict[str, Any]:
        return {
            "sample_id": self.sample_id,
            "status": self.status.value,
            "priority": self.priority,
            "work_dir": str(self.work_dir) if self.work_dir else None,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "completed_at": self.completed_at,
            "error_summary": self.error_summary,
            "merged_into": self.merged_into,
            "merged_from": self.merged_from,
            "reanalyze_count": self.reanalyze_count,
            "original_sample_id": self.original_sample_id,
            "seed_model": self.seed_model,
            "weather_file": self.weather_file,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> DataPoint:
        return DataPoint(
            sample_id=d["sample_id"],
            status=DataPointStatus(d.get("status", "pending")),
            priority=d.get("priority", 0),
            work_dir=Path(d["work_dir"]) if d.get("work_dir") else None,
            created_at=d.get("created_at", time.time()),
            updated_at=d.get("updated_at", time.time()),
            completed_at=d.get("completed_at"),
            error_summary=d.get("error_summary"),
            merged_into=d.get("merged_into"),
            merged_from=d.get("merged_from", []),
            reanalyze_count=d.get("reanalyze_count", 0),
            original_sample_id=d.get("original_sample_id"),
            seed_model=d.get("seed_model"),
            weather_file=d.get("weather_file"),
        )


class DataPointManager:
    """Manage data point lifecycle within a campaign ``outdir``.

    State is persisted to ``{outdir}/.osimflow/data_points.json`` on every
    mutation so the manager survives process restarts.

    Parameters
    ----------
    outdir
        Campaign output directory (same as ``--outdir``).
    """

    STATE_FILE = ".osimflow/data_points.json"
    LOCK_FILE = ".osimflow/data_points.lock"

    def __init__(self, outdir: Path) -> None:
        self.outdir = Path(outdir)
        self._state_file = self.outdir / self.STATE_FILE
        self._lock_file = self.outdir / self.LOCK_FILE
        self._data_points: dict[str, DataPoint] = {}
        self._load()

    # -------------------------------------------------------------------------
    # Persistence
    # -------------------------------------------------------------------------

    def _load(self) -> None:
        if self._state_file.exists():
            try:
                raw = json.loads(self._state_file.read_text())
                self._data_points = {sid: DataPoint.from_dict(rec) for sid, rec in raw.items()}
            except (json.JSONDecodeError, KeyError, TypeError) as exc:
                log.warning("Corrupt data_points.json, starting fresh: %s", exc)
                self._data_points = {}

    def _lock(self) -> fasteners.InterProcessLock:
        """Return an InterProcessLock for the data points state file.

        Uses a dedicated lock file to avoid conflicts with the state file
        itself. The lock supports timeout to prevent indefinite blocking if
        the lock-holder crashes.
        """
        return fasteners.InterProcessLock(str(self._lock_file))

    def _save_atomic(self) -> None:
        self.outdir.mkdir(parents=True, exist_ok=True)
        (self.outdir / ".osimflow").mkdir(parents=True, exist_ok=True)
        tmp = self.outdir / ".osimflow" / f".tmp.{uuid.uuid4().hex}"
        try:
            tmp.write_text(
                json.dumps({sid: dp.to_dict() for sid, dp in self._data_points.items()}, indent=2)
            )
            os.rename(tmp, self._state_file)
        finally:
            if tmp.exists():
                tmp.unlink()

    # -------------------------------------------------------------------------
    # CRUD
    # -------------------------------------------------------------------------

    def register(
        self,
        sample_id: str,
        work_dir: Path | None = None,
        priority: int = 0,
    ) -> DataPoint:
        """Register a new data point (or update priority/work_dir of existing)."""
        now = time.time()
        if sample_id in self._data_points:
            dp = self._data_points[sample_id]
            dp.priority = priority
            if work_dir is not None:
                dp.work_dir = work_dir
            dp.updated_at = now
        else:
            dp = DataPoint(
                sample_id=sample_id,
                priority=priority,
                work_dir=work_dir,
                created_at=now,
                updated_at=now,
            )
            self._data_points[sample_id] = dp
        self._save_atomic()
        return dp

    def get(self, sample_id: str) -> DataPoint | None:
        """Return a data point by id, or None if not registered."""
        return self._data_points.get(sample_id)

    # -------------------------------------------------------------------------
    # Per-sample overrides (GAP-009)
    # -------------------------------------------------------------------------

    def with_seed_model(self, sample_id: str, path: Path) -> DataPoint:
        """Set a per-sample seed model override.

        When set, this data point will use *path* as its seed model
        instead of the campaign-level ``template_sim_package``. This enables
        multi-archetype studies where different samples use different
        building models in a single campaign.

        Auto-registers the data point if it does not exist.

        Args:
            sample_id: the sample's identifier.
            path: absolute path to the per-sample seed model directory.

        Returns:
            The updated ``DataPoint``.
        """
        if sample_id not in self._data_points:
            self.register(sample_id)
        dp = self._data_points[sample_id]
        dp.seed_model = str(path)
        dp.updated_at = time.time()
        self._save_atomic()
        return dp

    def with_weather_file(self, sample_id: str, path: Path) -> DataPoint:
        """Set a per-sample weather file override.

        When set, this data point will use *path* as its weather file
        instead of the campaign-level default. This enables multi-climate-zone
        studies where different samples use different EPW files in a single
        campaign.

        Auto-registers the data point if it does not exist.

        Args:
            sample_id: the sample's identifier.
            path: absolute path to the per-sample ``.epw`` weather file.

        Returns:
            The updated ``DataPoint``.
        """
        if sample_id not in self._data_points:
            self.register(sample_id)
        dp = self._data_points[sample_id]
        dp.weather_file = str(path)
        dp.updated_at = time.time()
        self._save_atomic()
        return dp

    def list_all(self) -> list[DataPoint]:
        """Return all registered data points sorted by sample_id."""
        return sorted(self._data_points.values(), key=lambda dp: dp.sample_id)

    def list_by_status(self, status: DataPointStatus) -> list[DataPoint]:
        """Return all data points with the given status."""
        return [dp for dp in self._data_points.values() if dp.status == status]

    def list_pending(self) -> list[DataPoint]:
        """Return pending data points ordered by priority (highest first)."""
        pending = self.list_by_status(DataPointStatus.PENDING)
        pending.sort(key=lambda dp: dp.priority, reverse=True)
        return pending

    def update_status(
        self,
        sample_id: str,
        status: DataPointStatus,
        error_summary: str | None = None,
    ) -> DataPoint:
        """Update the status of a data point.

        If the data point is not yet registered, it will be auto-registered
        first with work_dir=None. This makes update_status idempotent and
        eliminates the need for callers to explicitly register before
        updating status — particularly useful in exception-handling paths
        where registration may not have occurred yet.
        """
        if sample_id not in self._data_points:
            self.register(sample_id)
        dp = self._data_points[sample_id]
        dp.status = status
        dp.updated_at = time.time()
        if status in (DataPointStatus.COMPLETED, DataPointStatus.FAILED, DataPointStatus.CANCELLED):
            dp.completed_at = time.time()
        if error_summary:
            dp.error_summary = error_summary
        self._save_atomic()
        return dp

    def cancel(self, sample_id: str) -> DataPoint:
        """Cancel a data point (marks as CANCELLED)."""
        return self.update_status(sample_id, DataPointStatus.CANCELLED)

    def set_priority(self, sample_id: str, priority: int) -> DataPoint:
        """Update the priority of a data point."""
        dp = self._data_points[sample_id]
        dp.priority = priority
        dp.updated_at = time.time()
        self._save_atomic()
        return dp

    def unregister(self, sample_id: str) -> None:
        """Remove a data point from tracking (does not delete work_dir)."""
        if sample_id in self._data_points:
            del self._data_points[sample_id]
            self._save_atomic()

    # -------------------------------------------------------------------------
    # Reanalysis (#420)
    # -------------------------------------------------------------------------

    def mark_for_reanalysis(self, sample_id: str) -> DataPoint:
        """Mark a completed/failed data point for re-running.

        Creates a new data point with the same parameters but a new sample_id
        (``{original}_reanalyze_{n}``) and increments the parent's
        ``reanalyze_count``.

        Uses file locking to ensure an atomic compare-and-swap: the in-memory
        state is discarded and re-read from disk after acquiring the exclusive
        lock, so if another process modified the data point between our
        initial read and our write, we detect the stale state and raise
        ``ValueError`` instead of silently overwriting.
        """
        self.outdir.mkdir(parents=True, exist_ok=True)
        (self.outdir / ".osimflow").mkdir(parents=True, exist_ok=True)
        if not self._state_file.exists():
            self._state_file.write_text("{}")

        MAX_RETRIES = 5
        for attempt in range(MAX_RETRIES):
            lock = self._lock()
            acquired = lock.acquire_lock(timeout=30)
            if not acquired:
                if attempt < MAX_RETRIES - 1:
                    time.sleep(0.05 * (attempt + 1))
                    continue
                raise TimeoutError(f"Could not acquire lock for reanalysis of {sample_id!r}")
            try:
                fh = self._state_file.open("r")
                try:
                    self._load()

                    original = self._data_points.get(sample_id)
                    if original is None:
                        raise KeyError(f"Data point {sample_id!r} not found")

                    if original.status not in (
                        DataPointStatus.COMPLETED,
                        DataPointStatus.FAILED,
                    ):
                        raise ValueError(
                            f"Cannot reanalysis {sample_id!r}: status is "
                            f"{original.status.value}, must be completed "
                            "or failed"
                        )

                    new_id = f"{sample_id}_reanalyze_{original.reanalyze_count + 1}"
                    original.reanalyze_count += 1
                    original.updated_at = time.time()

                    new_dp = DataPoint(
                        sample_id=new_id,
                        status=DataPointStatus.PENDING,
                        priority=original.priority,
                        work_dir=original.work_dir,
                        created_at=time.time(),
                        updated_at=time.time(),
                        reanalyze_count=0,
                        original_sample_id=sample_id,
                        seed_model=original.seed_model,
                        weather_file=original.weather_file,
                    )
                    self._data_points[new_id] = new_dp
                    self._save_atomic()
                    return new_dp
                finally:
                    fh.close()
            finally:
                lock.release_lock()
        raise RuntimeError("Unexpected exit from mark_for_reanalysis retry loop")

    # -------------------------------------------------------------------------
    # Merging (#418)
    # -------------------------------------------------------------------------

    def merge(
        self,
        source_ids: list[str],
        target_id: str,
        target_work_dir: Path,
    ) -> DataPoint:
        """Merge multiple data points into a single target.

        All source data points are marked as MERGED and linked to the target.
        The target is registered if not already present.
        """
        if not source_ids:
            raise ValueError("merge requires at least one source_id")

        self.outdir.mkdir(parents=True, exist_ok=True)
        (self.outdir / ".osimflow").mkdir(parents=True, exist_ok=True)
        if not self._state_file.exists():
            self._state_file.write_text("{}")

        MAX_RETRIES = 5
        for attempt in range(MAX_RETRIES):
            lock = self._lock()
            acquired = lock.acquire_lock(timeout=30)
            if not acquired:
                if attempt < MAX_RETRIES - 1:
                    time.sleep(0.05 * (attempt + 1))
                    continue
                raise TimeoutError("Could not acquire lock for merge operation")
            try:
                fh = self._state_file.open("r")
                try:
                    self._load()

                    for sid in source_ids:
                        if sid not in self._data_points:
                            raise KeyError(f"Unknown data point: {sid!r}")

                    if target_id in self._data_points:
                        target = self._data_points[target_id]
                        target.status = DataPointStatus.MERGED
                        target.merged_from = list(source_ids)
                        target.updated_at = time.time()
                    else:
                        target = DataPoint(
                            sample_id=target_id,
                            status=DataPointStatus.MERGED,
                            work_dir=target_work_dir,
                            merged_from=list(source_ids),
                            created_at=time.time(),
                            updated_at=time.time(),
                        )
                        self._data_points[target_id] = target

                    for sid in source_ids:
                        dp = self._data_points[sid]
                        dp.status = DataPointStatus.MERGED
                        dp.merged_into = target_id
                        dp.updated_at = time.time()

                    self._save_atomic()
                    return target
                finally:
                    fh.close()
            finally:
                lock.release_lock()
        raise RuntimeError("Unexpected exit from merge retry loop")

    def get_merge_graph(self) -> dict[str, list[str]]:
        """Return a dict mapping each non-merged data point to its sources."""
        merged: dict[str, list[str]] = {}
        for dp in self._data_points.values():
            if dp.merged_from:
                merged[dp.sample_id] = dp.merged_from
        return merged

    # -------------------------------------------------------------------------
    # Priority queue integration (#417)
    # -------------------------------------------------------------------------

    def reorder_pending(self, priority_updates: dict[str, int]) -> None:
        """Bulk-update priorities for a set of pending data points.

        Args:
            priority_updates: dict mapping sample_id -> new priority
        """
        self.outdir.mkdir(parents=True, exist_ok=True)
        (self.outdir / ".osimflow").mkdir(parents=True, exist_ok=True)
        if not self._state_file.exists():
            self._state_file.write_text("{}")

        MAX_RETRIES = 5
        for attempt in range(MAX_RETRIES):
            lock = self._lock()
            acquired = lock.acquire_lock(timeout=30)
            if not acquired:
                if attempt < MAX_RETRIES - 1:
                    time.sleep(0.05 * (attempt + 1))
                    continue
                raise TimeoutError("Could not acquire lock for reorder_pending operation")
            try:
                fh = self._state_file.open("r")
                try:
                    self._load()
                    for sid, prio in priority_updates.items():
                        if sid in self._data_points:
                            dp = self._data_points[sid]
                            if dp.status == DataPointStatus.PENDING:
                                dp.priority = prio
                                dp.updated_at = time.time()
                    self._save_atomic()
                finally:
                    fh.close()
            finally:
                lock.release_lock()

    def summary(self) -> dict[str, int]:
        """Return a count summary by status."""
        counts: dict[str, int] = {s.value: 0 for s in DataPointStatus}
        for dp in self._data_points.values():
            counts[dp.status.value] += 1
        return counts
