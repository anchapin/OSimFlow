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

    def test_malformed_variable_uuid_as_static(self, tmp_path: Path) -> None:
        """A string entry is skipped, but a uuid-only dict is imported as
        static (issue #196)."""
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
        # "not a dict" is skipped; uuid-only entry is imported as static.
        assert len(yml["variables"]) == 2
        assert yml["variables"][0]["name"] == "good_var"
        assert yml["variables"][1]["name"] == "bad_no_dist"
        assert yml["variables"][1]["distribution"] == "static"

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


# ---------------------------------------------------------------------------
# Test: _extract_analysis_data edge cases
# ---------------------------------------------------------------------------


class TestExtractAnalysisData:
    def test_analysis_key_present(self) -> None:
        from osimflow.importers.osa import _extract_analysis_data

        raw = {"analysis": {"problem": {"variables": []}}}
        result = _extract_analysis_data(raw)
        assert "problem" in result

    def test_top_level_problem(self) -> None:
        from osimflow.importers.osa import _extract_analysis_data

        raw = {"problem": {"variables": []}}
        result = _extract_analysis_data(raw)
        assert "problem" in result

    def test_unrecognised_raises(self) -> None:
        from osimflow.importers.osa import _extract_analysis_data

        with pytest.raises(OSAImportError, match="Unrecognised OSA structure"):
            _extract_analysis_data({"random_key": "value"})


# ---------------------------------------------------------------------------
# Test: _resolve_algorithm
# ---------------------------------------------------------------------------


class TestResolveAlgorithm:
    def test_lhs_algorithm(self) -> None:
        from osimflow.importers.osa import _resolve_algorithm

        algo = _resolve_algorithm({"type": "lhs"})
        assert algo.name() == "lhs"

    def test_latin_hypercube_maps_to_lhs(self) -> None:
        from osimflow.importers.osa import _resolve_algorithm

        algo = _resolve_algorithm({"type": "latin_hypercube"})
        assert algo.name() == "lhs"

    def test_sobol_algorithm(self) -> None:
        from osimflow.importers.osa import _resolve_algorithm

        algo = _resolve_algorithm({"type": "sobol"})
        assert algo.name() == "sobol"

    def test_doe_maps_to_lhs(self) -> None:
        from osimflow.importers.osa import _resolve_algorithm

        algo = _resolve_algorithm({"type": "doe"})
        assert algo.name() == "lhs"

    def test_unknown_algorithm_raises(self) -> None:
        from osimflow.importers.osa import _resolve_algorithm

        with pytest.raises(OSAImportError, match="Unknown algorithm"):
            _resolve_algorithm({"type": "nonexistent"})

    def test_empty_type_raises(self) -> None:
        from osimflow.importers.osa import _resolve_algorithm

        with pytest.raises(OSAImportError, match="Unknown algorithm"):
            _resolve_algorithm({"type": ""})

    def test_unavailable_algorithm_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from osimflow.importers.osa import _resolve_algorithm

        def _raise(name: str) -> None:
            raise ValueError(f"unknown algorithm '{name}'")

        monkeypatch.setattr(
            "osimflow.algorithms.AlgorithmRegistry.get",
            lambda name: (_ for _ in ()).throw(ValueError(f"unknown algorithm '{name}'")),
        )
        with pytest.raises(OSAImportError, match="maps to OSimFlow.*not available"):
            _resolve_algorithm({"type": "lhs"})


# ---------------------------------------------------------------------------
# Test: _map_distribution additional edge cases
# ---------------------------------------------------------------------------


class TestMapDistributionAdditional:
    def test_lognormal_missing_stddev(self) -> None:
        with pytest.raises(OSAImportError, match="Lognormal.*stddev"):
            _map_distribution({"type": "lognormal", "mean": 1.0})

    def test_lognormal_with_stddev_key(self) -> None:
        result = _map_distribution({"type": "lognormal", "mean": 2.0, "stddev": 0.3})
        assert result == {"distribution": "lognormal", "mean": 2.0, "sigma": 0.3}

    def test_triangular_with_peak_alias(self) -> None:
        result = _map_distribution(
            {"type": "triangular", "minimum": 0.0, "maximum": 1.0, "peak": 0.3}
        )
        assert result["mode"] == 0.3

    def test_categorical_with_mapping(self) -> None:
        result = _map_distribution(
            {"type": "categorical", "values": ["a", "b"], "mapping": {"a": 1, "b": 2}}
        )
        assert result["mapping"] == {"a": 1, "b": 2}

    def test_discrete_with_discrete_values_alias(self) -> None:
        result = _map_distribution({"type": "discrete", "discrete_values": [10, 20, 30]})
        assert result["values"] == [10, 20, 30]

    def test_triangular_missing_minimum(self) -> None:
        with pytest.raises(OSAImportError, match="Triangular.*'minimum'"):
            _map_distribution({"type": "triangular", "maximum": 1.0})


