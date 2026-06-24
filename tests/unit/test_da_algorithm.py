"""Tests for DualAnnealingAlgorithm (issue #125, G2b).

Covers:
- AlgorithmRegistry.get("dual_annealing") returns DualAnnealingAlgorithm
- is_iterative() returns True
- Registration in AlgorithmRegistry.list_available()
- Bounds extraction from variables
- generate_samples produces valid samples.json
- observe() returns new samples given history
- is_converged() works correctly
- Dual annealing converges on a 2D quadratic objective within 10 generations
"""

import json
from pathlib import Path
from typing import Any

from osimflow.algorithms import AlgorithmRegistry, BaseAlgorithm
from osimflow.algorithms.da import (
    DualAnnealingAlgorithm,
    _extract_bounds,
    _read_kpi_values,
)

# ======================================================================
# Fixtures and helpers
# ======================================================================

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


class TestDARegistry:
    """Registry discovery tests for DualAnnealingAlgorithm."""

    def test_get_da_returns_da_algorithm(self) -> None:
        algo = AlgorithmRegistry.get("dual_annealing")
        assert isinstance(algo, DualAnnealingAlgorithm)

    def test_get_da_returns_base_algorithm(self) -> None:
        algo = AlgorithmRegistry.get("dual_annealing")
        assert isinstance(algo, BaseAlgorithm)

    def test_list_available_includes_da(self) -> None:
        assert "dual_annealing" in AlgorithmRegistry.list_available()


# ======================================================================
# Interface tests
# ======================================================================


class TestDAInterface:
    """Contract tests for DualAnnealingAlgorithm."""

    def test_name(self) -> None:
        assert DualAnnealingAlgorithm().name() == "dual_annealing"

    def test_is_iterative_true(self) -> None:
        assert DualAnnealingAlgorithm().is_iterative() is True

    def test_is_converged_empty_history(self) -> None:
        assert DualAnnealingAlgorithm().is_converged([]) is False

    def test_is_converged_single_generation(self) -> None:
        algo = DualAnnealingAlgorithm()
        assert algo.is_converged([{"generation": 0, "samples": []}]) is False

    def test_observe_empty_history(self) -> None:
        algo = DualAnnealingAlgorithm()
        assert algo.observe([]) == []


# ======================================================================
# Bounds extraction tests
# ======================================================================


class TestDAExtractBounds:
    """Tests for _extract_bounds (DA module)."""

    def test_uniform_bounds(self) -> None:
        var_list = _VARIABLES_2D["variables"]
        bounds = _extract_bounds(var_list)
        assert bounds == [(1.0, 10.0), (0.1, 0.9)]

    def test_normal_bounds_3sigma(self) -> None:
        var_list = _VARIABLES_NORMAL["variables"]
        bounds = _extract_bounds(var_list)
        assert len(bounds) == 1
        assert bounds[0] == (2.0, 8.0)  # 5 ± 3*1

    def test_empty_vars(self) -> None:
        assert _extract_bounds([]) == []


# ======================================================================
# Sample generation tests
# ======================================================================


class TestDAGenerateSamples:
    """Generate-samples tests for DualAnnealingAlgorithm."""

    def test_creates_samples_json(self, tmp_path: Path) -> None:
        algo = DualAnnealingAlgorithm()
        result = algo.generate_samples(_VARIABLES_2D, n_samples=10, seed=42, outdir=tmp_path)
        assert result.exists()
        data = json.loads(result.read_text())
        assert "samples" in data
        assert len(data["samples"]) == 10

    def test_sample_structure(self, tmp_path: Path) -> None:
        algo = DualAnnealingAlgorithm()
        result = algo.generate_samples(_VARIABLES_2D, n_samples=5, seed=0, outdir=tmp_path)
        data = json.loads(result.read_text())
        for sample in data["samples"]:
            assert "sample_id" in sample
            assert "values" in sample
            assert "wall_r" in sample["values"]
            assert "window_shgc" in sample["values"]

    def test_sample_values_within_bounds(self, tmp_path: Path) -> None:
        algo = DualAnnealingAlgorithm()
        result = algo.generate_samples(_VARIABLES_2D, n_samples=20, seed=42, outdir=tmp_path)
        data = json.loads(result.read_text())
        for sample in data["samples"]:
            assert 1.0 <= sample["values"]["wall_r"] <= 10.0
            assert 0.1 <= sample["values"]["window_shgc"] <= 0.9

    def test_creates_outdir(self, tmp_path: Path) -> None:
        algo = DualAnnealingAlgorithm()
        nested = tmp_path / "deep" / "nested"
        algo.generate_samples(_VARIABLES_2D, n_samples=2, seed=0, outdir=nested)
        assert nested.is_dir()

    def test_empty_variables(self, tmp_path: Path) -> None:
        algo = DualAnnealingAlgorithm()
        result = algo.generate_samples({"variables": []}, n_samples=5, seed=None, outdir=tmp_path)
        data = json.loads(result.read_text())
        assert data["samples"] == []

    def test_seed_reproducible(self, tmp_path: Path) -> None:
        algo = DualAnnealingAlgorithm()
        r1 = algo.generate_samples(_VARIABLES_2D, n_samples=10, seed=123, outdir=tmp_path / "r1")
        r2 = algo.generate_samples(_VARIABLES_2D, n_samples=10, seed=123, outdir=tmp_path / "r2")
        assert json.loads(r1.read_text()) == json.loads(r2.read_text())


