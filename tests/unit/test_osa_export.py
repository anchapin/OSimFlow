"""Unit tests for osimflow.exporters.osa — OSA export converter.

Covers:
- OSAExporter.export() produces valid analysis.json
- Algorithm name translation (all known algorithms + unknown fallback)
- Variable serialization from variables.yml
- Distribution conversion (direct, lossy, discrete/categorical)
- Exported JSON is parseable
- Round-trip fidelity: export → import preserves structure
"""

import json
import textwrap
from pathlib import Path

import pytest
import yaml

from osimflow.config import CampaignConfig
from osimflow.exporters.osa import (
    _LOSSY_DISTRIBUTIONS,
    _OSIMFLOW_ALGO_TO_OSA,
    OSAExporter,
)
from osimflow.importers.osa import osa_to_variables_yml, parse_osa

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_config(tmp_path: Path) -> CampaignConfig:
    """Build a minimal CampaignConfig for testing."""
    variables_yml = tmp_path / "variables.yml"
    variables_yml.write_text(
        textwrap.dedent("""\
            variables:
              - name: window_u_value
                distribution: uniform
                min: 1.0
                max: 5.0
              - name: infiltration_rate
                distribution: lognormal
                mean: 0.5
                sigma: 0.2
              - name: hvac_setpoint
                distribution: normal
                mean: 22.0
                sigma: 1.0
        """),
        encoding="utf-8",
    )
    return CampaignConfig(
        input_variables=variables_yml,
        template_sim_package=tmp_path / "template",
        n_samples=50,
        outdir=tmp_path / "results",
        openstudio_version="3.11.0",
        algorithm="lhs",
    )


@pytest.fixture
def exporter() -> OSAExporter:
    return OSAExporter()


# ---------------------------------------------------------------------------
# Test: export produces valid analysis.json
# ---------------------------------------------------------------------------


class TestExportBasic:
    """Basic export functionality."""

    def test_export_creates_file(
        self, exporter: OSAExporter, tmp_config: CampaignConfig, tmp_path: Path
    ) -> None:
        outdir = tmp_path / "export_out"
        result = exporter.export(tmp_config, outdir)
        assert result.exists()
        assert result.name == "analysis.json"

    def test_export_is_valid_json(
        self, exporter: OSAExporter, tmp_config: CampaignConfig, tmp_path: Path
    ) -> None:
        outdir = tmp_path / "export_out"
        result = exporter.export(tmp_config, outdir)
        data = json.loads(result.read_text(encoding="utf-8"))
        assert isinstance(data, dict)

    def test_export_has_required_top_level_keys(
        self, exporter: OSAExporter, tmp_config: CampaignConfig, tmp_path: Path
    ) -> None:
        outdir = tmp_path / "export_out"
        result = exporter.export(tmp_config, outdir)
        data = json.loads(result.read_text(encoding="utf-8"))
        assert "analysis" in data
        assert "server" in data

    def test_export_analysis_structure(
        self, exporter: OSAExporter, tmp_config: CampaignConfig, tmp_path: Path
    ) -> None:
        outdir = tmp_path / "export_out"
        result = exporter.export(tmp_config, outdir)
        data = json.loads(result.read_text(encoding="utf-8"))
        analysis = data["analysis"]
        assert "display_name" in analysis
        assert "algorithm" in analysis
        assert "problem" in analysis

        problem = analysis["problem"]
        assert "algorithm" in problem
        assert "variables" in problem
        assert "file_format_version" in problem
        assert problem["file_format_version"] == 1

    def test_export_n_samples(
        self, exporter: OSAExporter, tmp_config: CampaignConfig, tmp_path: Path
    ) -> None:
        outdir = tmp_path / "export_out"
        result = exporter.export(tmp_config, outdir)
        data = json.loads(result.read_text(encoding="utf-8"))
        assert data["analysis"]["algorithm"]["number_of_samples"] == 50
        assert data["analysis"]["problem"]["algorithm"]["number_of_samples"] == 50

    def test_export_server_version(
        self, exporter: OSAExporter, tmp_config: CampaignConfig, tmp_path: Path
    ) -> None:
        outdir = tmp_path / "export_out"
        result = exporter.export(tmp_config, outdir)
        data = json.loads(result.read_text(encoding="utf-8"))
        assert data["server"]["base_oscli_version"] == "3.11.0"

    def test_export_server_default_version(self, tmp_path: Path) -> None:
        """When openstudio_version is empty, defaults to 3.11.0."""
        variables_yml = tmp_path / "variables.yml"
        variables_yml.write_text("variables: []\n", encoding="utf-8")
        cfg = CampaignConfig(
            input_variables=variables_yml,
            template_sim_package=tmp_path,
            n_samples=5,
            outdir=tmp_path / "out",
            openstudio_version="",
        )
        exporter = OSAExporter()
        outdir = tmp_path / "export_out"
        result = exporter.export(cfg, outdir)
        data = json.loads(result.read_text(encoding="utf-8"))
        assert data["server"]["base_oscli_version"] == "3.11.0"

    def test_export_creates_outdir(
        self, exporter: OSAExporter, tmp_config: CampaignConfig, tmp_path: Path
    ) -> None:
        outdir = tmp_path / "nested" / "deep" / "dir"
        result = exporter.export(tmp_config, outdir)
        assert outdir.exists()
        assert result.exists()


