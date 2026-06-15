"""Unit tests for type coercion in variable loading (issue #409).

Covers:
- ``coerce_variable_type()`` — all supported coercions and error paths
- ``_coerce_variables_yml_file()`` — file-level normalisation
- ``load_config()`` integration — string-typed YAML params are fixed
  before validation sees them
"""

import logging
from pathlib import Path

import pytest
import yaml

from osimflow.config import (
    _coerce_variables_yml_file,
    coerce_variable_type,
    load_config,
)
from osimflow.validation import ValidationError

# ======================================================================
# coerce_variable_type — direct unit tests
# ======================================================================


class TestCoerceToFloat:
    """str → float, int → float, identity."""

    def test_str_to_float(self) -> None:
        assert coerce_variable_type("1.5", float) == 1.5
        assert coerce_variable_type("42", float) == 42.0
        assert coerce_variable_type("-3.14", float) == pytest.approx(-3.14)

    def test_int_to_float(self) -> None:
        result = coerce_variable_type(5, float)
        assert result == 5.0
        assert isinstance(result, float)

    def test_float_identity(self) -> None:
        result = coerce_variable_type(3.14, float)
        assert result == pytest.approx(3.14)
        assert isinstance(result, float)

    def test_str_invalid_float(self) -> None:
        with pytest.raises(ValueError, match="could not convert"):
            coerce_variable_type("not_a_number", float)


class TestCoerceToInt:
    """str → int, float → int (exact only)."""

    def test_str_to_int(self) -> None:
        assert coerce_variable_type("42", int) == 42
        assert coerce_variable_type("-7", int) == -7

    def test_str_float_to_int(self) -> None:
        """'3.0' should coerce to 3."""
        assert coerce_variable_type("3.0", int) == 3

    def test_str_float_to_int_loss(self) -> None:
        """'3.5' should fail."""
        with pytest.raises(ValueError, match="loss of precision"):
            coerce_variable_type("3.5", int)

    def test_float_to_int_exact(self) -> None:
        assert coerce_variable_type(4.0, int) == 4

    def test_float_to_int_loss(self) -> None:
        with pytest.raises(ValueError, match="loss of precision"):
            coerce_variable_type(4.5, int)

    def test_int_identity(self) -> None:
        result = coerce_variable_type(10, int)
        assert result == 10
        assert isinstance(result, int)

    def test_str_invalid_int(self) -> None:
        with pytest.raises(ValueError):
            coerce_variable_type("hello", int)


class TestCoerceToBool:
    """str → bool, int/float → bool, identity."""

    @pytest.mark.parametrize("truthy", ["true", "True", "TRUE", "1", "yes", "on"])
    def test_str_truthy(self, truthy: str) -> None:
        result = coerce_variable_type(truthy, bool)
        assert result is True

    @pytest.mark.parametrize("falsy", ["false", "False", "FALSE", "0", "no", "off", ""])
    def test_str_falsy(self, falsy: str) -> None:
        result = coerce_variable_type(falsy, bool)
        assert result is False

    def test_bool_identity(self) -> None:
        assert coerce_variable_type(True, bool) is True
        assert coerce_variable_type(False, bool) is False

    def test_int_to_bool(self) -> None:
        assert coerce_variable_type(1, bool) is True
        assert coerce_variable_type(0, bool) is False

    def test_float_to_bool(self) -> None:
        assert coerce_variable_type(1.0, bool) is True
        assert coerce_variable_type(0.0, bool) is False

    def test_str_invalid_bool(self) -> None:
        with pytest.raises(ValueError, match="cannot coerce"):
            coerce_variable_type("maybe", bool)


