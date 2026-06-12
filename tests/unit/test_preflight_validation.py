"""Unit tests for preflight validation checks in osimflow/work.py.

Issue #198 — Enhanced PREFLIGHT_RUN_MODEL validation: weather files,
model geometry, and measure entry points.

Covers:
  * _validate_weather_files: valid EPW headers, invalid headers, no weather dir
  * _validate_model_geometry: .osm present/absent, CLI available/unavailable
  * _validate_measure_entry_points: measure.rb/measure.py present/missing/absent dir
  * Integration: preflight_run_model calls all three validators before simulation
"""

import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from osimflow.work import (
    SevereEnergyPlusError,
    _validate_measure_entry_points,
    _validate_model_geometry,
    _validate_weather_files,
    preflight_run_model,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_valid_epw(path: Path, city: str = "TestCity") -> Path:
    """Write a minimal valid EPW file."""
    header = (
        f"LOCATION,{city},State,Country,123456,40.0,-74.0,-5.0,10.0\n"
        "DESIGN CONDITIONS,0,\n"
        "DATA PERIODS,1,1,Data,Mon,1/1,12/31,8760,\n"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(header)
    return path


def _write_invalid_epw(path: Path) -> Path:
    """Write a file that does NOT start with LOCATION."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("This is not an EPW file\nSecond line\n")
    return path


# ---------------------------------------------------------------------------
# _validate_weather_files
# ---------------------------------------------------------------------------
class TestValidateWeatherFiles:
    """Tests for the _validate_weather_files helper."""

    def test_valid_epws_pass(self, tmp_path: Path) -> None:
        """Valid EPW files should not raise."""
        template = tmp_path / "template"
        weather = template / "weather"
        weather.mkdir(parents=True)
        _write_valid_epw(weather / "la.epw", "Los Angeles")
        _write_valid_epw(weather / "ny.epw", "New York")

        # Should not raise
        _validate_weather_files(template)

    def test_invalid_epw_raises_severe_error(self, tmp_path: Path) -> None:
        """An invalid EPW should raise SevereEnergyPlusError."""
        template = tmp_path / "template"
        weather = template / "weather"
        weather.mkdir(parents=True)
        _write_valid_epw(weather / "good.epw", "GoodCity")
        _write_invalid_epw(weather / "bad.epw")

        with pytest.raises(SevereEnergyPlusError, match="weather validation FAILED"):
            _validate_weather_files(template)

    def test_invalid_epw_error_lists_filename(self, tmp_path: Path) -> None:
        """The error message should include the invalid filename."""
        template = tmp_path / "template"
        weather = template / "weather"
        weather.mkdir(parents=True)
        _write_invalid_epw(weather / "corrupt.epw")

        with pytest.raises(SevereEnergyPlusError, match="corrupt.epw"):
            _validate_weather_files(template)

    def test_no_weather_dir_passes(self, tmp_path: Path) -> None:
        """Missing weather directory should not raise."""
        template = tmp_path / "template"
        template.mkdir()

        # Should not raise — no weather dir is fine
        _validate_weather_files(template)

    def test_empty_weather_dir_passes(self, tmp_path: Path) -> None:
        """Empty weather directory should not raise."""
        template = tmp_path / "template"
        (template / "weather").mkdir(parents=True)

        _validate_weather_files(template)

    def test_multiple_invalid_epws_all_reported(self, tmp_path: Path) -> None:
        """Multiple invalid EPWs should all be listed in the error."""
        template = tmp_path / "template"
        weather = template / "weather"
        weather.mkdir(parents=True)
        _write_invalid_epw(weather / "bad1.epw")
        _write_invalid_epw(weather / "bad2.epw")

        with pytest.raises(SevereEnergyPlusError) as exc_info:
            _validate_weather_files(template)

        msg = str(exc_info.value)
        assert "bad1.epw" in msg
        assert "bad2.epw" in msg

    def test_logs_city_country_on_valid_epw(self, tmp_path: Path) -> None:
        """Valid EPW metadata should be logged."""
        template = tmp_path / "template"
        weather = template / "weather"
        weather.mkdir(parents=True)
        _write_valid_epw(weather / "sf.epw", "San Francisco")

        with patch("osimflow.work.log") as mock_log:
            _validate_weather_files(template)

        # Check that info was called with city/country from the header
        info_calls = [c for c in mock_log.info.call_args_list if "validated" in str(c)]
        assert len(info_calls) >= 1


# ---------------------------------------------------------------------------
# _validate_model_geometry
# ---------------------------------------------------------------------------
class TestValidateModelGeometry:
    """Tests for the _validate_model_geometry helper."""

    def test_osm_present_logs_info(self, tmp_path: Path) -> None:
        """An .osm file should produce an info log, not an error."""
        template = tmp_path / "template"
        template.mkdir()
        (template / "model.osm").write_text("{}")

        with patch("osimflow.work.log") as mock_log:
            _validate_model_geometry(template)

        info_calls = [c for c in mock_log.info.call_args_list if ".osm" in str(c)]
        assert len(info_calls) >= 1

    def test_no_osm_logs_warning(self, tmp_path: Path) -> None:
        """Missing .osm should produce a warning but not raise."""
        template = tmp_path / "template"
        template.mkdir()
        (template / "workflow.osw").write_text("{}")

        with patch("osimflow.work.log") as mock_log:
            _validate_model_geometry(template)

        warning_calls = [c for c in mock_log.warning.call_args_list if "no .osm" in str(c)]
        assert len(warning_calls) >= 1

    def test_no_osm_does_not_raise(self, tmp_path: Path) -> None:
        """Missing .osm must NOT raise — some packages are .osw-only."""
        template = tmp_path / "template"
        template.mkdir()

        # Must not raise
        _validate_model_geometry(template)

    def test_nested_osm_found(self, tmp_path: Path) -> None:
        """An .osm in a subdirectory should be found by rglob."""
        template = tmp_path / "template"
        model_dir = template / "model"
        model_dir.mkdir(parents=True)
        (model_dir / "base.osm").write_text("{}")

        with patch("osimflow.work.log") as mock_log:
            _validate_model_geometry(template)

        # The format string contains "%d .osm" — check args, not interpolated string
        info_calls = [
            c for c in mock_log.info.call_args_list if ".osm" in str(c) and "found" in str(c)
        ]
        assert len(info_calls) >= 1

    def test_quick_parse_attempted_when_cli_available(self, tmp_path: Path) -> None:
        """When CLI is available, a quick parse should be attempted."""
        template = tmp_path / "template"
        template.mkdir()
        (template / "model.osm").write_text("{}")

        with (
            patch("osimflow.work._is_openstudio_available", return_value=True),
            patch("osimflow.work._is_stub_mode", return_value=False),
            patch("osimflow.work.subprocess.run") as mock_run,
        ):
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="model ok", stderr=""
            )
            _validate_model_geometry(template)

        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert "openstudio.cli" in cmd

    def test_quick_parse_failure_is_non_fatal(self, tmp_path: Path) -> None:
        """A failed quick parse should log a warning but not raise."""
        template = tmp_path / "template"
        template.mkdir()
        (template / "model.osm").write_text("{}")

        with (
            patch("osimflow.work._is_openstudio_available", return_value=True),
            patch("osimflow.work._is_stub_mode", return_value=False),
            patch("osimflow.work.subprocess.run") as mock_run,
        ):
            mock_run.side_effect = subprocess.SubprocessError("parse failed")
            # Must NOT raise
            _validate_model_geometry(template)

    def test_quick_parse_skipped_in_stub_mode(self, tmp_path: Path) -> None:
        """Quick parse should NOT be attempted in stub mode."""
        template = tmp_path / "template"
        template.mkdir()
        (template / "model.osm").write_text("{}")

        with (
            patch("osimflow.work._is_openstudio_available", return_value=True),
            patch("osimflow.work._is_stub_mode", return_value=True),
            patch("osimflow.work.subprocess.run") as mock_run,
        ):
            _validate_model_geometry(template)

        mock_run.assert_not_called()

    def test_quick_parse_skipped_when_cli_unavailable(self, tmp_path: Path) -> None:
        """Quick parse should NOT be attempted when CLI is unavailable."""
        template = tmp_path / "template"
        template.mkdir()
        (template / "model.osm").write_text("{}")

        with (
            patch("osimflow.work._is_openstudio_available", return_value=False),
            patch("osimflow.work.subprocess.run") as mock_run,
        ):
            _validate_model_geometry(template)

        mock_run.assert_not_called()


# ---------------------------------------------------------------------------
# _validate_measure_entry_points
# ---------------------------------------------------------------------------
class TestValidateMeasureEntryPoints:
    """Tests for the _validate_measure_entry_points helper."""

    def test_no_measures_dir_passes(self, tmp_path: Path) -> None:
        """Missing measures/ directory should not raise."""
        template = tmp_path / "template"
        template.mkdir()

        _validate_measure_entry_points(template)

    def test_measures_with_rb_passes(self, tmp_path: Path) -> None:
        """A measure with measure.rb should pass without warnings."""
        template = tmp_path / "template"
        measures = template / "measures" / "SetWWR"
        measures.mkdir(parents=True)
        (measures / "measure.rb").write_text("# SetWWR measure")

        with patch("osimflow.work.log") as mock_log:
            _validate_measure_entry_points(template)

        warning_calls = [c for c in mock_log.warning.call_args_list if "missing" in str(c).lower()]
        assert len(warning_calls) == 0

    def test_measures_with_py_passes(self, tmp_path: Path) -> None:
        """A measure with measure.py should pass without warnings."""
        template = tmp_path / "template"
        measures = template / "measures" / "AddOverhangs"
        measures.mkdir(parents=True)
        (measures / "measure.py").write_text("# AddOverhangs measure")

        with patch("osimflow.work.log") as mock_log:
            _validate_measure_entry_points(template)

        warning_calls = [c for c in mock_log.warning.call_args_list if "missing" in str(c).lower()]
        assert len(warning_calls) == 0

    def test_measure_missing_entry_point_logs_warning(self, tmp_path: Path) -> None:
        """A measure without measure.rb or measure.py should log a warning."""
        template = tmp_path / "template"
        measures = template / "measures" / "BadMeasure"
        measures.mkdir(parents=True)
        (measures / "README.md").write_text("no measure file")

        with patch("osimflow.work.log") as mock_log:
            # Must NOT raise
            _validate_measure_entry_points(template)

        warning_calls = [c for c in mock_log.warning.call_args_list if "missing" in str(c).lower()]
        assert len(warning_calls) >= 1

    def test_missing_entry_point_does_not_raise(self, tmp_path: Path) -> None:
        """Missing entry point files must NOT raise — non-standard names exist."""
        template = tmp_path / "template"
        measures = template / "measures" / "CustomMeasure"
        measures.mkdir(parents=True)
        (measures / "custom_script.rb").write_text("# custom")

        _validate_measure_entry_points(template)

    def test_mixed_measures(self, tmp_path: Path) -> None:
        """Mix of valid and invalid measures should log warnings for invalid."""
        template = tmp_path / "template"
        measures = template / "measures"
        good_rb = measures / "GoodMeasureRB"
        good_rb.mkdir(parents=True)
        (good_rb / "measure.rb").write_text("# good")
        good_py = measures / "GoodMeasurePY"
        good_py.mkdir(parents=True)
        (good_py / "measure.py").write_text("# good")
        bad = measures / "BadMeasure"
        bad.mkdir(parents=True)
        (bad / "other.rb").write_text("# not standard")

        with patch("osimflow.work.log") as mock_log:
            _validate_measure_entry_points(template)

        warning_calls = [c for c in mock_log.warning.call_args_list if "BadMeasure" in str(c)]
        # Should have at least one warning about BadMeasure
        assert len(warning_calls) >= 1

    def test_empty_measures_dir_passes(self, tmp_path: Path) -> None:
        """Empty measures/ directory should not raise."""
        template = tmp_path / "template"
        (template / "measures").mkdir(parents=True)

        _validate_measure_entry_points(template)

    def test_files_in_measures_dir_ignored(self, tmp_path: Path) -> None:
        """Loose files (not subdirs) in measures/ should be ignored."""
        template = tmp_path / "template"
        measures = template / "measures"
        measures.mkdir(parents=True)
        (measures / "README.md").write_text("docs")

        _validate_measure_entry_points(template)


# ---------------------------------------------------------------------------
# Integration: preflight_run_model calls validators before simulation
# ---------------------------------------------------------------------------
class TestPreflightRunModelValidation:
    """Integration tests verifying preflight_run_model invokes all validators."""

    def test_invalid_epw_stops_before_simulation(self, tmp_path: Path) -> None:
        """Invalid EPW should abort before any simulation attempt."""
        template = tmp_path / "template"
        weather = template / "weather"
        weather.mkdir(parents=True)
        _write_invalid_epw(weather / "bad.epw")

        with (
            patch.dict(os.environ, {"OSIMFLOW_STUB_SIM": "1"}),
            patch("osimflow.work._is_openstudio_available", return_value=False),
            patch("osimflow.work.shutil.copytree") as mock_copy,
        ):
            with pytest.raises(SevereEnergyPlusError, match="weather validation"):
                preflight_run_model(template, "3.11.0")

            # copytree should NOT have been called — we aborted before sim
            mock_copy.assert_not_called()

    def test_valid_package_runs_all_validators(self, tmp_path: Path) -> None:
        """A valid package should pass all validators and proceed to simulation."""
        template = tmp_path / "template"
        template.mkdir()

        # Valid weather
        weather = template / "weather"
        weather.mkdir(parents=True)
        _write_valid_epw(weather / "good.epw", "Denver")

        # Valid model
        (template / "model.osm").write_text("{}")

        # Valid measures
        measure_dir = template / "measures" / "SetWWR"
        measure_dir.mkdir(parents=True)
        (measure_dir / "measure.rb").write_text("# SetWWR")

        with (
            patch.dict(os.environ, {"OSIMFLOW_STUB_SIM": "1"}),
            patch("osimflow.work._is_openstudio_available", return_value=False),
            patch("osimflow.work._validate_weather_files") as mock_weather,
            patch("osimflow.work._validate_model_geometry") as mock_geo,
            patch("osimflow.work._validate_measure_entry_points") as mock_measures,
        ):
            preflight_run_model(template, "3.11.0")

        mock_weather.assert_called_once_with(template)
        mock_geo.assert_called_once_with(template)
        mock_measures.assert_called_once_with(template)

    def test_validators_run_in_order(self, tmp_path: Path) -> None:
        """Validators should run in order: weather, geometry, measures."""
        template = tmp_path / "template"
        template.mkdir()

        call_order: list[str] = []

        def _track_weather(pkg: Path) -> None:
            call_order.append("weather")

        def _track_geo(pkg: Path) -> None:
            call_order.append("geometry")

        def _track_measures(pkg: Path) -> None:
            call_order.append("measures")

        with (
            patch.dict(os.environ, {"OSIMFLOW_STUB_SIM": "1"}),
            patch("osimflow.work._is_openstudio_available", return_value=False),
            patch("osimflow.work._validate_weather_files", side_effect=_track_weather),
            patch("osimflow.work._validate_model_geometry", side_effect=_track_geo),
            patch(
                "osimflow.work._validate_measure_entry_points",
                side_effect=_track_measures,
            ),
        ):
            preflight_run_model(template, "3.11.0")

        assert call_order == ["weather", "geometry", "measures"]

    def test_missing_osm_warns_but_preflight_succeeds(self, tmp_path: Path) -> None:
        """Missing .osm should not prevent preflight from completing."""
        template = tmp_path / "template"
        template.mkdir()
        # No .osm, no weather, no measures — bare minimum

        with patch.dict(os.environ, {"OSIMFLOW_STUB_SIM": "1"}):
            # Should not raise
            preflight_run_model(template, "3.11.0")

    def test_missing_measure_entry_warns_but_preflight_succeeds(self, tmp_path: Path) -> None:
        """Missing measure.rb/measure.py should not prevent preflight."""
        template = tmp_path / "template"
        template.mkdir()
        measure_dir = template / "measures" / "NoEntry"
        measure_dir.mkdir(parents=True)
        # No measure.rb or measure.py

        with patch.dict(os.environ, {"OSIMFLOW_STUB_SIM": "1"}):
            # Should not raise
            preflight_run_model(template, "3.11.0")