# ---------------------------------------------------------------------------
# Test: algorithm translation
# ---------------------------------------------------------------------------


class TestAlgorithmTranslation:
    """Algorithm name translation between OSimFlow and OSA formats."""

    @pytest.mark.parametrize(
        ("osimflow_name", "expected_osa"),
        list(_OSIMFLOW_ALGO_TO_OSA.items()),
        ids=list(_OSIMFLOW_ALGO_TO_OSA.keys()),
    )
    def test_known_algorithms(self, osimflow_name: str, expected_osa: str) -> None:
        exporter = OSAExporter()
        assert exporter._translate_algorithm(osimflow_name) == expected_osa

    def test_unknown_algorithm_defaults_to_lhs(self) -> None:
        exporter = OSAExporter()
        assert exporter._translate_algorithm("nonexistent_algorithm") == "lhs"

    def test_empty_string_defaults_to_lhs(self) -> None:
        exporter = OSAExporter()
        assert exporter._translate_algorithm("") == "lhs"

    def test_all_known_algorithms_covered(self) -> None:
        """Ensure all algorithms in the translation table produce non-empty results."""
        for name in _OSIMFLOW_ALGO_TO_OSA:
            result = OSAExporter()._translate_algorithm(name)
            assert result, f"Algorithm {name!r} produced empty translation"


# ---------------------------------------------------------------------------
# Test: variable serialization
# ---------------------------------------------------------------------------


