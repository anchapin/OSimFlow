"""Unit tests for osimflow.importers.osa — OSA import converter."""

import json
import shutil
import zipfile
from pathlib import Path
from typing import Any

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

    def test_doe_maps_to_full_factorial(self) -> None:
        from osimflow.importers.osa import _resolve_algorithm

        assert _resolve_algorithm({"type": "doe"}).name() == "full_factorial"

    def test_full_factorial_maps_to_full_factorial(self) -> None:
        from osimflow.importers.osa import _resolve_algorithm

        assert _resolve_algorithm({"type": "full_factorial"}).name() == "full_factorial"

    def test_grid_stays_grid(self) -> None:
        from osimflow.importers.osa import _resolve_algorithm

        assert _resolve_algorithm({"type": "grid"}).name() == "grid"

    def test_single_run_maps_to_lhs(self) -> None:
        from osimflow.importers.osa import _resolve_algorithm

        algo = _resolve_algorithm({"type": "single_run"})
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
                                    ],
                                },
                            }
                        ],
                    }
                ],
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
        assert yml["seed"] == 123
        assert yml["_metadata"]["seed"] == 123
        assert yml["_metadata"]["algorithm"] == "lhs"

    def test_metadata_preserves_seed_n_samples_and_doe_mapping(self, tmp_path: Path) -> None:
        data = {
            "problem": {
                "algorithm": {
                    "type": "doe",
                    "number_of_samples": 16,
                    "seed": 1973,
                },
                "variables": [
                    {
                        "name": "SetRValue.r_val",
                        "distribution": {"type": "discrete", "values": [5.0, 10.0]},
                    }
                ],
            }
        }
        out = tmp_path / "variables.yml"
        osa_to_variables_yml(data, out)
        yml = yaml.safe_load(out.read_text(encoding="utf-8"))
        assert yml["algorithm"] == "full_factorial"
        assert yml["seed"] == 1973
        assert yml["_metadata"] == {
            "algorithm": "full_factorial",
            "n_samples": 16,
            "seed": 1973,
        }

    def test_invalid_seed_raises(self, tmp_path: Path) -> None:
        data = {
            "problem": {
                "algorithm": {"type": "lhs", "seed": True},
                "variables": [
                    {"name": "x", "distribution": {"type": "uniform", "minimum": 0, "maximum": 1}},
                ],
            }
        }
        with pytest.raises(OSAImportError, match="seed"):
            osa_to_variables_yml(data, tmp_path / "variables.yml")

    def test_invalid_n_samples_raises(self, tmp_path: Path) -> None:
        data = {
            "problem": {
                "algorithm": {"type": "lhs", "number_of_samples": 0},
                "variables": [
                    {"name": "x", "distribution": {"type": "uniform", "minimum": 0, "maximum": 1}},
                ],
            }
        }
        with pytest.raises(OSAImportError, match="number_of_samples"):
            osa_to_variables_yml(data, tmp_path / "variables.yml")

    def test_zero_variable_workflow_emits_prebuilt_workflow(self, tmp_path: Path) -> None:
        data = {
            "problem": {
                "algorithm": {"type": "single_run", "seed": 1973},
                "workflow": [
                    {
                        "name": "set_thermostat",
                        "measure_definition_directory": "./measures/SetThermostat",
                        "arguments": [{"name": "heating_setpoint", "value": 21.0}],
                        "variables": [
                            {
                                "variable": False,
                                "argument": {"name": "cooling_setpoint"},
                                "static_value": 24.0,
                            }
                        ],
                    }
                ],
            }
        }
        out = tmp_path / "variables.yml"
        osa_to_variables_yml(data, out)
        yml = yaml.safe_load(out.read_text(encoding="utf-8"))
        assert yml["prebuilt_workflow"] is True
        assert yml["variables"] == []
        assert yml["seed"] == 1973
        assert yml["_metadata"]["n_samples"] == 1
        assert yml["_metadata"]["seed"] == 1973


# ---------------------------------------------------------------------------
# Test: stage_osa_template_package & CLI import-osa staging
# ---------------------------------------------------------------------------


