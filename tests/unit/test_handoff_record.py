"""Unit tests for the local Coordinator handoff record (issue #630)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from osimflow.handoff_record import (
    HANDOFF_RECORD_NAME,
    IDEMPOTENCY_KEY_HEADER,
    HandoffRecord,
    NoHandoffRecordError,
    handoff_record_exists,
    read_handoff_record,
    write_handoff_record,
)


def _sample_record(**overrides: object) -> HandoffRecord:
    base = dict(
        campaign_id="camp-abc",
        coordinator_url="https://coord.example.com",
        submitted_at=1700000000.0,
        status_url="https://coord.example.com/api/v1/coordinator/campaigns/camp-abc",
        idempotency_key="osimflow-deadbeef",
    )
    base.update(overrides)
    return HandoffRecord(**base)  # type: ignore[arg-type]


class TestWriteReadRoundtrip:
    def test_write_then_read_returns_same_record(self, tmp_path: Path) -> None:
        rec = _sample_record()
        path = write_handoff_record(tmp_path, rec)
        assert path == tmp_path / HANDOFF_RECORD_NAME
        assert path.exists()

        loaded = read_handoff_record(tmp_path)
        assert loaded == rec

    def test_write_creates_outdir_if_missing(self, tmp_path: Path) -> None:
        outdir = tmp_path / "nested" / "outdir"
        assert not outdir.exists()
        write_handoff_record(outdir, _sample_record())
        assert handoff_record_exists(outdir)

    def test_written_file_is_valid_json_with_version(self, tmp_path: Path) -> None:
        write_handoff_record(tmp_path, _sample_record(idempotency_key=None))
        data = json.loads((tmp_path / HANDOFF_RECORD_NAME).read_text())
        assert data["version"] == 1
        assert data["campaign_id"] == "camp-abc"
        assert data["status_url"].endswith("/camp-abc")
        # idempotency_key is optional and omitted (None) -> not required on disk.
        assert "idempotency_key" in data and data["idempotency_key"] is None


class TestMissingRecord:
    def test_read_missing_record_raises_clear_message(self, tmp_path: Path) -> None:
        with pytest.raises(NoHandoffRecordError) as exc_info:
            read_handoff_record(tmp_path)
        # The actionable message required by the issue.
        msg = str(exc_info.value)
        assert "no Coordinator campaign associated with this outdir" in msg
        assert "--detach" in msg

    def test_handoff_record_exists_false_when_absent(self, tmp_path: Path) -> None:
        assert handoff_record_exists(tmp_path) is False


class TestCorruptRecord:
    def test_corrupt_json_raises_readable_error(self, tmp_path: Path) -> None:
        (tmp_path / HANDOFF_RECORD_NAME).write_text("{ not valid json")
        with pytest.raises(NoHandoffRecordError) as exc_info:
            read_handoff_record(tmp_path)
        assert "unreadable" in str(exc_info.value)

    def test_missing_required_field_raises(self, tmp_path: Path) -> None:
        (tmp_path / HANDOFF_RECORD_NAME).write_text(
            json.dumps({"campaign_id": "x", "coordinator_url": "u"})  # no status_url/submitted_at
        )
        with pytest.raises(NoHandoffRecordError) as exc_info:
            read_handoff_record(tmp_path)
        assert "missing required" in str(exc_info.value)


class TestHeaderConstant:
    def test_header_constant_matches_http_convention(self) -> None:
        # Sanity: the constant the CLI and server share is the canonical header.
        assert IDEMPOTENCY_KEY_HEADER == "Idempotency-Key"
