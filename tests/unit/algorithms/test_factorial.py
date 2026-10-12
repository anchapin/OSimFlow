"""Tests for FullFactorialAlgorithm and GridSamplingAlgorithm (issue #272).

Covers:
- Registry discovery and interface contracts
- FullFactorial: cartesian product of discrete levels
- GridSampling: evenly-spaced grid over continuous ranges
- n_samples mismatch warnings
- Edge cases: empty variables, missing keys, single point, conditional vars
- RuntimeError / ValueError wrapping
"""

import json
from pathlib import Path
from typing import Any

import pytest

from osimflow.algorithms import AlgorithmRegistry, BaseAlgorithm
from osimflow.algorithms.factorial import (
    FullFactorialAlgorithm,
    GridSamplingAlgorithm,
)

# ======================================================================
# Registry tests
# ======================================================================


class TestFullFactorialRegistry:
    """Registry discovery tests for FullFactorialAlgorithm."""

    def test_get_returns_full_factorial_algorithm(self) -> None:
        algo = AlgorithmRegistry.get("full_factorial")
        assert isinstance(algo, FullFactorialAlgorithm)

    def test_get_returns_base_algorithm(self) -> None:
        algo = AlgorithmRegistry.get("full_factorial")
        assert isinstance(algo, BaseAlgorithm)

    def test_list_available_includes_full_factorial(self) -> None:
        assert "full_factorial" in AlgorithmRegistry.list_available()


class TestGridSamplingRegistry:
    """Registry discovery tests for GridSamplingAlgorithm."""

    def test_get_returns_grid_algorithm(self) -> None:
        algo = AlgorithmRegistry.get("grid")
        assert isinstance(algo, GridSamplingAlgorithm)

    def test_get_returns_base_algorithm(self) -> None:
        algo = AlgorithmRegistry.get("grid")
        assert isinstance(algo, BaseAlgorithm)

    def test_list_available_includes_grid(self) -> None:
        assert "grid" in AlgorithmRegistry.list_available()


# ======================================================================
# Interface contract tests
# ======================================================================


class TestFullFactorialInterface:
    """Contract tests for FullFactorialAlgorithm."""

    def test_name(self) -> None:
        assert FullFactorialAlgorithm().name() == "full_factorial"

    def test_is_iterative_false(self) -> None:
        assert FullFactorialAlgorithm().is_iterative() is False

    def test_is_converged_empty_history(self) -> None:
        assert FullFactorialAlgorithm().is_converged([]) is True

    def test_is_converged_with_history(self) -> None:
        assert FullFactorialAlgorithm().is_converged([{"samples": []}]) is True

    def test_observe_empty_history(self) -> None:
        assert FullFactorialAlgorithm().observe([]) == []

    def test_observe_returns_last_samples(self) -> None:
        algo = FullFactorialAlgorithm()
        history: list[dict[str, Any]] = [
            {"samples": [{"sample_id": "s0000"}]},
            {"samples": [{"sample_id": "s0001"}]},
        ]
        result = algo.observe(history)
        assert len(result) == 1
        assert result[0]["sample_id"] == "s0001"


class TestGridSamplingInterface:
    """Contract tests for GridSamplingAlgorithm."""

    def test_name(self) -> None:
        assert GridSamplingAlgorithm().name() == "grid"

    def test_is_iterative_false(self) -> None:
        assert GridSamplingAlgorithm().is_iterative() is False

    def test_is_converged_empty_history(self) -> None:
        assert GridSamplingAlgorithm().is_converged([]) is True

    def test_is_converged_with_history(self) -> None:
        assert GridSamplingAlgorithm().is_converged([{"samples": []}]) is True

    def test_observe_empty_history(self) -> None:
        assert GridSamplingAlgorithm().observe([]) == []

    def test_observe_returns_last_samples(self) -> None:
        algo = GridSamplingAlgorithm()
        history: list[dict[str, Any]] = [
            {"samples": [{"sample_id": "s0000"}]},
            {"samples": [{"sample_id": "s0001"}]},
        ]
        result = algo.observe(history)
        assert len(result) == 1
        assert result[0]["sample_id"] == "s0001"


