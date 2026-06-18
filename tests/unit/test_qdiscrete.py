"""Tests for osimflow.algorithms.qdiscrete (issue #579).

Covers:
- qdiscrete inverse-CDF sampling for discrete distributions
- pmf_from_distribution factory for all distribution types
- QDError for invalid inputs
- Seed reproducibility
- Uniform vs weighted sampling correctness
"""

import json
from pathlib import Path
from typing import Any

import pytest

from osimflow.algorithms.qdiscrete import (
    QDError,
    pmf_from_distribution,
    qdiscrete,
)


class TestQdiscreteBasic:
    """Basic qdiscrete tests."""

    def test_single_sample(self) -> None:
        pmf = {"a": 1.0, "b": 2.0, "c": 3.0}
        result = qdiscrete(pmf, n=1, seed=42)
        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0] in {"a", "b", "c"}

    def test_multiple_samples(self) -> None:
        pmf = {"low": 0.2, "medium": 0.5, "high": 0.3}
        result = qdiscrete(pmf, n=10, seed=42)
        assert len(result) == 10
        for v in result:
            assert v in {"low", "medium", "high"}

    def test_reproducible_with_seed(self) -> None:
        pmf = {"a": 1.0, "b": 2.0}
        r1 = qdiscrete(pmf, n=5, seed=99)
        r2 = qdiscrete(pmf, n=5, seed=99)
        assert r1 == r2

    def test_different_seeds_different_results(self) -> None:
        pmf = {"a": 1.0, "b": 2.0}
        r1 = qdiscrete(pmf, n=5, seed=1)
        r2 = qdiscrete(pmf, n=5, seed=2)
        assert r1 != r2

    def test_empty_pmf_raises(self) -> None:
        with pytest.raises(QDError, match="non-empty"):
            qdiscrete({}, n=1)

    def test_negative_probability_raises(self) -> None:
        with pytest.raises(QDError, match="non-negative"):
            qdiscrete({"a": -0.5, "b": 1.5}, n=1)

    def test_zero_total_returns_uniform(self) -> None:
        result = qdiscrete({"a": 0.0, "b": 0.0}, n=10, seed=42)
        assert len(result) == 10
        assert all(v in {"a", "b"} for v in result)

    def test_n_zero_raises(self) -> None:
        with pytest.raises(QDError, match="n must be >= 1"):
            qdiscrete({"a": 1.0}, n=0)

    def test_normalises_unnormalised_pmf(self) -> None:
        pmf = {"a": 1.0, "b": 4.0}
        result = qdiscrete(pmf, n=100, seed=42)
        assert all(v in {"a", "b"} for v in result)
        count_b = result.count("b")
        assert count_b > 70

    def test_returns_list_not_scalar(self) -> None:
        pmf = {"x": 1.0}
        result = qdiscrete(pmf, n=1, seed=0)
        assert isinstance(result, list)
        assert len(result) == 1


class TestQdiscreteWeightedSampling:
    """Verify that qdiscrete respects probability weights (issue #579)."""

    def test_high_weight_selected_more_often(self) -> None:
        pmf = {"low": 0.01, "high": 0.99}
        result = qdiscrete(pmf, n=1000, seed=42)
        high_count = result.count("high")
        low_count = result.count("low")
        assert high_count > 900
        assert low_count < 100

    def test_uniform_weights_equal_selection(self) -> None:
        pmf = {"a": 1.0, "b": 1.0, "c": 1.0}
        result = qdiscrete(pmf, n=300, seed=42)
        for v in ["a", "b", "c"]:
            count = result.count(v)
            assert 80 <= count <= 120

    def test_three_value_weighted(self) -> None:
        pmf = {"small": 0.1, "medium": 0.2, "large": 0.7}
        result = qdiscrete(pmf, n=1000, seed=7)
        small = result.count("small")
        medium = result.count("medium")
        large = result.count("large")
        assert 50 <= small <= 200
        assert 100 <= medium <= 350
        assert large > 500


class TestQdiscreteNumericKeys:
    """qdiscrete with numeric keys (int/float)."""

    def test_integer_keys(self) -> None:
        pmf = {10: 1.0, 20: 3.0}
        result = qdiscrete(pmf, n=20, seed=0)
        assert all(v in {10, 20} for v in result)
        assert 20 in result

    def test_float_keys(self) -> None:
        pmf = {0.1: 1.0, 0.9: 4.0}
        result = qdiscrete(pmf, n=20, seed=0)
        assert all(v in {0.1, 0.9} for v in result)
        assert 0.9 in result


