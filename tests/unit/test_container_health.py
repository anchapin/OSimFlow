"""Unit tests for container health monitoring (issue #415)."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from osimflow.work import (
    HEARTBEAT_FILENAME,
    SimulationHealth,
    SimulationHealthStatus,
    _heartbeat_writer,
    _write_heartbeat,
    check_container_health,
)


@pytest.fixture
def sim_out(tmp_path: Path) -> Path:
    d = tmp_path / "sample_000"
    d.mkdir(parents=True)
    return d


class TestWriteHeartbeat:
    def test_writes_heartbeat_file(self, sim_out: Path) -> None:
        _write_heartbeat(sim_out, pid=12345, sample_id="sample_000", version="3.11.0")
        hb = sim_out / HEARTBEAT_FILENAME
        assert hb.is_file()
        data = json.loads(hb.read_text(encoding="utf-8"))
        assert data["pid"] == 12345
        assert data["sample_id"] == "sample_000"
        assert data["version"] == "3.11.0"
        assert "timestamp" in data

    def test_missing_sim_out_creates_parent(self, tmp_path: Path) -> None:
        child = tmp_path / "deeply" / "nested" / "sample"
        _write_heartbeat(child, pid=1, sample_id="x", version="1.0")
        assert (child / HEARTBEAT_FILENAME).is_file()

    def test_write_error_is_swallowed(self, sim_out: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import osimflow.work as w

        def _fail_write(self: Path, *args: object, **kwargs: object) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(w.Path, "write_text", _fail_write)
        # Should not raise — error is logged and swallowed
        _write_heartbeat(sim_out, pid=1, sample_id="x", version="1.0")


class TestCheckContainerHealth:
    def test_unknown_when_no_file(self, sim_out: Path) -> None:
        health = check_container_health(sim_out)
        assert health.status == SimulationHealthStatus.UNKNOWN
        assert "no heartbeat file" in health.message

    def test_healthy_when_fresh(self, sim_out: Path) -> None:
        hb = sim_out / HEARTBEAT_FILENAME
        hb.write_text(
            json.dumps({"pid": 1, "timestamp": time.time(), "sample_id": "s", "version": "v"}),
            encoding="utf-8",
        )
        health = check_container_health(sim_out, health_check_interval=60.0)
        assert health.status == SimulationHealthStatus.HEALTHY
        assert health.pid == 1

    def test_stale_when_too_old(self, sim_out: Path) -> None:
        hb = sim_out / HEARTBEAT_FILENAME
        hb.write_text(
            json.dumps(
                {
                    "pid": 42,
                    "timestamp": time.time() - 120,  # 120s ago
                    "sample_id": "s",
                    "version": "v",
                }
            ),
            encoding="utf-8",
        )
        health = check_container_health(sim_out, health_check_interval=60.0)
        assert health.status == SimulationHealthStatus.STALE
        assert health.pid == 42
        assert "120s old" in health.message

    def test_unknown_when_json_invalid(self, sim_out: Path) -> None:
        hb = sim_out / HEARTBEAT_FILENAME
        hb.write_text("not json {", encoding="utf-8")
        health = check_container_health(sim_out)
        assert health.status == SimulationHealthStatus.UNKNOWN
        assert "failed to read" in health.message

    def test_unknown_when_no_timestamp(self, sim_out: Path) -> None:
        hb = sim_out / HEARTBEAT_FILENAME
        hb.write_text(json.dumps({"pid": 1}), encoding="utf-8")
        health = check_container_health(sim_out)
        assert health.status == SimulationHealthStatus.UNKNOWN


class TestHeartbeatWriterThread:
    def test_writes_on_start_and_exit(self, sim_out: Path) -> None:
        """First beat is awaited via a hook event, not a timed wait (issue #1544)."""
        import osimflow.work as w

        original_interval = w.HEARTBEAT_INTERVAL_S
        original_write = w._write_heartbeat
        w.HEARTBEAT_INTERVAL_S = 0.05  # 50ms — write immediately on start
        first_beat = threading.Event()

        def _event_write(out: Path, pid: int, sample_id: str, version: str) -> None:
            original_write(out, pid, sample_id, version)
            first_beat.set()

        stop = threading.Event()
        try:
            with patch.object(w, "_write_heartbeat", _event_write):
                t = threading.Thread(
                    target=_heartbeat_writer,
                    args=(sim_out, "s", "v", stop),
                    daemon=True,
                )
                t.start()
                assert first_beat.wait(timeout=10.0), (
                    "heartbeat writer never produced its first beat"
                )
                stop.set()
                t.join(timeout=10.0)
                assert not t.is_alive()
        finally:
            w.HEARTBEAT_INTERVAL_S = original_interval
        hb = sim_out / HEARTBEAT_FILENAME
        assert hb.is_file()
        data = json.loads(hb.read_text(encoding="utf-8"))
        assert data["sample_id"] == "s"
        assert data["version"] == "v"

    def test_writes_multiple_beats(self, sim_out: Path) -> None:
        """The writer thread produces >=3 beats — counted, not timed (issue #1544).

        Deterministic synchronization: wrap ``_write_heartbeat`` with a
        counting hook and wait on an event set at the 3rd beat, with the
        interval at 0 so the loop beats as fast as it can write. No
        wall-clock sleep and no "~N beats in M seconds" timing assumption.
        """
        import osimflow.work as w

        original_interval = w.HEARTBEAT_INTERVAL_S
        original_write = w._write_heartbeat
        w.HEARTBEAT_INTERVAL_S = 0
        writes = 0
        third_beat = threading.Event()

        def _counting_write(out: Path, pid: int, sample_id: str, version: str) -> None:
            nonlocal writes
            writes += 1
            original_write(out, pid, sample_id, version)
            if writes >= 3:
                third_beat.set()

        stop = threading.Event()
        try:
            with patch.object(w, "_write_heartbeat", _counting_write):
                t = threading.Thread(
                    target=_heartbeat_writer,
                    args=(sim_out, "s", "v", stop),
                    daemon=True,
                )
                t.start()
                assert third_beat.wait(timeout=10.0), "heartbeat writer never produced a 3rd beat"
                stop.set()
                t.join(timeout=10.0)
                assert not t.is_alive()
        finally:
            w.HEARTBEAT_INTERVAL_S = original_interval
        data = json.loads((sim_out / HEARTBEAT_FILENAME).read_text(encoding="utf-8"))
        assert data["sample_id"] == "s"
        assert writes >= 3


class TestSimulationHealthDataclass:
    def test_healthy_roundtrip(self) -> None:
        h = SimulationHealth(
            status=SimulationHealthStatus.HEALTHY,
            last_heartbeat=123456.0,
            pid=99,
        )
        assert h.status == SimulationHealthStatus.HEALTHY
        assert h.last_heartbeat == 123456.0
        assert h.pid == 99
        assert h.message is None

    def test_stale_with_message(self) -> None:
        h = SimulationHealth(
            status=SimulationHealthStatus.STALE,
            last_heartbeat=100.0,
            pid=5,
            message="heartbeat is 300s old (threshold 60s)",
        )
        assert h.status == SimulationHealthStatus.STALE
        assert "300s old" in h.message
