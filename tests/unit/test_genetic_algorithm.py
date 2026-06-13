"""Tests for GeneticAlgorithm (issue #345, ALGO-001).

Covers:
- AlgorithmRegistry.get("ga") returns GeneticAlgorithm (when deap is installed)
- is_iterative() returns True
- Registration in AlgorithmRegistry.list_available()
- Bounds extraction from variables
- generate_samples produces valid samples.json
- observe() returns new samples given history
- is_converged() works correctly
- GA converges on a 2D quadratic objective within 10 generations
"""

import json
from pathlib import Path
from typing import Any

import pytest

from osimflow.algorithms import AlgorithmRegistry, BaseAlgorithm

# ======================================================================
# Helpers / fixtures
# ======================================================================


def _ga_not_available() -> bool:
    try:
        from osimflow.algorithms.ga import GeneticAlgorithm  # noqa: F401

        return False
    except ImportError:
        return True


# Only run if DEAP is installed.
pytestmark = pytest.mark.skipif(
    _ga_not_available(),
    reason="deap not installed — run: pip install deap",
)


def _get_ga_class():
    """Import and return GeneticAlgorithm class (assumes deap is installed)."""
    from osimflow.algorithms.ga import GeneticAlgorithm

    return GeneticAlgorithm


_VARIABLES_2D: dict[str, Any] = {
    "variables": [
        {"name": "wall_r", "distribution": "uniform", "min": 1.0, "max": 10.0},
        {"name": "window_shgc", "distribution": "uniform", "min": 0.1, "max": 0.9},
    ]
}

_VARIABLES_NORMAL: dict[str, Any] = {
    "variables": [
        {"name": "x", "distribution": "normal", "mean": 5.0, "sigma": 1.0},
    ]
}


def _make_history_with_kpis(
    tmp_path: Path,
    samples: list[dict[str, Any]],
    kpi_values: list[float],
    generation: int = 0,
) -> dict[str, Any]:
    """Create a history entry with KPI files on disk."""
    kpi_files: list[str] = []
    for _i, (sample, kpi_val) in enumerate(zip(samples, kpi_values, strict=True)):
        kpi_dir = tmp_path / f"kpi_gen{generation}" / sample["sample_id"]
        kpi_dir.mkdir(parents=True, exist_ok=True)
        kpi_path = kpi_dir / f"kpi_{sample['sample_id']}.json"
        kpi_path.write_text(
            json.dumps({"sample_id": sample["sample_id"], "kpis": {"eui": kpi_val}})
        )
        kpi_files.append(str(kpi_path))

    return {
        "generation": generation,
        "samples": samples,
        "kpi_files": kpi_files,
    }


# ======================================================================
# Registry tests
# ======================================================================


class TestGARegistry:
    """Registry discovery tests for GeneticAlgorithm."""

    def test_get_ga_returns_ga_algorithm(self) -> None:
        algo = AlgorithmRegistry.get("ga")
        assert isinstance(algo, _get_ga_class())

    def test_get_ga_returns_base_algorithm(self) -> None:
        algo = AlgorithmRegistry.get("ga")
        assert isinstance(algo, BaseAlgorithm)

    def test_list_available_includes_ga(self) -> None:
        assert "ga" in AlgorithmRegistry.list_available()


# ======================================================================
# Interface tests
# ======================================================================


class TestGAInterface:
    """Contract tests for GeneticAlgorithm."""

    def test_name(self) -> None:
        assert _get_ga_class()().name() == "ga"

    def test_is_iterative_true(self) -> None:
        assert _get_ga_class()().is_iterative() is True

    def test_is_converged_empty_history(self) -> None:
        assert _get_ga_class()().is_converged([]) is False

    def test_is_converged_single_generation(self) -> None:
        algo = _get_ga_class()()
        assert algo.is_converged([{"generation": 0, "samples": []}]) is False

    def test_observe_empty_history(self) -> None:
        algo = _get_ga_class()()
        assert algo.observe([]) == []

    def test_popsize_must_be_at_least_2(self) -> None:
        with pytest.raises(ValueError, match="popsize must be >= 2"):
            _get_ga_class()(popsize=1)


# ======================================================================
# Bounds extraction tests
# ======================================================================


