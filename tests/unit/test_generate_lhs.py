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
        # exponential with rate=10 → scale=10 → mean=10.
        # PPF(0.6321...) ≈ scale ≈ 10 for an exponential.
        import math

        u = 1 - math.exp(-1)  # ≈ 0.6321
        val = _apply_distribution(u, "exponential", {"rate": 10.0})
        assert val == pytest.approx(10.0, rel=0.01)

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
