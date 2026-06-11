"""Integration test: OSA .osa zip packaging and round-trip fidelity (G12b, #134).

Verifies that:
- ``OSAExporter.pack_osa()`` produces a valid .osa ZIP archive.
- The archive contains ``analysis.json`` at the root.
- ``seed.osm`` is included when a seed model exists in template_sim_package.
- ``_seed_missing_note`` is set when no seed model is present.
- Export → pack → unpack → import preserves algorithm type, variable names,
  distributions, and measure arguments.
- Template package files (measures, weather) survive the round trip inside the
  ZIP.
"""

import json
import textwrap
import zipfile
from pathlib import Path

import yaml

from osimflow.config import CampaignConfig
from osimflow.exporters.osa import OSAExporter
from osimflow.importers.osa import osa_to_variables_yml, parse_osa

# ---------------------------------------------------------------------------
# Test data constants
# ---------------------------------------------------------------------------

_MINIMAL_VARIABLES = textwrap.dedent("""\
    variables:
      - name: x
        distribution: uniform
        min: 0.0
        max: 1.0
""")

VARIABLES_YML = textwrap.dedent("""\
    variables:
      - name: insulation_r
        distribution: uniform
        min: 5.0
        max: 30.0
        measure_argument: SetInsulationRValue.r_value
      - name: wwr
        distribution: normal
        mean: 0.4
        sigma: 0.05
        measure_argument: SetWWR.wwr_value
      - name: shgc
        distribution: lognormal
        mean: 0.3
        sigma: 0.1
      - name: roof_abs
        distribution: triangular
        min: 0.1
        max: 0.9
        mode: 0.5
      - name: hvac_type
        distribution: discrete
        values: ["packaged_rooftop", "vav", "gshtp"]
""")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(
    tmp_path: Path,
    *,
    variables_text: str = _MINIMAL_VARIABLES,
    algorithm: str = "lhs",
    n_samples: int = 20,
    with_seed: bool = False,
    extra_files: dict[str, str] | None = None,
) -> CampaignConfig:
    """Build a CampaignConfig with a variables.yml and optional template files."""
    variables_yml = tmp_path / "variables.yml"
    variables_yml.write_text(variables_text, encoding="utf-8")

    template = tmp_path / "template"
    template.mkdir(parents=True, exist_ok=True)

    if with_seed:
        (template / "seed.osm").write_text(
            "OSM 3.0\n# placeholder seed model\n",
            encoding="utf-8",
        )

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


# ---------------------------------------------------------------------------
# Tests: pack_osa basic structure
# ---------------------------------------------------------------------------


class TestPackOsaBasic:
    """Basic .osa packaging functionality."""

    def test_pack_produces_osa_file(self, tmp_path: Path) -> None:
        cfg = _make_config(tmp_path)
        exporter = OSAExporter()
        osa_path = exporter.pack_osa(cfg, tmp_path / "pack_out")
        assert osa_path.exists()
        assert osa_path.suffix == ".osa"

    def test_pack_is_valid_zip(self, tmp_path: Path) -> None:
        cfg = _make_config(tmp_path)
        exporter = OSAExporter()
        osa_path = exporter.pack_osa(cfg, tmp_path / "pack_out")
        assert zipfile.is_zipfile(osa_path)

    def test_pack_contains_analysis_json(self, tmp_path: Path) -> None:
        cfg = _make_config(tmp_path)
        exporter = OSAExporter()
        osa_path = exporter.pack_osa(cfg, tmp_path / "pack_out")
        with zipfile.ZipFile(osa_path) as zf:
            assert "analysis.json" in zf.namelist()

    def test_pack_analysis_json_is_valid(self, tmp_path: Path) -> None:
        cfg = _make_config(tmp_path)
        exporter = OSAExporter()
        osa_path = exporter.pack_osa(cfg, tmp_path / "pack_out")
        with zipfile.ZipFile(osa_path) as zf:
            data = json.loads(zf.read("analysis.json"))
            assert "analysis" in data
            assert "server" in data


class TestPackOsaSeedModel:
    """Seed model (.osm) handling in .osa archives."""

    def test_seed_osm_included_when_present(self, tmp_path: Path) -> None:
        cfg = _make_config(tmp_path, with_seed=True)
        exporter = OSAExporter()
        osa_path = exporter.pack_osa(cfg, tmp_path / "pack_out")
        with zipfile.ZipFile(osa_path) as zf:
            names = zf.namelist()
            assert "seed.osm" in names
            content = zf.read("seed.osm").decode("utf-8")
            assert "placeholder seed model" in content

    def test_seed_missing_note_when_no_osm(self, tmp_path: Path) -> None:
        cfg = _make_config(tmp_path, with_seed=False)
        exporter = OSAExporter()
        osa_path = exporter.pack_osa(cfg, tmp_path / "pack_out")
        with zipfile.ZipFile(osa_path) as zf:
            data = json.loads(zf.read("analysis.json"))
            assert "_seed_missing_note" in data
            assert "No seed model" in data["_seed_missing_note"]

    def test_no_seed_missing_note_when_osm_present(self, tmp_path: Path) -> None:
        cfg = _make_config(tmp_path, with_seed=True)
        exporter = OSAExporter()
        osa_path = exporter.pack_osa(cfg, tmp_path / "pack_out")
        with zipfile.ZipFile(osa_path) as zf:
            data = json.loads(zf.read("analysis.json"))
            assert "_seed_missing_note" not in data


