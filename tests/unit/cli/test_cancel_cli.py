"""Tests for the ``osimflow cancel`` CLI subcommand (issue #255)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from osimflow.__main__ import _build_parser, _cmd_cancel


def _make_run_json(
    campaign_id: str = "test-campaign-001",
    finished_at: float | None = None,
) -> dict:
    """Build a minimal run.json dict."""
    return {
        "schema_version": 1,
        "campaign_id": campaign_id,
        "started_at": 1000.0,
        "finished_at": finished_at,
        "config_summary": {"executor": "local", "n_samples": 3},
        "steps": [],
        "per_sample": [],
    }


class TestCancelCLI:
    """Tests for the cancel subcommand wiring."""

    def test_cancel_requires_outdir(self) -> None:
        """Cancel subcommand requires outdir positional argument."""
        from osimflow.__main__ import main

        with pytest.raises(SystemExit) as exc_info:
            main(["cancel"])
        assert exc_info.value.code == 2

    def test_cancel_writes_stop_file(self, tmp_path: Path) -> None:
        """Cancel writes .stop file to the campaign outdir."""
        outdir = tmp_path / "campaign"
        outdir.mkdir()
        (outdir / "run.json").write_text(json.dumps(_make_run_json()))

        parser = _build_parser()
        args = parser.parse_args(["cancel", str(outdir)])
        result = _cmd_cancel(args)

        assert result == 0
        stop_file = outdir / ".stop"
        assert stop_file.exists()
        content = json.loads(stop_file.read_text())
        assert "requested_at" in content

    def test_cancel_no_run_json(self, tmp_path: Path) -> None:
        """Cancel returns 1 when run.json is not found."""
        outdir = tmp_path / "nonexistent"
        outdir.mkdir()

        parser = _build_parser()
        args = parser.parse_args(["cancel", str(outdir)])
        result = _cmd_cancel(args)

        assert result == 1

    def test_cancel_completed_campaign(self, tmp_path: Path) -> None:
        """Cancel returns 1 when campaign has already completed."""
        outdir = tmp_path / "campaign"
        outdir.mkdir()
        (outdir / "run.json").write_text(json.dumps(_make_run_json(finished_at=2000.0)))

        parser = _build_parser()
        args = parser.parse_args(["cancel", str(outdir)])
        result = _cmd_cancel(args)

        assert result == 1

    def test_cancel_idempotent(self, tmp_path: Path) -> None:
        """Multiple cancel requests are idempotent."""
        outdir = tmp_path / "campaign"
        outdir.mkdir()
        (outdir / "run.json").write_text(json.dumps(_make_run_json()))

        parser = _build_parser()
        args = parser.parse_args(["cancel", str(outdir)])

        result1 = _cmd_cancel(args)
        result2 = _cmd_cancel(args)

        assert result1 == 0
        assert result2 == 0
        stop_file = outdir / ".stop"
        assert stop_file.exists()

    def test_cancel_corrupt_run_json(self, tmp_path: Path) -> None:
        """Cancel returns 1 when run.json is corrupt."""
        outdir = tmp_path / "campaign"
        outdir.mkdir()
        (outdir / "run.json").write_text("not valid json {{{{")

        parser = _build_parser()
        args = parser.parse_args(["cancel", str(outdir)])
        result = _cmd_cancel(args)

        assert result == 1

    def test_cancel_log_level(self, tmp_path: Path) -> None:
        """Cancel subcommand accepts --log_level."""
        outdir = tmp_path / "campaign"
        outdir.mkdir()
        (outdir / "run.json").write_text(json.dumps(_make_run_json()))

        parser = _build_parser()
        args = parser.parse_args(["cancel", str(outdir), "--log_level", "DEBUG"])
        assert args.log_level == "DEBUG"

    def test_cancel_default_log_level(self, tmp_path: Path) -> None:
        """Cancel subcommand defaults log_level to INFO."""
        outdir = tmp_path / "campaign"
        outdir.mkdir()
        (outdir / "run.json").write_text(json.dumps(_make_run_json()))

        parser = _build_parser()
        args = parser.parse_args(["cancel", str(outdir)])
        assert args.log_level == "INFO"
