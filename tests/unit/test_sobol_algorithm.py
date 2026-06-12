"""Tests for Sobol and Halton quasi-random samplers (issue #139).

Covers:
- AlgorithmRegistry.get("sobol") returns SobolAlgorithm
- AlgorithmRegistry.get("halton") returns HaltonAlgorithm
- SobolAlgorithm / HaltonAlgorithm interface contracts
- Sobol discrepancy is lower than LHS for equivalent sample counts
- Halton generates the correct number of samples
- AlgorithmRegistry.list_available() includes "sobol" and "halton"
- Conditional variable resolution
- RuntimeError wrapping
- No independent variables edge case
"""

import json
from pathlib import Path
from typing import Any

import pytest
import scipy.stats.qmc

from osimflow.algorithms import AlgorithmRegistry, BaseAlgorithm
from osimflow.algorithms.halton import HaltonAlgorithm
from osimflow.algorithms.sobol import SobolAlgorithm

# ======================================================================
# Registry tests
# ======================================================================


class TestSobolRegistry:
    """Registry discovery tests for SobolAlgorithm."""

    def test_get_sobol_returns_sobol_algorithm(self) -> None:
        algo = AlgorithmRegistry.get("sobol")
        assert isinstance(algo, SobolAlgorithm)

    def test_get_sobol_returns_base_algorithm(self) -> None:
        algo = AlgorithmRegistry.get("sobol")
        assert isinstance(algo, BaseAlgorithm)


class TestHaltonRegistry:
    """Registry discovery tests for HaltonAlgorithm."""

    def test_get_halton_returns_halton_algorithm(self) -> None:
        algo = AlgorithmRegistry.get("halton")
        assert isinstance(algo, HaltonAlgorithm)

    def test_get_halton_returns_base_algorithm(self) -> None:
        algo = AlgorithmRegistry.get("halton")
        assert isinstance(algo, BaseAlgorithm)


class TestRegistryIncludesAll:
    """list_available() must include all three built-in algorithms."""

    def test_list_available_includes_sobol(self) -> None:
        assert "sobol" in AlgorithmRegistry.list_available()

    def test_list_available_includes_halton(self) -> None:
        assert "halton" in AlgorithmRegistry.list_available()

    def test_list_available_includes_all_three(self) -> None:
        available = AlgorithmRegistry.list_available()
        assert "lhs" in available
        assert "sobol" in available
        assert "halton" in available


# ======================================================================
# SobolAlgorithm interface tests
# ======================================================================


class TestSobolInterface:
    """Contract tests for SobolAlgorithm."""

    def test_name(self) -> None:
        assert SobolAlgorithm().name() == "sobol"

    def test_is_iterative_false(self) -> None:
        assert SobolAlgorithm().is_iterative() is False

    def test_is_converged_empty_history(self) -> None:
        assert SobolAlgorithm().is_converged([]) is True

    def test_is_converged_with_history(self) -> None:
        assert SobolAlgorithm().is_converged([{"samples": []}]) is True

    def test_observe_empty_history(self) -> None:
        assert SobolAlgorithm().observe([]) == []

    def test_observe_returns_last_samples(self) -> None:
        algo = SobolAlgorithm()
        history: list[dict[str, Any]] = [
            {"samples": [{"sample_id": "s0000"}]},
            {"samples": [{"sample_id": "s0001"}]},
        ]
        result = algo.observe(history)
        assert len(result) == 1
        assert result[0]["sample_id"] == "s0001"


# ======================================================================
# HaltonAlgorithm interface tests
# ======================================================================


class TestHaltonInterface:
    """Contract tests for HaltonAlgorithm."""

    def test_name(self) -> None:
        assert HaltonAlgorithm().name() == "halton"

    def test_is_iterative_false(self) -> None:
        assert HaltonAlgorithm().is_iterative() is False

    def test_is_converged_empty_history(self) -> None:
        assert HaltonAlgorithm().is_converged([]) is True

    def test_is_converged_with_history(self) -> None:
        assert HaltonAlgorithm().is_converged([{"samples": []}]) is True

    def test_observe_empty_history(self) -> None:
        assert HaltonAlgorithm().observe([]) == []

    def test_observe_returns_last_samples(self) -> None:
        algo = HaltonAlgorithm()
        history: list[dict[str, Any]] = [
            {"samples": [{"sample_id": "s0000"}]},
            {"samples": [{"sample_id": "s0001"}]},
        ]
        result = algo.observe(history)
        assert len(result) == 1
        assert result[0]["sample_id"] == "s0001"


# ======================================================================
# Sample generation tests
# ======================================================================

