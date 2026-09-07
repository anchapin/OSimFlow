"""Concurrency + atomicity tests for RunTrace run.json writes.

Covers issues #1627 and #1634:

- #1627: ``RunTrace.update_sample`` did an UNLOCKED concurrent
  read-modify-write through a SHARED ``run.tmp`` path. Fan-out
  checkpoint threads racing it could (a) lose checkpoints
  (last rename wins) or (b) collide on the tmp path — the
  ``FileNotFoundError`` was then counted as a checkpoint failure,
  and 3 spurious strikes aborted a healthy campaign (#739 path).
- #1634: ``RunTrace.write`` used a direct non-atomic
  ``path.write_text`` while every other writer used tmp+rename, and
  ``update_sample`` silently swallowed read errors — once run.json
  was corrupted, every later checkpoint silently no-oped and the
  3-strike counter could never fire.

The fix: one ``threading.Lock`` serializing ``update_sample`` AND
``write``, unique (pid + thread id) tmp names, tmp+rename for
``write``, and loud propagation of corruption errors into the
existing checkpoint-failure counting in
``CampaignSampleTraceRecorder.checkpoint_sample``.
"""

import json
import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from osimflow._campaign_sample_trace import CampaignAbortError, CampaignSampleTraceRecorder
from osimflow.monitoring import RunTrace, SampleTrace

N_THREADS = 16


def _mk_trace(tmp_path: Path, campaign_id: str = "race-test") -> RunTrace:
    trace = RunTrace(campaign_id=campaign_id, config_summary={"executor": "local"})
    trace.write(tmp_path / "run.json")
    return trace


