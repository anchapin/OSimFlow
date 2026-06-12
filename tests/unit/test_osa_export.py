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

        exporter = OSAExporter()
        export_dir = tmp_path / "export"
        analysis_path = exporter.export(cfg, export_dir)
        assert analysis_path.exists()

        osa_data = parse_osa(analysis_path)
        reimport_path = tmp_path / "roundtrip_variables.yml"
        osa_to_variables_yml(osa_data, reimport_path)

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


# ---------------------------------------------------------------------------
# Test: pack_osa (ZIP archive production)
# ---------------------------------------------------------------------------


class TestPackOsa:
    """pack_osa() — .osa ZIP archive creation."""

    def _make_config(
        self,
        tmp_path: Path,
        *,
        variables_text: str | None = None,
        with_seed: bool = False,
        extra_files: dict[str, str] | None = None,
        algorithm: str = "lhs",
        n_samples: int = 10,
    ) -> CampaignConfig:
        if variables_text is None:
            variables_text = textwrap.dedent("""\
                variables:
                  - name: x
                    distribution: uniform
                    min: 0.0
                    max: 1.0
            """)
        variables_yml = tmp_path / "variables.yml"
        variables_yml.write_text(variables_text, encoding="utf-8")

        template = tmp_path / "template"
        template.mkdir(parents=True, exist_ok=True)

        if with_seed:
            (template / "seed.osm").write_text("OSM 3.0\n# seed\n", encoding="utf-8")

        if extra_files:
            for relpath, content in extra_files.items():
                fpath = template / relpath
                fpath.parent.mkdir(parents=True, exist_ok=True)
                fpath.write_text(content, encoding="utf-8")

        return CampaignConfig(
            input_variables=variables_yml,
            template_sim_package=template,
            n_samples=n_samples,
            outdir=tmp_path / "results",
            openstudio_version="3.11.0",
            algorithm=algorithm,
        )

    def test_pack_creates_osa_file(self, tmp_path: Path) -> None:
        cfg = self._make_config(tmp_path)
        osa_path = OSAExporter().pack_osa(cfg, tmp_path / "pack_out")
        assert osa_path.exists()
        assert osa_path.suffix == ".osa"

    def test_pack_is_valid_zip(self, tmp_path: Path) -> None:
        cfg = self._make_config(tmp_path)
        osa_path = OSAExporter().pack_osa(cfg, tmp_path / "pack_out")
        import zipfile

        assert zipfile.is_zipfile(osa_path)

    def test_pack_contains_analysis_json(self, tmp_path: Path) -> None:
        cfg = self._make_config(tmp_path)
        osa_path = OSAExporter().pack_osa(cfg, tmp_path / "pack_out")
        import zipfile

        with zipfile.ZipFile(osa_path) as zf:
            assert "analysis.json" in zf.namelist()

    def test_pack_analysis_json_valid(self, tmp_path: Path) -> None:
        cfg = self._make_config(tmp_path)
        osa_path = OSAExporter().pack_osa(cfg, tmp_path / "pack_out")
        import zipfile

        with zipfile.ZipFile(osa_path) as zf:
            data = json.loads(zf.read("analysis.json"))
            assert "analysis" in data
            assert "server" in data

    def test_pack_seed_osm_included(self, tmp_path: Path) -> None:
        cfg = self._make_config(tmp_path, with_seed=True)
        osa_path = OSAExporter().pack_osa(cfg, tmp_path / "pack_out")
        import zipfile

        with zipfile.ZipFile(osa_path) as zf:
            assert "seed.osm" in zf.namelist()
            content = zf.read("seed.osm").decode("utf-8")
            assert "seed" in content

    def test_pack_seed_missing_note(self, tmp_path: Path) -> None:
        cfg = self._make_config(tmp_path, with_seed=False)
        osa_path = OSAExporter().pack_osa(cfg, tmp_path / "pack_out")
        import zipfile

        with zipfile.ZipFile(osa_path) as zf:
            data = json.loads(zf.read("analysis.json"))
            assert "_seed_missing_note" in data
            assert "No seed model" in data["_seed_missing_note"]

    def test_pack_no_seed_note_when_osm_present(self, tmp_path: Path) -> None:
        cfg = self._make_config(tmp_path, with_seed=True)
        osa_path = OSAExporter().pack_osa(cfg, tmp_path / "pack_out")
        import zipfile

        with zipfile.ZipFile(osa_path) as zf:
            data = json.loads(zf.read("analysis.json"))
            assert "_seed_missing_note" not in data

    def test_pack_measures_included(self, tmp_path: Path) -> None:
        cfg = self._make_config(
            tmp_path,
            extra_files={
                "measures/SetRValue/measure.rb": "# Ruby measure",
                "measures/SetRValue/measure.xml": "<measure/>",
            },
        )
        osa_path = OSAExporter().pack_osa(cfg, tmp_path / "pack_out")
        import zipfile

        with zipfile.ZipFile(osa_path) as zf:
            names = zf.namelist()
            assert "measures/SetRValue/measure.rb" in names
            assert "measures/SetRValue/measure.xml" in names

    def test_pack_weather_included(self, tmp_path: Path) -> None:
        cfg = self._make_config(
            tmp_path,
            extra_files={"weather/denver.epw": "LOCATION,DENVER"},
        )
        osa_path = OSAExporter().pack_osa(cfg, tmp_path / "pack_out")
        import zipfile

        with zipfile.ZipFile(osa_path) as zf:
            assert "weather/denver.epw" in zf.namelist()

    def test_pack_empty_weather_ok(self, tmp_path: Path) -> None:
        cfg = self._make_config(tmp_path, extra_files=None)
        osa_path = OSAExporter().pack_osa(cfg, tmp_path / "pack_out")
        import zipfile

        with zipfile.ZipFile(osa_path) as zf:
            names = zf.namelist()
            weather_files = [n for n in names if "weather" in n]
            assert weather_files == []

    def test_pack_osm_not_duplicated(self, tmp_path: Path) -> None:
        cfg = self._make_config(tmp_path, with_seed=True)
        osa_path = OSAExporter().pack_osa(cfg, tmp_path / "pack_out")
        import zipfile

        with zipfile.ZipFile(osa_path) as zf:
            osm_names = [n for n in zf.namelist() if n.endswith(".osm")]
            assert osm_names == ["seed.osm"]

    def test_pack_creates_outdir(self, tmp_path: Path) -> None:
        cfg = self._make_config(tmp_path)
        deep_out = tmp_path / "nested" / "deep"
        osa_path = OSAExporter().pack_osa(cfg, deep_out)
        assert deep_out.exists()
        assert osa_path.exists()

    def test_pack_no_template_sim_package(self, tmp_path: Path) -> None:
        variables_yml = tmp_path / "variables.yml"
        variables_yml.write_text(
            "variables:\n  - name: x\n    distribution: uniform\n    min: 0\n    max: 1\n",
            encoding="utf-8",
        )
        nonexistent = tmp_path / "nonexistent_template"
        cfg = CampaignConfig(
            input_variables=variables_yml,
            template_sim_package=nonexistent,
            n_samples=5,
            outdir=tmp_path / "out",
            openstudio_version="3.11.0",
        )
        osa_path = OSAExporter().pack_osa(cfg, tmp_path / "pack_out")
        import zipfile

        assert osa_path.exists()
        with zipfile.ZipFile(osa_path) as zf:
            data = json.loads(zf.read("analysis.json"))
            assert "_seed_missing_note" in data