class TestVariableSerialization:
    """Variable conversion from variables.yml to OSA format."""

    def test_uniform_variable(self, tmp_path: Path) -> None:
        variables_yml = tmp_path / "variables.yml"
        variables_yml.write_text(
            textwrap.dedent("""\
                variables:
                  - name: test_var
                    distribution: uniform
                    min: 0.0
                    max: 10.0
            """),
            encoding="utf-8",
        )
        cfg = CampaignConfig(
            input_variables=variables_yml,
            template_sim_package=tmp_path,
            n_samples=5,
            outdir=tmp_path / "out",
            openstudio_version="3.11.0",
        )
        exporter = OSAExporter()
        outdir = tmp_path / "export"
        result = exporter.export(cfg, outdir)
        data = json.loads(result.read_text(encoding="utf-8"))
        variables = data["analysis"]["problem"]["variables"]
        assert len(variables) == 1
        var = variables[0]
        assert var["name"] == "test_var"
        assert var["distribution"]["type"] == "uniform"
        assert var["distribution"]["minimum"] == 0.0
        assert var["distribution"]["maximum"] == 10.0

    def test_normal_variable(self, tmp_path: Path) -> None:
        variables_yml = tmp_path / "variables.yml"
        variables_yml.write_text(
            textwrap.dedent("""\
                variables:
                  - name: setpoint
                    distribution: normal
                    mean: 22.0
                    sigma: 1.0
            """),
            encoding="utf-8",
        )
        cfg = CampaignConfig(
            input_variables=variables_yml,
            template_sim_package=tmp_path,
            n_samples=5,
            outdir=tmp_path / "out",
            openstudio_version="3.11.0",
        )
        exporter = OSAExporter()
        outdir = tmp_path / "export"
        result = exporter.export(cfg, outdir)
        data = json.loads(result.read_text(encoding="utf-8"))
        var = data["analysis"]["problem"]["variables"][0]
        assert var["distribution"]["type"] == "normal"
        assert var["distribution"]["mean"] == 22.0
        assert var["distribution"]["stddev"] == 1.0

    def test_lognormal_variable(self, tmp_path: Path) -> None:
        variables_yml = tmp_path / "variables.yml"
        variables_yml.write_text(
            textwrap.dedent("""\
                variables:
                  - name: infil
                    distribution: lognormal
                    mean: 0.5
                    sigma: 0.2
            """),
            encoding="utf-8",
        )
        cfg = CampaignConfig(
            input_variables=variables_yml,
            template_sim_package=tmp_path,
            n_samples=5,
            outdir=tmp_path / "out",
            openstudio_version="3.11.0",
        )
        exporter = OSAExporter()
        outdir = tmp_path / "export"
        result = exporter.export(cfg, outdir)
        data = json.loads(result.read_text(encoding="utf-8"))
        var = data["analysis"]["problem"]["variables"][0]
        assert var["distribution"]["type"] == "lognormal"
        assert var["distribution"]["mean"] == 0.5
        assert var["distribution"]["stddev"] == 0.2

    def test_triangular_variable(self, tmp_path: Path) -> None:
        variables_yml = tmp_path / "variables.yml"
        variables_yml.write_text(
            textwrap.dedent("""\
                variables:
                  - name: lpd
                    distribution: triangular
                    min: 5.0
                    max: 15.0
                    mode: 10.0
            """),
            encoding="utf-8",
        )
        cfg = CampaignConfig(
            input_variables=variables_yml,
            template_sim_package=tmp_path,
            n_samples=5,
            outdir=tmp_path / "out",
            openstudio_version="3.11.0",
        )
        exporter = OSAExporter()
        outdir = tmp_path / "export"
        result = exporter.export(cfg, outdir)
        data = json.loads(result.read_text(encoding="utf-8"))
        var = data["analysis"]["problem"]["variables"][0]
        assert var["distribution"]["type"] == "triangular"
        assert var["distribution"]["minimum"] == 5.0
        assert var["distribution"]["maximum"] == 15.0
        assert var["distribution"]["mode"] == 10.0

    def test_discrete_variable(self, tmp_path: Path) -> None:
        variables_yml = tmp_path / "variables.yml"
        variables_yml.write_text(
            textwrap.dedent("""\
                variables:
                  - name: hvac_type
                    distribution: discrete
                    values: ["gas_furnace", "heat_pump", "electric"]
            """),
            encoding="utf-8",
        )
        cfg = CampaignConfig(
            input_variables=variables_yml,
            template_sim_package=tmp_path,
            n_samples=5,
            outdir=tmp_path / "out",
            openstudio_version="3.11.0",
        )
        exporter = OSAExporter()
        outdir = tmp_path / "export"
        result = exporter.export(cfg, outdir)
        data = json.loads(result.read_text(encoding="utf-8"))
        var = data["analysis"]["problem"]["variables"][0]
        assert var["distribution"]["type"] == "discrete"
        assert var["distribution"]["values"] == ["gas_furnace", "heat_pump", "electric"]

    def test_categorical_variable(self, tmp_path: Path) -> None:
        variables_yml = tmp_path / "variables.yml"
        variables_yml.write_text(
            textwrap.dedent("""\
                variables:
                  - name: climate_zone
                    distribution: categorical
                    values: ["CZ4", "CZ5"]
                    mapping:
                      CZ4: 4
                      CZ5: 5
            """),
            encoding="utf-8",
        )
        cfg = CampaignConfig(
            input_variables=variables_yml,
            template_sim_package=tmp_path,
            n_samples=5,
            outdir=tmp_path / "out",
            openstudio_version="3.11.0",
        )
        exporter = OSAExporter()
        outdir = tmp_path / "export"
        result = exporter.export(cfg, outdir)
        data = json.loads(result.read_text(encoding="utf-8"))
        var = data["analysis"]["problem"]["variables"][0]
        assert var["distribution"]["type"] == "categorical"
        assert var["distribution"]["mapping"] == {"CZ4": 4, "CZ5": 5}

    def test_measure_argument_serialized(self, tmp_path: Path) -> None:
        variables_yml = tmp_path / "variables.yml"
        variables_yml.write_text(
            textwrap.dedent("""\
                variables:
                  - name: insul_r
                    distribution: uniform
                    min: 5.0
                    max: 30.0
                    measure_argument: SetInsulationRValue.r_value
            """),
            encoding="utf-8",
        )
        cfg = CampaignConfig(
            input_variables=variables_yml,
            template_sim_package=tmp_path,
            n_samples=5,
            outdir=tmp_path / "out",
            openstudio_version="3.11.0",
        )
        exporter = OSAExporter()
        outdir = tmp_path / "export"
        result = exporter.export(cfg, outdir)
        data = json.loads(result.read_text(encoding="utf-8"))
        var = data["analysis"]["problem"]["variables"][0]
        assert "measure" in var
        assert var["measure"]["display_name"] == "SetInsulationRValue"
        assert var["measure"]["argument"] == "r_value"

    def test_multiple_variables(self, tmp_path: Path) -> None:
        variables_yml = tmp_path / "variables.yml"
        variables_yml.write_text(
            textwrap.dedent("""\
                variables:
                  - name: var_a
                    distribution: uniform
                    min: 0.0
                    max: 1.0
                  - name: var_b
                    distribution: normal
                    mean: 10.0
                    sigma: 2.0
                  - name: var_c
                    distribution: lognormal
                    mean: 1.0
                    sigma: 0.5
            """),
            encoding="utf-8",
        )
        cfg = CampaignConfig(
            input_variables=variables_yml,
            template_sim_package=tmp_path,
            n_samples=5,
            outdir=tmp_path / "out",
            openstudio_version="3.11.0",
        )
        exporter = OSAExporter()
        outdir = tmp_path / "export"
        result = exporter.export(cfg, outdir)
        data = json.loads(result.read_text(encoding="utf-8"))
        variables = data["analysis"]["problem"]["variables"]
        assert len(variables) == 3
        assert [v["name"] for v in variables] == ["var_a", "var_b", "var_c"]

    def test_empty_variables(self, tmp_path: Path) -> None:
        variables_yml = tmp_path / "variables.yml"
        variables_yml.write_text("variables: []\n", encoding="utf-8")
        cfg = CampaignConfig(
            input_variables=variables_yml,
            template_sim_package=tmp_path,
            n_samples=5,
            outdir=tmp_path / "out",
            openstudio_version="3.11.0",
        )
        exporter = OSAExporter()
        outdir = tmp_path / "export"
        result = exporter.export(cfg, outdir)
        data = json.loads(result.read_text(encoding="utf-8"))
        assert data["analysis"]["problem"]["variables"] == []

    def test_missing_variables_file(self, tmp_path: Path) -> None:
        nonexistent = tmp_path / "nonexistent.yml"
        cfg = CampaignConfig(
            input_variables=nonexistent,
            template_sim_package=tmp_path,
            n_samples=5,
            outdir=tmp_path / "out",
            openstudio_version="3.11.0",
        )
        exporter = OSAExporter()
        outdir = tmp_path / "export"
        result = exporter.export(cfg, outdir)
        data = json.loads(result.read_text(encoding="utf-8"))
        assert data["analysis"]["problem"]["variables"] == []