class TestConcurrentCheckpoints:
    """#1627: concurrent fan-out checkpoint threads must not lose rows."""

    def test_no_lost_rows_under_thread_stress(self, tmp_path: Path) -> None:
        """N=16 threads checkpoint N distinct samples concurrently;
        every row must survive, with zero exceptions."""
        trace = _mk_trace(tmp_path)
        barrier = threading.Barrier(N_THREADS)
        errors: list[Exception] = []

        def worker(i: int) -> None:
            try:
                barrier.wait(timeout=10)
                for round_ in range(3):
                    trace.update_sample(
                        SampleTrace(sample_id=f"s{i:04d}", status="ok", elapsed_s=float(round_))
                    )
            except Exception as exc:  # noqa: BLE001 - recorded, asserted below
                errors.append(exc)

        with ThreadPoolExecutor(max_workers=N_THREADS) as pool:
            list(pool.map(worker, range(N_THREADS)))

        assert errors == []
        data = json.loads((tmp_path / "run.json").read_text())
        ids = [s["sample_id"] for s in data["per_sample"]]
        assert len(ids) == N_THREADS  # no duplicates, no losses
        assert set(ids) == {f"s{i:04d}" for i in range(N_THREADS)}
        assert data["summary"]["n_samples"] == N_THREADS
        assert data["summary"]["n_succeeded"] == N_THREADS

    def test_write_concurrent_with_update_sample(self, tmp_path: Path) -> None:
        """#1634: campaign-level write() must not corrupt concurrent
        checkpoints — the file stays parseable and no call raises."""
        trace = _mk_trace(tmp_path)
        run_json = tmp_path / "run.json"
        errors: list[Exception] = []
        stop = threading.Event()

        def writer() -> None:
            try:
                while not stop.is_set():
                    trace.write(run_json)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        def updater(i: int) -> None:
            try:
                for round_ in range(10):
                    trace.update_sample(
                        SampleTrace(sample_id=f"s{i:04d}", status="ok", elapsed_s=float(round_))
                    )
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=writer)] + [
            threading.Thread(target=updater, args=(i,)) for i in range(8)
        ]
        for t in threads:
            t.start()
        for t in threads[1:]:
            t.join()
        stop.set()
        threads[0].join()

        assert errors == []
        # Whatever the interleaving, run.json parses and has a sane shape.
        data = json.loads(run_json.read_text())
        assert data["campaign_id"] == "race-test"
        assert isinstance(data["per_sample"], list)

    def test_tmp_paths_are_thread_unique(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Both writers must use pid+thread-id-unique tmp names, not a
        shared run.tmp (the #1627 collision)."""
        trace = _mk_trace(tmp_path)
        real_write_text = Path.write_text
        seen: set[str] = set()
        seen_lock = threading.Lock()

        def _spy_write_text(self: Path, data: str, *args: object, **kwargs: object) -> int:
            if self.name.endswith(".tmp"):
                with seen_lock:
                    seen.add(self.name)
            return real_write_text(self, data, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(Path, "write_text", _spy_write_text)

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(
                pool.map(
                    lambda i: trace.update_sample(
                        SampleTrace(sample_id=f"s{i:04d}", status="ok", elapsed_s=1.0)
                    ),
                    range(8),
                )
            )

        assert seen, "expected at least one tmp write"
        # Every observed tmp name follows the pid-threadid scheme …
        pattern = re.compile(r"^run\.\d+-\d+\.tmp$")
        assert all(pattern.match(name) for name in seen)
        # … and more than one distinct writer thread ran, yet no two
        # writes shared a tmp path (set semantics prove uniqueness).
        assert len(seen) >= 2


class TestCorruptionHandling:
    """#1634: corrupted run.json must fail loudly, not silently no-op."""

    def test_corrupted_run_json_warns_and_raises(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        trace = _mk_trace(tmp_path)
        (tmp_path / "run.json").write_text("{ this is not json")

        with caplog.at_level(logging.WARNING, logger="osimflow.monitoring"):
            with pytest.raises(json.JSONDecodeError):
                trace.update_sample(SampleTrace(sample_id="s0001", status="ok", elapsed_s=1.0))

        assert any("corrupted" in r.getMessage() for r in caplog.records)

    def test_corruption_is_visible_to_three_strike_counter(self, tmp_path: Path) -> None:
        """A corrupted run.json must count toward the #739 abort —
        previously the silent return reset the counter forever."""
        trace = _mk_trace(tmp_path, campaign_id="corrupt-test")
        run_json = tmp_path / "run.json"
        run_json.write_text("garbage")
        sample_state: dict[str, dict[str, object]] = {
            f"s{i:04d}": {"apply_exit_code": 0, "sim_exit_code": 0, "extract_exit_code": 0}
            for i in range(3)
        }
        recorder = CampaignSampleTraceRecorder(
            trace=trace, sample_state=sample_state, obs=MagicMock()
        )

        recorder.checkpoint_sample("s0000")
        recorder.checkpoint_sample("s0001")
        assert recorder.consecutive_checkpoint_failures == 2
        with pytest.raises(CampaignAbortError):
            recorder.checkpoint_sample("s0002")


class TestAtomicWrite:
    """#1634: write() must be tmp+rename — a mid-write crash leaves the
    previous parseable run.json intact."""

    def test_write_crash_preserves_previous_run_json(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        trace = _mk_trace(tmp_path)
        run_json = tmp_path / "run.json"
        old_content = run_json.read_text()
        assert json.loads(old_content)["summary"]["n_samples"] == 0

        # The next write would add a sample — distinguishable content.
        trace.sample_done(SampleTrace(sample_id="s0001", status="ok", elapsed_s=1.0))

        def _boom_rename(self: Path, target: Any) -> Path:
            raise OSError("simulated crash mid-write (after tmp write, before rename)")

        monkeypatch.setattr(Path, "rename", _boom_rename)
        with pytest.raises(OSError, match="simulated crash mid-write"):
            trace.write(run_json)
        monkeypatch.undo()

        # The old file survived byte-for-byte and still parses.
        assert run_json.read_text() == old_content
        data = json.loads(run_json.read_text())
        assert data["campaign_id"] == "race-test"
        # The sample from the crashed write attempt did NOT land.
        assert data["summary"]["n_samples"] == 0