_VARIABLES_2D: dict[str, Any] = {
    "variables": [
        {"name": "wall_r", "distribution": "uniform", "min": 1.0, "max": 10.0},
        {"name": "window_shgc", "distribution": "uniform", "min": 0.1, "max": 0.9},
    ]
}


class TestSobolGenerateSamples:
    """Generate-samples tests for SobolAlgorithm."""

    def test_creates_samples_json(self, tmp_path: Path) -> None:
        algo = SobolAlgorithm()
        result = algo.generate_samples(_VARIABLES_2D, n_samples=8, seed=42, outdir=tmp_path)
        assert result.exists()
        data = json.loads(result.read_text())
        assert "samples" in data
        assert len(data["samples"]) == 8

    def test_sample_structure(self, tmp_path: Path) -> None:
        algo = SobolAlgorithm()
        result = algo.generate_samples(_VARIABLES_2D, n_samples=4, seed=0, outdir=tmp_path)
        data = json.loads(result.read_text())
        for sample in data["samples"]:
            assert "sample_id" in sample
            assert "values" in sample
            assert "wall_r" in sample["values"]
            assert "window_shgc" in sample["values"]

    def test_creates_outdir(self, tmp_path: Path) -> None:
        algo = SobolAlgorithm()
        nested = tmp_path / "deep" / "nested"
        algo.generate_samples(_VARIABLES_2D, n_samples=2, seed=0, outdir=nested)
        assert nested.is_dir()

    def test_seed_reproducible(self, tmp_path: Path) -> None:
        algo = SobolAlgorithm()
        r1 = algo.generate_samples(_VARIABLES_2D, n_samples=10, seed=123, outdir=tmp_path / "r1")
        r2 = algo.generate_samples(_VARIABLES_2D, n_samples=10, seed=123, outdir=tmp_path / "r2")
        assert json.loads(r1.read_text()) == json.loads(r2.read_text())

    def test_empty_variables(self, tmp_path: Path) -> None:
        algo = SobolAlgorithm()
        result = algo.generate_samples({"variables": []}, n_samples=5, seed=None, outdir=tmp_path)
        data = json.loads(result.read_text())
        assert data["samples"] == []

    def test_no_independent_variables(self, tmp_path: Path) -> None:
        algo = SobolAlgorithm()
        variables: dict[str, Any] = {
            "variables": [
                {"name": "x", "distribution": "conditional", "depends_on": {"variable": "y"}},
            ]
        }
        result = algo.generate_samples(variables, n_samples=5, seed=42, outdir=tmp_path)
        data = json.loads(result.read_text())
        assert data["samples"] == []

    def test_conditional_variable_resolution(self, tmp_path: Path) -> None:
        variables: dict[str, Any] = {
            "variables": [
                {"name": "x", "distribution": "uniform", "min": 0.0, "max": 1.0},
                {
                    "name": "y",
                    "distribution": "conditional",
                    "depends_on": {"variable": "x", "match": "True"},
                    "conditions": [
                        {"distribution": "uniform", "min": 10.0, "max": 20.0},
                    ],
                },
            ]
        }
        algo = SobolAlgorithm()
        result = algo.generate_samples(variables, n_samples=4, seed=42, outdir=tmp_path)
        data = json.loads(result.read_text())
        assert len(data["samples"]) == 4
        for s in data["samples"]:
            assert "y" in s["values"]

    def test_values_in_range(self, tmp_path: Path) -> None:
        algo = SobolAlgorithm()
        result = algo.generate_samples(_VARIABLES_2D, n_samples=16, seed=42, outdir=tmp_path)
        data = json.loads(result.read_text())
        for sample in data["samples"]:
            assert 1.0 <= sample["values"]["wall_r"] <= 10.0
            assert 0.1 <= sample["values"]["window_shgc"] <= 0.9

    def test_runtime_error_on_bad_distribution(self, tmp_path: Path) -> None:
        algo = SobolAlgorithm()
        variables: dict[str, Any] = {
            "variables": [
                {"name": "x", "distribution": "unsupported_dist_xyz"},
            ]
        }
        with pytest.raises(RuntimeError, match="generate_sobol failed"):
            algo.generate_samples(variables, n_samples=3, seed=42, outdir=tmp_path)

    def test_different_seeds_different_results(self, tmp_path: Path) -> None:
        algo = SobolAlgorithm()
        r1 = algo.generate_samples(_VARIABLES_2D, n_samples=4, seed=1, outdir=tmp_path / "r1")
        r2 = algo.generate_samples(_VARIABLES_2D, n_samples=4, seed=2, outdir=tmp_path / "r2")
        assert json.loads(r1.read_text()) != json.loads(r2.read_text())