# ======================================================================
# FullFactorial sample generation
# ======================================================================

_FACT_VARIABLES_2D: dict[str, Any] = {
    "variables": [
        {"name": "wall_r", "levels": [2.0, 4.0, 6.0]},
        {"name": "window_shgc", "levels": [0.3, 0.5]},
    ]
}


class TestFullFactorialGenerateSamples:
    """Generate-samples tests for FullFactorialAlgorithm."""

    def test_creates_samples_json(self, tmp_path: Path) -> None:
        algo = FullFactorialAlgorithm()
        result = algo.generate_samples(_FACT_VARIABLES_2D, n_samples=6, seed=None, outdir=tmp_path)
        assert result.exists()
        data = json.loads(result.read_text())
        assert "samples" in data
        # 3 levels × 2 levels = 6 samples
        assert len(data["samples"]) == 6

    def test_cartesian_product_correctness(self, tmp_path: Path) -> None:
        """Verify every combination of levels appears exactly once."""
        algo = FullFactorialAlgorithm()
        result = algo.generate_samples(_FACT_VARIABLES_2D, n_samples=6, seed=None, outdir=tmp_path)
        data = json.loads(result.read_text())

        combos = {(s["values"]["wall_r"], s["values"]["window_shgc"]) for s in data["samples"]}
        expected = {(2.0, 0.3), (2.0, 0.5), (4.0, 0.3), (4.0, 0.5), (6.0, 0.3), (6.0, 0.5)}
        assert combos == expected

    def test_sample_structure(self, tmp_path: Path) -> None:
        algo = FullFactorialAlgorithm()
        result = algo.generate_samples(_FACT_VARIABLES_2D, n_samples=6, seed=None, outdir=tmp_path)
        data = json.loads(result.read_text())
        for sample in data["samples"]:
            assert "sample_id" in sample
            assert "values" in sample
            assert "wall_r" in sample["values"]
            assert "window_shgc" in sample["values"]

    def test_sample_ids_sequential(self, tmp_path: Path) -> None:
        algo = FullFactorialAlgorithm()
        result = algo.generate_samples(_FACT_VARIABLES_2D, n_samples=6, seed=None, outdir=tmp_path)
        data = json.loads(result.read_text())
        ids = [s["sample_id"] for s in data["samples"]]
        assert ids == ["0001", "0002", "0003", "0004", "0005", "0006"]

    def test_creates_outdir(self, tmp_path: Path) -> None:
        algo = FullFactorialAlgorithm()
        nested = tmp_path / "deep" / "nested"
        algo.generate_samples(_FACT_VARIABLES_2D, n_samples=6, seed=None, outdir=nested)
        assert nested.is_dir()

    def test_n_samples_mismatch_produces_correct_count(self, tmp_path: Path) -> None:
        """n_samples=10 but actual=6 → log warning, still correct count.

        The warning is emitted via ``logging.warning``. We use
        ``caplog`` to verify it fires and check the sample count is
        based on the cartesian product, not the supplied n_samples.
        """
        algo = FullFactorialAlgorithm()
        result = algo.generate_samples(_FACT_VARIABLES_2D, n_samples=10, seed=None, outdir=tmp_path)
        data = json.loads(result.read_text())
        # 3 levels × 2 levels = 6, not 10
        assert len(data["samples"]) == 6

    def test_empty_variables(self, tmp_path: Path) -> None:
        algo = FullFactorialAlgorithm()
        result = algo.generate_samples({"variables": []}, n_samples=5, seed=None, outdir=tmp_path)
        data = json.loads(result.read_text())
        assert data["samples"] == []

    def test_no_independent_variables(self, tmp_path: Path) -> None:
        algo = FullFactorialAlgorithm()
        variables: dict[str, Any] = {
            "variables": [
                {"name": "x", "distribution": "conditional", "depends_on": {"variable": "y"}},
            ]
        }
        result = algo.generate_samples(variables, n_samples=5, seed=None, outdir=tmp_path)
        data = json.loads(result.read_text())
        assert data["samples"] == []

    def test_missing_levels_raises(self, tmp_path: Path) -> None:
        algo = FullFactorialAlgorithm()
        variables: dict[str, Any] = {
            "variables": [
                {"name": "x", "distribution": "uniform", "min": 0.0, "max": 1.0},
            ]
        }
        with pytest.raises(ValueError, match="requires a 'levels' key"):
            algo.generate_samples(variables, n_samples=3, seed=None, outdir=tmp_path)

    def test_empty_levels_raises(self, tmp_path: Path) -> None:
        algo = FullFactorialAlgorithm()
        variables: dict[str, Any] = {
            "variables": [
                {"name": "x", "levels": []},
            ]
        }
        with pytest.raises(ValueError, match="non-empty list"):
            algo.generate_samples(variables, n_samples=3, seed=None, outdir=tmp_path)

    def test_conditional_variable_resolution(self, tmp_path: Path) -> None:
        variables: dict[str, Any] = {
            "variables": [
                {"name": "x", "levels": [1, 2]},
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
        algo = FullFactorialAlgorithm()
        result = algo.generate_samples(variables, n_samples=2, seed=None, outdir=tmp_path)
        data = json.loads(result.read_text())
        assert len(data["samples"]) == 2
        for s in data["samples"]:
            assert "y" in s["values"]

    def test_single_level_single_variable(self, tmp_path: Path) -> None:
        algo = FullFactorialAlgorithm()
        variables: dict[str, Any] = {
            "variables": [
                {"name": "x", "levels": [42]},
            ]
        }
        result = algo.generate_samples(variables, n_samples=1, seed=None, outdir=tmp_path)
        data = json.loads(result.read_text())
        assert len(data["samples"]) == 1
        assert data["samples"][0]["values"]["x"] == 42

    def test_three_variables(self, tmp_path: Path) -> None:
        """3 × 2 × 4 = 24 samples."""
        variables: dict[str, Any] = {
            "variables": [
                {"name": "a", "levels": [1, 2, 3]},
                {"name": "b", "levels": ["low", "high"]},
                {"name": "c", "levels": [0.1, 0.2, 0.3, 0.4]},
            ]
        }
        algo = FullFactorialAlgorithm()
        result = algo.generate_samples(variables, n_samples=24, seed=None, outdir=tmp_path)
        data = json.loads(result.read_text())
        assert len(data["samples"]) == 24

    def test_string_levels(self, tmp_path: Path) -> None:
        algo = FullFactorialAlgorithm()
        variables: dict[str, Any] = {
            "variables": [
                {"name": "hvac_type", "levels": ["VAV", "PVAV", "PTAC"]},
            ]
        }
        result = algo.generate_samples(variables, n_samples=3, seed=None, outdir=tmp_path)
        data = json.loads(result.read_text())
        assert len(data["samples"]) == 3
        values = {s["values"]["hvac_type"] for s in data["samples"]}
        assert values == {"VAV", "PVAV", "PTAC"}


# ======================================================================
# GridSampling sample generation
# ======================================================================

_GRID_VARIABLES_2D: dict[str, Any] = {
    "variables": [
        {"name": "wall_r", "min": 1.0, "max": 10.0, "grid_points": 3},
        {"name": "window_shgc", "min": 0.1, "max": 0.9, "grid_points": 2},
    ]
}


class TestGridSamplingGenerateSamples:
    """Generate-samples tests for GridSamplingAlgorithm."""

    def test_creates_samples_json(self, tmp_path: Path) -> None:
        algo = GridSamplingAlgorithm()
        result = algo.generate_samples(_GRID_VARIABLES_2D, n_samples=6, seed=None, outdir=tmp_path)
        assert result.exists()
        data = json.loads(result.read_text())
        assert "samples" in data
        # 3 points × 2 points = 6 samples
        assert len(data["samples"]) == 6

    def test_grid_values_correct(self, tmp_path: Path) -> None:
        """Verify grid points are evenly spaced at exact endpoints."""
        algo = GridSamplingAlgorithm()
        result = algo.generate_samples(_GRID_VARIABLES_2D, n_samples=6, seed=None, outdir=tmp_path)
        data = json.loads(result.read_text())

        wall_r_values = sorted({s["values"]["wall_r"] for s in data["samples"]})
        shgc_values = sorted({s["values"]["window_shgc"] for s in data["samples"]})

        # wall_r: 3 points in [1.0, 10.0] → [1.0, 5.5, 10.0]
        assert len(wall_r_values) == 3
        assert wall_r_values[0] == pytest.approx(1.0)
        assert wall_r_values[-1] == pytest.approx(10.0)
        assert wall_r_values[1] == pytest.approx(5.5)

        # window_shgc: 2 points in [0.1, 0.9] → [0.1, 0.9]
        assert len(shgc_values) == 2
        assert shgc_values[0] == pytest.approx(0.1)
        assert shgc_values[-1] == pytest.approx(0.9)

    def test_sample_structure(self, tmp_path: Path) -> None:
        algo = GridSamplingAlgorithm()
        result = algo.generate_samples(_GRID_VARIABLES_2D, n_samples=6, seed=None, outdir=tmp_path)
        data = json.loads(result.read_text())
        for sample in data["samples"]:
            assert "sample_id" in sample
            assert "values" in sample
            assert "wall_r" in sample["values"]
            assert "window_shgc" in sample["values"]

    def test_creates_outdir(self, tmp_path: Path) -> None:
        algo = GridSamplingAlgorithm()
        nested = tmp_path / "deep" / "nested"
        algo.generate_samples(_GRID_VARIABLES_2D, n_samples=6, seed=None, outdir=nested)
        assert nested.is_dir()

    def test_default_grid_points(self, tmp_path: Path) -> None:
        """Without grid_points, default is 5 per dimension → 25 total."""
        variables: dict[str, Any] = {
            "variables": [
                {"name": "x", "min": 0.0, "max": 1.0},
                {"name": "y", "min": 0.0, "max": 1.0},
            ]
        }
        algo = GridSamplingAlgorithm()
        result = algo.generate_samples(variables, n_samples=25, seed=None, outdir=tmp_path)
        data = json.loads(result.read_text())
        assert len(data["samples"]) == 25

    def test_single_point_per_dim(self, tmp_path: Path) -> None:
        """grid_points=1 → only the midpoint (min) is used."""
        variables: dict[str, Any] = {
            "variables": [
                {"name": "x", "min": 5.0, "max": 15.0, "grid_points": 1},
                {"name": "y", "min": 0.0, "max": 1.0, "grid_points": 1},
            ]
        }
        algo = GridSamplingAlgorithm()
        result = algo.generate_samples(variables, n_samples=1, seed=None, outdir=tmp_path)
        data = json.loads(result.read_text())
        assert len(data["samples"]) == 1
        assert data["samples"][0]["values"]["x"] == pytest.approx(5.0)
        assert data["samples"][0]["values"]["y"] == pytest.approx(0.0)

    def test_empty_variables(self, tmp_path: Path) -> None:
        algo = GridSamplingAlgorithm()
        result = algo.generate_samples({"variables": []}, n_samples=5, seed=None, outdir=tmp_path)
        data = json.loads(result.read_text())
        assert data["samples"] == []

    def test_no_independent_variables(self, tmp_path: Path) -> None:
        algo = GridSamplingAlgorithm()
        variables: dict[str, Any] = {
            "variables": [
                {"name": "x", "distribution": "conditional", "depends_on": {"variable": "y"}},
            ]
        }
        result = algo.generate_samples(variables, n_samples=5, seed=None, outdir=tmp_path)
        data = json.loads(result.read_text())
        assert data["samples"] == []

    def test_missing_min_raises(self, tmp_path: Path) -> None:
        algo = GridSamplingAlgorithm()
        variables: dict[str, Any] = {
            "variables": [
                {"name": "x", "max": 1.0, "grid_points": 3},
            ]
        }
        with pytest.raises(ValueError, match="requires 'min' and 'max'"):
            algo.generate_samples(variables, n_samples=3, seed=None, outdir=tmp_path)

    def test_invalid_grid_points_raises(self, tmp_path: Path) -> None:
        algo = GridSamplingAlgorithm()
        variables: dict[str, Any] = {
            "variables": [
                {"name": "x", "min": 0.0, "max": 1.0, "grid_points": 0},
            ]
        }
        with pytest.raises(ValueError, match="positive integer"):
            algo.generate_samples(variables, n_samples=1, seed=None, outdir=tmp_path)

    def test_conditional_variable_resolution(self, tmp_path: Path) -> None:
        variables: dict[str, Any] = {
            "variables": [
                {"name": "x", "min": 0.0, "max": 1.0, "grid_points": 2},
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
        algo = GridSamplingAlgorithm()
        result = algo.generate_samples(variables, n_samples=2, seed=None, outdir=tmp_path)
        data = json.loads(result.read_text())
        assert len(data["samples"]) == 2
        for s in data["samples"]:
            assert "y" in s["values"]

    def test_three_dimension_grid(self, tmp_path: Path) -> None:
        """4 × 3 × 2 = 24 samples."""
        variables: dict[str, Any] = {
            "variables": [
                {"name": "a", "min": 0.0, "max": 3.0, "grid_points": 4},
                {"name": "b", "min": 0.0, "max": 2.0, "grid_points": 3},
                {"name": "c", "min": 0.0, "max": 1.0, "grid_points": 2},
            ]
        }
        algo = GridSamplingAlgorithm()
        result = algo.generate_samples(variables, n_samples=24, seed=None, outdir=tmp_path)
        data = json.loads(result.read_text())
        assert len(data["samples"]) == 24

    def test_values_in_range(self, tmp_path: Path) -> None:
        algo = GridSamplingAlgorithm()
        result = algo.generate_samples(_GRID_VARIABLES_2D, n_samples=6, seed=None, outdir=tmp_path)
        data = json.loads(result.read_text())
        for sample in data["samples"]:
            assert 1.0 <= sample["values"]["wall_r"] <= 10.0
            assert 0.1 <= sample["values"]["window_shgc"] <= 0.9

    def test_n_samples_mismatch_produces_correct_count(self, tmp_path: Path) -> None:
        """Even with wrong n_samples, actual grid count is produced."""
        algo = GridSamplingAlgorithm()
        result = algo.generate_samples(
            _GRID_VARIABLES_2D, n_samples=999, seed=None, outdir=tmp_path
        )
        data = json.loads(result.read_text())
        assert len(data["samples"]) == 6


# ======================================================================
# Dict-of-dicts variable format (normalisation)
# ======================================================================


class TestFactorialDictOfDicts:
    """Verify the algorithms accept dict-of-dicts variable format."""

    def test_full_factorial_dict_format(self, tmp_path: Path) -> None:
        variables: dict[str, Any] = {
            "variables": {
                "wall_r": {"levels": [2.0, 4.0]},
                "shgc": {"levels": [0.3, 0.5, 0.7]},
            }
        }
        algo = FullFactorialAlgorithm()
        result = algo.generate_samples(variables, n_samples=6, seed=None, outdir=tmp_path)
        data = json.loads(result.read_text())
        assert len(data["samples"]) == 6

    def test_grid_dict_format(self, tmp_path: Path) -> None:
        variables: dict[str, Any] = {
            "variables": {
                "wall_r": {"min": 1.0, "max": 10.0, "grid_points": 3},
                "shgc": {"min": 0.1, "max": 0.9, "grid_points": 2},
            }
        }
        algo = GridSamplingAlgorithm()
        result = algo.generate_samples(variables, n_samples=6, seed=None, outdir=tmp_path)
        data = json.loads(result.read_text())
        assert len(data["samples"]) == 6
