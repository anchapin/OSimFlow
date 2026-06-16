"""Unit tests for the DataPointManager (issues #418, #419, #420)."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from osimflow.data_point_manager import (
    DataPoint,
    DataPointManager,
    DataPointStatus,
)


@pytest.fixture
def tmpdir():
    with tempfile.TemporaryDirectory() as td:
        yield Path(td)


@pytest.fixture
def mgr(tmpdir):
    return DataPointManager(tmpdir)


class TestDataPointManager_register:
    def test_register_new(self, mgr: DataPointManager) -> None:
        dp = mgr.register("sample_000", priority=5)
        assert dp.sample_id == "sample_000"
        assert dp.status == DataPointStatus.PENDING
        assert dp.priority == 5
        assert dp.work_dir is None

    def test_register_with_work_dir(self, mgr: DataPointManager, tmpdir: Path) -> None:
        wd = tmpdir / "work" / "sample_001"
        dp = mgr.register("sample_001", work_dir=wd, priority=10)
        assert dp.work_dir == wd
        assert dp.priority == 10

    def test_register_updates_existing(self, mgr: DataPointManager) -> None:
        mgr.register("sample_000", priority=1)
        dp = mgr.register("sample_000", priority=99)
        assert dp.priority == 99
        assert len(mgr.list_all()) == 1

    def test_persistence(self, mgr: DataPointManager, tmpdir: Path) -> None:
        mgr.register("sample_000", priority=7)
        # Load a new manager from the same dir
        mgr2 = DataPointManager(tmpdir)
        assert mgr2.get("sample_000").priority == 7

    def test_list_all(self, mgr: DataPointManager) -> None:
        mgr.register("c", priority=0)
        mgr.register("a", priority=10)
        mgr.register("b", priority=5)
        all_ids = [dp.sample_id for dp in mgr.list_all()]
        assert all_ids == ["a", "b", "c"]  # sorted by sample_id


class TestDataPointManager_status:
    def test_update_status(self, mgr: DataPointManager) -> None:
        mgr.register("sample_000")
        dp = mgr.update_status("sample_000", DataPointStatus.RUNNING)
        assert dp.status == DataPointStatus.RUNNING
        assert dp.completed_at is None

    def test_update_status_sets_completed_at(
        self, mgr: DataPointManager
    ) -> None:
        mgr.register("sample_000")
        dp = mgr.update_status("sample_000", DataPointStatus.COMPLETED)
        assert dp.status == DataPointStatus.COMPLETED
        assert dp.completed_at is not None

    def test_update_status_with_error(self, mgr: DataPointManager) -> None:
        mgr.register("sample_000")
        dp = mgr.update_status(
            "sample_000", DataPointStatus.FAILED, error_summary="Severe: Zone not found"
        )
        assert dp.error_summary == "Severe: Zone not found"

    def test_list_by_status(self, mgr: DataPointManager) -> None:
        mgr.register("a")
        mgr.register("b")
        mgr.register("c")
        mgr.update_status("b", DataPointStatus.COMPLETED)
        pending = mgr.list_by_status(DataPointStatus.PENDING)
        assert [dp.sample_id for dp in pending] == ["a", "c"]

    def test_cancel(self, mgr: DataPointManager) -> None:
        mgr.register("sample_000")
        dp = mgr.cancel("sample_000")
        assert dp.status == DataPointStatus.CANCELLED
        assert dp.completed_at is not None


class TestDataPointManager_priority:
    def test_set_priority(self, mgr: DataPointManager) -> None:
        mgr.register("sample_000", priority=1)
        dp = mgr.set_priority("sample_000", priority=999)
        assert dp.priority == 999

    def test_list_pending_ordered_by_priority(
        self, mgr: DataPointManager
    ) -> None:
        mgr.register("low", priority=1)
        mgr.register("high", priority=100)
        mgr.register("mid", priority=50)
        pending = mgr.list_pending()
        assert [dp.sample_id for dp in pending] == ["high", "mid", "low"]

    def test_reorder_pending_bulk(self, mgr: DataPointManager) -> None:
        mgr.register("a", priority=1)
        mgr.register("b", priority=2)
        mgr.register("c", priority=3)
        mgr.reorder_pending({"a": 300, "b": 200})
        assert mgr.get("a").priority == 300
        assert mgr.get("b").priority == 200
        assert mgr.get("c").priority == 3  # unchanged


class TestDataPointManager_reanalysis:
    def test_mark_for_reanalysis_completed(
        self, mgr: DataPointManager
    ) -> None:
        mgr.register("sample_000")
        mgr.update_status("sample_000", DataPointStatus.COMPLETED)
        new_dp = mgr.mark_for_reanalysis("sample_000")
        assert new_dp.sample_id == "sample_000_reanalyze_1"
        assert new_dp.original_sample_id == "sample_000"
        assert new_dp.status == DataPointStatus.PENDING
        assert mgr.get("sample_000").reanalyze_count == 1

    def test_mark_for_reanalysis_failed(self, mgr: DataPointManager) -> None:
        mgr.register("sample_000")
        mgr.update_status("sample_000", DataPointStatus.FAILED, error_summary="crash")
        new_dp = mgr.mark_for_reanalysis("sample_000")
        assert new_dp.sample_id == "sample_000_reanalyze_1"
        assert mgr.get("sample_000").reanalyze_count == 1

    def test_mark_for_reanalysis_requires_completed_or_failed(
        self, mgr: DataPointManager
    ) -> None:
        mgr.register("sample_000")
        with pytest.raises(ValueError, match="must be completed or failed"):
            mgr.mark_for_reanalysis("sample_000")

    def test_mark_for_reanalysis_increments_count(
        self, mgr: DataPointManager
    ) -> None:
        mgr.register("sample_000")
        mgr.update_status("sample_000", DataPointStatus.COMPLETED)
        mgr.mark_for_reanalysis("sample_000")
        mgr.mark_for_reanalysis("sample_000")
        assert mgr.get("sample_000").reanalyze_count == 2
        # New IDs are always created
        ids = [dp.sample_id for dp in mgr.list_all() if "reanalyze" in dp.sample_id]
        assert len(ids) == 2


class TestDataPointManager_merge:
    def test_merge_two_samples(self, mgr: DataPointManager, tmpdir: Path) -> None:
        mgr.register("a")
        mgr.register("b")
        target_dir = tmpdir / "merged"
        target_dir.mkdir()
        target = mgr.merge(["a", "b"], "merged_ab", target_dir)
        assert target.sample_id == "merged_ab"
        assert target.status == DataPointStatus.MERGED
        assert mgr.get("a").status == DataPointStatus.MERGED
        assert mgr.get("a").merged_into == "merged_ab"
        assert mgr.get("b").merged_into == "merged_ab"

    def test_merge_unknown_source_raises(self, mgr: DataPointManager, tmpdir: Path) -> None:
        mgr.register("a")
        with pytest.raises(KeyError, match="Unknown data point"):
            mgr.merge(["a", "nonexistent"], "merged", tmpdir / "merged")

    def test_merge_empty_raises(self, mgr: DataPointManager, tmpdir: Path) -> None:
        with pytest.raises(ValueError, match="at least one source"):
            mgr.merge([], "merged", tmpdir / "merged")

    def test_get_merge_graph(self, mgr: DataPointManager, tmpdir: Path) -> None:
        mgr.register("a")
        mgr.register("b")
        mgr.register("c")
        mgr.merge(["a", "b"], "ab", tmpdir / "ab")
        graph = mgr.get_merge_graph()
        assert graph == {"ab": ["a", "b"]}


class TestDataPointManager_summary:
    def test_summary_counts(self, mgr: DataPointManager) -> None:
        mgr.register("a")
        mgr.register("b")
        mgr.register("c")
        mgr.update_status("a", DataPointStatus.RUNNING)
        mgr.update_status("b", DataPointStatus.COMPLETED)
        summary = mgr.summary()
        assert summary["pending"] == 1
        assert summary["running"] == 1
        assert summary["completed"] == 1
        assert summary["failed"] == 0


class TestDataPointSerialization:
    def test_to_dict_roundtrip(self, mgr: DataPointManager) -> None:
        mgr.register("sample_000", priority=5)
        dp = mgr.get("sample_000")
        d = dp.to_dict()
        restored = DataPoint.from_dict(d)
        assert restored.sample_id == dp.sample_id
        assert restored.priority == dp.priority
        assert restored.status == dp.status