class TestCoerceToList:
    """str → list (comma-separated), identity."""

    def test_str_to_list(self) -> None:
        result = coerce_variable_type("a, b, c", list)
        assert result == ["a", "b", "c"]

    def test_str_to_list_single(self) -> None:
        assert coerce_variable_type("solo", list) == ["solo"]

    def test_str_to_list_with_spaces(self) -> None:
        assert coerce_variable_type("  x  ,  y  ", list) == ["x", "y"]

    def test_str_to_list_empty_entries_skipped(self) -> None:
        """Empty entries from trailing commas are dropped."""
        assert coerce_variable_type("a, , b,", list) == ["a", "b"]

    def test_list_identity(self) -> None:
        assert coerce_variable_type([1, 2, 3], list) == [1, 2, 3]

    def test_int_to_list_fails(self) -> None:
        with pytest.raises(ValueError, match="cannot coerce"):
            coerce_variable_type(42, list)


class TestCoerceToStr:
    """Anything → str."""

    def test_int_to_str(self) -> None:
        assert coerce_variable_type(42, str) == "42"

    def test_float_to_str(self) -> None:
        assert coerce_variable_type(3.14, str) == "3.14"

    def test_str_identity(self) -> None:
        assert coerce_variable_type("hello", str) == "hello"


class TestCoerceStringTypeNames:
    """expected_type can be a string name."""

    def test_float_string(self) -> None:
        assert coerce_variable_type("1.0", "float") == 1.0

    def test_int_string(self) -> None:
        assert coerce_variable_type("5", "int") == 5

    def test_bool_string(self) -> None:
        assert coerce_variable_type("true", "bool") is True

    def test_list_string(self) -> None:
        assert coerce_variable_type("a,b", "list") == ["a", "b"]

    def test_aliases(self) -> None:
        assert coerce_variable_type("1.0", "double") == 1.0
        assert coerce_variable_type("5", "integer") == 5
        assert coerce_variable_type("true", "boolean") is True
        assert coerce_variable_type(42, "string") == "42"

    def test_unknown_type_name(self) -> None:
        with pytest.raises(ValueError, match="unknown type"):
            coerce_variable_type("x", "nonexistent")


class TestCoerceBoolNotInt:
    """bool must not be silently treated as int."""

    def test_bool_to_int_fails(self) -> None:
        """A bool value with expected_type=int is an error, not 0/1."""
        with pytest.raises(ValueError):
            coerce_variable_type(True, int)

    def test_bool_not_treated_as_float(self) -> None:
        with pytest.raises(ValueError):
            coerce_variable_type(False, float)


class TestCoerceDebugLog:
    """Coercion events are logged at DEBUG."""

    def test_debug_log_on_coercion(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.DEBUG, logger="osimflow.config"):
            coerce_variable_type("3.14", float)
        assert any("coerced" in r.message for r in caplog.records)

    def test_no_log_on_identity(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.DEBUG, logger="osimflow.config"):
            coerce_variable_type(3.14, float)
        assert not any("coerced" in r.message for r in caplog.records)


# ======================================================================
# _coerce_variables_yml_file — file-level tests
# ======================================================================


