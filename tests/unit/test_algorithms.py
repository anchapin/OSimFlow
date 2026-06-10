"""Tests for the algorithm plug-in framework (issue #121).

Covers:
- AlgorithmRegistry.get("lhs") returns a LHSAlgorithm instance
- AlgorithmRegistry.get("unknown") raises ValueError with helpful message
- LHSAlgorithm.is_iterative() returns False
- LHSAlgorithm.is_converged([]) returns True
- LHSAlgorithm.name() returns "lhs"
- AlgorithmRegistry.list_available() includes "lhs"
"""

import json
from pathlib import Path

import pytest

from osimflow.algorithms import AlgorithmRegistry, BaseAlgorithm, LHSAlgorithm


class TestAlgorithmRegistry:
    """Tests for AlgorithmRegistry discovery and instantiation."""

    def test_get_lhs_returns_lhs_algorithm(self) -> None:
        algo = AlgorithmRegistry.get("lhs")
        assert isinstance(algo, LHSAlgorithm)

    def test_get_lhs_returns_base_algorithm(self) -> None:
        algo = AlgorithmRegistry.get("lhs")
        assert isinstance(algo, BaseAlgorithm)

    def test_get_unknown_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="unknown algorithm 'sobol_not_yet'"):
            AlgorithmRegistry.get("sobol_not_yet")

    def test_get_unknown_error_lists_available(self) -> None:
        with pytest.raises(ValueError, match="Available algorithms:.*lhs"):
            AlgorithmRegistry.get("nonexistent")

    def test_list_available_includes_lhs(self) -> None:
        available = AlgorithmRegistry.list_available()
        assert "lhs" in available

    def test_list_available_returns_sorted(self) -> None:
        available = AlgorithmRegistry.list_available()
        assert available == sorted(available)

    def test_register_and_retrieve_custom(self) -> None:
        """Register a minimal custom algorithm and verify retrieval."""

        class StubAlgo(BaseAlgorithm):
            def generate_samples(
                self, variables: dict, n_samples: int, seed: int | None, outdir: Path
            ) -> Path:
                return Path("stub")

            def observe(self, history: list[dict]) -> list[dict]:
                return []

            def is_converged(self, history: list[dict]) -> bool:
                return True

            def name(self) -> str:
                return "stub"

            def is_iterative(self) -> bool:
                return False

        AlgorithmRegistry.register("stub_test", StubAlgo)
        try:
            algo = AlgorithmRegistry.get("stub_test")
            assert isinstance(algo, StubAlgo)
            assert algo.name() == "stub"
        finally:
            # Clean up so other tests are not affected.
            AlgorithmRegistry._registry.pop("stub_test", None)


class TestLHSAlgorithm:
    """Tests for the built-in LHSAlgorithm."""

    def test_name(self) -> None:
        algo = LHSAlgorithm()
        assert algo.name() == "lhs"

    def test_is_iterative_false(self) -> None:
        algo = LHSAlgorithm()
        assert algo.is_iterative() is False

    def test_is_converged_empty_history(self) -> None:
        algo = LHSAlgorithm()
        assert algo.is_converged([]) is True

    def test_is_converged_with_history(self) -> None:
        algo = LHSAlgorithm()
        assert algo.is_converged([{"samples": []}, {"samples": []}]) is True

    def test_observe_empty_history(self) -> None:
        algo = LHSAlgorithm()
        assert algo.observe([]) == []

    def test_observe_returns_last_samples(self) -> None:
        algo = LHSAlgorithm()
        history = [
            {"samples": [{"sample_id": "s0000"}]},
            {"samples": [{"sample_id": "s0001"}]},
        ]
        result = algo.observe(history)
        assert len(result) == 1
        assert result[0]["sample_id"] == "s0001"

    def test_generate_samples_creates_file(self, tmp_path: Path) -> None:
        algo = LHSAlgorithm()
        variables: dict[str, object] = {
            "variables": [
                {
                    "name": "wall_r",
                    "distribution": "uniform",
                    "min": 1.0,
                    "max": 10.0,
                },
                {
                    "name": "window_shgc",
                    "distribution": "uniform",
                    "min": 0.1,
                    "max": 0.9,
                },
            ]
        }
        result = algo.generate_samples(variables, n_samples=5, seed=42, outdir=tmp_path)
        assert result.exists()
        data = json.loads(result.read_text())
        assert "samples" in data
        assert len(data["samples"]) == 5
        for sample in data["samples"]:
            assert "sample_id" in sample
            assert "values" in sample
            assert "wall_r" in sample["values"]
            assert "window_shgc" in sample["values"]

    def test_generate_samples_creates_outdir(self, tmp_path: Path) -> None:
        algo = LHSAlgorithm()
        nested = tmp_path / "deep" / "nested"
        variables: dict[str, object] = {
            "variables": [
                {"name": "x", "distribution": "uniform", "min": 0.0, "max": 1.0},
            ]
        }
        result = algo.generate_samples(variables, n_samples=2, seed=0, outdir=nested)
        assert result.exists()
        assert nested.is_dir()

    def test_generate_samples_with_seed_reproducible(self, tmp_path: Path) -> None:
        algo = LHSAlgorithm()
        variables: dict[str, object] = {
            "variables": [
                {"name": "x", "distribution": "uniform", "min": 0.0, "max": 1.0},
            ]
        }
        r1 = algo.generate_samples(variables, n_samples=10, seed=123, outdir=tmp_path / "run1")
        r2 = algo.generate_samples(variables, n_samples=10, seed=123, outdir=tmp_path / "run2")
        d1 = json.loads(r1.read_text())
        d2 = json.loads(r2.read_text())
        assert d1 == d2