# ---------------------------------------------------------------------------
# Test: _convert_variable edge cases
# ---------------------------------------------------------------------------


class TestConvertVariable:
    def test_non_dict_entry(self) -> None:
        from osimflow.importers.osa import _convert_variable

        entry, warnings = _convert_variable("not a dict", 0)
        assert entry == {}
        assert len(warnings) == 1

    def test_no_name_uses_uuid(self) -> None:
        from osimflow.importers.osa import _convert_variable

        osa_var = {
            "uuid": "var-uuid-123",
            "distribution": {"type": "uniform", "minimum": 0, "maximum": 1},
        }
        entry, _ = _convert_variable(osa_var, 0)
        assert entry["name"] == "var-uuid-123"

    def test_no_name_no_uuid_skipped(self) -> None:
        from osimflow.importers.osa import _convert_variable

        entry, warnings = _convert_variable(
            {"distribution": {"type": "uniform", "minimum": 0, "maximum": 1}}, 0
        )
        assert entry == {}
        assert "no name" in warnings[0]

    def test_no_distribution_returns_static(self) -> None:
        """Variables without a distribution are now imported as static (issue #196)."""
        from osimflow.importers.osa import _convert_variable

        entry, warnings = _convert_variable({"name": "x"}, 0)
        assert entry["name"] == "x"
        assert entry["distribution"] == "static"

    def test_bad_distribution_skipped(self) -> None:
        from osimflow.importers.osa import _convert_variable

        entry, warnings = _convert_variable({"name": "x", "distribution": {"type": "weibull"}}, 0)
        assert entry == {}

    def test_display_name_same_as_name_not_carried(self) -> None:
        from osimflow.importers.osa import _convert_variable

        entry, _ = _convert_variable(
            {
                "name": "x",
                "display_name": "x",
                "distribution": {"type": "uniform", "minimum": 0, "maximum": 1},
            },
            0,
        )
        assert "display_name" not in entry

    def test_display_name_different_carried(self) -> None:
        from osimflow.importers.osa import _convert_variable

        entry, _ = _convert_variable(
            {
                "name": "x",
                "display_name": "X Variable",
                "distribution": {"type": "uniform", "minimum": 0, "maximum": 1},
            },
            0,
        )
        assert entry["display_name"] == "X Variable"

    def test_uses_display_name_as_fallback_name(self) -> None:
        from osimflow.importers.osa import _convert_variable

        entry, _ = _convert_variable(
            {
                "display_name": "MyVar",
                "distribution": {"type": "uniform", "minimum": 0, "maximum": 1},
            },
            0,
        )
        assert entry["name"] == "MyVar"


# ---------------------------------------------------------------------------
# Test: parse_osa additional edge cases
# ---------------------------------------------------------------------------


class TestParseOsaAdditional:
    def test_zip_with_deep_analysis_json(self, tmp_path: Path) -> None:
        osa = tmp_path / "study.osa"
        with zipfile.ZipFile(osa, "w") as zf:
            zf.writestr("subdir/analysis.json", json.dumps(SAMPLE_ANALYSIS))
        result = parse_osa(osa)
        assert "problem" in result

    def test_bad_zip_file(self, tmp_path: Path) -> None:
        bad_osa = tmp_path / "bad.osa"
        bad_osa.write_bytes(b"not a zip file content")
        with pytest.raises(OSAImportError, match="Not a valid ZIP|Invalid JSON"):
            parse_osa(bad_osa)

    def test_zip_with_invalid_json(self, tmp_path: Path) -> None:
        osa = tmp_path / "bad_content.osa"
        with zipfile.ZipFile(osa, "w") as zf:
            zf.writestr("analysis.json", "not valid json {{{")
        with pytest.raises(OSAImportError, match="Invalid JSON"):
            parse_osa(osa)