class TestStageOsaTemplatePackage:
    def _make_seed_package(self, pkg_dir: Path) -> Path:
        pkg_dir.mkdir(parents=True, exist_ok=True)
        (pkg_dir / "seeds").mkdir()
        (pkg_dir / "seeds" / "baseline.osm").write_text("OS:Version,3.10.0;\n", encoding="utf-8")
        (pkg_dir / "weather").mkdir()
        (pkg_dir / "weather" / "USA_CO_Golden.epw").write_text("LOCATION,...\n", encoding="utf-8")
        (pkg_dir / "measures" / "SetThermostat").mkdir(parents=True)
        (pkg_dir / "measures" / "ServerDirectoryCleanup").mkdir(parents=True)
        osw = {
            "seed_file": "../../seeds/baseline.osm",
            "weather_file": "../../weather/USA_CO_Golden.epw",
            "measure_paths": ["../../measures"],
            "file_paths": ["../../weather", "../../seeds"],
            "steps": [
                {
                    "measure_dir_name": "SetThermostat",
                    "arguments": {
                        "__SKIP__": False,
                        "heating_setpoint": 20.0,
                        "cooling_setpoint": "25.0",
                    },
                },
                {
                    "measure_dir_name": "ServerDirectoryCleanup",
                    "arguments": {
                        "sql": True,
                    },
                },
            ],
        }
        (pkg_dir / "workflow.osw").write_text(json.dumps(osw, indent=2), encoding="utf-8")
        return pkg_dir

    def test_stage_from_template_package_overlays_static_args_and_normalizes_paths(
        self, tmp_path: Path
    ) -> None:
        from osimflow.importers.osa import stage_osa_template_package

        seed_pkg = self._make_seed_package(tmp_path / "seed_pkg")
        analysis_data = {
            "analysis": {
                "problem": {
                    "algorithm": {"type": "lhs", "number_of_samples": 10, "seed": 42},
                    "workflow": [
                        {
                            "measure_definition_directory": "./measures/SetThermostat",
                            "arguments": {
                                "__SKIP__": "true",
                                "heating_setpoint": 22.5,
                                "cooling_setpoint": 24.0,
                            },
                            "variables": [
                                {
                                    "name": "heating_setpoint",
                                    "variable": True,
                                    "argument": {"name": "heating_setpoint"},
                                    "uncertainty_description": {
                                        "type": "uniform",
                                        "attributes": [
                                            {"name": "lower_bounds", "value": 18.0},
                                            {"name": "upper_bounds", "value": 23.0},
                                        ],
                                    },
                                }
                            ],
                        }
                    ],
                }
            }
        }
        analysis_json = _write_json(tmp_path / "analysis.json", analysis_data)
        staged_pkg = tmp_path / "staged_pkg"

        stage_osa_template_package(
            analysis_json,
            staged_pkg,
            template_package=seed_pkg,
        )

        osw = json.loads((staged_pkg / "workflow.osw").read_text(encoding="utf-8"))
        assert osw["seed_file"] == "seeds/baseline.osm"
        assert osw["weather_file"] == "weather/USA_CO_Golden.epw"
        assert osw["measure_paths"] == ["measures"]
        assert "weather" in osw["file_paths"]
        assert "seeds" in osw["file_paths"]

        thermostat_step = osw["steps"][0]
        # __SKIP__ coerced to bool True; cooling_setpoint coerced to str "24.0"
        assert thermostat_step["arguments"]["__SKIP__"] is True
        assert thermostat_step["arguments"]["cooling_setpoint"] == "24.0"
        # heating_setpoint is varied in problem.workflow, so static value 22.5 is NOT overlaid
        assert thermostat_step["arguments"]["heating_setpoint"] == 20.0

        cleanup_step = osw["steps"][1]
        # Measure-specific arguments are left untouched by import-osa.
        assert cleanup_step["arguments"]["sql"] is True

    def test_stage_directly_from_osa_zip(self, tmp_path: Path) -> None:
        from osimflow.importers.osa import stage_osa_template_package

        osa_zip = tmp_path / "study.osa"
        analysis_data = {
            "analysis": {
                "seed": {"file_name": "baseline.osm"},
                "weather_file": {"file_name": "USA_CO_Golden.epw"},
                "problem": {
                    "algorithm": {"type": "single_run"},
                    "workflow": [
                        {
                            "name": "set_thermostat",
                            "measure_definition_directory": "./measures/SetThermostat",
                            "arguments": [{"name": "heating_setpoint", "value": 21.0}],
                        }
                    ],
                },
            }
        }
        with zipfile.ZipFile(osa_zip, "w") as zf:
            zf.writestr("analysis.json", json.dumps(analysis_data))
            zf.writestr("seeds/baseline.osm", "OS:Version,3.10.0;\n")
            zf.writestr("weather/USA_CO_Golden.epw", "LOCATION,...\n")
            zf.writestr("measures/SetThermostat/measure.rb", "# measure\n")

        staged_pkg = tmp_path / "staged_from_zip"
        stage_osa_template_package(osa_zip, staged_pkg)

        osw = json.loads((staged_pkg / "workflow.osw").read_text(encoding="utf-8"))
        assert osw["seed_file"] == "seeds/baseline.osm"
        assert osw["weather_file"] == "weather/USA_CO_Golden.epw"
        assert osw["measure_paths"] == ["measures"]
        assert osw["steps"] == [
            {
                "measure_dir_name": "SetThermostat",
                "arguments": {"heating_setpoint": 21.0},
                "name": "set_thermostat",
            }
        ]

    def test_stage_alt_osw_writes_canonical_workflow_osw(self, tmp_path: Path) -> None:
        from osimflow.importers.osa import stage_osa_template_package

        osa_zip = tmp_path / "study.osa"
        analysis_data = {
            "analysis": {
                "problem": {
                    "algorithm": {"type": "single_run"},
                    "workflow": [
                        {
                            "name": "set_thermostat",
                            "measure_definition_directory": "./measures/SetThermostat",
                            "arguments": [{"name": "heating_setpoint", "value": 21.0}],
                        }
                    ],
                },
            }
        }
        seed_osw = {
            "seed_file": "seeds/baseline.osm",
            "steps": [{"measure_dir_name": "SetThermostat", "arguments": {}}],
        }
        with zipfile.ZipFile(osa_zip, "w") as zf:
            zf.writestr("analysis.json", json.dumps(analysis_data))
            zf.writestr("seed_workflow.osw", json.dumps(seed_osw))
            zf.writestr("seeds/baseline.osm", "OS:Version,3.10.0;\n")
            zf.writestr("measures/SetThermostat/measure.rb", "# measure\n")

        staged = tmp_path / "staged"
        stage_osa_template_package(osa_zip, staged)

        canonical = staged / "workflow.osw"
        assert canonical.is_file()
        osw = json.loads(canonical.read_text(encoding="utf-8"))
        assert osw["seed_file"] == "seeds/baseline.osm"
        assert osw["steps"][0]["arguments"]["heating_setpoint"] == 21.0

    def test_stage_rejects_zip_slip(self, tmp_path: Path) -> None:
        from osimflow.importers.osa import stage_osa_template_package

        osa_zip = tmp_path / "malicious.osa"
        with zipfile.ZipFile(osa_zip, "w") as zf:
            zf.writestr("analysis.json", json.dumps(SAMPLE_ANALYSIS))
            zf.writestr("../escape.txt", "pwned")

        with pytest.raises(OSAImportError, match="outside destination"):
            stage_osa_template_package(osa_zip, tmp_path / "staged")

    def test_cli_import_osa_stages_package(self, tmp_path: Path) -> None:
        from osimflow.__main__ import main

        seed_pkg = self._make_seed_package(tmp_path / "seed_pkg")
        analysis_json = _write_json(tmp_path / "analysis.json", SAMPLE_ANALYSIS)
        out_yml = tmp_path / "out" / "variables.yml"
        pkg_out = tmp_path / "out" / "staged_package"

        rc = main(
            [
                "import-osa",
                str(analysis_json),
                "--output",
                str(out_yml),
                "--template-package",
                str(seed_pkg),
                "--package-output",
                str(pkg_out),
            ]
        )
        assert rc == 0
        assert out_yml.is_file()
        assert (pkg_out / "workflow.osw").is_file()

    def test_normalize_osw_paths_missing_or_ambiguous_raises_osa_import_error(
        self, tmp_path: Path
    ) -> None:
        from osimflow.importers.osa import stage_osa_template_package

        analysis_json = _write_json(tmp_path / "analysis.json", SAMPLE_ANALYSIS)

        # Missing seed_file
        pkg_missing = tmp_path / "pkg_missing"
        pkg_missing.mkdir()
        (pkg_missing / "workflow.osw").write_text(
            json.dumps(
                {
                    "seed_file": "../outside/missing.osm",
                    "steps": [{"measure_dir_name": "bar", "arguments": {"x": 0}}],
                }
            ),
            encoding="utf-8",
        )
        with pytest.raises(OSAImportError, match="does not exist in package"):
            stage_osa_template_package(
                analysis_json, tmp_path / "out_missing", template_package=pkg_missing
            )

        # Ambiguous weather_file
        pkg_ambig = tmp_path / "pkg_ambig"
        (pkg_ambig / "weather_a").mkdir(parents=True)
        (pkg_ambig / "weather_b").mkdir(parents=True)
        (pkg_ambig / "weather_a" / "dup.epw").write_text("A\n", encoding="utf-8")
        (pkg_ambig / "weather_b" / "dup.epw").write_text("B\n", encoding="utf-8")
        (pkg_ambig / "workflow.osw").write_text(
            json.dumps(
                {
                    "weather_file": "../outside/dup.epw",
                    "steps": [{"measure_dir_name": "bar", "arguments": {"x": 0}}],
                }
            ),
            encoding="utf-8",
        )
        with pytest.raises(OSAImportError, match="multiple files named 'dup.epw'"):
            stage_osa_template_package(
                analysis_json, tmp_path / "out_ambig", template_package=pkg_ambig
            )

        # Ambiguous measure_dir_name when measure_paths escaped package
        pkg_ambig_m = tmp_path / "pkg_ambig_m"
        (pkg_ambig_m / "custom_a" / "bar").mkdir(parents=True)
        (pkg_ambig_m / "custom_b" / "bar").mkdir(parents=True)
        (pkg_ambig_m / "workflow.osw").write_text(
            json.dumps(
                {
                    "measure_paths": ["../../measures"],
                    "steps": [{"measure_dir_name": "bar", "arguments": {"x": 0}}],
                }
            ),
            encoding="utf-8",
        )
        with pytest.raises(OSAImportError, match="measure 'bar' is ambiguous"):
            stage_osa_template_package(
                analysis_json, tmp_path / "out_ambig_m", template_package=pkg_ambig_m
            )

        # Missing measure_dir_name when measure_paths escaped package
        pkg_missing_m = tmp_path / "pkg_missing_m"
        pkg_missing_m.mkdir()
        (pkg_missing_m / "workflow.osw").write_text(
            json.dumps(
                {
                    "measure_paths": ["../../measures"],
                    "steps": [{"measure_dir_name": "bar", "arguments": {"x": 0}}],
                }
            ),
            encoding="utf-8",
        )
        with pytest.raises(OSAImportError, match="could not resolve step measure 'bar'"):
            stage_osa_template_package(
                analysis_json, tmp_path / "out_missing_m", template_package=pkg_missing_m
            )

    def test_normalize_osw_paths_contained_absolute_path_with_duplicate_basename(
        self, tmp_path: Path
    ) -> None:
        from osimflow.importers.osa import stage_osa_template_package

        analysis_json = _write_json(tmp_path / "analysis.json", SAMPLE_ANALYSIS)
        pkg = tmp_path / "pkg_abs"
        (pkg / "measures" / "bar").mkdir(parents=True)
        (pkg / "seed_a").mkdir(parents=True)
        (pkg / "seed_b").mkdir(parents=True)
        (pkg / "seed_a" / "model.osm").write_text("A\n", encoding="utf-8")
        (pkg / "seed_b" / "model.osm").write_text("B\n", encoding="utf-8")
        (pkg / "weather_a").mkdir(parents=True)
        (pkg / "weather_b").mkdir(parents=True)
        (pkg / "weather_a" / "site.epw").write_text("WA\n", encoding="utf-8")
        (pkg / "weather_b" / "site.epw").write_text("WB\n", encoding="utf-8")
        (pkg / "workflow.osw").write_text(
            json.dumps(
                {
                    "seed_file": str((pkg / "seed_b" / "model.osm").resolve()),
                    "weather_file": str((pkg / "weather_a" / "site.epw").resolve()),
                    "measure_paths": [str((pkg / "measures").resolve())],
                    "file_paths": [str((pkg / "seed_b").resolve())],
                    "steps": [{"measure_dir_name": "bar", "arguments": {"x": 0}}],
                }
            ),
            encoding="utf-8",
        )

        out = tmp_path / "out_abs"
        stage_osa_template_package(analysis_json, out, template_package=pkg)
        osw = json.loads((out / "workflow.osw").read_text(encoding="utf-8"))
        assert osw["seed_file"] == "seed_b/model.osm"
        assert osw["weather_file"] == "weather_a/site.epw"
        assert osw["measure_paths"] == ["measures"]
        assert "seed_b" in osw["file_paths"]
        assert "weather_a" in osw["file_paths"]

    def test_normalize_osw_paths_rewrites_out_of_tree_and_works_in_isolated_dir(
        self, tmp_path: Path
    ) -> None:
        from osimflow.importers.osa import stage_osa_template_package

        analysis_json = _write_json(tmp_path / "analysis.json", SAMPLE_ANALYSIS)
        pkg = tmp_path / "staged_pkg"
        (pkg / "measures" / "bar").mkdir(parents=True)
        (pkg / "weather").mkdir(parents=True)
        (pkg / "weather" / "Chicago.epw").write_text("LOCATION,Chicago\n", encoding="utf-8")
        (pkg / "seed_models").mkdir(parents=True)
        (pkg / "seed_models" / "model.osm").write_text("OS:Version,{1};\n", encoding="utf-8")
        (pkg / "workflow.osw").write_text(
            json.dumps(
                {
                    "seed_file": "../seed_models/model.osm",
                    "weather_file": "../../weather/Chicago.epw",
                    "measure_paths": ["../../measures"],
                    "file_paths": ["../../weather", "../../seed_models"],
                    "steps": [
                        {"measure_dir_name": "bar", "arguments": {"floor_area": 100}},
                    ],
                }
            ),
            encoding="utf-8",
        )

        out = tmp_path / "out_isolated"
        stage_osa_template_package(analysis_json, out, template_package=pkg)

        isolated = tmp_path / "isolated_worker" / "0000"
        isolated.parent.mkdir(parents=True)
        shutil.copytree(out, isolated)
        isolated_root = isolated.resolve()
        osw = json.loads((isolated / "workflow.osw").read_text(encoding="utf-8"))

        assert osw["measure_paths"] == ["measures"]
        assert "seed_models" in osw["file_paths"]
        assert "weather" in osw["file_paths"]
        assert osw["seed_file"] == "seed_models/model.osm"
        assert osw["weather_file"] == "weather/Chicago.epw"

        for mp in osw["measure_paths"]:
            p = (isolated / mp).resolve()
            p.relative_to(isolated_root)
            assert p.is_dir()
        for fp in osw["file_paths"]:
            p = (isolated / fp).resolve()
            p.relative_to(isolated_root)
            assert p.is_dir()
        for field in ("seed_file", "weather_file"):
            p = (isolated / osw[field]).resolve()
            p.relative_to(isolated_root)
            assert p.is_file()

    def test_normalize_osw_paths_preserves_valid_in_package_paths(self, tmp_path: Path) -> None:
        from osimflow.importers.osa import stage_osa_template_package

        analysis_json = _write_json(tmp_path / "analysis.json", SAMPLE_ANALYSIS)
        pkg = tmp_path / "pkg_valid"
        (pkg / "custom_measures" / "bar").mkdir(parents=True)
        (pkg / "files" / "seeds").mkdir(parents=True)
        (pkg / "files" / "seeds" / "seed.osm").write_text("OS:Version,{1};\n", encoding="utf-8")
        (pkg / "files" / "epw").mkdir(parents=True)
        (pkg / "files" / "epw" / "site.epw").write_text("LOCATION,Site\n", encoding="utf-8")
        (pkg / "workflow.osw").write_text(
            json.dumps(
                {
                    "seed_file": "seed.osm",
                    "weather_file": "files/epw/site.epw",
                    "measure_paths": ["custom_measures"],
                    "file_paths": ["files/seeds"],
                    "steps": [{"measure_dir_name": "bar", "arguments": {"x": 0}}],
                }
            ),
            encoding="utf-8",
        )

        out = tmp_path / "out_valid"
        stage_osa_template_package(analysis_json, out, template_package=pkg)
        osw = json.loads((out / "workflow.osw").read_text(encoding="utf-8"))
        assert osw["seed_file"] == "files/seeds/seed.osm"
        assert osw["weather_file"] == "files/epw/site.epw"
        assert osw["measure_paths"] == ["custom_measures"]
        assert "files/seeds" in osw["file_paths"]
        assert "files/epw" in osw["file_paths"]


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


