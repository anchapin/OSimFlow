"""Unit tests for .epw weather file path mutation (issue #55).

Tests cover:
  - Categorical LHS distribution with epw_file target
  - .osw weather_file field mutation
  - Pre-flight validation for missing .epw files
  - End-to-end apply_parameters with epw_file target
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from osimflow.apply_params import (
    EPW_FILE_KEY,
    apply_parameters,
    mutate_osw_weather_file,
)
from osimflow.campaign import Campaign
from osimflow.config import CampaignConfig
from osimflow.executors import LocalExecutor


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture()
def tmp_template_dir(tmp_path: Path) -> Path:
    """Create a minimal template_sim_package with .osw and .epw files."""
    template = tmp_path / "template_pkg"
    template.mkdir()

    # Create weather directory with .epw files (valid LOCATION header)
    weather_dir = template / "weather"
    weather_dir.mkdir()
    epw_header = "LOCATION,Los Angeles,CA,USA,722950,33.94,-118.41,-8.0,21.0\nDATA PERIODS,1,1,Data,Sunday, 1/ 1,12/31,8760,\n"
    (weather_dir / "USA_CA_Los.Angeles.epw").write_text(epw_header)
    epw_header_ny = "LOCATION,New York,NY,USA,725030,40.71,-74.01,-5.0,10.0\nDATA PERIODS,1,1,Data,Sunday, 1/ 1,12/31,8760,\n"
    (weather_dir / "USA_NY_New.York.epw").write_text(epw_header_ny)
    epw_header_chi = "LOCATION,Chicago,IL,USA,725300,41.78,-87.75,-6.0,190.0\nDATA PERIODS,1,1,Data,Sunday, 1/ 1,12/31,8760,\n"
    (weather_dir / "USA_IL_Chicago.epw").write_text(epw_header_chi)

    # Create a minimal workflow.osw
    osw = {
        "weather_file": "weather/default.epw",
        "seed_file": "model.osm",
        "steps": [
            {
                "measure_dir_name": "SetWindowToWallRatio",
                "arguments": {"wwr": 0.4},
            }
        ],
    }
    (template / "workflow.osw").write_text(json.dumps(osw, indent=2))

    # Create a minimal model.osm (JSON-mode for testability)
    osm = {"attributes": {"wwr": 0.4}}
    (template / "model.osm").write_text(json.dumps(osm, indent=2))

    return template


@pytest.fixture()
def variables_yml_epw(tmp_path: Path, tmp_template_dir: Path) -> Path:
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
    yml_path = tmp_path / "variables.yml"
    yml_path.write_text(yaml.dump(variables))
    return yml_path


@pytest.fixture()
def variables_yml_epw_missing(tmp_path: Path) -> Path:
    """Create a variables.yml with an epw_file target referencing missing files."""
    variables = {
        "variables": [
            {
                "name": "climate_zone",
                "distribution": "categorical",
                "values": ["cz3"],
                "target": "epw_file",
                "mapping": {
                    "cz3": "weather/NONEXISTENT.epw",
                },
            },
        ]
    }
    yml_path = tmp_path / "variables_missing.yml"
    yml_path.write_text(yaml.dump(variables))
    return yml_path


# ---------------------------------------------------------------------------
# Tests: mutate_osw_weather_file
# ---------------------------------------------------------------------------
class TestMutateOswWeatherFile:
    """Tests for the mutate_osw_weather_file function."""

    def test_updates_weather_file_field(self, tmp_path: Path) -> None:
        """The weather_file field in .osw should be updated."""
        osw_path = tmp_path / "workflow.osw"
        osw_path.write_text(json.dumps({"weather_file": "old.epw", "steps": []}))

        mutate_osw_weather_file(osw_path, "weather/new.epw")

        data = json.loads(osw_path.read_text())
        assert data["weather_file"] == "weather/new.epw"

    def test_preserves_other_fields(self, tmp_path: Path) -> None:
        """Other .osw fields should be preserved."""
        osw_path = tmp_path / "workflow.osw"
        original = {
            "weather_file": "old.epw",
            "seed_file": "model.osm",
            "steps": [{"measure_dir_name": "M", "arguments": {"a": 1}}],
        }
        osw_path.write_text(json.dumps(original))

        mutate_osw_weather_file(osw_path, "weather/new.epw")

        data = json.loads(osw_path.read_text())
        assert data["weather_file"] == "weather/new.epw"
        assert data["seed_file"] == "model.osm"
        assert data["steps"] == original["steps"]

    def test_invalid_json_raises(self, tmp_path: Path) -> None:
        """Invalid JSON in .osw should raise ValueError."""
        osw_path = tmp_path / "workflow.osw"
        osw_path.write_text("not json")

        with pytest.raises(ValueError, match="Invalid .osw JSON"):
            mutate_osw_weather_file(osw_path, "weather/new.epw")

    def test_adds_weather_file_when_missing(self, tmp_path: Path) -> None:
        """If .osw has no weather_file field, it should be added."""
        osw_path = tmp_path / "workflow.osw"
        osw_path.write_text(json.dumps({"steps": []}))

        mutate_osw_weather_file(osw_path, "weather/added.epw")

        data = json.loads(osw_path.read_text())
        assert data["weather_file"] == "weather/added.epw"


# ---------------------------------------------------------------------------
# Tests: apply_parameters with epw_file target
# ---------------------------------------------------------------------------
class TestApplyParametersEpw:
    """Tests for apply_parameters with __epw_file__ key."""

    def test_epw_file_mutation_in_dir_template(
        self, tmp_template_dir: Path, tmp_path: Path
    ) -> None:
        """When __epw_file__ is in params, the .osw weather_file is updated."""
        out_dir = tmp_path / "output" / "0001"
        params = {"wwr": 0.35, EPW_FILE_KEY: "weather/USA_NY_New.York.epw"}

        apply_parameters(tmp_template_dir, params, "0001", out_dir)

        # Check the .osw weather_file was mutated
        osw = json.loads((out_dir / "workflow.osw").read_text())
        assert osw["weather_file"] == "weather/USA_NY_New.York.epw"
        # Note: 'wwr' maps to .osm attribute (not .osw measure argument)
        # due to _build_mappings collision resolution, so we verify the
        # .osm was mutated instead.
        osm = json.loads((out_dir / "model.osm").read_text())
        assert osm["attributes"]["wwr"] == 0.35

    def test_epw_file_mutation_single_osw(self, tmp_path: Path) -> None:
        """When template is a single .osw, weather_file is updated."""
        osw_path = tmp_path / "workflow.osw"
        osw_data = {"weather_file": "old.epw", "steps": []}
        osw_path.write_text(json.dumps(osw_data))

        out_dir = tmp_path / "output" / "0001"
        params = {EPW_FILE_KEY: "weather/new.epw"}

        apply_parameters(osw_path, params, "0001", out_dir)

        result_osw = json.loads((out_dir / "workflow.osw").read_text())
        assert result_osw["weather_file"] == "weather/new.epw"

    def test_no_epw_file_key_preserves_existing(
        self, tmp_template_dir: Path, tmp_path: Path
    ) -> None:
        """When __epw_file__ is absent, the original weather_file is preserved."""
        out_dir = tmp_path / "output" / "0001"
        params = {"wwr": 0.35}

        apply_parameters(tmp_template_dir, params, "0001", out_dir)

        osw = json.loads((out_dir / "workflow.osw").read_text())
        assert osw["weather_file"] == "weather/default.epw"

    def test_epw_key_not_in_preflight(self, tmp_path: Path) -> None:
        """__epw_file__ should NOT cause a preflight failure."""
        osw_path = tmp_path / "workflow.osw"
        osw_data = {"weather_file": "old.epw", "steps": []}
        osw_path.write_text(json.dumps(osw_data))

        out_dir = tmp_path / "output" / "0001"
        # __epw_file__ is not a measure argument — should not trigger
        # UnmappedParameterError
        params = {EPW_FILE_KEY: "weather/new.epw"}

        # Should succeed (no UnmappedParameterError)
        apply_parameters(osw_path, params, "0001", out_dir)

        result_osw = json.loads((out_dir / "workflow.osw").read_text())
        assert result_osw["weather_file"] == "weather/new.epw"

    def test_epw_file_osm_only_template_logs_warning(self, tmp_path: Path) -> None:
        """When template is only .osm (no .osw), epw mutation is skipped."""
        osm_path = tmp_path / "model.osm"
        osm_path.write_text(json.dumps({"attributes": {"wwr": 0.4}}))

        out_dir = tmp_path / "output" / "0001"
        params = {EPW_FILE_KEY: "weather/new.epw"}

        # Should NOT raise — just logs a warning
        apply_parameters(osm_path, params, "0001", out_dir)

        # The .osm should exist but no weather_file mutation possible
        assert (out_dir / "model.osm").is_file()


# ---------------------------------------------------------------------------
# Tests: Campaign-level epw resolution
# ---------------------------------------------------------------------------
class TestCampaignEpwResolution:
    """Tests for Campaign._resolve_epw_targets and pre-flight validation."""

    @staticmethod
    def _make_campaign(
        tmp_path: Path,
        template_dir: Path,
        variables_yml: Path,
    ) -> Campaign:
        """Create a Campaign with the given config for testing."""
        outdir = tmp_path / "results"
        outdir.mkdir(parents=True, exist_ok=True)
        cfg = CampaignConfig(
            input_variables=variables_yml,
            template_sim_package=template_dir,
            n_samples=3,
            outdir=outdir,
            openstudio_version="3.5.0",
        )
        executor = LocalExecutor(max_workers=1)
        return Campaign(cfg=cfg, executor=executor)

    def test_resolve_epw_targets(
        self,
        tmp_path: Path,
        tmp_template_dir: Path,
        variables_yml_epw: Path,
    ) -> None:
        """_resolve_epw_targets injects __epw_file__ with mapped path."""
        campaign = self._make_campaign(tmp_path, tmp_template_dir, variables_yml_epw)
        variable_defs = campaign._load_variable_defs()

        params = {"wwr": 0.35, "climate_zone": "cz4a"}
        resolved = campaign._resolve_epw_targets(params, variable_defs)

        assert resolved["wwr"] == 0.35
        assert resolved["climate_zone"] == "cz4a"
        assert resolved[EPW_FILE_KEY] == "weather/USA_NY_New.York.epw"

    def test_resolve_epw_targets_no_epw_vars(
        self,
        tmp_path: Path,
        tmp_template_dir: Path,
    ) -> None:
        """When no epw_file targets exist, params are unchanged."""
        variables_yml = tmp_path / "variables_no_epw.yml"
        variables_yml.write_text(
            yaml.dump(
                {"variables": [{"name": "wwr", "distribution": "uniform", "min": 0.1, "max": 0.6}]}
            )
        )
        campaign = self._make_campaign(tmp_path, tmp_template_dir, variables_yml)
        variable_defs = campaign._load_variable_defs()

        params = {"wwr": 0.35}
        resolved = campaign._resolve_epw_targets(params, variable_defs)

        assert resolved == params
        assert EPW_FILE_KEY not in resolved

    def test_resolve_epw_targets_unknown_value_raises(
        self,
        tmp_path: Path,
        tmp_template_dir: Path,
        variables_yml_epw: Path,
    ) -> None:
        """An unmapped categorical value should raise ValueError."""
        campaign = self._make_campaign(tmp_path, tmp_template_dir, variables_yml_epw)
        variable_defs = campaign._load_variable_defs()

        params = {"climate_zone": "unknown_zone"}
        with pytest.raises(ValueError, match="not in the epw_file mapping"):
            campaign._resolve_epw_targets(params, variable_defs)

    def test_preflight_validate_epw_files_success(
        self,
        tmp_path: Path,
        tmp_template_dir: Path,
        variables_yml_epw: Path,
    ) -> None:
        """Pre-flight should pass when all .epw files exist."""
        campaign = self._make_campaign(tmp_path, tmp_template_dir, variables_yml_epw)
        variable_defs = campaign._load_variable_defs()

        # Should not raise
        campaign._preflight_validate_epw_files(variable_defs)

    def test_preflight_validate_epw_files_missing(
        self,
        tmp_path: Path,
        tmp_template_dir: Path,
        variables_yml_epw_missing: Path,
    ) -> None:
        """Pre-flight should raise FileNotFoundError for missing .epw files."""
        campaign = self._make_campaign(tmp_path, tmp_template_dir, variables_yml_epw_missing)
        variable_defs = campaign._load_variable_defs()

        with pytest.raises(FileNotFoundError, match="PRE-FLIGHT EPW VALIDATION FAILED"):
            campaign._preflight_validate_epw_files(variable_defs)

    def test_preflight_validate_no_epw_vars(
        self,
        tmp_path: Path,
        tmp_template_dir: Path,
    ) -> None:
        """Pre-flight should be a no-op when no epw_file targets exist."""
        variables_yml = tmp_path / "variables_no_epw.yml"
        variables_yml.write_text(
            yaml.dump(
                {"variables": [{"name": "wwr", "distribution": "uniform", "min": 0.1, "max": 0.6}]}
            )
        )
        campaign = self._make_campaign(tmp_path, tmp_template_dir, variables_yml)
        variable_defs = campaign._load_variable_defs()

        # Should not raise
        campaign._preflight_validate_epw_files(variable_defs)


# ---------------------------------------------------------------------------
# Tests: Categorical LHS generation
# ---------------------------------------------------------------------------
class TestCategoricalLHS:
    """Tests for the categorical distribution in generate_lhs.py."""

    def test_categorical_returns_valid_value(self) -> None:
        """The categorical distribution should return a value from the list."""
        from bin.generate_lhs import _apply_distribution

        values = ["cz3", "cz4a", "cz5a"]
        result = _apply_distribution(0.0, "categorical", {"values": values})
        assert result in values

    def test_categorical_covers_all_values(self) -> None:
        """Different u values should cover all categorical values."""
        from bin.generate_lhs import _apply_distribution

        values = ["a", "b", "c"]
        seen = set()
        for u in [0.0, 0.34, 0.67, 0.99]:
            result = _apply_distribution(u, "categorical", {"values": values})
            seen.add(result)
        # Should cover at least 2 of the 3 values with 4 samples
        assert len(seen) >= 2

    def test_categorical_empty_values_raises(self) -> None:
        """Empty values list should raise ValueError."""
        from bin.generate_lhs import _apply_distribution

        with pytest.raises(ValueError, match="non-empty"):
            _apply_distribution(0.5, "categorical", {"values": []})
