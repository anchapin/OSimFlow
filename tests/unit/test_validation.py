"""Tests for osimflow/validation.py (issue #278).

Covers:
- ValidationError exception
- Path traversal protection (validate_path_within)
- variables.yml schema validation (validate_variables_yml)
- Template package validation (validate_template_package)
- API input sanitization (sanitize_sample_id, sanitize_filename)
"""

from pathlib import Path

import pytest
import yaml

from osimflow.validation import (
    DISTRIBUTION_PARAMS,
    VALID_DISTRIBUTIONS,
    ValidationError,
    sanitize_filename,
    sanitize_sample_id,
    validate_path_within,
    validate_path_within_base,
    validate_template_package,
    validate_variables_yml,
)


# ======================================================================
# ValidationError
# ======================================================================


class TestValidationError:
    def test_message(self) -> None:
        exc = ValidationError("something broke")
        assert str(exc) == "something broke"

    def test_field(self) -> None:
        exc = ValidationError("bad input", field="n_samples")
        assert exc.field == "n_samples"

    def test_field_default_none(self) -> None:
        exc = ValidationError("no field")
        assert exc.field is None

    def test_is_exception(self) -> None:
        assert issubclass(ValidationError, Exception)

    def test_can_be_caught_as_exception(self) -> None:
        with pytest.raises(ValidationError, match="boom"):
            raise ValidationError("boom")


# ======================================================================
# Path traversal protection
# ======================================================================


class TestValidatePathWithin:
    def test_valid_path_within_base(self, tmp_path: Path) -> None:
        base = tmp_path / "base"
        base.mkdir()
        target = base / "file.txt"
        target.write_text("ok")
        result = validate_path_within(target, base, must_exist=True)
        assert result == target.resolve()

    def test_traversal_with_dotdot(self, tmp_path: Path) -> None:
        base = tmp_path / "base"
        base.mkdir()
        target = base / ".." / "etc" / "passwd"
        with pytest.raises(ValidationError, match="escapes allowed directory"):
            validate_path_within(target, base)

    def test_absolute_path_outside_base(self, tmp_path: Path) -> None:
        base = tmp_path / "base"
        base.mkdir()
        with pytest.raises(ValidationError, match="escapes allowed directory"):
            validate_path_within("/etc/passwd", base)

    def test_must_exist_fails(self, tmp_path: Path) -> None:
        base = tmp_path / "base"
        base.mkdir()
        with pytest.raises(ValidationError, match="does not exist"):
            validate_path_within(base / "nonexistent.txt", base, must_exist=True)

    def test_must_be_file_fails_for_dir(self, tmp_path: Path) -> None:
        base = tmp_path / "base"
        base.mkdir()
        subdir = base / "subdir"
        subdir.mkdir()
        with pytest.raises(ValidationError, match="not a regular file"):
            validate_path_within(subdir, base, must_be_file=True)

    def test_must_be_dir_fails_for_file(self, tmp_path: Path) -> None:
        base = tmp_path / "base"
        base.mkdir()
        f = base / "file.txt"
        f.write_text("data")
        with pytest.raises(ValidationError, match="not a directory"):
            validate_path_within(f, base, must_be_dir=True)

    def test_readable_check(self, tmp_path: Path) -> None:
        base = tmp_path / "base"
        base.mkdir()
        f = base / "secret.txt"
        f.write_text("data")
        f.chmod(0o000)
        try:
            with pytest.raises(ValidationError, match="not readable"):
                validate_path_within(f, base, must_exist=True, readable=True)
        finally:
            f.chmod(0o644)

    def test_null_byte_in_path(self) -> None:
        with pytest.raises(ValidationError, match="Null byte"):
            validate_path_within("/tmp/evil\0.txt", "/tmp")

    def test_path_too_long(self) -> None:
        long_path = "/" + "a" * 5000
        with pytest.raises(ValidationError, match="Path too long"):
            validate_path_within(long_path, "/")

    def test_symlink_inside_base_ok(self, tmp_path: Path) -> None:
        base = tmp_path / "base"
        base.mkdir()
        target = base / "real.txt"
        target.write_text("data")
        link = base / "link.txt"
        link.symlink_to(target)
        result = validate_path_within(link, base, must_exist=True)
        assert result == target.resolve()

    def test_symlink_outside_base_blocked(self, tmp_path: Path) -> None:
        base = tmp_path / "base"
        base.mkdir()
        outside = tmp_path / "outside.txt"
        outside.write_text("secret")
        link = base / "evil.txt"
        link.symlink_to(outside)
        with pytest.raises(ValidationError, match="escapes allowed directory"):
            validate_path_within(link, base, must_exist=True)

    def test_string_input(self, tmp_path: Path) -> None:
        base = tmp_path / "base"
        base.mkdir()
        target = base / "file.txt"
        target.write_text("ok")
        result = validate_path_within(str(target), str(base), must_exist=True)
        assert result == target.resolve()