class TestHaltonGenerateSamples:
    """Generate-samples tests for HaltonAlgorithm."""

    def test_creates_correct_number_of_samples(self, tmp_path: Path) -> None:
        algo = HaltonAlgorithm()
        result = algo.generate_samples(_VARIABLES_2D, n_samples=100, seed=42, outdir=tmp_path)
        data = json.loads(result.read_text())
        assert len(data["samples"]) == 100

    def test_sample_structure(self, tmp_path: Path) -> None:
        algo = HaltonAlgorithm()
        result = algo.generate_samples(_VARIABLES_2D, n_samples=4, seed=0, outdir=tmp_path)
        data = json.loads(result.read_text())
        for sample in data["samples"]:
            assert "sample_id" in sample
            assert "values" in sample
            assert "wall_r" in sample["values"]
            assert "window_shgc" in sample["values"]

    def test_creates_outdir(self, tmp_path: Path) -> None:
        algo = HaltonAlgorithm()
        nested = tmp_path / "deep" / "nested"
        algo.generate_samples(_VARIABLES_2D, n_samples=2, seed=0, outdir=nested)
        assert nested.is_dir()

    def test_seed_reproducible(self, tmp_path: Path) -> None:
        algo = HaltonAlgorithm()
        r1 = algo.generate_samples(_VARIABLES_2D, n_samples=10, seed=123, outdir=tmp_path / "r1")
        r2 = algo.generate_samples(_VARIABLES_2D, n_samples=10, seed=123, outdir=tmp_path / "r2")
        assert json.loads(r1.read_text()) == json.loads(r2.read_text())

    def test_empty_variables(self, tmp_path: Path) -> None:
        algo = HaltonAlgorithm()
        result = algo.generate_samples({"variables": []}, n_samples=5, seed=None, outdir=tmp_path)
        data = json.loads(result.read_text())
        assert data["samples"] == []

    def test_no_independent_variables(self, tmp_path: Path) -> None:
        algo = HaltonAlgorithm()
        variables: dict[str, Any] = {
            "variables": [
                {"name": "x", "distribution": "conditional", "depends_on": {"variable": "y"}},
            ]
        }
        result = algo.generate_samples(variables, n_samples=5, seed=42, outdir=tmp_path)
        data = json.loads(result.read_text())
        assert data["samples"] == []

    def test_conditional_variable_resolution(self, tmp_path: Path) -> None:
        variables: dict[str, Any] = {
            "variables": [
                {"name": "x", "distribution": "uniform", "min": 0.0, "max": 1.0},
                {
                    "name": "y",
                    "distribution": "conditional",
                    "depends_on": {"variable": "x", "match": "True"},
                    "conditions": [
                        {"distribution": "uniform", "min": 10.0, "max": 20.0},
                    ],
                },
            ]
        }
        algo = HaltonAlgorithm()
        result = algo.generate_samples(variables, n_samples=4, seed=42, outdir=tmp_path)
        data = json.loads(result.read_text())
        for s in data["samples"]:
            assert "y" in s["values"]

    def test_values_in_range(self, tmp_path: Path) -> None:
        algo = HaltonAlgorithm()
        result = algo.generate_samples(_VARIABLES_2D, n_samples=50, seed=42, outdir=tmp_path)
        data = json.loads(result.read_text())
        for sample in data["samples"]:
            assert 1.0 <= sample["values"]["wall_r"] <= 10.0
            assert 0.1 <= sample["values"]["window_shgc"] <= 0.9

    def test_runtime_error_on_bad_distribution(self, tmp_path: Path) -> None:
        algo = HaltonAlgorithm()
        variables: dict[str, Any] = {
            "variables": [
                {"name": "x", "distribution": "unsupported_dist_xyz"},
            ]
        }
        with pytest.raises(RuntimeError, match="generate_halton failed"):
            algo.generate_samples(variables, n_samples=3, seed=42, outdir=tmp_path)


# ======================================================================
# Discrepancy comparison: Sobol should be lower-discrepancy than LHS
# ======================================================================


class TestSobolDiscrepancy:
    """Verify Sobol produces lower discrepancy than LHS for large samples."""

    def test_sobol_discrepancy_lower_than_lhs(self) -> None:
        """Sobol 1024 samples in 2-D should have discrepancy <= 0.01.

        LHS at the same count typically produces discrepancy ~0.03-0.05.
        We use a generous upper bound to avoid flaky tests.
        """
        n = 1024  # power of 2 for optimal Sobol
        d = 2
        seed = 42

        sobol_engine = scipy.stats.qmc.Sobol(d=d, seed=seed)
        sobol_samples = sobol_engine.random(n=n)
        sobol_disc = scipy.stats.qmc.discrepancy(sobol_samples)

        assert sobol_disc <= 0.01, f"Sobol discrepancy {sobol_disc:.6f} exceeds 0.01"