class TestCoerceVariablesYmlFile:
    def test_numeric_params_coerced(self, tmp_path: Path) -> None:
        """String-typed min/max are coerced to float."""
        vyml = tmp_path / "variables.yml"
        vyml.write_text(
            yaml.dump(
                {
                    "variables": [
                        {
                            "name": "x",
                            "distribution": "uniform",
                            "min": "1.0",
                            "max": "10.0",
                        }
                    ]
                }
            )
        )
        changed = _coerce_variables_yml_file(vyml)
        assert changed is True

        data = yaml.safe_load(vyml.read_text())
        assert data["variables"][0]["min"] == 1.0
        assert isinstance(data["variables"][0]["min"], float)
        assert data["variables"][0]["max"] == 10.0

    def test_no_change_when_already_typed(self, tmp_path: Path) -> None:
        vyml = tmp_path / "variables.yml"
        vyml.write_text(
            yaml.dump(
                {
                    "variables": [
                        {
                            "name": "x",
                            "distribution": "uniform",
                            "min": 1.0,
                            "max": 10.0,
                        }
                    ]
                }
            )
        )
        original = vyml.read_text()
        changed = _coerce_variables_yml_file(vyml)
        assert changed is False
        assert vyml.read_text() == original

    def test_all_numeric_keys_coerced(self, tmp_path: Path) -> None:
        vyml = tmp_path / "variables.yml"
        vyml.write_text(
            yaml.dump(
                {
                    "variables": [
                        {
                            "name": "x",
                            "distribution": "normal",
                            "mean": "22.0",
                            "sigma": "1.5",
                        },
                        {
                            "name": "y",
                            "distribution": "beta",
                            "alpha": "2.0",
                            "beta": "5.0",
                            "loc": "0.1",
                            "scale": "2.0",
                        },
                        {
                            "name": "z",
                            "distribution": "exponential",
                            "rate": "10.0",
                        },
                    ]
                }
            )
        )
        changed = _coerce_variables_yml_file(vyml)
        assert changed is True

        data = yaml.safe_load(vyml.read_text())
        for var in data["variables"]:
            for key in ("mean", "sigma", "alpha", "beta", "loc", "scale", "rate"):
                if key in var:
                    assert isinstance(var[key], float), f"{key} should be float"

    def test_triangular_mode_coerced(self, tmp_path: Path) -> None:
        vyml = tmp_path / "variables.yml"
        vyml.write_text(
            yaml.dump(
                {
                    "variables": [
                        {
                            "name": "x",
                            "distribution": "triangular",
                            "min": "1.0",
                            "max": "10.0",
                            "mode": "5.0",
                        }
                    ]
                }
            )
        )
        _coerce_variables_yml_file(vyml)
        data = yaml.safe_load(vyml.read_text())
        assert data["variables"][0]["mode"] == 5.0

    def test_discrete_values_str_to_list(self, tmp_path: Path) -> None:
        vyml = tmp_path / "variables.yml"
        vyml.write_text(
            yaml.dump(
                {
                    "variables": [
                        {
                            "name": "x",
                            "distribution": "discrete",
                            "values": "a, b, c",
                        }
                    ]
                }
            )
        )
        changed = _coerce_variables_yml_file(vyml)
        assert changed is True
        data = yaml.safe_load(vyml.read_text())
        assert data["variables"][0]["values"] == ["a", "b", "c"]

    def test_categorical_values_str_to_list(self, tmp_path: Path) -> None:
        vyml = tmp_path / "variables.yml"
        vyml.write_text(
            yaml.dump(
                {
                    "variables": [
                        {
                            "name": "x",
                            "distribution": "categorical",
                            "values": "low, medium, high",
                        }
                    ]
                }
            )
        )
        _coerce_variables_yml_file(vyml)
        data = yaml.safe_load(vyml.read_text())
        assert data["variables"][0]["values"] == ["low", "medium", "high"]

    def test_empty_file_returns_false(self, tmp_path: Path) -> None:
        vyml = tmp_path / "variables.yml"
        vyml.write_text("")
        assert _coerce_variables_yml_file(vyml) is False

    def test_non_dict_yaml_returns_false(self, tmp_path: Path) -> None:
        vyml = tmp_path / "variables.yml"
        vyml.write_text("just a string")
        assert _coerce_variables_yml_file(vyml) is False

    def test_malformed_yaml_returns_false(self, tmp_path: Path) -> None:
        vyml = tmp_path / "variables.yml"
        vyml.write_text("{invalid: yaml: [")
        assert _coerce_variables_yml_file(vyml) is False

    def test_no_variables_key_returns_false(self, tmp_path: Path) -> None:
        vyml = tmp_path / "variables.yml"
        vyml.write_text(yaml.dump({"objective": {"name": "eui"}}))
        assert _coerce_variables_yml_file(vyml) is False


# ======================================================================
# load_config integration — end-to-end coercion
# ======================================================================


@pytest.fixture
def template_pkg(tmp_path: Path) -> Path:
    pkg = tmp_path / "template"
    pkg.mkdir()
    (pkg / "workflow.osw").write_text("{}")
    return pkg


@pytest.fixture
def outdir(tmp_path: Path) -> Path:
    od = tmp_path / "out"
    od.mkdir()
    return od