# ---------------------------------------------------------------------------
# Test: lossy distribution conversion
# ---------------------------------------------------------------------------


class TestLossyDistributions:
    """Beta, gamma, exponential → uniform fallback."""

    @pytest.mark.parametrize("dist_name", sorted(_LOSSY_DISTRIBUTIONS))
    def test_lossy_becomes_uniform(self, dist_name: str, tmp_path: Path) -> None:
        variables_yml = tmp_path / "variables.yml"
        variables_yml.write_text(
            textwrap.dedent(f"""\
                variables:
                  - name: test_var
                    distribution: {dist_name}
                    min: 1.0
                    max: 5.0
            """),
            encoding="utf-8",
        )
        cfg = CampaignConfig(
            input_variables=variables_yml,
            template_sim_package=tmp_path,
            n_samples=5,
            outdir=tmp_path / "out",
            openstudio_version="3.11.0",
        )
        exporter = OSAExporter()
        outdir = tmp_path / "export"
        result = exporter.export(cfg, outdir)
        data = json.loads(result.read_text(encoding="utf-8"))
        var = data["analysis"]["problem"]["variables"][0]
        assert var["distribution"]["type"] == "uniform"
        assert var["distribution"]["minimum"] == 1.0
        assert var["distribution"]["maximum"] == 5.0

    def test_gamma_without_min_max(self, tmp_path: Path) -> None:
        """Gamma with loc/scale but no explicit min/max should derive range."""
        variables_yml = tmp_path / "variables.yml"
        variables_yml.write_text(
            textwrap.dedent("""\
                variables:
                  - name: gamma_var
                    distribution: gamma
                    alpha: 2.0
                    loc: 0.0
                    scale: 5.0
            """),
            encoding="utf-8",
        )
        cfg = CampaignConfig(
            input_variables=variables_yml,
            template_sim_package=tmp_path,
            n_samples=5,
            outdir=tmp_path / "out",
            openstudio_version="3.11.0",
        )
        exporter = OSAExporter()
        outdir = tmp_path / "export"
        result = exporter.export(cfg, outdir)
        data = json.loads(result.read_text(encoding="utf-8"))
        var = data["analysis"]["problem"]["variables"][0]
        assert var["distribution"]["type"] == "uniform"
        # loc=0, scale=5 → max = 0 + 5*4 = 20
        assert var["distribution"]["minimum"] == 0.0
        assert var["distribution"]["maximum"] == 20.0