class TestDoeFullFactorialImport:
    @staticmethod
    def _problem(algo: dict[str, Any]) -> dict[str, Any]:
        return {
            "problem": {
                "algorithm": algo,
                "variables": [
                    {
                        "name": "A.x",
                        "uncertainty_description": {
                            "type": "discrete",
                            "attributes": [
                                {
                                    "name": "discrete",
                                    "values_and_weights": [
                                        {"value": 1.0, "weight": 0.7},
                                        {"value": 2.0, "weight": 0.2},
                                        {"value": 3.0, "weight": 0.1},
                                    ],
                                }
                            ],
                        },
                    },
                    {
                        "name": "B.y",
                        "uncertainty_description": {
                            "type": "discrete",
                            "attributes": [
                                {
                                    "name": "discrete",
                                    "values_and_weights": [
                                        {"value": "a", "weight": 0.5},
                                        {"value": "b", "weight": 0.5},
                                    ],
                                }
                            ],
                        },
                    },
                ],
            }
        }

    @pytest.mark.parametrize("algo_type", ["doe", "full_factorial"])
    def test_cartesian_n_samples_and_pmf(self, tmp_path: Path, algo_type: str) -> None:
        out = tmp_path / "variables.yml"
        osa_to_variables_yml(self._problem({"type": algo_type}), out)
        yml = yaml.safe_load(out.read_text(encoding="utf-8"))
        assert yml["algorithm"] == "full_factorial"
        assert yml["_metadata"]["n_samples"] == 6
        x, y = yml["variables"]
        assert x["pmf"] == {1.0: 0.7, 2.0: 0.2, 3.0: 0.1}
        assert x["values"] == [1.0, 2.0, 3.0]
        assert "pmf" not in y  # uniform weights are dropped

    def test_explicit_number_of_samples_wins(self, tmp_path: Path) -> None:
        out = tmp_path / "variables.yml"
        osa_to_variables_yml(self._problem({"type": "doe", "number_of_samples": 12}), out)
        assert yaml.safe_load(out.read_text())["_metadata"]["n_samples"] == 12

    def test_weights_attribute_and_validation(self) -> None:
        dist = {"type": "discrete", "values": [1, 2], "weights": [3, 1]}
        assert _map_distribution(dist)["pmf"] == {1: 3.0, 2: 1.0}
        with pytest.raises(OSAImportError, match="weights"):
            _map_distribution({"type": "discrete", "values": [1, 2], "weights": [1]})
        with pytest.raises(OSAImportError, match="non-negative"):
            _map_distribution({"type": "discrete", "values": [1, 2], "weights": [-1, 2]})

    def test_pmf_accepted_by_variables_validation(self) -> None:
        from osimflow.validation import ValidationError, _validate_discrete_categorical

        _validate_discrete_categorical({"values": [1, 2], "pmf": {1: 0.3, 2: 0.7}}, "v")
        with pytest.raises(ValidationError):
            _validate_discrete_categorical({"values": [1, 2], "pmf": {1: 0.3, 3: 0.7}}, "v")

    def test_campaign_runs_full_factorial_from_import(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from osimflow import Campaign
        from osimflow.config import load_config
        from osimflow.executors import LocalExecutor

        monkeypatch.setenv("OSIMFLOW_STUB_SIM", "1")
        data = self._problem({"type": "full_factorial", "seed": 7})
        for var in data["problem"]["variables"]:
            for attr in var["uncertainty_description"]["attributes"]:
                for item in attr["values_and_weights"]:
                    item["weight"] = 1.0
                    if isinstance(item["value"], str):
                        item["value"] = {"a": 1.0, "b": 2.0}[item["value"]]
        out = tmp_path / "variables.yml"
        osa_to_variables_yml(data, out)
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        (pkg / "workflow.osw").write_text(json.dumps({"steps": []}))
        (pkg / "model.osm").write_text(json.dumps({"attributes": {"A.x": 1.0, "B.y": 1.0}}))
        cfg = load_config(
            {
                "template_sim_package": str(pkg),
                "input_variables": str(out),
                "n_samples": 6,
                "outdir": str(tmp_path / "out"),
                "openstudio_version": "3.11.0",
                "executor": "local",
            }
        )
        Campaign(cfg=cfg, executor=LocalExecutor(max_workers=1)).run()
        samples = json.loads((cfg.work_dir / "samples.json").read_text())["samples"]
        assert len(samples) == 6


def test_cartesian_count_with_static_and_nonfinite_weights() -> None:
    from osimflow.importers.osa import _cartesian_sample_count

    assert _cartesian_sample_count([{"distribution": "static", "value": 1}]) == 1
    assert (
        _cartesian_sample_count(
            [{"distribution": "static"}, {"distribution": "discrete", "values": [1, 2, 3]}]
        )
        == 3
    )
    with pytest.raises(OSAImportError, match="non-negative"):
        _map_distribution({"type": "discrete", "values": [1, 2], "weights": [float("inf"), 1]})