class TestGABoundsExtraction:
    """Tests for _extract_bounds (GA module)."""

    def test_uniform_bounds(self) -> None:
        from osimflow.algorithms.ga import _extract_bounds

        var_list = _VARIABLES_2D["variables"]
        bounds = _extract_bounds(var_list)
        assert bounds == [(1.0, 10.0), (0.1, 0.9)]

    def test_normal_bounds_3sigma(self) -> None:
        from osimflow.algorithms.ga import _extract_bounds

        var_list = _VARIABLES_NORMAL["variables"]
        bounds = _extract_bounds(var_list)
        assert len(bounds) == 1
        assert bounds[0] == (2.0, 8.0)  # 5 ± 3*1

    def test_empty_vars(self) -> None:
        from osimflow.algorithms.ga import _extract_bounds

        assert _extract_bounds([]) == []

    def test_lognormal_bounds(self) -> None:
        from osimflow.algorithms.ga import _extract_bounds

        var_list = [{"name": "x", "distribution": "lognormal", "mean": 2.0, "sigma": 0.5}]
        bounds = _extract_bounds(var_list)
        assert len(bounds) == 1
        lo, hi = bounds[0]
        assert lo > 0
        assert hi > lo

    def test_triangular_bounds(self) -> None:
        from osimflow.algorithms.ga import _extract_bounds

        var_list = [
            {"name": "x", "distribution": "triangular", "min": 1.0, "max": 5.0, "mode": 3.0}
        ]
        bounds = _extract_bounds(var_list)
        assert bounds == [(1.0, 5.0)]

    def test_unknown_distribution_fallback(self) -> None:
        from osimflow.algorithms.ga import _extract_bounds

        var_list = [{"name": "x", "distribution": "beta", "alpha": 2.0, "beta": 5.0}]
        bounds = _extract_bounds(var_list)
        assert bounds == [(0.0, 1.0)]


# ======================================================================
# Sample generation tests
# ======================================================================


class TestGAGenerateSamples:
    """Generate-samples tests for GeneticAlgorithm."""

    def test_creates_samples_json(self, tmp_path: Path) -> None:
        algo = _get_ga_class()()
        result = algo.generate_samples(_VARIABLES_2D, n_samples=10, seed=42, outdir=tmp_path)
        assert result.exists()
        data = json.loads(result.read_text())
        assert "samples" in data
        assert len(data["samples"]) == 10

    def test_sample_structure(self, tmp_path: Path) -> None:
        algo = _get_ga_class()()
        result = algo.generate_samples(_VARIABLES_2D, n_samples=5, seed=0, outdir=tmp_path)
        data = json.loads(result.read_text())
        for sample in data["samples"]:
            assert "sample_id" in sample
            assert "values" in sample
            assert "wall_r" in sample["values"]
            assert "window_shgc" in sample["values"]

    def test_sample_values_within_bounds(self, tmp_path: Path) -> None:
        algo = _get_ga_class()()
        result = algo.generate_samples(_VARIABLES_2D, n_samples=20, seed=42, outdir=tmp_path)
        data = json.loads(result.read_text())
        for sample in data["samples"]:
            assert 1.0 <= sample["values"]["wall_r"] <= 10.0
            assert 0.1 <= sample["values"]["window_shgc"] <= 0.9

    def test_creates_outdir(self, tmp_path: Path) -> None:
        algo = _get_ga_class()()
        nested = tmp_path / "deep" / "nested"
        algo.generate_samples(_VARIABLES_2D, n_samples=2, seed=0, outdir=nested)
        assert nested.is_dir()

    def test_empty_variables(self, tmp_path: Path) -> None:
        algo = _get_ga_class()()
        result = algo.generate_samples(
            {"variables": []}, n_samples=5, seed=None, outdir=tmp_path
        )
        data = json.loads(result.read_text())
        assert data["samples"] == []

    def test_seed_reproducible(self, tmp_path: Path) -> None:
        algo = _get_ga_class()()
        r1 = algo.generate_samples(
            _VARIABLES_2D, n_samples=10, seed=123, outdir=tmp_path / "r1"
        )
        r2 = algo.generate_samples(
            _VARIABLES_2D, n_samples=10, seed=123, outdir=tmp_path / "r2"
        )
        assert json.loads(r1.read_text()) == json.loads(r2.read_text())

    def test_no_independent_vars(self, tmp_path: Path) -> None:
        algo = _get_ga_class()()
        variables: dict[str, Any] = {
            "variables": [
                {"name": "x", "distribution": "conditional", "depends_on": {"variable": "y"}},
            ]
        }
        result = algo.generate_samples(variables, n_samples=5, seed=42, outdir=tmp_path)
        data = json.loads(result.read_text())
        assert data["samples"] == []


