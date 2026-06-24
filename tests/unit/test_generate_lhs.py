"""Unit tests for bin/generate_lhs.py — LHS distribution dispatch.

Verifies that every supported distribution:
  - produces the correct output shape (n_samples × n_variables),
  - produces values within expected bounds, and
  - raises ``ValueError`` with a helpful message for unsupported distributions.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import pytest
import yaml

# Import the function under test directly so we can unit-test without CLI.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "bin"))
from generate_lhs import (  # type: ignore[import-untyped]
    SUPPORTED_DISTRIBUTIONS,
    _apply_distribution,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_cli(variables: list[dict[str, Any]], n_samples: int) -> dict[str, Any]:
    """Run ``generate_lhs.py`` as a subprocess and return parsed JSON output."""
    with tempfile.TemporaryDirectory() as tmpdir:
        var_path = Path(tmpdir) / "variables.yml"
        out_path = Path(tmpdir) / "lhs_samples.json"
        var_path.write_text(yaml.dump({"variables": variables}))

        result = subprocess.run(
            [
                sys.executable,
                str(Path(__file__).resolve().parents[2] / "bin" / "generate_lhs.py"),
                "--variables_yml",
                str(var_path),
                "--n_samples",
                str(n_samples),
                "--out",
                str(out_path),
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"generate_lhs.py exited {result.returncode}\n"
                f"stdout: {result.stdout}\nstderr: {result.stderr}"
            )
        return json.loads(out_path.read_text())


def _single_var(dist: str, **params: Any) -> list[dict[str, Any]]:
    """Build a single-variable ``variables`` list."""
    return [{"name": "x", "distribution": dist, **params}]


# ---------------------------------------------------------------------------
# _apply_distribution unit tests
# ---------------------------------------------------------------------------


class TestApplyDistribution:
    """Direct unit tests for the ``_apply_distribution`` dispatch function."""

    @pytest.mark.parametrize("u", [0.01, 0.25, 0.5, 0.75, 0.99])
    def test_uniform_range(self, u: float) -> None:
        val = _apply_distribution(u, "uniform", {"min": 2.0, "max": 8.0})
        assert 2.0 <= val <= 8.0

    def test_uniform_endpoints(self) -> None:
        assert _apply_distribution(0.0, "uniform", {"min": 0.0, "max": 10.0}) == pytest.approx(0.0)
        assert _apply_distribution(1.0, "uniform", {"min": 0.0, "max": 10.0}) == pytest.approx(10.0)

    @pytest.mark.parametrize("u", [0.01, 0.5, 0.99])
    def test_normal_finite(self, u: float) -> None:
        val = _apply_distribution(u, "normal", {"mean": 0.0, "sigma": 1.0})
        assert isinstance(val, float)
        assert not (val != val)  # NaN check

    def test_normal_mean_at_median(self) -> None:
        # PPF at u=0.5 should equal the mean for normal distribution.
        val = _apply_distribution(0.5, "normal", {"mean": 42.0, "sigma": 3.0})
        assert val == pytest.approx(42.0, abs=1e-6)

    @pytest.mark.parametrize("u", [0.01, 0.25, 0.5, 0.75, 0.99])
    def test_lognormal_positive(self, u: float) -> None:
        val = _apply_distribution(u, "lognormal", {"mean": 0.5, "sigma": 0.2})
        assert val > 0.0

    @pytest.mark.parametrize("u", [0.01, 0.25, 0.5, 0.75, 0.99])
    def test_triangular_range(self, u: float) -> None:
        val = _apply_distribution(u, "triangular", {"min": 1.0, "max": 5.0})
        assert 1.0 <= val <= 5.0

    def test_triangular_mode(self) -> None:
        # With mode=2.0, PPF at ~0.25 should be closer to 2 than to 5.
        val = _apply_distribution(0.25, "triangular", {"min": 1.0, "max": 5.0, "mode": 2.0})
        assert 1.0 <= val <= 5.0

    @pytest.mark.parametrize("u", [0.01, 0.5, 0.99])
    def test_beta_range_default(self, u: float) -> None:
        val = _apply_distribution(u, "beta", {"alpha": 2.0, "beta": 5.0})
        assert 0.0 <= val <= 1.0

    @pytest.mark.parametrize("u", [0.01, 0.5, 0.99])
    def test_beta_range_shifted(self, u: float) -> None:
        val = _apply_distribution(u, "beta", {"alpha": 2.0, "beta": 5.0, "loc": 10.0, "scale": 3.0})
        assert 10.0 <= val <= 13.0

    @pytest.mark.parametrize("u", [0.01, 0.5, 0.99])
    def test_gamma_positive(self, u: float) -> None:
        val = _apply_distribution(u, "gamma", {"alpha": 2.0})
        assert val >= 0.0

    @pytest.mark.parametrize("u", [0.01, 0.5, 0.99])
    def test_exponential_positive(self, u: float) -> None:
        val = _apply_distribution(u, "exponential", {"rate": 5.0})
        assert val >= 0.0

    def test_exponential_scale(self) -> None:
        # exponential with rate=10 → mean = 1/rate = 0.1.
        # PPF(0.6321...) = -ln(1-u)/rate = -ln(exp(-1))/10 = 1/10 = 0.1
        import math

        u = 1 - math.exp(-1)  # ≈ 0.6321
        val = _apply_distribution(u, "exponential", {"rate": 10.0})
        assert val == pytest.approx(0.1, rel=0.01)

    def test_unsupported_raises_valueerror(self) -> None:
        with pytest.raises(ValueError, match="unsupported distribution.*'weibull'"):
            _apply_distribution(0.5, "weibull", {})

    def test_unsupported_message_lists_supported(self) -> None:
        with pytest.raises(ValueError) as exc_info:
            _apply_distribution(0.5, "nonexistent", {})
        for d in SUPPORTED_DISTRIBUTIONS:
            assert d in str(exc_info.value)


# ---------------------------------------------------------------------------
# CLI integration tests (end-to-end through main())
# ---------------------------------------------------------------------------


class TestCLIEndToEnd:
    """Run generate_lhs.py as a subprocess and check output shape."""

    @pytest.mark.parametrize(
        "dist,params",
        [
            ("uniform", {"min": 1.0, "max": 5.0}),
            ("lognormal", {"mean": 0.5, "sigma": 0.2}),
            ("normal", {"mean": 22.0, "sigma": 1.0}),
            ("triangular", {"min": 1.0, "max": 5.0}),
            ("beta", {"alpha": 2.0, "beta": 5.0}),
            ("gamma", {"alpha": 2.0}),
            ("exponential", {"rate": 10.0}),
        ],
    )
    def test_output_shape(self, dist: str, params: dict[str, Any]) -> None:
        """n_samples rows × 1 variable, all with correct keys."""
        n = 50
        data = _run_cli(_single_var(dist, **params), n)
        assert data["n_samples"] == n
        assert len(data["samples"]) == n
        for sample in data["samples"]:
            assert "sample_id" in sample
            assert "values" in sample
            assert "x" in sample["values"]

    @pytest.mark.parametrize(
        "dist,params,check",
        [
            ("uniform", {"min": 1.0, "max": 5.0}, lambda v: 1.0 <= v <= 5.0),
            ("triangular", {"min": 2.0, "max": 8.0}, lambda v: 2.0 <= v <= 8.0),
            ("beta", {"alpha": 2.0, "beta": 5.0}, lambda v: 0.0 <= v <= 1.0),
            ("gamma", {"alpha": 2.0}, lambda v: v >= 0.0),
            ("exponential", {"rate": 5.0}, lambda v: v >= 0.0),
            ("lognormal", {"mean": 0.5, "sigma": 0.2}, lambda v: v > 0.0),
        ],
    )
    def test_output_range(
        self,
        dist: str,
        params: dict[str, Any],
        check: Any,
    ) -> None:
        """All samples satisfy the distribution's range constraint."""
        data = _run_cli(_single_var(dist, **params), 100)
        for sample in data["samples"]:
            assert check(sample["values"]["x"]), (
                f"{dist}: value {sample['values']['x']!r} out of range"
            )

    def test_multiple_variables(self) -> None:
        """Multiple variables in one run all resolve correctly."""
        variables = [
            {"name": "a", "distribution": "uniform", "min": 0.0, "max": 1.0},
            {"name": "b", "distribution": "normal", "mean": 0.0, "sigma": 1.0},
            {"name": "c", "distribution": "exponential", "rate": 2.0},
        ]
        data = _run_cli(variables, 20)
        for sample in data["samples"]:
            assert len(sample["values"]) == 3
            assert "a" in sample["values"]
            assert "b" in sample["values"]
            assert "c" in sample["values"]

    def test_unsupported_distribution_exits_with_error(self) -> None:
        """An unknown distribution causes a non-zero exit with ValueError."""
        with pytest.raises(RuntimeError, match="unsupported distribution"):
            _run_cli(
                _single_var("weibull", shape=2.0, scale=1.0),
                10,
            )

    def test_zero_samples(self) -> None:
        """n_samples=0 produces empty output without error."""
        data = _run_cli(
            [{"name": "x", "distribution": "uniform", "min": 0.0, "max": 1.0}],
            0,
        )
        assert data["n_samples"] == 0
        assert data["samples"] == []