class TestValidatePathWithinBase:
    def test_valid(self, tmp_path: Path) -> None:
        base = tmp_path.resolve()
        child = (tmp_path / "child").resolve()
        result = validate_path_within_base(child, base)
        assert result == child

    def test_invalid(self, tmp_path: Path) -> None:
        base = tmp_path.resolve()
        outside = Path("/etc/passwd").resolve()
        with pytest.raises(ValidationError, match="escapes allowed directory"):
            validate_path_within_base(outside, base)


# ======================================================================
# variables.yml schema validation
# ======================================================================


def _write_yml(tmp_path: Path, data: object, name: str = "variables.yml") -> Path:
    p = tmp_path / name
    p.write_text(yaml.dump(data) if isinstance(data, (dict, list)) else str(data))
    return p


class TestValidateVariablesYml:
    def test_valid_single_variable(self, tmp_path: Path) -> None:
        path = _write_yml(tmp_path, {
            "variables": [
                {"name": "wall_r", "distribution": "uniform", "min": 1.0, "max": 10.0}
            ]
        })
        result = validate_variables_yml(path)
        assert len(result) == 1
        assert result[0]["name"] == "wall_r"

    def test_valid_multiple_variables(self, tmp_path: Path) -> None:
        path = _write_yml(tmp_path, {
            "variables": [
                {"name": "a", "distribution": "uniform", "min": 0, "max": 1},
                {"name": "b", "distribution": "normal", "mean": 0, "sigma": 1},
            ]
        })
        result = validate_variables_yml(path)
        assert len(result) == 2

    def test_empty_file(self, tmp_path: Path) -> None:
        p = tmp_path / "variables.yml"
        p.write_text("")
        with pytest.raises(ValidationError, match="empty"):
            validate_variables_yml(p)

    def test_whitespace_only_file(self, tmp_path: Path) -> None:
        p = tmp_path / "variables.yml"
        p.write_text("   \n  \n")
        with pytest.raises(ValidationError, match="empty"):
            validate_variables_yml(p)

    def test_invalid_yaml(self, tmp_path: Path) -> None:
        p = tmp_path / "variables.yml"
        p.write_text("{{{{invalid yaml")
        with pytest.raises(ValidationError, match="Invalid YAML"):
            validate_variables_yml(p)

    def test_non_dict_top_level(self, tmp_path: Path) -> None:
        path = _write_yml(tmp_path, ["not", "a", "dict"])
        with pytest.raises(ValidationError, match="must be a mapping"):
            validate_variables_yml(path)

    def test_missing_variables_key(self, tmp_path: Path) -> None:
        path = _write_yml(tmp_path, {"other_key": []})
        with pytest.raises(ValidationError, match="missing required 'variables'"):
            validate_variables_yml(path)

    def test_variables_not_list(self, tmp_path: Path) -> None:
        path = _write_yml(tmp_path, {"variables": "not a list"})
        with pytest.raises(ValidationError, match="must be a list"):
            validate_variables_yml(path)

    def test_variables_empty_list(self, tmp_path: Path) -> None:
        path = _write_yml(tmp_path, {"variables": []})
        with pytest.raises(ValidationError, match="empty"):
            validate_variables_yml(path)

    def test_variable_not_dict(self, tmp_path: Path) -> None:
        path = _write_yml(tmp_path, {"variables": ["not a dict"]})
        with pytest.raises(ValidationError, match="must be a mapping"):
            validate_variables_yml(path)

    def test_missing_name(self, tmp_path: Path) -> None:
        path = _write_yml(tmp_path, {
            "variables": [{"distribution": "uniform", "min": 0, "max": 1}]
        })
        with pytest.raises(ValidationError, match="missing required field 'name'"):
            validate_variables_yml(path)

    def test_empty_name(self, tmp_path: Path) -> None:
        path = _write_yml(tmp_path, {
            "variables": [{"name": "", "distribution": "uniform", "min": 0, "max": 1}]
        })
        with pytest.raises(ValidationError, match="non-empty string"):
            validate_variables_yml(path)

    def test_missing_distribution(self, tmp_path: Path) -> None:
        path = _write_yml(tmp_path, {
            "variables": [{"name": "x", "min": 0, "max": 1}]
        })
        with pytest.raises(ValidationError, match="missing required field 'distribution'"):
            validate_variables_yml(path)

    def test_unknown_distribution(self, tmp_path: Path) -> None:
        path = _write_yml(tmp_path, {
            "variables": [{"name": "x", "distribution": "gamma_ray", "min": 0, "max": 1}]
        })
        with pytest.raises(ValidationError, match="unknown distribution 'gamma_ray'"):
            validate_variables_yml(path)

    def test_uniform_missing_max(self, tmp_path: Path) -> None:
        path = _write_yml(tmp_path, {
            "variables": [{"name": "x", "distribution": "uniform", "min": 0}]
        })
        with pytest.raises(ValidationError, match="requires parameter 'max'"):
            validate_variables_yml(path)

    def test_uniform_min_ge_max(self, tmp_path: Path) -> None:
        path = _write_yml(tmp_path, {
            "variables": [{"name": "x", "distribution": "uniform", "min": 10, "max": 5}]
        })
        with pytest.raises(ValidationError, match="must be less than 'max'"):
            validate_variables_yml(path)

    def test_uniform_min_eq_max(self, tmp_path: Path) -> None:
        path = _write_yml(tmp_path, {
            "variables": [{"name": "x", "distribution": "uniform", "min": 5, "max": 5}]
        })
        with pytest.raises(ValidationError, match="must be less than 'max'"):
            validate_variables_yml(path)

    def test_normal_negative_sigma(self, tmp_path: Path) -> None:
        path = _write_yml(tmp_path, {
            "variables": [{"name": "x", "distribution": "normal", "mean": 0, "sigma": -1}]
        })
        with pytest.raises(ValidationError, match="sigma.*positive"):
            validate_variables_yml(path)

    def test_normal_zero_sigma(self, tmp_path: Path) -> None:
        path = _write_yml(tmp_path, {
            "variables": [{"name": "x", "distribution": "normal", "mean": 0, "sigma": 0}]
        })
        with pytest.raises(ValidationError, match="sigma.*positive"):
            validate_variables_yml(path)

    def test_lognormal_negative_sigma(self, tmp_path: Path) -> None:
        path = _write_yml(tmp_path, {
            "variables": [{"name": "x", "distribution": "lognormal", "mean": 1, "sigma": -0.5}]
        })
        with pytest.raises(ValidationError, match="sigma.*positive"):
            validate_variables_yml(path)

    def test_triangular_mode_out_of_range(self, tmp_path: Path) -> None:
        path = _write_yml(tmp_path, {
            "variables": [
                {"name": "x", "distribution": "triangular", "min": 0, "max": 1, "mode": 2}
            ]
        })
        with pytest.raises(ValidationError, match="mode.*between"):
            validate_variables_yml(path)

    def test_discrete_empty_values(self, tmp_path: Path) -> None:
        path = _write_yml(tmp_path, {
            "variables": [{"name": "x", "distribution": "discrete", "values": []}]
        })
        with pytest.raises(ValidationError, match="must not be empty"):
            validate_variables_yml(path)

    def test_discrete_values_not_list(self, tmp_path: Path) -> None:
        path = _write_yml(tmp_path, {
            "variables": [{"name": "x", "distribution": "discrete", "values": "bad"}]
        })
        with pytest.raises(ValidationError, match="'values' must be a list"):
            validate_variables_yml(path)

    def test_beta_negative_alpha(self, tmp_path: Path) -> None:
        path = _write_yml(tmp_path, {
            "variables": [{"name": "x", "distribution": "beta", "alpha": -1, "beta": 2}]
        })
        with pytest.raises(ValidationError, match="alpha.*positive"):
            validate_variables_yml(path)

    def test_gamma_zero_alpha(self, tmp_path: Path) -> None:
        path = _write_yml(tmp_path, {
            "variables": [{"name": "x", "distribution": "gamma", "alpha": 0}]
        })
        with pytest.raises(ValidationError, match="alpha.*positive"):
            validate_variables_yml(path)

    def test_exponential_negative_rate(self, tmp_path: Path) -> None:
        path = _write_yml(tmp_path, {
            "variables": [{"name": "x", "distribution": "exponential", "rate": -1}]
        })
        with pytest.raises(ValidationError, match="rate.*positive"):
            validate_variables_yml(path)

    def test_non_numeric_min(self, tmp_path: Path) -> None:
        path = _write_yml(tmp_path, {
            "variables": [{"name": "x", "distribution": "uniform", "min": "bad", "max": 10}]
        })
        with pytest.raises(ValidationError, match="must be numeric"):
            validate_variables_yml(path)

    @pytest.mark.parametrize("dist", sorted(VALID_DISTRIBUTIONS))
    def test_all_distributions_accepted_with_valid_input(
        self, tmp_path: Path, dist: str
    ) -> None:
        """Every known distribution should pass with its required params."""
        required = DISTRIBUTION_PARAMS[dist]
        var: dict[str, object] = {"name": "v", "distribution": dist}
        # Provide valid defaults for each required param.
        for p in required:
            if p in ("min", "max"):
                var[p] = 1.0 if p == "min" else 10.0
            elif p in ("mean",):
                var[p] = 5.0
            elif p in ("sigma",):
                var[p] = 1.0
            elif p in ("alpha", "beta"):
                var[p] = 2.0
            elif p == "rate":
                var[p] = 1.0
            elif p == "values":
                var[p] = ["a", "b", "c"]
        # triangular needs mode between min/max
        if dist == "triangular":
            var["mode"] = 5.0
        path = _write_yml(tmp_path, {"variables": [var]})
        result = validate_variables_yml(path)
        assert len(result) == 1