# ======================================================================
# Observe tests
# ======================================================================


class TestDAObserve:
    """Tests for DualAnnealingAlgorithm.observe."""

    def test_observe_returns_samples(self, tmp_path: Path) -> None:
        algo = DualAnnealingAlgorithm()
        samples_path = algo.generate_samples(
            _VARIABLES_2D, n_samples=5, seed=42, outdir=tmp_path / "gen0"
        )
        samples = json.loads(samples_path.read_text())["samples"]

        kpi_values = [100.0 + i * 10.0 for i in range(len(samples))]
        history_entry = _make_history_with_kpis(tmp_path, samples, kpi_values, generation=0)

        new_samples = algo.observe([history_entry])
        assert len(new_samples) > 0
        for sample in new_samples:
            assert "sample_id" in sample
            assert "values" in sample

    def test_observe_no_vars_returns_empty(self) -> None:
        algo = DualAnnealingAlgorithm()
        assert algo.observe([{"generation": 0, "samples": [], "kpi_files": []}]) == []


# ======================================================================
# Convergence test with mock KPI (2D quadratic)
# ======================================================================


class TestDAConvergence:
    """Dual annealing should converge on a 2D quadratic within 10 generations."""

    def test_converges_on_quadratic(self, tmp_path: Path) -> None:
        """Minimise f(x, y) = (x-5)^2 + (y-0.5)^2 over [1,10] x [0.1,0.9].

        The minimum is at (5, 0.5) with value 0.0.  Dual annealing should
        get close within 10 generations.
        """
        algo = DualAnnealingAlgorithm(tol=0.05, maxiter=50)
        samples_path = algo.generate_samples(
            _VARIABLES_2D, n_samples=10, seed=42, outdir=tmp_path / "gen0"
        )
        samples = json.loads(samples_path.read_text())["samples"]

        history: list[dict[str, Any]] = []
        for gen in range(10):
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

        # Verify convergence.
        assert algo._best_value < 1.0, (
            f"DA best_value {algo._best_value} should be < 1.0 after 10 gens"
        )
        best_wall_r = float(algo._best_params[0])
        best_shgc = float(algo._best_params[1])
        assert abs(best_wall_r - 5.0) < 2.0, f"wall_r {best_wall_r} should be near 5.0"
        assert abs(best_shgc - 0.5) < 0.3, f"shgc {best_shgc} should be near 0.5"


class TestDAReadKPIValues:
    """Tests for _read_kpi_values helper (DA module)."""

    def test_reads_kpi_values(self, tmp_path: Path) -> None:
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
        assert _read_kpi_values([], "eui") == []


class TestDAExtractBoundsMoreDistributions:
    """Additional bound extraction tests for DA."""

    def test_lognormal_bounds(self) -> None:
        var_list = [{"name": "x", "distribution": "lognormal", "mean": 2.0, "sigma": 0.5}]
        bounds = _extract_bounds(var_list)
        assert len(bounds) == 1
        lo, hi = bounds[0]
        assert lo > 0
        assert hi > lo

    def test_triangular_bounds(self) -> None:
        var_list = [
            {"name": "x", "distribution": "triangular", "min": 1.0, "max": 5.0, "mode": 3.0}
        ]
        bounds = _extract_bounds(var_list)
        assert bounds == [(1.0, 5.0)]

    def test_unknown_distribution_fallback(self) -> None:
        var_list = [{"name": "x", "distribution": "beta", "alpha": 2.0, "beta": 5.0}]
        bounds = _extract_bounds(var_list)
        assert bounds == [(0.0, 1.0)]


class TestDAMaximize:
    """Tests for DA in maximize mode."""

    def test_maximize_mode(self) -> None:
        algo = DualAnnealingAlgorithm(maximize=True)
        assert algo._maximize is True

    def test_observe_with_maximize(self, tmp_path: Path) -> None:
        algo = DualAnnealingAlgorithm(maximize=True)
        samples_path = algo.generate_samples(
            _VARIABLES_2D, n_samples=5, seed=42, outdir=tmp_path / "gen0"
        )
        samples = json.loads(samples_path.read_text())["samples"]

        kpi_values = [100.0 - i * 10.0 for i in range(len(samples))]
        history_entry = _make_history_with_kpis(tmp_path, samples, kpi_values, generation=0)

        new_samples = algo.observe([history_entry])
        assert len(new_samples) > 0