# ---------------------------------------------------------------------------
# Test: additional edge cases for _convert_variable branches
# ---------------------------------------------------------------------------


class TestConvertVariableEdgeCases:
    """Cover branches in _convert_variable and _convert_distribution."""

    def test_variable_without_measure_argument(self, tmp_path: Path) -> None:
        variables_yml = tmp_path / "variables.yml"
        variables_yml.write_text(
            textwrap.dedent("""\
                variables:
                  - name: bare_var
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
        result = exporter.export(cfg, tmp_path / "export")
        data = json.loads(result.read_text(encoding="utf-8"))
        var = data["analysis"]["problem"]["variables"][0]
        assert "measure" not in var

    def test_measure_argument_without_dot(self, tmp_path: Path) -> None:
        variables_yml = tmp_path / "variables.yml"
        variables_yml.write_text(
            textwrap.dedent("""\
                variables:
                  - name: var1
                    distribution: uniform
                    min: 0.0
                    max: 1.0
                    measure_argument: no_dot_value
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
        result = exporter.export(cfg, tmp_path / "export")
        data = json.loads(result.read_text(encoding="utf-8"))
        var = data["analysis"]["problem"]["variables"][0]
        assert "measure" not in var

    def test_measure_argument_non_string(self, tmp_path: Path) -> None:
        variables_yml = tmp_path / "variables.yml"
        variables_yml.write_text(
            textwrap.dedent("""\
                variables:
                  - name: var1
                    distribution: uniform
                    min: 0.0
                    max: 1.0
                    measure_argument: 42
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
        result = exporter.export(cfg, tmp_path / "export")
        data = json.loads(result.read_text(encoding="utf-8"))
        var = data["analysis"]["problem"]["variables"][0]
        assert "measure" not in var

    def test_unknown_distribution_maps_to_uniform(self, tmp_path: Path) -> None:
        variables_yml = tmp_path / "variables.yml"
        variables_yml.write_text(
            textwrap.dedent("""\
                variables:
                  - name: mystery
                    distribution: unknown_dist
                    min: 2.0
                    max: 8.0
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
        result = exporter.export(cfg, tmp_path / "export")
        data = json.loads(result.read_text(encoding="utf-8"))
        var = data["analysis"]["problem"]["variables"][0]
        assert var["distribution"]["type"] == "uniform"

    def test_variables_not_list(self, tmp_path: Path) -> None:
        variables_yml = tmp_path / "variables.yml"
        variables_yml.write_text("variables: not_a_list\n", encoding="utf-8")
        cfg = CampaignConfig(
            input_variables=variables_yml,
            template_sim_package=tmp_path,
            n_samples=5,
            outdir=tmp_path / "out",
            openstudio_version="3.11.0",
        )
        exporter = OSAExporter()
        result = exporter.export(cfg, tmp_path / "export")
        data = json.loads(result.read_text(encoding="utf-8"))
        assert data["analysis"]["problem"]["variables"] == []

    def test_yaml_not_dict(self, tmp_path: Path) -> None:
        variables_yml = tmp_path / "variables.yml"
        variables_yml.write_text("just_a_string\n", encoding="utf-8")
        cfg = CampaignConfig(
            input_variables=variables_yml,
            template_sim_package=tmp_path,
            n_samples=5,
            outdir=tmp_path / "out",
            openstudio_version="3.11.0",
        )
        exporter = OSAExporter()
        result = exporter.export(cfg, tmp_path / "export")
        data = json.loads(result.read_text(encoding="utf-8"))
        assert data["analysis"]["problem"]["variables"] == []

    def test_variable_entry_not_dict(self, tmp_path: Path) -> None:
        variables_yml = tmp_path / "variables.yml"
        variables_yml.write_text(
            "variables:\n  - 'string_entry'\n  - name: ok\n    distribution: uniform\n    min: 0\n    max: 1\n",
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
        result = exporter.export(cfg, tmp_path / "export")
        data = json.loads(result.read_text(encoding="utf-8"))
        variables = data["analysis"]["problem"]["variables"]
        assert len(variables) == 1
        assert variables[0]["name"] == "ok"

    def test_no_display_name_omitted(self, tmp_path: Path) -> None:
        variables_yml = tmp_path / "variables.yml"
        variables_yml.write_text(
            textwrap.dedent("""\
                variables:
                  - name: x
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
        result = exporter.export(cfg, tmp_path / "export")
        data = json.loads(result.read_text(encoding="utf-8"))
        var = data["analysis"]["problem"]["variables"][0]
        assert "display_name" not in var

    def test_exponential_lossy_no_min_max(self, tmp_path: Path) -> None:
        variables_yml = tmp_path / "variables.yml"
        variables_yml.write_text(
            textwrap.dedent("""\
                variables:
                  - name: exp_var
                    distribution: exponential
                    loc: 1.0
                    scale: 3.0
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
        result = exporter.export(cfg, tmp_path / "export")
        data = json.loads(result.read_text(encoding="utf-8"))
        var = data["analysis"]["problem"]["variables"][0]
        assert var["distribution"]["type"] == "uniform"
        assert var["distribution"]["minimum"] == 1.0
        assert var["distribution"]["maximum"] == 1.0 + 3.0 * 4