# ======================================================================
# Observe tests
# ======================================================================


class TestGAObserve:
    """Tests for GeneticAlgorithm.observe."""

    def test_observe_returns_samples(self, tmp_path: Path) -> None:
        algo = _get_ga_class()()
        # First generate initial samples.
        samples_path = algo.generate_samples(
            _VARIABLES_2D, n_samples=5, seed=42, outdir=tmp_path / "gen0"
        )
        samples = json.loads(samples_path.read_text())["samples"]

        # Create history with KPI files.
        kpi_values = [100.0 + i * 10.0 for i in range(len(samples))]
        history_entry = _make_history_with_kpis(tmp_path, samples, kpi_values, generation=0)

        new_samples = algo.observe([history_entry])
        assert len(new_samples) > 0
        for sample in new_samples:
            assert "sample_id" in sample
            assert "values" in sample

    def test_observe_no_vars_returns_empty(self) -> None:
        algo = _get_ga_class()()
        # No _independent_vars set yet.
        assert algo.observe([{"generation": 0, "samples": [], "kpi_files": []}]) == []

    def test_observe_no_kpi_values(self, tmp_path: Path) -> None:
        algo = _get_ga_class()()
        algo.generate_samples(_VARIABLES_2D, n_samples=5, seed=42, outdir=tmp_path / "gen0")
        history_entry: dict[str, Any] = {
            "generation": 0,
            "samples": [{"sample_id": "0001", "values": {"wall_r": 5.0}}],
            "kpi_files": [str(tmp_path / "nonexistent.json")],
        }
        result = algo.observe([history_entry])
        assert result == []


# ======================================================================
# Convergence test with mock KPI (2D quadratic)
# ======================================================================


class TestGAConvergence:
    """GA should converge on a 2D quadratic within 10 generations."""

    def test_converges_on_quadratic(self, tmp_path: Path) -> None:
        """Minimise f(x, y) = (x-5)^2 + (y-0.5)^2 over [1,10] x [0.1,0.9].

        The minimum is at (5, 0.5) with value 0.0.  GA should get close
        within 10 generations.
        """
        algo = _get_ga_class()(tol=0.05, popsize=20)
        samples_path = algo.generate_samples(
            _VARIABLES_2D, n_samples=20, seed=42, outdir=tmp_path / "gen0"
        )
        samples = json.loads(samples_path.read_text())["samples"]

        history: list[dict[str, Any]] = []
        for gen in range(10):
            # Evaluate mock KPI for each sample.
            kpi_values: list[float] = []
            for s in samples:
                x = s["values"]["wall_r"]
                y = s["values"]["window_shgc"]
                kpi = (x - 5.0) ** 2 + (y - 0.5) ** 2
                kpi_values.append(kpi)

            entry = _make_history_with_kpis(tmp_path, samples, kpi_values, generation=gen)
            history.append(entry)

            if algo.is_converged(history):
                break

            new_samples = algo.observe(history)
            if not new_samples:
                break
            samples = new_samples

        # Verify convergence happened and best is reasonable.
        assert algo._best_value < 1.0, (
            f"GA best_value {algo._best_value} should be < 1.0 after 10 gens"
        )
        # Best params should be near the optimum (5.0, 0.5).
        best_wall_r = float(algo._best_params[0])
        best_shgc = float(algo._best_params[1])
        assert abs(best_wall_r - 5.0) < 2.5, f"wall_r {best_wall_r} should be near 5.0"
        assert abs(best_shgc - 0.5) < 0.4, f"shgc {best_shgc} should be near 0.5"


# ======================================================================
# Read KPI values tests
# ======================================================================


