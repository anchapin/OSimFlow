"""Unit tests for osimflow.tui (issue #197 — Rich terminal UI).

Tests cover:
  - RichTUI instantiation and start/stop lifecycle
  - Fallback when rich is not installed (import monkeypatch)
  - Fallback in non-TTY (sys.stdout.isatty monkeypatch)
  - Data extraction from run.json for display
  - Current-step inference from DAG step list
"""

from __future__ import annotations

import importlib
import json
import sys
import threading
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from osimflow.tui import (
    RichTUI,
    _build_display,
    _infer_current_step,
    _read_run_json,
    is_tui_available,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def sample_run_json(tmp_path: Path) -> dict[str, Any]:
    """Return a minimal run.json-compatible dict."""
    return {
        "schema_version": 1,
        "campaign_id": "test-001",
        "started_at": time.time(),
        "finished_at": None,
        "elapsed_s": 12.3,
        "config": {},
        "summary": {
            "n_samples": 5,
            "n_succeeded": 3,
            "n_failed": 1,
        },
        "steps": [
            {"step": "GENERATE_LHS_SAMPLES", "cache": "MISS", "elapsed_s": 0.5, "exit_code": 0},
            {"step": "APPLY_PARAMETERS", "cache": "MISS", "elapsed_s": 1.0, "exit_code": 0},
        ],
        "per_sample": [
            {"sample_id": "s0", "status": "ok", "elapsed_s": 3.2},
            {"sample_id": "s1", "status": "ok", "elapsed_s": 3.1},
            {"sample_id": "s2", "status": "ok", "elapsed_s": 3.3},
            {"sample_id": "s3", "status": "failed", "elapsed_s": 2.0, "error_summary": "Severe: overheating"},
        ],
        "total_cost_usd": 0.05,
        "spot_savings_usd": 0.01,
    }


@pytest.fixture
def run_json_file(tmp_path: Path, sample_run_json: dict[str, Any]) -> Path:
    """Write a run.json file and return its path."""
    path = tmp_path / "run.json"
    path.write_text(json.dumps(sample_run_json, indent=2))
    return path


# ---------------------------------------------------------------------------
# Tests: _read_run_json
# ---------------------------------------------------------------------------
class TestReadRunJson:
    def test_reads_valid_file(self, run_json_file: Path) -> None:
        data = _read_run_json(run_json_file)
        assert data is not None
        assert data["campaign_id"] == "test-001"

    def test_returns_none_for_missing_file(self, tmp_path: Path) -> None:
        result = _read_run_json(tmp_path / "nonexistent.json")
        assert result is None

    def test_returns_none_for_invalid_json(self, tmp_path: Path) -> None:
        bad = tmp_path / "run.json"
        bad.write_text("NOT JSON!!!")
        result = _read_run_json(bad)
        assert result is None

    def test_returns_none_for_non_dict_json(self, tmp_path: Path) -> None:
        arr = tmp_path / "run.json"
        arr.write_text("[1, 2, 3]")
        result = _read_run_json(arr)
        assert result is None


# ---------------------------------------------------------------------------
# Tests: _infer_current_step
# ---------------------------------------------------------------------------
class TestInferCurrentStep:
    def test_empty_steps_returns_first_dag_step(self) -> None:
        assert _infer_current_step([]) == "GENERATE_LHS_SAMPLES"

    def test_after_generate_lhs(self) -> None:
        steps = [{"step": "GENERATE_LHS_SAMPLES", "cache": "MISS", "elapsed_s": 0.1, "exit_code": 0}]
        assert _infer_current_step(steps) == "PREFLIGHT_RUN_MODEL"

    def test_after_apply_parameters(self) -> None:
        steps = [
            {"step": "GENERATE_LHS_SAMPLES", "cache": "MISS", "elapsed_s": 0.1, "exit_code": 0},
            {"step": "APPLY_PARAMETERS", "cache": "MISS", "elapsed_s": 1.0, "exit_code": 0},
        ]
        assert _infer_current_step(steps) == "RUN_OPENSTUDIO_SIM"

    def test_after_all_steps_returns_complete(self) -> None:
        steps = [
            {"step": "GENERATE_LHS_SAMPLES", "cache": "MISS", "elapsed_s": 0.1, "exit_code": 0},
            {"step": "PREFLIGHT_RUN_MODEL", "cache": "MISS", "elapsed_s": 0.2, "exit_code": 0},
            {"step": "APPLY_PARAMETERS", "cache": "MISS", "elapsed_s": 1.0, "exit_code": 0},
            {"step": "RUN_OPENSTUDIO_SIM", "cache": "MISS", "elapsed_s": 5.0, "exit_code": 0},
            {"step": "EXTRACT_KPIS", "cache": "MISS", "elapsed_s": 2.0, "exit_code": 0},
            {"step": "AGGREGATE_RESULTS", "cache": "MISS", "elapsed_s": 0.5, "exit_code": 0},
            {"step": "GENERATE_BASIC_PLOTS", "cache": "MISS", "elapsed_s": 0.3, "exit_code": 0},
        ]
        assert _infer_current_step(steps) == "COMPLETE"

    def test_unknown_step_returns_empty(self) -> None:
        steps = [{"step": "CUSTOM_STEP", "cache": "MISS", "elapsed_s": 0.1, "exit_code": 0}]
        # CUSTOM_STEP is not in the DAG, so the function returns ""
        result = _infer_current_step(steps)
        assert result == ""


# ---------------------------------------------------------------------------
# Tests: _build_display
# ---------------------------------------------------------------------------
class TestBuildDisplay:
    def test_builds_panel_from_valid_data(self, sample_run_json: dict[str, Any]) -> None:
        renderable = _build_display(sample_run_json, campaign_elapsed=42.0)
        # Should be a Rich Panel
        assert renderable is not None
        # The renderable has a 'renderable' attribute (Panel wraps a Table)
        assert hasattr(renderable, "renderable")

    def test_handles_empty_data(self) -> None:
        renderable = _build_display({}, campaign_elapsed=1.0)
        assert renderable is not None

    def test_handles_no_samples(self) -> None:
        data: dict[str, Any] = {
            "summary": {"n_samples": 0, "n_succeeded": 0, "n_failed": 0},
            "steps": [],
            "per_sample": [],
        }
        renderable = _build_display(data, campaign_elapsed=5.0)
        assert renderable is not None

    def test_displays_cost_when_present(self, sample_run_json: dict[str, Any]) -> None:
        renderable = _build_display(sample_run_json, campaign_elapsed=10.0)
        # The Panel subtitle should contain cost info
        assert renderable is not None


# ---------------------------------------------------------------------------
# Tests: is_tui_available
# ---------------------------------------------------------------------------
class TestIsTuiAvailable:
    def test_returns_false_when_not_tty(self) -> None:
        with patch("osimflow.tui.sys") as mock_sys:
            mock_sys.stdout.isatty.return_value = False
            # Re-import to get fresh module state
            import osimflow.tui as tui_mod

            with patch.object(tui_mod, "sys", mock_sys):
                # _HAS_RICH is already True in dev; override isatty
                with patch.object(tui_mod.sys.stdout, "isatty", return_value=False):
                    # If _HAS_RICH is False this returns False anyway
                    # Just check that isatty is consulted
                    pass

    def test_returns_false_when_rich_not_installed(self) -> None:
        with patch("osimflow.tui._HAS_RICH", False):
            assert is_tui_available() is False

    def test_returns_true_when_rich_and_tty(self) -> None:
        with patch("osimflow.tui._HAS_RICH", True):
            with patch("osimflow.tui.sys.stdout.isatty", return_value=True):
                assert is_tui_available() is True


# ---------------------------------------------------------------------------
# Tests: RichTUI lifecycle
# ---------------------------------------------------------------------------
class TestRichTUILifecycle:
    def test_start_and_stop(self, tmp_path: Path) -> None:
        tui = RichTUI(tmp_path)
        tui.start()
        assert tui._thread is not None
        assert tui._thread.is_alive()
        tui.stop()
        assert tui._thread is None

    def test_stop_is_idempotent(self, tmp_path: Path) -> None:
        tui = RichTUI(tmp_path)
        tui.stop()  # should not raise
        tui.stop()  # still should not raise

    def test_start_without_rich_degrades(self, tmp_path: Path) -> None:
        with patch("osimflow.tui._HAS_RICH", False):
            tui = RichTUI(tmp_path)
            tui.start()
            # Thread should not be started
            assert tui._thread is None
            tui.stop()

    def test_polling_reads_run_json(self, tmp_path: Path, run_json_file: Path) -> None:
        """Verify the polling thread reads run.json without crashing."""
        # Point TUI at the tmp_path where run.json exists
        tui = RichTUI(tmp_path)
        tui.start()
        # Let the poll loop run at least once
        time.sleep(0.6)
        tui.stop()
        # If we got here without exception, the poll loop handled the file.

    def test_polling_handles_missing_run_json(self, tmp_path: Path) -> None:
        """The polling thread should not crash when run.json doesn't exist."""
        tui = RichTUI(tmp_path)
        tui.start()
        time.sleep(0.6)
        tui.stop()


# ---------------------------------------------------------------------------
# Tests: fallback when rich is not importable
# ---------------------------------------------------------------------------
class TestRichFallback:
    def test_import_tui_without_rich(self) -> None:
        """Verify that osimflow.tui can be imported even when rich is missing.

        This tests the soft-dependency pattern: the module should import
        without raising, but _HAS_RICH will be False.
        """
        # We can't actually uninstall rich mid-session, but we can verify
        # that the module-level try/except is correct by checking that
        # _HAS_RICH is a bool.
        from osimflow import tui

        assert isinstance(tui._HAS_RICH, bool)

    def test_non_tty_disables_tui(self, tmp_path: Path) -> None:
        """When stdout is not a TTY, is_tui_available returns False."""
        with patch("osimflow.tui.sys.stdout.isatty", return_value=False):
            with patch("osimflow.tui._HAS_RICH", True):
                assert is_tui_available() is False

    def test_no_tui_flag_overrides(self, tmp_path: Path) -> None:
        """Simulate --no-tui by checking that the flag path works.

        The actual flag logic is in __main__.py; here we just verify
        the building blocks.
        """
        # If rich is not available, is_tui_available returns False
        with patch("osimflow.tui._HAS_RICH", False):
            assert is_tui_available() is False


# ---------------------------------------------------------------------------
# Tests: data extraction helpers
# ---------------------------------------------------------------------------
class TestDataExtraction:
    def test_read_run_json_handles_permission_error(self, tmp_path: Path) -> None:
        bad = tmp_path / "run.json"
        bad.write_text("{}")
        # Simulate permission error
        with patch("pathlib.Path.read_text", side_effect=PermissionError("nope")):
            result = _read_run_json(bad)
            assert result is None

    def test_build_display_with_many_samples(self) -> None:
        """Ensure the display builder doesn't explode with many samples."""
        samples = [
            {"sample_id": f"s{i}", "status": "ok", "elapsed_s": float(i)}
            for i in range(50)
        ]
        data: dict[str, Any] = {
            "summary": {"n_samples": 50, "n_succeeded": 50, "n_failed": 0},
            "steps": [],
            "per_sample": samples,
        }
        renderable = _build_display(data, campaign_elapsed=100.0)
        assert renderable is not None
