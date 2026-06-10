"""Unit tests for osimflow.importers.osa — OSA import converter."""

import json
import zipfile
from pathlib import Path

import pytest
import yaml

from osimflow.importers.osa import (
    OSAImportError,
    _map_distribution,
    _resolve_measure_argument,
    osa_to_variables_yml,
    parse_analysis_json,
    parse_osa,
)

SAMPLE_ANALYSIS: dict = {
    "analysis": {
        "display_name": "Test Parametric Study",
        "problem": {
            "algorithm": {
                "type": "lhs",
                "number_of_samples": 50,
                "seed": 42,
            },
            "variables": [
                {
                    "name": "insul_r",
                    "display_name": "Insulation R-value",
                    "variable_type": "variable",
                    "distribution": {
                        "type": "uniform",
                        "minimum": 5.0,
                        "maximum": 30.0,
                    },
                    "measure": {
                        "display_name": "SetInsulationRValue",
                        "argument": "r_value",
                    },
                },
                {
                    "name": "wwr",
                    "display_name": "Window-to-Wall Ratio",
                    "variable_type": "variable",
                    "distribution": {
                        "type": "normal",
                        "mean": 0.4,
                        "stddev": 0.05,
                    },
                    "measure": {
                        "display_name": "SetWWR",
                        "argument": "wwr_value",
                    },
                },
                {
                    "name": "shgc",
                    "display_name": "Solar Heat Gain Coefficient",
                    "variable_type": "variable",
                    "distribution": {
                        "type": "lognormal",
                        "mean": 0.3,
                        "sigma": 0.1,
                    },
                },
                {
                    "name": "roof_abs",
                    "display_name": "Roof Absorptivity",
                    "variable_type": "variable",
                    "distribution": {
                        "type": "triangular",
                        "minimum": 0.1,
                        "maximum": 0.9,
                        "mode": 0.5,
                    },
                },
                {
                    "name": "hvac_type",
                    "display_name": "HVAC System Type",
                    "variable_type": "variable",
                    "distribution": {
                        "type": "discrete",
                        "values": ["packaged_rooftop", "vav", "gshtp"],
                    },
                },
            ],
        },
    }
}


class TestMapDistribution:
    def test_uniform(self) -> None:
        result = _map_distribution({"type": "uniform", "minimum": 1.0, "maximum": 10.0})
        assert result == {"distribution": "uniform", "min": 1.0, "max": 10.0}

    def test_normal_with_stddev(self) -> None:
        result = _map_distribution({"type": "normal", "mean": 5.0, "stddev": 1.0})
        assert result == {"distribution": "normal", "mean": 5.0, "sigma": 1.0}

    def test_normal_with_sigma(self) -> None:
        result = _map_distribution({"type": "normal", "mean": 5.0, "sigma": 2.0})
        assert result == {"distribution": "normal", "mean": 5.0, "sigma": 2.0}

    def test_lognormal(self) -> None:
        result = _map_distribution({"type": "lognormal", "mean": 1.0, "sigma": 0.5})
        assert result == {"distribution": "lognormal", "mean": 1.0, "sigma": 0.5}

    def test_lognormal_uncertain(self) -> None:
        result = _map_distribution({"type": "lognormal_uncertain", "mean": 1.0, "sigma": 0.5})
        assert result == {"distribution": "lognormal", "mean": 1.0, "sigma": 0.5}

    def test_triangular_with_mode(self) -> None:
        result = _map_distribution(
            {"type": "triangular", "minimum": 0.0, "maximum": 1.0, "mode": 0.7}
        )
        assert result == {"distribution": "triangular", "min": 0.0, "max": 1.0, "mode": 0.7}

    def test_triangular_without_mode(self) -> None:
        result = _map_distribution({"type": "triangular", "minimum": 0.0, "maximum": 1.0})
        assert result == {"distribution": "triangular", "min": 0.0, "max": 1.0}

    def test_discrete(self) -> None:
        result = _map_distribution({"type": "discrete", "values": [1, 2, 3]})
        assert result == {"distribution": "discrete", "values": [1, 2, 3]}

    def test_categorical(self) -> None:
        result = _map_distribution({"type": "categorical", "values": ["a", "b"]})
        assert result == {"distribution": "categorical", "values": ["a", "b"]}

    def test_enum_maps_to_categorical(self) -> None:
        result = _map_distribution({"type": "enum", "values": ["x", "y"]})
        assert result["distribution"] == "categorical"

    def test_unknown_type_raises(self) -> None:
        with pytest.raises(OSAImportError, match="Unsupported OSA distribution"):
            _map_distribution({"type": "weibull", "shape": 2.0})

    def test_uniform_missing_max(self) -> None:
        with pytest.raises(OSAImportError, match="Uniform.*'maximum'"):
            _map_distribution({"type": "uniform", "minimum": 1.0})

    def test_normal_missing_stddev(self) -> None:
        with pytest.raises(OSAImportError, match="stddev"):
            _map_distribution({"type": "normal", "mean": 0.0})

    def test_discrete_empty_values(self) -> None:
        with pytest.raises(OSAImportError, match="values"):
            _map_distribution({"type": "discrete", "values": []})

    def test_discrete_missing_values(self) -> None:
        with pytest.raises(OSAImportError, match="values"):
            _map_distribution({"type": "discrete"})