# ======================================================================
# Template package validation
# ======================================================================


class TestValidateTemplatePackage:
    def test_valid_package(self, tmp_path: Path) -> None:
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        (pkg / "workflow.osw").write_text("{}")
        result = validate_template_package(pkg)
        assert result == pkg.resolve()

    def test_not_a_directory(self, tmp_path: Path) -> None:
        f = tmp_path / "not_a_dir.txt"
        f.write_text("data")
        with pytest.raises(ValidationError, match="not a directory"):
            validate_template_package(f)

    def test_missing_workflow_osw(self, tmp_path: Path) -> None:
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        (pkg / "other.txt").write_text("data")
        with pytest.raises(ValidationError, match="missing required file 'workflow.osw'"):
            validate_template_package(pkg)

    def test_workflow_osw_is_dir(self, tmp_path: Path) -> None:
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        (pkg / "workflow.osw").mkdir()
        with pytest.raises(ValidationError, match="not a regular file"):
            validate_template_package(pkg)

    def test_unreadable_workflow_osw(self, tmp_path: Path) -> None:
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        osw = pkg / "workflow.osw"
        osw.write_text("{}")
        osw.chmod(0o000)
        try:
            with pytest.raises(ValidationError, match="not readable"):
                validate_template_package(pkg)
        finally:
            osw.chmod(0o644)

    def test_valid_with_extra_files(self, tmp_path: Path) -> None:
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        (pkg / "workflow.osw").write_text("{}")
        (pkg / "model.osm").write_text("model data")
        weather = pkg / "weather"
        weather.mkdir()
        (weather / "file.epw").write_text("weather data")
        result = validate_template_package(pkg)
        assert result == pkg.resolve()