# ---------------------------------------------------------------------------
# Test: exported JSON is valid JSON
# ---------------------------------------------------------------------------


class TestJsonValidity:
    """Ensure exported files are well-formed JSON."""

    def test_parseable_json(
        self, exporter: OSAExporter, tmp_config: CampaignConfig, tmp_path: Path
    ) -> None:
        outdir = tmp_path / "export"
        result = exporter.export(tmp_config, outdir)
        text = result.read_text(encoding="utf-8")
        # Should not raise
        data = json.loads(text)
        assert isinstance(data, dict)

    def test_json_has_trailing_newline(
        self, exporter: OSAExporter, tmp_config: CampaignConfig, tmp_path: Path
    ) -> None:
        outdir = tmp_path / "export"
        result = exporter.export(tmp_config, outdir)
        text = result.read_text(encoding="utf-8")
        assert text.endswith("\n"), "analysis.json should end with a newline"

    def test_json_indent_formatted(
        self, exporter: OSAExporter, tmp_config: CampaignConfig, tmp_path: Path
    ) -> None:
        outdir = tmp_path / "export"
        result = exporter.export(tmp_config, outdir)
        text = result.read_text(encoding="utf-8")
        # Indented JSON should have newlines inside
        assert "\n" in text.strip()
        # Should have 2-space indentation
        assert '  "' in text