# ---------------------------------------------------------------------------
# Discrete and categorical distribution tests (issue #54)
# ---------------------------------------------------------------------------


class TestDiscreteDistribution:
    """Tests for the ``discrete`` distribution type."""

    def test_apply_returns_value_from_list(self) -> None:
        """Each u ∈ [0,1) maps to a value from the provided list."""
        values = [10, 20, 30, 40]
        for u in [0.0, 0.25, 0.5, 0.75, 0.99]:
            result = _apply_distribution(u, "discrete", {"values": values})
            assert result in values, f"u={u} produced {result!r}, expected one of {values}"

    def test_apply_index_mapping(self) -> None:
        """u values map to predictable indices: floor(u * len(values))."""
        values = [100, 200, 300]
        assert _apply_distribution(0.0, "discrete", {"values": values}) == 100
        assert _apply_distribution(0.33, "discrete", {"values": values}) == 100
        assert _apply_distribution(0.34, "discrete", {"values": values}) == 200
        assert _apply_distribution(0.66, "discrete", {"values": values}) == 200
        assert _apply_distribution(0.99, "discrete", {"values": values}) == 300

    def test_apply_empty_values_raises(self) -> None:
        """Empty values list raises ValueError."""
        with pytest.raises(ValueError, match="non-empty 'values' list"):
            _apply_distribution(0.5, "discrete", {"values": []})

    def test_apply_missing_values_raises(self) -> None:
        """Missing values key raises ValueError."""
        with pytest.raises(ValueError, match="non-empty 'values' list"):
            _apply_distribution(0.5, "discrete", {})

    def test_all_values_appear_in_large_sample(self) -> None:
        """With enough samples, every discrete value appears at least once."""
        values = [0, 90, 180, 270]
        data = _run_cli(
            [{"name": "orientation", "distribution": "discrete", "values": values}],
            200,
        )
        seen = set()
        for sample in data["samples"]:
            seen.add(sample["values"]["orientation"])
        assert values == sorted(seen), f"Expected all of {values}, got {sorted(seen)}"

    def test_cli_output_shape(self) -> None:
        """CLI end-to-end produces correct shape for discrete variables."""
        data = _run_cli(
            [{"name": "orient", "distribution": "discrete", "values": [0, 90, 180, 270]}],
            20,
        )
        assert data["n_samples"] == 20
        assert len(data["samples"]) == 20
        for sample in data["samples"]:
            assert "orient" in sample["values"]
            assert sample["values"]["orient"] in [0, 90, 180, 270]