# ======================================================================
# API input sanitization
# ======================================================================


class TestSanitizeSampleId:
    def test_valid_alphanumeric(self) -> None:
        assert sanitize_sample_id("sample_001") == "sample_001"

    def test_valid_with_dots(self) -> None:
        assert sanitize_sample_id("v1.2.3") == "v1.2.3"

    def test_valid_with_hyphens(self) -> None:
        assert sanitize_sample_id("sample-001") == "sample-001"

    def test_empty_string(self) -> None:
        with pytest.raises(ValidationError, match="must not be empty"):
            sanitize_sample_id("")

    def test_slash_blocked(self) -> None:
        with pytest.raises(ValidationError, match="invalid characters"):
            sanitize_sample_id("../etc/passwd")

    def test_backslash_blocked(self) -> None:
        with pytest.raises(ValidationError, match="invalid characters"):
            sanitize_sample_id("..\\windows\\system32")

    def test_dotdot_blocked(self) -> None:
        with pytest.raises(ValidationError, match="invalid characters"):
            sanitize_sample_id("..")

    def test_null_byte_blocked(self) -> None:
        with pytest.raises(ValidationError, match="disallowed characters"):
            sanitize_sample_id("sample\0.txt")

    def test_html_injection_blocked(self) -> None:
        with pytest.raises(ValidationError, match="invalid characters"):
            sanitize_sample_id("<script>alert(1)</script>")

    def test_sql_injection_blocked(self) -> None:
        with pytest.raises(ValidationError, match="disallowed characters"):
            sanitize_sample_id("'; DROP TABLE samples;--")

    def test_too_long(self) -> None:
        with pytest.raises(ValidationError, match="too long"):
            sanitize_sample_id("a" * 300)

    def test_spaces_blocked(self) -> None:
        with pytest.raises(ValidationError, match="invalid characters"):
            sanitize_sample_id("has space")


class TestSanitizeFilename:
    def test_valid_png(self) -> None:
        assert sanitize_filename("plot.png") == "plot.png"

    def test_valid_with_dots(self) -> None:
        assert sanitize_filename("results.v2.png") == "results.v2.png"

    def test_empty(self) -> None:
        with pytest.raises(ValidationError, match="must not be empty"):
            sanitize_filename("")

    def test_slash_blocked(self) -> None:
        with pytest.raises(ValidationError, match="path separator"):
            sanitize_filename("../etc/passwd")

    def test_backslash_blocked(self) -> None:
        with pytest.raises(ValidationError, match="path separator"):
            sanitize_filename("..\\evil")

    def test_dotdot_blocked(self) -> None:
        with pytest.raises(ValidationError, match="traversal"):
            sanitize_filename("..")

    def test_null_byte(self) -> None:
        with pytest.raises(ValidationError, match="null byte"):
            sanitize_filename("file\0.png")

    def test_too_long(self) -> None:
        with pytest.raises(ValidationError, match="too long"):
            sanitize_filename("a" * 300)

    def test_spaces_blocked(self) -> None:
        with pytest.raises(ValidationError, match="invalid characters"):
            sanitize_filename("has space.png")
