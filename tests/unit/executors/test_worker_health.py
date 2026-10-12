"""Unit tests for worker health checks (issue #341).

Covers:
- WorkerHeartbeat: construction, start/stop, state update, path structure
- check_heartbeat: stale detection, fresh heartbeat, missing file
- HEARTBEAT_INTERVAL_SEC and STALE_THRESHOLD_SEC constants
"""

import json
from datetime import UTC, datetime
from pathlib import Path

from osimflow.monitoring import (
    HEARTBEAT_INTERVAL_SEC,
    STALE_THRESHOLD_SEC,
    WorkerHeartbeat,
    check_heartbeat,
)


class TestWorkerHeartbeatConstants:
    def test_heartbeat_interval_is_30(self) -> None:
        assert HEARTBEAT_INTERVAL_SEC == 30

    def test_stale_threshold_is_60(self) -> None:
        assert STALE_THRESHOLD_SEC == 60


class TestWorkerHeartbeatConstruction:
    def test_default_values(self, tmp_path: Path) -> None:
        hb = WorkerHeartbeat(outdir=tmp_path, sample_id="s0001", worker_id="local")
        assert hb.sample_id == "s0001"
        assert hb.worker_id == "local"
        assert hb.job_handle_state == "running"
        assert hb._path is None

    def test_custom_job_handle_state(self, tmp_path: Path) -> None:
        hb = WorkerHeartbeat(
            outdir=tmp_path,
            sample_id="s0001",
            worker_id="slurm-12345",
            job_handle_state="pending",
        )
        assert hb.job_handle_state == "pending"


class TestWorkerHeartbeatPath:
    def test_heartbeat_path_structure(self, tmp_path: Path) -> None:
        hb = WorkerHeartbeat(outdir=tmp_path, sample_id="s0042", worker_id="local")
        path = hb._heartbeat_path()
        assert path == tmp_path / "work" / "sim" / "s0042" / "heartbeat.json"

    def test_creates_parent_directories(self, tmp_path: Path) -> None:
        hb = WorkerHeartbeat(outdir=tmp_path, sample_id="s0042", worker_id="local")
        path = hb._heartbeat_path()
        assert path.parent.exists()


class TestWorkerHeartbeatWrite:
    def test_write_produces_valid_json(self, tmp_path: Path) -> None:
        hb = WorkerHeartbeat(outdir=tmp_path, sample_id="s0001", worker_id="local")
        hb._write()
        path = hb._heartbeat_path()
        assert path.exists()
        data = json.loads(path.read_text())
        assert data["worker_id"] == "local"
        assert data["sample_id"] == "s0001"
        assert data["job_handle"] == "running"
        assert "last_seen" in data

    def test_write_overwrites_previous(self, tmp_path: Path) -> None:
        hb = WorkerHeartbeat(outdir=tmp_path, sample_id="s0001", worker_id="local")
        hb._write()
        hb.job_handle_state = "completed"
        hb._write()
        path = hb._heartbeat_path()
        data = json.loads(path.read_text())
        assert data["job_handle"] == "completed"


class TestWorkerHeartbeatStartStop:
    def test_start_spawns_thread(self, tmp_path: Path) -> None:
        hb = WorkerHeartbeat(outdir=tmp_path, sample_id="s0001", worker_id="local")
        hb.start()
        assert hb._thread is not None
        assert hb._thread.daemon is True
        hb.stop()

    def test_stop_writes_final_entry(self, tmp_path: Path) -> None:
        hb = WorkerHeartbeat(outdir=tmp_path, sample_id="s0001", worker_id="local")
        hb.start()
        hb.stop()
        path = hb._heartbeat_path()
        data = json.loads(path.read_text())
        assert data["job_handle"] == "stopped"

    def test_stop_joins_without_hanging(self, tmp_path: Path) -> None:
        hb = WorkerHeartbeat(outdir=tmp_path, sample_id="s0001", worker_id="local")
        hb.start()
        hb.stop()
        assert not hb._thread.is_alive()


class TestWorkerHeartbeatUpdateState:
    def test_update_state_writes_immediately(self, tmp_path: Path) -> None:
        hb = WorkerHeartbeat(outdir=tmp_path, sample_id="s0001", worker_id="local")
        hb._write()
        hb.update_state("completed")
        path = hb._heartbeat_path()
        data = json.loads(path.read_text())
        assert data["job_handle"] == "completed"


class TestCheckHeartbeat:
    def test_missing_file_is_stale(self, tmp_path: Path) -> None:
        assert check_heartbeat(tmp_path, "nonexistent") is True

    def test_fresh_heartbeat_is_not_stale(self, tmp_path: Path) -> None:
        d = tmp_path / "work" / "sim" / "s0001"
        d.mkdir(parents=True)
        hb_path = d / "heartbeat.json"
        hb_path.write_text(
            json.dumps(
                {
                    "worker_id": "local",
                    "sample_id": "s0001",
                    "last_seen": datetime.now(UTC).isoformat(),
                    "job_handle": "running",
                }
            )
        )
        assert check_heartbeat(tmp_path, "s0001") is False

    def test_stale_heartbeat_is_detected(self, tmp_path: Path) -> None:
        d = tmp_path / "work" / "sim" / "s0001"
        d.mkdir(parents=True)
        hb_path = d / "heartbeat.json"
        old_time = datetime.now(UTC).timestamp() - STALE_THRESHOLD_SEC - 10
        old_time_str = datetime.fromtimestamp(old_time, tz=UTC).isoformat()
        hb_path.write_text(
            json.dumps(
                {
                    "worker_id": "local",
                    "sample_id": "s0001",
                    "last_seen": old_time_str,
                    "job_handle": "running",
                }
            )
        )
        assert check_heartbeat(tmp_path, "s0001") is True

    def test_invalid_json_is_stale(self, tmp_path: Path) -> None:
        d = tmp_path / "work" / "sim" / "s0001"
        d.mkdir(parents=True)
        hb_path = d / "heartbeat.json"
        hb_path.write_text("not valid json")
        assert check_heartbeat(tmp_path, "s0001") is True

    def test_missing_last_seen_is_stale(self, tmp_path: Path) -> None:
        d = tmp_path / "work" / "sim" / "s0001"
        d.mkdir(parents=True)
        hb_path = d / "heartbeat.json"
        hb_path.write_text(json.dumps({"worker_id": "local", "sample_id": "s0001"}))
        assert check_heartbeat(tmp_path, "s0001") is True

    def test_last_seen_as_iso_string(self, tmp_path: Path) -> None:
        d = tmp_path / "work" / "sim" / "s0001"
        d.mkdir(parents=True)
        hb_path = d / "heartbeat.json"
        hb_path.write_text(
            json.dumps(
                {
                    "worker_id": "local",
                    "sample_id": "s0001",
                    "last_seen": datetime.now(UTC).isoformat(),
                    "job_handle": "running",
                }
            )
        )
        assert check_heartbeat(tmp_path, "s0001") is False