class TestPmfFromDistribution:
    """Tests for pmf_from_distribution factory."""

    def test_uniform(self) -> None:
        var_def = {"distribution": "uniform", "values": ["a", "b", "c"]}
        pmf = pmf_from_distribution(var_def)
        assert pmf == {"a": 1.0, "b": 1.0, "c": 1.0}

    def test_uniform_single_value(self) -> None:
        var_def = {"distribution": "uniform", "values": ["only"]}
        pmf = pmf_from_distribution(var_def)
        assert pmf == {"only": 1.0}

    def test_normal_pmf(self) -> None:
        var_def = {
            "distribution": "normal",
            "mean": 0.0,
            "sigma": 1.0,
            "values": [-1.0, 0.0, 1.0],
        }
        pmf = pmf_from_distribution(var_def)
        assert set(pmf.keys()) == {-1.0, 0.0, 1.0}
        assert all(v > 0 for v in pmf.values())
        assert pmf[0.0] > pmf[-1.0]
        assert pmf[0.0] > pmf[1.0]

    def test_normal_requires_sigma(self) -> None:
        var_def = {"distribution": "normal", "mean": 0.0, "values": [1.0, 2.0]}
        with pytest.raises(QDError, match="sigma"):
            pmf_from_distribution(var_def)

    def test_lognormal_pmf(self) -> None:
        var_def = {
            "distribution": "lognormal",
            "mean": 0.0,
            "sigma": 0.5,
            "values": [0.5, 1.0, 2.0],
        }
        pmf = pmf_from_distribution(var_def)
        assert set(pmf.keys()) == {0.5, 1.0, 2.0}
        assert all(v > 0 for v in pmf.values())

    def test_lognormal_requires_positive_values(self) -> None:
        var_def = {
            "distribution": "lognormal",
            "mean": 0.0,
            "sigma": 0.5,
            "values": [-1.0, 1.0],
        }
        with pytest.raises(QDError, match="positive"):
            pmf_from_distribution(var_def)

    def test_triangular_pmf(self) -> None:
        var_def = {
            "distribution": "triangular",
            "min": 0.0,
            "max": 10.0,
            "mode": 8.0,
            "values": [0.0, 2.0, 5.0, 8.0, 10.0],
        }
        pmf = pmf_from_distribution(var_def)
        assert set(pmf.keys()) == {0.0, 2.0, 5.0, 8.0, 10.0}
        assert pmf[8.0] > pmf[0.0]

    def test_triangular_without_mode(self) -> None:
        var_def = {
            "distribution": "triangular",
            "min": 0.0,
            "max": 10.0,
            "values": [0.0, 5.0, 10.0],
        }
        pmf = pmf_from_distribution(var_def)
        assert set(pmf.keys()) == {0.0, 5.0, 10.0}

    def test_discrete_explicit_pmf(self) -> None:
        var_def = {"distribution": "discrete", "pmf": {"x": 0.3, "y": 0.7}}
        pmf = pmf_from_distribution(var_def)
        assert pmf == {"x": 0.3, "y": 0.7}

    def test_discrete_values_uniform(self) -> None:
        var_def = {"distribution": "discrete", "values": [10, 20, 30]}
        pmf = pmf_from_distribution(var_def)
        assert pmf == {10: 1.0, 20: 1.0, 30: 1.0}

    def test_discrete_requires_values_or_pmf(self) -> None:
        var_def = {"distribution": "discrete"}
        with pytest.raises(QDError, match="values.*pmf"):
            pmf_from_distribution(var_def)

    def test_unknown_distribution_raises(self) -> None:
        var_def = {"distribution": "not_a_real_dist", "values": [1, 2]}
        with pytest.raises(QDError, match="unsupported"):
            pmf_from_distribution(var_def)


