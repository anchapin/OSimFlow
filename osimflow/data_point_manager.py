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

import json
import time
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
    """Immutable-ish record of a single simulation data point."""

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

    def __init__(self, outdir: Path) -> None:
        self.outdir = Path(outdir)
        self._state_file = self.outdir / self.STATE_FILE
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

    def _save(self) -> None:
        self.outdir.mkdir(parents=True, exist_ok=True)
        (self.outdir / ".osimflow").mkdir(parents=True, exist_ok=True)
        self._state_file.write_text(
            json.dumps({sid: dp.to_dict() for sid, dp in self._data_points.items()}, indent=2)
        )

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
        self._save()
        return dp

    def get(self, sample_id: str) -> DataPoint | None:
        """Return a data point by id, or None if not registered."""
        return self._data_points.get(sample_id)

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
            # Auto-register if not found — work_dir will be set properly
            # if/when the sample is re-registered with real path info.
            self.register(sample_id)
        dp = self._data_points[sample_id]
        dp.status = status
        dp.updated_at = time.time()
        if status in (DataPointStatus.COMPLETED, DataPointStatus.FAILED, DataPointStatus.CANCELLED):
            dp.completed_at = time.time()
        if error_summary:
            dp.error_summary = error_summary
        self._save()
        return dp

    def cancel(self, sample_id: str) -> DataPoint:
        """Cancel a data point (marks as CANCELLED)."""
        return self.update_status(sample_id, DataPointStatus.CANCELLED)

    def set_priority(self, sample_id: str, priority: int) -> DataPoint:
        """Update the priority of a data point."""
        dp = self._data_points[sample_id]
        dp.priority = priority
        dp.updated_at = time.time()
        self._save()
        return dp

    def unregister(self, sample_id: str) -> None:
        """Remove a data point from tracking (does not delete work_dir)."""
        if sample_id in self._data_points:
            del self._data_points[sample_id]
            self._save()

    # -------------------------------------------------------------------------
    # Reanalysis (#420)
    # -------------------------------------------------------------------------

    def mark_for_reanalysis(self, sample_id: str) -> DataPoint:
        """Mark a completed/failed data point for re-running.

        Creates a new data point with the same parameters but a new sample_id
        (``{original}_reanalyze_{n}``) and increments the parent's
        ``reanalyze_count``.
        """
        original = self._data_points[sample_id]
        if original.status not in (
            DataPointStatus.COMPLETED,
            DataPointStatus.FAILED,
        ):
            raise ValueError(
                f"Cannot reanalysis {sample_id!r}: status is {original.status.value}, "
                "must be completed or failed"
            )

        new_id = f"{sample_id}_reanalyze_{original.reanalyze_count + 1}"
        original.reanalyze_count += 1
        original.updated_at = time.time()

        new_dp = DataPoint(
            sample_id=new_id,
            status=DataPointStatus.PENDING,
            priority=original.priority,
            work_dir=original.work_dir,  # will be overwritten on re-run
            created_at=time.time(),
            updated_at=time.time(),
            reanalyze_count=0,
            original_sample_id=sample_id,
        )
        self._data_points[new_id] = new_dp
        self._save()
        return new_dp

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

        for sid in source_ids:
            if sid not in self._data_points:
                raise KeyError(f"Unknown data point: {sid!r}")

        # Register or update target
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

        # Mark all sources as merged_into target
        for sid in source_ids:
            dp = self._data_points[sid]
            dp.status = DataPointStatus.MERGED
            dp.merged_into = target_id
            dp.updated_at = time.time()

        self._save()
        return target

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
        for sid, prio in priority_updates.items():
            if sid in self._data_points:
                dp = self._data_points[sid]
                if dp.status == DataPointStatus.PENDING:
                    dp.priority = prio
                    dp.updated_at = time.time()
        self._save()

    def summary(self) -> dict[str, int]:
        """Return a count summary by status."""
        counts: dict[str, int] = {s.value: 0 for s in DataPointStatus}
        for dp in self._data_points.values():
            counts[dp.status.value] += 1
        return counts