class TestPackOsaTemplateFiles:
    """Template package file inclusion in .osa archives."""

    def test_measures_dir_included(self, tmp_path: Path) -> None:
        cfg = _make_config(
            tmp_path,
            extra_files={
                "measures/SetRValue/measure.rb": "# Ruby measure",
                "measures/SetRValue/measure.xml": "<measure/>",
            },
        )
        exporter = OSAExporter()
        osa_path = exporter.pack_osa(cfg, tmp_path / "pack_out")
        with zipfile.ZipFile(osa_path) as zf:
            names = zf.namelist()
            assert "measures/SetRValue/measure.rb" in names
            assert "measures/SetRValue/measure.xml" in names

    def test_weather_dir_included(self, tmp_path: Path) -> None:
        cfg = _make_config(
            tmp_path,
            extra_files={
                "weather/USA_CO_Denver.epw": "LOCATION,DENVER",
            },
        )
        exporter = OSAExporter()
        osa_path = exporter.pack_osa(cfg, tmp_path / "pack_out")
        with zipfile.ZipFile(osa_path) as zf:
            assert "weather/USA_CO_Denver.epw" in zf.namelist()

    def test_osm_not_duplicated(self, tmp_path: Path) -> None:
        """The seed .osm should appear only as 'seed.osm', not under its
        original relative path from template_sim_package."""
        cfg = _make_config(tmp_path, with_seed=True)
        exporter = OSAExporter()
        osa_path = exporter.pack_osa(cfg, tmp_path / "pack_out")
        with zipfile.ZipFile(osa_path) as zf:
            osm_names = [n for n in zf.namelist() if n.endswith(".osm")]
            assert osm_names == ["seed.osm"]


# ---------------------------------------------------------------------------
# Tests: round-trip fidelity (export → pack → unpack → import)
# ---------------------------------------------------------------------------


