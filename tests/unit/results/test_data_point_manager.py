"""Unit tests for the DataPointManager (issues #418, #419, #420)."""

from __future__ import annotations

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

    def test_update_status_sets_completed_at(self, mgr: DataPointManager) -> None:
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

    def test_list_pending_ordered_by_priority(self, mgr: DataPointManager) -> None:
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
    def test_mark_for_reanalysis_completed(self, mgr: DataPointManager) -> None:
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

    def test_mark_for_reanalysis_requires_completed_or_failed(self, mgr: DataPointManager) -> None:
        mgr.register("sample_000")
        with pytest.raises(ValueError, match="must be completed or failed"):
            mgr.mark_for_reanalysis("sample_000")

    def test_mark_for_reanalysis_increments_count(self, mgr: DataPointManager) -> None:
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


class TestDataPointManagerConcurrency:
    """Cross-process safety of read-modify-write cycles (issue #1090)."""

    def test_no_lost_update_between_manager_instances(self, tmpdir: Path) -> None:
        """Two managers on a shared outdir must not clobber each other.

        Reproduces the lost-update: p2 is constructed BEFORE p1 writes, so
        its in-memory snapshot is stale. Without the locked refresh in
        _locked_rmw, p2's save would erase p1's status change.
        """
        p1 = DataPointManager(tmpdir)
        p1.register("a")
        p1.register("b")

        p2 = DataPointManager(tmpdir)  # stale snapshot from here on
        p1.update_status("a", DataPointStatus.RUNNING)
        p2.update_status("b", DataPointStatus.COMPLETED)

        final = DataPointManager(tmpdir)
        assert final.get("a").status == DataPointStatus.RUNNING
        assert final.get("b").status == DataPointStatus.COMPLETED

    def test_interleaved_updates_all_survive(self, tmpdir: Path) -> None:
        """Many managers updating disjoint samples concurrently — all persist."""
        import threading

        writers = [DataPointManager(tmpdir) for _ in range(4)]
        for i in range(8):
            writers[i % 4].register(f"s{i}")
        errors: list[Exception] = []

        def run(w: DataPointManager, base: int) -> None:
            try:
                # Each thread owns two disjoint samples; interleaving across
                # threads still exercises concurrent lock acquisition.
                for _j in range(20):
                    w.update_status(f"s{base}", DataPointStatus.RUNNING)
                    w.update_status(f"s{base + 1}", DataPointStatus.RUNNING)
                    w.update_status(f"s{base}", DataPointStatus.COMPLETED)
                    w.update_status(f"s{base + 1}", DataPointStatus.COMPLETED)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [
            threading.Thread(target=run, args=(writers[k], k * 2)) for k in range(len(writers))
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        final = DataPointManager(tmpdir)
        for i in range(8):
            assert final.get(f"s{i}").status == DataPointStatus.COMPLETED

    def test_failed_write_leaves_previous_state_intact(self, mgr: DataPointManager) -> None:
        """A crash mid-write (os.replace raises) keeps the last good state file."""
        import json as json_mod
        from unittest.mock import patch

        mgr.register("keep", priority=1)

        real_replace = __import__("os").replace

        def boom(src: str, dst: str) -> None:
            raise OSError("simulated crash mid-write")

        with patch("os.replace", side_effect=boom):
            with pytest.raises(OSError):
                mgr.register("doomed", priority=9)

        # State file still holds the last successful write.
        raw = json_mod.loads((mgr.outdir / DataPointManager.STATE_FILE).read_text())
        assert "keep" in raw
        assert "doomed" not in raw
        assert real_replace  # keep the import referenced

    def test_save_atomic_uses_replace(self, mgr: DataPointManager) -> None:
        """Persistence goes through os.replace (atomic overwrite), not os.rename."""
        import inspect

        src = inspect.getsource(DataPointManager._save_atomic)
        assert "os.replace" in src
        assert "os.rename" not in src


class TestDataPointManagerCrossProcess:
    """True multi-process exclusion (the NFS/GPFS scenario from issue #1090)."""

    def test_no_lost_update_across_processes(self, tmpdir: Path) -> None:
        """Child processes with stale snapshots must not clobber each other."""
        import multiprocessing

        mgr = DataPointManager(tmpdir)
        for i in range(6):
            mgr.register(f"p{i}")

        ctx = multiprocessing.get_context("fork")

        def worker(outdir: str, idx: int) -> None:
            # Constructed AFTER the parent registered everything, but each
            # child's snapshot goes stale as soon as a sibling writes.
            w = DataPointManager(Path(outdir))
            for _ in range(15):
                w.update_status(f"p{idx}", DataPointStatus.RUNNING)
            w.update_status(f"p{idx}", DataPointStatus.COMPLETED)

        procs = [ctx.Process(target=worker, args=(str(tmpdir), i), name=f"w{i}") for i in range(6)]
        for p in procs:
            p.start()
        for p in procs:
            p.join()
            assert p.exitcode == 0

        final = DataPointManager(tmpdir)
        statuses = {f"p{i}": final.get(f"p{i}").status for i in range(6)}
        assert all(s == DataPointStatus.COMPLETED for s in statuses.values()), statuses