# ---------------------------------------------------------------------------
# Test: variable without name is skipped
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Edge cases and robustness."""

    def test_variable_without_name_skipped(self, tmp_path: Path) -> None:
        variables_yml = tmp_path / "variables.yml"
        variables_yml.write_text(
            textwrap.dedent("""\
                variables:
                  - distribution: uniform
                    min: 0.0
                    max: 1.0
                  - name: valid_var
                    distribution: uniform
                    min: 0.0
                    max: 1.0
            """),
            encoding="utf-8",
        )
        cfg = CampaignConfig(
            input_variables=variables_yml,
            template_sim_package=tmp_path,
            n_samples=5,
            outdir=tmp_path / "out",
            openstudio_version="3.11.0",
        )
        exporter = OSAExporter()
        outdir = tmp_path / "export"
        result = exporter.export(cfg, outdir)
        data = json.loads(result.read_text(encoding="utf-8"))
        variables = data["analysis"]["problem"]["variables"]
        assert len(variables) == 1
        assert variables[0]["name"] == "valid_var"

    def test_display_name_carried_through(self, tmp_path: Path) -> None:
        variables_yml = tmp_path / "variables.yml"
        variables_yml.write_text(
            textwrap.dedent("""\
                variables:
                  - name: var1
                    display_name: "My Special Variable"
                    distribution: uniform
                    min: 0.0
                    max: 1.0
            """),
            encoding="utf-8",
        )
        cfg = CampaignConfig(
            input_variables=variables_yml,
            template_sim_package=tmp_path,
            n_samples=5,
            outdir=tmp_path / "out",
            openstudio_version="3.11.0",
        )
        exporter = OSAExporter()
        outdir = tmp_path / "export"
        result = exporter.export(cfg, outdir)
        data = json.loads(result.read_text(encoding="utf-8"))
        var = data["analysis"]["problem"]["variables"][0]
        assert var.get("display_name") == "My Special Variable"

    def test_invalid_yaml_returns_empty(self, tmp_path: Path) -> None:
        variables_yml = tmp_path / "variables.yml"
        variables_yml.write_text(": invalid yaml {{{", encoding="utf-8")
        cfg = CampaignConfig(
            input_variables=variables_yml,
            template_sim_package=tmp_path,
            n_samples=5,
            outdir=tmp_path / "out",
            openstudio_version="3.11.0",
        )
        exporter = OSAExporter()
        outdir = tmp_path / "export"
        result = exporter.export(cfg, outdir)
        data = json.loads(result.read_text(encoding="utf-8"))
        assert data["analysis"]["problem"]["variables"] == []


# ---------------------------------------------------------------------------
# Test: round-trip (export → import) basic check
# ---------------------------------------------------------------------------


class TestRoundTrip:
    """Verify that export → import preserves the essential structure."""

    def test_round_trip_uniform(self, tmp_path: Path) -> None:
        """Export a uniform variable, re-import it, check it survives."""
        # Step 1: Create variables.yml
        variables_yml = tmp_path / "variables.yml"
        variables_yml.write_text(
            textwrap.dedent("""\
                variables:
                  - name: insulation_r
                    distribution: uniform
                    min: 5.0
                    max: 30.0
                    measure_argument: SetInsulationRValue.r_value
            """),
            encoding="utf-8",
        )
        cfg = CampaignConfig(
            input_variables=variables_yml,
            template_sim_package=tmp_path,
            n_samples=20,
            outdir=tmp_path / "out",
            openstudio_version="3.11.0",
            algorithm="lhs",
        )

        # Step 2: Export to analysis.json
        exporter = OSAExporter()
        export_dir = tmp_path / "export"
        analysis_path = exporter.export(cfg, export_dir)
        assert analysis_path.exists()

        # Step 3: Re-import
        osa_data = parse_osa(analysis_path)
        reimport_path = tmp_path / "roundtrip_variables.yml"
        osa_to_variables_yml(osa_data, reimport_path)

        # Step 4: Verify the re-imported file
        with reimport_path.open(encoding="utf-8") as f:
            reimported = yaml.safe_load(f)

        assert reimported["algorithm"] == "lhs"
        variables = reimported["variables"]
        assert len(variables) == 1
        var = variables[0]
        assert var["name"] == "insulation_r"
        assert var["distribution"] == "uniform"
        assert var["min"] == 5.0
        assert var["max"] == 30.0
        assert var["measure_argument"] == "SetInsulationRValue.r_value"