class TestDAConvergenceEdgeCases:
    """Edge case tests for DA convergence."""

    def test_converged_when_prev_best_zero(self) -> None:
        algo = DualAnnealingAlgorithm()
        algo._prev_best = 0.0
        algo._best_value = 0.0
        assert algo.is_converged([{"gen": 0}, {"gen": 1}]) is True

    def test_not_converged_when_prev_best_zero_best_nonzero(self) -> None:
        algo = DualAnnealingAlgorithm()
        algo._prev_best = 0.0
        algo._best_value = 1.0
        assert algo.is_converged([{"gen": 0}, {"gen": 1}]) is False

    def test_not_converged_with_inf_values(self) -> None:
        algo = DualAnnealingAlgorithm()
        algo._prev_best = float("inf")
        algo._best_value = float("inf")
        assert algo.is_converged([{"gen": 0}, {"gen": 1}]) is False

    def test_observe_no_kpi_values(self, tmp_path: Path) -> None:
        algo = DualAnnealingAlgorithm()
        algo.generate_samples(_VARIABLES_2D, n_samples=5, seed=42, outdir=tmp_path / "gen0")
        history_entry: dict[str, Any] = {
            "generation": 0,
            "samples": [{"sample_id": "0001", "values": {"wall_r": 5.0}}],
            "kpi_files": [str(tmp_path / "nonexistent.json")],
        }
        result = algo.observe([history_entry])
        assert result == []

    def test_observe_with_zero_new_samples(self, tmp_path: Path) -> None:
        algo = DualAnnealingAlgorithm()
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
        algo = DualAnnealingAlgorithm()
        result = algo.generate_samples({"variables": []}, n_samples=5, seed=None, outdir=tmp_path)
        data = json.loads(result.read_text())
        assert data["samples"] == []

    def test_no_independent_vars(self, tmp_path: Path) -> None:
        algo = DualAnnealingAlgorithm()
        variables: dict[str, Any] = {
            "variables": [
                {"name": "x", "distribution": "conditional", "depends_on": {"variable": "y"}},
            ]
        }
        result = algo.generate_samples(variables, n_samples=5, seed=42, outdir=tmp_path)
        data = json.loads(result.read_text())
        assert data["samples"] == []


class TestDAProposeSamplesAround:
    """Tests for _propose_samples_around (DA module)."""

    def test_proposes_correct_count(self) -> None:
        import numpy as np

        from osimflow.algorithms.da import _propose_samples_around

        center = np.array([5.0, 0.5])
        bounds = [(1.0, 10.0), (0.1, 0.9)]
        var_names = ["wall_r", "window_shgc"]
        result = _propose_samples_around(center, 5, bounds, var_names)
        assert len(result) == 5

    def test_proposed_samples_clipped_to_bounds(self) -> None:
        import numpy as np

        from osimflow.algorithms.da import _propose_samples_around

        center = np.array([1.0, 0.1])
        bounds = [(1.0, 10.0), (0.1, 0.9)]
        var_names = ["wall_r", "window_shgc"]
        result = _propose_samples_around(center, 20, bounds, var_names, width=5.0)
        for s in result:
            assert 1.0 <= s["values"]["wall_r"] <= 10.0
            assert 0.1 <= s["values"]["window_shgc"] <= 0.9


class TestDAReadKPIValuesEdgeCases:
    """Edge case tests for DA _read_kpi_values."""

    def test_missing_kpi_file(self, tmp_path: Path) -> None:
        samples = [{"sample_id": "0001", "values": {"wall_r": 5.0}}]
        entry: dict[str, Any] = {
            "generation": 0,
            "samples": samples,
            "kpi_files": [str(tmp_path / "nonexistent.json")],
        }
        results = _read_kpi_values([entry], "eui")
        assert results == []

    def test_invalid_json(self, tmp_path: Path) -> None:
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

    def test_missing_kpi_key(self, tmp_path: Path) -> None:
        samples = [{"sample_id": "0001", "values": {"wall_r": 5.0}}]
        kpi_dir = tmp_path / "kpi"
        kpi_dir.mkdir()
        kpi_path = kpi_dir / "kpi_0001.json"
        kpi_path.write_text(json.dumps({"sample_id": "0001", "kpis": {"other": 42.0}}))
        entry: dict[str, Any] = {
            "generation": 0,
            "samples": samples,
            "kpi_files": [str(kpi_path)],
        }
        results = _read_kpi_values([entry], "eui")
        assert len(results) == 1
        assert results[0][1] == float("inf")