class TestCategoricalDistribution:
    """Tests for the ``categorical`` distribution type."""

    def test_apply_returns_structured_output(self) -> None:
        """Categorical returns dict with label, index, and mapping."""
        values = ["packaged_rooftop", "vav", "wshp", "gshp"]
        mapping = {
            "packaged_rooftop": {"type": "PackagedRooftop", "efficiency": 0.85},
            "vav": {"type": "VAV", "efficiency": 0.90},
            "wshp": {"type": "WSHP", "efficiency": 0.88},
            "gshp": {"type": "GSHP", "efficiency": 0.95},
        }
        result = _apply_distribution(0.0, "categorical", {"values": values, "mapping": mapping})
        assert isinstance(result, dict)
        assert result["label"] == "packaged_rooftop"
        assert result["index"] == 0
        assert result["mapping"] == {"type": "PackagedRooftop", "efficiency": 0.85}

    def test_apply_categorical_no_mapping(self) -> None:
        """Categorical without mapping returns label + index only."""
        values = ["a", "b", "c"]
        result = _apply_distribution(0.5, "categorical", {"values": values})
        assert isinstance(result, dict)
        assert result["label"] == "b"
        assert result["index"] == 1
        assert "mapping" not in result

    def test_apply_empty_values_raises(self) -> None:
        """Empty values list raises ValueError."""
        with pytest.raises(ValueError, match="non-empty 'values' list"):
            _apply_distribution(0.5, "categorical", {"values": []})

    def test_all_labels_appear_in_large_sample(self) -> None:
        """With enough samples, every categorical label appears at least once."""
        labels = ["packaged_rooftop", "vav", "wshp", "gshp"]
        data = _run_cli(
            [
                {
                    "name": "hvac_system",
                    "distribution": "categorical",
                    "values": labels,
                    "mapping": {lbl: {"type": lbl} for lbl in labels},
                }
            ],
            200,
        )
        seen_labels = set()
        for sample in data["samples"]:
            val = sample["values"]["hvac_system"]
            assert isinstance(val, dict), f"Expected dict, got {type(val).__name__}"
            assert "label" in val
            assert "mapping" in val
            seen_labels.add(val["label"])
        assert set(labels) == seen_labels, f"Expected all of {labels}, got {seen_labels}"

    def test_cli_output_includes_mapping(self) -> None:
        """CLI output includes the resolved mapping for each sample."""
        mapping = {
            "packaged_rooftop": {"type": "PackagedRooftop", "efficiency": 0.85},
            "vav": {"type": "VAV", "efficiency": 0.90},
        }
        data = _run_cli(
            [
                {
                    "name": "hvac",
                    "distribution": "categorical",
                    "values": ["packaged_rooftop", "vav"],
                    "mapping": mapping,
                }
            ],
            20,
        )
        for sample in data["samples"]:
            val = sample["values"]["hvac"]
            assert val["label"] in ["packaged_rooftop", "vav"]
            assert val["mapping"] is not None
            assert "type" in val["mapping"]
            assert "efficiency" in val["mapping"]

    def test_per_sample_param_file_flattens_label(self) -> None:
        """Per-sample .params.json files contain the label string, not the struct."""
        import tempfile

        mapping = {"a": {"type": "A"}, "b": {"type": "B"}}
        with tempfile.TemporaryDirectory() as tmpdir:
            var_path = Path(tmpdir) / "variables.yml"
            out_path = Path(tmpdir) / "lhs_samples.json"
            var_path.write_text(
                yaml.dump(
                    {
                        "variables": [
                            {
                                "name": "choice",
                                "distribution": "categorical",
                                "values": ["a", "b"],
                                "mapping": mapping,
                            }
                        ]
                    }
                )
            )
            result = subprocess.run(
                [
                    sys.executable,
                    str(Path(__file__).resolve().parents[2] / "bin" / "generate_lhs.py"),
                    "--variables_yml",
                    str(var_path),
                    "--n_samples",
                    "10",
                    "--out",
                    str(out_path),
                ],
                capture_output=True,
                text=True,
            )
            assert result.returncode == 0, f"stderr: {result.stderr}"
            for i in range(1, 11):
                param_file = out_path.parent / f"{i:04d}.params.json"
                assert param_file.exists(), f"Missing param file {param_file}"
                flat = json.loads(param_file.read_text())
                assert flat["choice"] in ["a", "b"], f"Expected label, got {flat['choice']!r}"