# ---------------------------------------------------------------------------
# Test: osa_to_variables_yml additional edge cases
# ---------------------------------------------------------------------------


class TestOsaToVariablesYmlAdditional:
    def test_algorithm_resolution_sobol(self, tmp_path: Path) -> None:
        data = {
            "problem": {
                "algorithm": {"type": "sobol", "number_of_samples": 50},
                "variables": [
                    {"name": "x", "distribution": {"type": "uniform", "minimum": 0, "maximum": 1}},
                ],
            },
        }
        out = tmp_path / "variables.yml"
        osa_to_variables_yml(data, out)
        with out.open() as f:
            yml = yaml.safe_load(f)
        assert yml["algorithm"] == "sobol"

    def test_no_algorithm_defaults_to_lhs(self, tmp_path: Path) -> None:
        data = {
            "problem": {
                "variables": [
                    {"name": "x", "distribution": {"type": "uniform", "minimum": 0, "maximum": 1}},
                ],
            },
        }
        out = tmp_path / "variables.yml"
        osa_to_variables_yml(data, out)
        with out.open() as f:
            yml = yaml.safe_load(f)
        assert yml["algorithm"] == "lhs"

    def test_all_valid_but_no_convertible_variables(self, tmp_path: Path) -> None:
        data = {
            "problem": {
                "variables": [
                    {"name": "bad_var", "distribution": {"type": "weibull"}},
                ],
            },
        }
        out = tmp_path / "variables.yml"
        with pytest.raises(OSAImportError, match="No variables could be converted"):
            osa_to_variables_yml(data, out)

    def test_mixed_good_and_bad_variables(self, tmp_path: Path) -> None:
        data = {
            "problem": {
                "algorithm": {"type": "lhs"},
                "variables": [
                    {"name": "bad", "distribution": {"type": "weibull"}},
                    {
                        "name": "good",
                        "distribution": {"type": "uniform", "minimum": 0, "maximum": 1},
                    },
                ],
            },
        }
        out = tmp_path / "variables.yml"
        osa_to_variables_yml(data, out)
        with out.open() as f:
            yml = yaml.safe_load(f)
        assert len(yml["variables"]) == 1
        assert yml["variables"][0]["name"] == "good"

    def test_workflow_nested_variables_and_uncertainty_description(self, tmp_path: Path) -> None:
        data = {
            "problem": {
                "analysis_type": "lhs",
                "algorithm": {
                    "seed": 123,
                },
                "workflow": [
                    {
                        "measure_definition_class_name": "SetRValue",
                        "variables": [
                            {
                                "display_name": "insul_r",
                                "variable_type": "variable",
                                "argument": {
                                    "name": "r_val",
                                },
                                "uncertainty_description": {
                                    "type": "uniform",
                                    "attributes": [
                                        {"name": "lower_bounds", "value": 5.0},
                                        {"name": "upper_bounds", "value": 30.0},
                                    ]
                                }
                            }
                        ]
                    }
                ]
            }
        }
        out = tmp_path / "variables.yml"
        osa_to_variables_yml(data, out)
        with out.open() as f:
            yml = yaml.safe_load(f)
        assert yml["algorithm"] == "lhs"
        assert len(yml["variables"]) == 1
        v = yml["variables"][0]
        assert v["name"] == "insul_r"
        assert v["distribution"] == "uniform"
        assert v["min"] == 5.0
        assert v["max"] == 30.0
        assert v["measure_argument"] == "SetRValue.r_val"



# ---------------------------------------------------------------------------
# Test: importers __init__ re-exports
# ---------------------------------------------------------------------------


class TestImportersPublicAPI:
    def test_re_exports(self) -> None:
        from osimflow.importers import osa_to_variables_yml, parse_analysis_json, parse_osa

        assert callable(parse_osa)
        assert callable(parse_analysis_json)
        assert callable(osa_to_variables_yml)

    def test_parse_analysis_json_delegates(self, tmp_path: Path) -> None:
        from osimflow.importers import parse_analysis_json as paj

        p = tmp_path / "analysis.json"
        p.write_text(json.dumps(SAMPLE_ANALYSIS))
        result = paj(p)
        assert "problem" in result


def _write_json(path: Path, data: dict) -> Path:
    path.write_text(json.dumps(data))
    return path