class TestOsaRoundTrip:
    """End-to-end round-trip: export → pack → parse → import must preserve
    algorithm type, variable names, distributions, and measure arguments."""

    def test_round_trip_preserves_algorithm(self, tmp_path: Path) -> None:
        cfg = _make_config(tmp_path, algorithm="lhs", n_samples=50)
        exporter = OSAExporter()
        osa_path = exporter.pack_osa(cfg, tmp_path / "pack_out")

        # Re-import from the .osa archive.
        osa_data = parse_osa(osa_path)
        reimport_path = tmp_path / "roundtrip_variables.yml"
        osa_to_variables_yml(osa_data, reimport_path)

        with reimport_path.open(encoding="utf-8") as f:
            reimported = yaml.safe_load(f)

        assert reimported["algorithm"] == "lhs"

    def test_round_trip_preserves_variable_count(self, tmp_path: Path) -> None:
        cfg = _make_config(tmp_path, variables_text=VARIABLES_YML)
        exporter = OSAExporter()
        osa_path = exporter.pack_osa(cfg, tmp_path / "pack_out")

        osa_data = parse_osa(osa_path)
        reimport_path = tmp_path / "roundtrip_variables.yml"
        osa_to_variables_yml(osa_data, reimport_path)

        with reimport_path.open(encoding="utf-8") as f:
            reimported = yaml.safe_load(f)

        assert len(reimported["variables"]) == 5

    def test_round_trip_preserves_variable_names(self, tmp_path: Path) -> None:
        cfg = _make_config(tmp_path, variables_text=VARIABLES_YML)
        exporter = OSAExporter()
        osa_path = exporter.pack_osa(cfg, tmp_path / "pack_out")

        osa_data = parse_osa(osa_path)
        reimport_path = tmp_path / "roundtrip_variables.yml"
        osa_to_variables_yml(osa_data, reimport_path)

        with reimport_path.open(encoding="utf-8") as f:
            reimported = yaml.safe_load(f)

        names = [v["name"] for v in reimported["variables"]]
        assert names == ["insulation_r", "wwr", "shgc", "roof_abs", "hvac_type"]

    def test_round_trip_preserves_distributions(self, tmp_path: Path) -> None:
        cfg = _make_config(tmp_path, variables_text=VARIABLES_YML)
        exporter = OSAExporter()
        osa_path = exporter.pack_osa(cfg, tmp_path / "pack_out")

        osa_data = parse_osa(osa_path)
        reimport_path = tmp_path / "roundtrip_variables.yml"
        osa_to_variables_yml(osa_data, reimport_path)

        with reimport_path.open(encoding="utf-8") as f:
            reimported = yaml.safe_load(f)

        expected_dists = ["uniform", "normal", "lognormal", "triangular", "discrete"]
        actual_dists = [v["distribution"] for v in reimported["variables"]]
        assert actual_dists == expected_dists

    def test_round_trip_preserves_distribution_params(self, tmp_path: Path) -> None:
        """Verify that specific distribution parameters survive the round trip."""
        cfg = _make_config(tmp_path, variables_text=VARIABLES_YML)
        exporter = OSAExporter()
        osa_path = exporter.pack_osa(cfg, tmp_path / "pack_out")

        osa_data = parse_osa(osa_path)
        reimport_path = tmp_path / "roundtrip_variables.yml"
        osa_to_variables_yml(osa_data, reimport_path)

        with reimport_path.open(encoding="utf-8") as f:
            reimported = yaml.safe_load(f)

        variables = {v["name"]: v for v in reimported["variables"]}

        # Uniform: min/max
        assert variables["insulation_r"]["min"] == 5.0
        assert variables["insulation_r"]["max"] == 30.0

        # Normal: mean/sigma
        assert variables["wwr"]["mean"] == 0.4
        assert variables["wwr"]["sigma"] == 0.05

        # Lognormal: mean/sigma
        assert variables["shgc"]["mean"] == 0.3
        assert variables["shgc"]["sigma"] == 0.1

        # Triangular: min/max/mode
        assert variables["roof_abs"]["min"] == 0.1
        assert variables["roof_abs"]["max"] == 0.9
        assert variables["roof_abs"]["mode"] == 0.5

        # Discrete: values
        assert variables["hvac_type"]["values"] == [
            "packaged_rooftop",
            "vav",
            "gshtp",
        ]

    def test_round_trip_preserves_measure_arguments(self, tmp_path: Path) -> None:
        cfg = _make_config(tmp_path, variables_text=VARIABLES_YML)
        exporter = OSAExporter()
        osa_path = exporter.pack_osa(cfg, tmp_path / "pack_out")

        osa_data = parse_osa(osa_path)
        reimport_path = tmp_path / "roundtrip_variables.yml"
        osa_to_variables_yml(osa_data, reimport_path)

        with reimport_path.open(encoding="utf-8") as f:
            reimported = yaml.safe_load(f)

        variables = {v["name"]: v for v in reimported["variables"]}
        assert variables["insulation_r"]["measure_argument"] == "SetInsulationRValue.r_value"
        assert variables["wwr"]["measure_argument"] == "SetWWR.wwr_value"
        # shgc has no measure_argument
        assert "measure_argument" not in variables["shgc"]

    def test_round_trip_sobol_algorithm(self, tmp_path: Path) -> None:
        variables_text = textwrap.dedent("""\
            variables:
              - name: x
                distribution: uniform
                min: 0.0
                max: 1.0
        """)
        cfg = _make_config(tmp_path, variables_text=variables_text, algorithm="sobol")
        exporter = OSAExporter()
        osa_path = exporter.pack_osa(cfg, tmp_path / "pack_out")

        osa_data = parse_osa(osa_path)
        reimport_path = tmp_path / "roundtrip_variables.yml"
        osa_to_variables_yml(osa_data, reimport_path)

        with reimport_path.open(encoding="utf-8") as f:
            reimported = yaml.safe_load(f)

        assert reimported["algorithm"] == "sobol"

    def test_round_trip_n_samples(self, tmp_path: Path) -> None:
        cfg = _make_config(tmp_path, n_samples=100)
        exporter = OSAExporter()
        osa_path = exporter.pack_osa(cfg, tmp_path / "pack_out")

        # Verify n_samples in the analysis.json inside the archive.
        with zipfile.ZipFile(osa_path) as zf:
            data = json.loads(zf.read("analysis.json"))
        assert data["analysis"]["problem"]["algorithm"]["number_of_samples"] == 100

    def test_full_round_trip_with_seed_and_files(self, tmp_path: Path) -> None:
        """Full round-trip with a seed model and template files in the archive."""
        cfg = _make_config(
            tmp_path,
            variables_text=VARIABLES_YML,
            with_seed=True,
            extra_files={
                "measures/SetRValue/measure.rb": "# measure",
                "weather/denver.epw": "LOCATION,DENVER",
                "workflow.osw": '{"steps": []}',
            },
        )
        exporter = OSAExporter()
        osa_path = exporter.pack_osa(cfg, tmp_path / "pack_out")

        # Verify archive contents.
        with zipfile.ZipFile(osa_path) as zf:
            names = zf.namelist()
            assert "analysis.json" in names
            assert "seed.osm" in names
            assert "measures/SetRValue/measure.rb" in names
            assert "weather/denver.epw" in names
            assert "workflow.osw" in names

        # Re-import and verify variable fidelity.
        osa_data = parse_osa(osa_path)
        reimport_path = tmp_path / "roundtrip_variables.yml"
        osa_to_variables_yml(osa_data, reimport_path)

        with reimport_path.open(encoding="utf-8") as f:
            reimported = yaml.safe_load(f)

        assert reimported["algorithm"] == "lhs"
        assert len(reimported["variables"]) == 5
        names = [v["name"] for v in reimported["variables"]]
        assert names == ["insulation_r", "wwr", "shgc", "roof_abs", "hvac_type"]