class TestMixedDistributionTypes:
    """Tests that continuous + discrete + categorical variables coexist."""

    def test_mixed_distributions(self) -> None:
        """All three types produce correct output in a single run."""
        variables = [
            {"name": "wwr", "distribution": "uniform", "min": 0.2, "max": 0.6},
            {"name": "orientation", "distribution": "discrete", "values": [0, 90, 180, 270]},
            {
                "name": "hvac",
                "distribution": "categorical",
                "values": ["rtu", "vav"],
                "mapping": {"rtu": {"eff": 0.85}, "vav": {"eff": 0.90}},
            },
        ]
        data = _run_cli(variables, 30)
        assert data["n_samples"] == 30
        for sample in data["samples"]:
            vals = sample["values"]
            # Continuous: float in range
            assert isinstance(vals["wwr"], float)
            assert 0.2 <= vals["wwr"] <= 0.6
            # Discrete: exact value from list
            assert vals["orientation"] in [0, 90, 180, 270]
            # Categorical: structured dict
            assert isinstance(vals["hvac"], dict)
            assert vals["hvac"]["label"] in ["rtu", "vav"]
            assert vals["hvac"]["mapping"] is not None

    def test_backward_compatible_with_continuous_only(self) -> None:
        """Pre-existing continuous-only configs still work identically."""
        variables = [
            {"name": "x", "distribution": "uniform", "min": 0.0, "max": 1.0},
            {"name": "y", "distribution": "normal", "mean": 0.0, "sigma": 1.0},
        ]
        data = _run_cli(variables, 50)
        assert data["n_samples"] == 50
        for sample in data["samples"]:
            assert isinstance(sample["values"]["x"], float)
            assert isinstance(sample["values"]["y"], float)
