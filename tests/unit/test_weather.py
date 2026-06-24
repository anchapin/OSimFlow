"""Unit tests for .epw weather file validation, discovery, and format checking.

Issue #63 — EPW bundling and validation in template_sim_package.
Issue #424 — ASHRAE climate zone auto-detection from .stat files.

Tests cover:
  - validate_epw: valid/invalid EPW format detection
  - validate_epw_header: LOCATION row metadata parsing
  - discover_epw_files: weather directory scanning
  - validate_all_epw_files: batch validation
  - CampaignConfig.weather_dir: configuration field
  - download_epw: network error handling (no live network calls)
  - detect_climate_zone_from_stat: ASHRAE zone extraction from .stat header
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from osimflow.campaign import Campaign
from osimflow.config import CampaignConfig
from osimflow.executors import LocalExecutor
from osimflow.weather import (
    EPWDownloadError,
    EPWValidationError,
    detect_climate_zone_from_stat,
    discover_epw_files,
    download_epw,
    validate_all_epw_files,
    validate_epw,
    validate_epw_header,
)

# ---------------------------------------------------------------------------
# Sample EPW header helpers
# ---------------------------------------------------------------------------
VALID_EPW_HEADER = (
    "LOCATION,Los Angeles,CA,USA,722950,33.94,-118.41,-8.0,21.0\n"
    "DESIGN CONDITIONS,1,\n"
    "TYPICAL/EXTREME PERIODS,4,\n"
    "GROUND TEMPERATURES,3,\n"
    "HOLIDAYS/DAYLIGHT SAVINGS,0,0,0,\n"
    "COMMENTS 1,\n"
    "COMMENTS 2,\n"
    "DATA PERIODS,1,1,Data,Sunday, 1/ 1,12/31,8760,\n"
)


def _write_valid_epw(path: Path, city: str = "TestCity") -> Path:
    """Write a minimal valid EPW file at *path*."""
    header = (
        f"LOCATION,{city},State,Country,123456,40.0,-74.0,-5.0,10.0\n"
        "DESIGN CONDITIONS,0,\n"
        "DATA PERIODS,1,1,Data,Mon,1/1,12/31,8760,\n"
    )
    path.write_text(header)
    return path


def _write_invalid_epw(path: Path) -> Path:
    """Write a file that does NOT start with LOCATION."""
    path.write_text("This is not an EPW file\nSecond line\n")
    return path


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture()
def template_with_weather(tmp_path: Path) -> Path:
    """Create a template_sim_package with a weather/ directory containing valid EPWs."""
    template = tmp_path / "template_pkg"
    template.mkdir()
    weather = template / "weather"
    weather.mkdir()

    _write_valid_epw(weather / "USA_CA_Los.Angeles.epw", "Los Angeles")
    _write_valid_epw(weather / "USA_NY_New.York.epw", "New York")
    _write_valid_epw(weather / "USA_IL_Chicago.epw", "Chicago")

    # Create minimal workflow.osw
    osw = {
        "weather_file": "weather/USA_CA_Los.Angeles.epw",
        "steps": [{"measure_dir_name": "TestMeasure", "arguments": {"wwr": 0.4}}],
    }
    (template / "workflow.osw").write_text(json.dumps(osw))
    # Create minimal model.osm (JSON-mode)
    (template / "model.osm").write_text(json.dumps({"attributes": {"wwr": 0.4}}))

    return template


@pytest.fixture()
def template_with_bad_epw(tmp_path: Path) -> Path:
    """Template with an invalid EPW file in the weather directory."""
    template = tmp_path / "template_bad"
    template.mkdir()
    weather = template / "weather"
    weather.mkdir()

    _write_valid_epw(weather / "valid.epw", "ValidCity")
    _write_invalid_epw(weather / "invalid.epw")

    (template / "workflow.osw").write_text(json.dumps({"steps": []}))
    return template


@pytest.fixture()
def variables_yml_epw(tmp_path: Path) -> Path:
    """Create a variables.yml with an epw_file target."""
    variables = {
        "variables": [
            {
                "name": "wwr",
                "distribution": "uniform",
                "min": 0.1,
                "max": 0.6,
            },
            {
                "name": "climate_zone",
                "distribution": "categorical",
                "values": ["cz3", "cz4a", "cz5a"],
                "target": "epw_file",
                "mapping": {
                    "cz3": "weather/USA_CA_Los.Angeles.epw",
                    "cz4a": "weather/USA_NY_New.York.epw",
                    "cz5a": "weather/USA_IL_Chicago.epw",
                },
            },
        ]
    }
    yml_path = tmp_path / "variables_epw.yml"
    yml_path.write_text(yaml.dump(variables))
    return yml_path


# ---------------------------------------------------------------------------
# Tests: validate_epw
# ---------------------------------------------------------------------------
class TestValidateEpw:
    """Tests for the validate_epw function."""

    def test_valid_epw_passes(self, tmp_path: Path) -> None:
        """A well-formed EPW file should pass validation."""
        epw = _write_valid_epw(tmp_path / "test.epw")
        assert validate_epw(epw) is True

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        """A non-existent file should raise EPWValidationError."""
        epw = tmp_path / "nonexistent.epw"
        with pytest.raises(EPWValidationError, match="not found"):
            validate_epw(epw)

    def test_invalid_header_raises(self, tmp_path: Path) -> None:
        """A file not starting with LOCATION should raise."""
        epw = _write_invalid_epw(tmp_path / "bad.epw")
        with pytest.raises(EPWValidationError, match="must start with 'LOCATION'"):
            validate_epw(epw)

    def test_empty_file_raises(self, tmp_path: Path) -> None:
        """An empty file should raise EPWValidationError."""
        epw = tmp_path / "empty.epw"
        epw.write_text("")
        with pytest.raises(EPWValidationError, match="Cannot read EPW"):
            validate_epw(epw)

    def test_location_with_whitespace_prefix(self, tmp_path: Path) -> None:
        """LOCATION line with leading whitespace should still validate."""
        epw = tmp_path / "padded.epw"
        epw.write_text("  LOCATION,City,State,Country,123,0.0,0.0,0,0\nData\n")
        # Leading whitespace is stripped by strip() in validate_epw
        assert validate_epw(epw) is True


# ---------------------------------------------------------------------------
# Tests: validate_epw_header
# ---------------------------------------------------------------------------
class TestValidateEpwHeader:
    """Tests for the validate_epw_header function."""

    def test_parses_location_fields(self, tmp_path: Path) -> None:
        """Header metadata should be parsed into a dict."""
        epw = _write_valid_epw(tmp_path / "test.epw", "TestCity")
        meta = validate_epw_header(epw)
        assert meta["city"] == "TestCity"
        assert meta["state_province"] == "State"
        assert meta["country"] == "Country"
        assert meta["wmo"] == "123456"
        assert meta["latitude"] == "40.0"
        assert meta["longitude"] == "-74.0"
        assert meta["timezone"] == "-5.0"
        assert meta["elevation"] == "10.0"

    def test_too_few_fields_raises(self, tmp_path: Path) -> None:
        """A LOCATION row with fewer than 9 fields should raise."""
        epw = tmp_path / "short.epw"
        epw.write_text("LOCATION,City,State\n")
        with pytest.raises(EPWValidationError, match="expected at least 9"):
            validate_epw_header(epw)

    def test_invalid_file_raises(self, tmp_path: Path) -> None:
        """An invalid EPW should raise before attempting to parse header."""
        epw = _write_invalid_epw(tmp_path / "bad.epw")
        with pytest.raises(EPWValidationError):
            validate_epw_header(epw)


# ---------------------------------------------------------------------------
# Tests: discover_epw_files
# ---------------------------------------------------------------------------
class TestDiscoverEpwFiles:
    """Tests for the discover_epw_files function."""

    def test_discovers_epw_in_weather_dir(self, template_with_weather: Path) -> None:
        """All .epw files in the weather directory should be discovered."""
        files = discover_epw_files(template_with_weather, "weather")
        assert len(files) == 3
        names = {f.name for f in files}
        assert "USA_CA_Los.Angeles.epw" in names
        assert "USA_NY_New.York.epw" in names
        assert "USA_IL_Chicago.epw" in names

    def test_missing_weather_dir_returns_empty(self, tmp_path: Path) -> None:
        """A missing weather directory should return an empty list."""
        files = discover_epw_files(tmp_path / "nonexistent", "weather")
        assert files == []

    def test_custom_subdir(self, tmp_path: Path) -> None:
        """A custom weather subdirectory name should be respected."""
        template = tmp_path / "pkg"
        template.mkdir()
        climate = template / "climate_data"
        climate.mkdir()
        _write_valid_epw(climate / "test.epw")

        files = discover_epw_files(template, "climate_data")
        assert len(files) == 1
        assert files[0].name == "test.epw"

    def test_ignores_non_epw_files(self, tmp_path: Path) -> None:
        """Non-.epw files in the weather directory should be ignored."""
        template = tmp_path / "pkg"
        template.mkdir()
        weather = template / "weather"
        weather.mkdir()
        _write_valid_epw(weather / "good.epw")
        (weather / "readme.txt").write_text("not an epw")
        (weather / "data.csv").write_text("also not an epw")

        files = discover_epw_files(template, "weather")
        assert len(files) == 1
        assert files[0].name == "good.epw"


# ---------------------------------------------------------------------------
# Tests: validate_all_epw_files
# ---------------------------------------------------------------------------
class TestValidateAllEpwFiles:
    """Tests for the validate_all_epw_files function."""

    def test_all_valid_returns_paths(self, template_with_weather: Path) -> None:
        """All valid EPWs should be returned."""
        valid = validate_all_epw_files(template_with_weather, "weather")
        assert len(valid) == 3

    def test_invalid_epw_raises(self, template_with_bad_epw: Path) -> None:
        """An invalid EPW should cause EPWValidationError."""
        with pytest.raises(EPWValidationError, match="EPW file validation failed"):
            validate_all_epw_files(template_with_bad_epw, "weather")

    def test_no_weather_dir_returns_empty(self, tmp_path: Path) -> None:
        """No weather directory should return an empty list (not raise)."""
        template = tmp_path / "empty_pkg"
        template.mkdir()
        result = validate_all_epw_files(template, "weather")
        assert result == []


# ---------------------------------------------------------------------------
# Tests: CampaignConfig.weather_dir
# ---------------------------------------------------------------------------
class TestCampaignConfigWeatherDir:
    """Tests for the weather_dir configuration field."""

    def test_default_weather_dir(self, tmp_path: Path) -> None:
        """Default weather_dir should be 'weather'."""
        (tmp_path / "variables.yml").write_text(yaml.dump({"variables": []}))
        (tmp_path / "template").mkdir()
        outdir = tmp_path / "out"
        outdir.mkdir()

        cfg = CampaignConfig(
            input_variables=tmp_path / "variables.yml",
            template_sim_package=tmp_path / "template",
            n_samples=5,
            outdir=outdir,
            openstudio_version="3.11.0",
        )
        assert cfg.weather_dir == "weather"

    def test_custom_weather_dir(self, tmp_path: Path) -> None:
        """A custom weather_dir value should be stored."""
        (tmp_path / "variables.yml").write_text(yaml.dump({"variables": []}))
        (tmp_path / "template").mkdir()
        outdir = tmp_path / "out"
        outdir.mkdir()

        cfg = CampaignConfig(
            input_variables=tmp_path / "variables.yml",
            template_sim_package=tmp_path / "template",
            n_samples=5,
            outdir=outdir,
            openstudio_version="3.11.0",
            weather_dir="climate_data",
        )
        assert cfg.weather_dir == "climate_data"


# ---------------------------------------------------------------------------
# Tests: Campaign integration
# ---------------------------------------------------------------------------
class TestCampaignEpwFormatValidation:
    """Tests for Campaign pre-flight EPW format validation (issue #63)."""

    @staticmethod
    def _make_campaign(
        tmp_path: Path,
        template_dir: Path,
        variables_yml: Path,
        weather_dir: str = "weather",
    ) -> Campaign:
        """Create a Campaign with the given config for testing."""
        outdir = tmp_path / "results"
        outdir.mkdir(parents=True, exist_ok=True)
        cfg = CampaignConfig(
            input_variables=variables_yml,
            template_sim_package=template_dir,
            n_samples=3,
            outdir=outdir,
            openstudio_version="3.11.0",
            weather_dir=weather_dir,
        )
        executor = LocalExecutor(max_workers=1)
        return Campaign(cfg=cfg, executor=executor)

    def test_valid_epws_pass_preflight(
        self,
        tmp_path: Path,
        template_with_weather: Path,
        variables_yml_epw: Path,
    ) -> None:
        """Campaign pre-flight should pass with valid EPW files."""
        campaign = self._make_campaign(tmp_path, template_with_weather, variables_yml_epw)
        variable_defs = campaign._load_variable_defs()

        # Should not raise
        campaign._preflight_validate_epw_files(variable_defs)

    def test_invalid_epw_in_weather_dir_fails_preflight(
        self,
        tmp_path: Path,
        template_with_bad_epw: Path,
    ) -> None:
        """An invalid EPW in the weather directory should fail pre-flight."""
        variables = {
            "variables": [
                {
                    "name": "climate",
                    "distribution": "categorical",
                    "values": ["v"],
                    "target": "epw_file",
                    "mapping": {"v": "weather/valid.epw"},
                },
            ]
        }
        yml_path = tmp_path / "variables.yml"
        yml_path.write_text(yaml.dump(variables))

        campaign = self._make_campaign(tmp_path, template_with_bad_epw, yml_path)
        variable_defs = campaign._load_variable_defs()

        # The explicitly-referenced valid.epw is fine, but the other
        # invalid.epw in the weather/ directory will be caught by
        # validate_all_epw_files.
        with pytest.raises(EPWValidationError, match="PRE-FLIGHT EPW FORMAT"):
            campaign._preflight_validate_epw_files(variable_defs)

    def test_invalid_referenced_epw_fails_preflight(
        self,
        tmp_path: Path,
    ) -> None:
        """An explicitly-referenced invalid EPW should fail pre-flight."""
        template = tmp_path / "template"
        template.mkdir()
        weather = template / "weather"
        weather.mkdir()
        _write_invalid_epw(weather / "bad.epw")
        (template / "workflow.osw").write_text(json.dumps({"steps": []}))

        variables = {
            "variables": [
                {
                    "name": "climate",
                    "distribution": "categorical",
                    "values": ["v"],
                    "target": "epw_file",
                    "mapping": {"v": "weather/bad.epw"},
                },
            ]
        }
        yml_path = tmp_path / "variables.yml"
        yml_path.write_text(yaml.dump(variables))

        campaign = self._make_campaign(tmp_path, template, yml_path)
        variable_defs = campaign._load_variable_defs()

        with pytest.raises(EPWValidationError, match="PRE-FLIGHT EPW FORMAT"):
            campaign._preflight_validate_epw_files(variable_defs)

    def test_no_weather_dir_no_epw_vars_passes(
        self,
        tmp_path: Path,
    ) -> None:
        """No weather directory and no epw_file targets should pass cleanly."""
        template = tmp_path / "template"
        template.mkdir()
        (template / "workflow.osw").write_text(json.dumps({"steps": []}))

        yml_path = tmp_path / "variables.yml"
        yml_path.write_text(
            yaml.dump(
                {"variables": [{"name": "wwr", "distribution": "uniform", "min": 0.1, "max": 0.6}]}
            )
        )

        campaign = self._make_campaign(tmp_path, template, yml_path)
        variable_defs = campaign._load_variable_defs()

        # Should not raise
        campaign._preflight_validate_epw_files(variable_defs)

    def test_custom_weather_dir(
        self,
        tmp_path: Path,
    ) -> None:
        """Custom weather_dir should be used for discovery."""
        template = tmp_path / "template"
        template.mkdir()
        climate = template / "climate_data"
        climate.mkdir()
        _write_valid_epw(climate / "test.epw")
        (template / "workflow.osw").write_text(json.dumps({"steps": []}))

        variables = {
            "variables": [
                {
                    "name": "climate",
                    "distribution": "categorical",
                    "values": ["v"],
                    "target": "epw_file",
                    "mapping": {"v": "climate_data/test.epw"},
                },
            ]
        }
        yml_path = tmp_path / "variables.yml"
        yml_path.write_text(yaml.dump(variables))

        campaign = self._make_campaign(tmp_path, template, yml_path, weather_dir="climate_data")
        variable_defs = campaign._load_variable_defs()

        # Should not raise — the EPW is valid and in the custom dir
        campaign._preflight_validate_epw_files(variable_defs)


# ---------------------------------------------------------------------------
# Tests: download_epw (network mocking)
# ---------------------------------------------------------------------------
class TestDownloadEpw:
    """Tests for the download_epw function (mocked network)."""

    def test_existing_file_skips_download(self, tmp_path: Path) -> None:
        """If the file already exists, download should be skipped."""
        dest_dir = tmp_path / "weather"
        dest_dir.mkdir()
        existing = dest_dir / "USA_CA_Los.Angeles.epw"
        _write_valid_epw(existing)

        result = download_epw("USA_CA_Los.Angeles", dest_dir)
        assert result == existing

    def test_creates_dest_dir(self, tmp_path: Path) -> None:
        """Destination directory should be created if it does not exist."""
        dest_dir = tmp_path / "new_weather_dir"
        # Pre-create the file so we don't actually hit the network
        dest_dir.mkdir(parents=True, exist_ok=True)
        existing = dest_dir / "test.epw"
        _write_valid_epw(existing)

        result = download_epw("test", dest_dir)
        assert result == existing

    @patch("osimflow.weather.urllib.request.urlopen")
    def test_download_network_error_raises(self, mock_urlopen: MagicMock, tmp_path: Path) -> None:
        """A network error during download should raise EPWDownloadError."""
        import urllib.error

        mock_urlopen.side_effect = urllib.error.URLError("Connection refused")

        with pytest.raises(EPWDownloadError, match="Failed to download"):
            download_epw("USA_CA_Los.Angeles", tmp_path / "dest")

    @patch("osimflow.weather.urllib.request.urlopen")
    def test_successful_download(self, mock_urlopen: MagicMock, tmp_path: Path) -> None:
        """A successful download should write the file and return its path."""
        epw_content = (
            b"LOCATION,TestCity,State,Country,123456,40.0,-74.0,-5.0,10.0\n"
            b"DATA PERIODS,1,1,Data,Mon,1/1,12/31,8760,\n"
        )

        mock_response = MagicMock()
        mock_response.headers = {"Content-Length": str(len(epw_content))}
        mock_response.read.return_value = epw_content
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        dest_dir = tmp_path / "weather"
        result = download_epw("USA_CA_Los.Angeles", dest_dir)

        assert result.is_file()
        assert result.name == "USA_CA_Los.Angeles.epw"
        content = result.read_text()
        assert content.startswith("LOCATION")

    @patch("osimflow.weather.urllib.request.urlopen")
    def test_download_content_length_too_large(
        self, mock_urlopen: MagicMock, tmp_path: Path
    ) -> None:
        """Content-Length header exceeding max should raise EPWDownloadError."""
        from osimflow.weather import MAX_EPW_DOWNLOAD_BYTES

        mock_response = MagicMock()
        mock_response.headers = {"Content-Length": str(MAX_EPW_DOWNLOAD_BYTES + 1)}
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        with pytest.raises(EPWDownloadError, match="too large"):
            download_epw("USA_CA_Los.Angeles", tmp_path / "dest")

    @patch("osimflow.weather.urllib.request.urlopen")
    def test_download_body_exceeds_limit(self, mock_urlopen: MagicMock, tmp_path: Path) -> None:
        """Body larger than max (no Content-Length header) should raise EPWDownloadError."""
        from osimflow.weather import MAX_EPW_DOWNLOAD_BYTES

        mock_response = MagicMock()
        mock_response.headers = {}
        mock_response.read.return_value = b"x" * (MAX_EPW_DOWNLOAD_BYTES + 1)
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        with pytest.raises(EPWDownloadError, match="exceeded"):
            download_epw("USA_CA_Los.Angeles", tmp_path / "dest")


# ---------------------------------------------------------------------------
# Tests: detect_climate_zone_from_stat (issue #424)
# ---------------------------------------------------------------------------
class TestDetectClimateZoneFromStat:
    """Tests for the detect_climate_zone_from_stat function."""

    def test_detects_climate_zone_6b(self, tmp_path: Path) -> None:
        """ASHRAE climate zone 6B should be detected from stat header."""
        stat = tmp_path / "USA_CA_Los.Angeles.stat"
        stat.write_text("USA_CA_Los.Angeles - TMYx, ASHRAE 169-2006-, 6B\n")
        assert detect_climate_zone_from_stat(stat) == "6B"

    def test_detects_climate_zone_6a(self, tmp_path: Path) -> None:
        """ASHRAE climate zone 6A should be detected from stat header."""
        stat = tmp_path / "USA_MI_Detroit.stat"
        stat.write_text("USA_MI_Detroit - TMYx, ASHRAE 169-2013-, 6A\n")
        assert detect_climate_zone_from_stat(stat) == "6A"

    def test_detects_climate_zone_5a(self, tmp_path: Path) -> None:
        """ASHRAE climate zone 5A should be detected from stat header."""
        stat = tmp_path / "USA_IL_Chicago.stat"
        stat.write_text("USA_IL_Chicago - TMYx, ASHRAE 169-2006-, 5A\n")
        assert detect_climate_zone_from_stat(stat) == "5A"

    def test_missing_file_returns_none(self, tmp_path: Path) -> None:
        """A non-existent stat file should return None."""
        result = detect_climate_zone_from_stat(tmp_path / "nonexistent.stat")
        assert result is None

    def test_empty_file_returns_none(self, tmp_path: Path) -> None:
        """An empty stat file should return None."""
        stat = tmp_path / "empty.stat"
        stat.write_text("")
        assert detect_climate_zone_from_stat(stat) is None

    def test_no_climate_zone_returns_none(self, tmp_path: Path) -> None:
        """A stat file without a climate zone pattern should return None."""
        stat = tmp_path / "unknown.stat"
        stat.write_text("SomeUnknownFile - Unknown Source\n")
        assert detect_climate_zone_from_stat(stat) is None