class TestResolveMeasureArgument:
    def test_normal_measure(self) -> None:
        result = _resolve_measure_argument(
            {
                "measure": {"display_name": "SetRValue", "argument": "r_val"},
            }
        )
        assert result == "SetRValue.r_val"

    def test_with_name_instead_of_display_name(self) -> None:
        result = _resolve_measure_argument(
            {
                "measure": {"name": "SetRValue", "argument_name": "r_val"},
            }
        )
        assert result == "SetRValue.r_val"

    def test_no_measure(self) -> None:
        assert _resolve_measure_argument({}) is None

    def test_measure_missing_argument(self) -> None:
        assert _resolve_measure_argument({"measure": {"display_name": "Foo"}}) is None


class TestParseOsa:
    def test_parse_json_file(self, tmp_path: Path) -> None:
        p = tmp_path / "analysis.json"
        p.write_text(json.dumps(SAMPLE_ANALYSIS))
        result = parse_osa(p)
        assert "problem" in result
        assert len(result["problem"]["variables"]) == 5

    def test_parse_osa_zip(self, tmp_path: Path) -> None:
        osa = tmp_path / "study.osa"
        with zipfile.ZipFile(osa, "w") as zf:
            zf.writestr("analysis.json", json.dumps(SAMPLE_ANALYSIS))
        result = parse_osa(osa)
        assert "problem" in result
        assert len(result["problem"]["variables"]) == 5

    def test_parse_osa_zip_nested_path(self, tmp_path: Path) -> None:
        osa = tmp_path / "study.osa"
        with zipfile.ZipFile(osa, "w") as zf:
            zf.writestr("analysis/analysis.json", json.dumps(SAMPLE_ANALYSIS))
        result = parse_osa(osa)
        assert "problem" in result

    def test_file_not_found(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            parse_osa(tmp_path / "nonexistent.json")

    def test_invalid_json(self, tmp_path: Path) -> None:
        p = tmp_path / "bad.json"
        p.write_text("not json at all{{{")
        with pytest.raises(OSAImportError, match="Invalid JSON"):
            parse_osa(p)

    def test_unrecognised_structure(self, tmp_path: Path) -> None:
        p = tmp_path / "empty.json"
        p.write_text(json.dumps({"foo": "bar"}))
        with pytest.raises(OSAImportError, match="Unrecognised OSA structure"):
            parse_osa(p)

    def test_top_level_problem(self, tmp_path: Path) -> None:
        top_level = {
            "problem": SAMPLE_ANALYSIS["analysis"]["problem"],
        }
        p = tmp_path / "analysis.json"
        p.write_text(json.dumps(top_level))
        result = parse_osa(p)
        assert "problem" in result

    def test_zip_no_analysis_json(self, tmp_path: Path) -> None:
        osa = tmp_path / "empty.osa"
        with zipfile.ZipFile(osa, "w") as zf:
            zf.writestr("readme.txt", "nothing useful")
        with pytest.raises(OSAImportError, match="No analysis.json found"):
            parse_osa(osa)


class TestParseAnalysisJson:
    def test_delegates_to_parse_osa(self, tmp_path: Path) -> None:
        p = tmp_path / "analysis.json"
        p.write_text(json.dumps(SAMPLE_ANALYSIS))
        result = parse_analysis_json(p)
        assert "problem" in result


class TestOsaToVariablesYml:
    def test_full_conversion(self, tmp_path: Path) -> None:
        osa_data = parse_osa(_write_json(tmp_path / "input.json", SAMPLE_ANALYSIS))
        out = tmp_path / "variables.yml"
        osa_to_variables_yml(osa_data, out)

        with out.open() as f:
            yml = yaml.safe_load(f)

        variables = yml["variables"]
        assert len(variables) == 5

        v0 = variables[0]
        assert v0["name"] == "insul_r"
        assert v0["distribution"] == "uniform"
        assert v0["min"] == 5.0
        assert v0["max"] == 30.0
        assert v0["measure_argument"] == "SetInsulationRValue.r_value"
        assert v0["display_name"] == "Insulation R-value"

        v1 = variables[1]
        assert v1["name"] == "wwr"
        assert v1["distribution"] == "normal"
        assert v1["mean"] == 0.4
        assert v1["sigma"] == 0.05

        v2 = variables[2]
        assert v2["distribution"] == "lognormal"
        assert "measure_argument" not in v2

        v3 = variables[3]
        assert v3["distribution"] == "triangular"
        assert v3["mode"] == 0.5

        v4 = variables[4]
        assert v4["distribution"] == "discrete"
        assert v4["values"] == ["packaged_rooftop", "vav", "gshtp"]

    def test_no_variables_raises(self, tmp_path: Path) -> None:
        osa_data = {"problem": {"variables": []}}
        out = tmp_path / "variables.yml"
        with pytest.raises(OSAImportError, match="No variables"):
            osa_to_variables_yml(osa_data, out)

    def test_no_problem_raises(self, tmp_path: Path) -> None:
        out = tmp_path / "variables.yml"
        with pytest.raises(OSAImportError, match="No variables"):
            osa_to_variables_yml({}, out)

    def test_malformed_variable_skipped(self, tmp_path: Path) -> None:
        data = {
            "problem": {
                "variables": [
                    "not a dict",
                    {
                        "name": "good_var",
                        "distribution": {"type": "uniform", "minimum": 0, "maximum": 1},
                    },
                    {"uuid": "bad_no_dist"},
                ],
            },
        }
        out = tmp_path / "variables.yml"
        osa_to_variables_yml(data, out)
        with out.open() as f:
            yml = yaml.safe_load(f)
        assert len(yml["variables"]) == 1
        assert yml["variables"][0]["name"] == "good_var"

    def test_bad_distribution_skipped_with_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        data = {
            "problem": {
                "variables": [
                    {
                        "name": "bad_dist",
                        "distribution": {"type": "unsupported_dist"},
                    },
                ],
            },
        }
        out = tmp_path / "variables.yml"
        with pytest.raises(OSAImportError, match="No variables could be converted"):
            osa_to_variables_yml(data, out)

    def test_creates_parent_dirs(self, tmp_path: Path) -> None:
        osa_data = {
            "problem": {
                "variables": [
                    {"name": "x", "distribution": {"type": "uniform", "minimum": 0, "maximum": 1}},
                ],
            },
        }
        out = tmp_path / "deep" / "nested" / "variables.yml"
        osa_to_variables_yml(osa_data, out)
        assert out.exists()

    def test_problem_not_dict_raises(self, tmp_path: Path) -> None:
        out = tmp_path / "variables.yml"
        with pytest.raises(OSAImportError, match="not a dict"):
            osa_to_variables_yml({"problem": "garbage"}, out)

    def test_roundtrip_with_lhs_sampler(self, tmp_path: Path) -> None:
        osa_data = parse_osa(_write_json(tmp_path / "input.json", SAMPLE_ANALYSIS))
        out = tmp_path / "variables.yml"
        osa_to_variables_yml(osa_data, out)

        with out.open() as f:
            config = yaml.safe_load(f)

        variables = config.get("variables", [])
        assert len(variables) > 0
        for v in variables:
            assert "name" in v
            assert "distribution" in v
            dist = v["distribution"]
            if dist == "uniform":
                assert "min" in v and "max" in v
            elif dist == "normal":
                assert "mean" in v and "sigma" in v
            elif dist == "lognormal":
                assert "mean" in v and "sigma" in v
            elif dist == "triangular":
                assert "min" in v and "max" in v
            elif dist in ("discrete", "categorical"):
                assert "values" in v and isinstance(v["values"], list)


def _write_json(path: Path, data: dict) -> Path:
    path.write_text(json.dumps(data))
    return path