class TestQdiscreteRegressionIssue579:
    """Regression test: verify qdiscrete matches DoE.base behaviour for issue #579.

    The key property is that drawing N samples from a PMF using inverse-CDF
    (qdiscrete) produces a distribution of values whose empirical proportions
    approximate the input PMF for sufficiently large N.
    """

    def test_empirical_distribution_matches_pmf(self) -> None:
        pmf = {"a": 0.25, "b": 0.50, "c": 0.25}
        n = 10_000
        result = qdiscrete(pmf, n=n, seed=123)
        counts = {k: result.count(k) / n for k in pmf}
        for k, expected_p in pmf.items():
            actual_p = counts[k]
            assert abs(actual_p - expected_p) < 0.03

    def test_lognormal_discrete_values_approximate_distribution(self) -> None:
        var_def = {
            "distribution": "lognormal",
            "mean": 1.0,
            "sigma": 0.5,
            "values": [0.5, 1.0, 1.5, 2.0, 3.0],
        }
        pmf = pmf_from_distribution(var_def)
        n = 5000
        result = qdiscrete(pmf, n=n, seed=456)
        assert len(result) == n
        assert all(v in pmf for v in result)
        assert result.count(1.0) > result.count(0.5)


class TestQdiscreteIntegrationFullFactorial:
    """Integration: FullFactorialAlgorithm with discrete_distribution/qdiscrete."""

    def test_fullfact_with_discrete_distribution(self, tmp_path: Path) -> None:
        from osimflow.algorithms import FullFactorialAlgorithm

        variables: dict[str, Any] = {
            "variables": [
                {
                    "name": "hvac_type",
                    "levels": ["VAV", "PTAC", "Radiant"],
                    "discrete_distribution": {
                        "pmf": {"VAV": 0.6, "PTAC": 0.3, "Radiant": 0.1},
                    },
                },
            ]
        }
        algo = FullFactorialAlgorithm()
        result = algo.generate_samples(variables, n_samples=20, seed=42, outdir=tmp_path)
        data = json.loads(result.read_text())
        assert len(data["samples"]) == 20
        for s in data["samples"]:
            assert s["values"]["hvac_type"] in {"VAV", "PTAC", "Radiant"}

    def test_fullfact_discrete_distribution_reproducible(self, tmp_path: Path) -> None:
        from osimflow.algorithms import FullFactorialAlgorithm

        variables: dict[str, Any] = {
            "variables": [
                {
                    "name": "x",
                    "levels": ["a", "b"],
                    "discrete_distribution": {"pmf": {"a": 0.2, "b": 0.8}},
                },
            ]
        }
        algo = FullFactorialAlgorithm()
        r1 = algo.generate_samples(variables, n_samples=10, seed=7, outdir=tmp_path / "a")
        r2 = algo.generate_samples(variables, n_samples=10, seed=7, outdir=tmp_path / "b")
        assert json.loads(r1.read_text()) == json.loads(r2.read_text())

    def test_fullfact_without_discrete_distribution_unchanged(self, tmp_path: Path) -> None:
        from osimflow.algorithms import FullFactorialAlgorithm

        variables: dict[str, Any] = {
            "variables": [
                {"name": "wall_r", "levels": [2.0, 4.0, 6.0]},
                {"name": "window_shgc", "levels": [0.3, 0.5]},
            ]
        }
        algo = FullFactorialAlgorithm()
        result = algo.generate_samples(variables, n_samples=6, seed=None, outdir=tmp_path)
        data = json.loads(result.read_text())
        combos = {(s["values"]["wall_r"], s["values"]["window_shgc"]) for s in data["samples"]}
        assert combos == {(2.0, 0.3), (2.0, 0.5), (4.0, 0.3), (4.0, 0.5), (6.0, 0.3), (6.0, 0.5)}

    def test_fullfact_discrete_distribution_violates_levels_count(self, tmp_path: Path) -> None:
        from osimflow.algorithms import FullFactorialAlgorithm

        variables: dict[str, Any] = {
            "variables": [
                {
                    "name": "x",
                    "levels": ["A", "B", "C"],
                    "discrete_distribution": {"pmf": {"A": 0.0, "B": 0.0, "C": 1.0}},
                },
            ]
        }
        algo = FullFactorialAlgorithm()
        result = algo.generate_samples(variables, n_samples=10, seed=0, outdir=tmp_path)
        data = json.loads(result.read_text())
        assert len(data["samples"]) == 10
        for s in data["samples"]:
            assert s["values"]["x"] == "C"