class TestGAReadKPIValues:
    """Tests for _read_kpi_values helper (GA module)."""

    def test_reads_kpi_values(self, tmp_path: Path) -> None:
        from osimflow.algorithms.ga import _read_kpi_values

        samples = [
            {"sample_id": "0001", "values": {"wall_r": 5.0}},
            {"sample_id": "0002", "values": {"wall_r": 7.0}},
        ]
        entry = _make_history_with_kpis(tmp_path, samples, [100.0, 200.0])
        results = _read_kpi_values([entry], "eui")
        assert len(results) == 2
        assert results[0][1] == 100.0
        assert results[1][1] == 200.0

    def test_empty_history(self) -> None:
        from osimflow.algorithms.ga import _read_kpi_values

        assert _read_kpi_values([], "eui") == []

    def test_missing_kpi_file(self, tmp_path: Path) -> None:
        from osimflow.algorithms.ga import _read_kpi_values

        samples = [{"sample_id": "0001", "values": {"wall_r": 5.0}}]
        entry: dict[str, Any] = {
            "generation": 0,
            "samples": samples,
            "kpi_files": [str(tmp_path / "nonexistent.json")],
        }
        results = _read_kpi_values([entry], "eui")
        assert results == []

    def test_invalid_json(self, tmp_path: Path) -> None:
        from osimflow.algorithms.ga import _read_kpi_values

        samples = [{"sample_id": "0001", "values": {"wall_r": 5.0}}]
        kpi_dir = tmp_path / "kpi"
        kpi_dir.mkdir()
        kpi_path = kpi_dir / "kpi_0001.json"
        kpi_path.write_text("not valid json{{{")
        entry: dict[str, Any] = {
            "generation": 0,
            "samples": samples,
            "kpi_files": [str(kpi_path)],
        }
        results = _read_kpi_values([entry], "eui")
        assert results == []


# ======================================================================
# Maximize mode tests
# ======================================================================


class TestGAMaximize:
    """Tests for GA in maximize mode."""

    def test_maximize_mode(self) -> None:
        algo = _get_ga_class()(maximize=True)
        assert algo._maximize is True

    def test_observe_with_maximize(self, tmp_path: Path) -> None:
        algo = _get_ga_class()(maximize=True)
        samples_path = algo.generate_samples(
            _VARIABLES_2D, n_samples=5, seed=42, outdir=tmp_path / "gen0"
        )
        samples = json.loads(samples_path.read_text())["samples"]

        kpi_values = [100.0 - i * 10.0 for i in range(len(samples))]
        history_entry = _make_history_with_kpis(tmp_path, samples, kpi_values, generation=0)

        new_samples = algo.observe([history_entry])
        assert len(new_samples) > 0


# ======================================================================
# Convergence edge case tests
# ======================================================================


class TestGAConvergenceEdgeCases:
    """Edge case tests for GA convergence."""

    def test_converged_when_prev_best_zero(self) -> None:
        algo = _get_ga_class()()
        algo._prev_best = 0.0
        algo._best_value = 0.0
        assert algo.is_converged([{"gen": 0}, {"gen": 1}]) is True

    def test_not_converged_when_prev_best_zero_best_nonzero(self) -> None:
        algo = _get_ga_class()()
        algo._prev_best = 0.0
        algo._best_value = 1.0
        assert algo.is_converged([{"gen": 0}, {"gen": 1}]) is False

    def test_not_converged_with_inf_values(self) -> None:
        algo = _get_ga_class()()
        algo._prev_best = float("inf")
        algo._best_value = float("inf")
        assert algo.is_converged([{"gen": 0}, {"gen": 1}]) is False

    def test_observe_with_zero_new_samples(self, tmp_path: Path) -> None:
        algo = _get_ga_class()()
        algo._independent_vars = [{"name": "x", "distribution": "uniform", "min": 0.0, "max": 1.0}]
        algo._bounds = [(0.0, 1.0)]
        history_entry: dict[str, Any] = {
            "generation": 0,
            "samples": [],
            "kpi_files": [],
        }
        result = algo.observe([history_entry])
        assert result == []

    def test_empty_variables_returns_empty(self, tmp_path: Path) -> None:
        algo = _get_ga_class()()
        result = algo.generate_samples(
            {"variables": []}, n_samples=5, seed=None, outdir=tmp_path
        )
        data = json.loads(result.read_text())
        assert data["samples"] == []