def _base_args(variables_yml: Path, template_pkg: Path, outdir: Path) -> dict[str, object]:
    return {
        "input_variables": str(variables_yml),
        "template_sim_package": str(template_pkg),
        "n_samples": "5",
        "outdir": str(outdir),
        "openstudio_version": "3.11.0",
    }


class TestLoadConfigTypeCoercion:
    def test_string_min_max_passes_validation(
        self, tmp_path: Path, template_pkg: Path, outdir: Path
    ) -> None:
        """variables.yml with quoted min/max should load without error."""
        vyml = tmp_path / "variables.yml"
        vyml.write_text(
            yaml.dump(
                {
                    "variables": [
                        {
                            "name": "window_u",
                            "distribution": "uniform",
                            "min": "1.0",
                            "max": "5.0",
                        }
                    ]
                }
            )
        )
        args = _base_args(vyml, template_pkg, outdir)
        cfg = load_config(args)
        assert cfg is not None

    def test_string_params_normalized_in_file(
        self, tmp_path: Path, template_pkg: Path, outdir: Path
    ) -> None:
        """The file should be rewritten with float types after load."""
        vyml = tmp_path / "variables.yml"
        vyml.write_text(
            yaml.dump(
                {
                    "variables": [
                        {
                            "name": "x",
                            "distribution": "uniform",
                            "min": "1.0",
                            "max": "10.0",
                        }
                    ]
                }
            )
        )
        args = _base_args(vyml, template_pkg, outdir)
        load_config(args)

        data = yaml.safe_load(vyml.read_text())
        assert isinstance(data["variables"][0]["min"], float)
        assert isinstance(data["variables"][0]["max"], float)

    def test_string_mean_sigma_normalized(
        self, tmp_path: Path, template_pkg: Path, outdir: Path
    ) -> None:
        vyml = tmp_path / "variables.yml"
        vyml.write_text(
            yaml.dump(
                {
                    "variables": [
                        {
                            "name": "sp",
                            "distribution": "normal",
                            "mean": "22.0",
                            "sigma": "1.0",
                        }
                    ]
                }
            )
        )
        args = _base_args(vyml, template_pkg, outdir)
        load_config(args)

        data = yaml.safe_load(vyml.read_text())
        assert isinstance(data["variables"][0]["mean"], float)
        assert isinstance(data["variables"][0]["sigma"], float)

    def test_discrete_string_values_normalized(
        self, tmp_path: Path, template_pkg: Path, outdir: Path
    ) -> None:
        vyml = tmp_path / "variables.yml"
        vyml.write_text(
            yaml.dump(
                {
                    "variables": [
                        {
                            "name": "cat",
                            "distribution": "categorical",
                            "values": "type_a, type_b, type_c",
                        }
                    ]
                }
            )
        )
        args = _base_args(vyml, template_pkg, outdir)
        load_config(args)

        data = yaml.safe_load(vyml.read_text())
        assert data["variables"][0]["values"] == ["type_a", "type_b", "type_c"]

    def test_already_correct_types_unchanged(
        self, tmp_path: Path, template_pkg: Path, outdir: Path
    ) -> None:
        vyml = tmp_path / "variables.yml"
        original_content = yaml.dump(
            {
                "variables": [
                    {
                        "name": "x",
                        "distribution": "uniform",
                        "min": 1.0,
                        "max": 10.0,
                    }
                ]
            }
        )
        vyml.write_text(original_content)
        args = _base_args(vyml, template_pkg, outdir)
        load_config(args)
        # File should be byte-identical when no coercion was needed.
        assert vyml.read_text() == original_content

    def test_invalid_numeric_still_raises_validation_error(
        self, tmp_path: Path, template_pkg: Path, outdir: Path
    ) -> None:
        """A genuinely non-numeric value must still fail validation."""
        vyml = tmp_path / "variables.yml"
        vyml.write_text(
            yaml.dump(
                {
                    "variables": [
                        {
                            "name": "x",
                            "distribution": "uniform",
                            "min": "not_a_number",
                            "max": 10.0,
                        }
                    ]
                }
            )
        )
        args = _base_args(vyml, template_pkg, outdir)
        with pytest.raises(ValidationError):
            load_config(args)
