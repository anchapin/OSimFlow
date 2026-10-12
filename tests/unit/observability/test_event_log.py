"""Tests for the append-only campaign event log (issue #396).

Covers:
- CampaignEventLog writes valid JSON Lines to events.jsonl
- All event types produce correct structure (ts, type, data, trace_id)
- Thread-safe concurrent writes
- read_event_log parses files correctly (including malformed lines)
- flush() and path property
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from osimflow.event_log import (
    CampaignEventLog,
    EventType,
    read_event_log,
)


class TestEventType:
    """EventType StrEnum values."""

    def test_all_event_types(self) -> None:
        expected = {
            "CAMPAIGN_STARTED",
            "STEP_STARTED",
            "STEP_COMPLETED",
            "SAMPLE_STARTED",
            "SAMPLE_COMPLETED",
            "SAMPLE_FAILED",
            "CAMPAIGN_COMPLETED",
            "CAMPAIGN_FAILED",
        }
        actual = {e.value for e in EventType}
        assert actual == expected

    def test_event_type_is_str(self) -> None:
        assert EventType.CAMPAIGN_STARTED == "CAMPAIGN_STARTED"


class TestCampaignEventLogEmit:
    """Tests for individual event emission methods."""

    @pytest.fixture
    def event_log(self, tmp_path: Path) -> CampaignEventLog:
        return CampaignEventLog(outdir=tmp_path)

    def test_campaign_started(self, event_log: CampaignEventLog) -> None:
        event_log.campaign_started(
            campaign_id="c1",
            executor="local",
            n_samples=10,
            algorithm="lhs",
            openstudio_version="3.11.0",
        )
        events = read_event_log(event_log.path)
        assert len(events) == 1
        e = events[0]
        assert e["type"] == "CAMPAIGN_STARTED"
        assert e["data"]["campaign_id"] == "c1"
        assert e["data"]["executor"] == "local"
        assert e["data"]["n_samples"] == 10
        assert e["data"]["algorithm"] == "lhs"
        assert e["data"]["openstudio_version"] == "3.11.0"
        assert "ts" in e
        assert e["trace_id"] is None

    def test_step_started(self, event_log: CampaignEventLog) -> None:
        event_log.step_started("c1", "RUN_OPENSTUDIO_SIM", generation=2, trace_id="abc123")
        events = read_event_log(event_log.path)
        assert len(events) == 1
        e = events[0]
        assert e["type"] == "STEP_STARTED"
        assert e["data"]["step"] == "RUN_OPENSTUDIO_SIM"
        assert e["data"]["generation"] == 2
        assert e["trace_id"] == "abc123"

    def test_step_started_default_generation(self, event_log: CampaignEventLog) -> None:
        event_log.step_started("c1", "APPLY_PARAMETERS")
        events = read_event_log(event_log.path)
        assert events[0]["data"]["generation"] == 0

    def test_step_completed(self, event_log: CampaignEventLog) -> None:
        event_log.step_completed(
            "c1", "EXTRACT_KPIS", elapsed_s=3.14159, cache_hit=True, generation=1
        )
        events = read_event_log(event_log.path)
        e = events[0]
        assert e["type"] == "STEP_COMPLETED"
        assert e["data"]["elapsed_s"] == 3.142  # rounded to 3 decimals
        assert e["data"]["cache_hit"] is True
        assert e["data"]["generation"] == 1

    def test_sample_started(self, event_log: CampaignEventLog) -> None:
        event_log.sample_started("c1", "RUN_OPENSTUDIO_SIM", "0001", trace_id="t1")
        events = read_event_log(event_log.path)
        assert events[0]["type"] == "SAMPLE_STARTED"
        assert events[0]["data"]["sample_id"] == "0001"

    def test_sample_completed(self, event_log: CampaignEventLog) -> None:
        event_log.sample_completed("c1", "RUN_OPENSTUDIO_SIM", "0002")
        events = read_event_log(event_log.path)
        assert events[0]["type"] == "SAMPLE_COMPLETED"
        assert events[0]["data"]["sample_id"] == "0002"

    def test_sample_failed(self, event_log: CampaignEventLog) -> None:
        event_log.sample_failed("c1", "RUN_OPENSTUDIO_SIM", "0003", reason="timeout")
        events = read_event_log(event_log.path)
        e = events[0]
        assert e["type"] == "SAMPLE_FAILED"
        assert e["data"]["reason"] == "timeout"

    def test_campaign_completed(self, event_log: CampaignEventLog) -> None:
        event_log.campaign_completed("c1", elapsed_s=42.5, n_succeeded=8, n_failed=2)
        events = read_event_log(event_log.path)
        e = events[0]
        assert e["type"] == "CAMPAIGN_COMPLETED"
        assert e["data"]["elapsed_s"] == 42.5
        assert e["data"]["n_succeeded"] == 8
        assert e["data"]["n_failed"] == 2

    def test_campaign_failed(self, event_log: CampaignEventLog) -> None:
        event_log.campaign_failed("c1", reason="OOM", elapsed_s=10.0)
        events = read_event_log(event_log.path)
        e = events[0]
        assert e["type"] == "CAMPAIGN_FAILED"
        assert e["data"]["reason"] == "OOM"


class TestCampaignEventLogProperties:
    """Tests for path property, flush, and file creation."""

    def test_path_property(self, tmp_path: Path) -> None:
        log = CampaignEventLog(outdir=tmp_path)
        assert log.path == tmp_path / "events.jsonl"

    def test_file_not_created_until_first_write(self, tmp_path: Path) -> None:
        log = CampaignEventLog(outdir=tmp_path)
        assert not log.path.exists()
        log.campaign_started("c1", "local", 5, "lhs", "3.11.0")
        assert log.path.is_file()

    def test_flush_no_file(self, tmp_path: Path) -> None:
        log = CampaignEventLog(outdir=tmp_path)
        log.flush()  # should not raise

    def test_flush_after_write(self, tmp_path: Path) -> None:
        log = CampaignEventLog(outdir=tmp_path)
        log.campaign_started("c1", "local", 5, "lhs", "3.11.0")
        log.flush()  # should not raise


class TestCampaignEventLogAppend:
    """Multiple events are appended in order."""

    def test_multiple_events_in_order(self, tmp_path: Path) -> None:
        log = CampaignEventLog(outdir=tmp_path)
        log.campaign_started("c1", "local", 3, "lhs", "3.11.0")
        log.step_started("c1", "RUN_OPENSTUDIO_SIM")
        log.sample_started("c1", "RUN_OPENSTUDIO_SIM", "0001")
        log.sample_completed("c1", "RUN_OPENSTUDIO_SIM", "0001")
        log.step_completed("c1", "RUN_OPENSTUDIO_SIM", elapsed_s=1.0, cache_hit=False)
        log.campaign_completed("c1", elapsed_s=5.0, n_succeeded=1, n_failed=0)

        events = read_event_log(log.path)
        assert len(events) == 6
        types = [e["type"] for e in events]
        assert types == [
            "CAMPAIGN_STARTED",
            "STEP_STARTED",
            "SAMPLE_STARTED",
            "SAMPLE_COMPLETED",
            "STEP_COMPLETED",
            "CAMPAIGN_COMPLETED",
        ]

    def test_appends_across_multiple_instances(self, tmp_path: Path) -> None:
        """A second CampaignEventLog on the same outdir appends (not truncates)."""
        log1 = CampaignEventLog(outdir=tmp_path)
        log1.campaign_started("c1", "local", 3, "lhs", "3.11.0")

        log2 = CampaignEventLog(outdir=tmp_path)
        log2.campaign_completed("c1", elapsed_s=2.0, n_succeeded=3, n_failed=0)

        events = read_event_log(log1.path)
        assert len(events) == 2


class TestCampaignEventLogThreadSafety:
    """Concurrent writes must not corrupt the file."""

    def test_concurrent_writes(self, tmp_path: Path) -> None:
        log = CampaignEventLog(outdir=tmp_path)
        n_threads = 10
        n_per_thread = 20

        def worker(tid: int) -> None:
            for i in range(n_per_thread):
                log.sample_started("c1", "RUN_OPENSTUDIO_SIM", f"{tid:02d}{i:03d}")

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        events = read_event_log(log.path)
        assert len(events) == n_threads * n_per_thread
        # Every line must be valid JSON with correct keys.
        for e in events:
            assert "ts" in e
            assert "type" in e
            assert "data" in e


class TestReadEventLog:
    """Tests for the read_event_log reader function."""

    def test_nonexistent_file(self, tmp_path: Path) -> None:
        result = read_event_log(tmp_path / "nonexistent.jsonl")
        assert result == []

    def test_empty_file(self, tmp_path: Path) -> None:
        p = tmp_path / "events.jsonl"
        p.write_text("")
        assert read_event_log(p) == []

    def test_skips_blank_lines(self, tmp_path: Path) -> None:
        p = tmp_path / "events.jsonl"
        entry = json.dumps({"ts": "2024-01-01", "type": "TEST", "data": {}, "trace_id": None})
        p.write_text(f"\n{entry}\n\n")
        events = read_event_log(p)
        assert len(events) == 1

    def test_skips_malformed_lines(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        p = tmp_path / "events.jsonl"
        good = json.dumps({"ts": "t", "type": "OK", "data": {}, "trace_id": None})
        p.write_text(f"{good}\nNOT_JSON\n{good}\n")
        events = read_event_log(p)
        assert len(events) == 2

    def test_preserves_order(self, tmp_path: Path) -> None:
        p = tmp_path / "events.jsonl"
        lines = []
        for i in range(5):
            lines.append(json.dumps({"ts": "t", "type": f"T{i}", "data": {}, "trace_id": None}))
        p.write_text("\n".join(lines) + "\n")
        events = read_event_log(p)
        assert [e["type"] for e in events] == ["T0", "T1", "T2", "T3", "T4"]
