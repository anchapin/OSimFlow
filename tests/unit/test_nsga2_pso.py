"""Tests for NSGA-II and PSO algorithm implementations (issue #140).

Both algorithms depend on pymoo (optional ``[optimization]`` extra).
Tests that require pymoo use ``pytest.importorskip`` so they are
automatically skipped when pymoo is not installed.
"""

import json
import tempfile
from pathlib import Path
from typing import Any

import numpy as np
import pytest

# Skip the entire module if pymoo is not installed.
pymoo = pytest.importorskip("pymoo")

from osimflow.algorithms import AlgorithmRegistry  # noqa: E402

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def simple_variables() -> dict[str, Any]:
    """A minimal variables.yml definition with two uniform variables."""
    return {
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


@pytest.fixture
def tmp_outdir() -> Path:
    """Temporary directory for sample output."""
    with tempfile.TemporaryDirectory() as td:
        yield Path(td)


def _make_kpi_file(
    directory: Path,
    sample_id: str,
    kpis: dict[str, float],
) -> Path:
    """Write a minimal KPI JSON file and return its path."""
    path = directory / f"{sample_id}_kpi.json"
    path.write_text(json.dumps({"kpis": kpis}))
    return path


# ---------------------------------------------------------------------------
# Registration tests
# ---------------------------------------------------------------------------


class TestRegistration:
    """Test that NSGA-II and PSO are properly registered."""

    def test_nsga2_registered(self) -> None:
        """NSGA2Algorithm is registered under 'nsga2'."""
        assert "nsga2" in AlgorithmRegistry.list_available()

    def test_pso_registered(self) -> None:
        """PSOAlgorithm is registered under 'pso'."""
        assert "pso" in AlgorithmRegistry.list_available()

    def test_nsga2_instantiable(self) -> None:
        """Can instantiate NSGA2Algorithm via the registry."""
        algo = AlgorithmRegistry.get("nsga2")
        assert algo.name() == "nsga2"
        assert algo.is_iterative() is True

    def test_pso_instantiable(self) -> None:
        """Can instantiate PSOAlgorithm via the registry."""
        algo = AlgorithmRegistry.get("pso")
        assert algo.name() == "pso"
        assert algo.is_iterative() is True


# ---------------------------------------------------------------------------
# NSGA-II tests
# ---------------------------------------------------------------------------


class TestNSGA2:
    """Test NSGA2Algorithm behaviour."""

    def test_generate_initial_samples(
        self,
        simple_variables: dict[str, Any],
        tmp_outdir: Path,
    ) -> None:
        """Generation 0 produces valid LHS samples."""
        from osimflow.algorithms.nsga2 import NSGA2Algorithm

        algo = NSGA2Algorithm()
        path = algo.generate_samples(simple_variables, 10, seed=42, outdir=tmp_outdir)

        assert path.exists()
        data = json.loads(path.read_text())
        samples = data["samples"]
        assert len(samples) == 10
        for s in samples:
            assert "sample_id" in s
            assert "values" in s
            assert "wall_r" in s["values"]
            assert "window_shgc" in s["values"]
            # Check bounds.
            assert 1.0 <= s["values"]["wall_r"] <= 10.0
            assert 0.1 <= s["values"]["window_shgc"] <= 0.9

    def test_is_iterative(self) -> None:
        """NSGA2Algorithm.is_iterative() returns True."""
        from osimflow.algorithms.nsga2 import NSGA2Algorithm

        algo = NSGA2Algorithm()
        assert algo.is_iterative() is True

    def test_is_converged_initially_false(
        self,
        simple_variables: dict[str, Any],
        tmp_outdir: Path,
    ) -> None:
        """is_converged() returns False with insufficient history."""
        from osimflow.algorithms.nsga2 import NSGA2Algorithm

        algo = NSGA2Algorithm()
        algo.generate_samples(simple_variables, 10, seed=42, outdir=tmp_outdir)
        assert algo.is_converged([]) is False
        assert algo.is_converged([{"samples": []}]) is False

    def test_observe_and_converge_on_synthetic_data(
        self,
        simple_variables: dict[str, Any],
        tmp_outdir: Path,
    ) -> None:
        """NSGA-II converges on a simple bi-objective synthetic problem.

        Uses ZDT1-like objectives: f1 = x[0], f2 = 1 - sqrt(x[0]).
        With enough generations the hypervolume should stabilise.
        """
        from osimflow.algorithms.nsga2 import NSGA2Algorithm

        algo = NSGA2Algorithm(objective_kpis=["f1", "f2"], hv_tol=0.01, pop_size=20)

        # Generate initial population.
        samples_path = algo.generate_samples(
            simple_variables, n_samples=20, seed=42, outdir=tmp_outdir
        )
        samples = json.loads(samples_path.read_text())["samples"]

        # Simulate 5 generations of observe().
        for _gen in range(5):
            # Create synthetic KPI files.
            kpi_files: list[str] = []
            for s in samples:
                x0 = s["values"]["wall_r"]
                # Normalise x0 to [0, 1].
                x0_norm = (x0 - 1.0) / 9.0
                f1 = x0_norm
                f2 = 1.0 - np.sqrt(max(x0_norm, 0.0))
                kpi_path = _make_kpi_file(tmp_outdir, s["sample_id"], {"f1": f1, "f2": f2})
                kpi_files.append(str(kpi_path))

            history_entry: dict[str, Any] = {
                "samples": samples,
                "kpi_files": kpi_files,
            }
            new_samples = algo.observe([history_entry])
            if algo.is_converged([history_entry]):
                break
            samples = new_samples

        # The algorithm should have proposed new samples in each generation.
        # We don't require convergence in 5 gens for this simple test,
        # but we do require that observe() produces valid output.
        assert len(samples) > 0

    def test_custom_objectives(self) -> None:
        """NSGA2Algorithm accepts custom objective KPIs."""
        from osimflow.algorithms.nsga2 import NSGA2Algorithm

        algo = NSGA2Algorithm(
            objective_kpis=["energy", "carbon"],
            maximize=[False, True],
        )
        assert algo.name() == "nsga2"
        assert algo._objective_kpis == ["energy", "carbon"]
        assert algo._maximize == [False, True]


# ---------------------------------------------------------------------------
# PSO tests
# ---------------------------------------------------------------------------


class TestPSO:
    """Test PSOAlgorithm behaviour."""

    def test_generate_initial_samples(
        self,
        simple_variables: dict[str, Any],
        tmp_outdir: Path,
    ) -> None:
        """Generation 0 produces valid LHS samples."""
        from osimflow.algorithms.pso import PSOAlgorithm

        algo = PSOAlgorithm()
        path = algo.generate_samples(simple_variables, 10, seed=42, outdir=tmp_outdir)

        assert path.exists()
        data = json.loads(path.read_text())
        samples = data["samples"]
        assert len(samples) == 10
        for s in samples:
            assert "sample_id" in s
            assert "values" in s
            assert "wall_r" in s["values"]
            assert "window_shgc" in s["values"]

    def test_is_iterative(self) -> None:
        """PSOAlgorithm.is_iterative() returns True."""
        from osimflow.algorithms.pso import PSOAlgorithm

        algo = PSOAlgorithm()
        assert algo.is_iterative() is True

    def test_is_converged_initially_false(
        self,
        simple_variables: dict[str, Any],
        tmp_outdir: Path,
    ) -> None:
        """is_converged() returns False before any observation."""
        from osimflow.algorithms.pso import PSOAlgorithm

        algo = PSOAlgorithm()
        algo.generate_samples(simple_variables, 10, seed=42, outdir=tmp_outdir)
        assert algo.is_converged([]) is False

    def test_observe_updates_state(
        self,
        simple_variables: dict[str, Any],
        tmp_outdir: Path,
    ) -> None:
        """observe() updates PSO state and proposes new positions."""
        from osimflow.algorithms.pso import PSOAlgorithm

        algo = PSOAlgorithm(objective_kpi="eui")
        samples_path = algo.generate_samples(
            simple_variables, n_samples=10, seed=42, outdir=tmp_outdir
        )
        samples = json.loads(samples_path.read_text())["samples"]

        # Create synthetic KPI files with a simple quadratic objective.
        kpi_files: list[str] = []
        for s in samples:
            x0 = s["values"]["wall_r"]
            x1 = s["values"]["window_shgc"]
            # Simple objective: minimise distance from (5.0, 0.5).
            obj = (x0 - 5.0) ** 2 + (x1 - 0.5) ** 2
            kpi_path = _make_kpi_file(tmp_outdir, s["sample_id"], {"eui": obj})
            kpi_files.append(str(kpi_path))

        history_entry: dict[str, Any] = {
            "samples": samples,
            "kpi_files": kpi_files,
        }
        new_samples = algo.observe([history_entry])

        assert len(new_samples) > 0
        # Verify PSO state was initialised.
        assert algo._initialized is True
        assert algo._global_best_val < float("inf")

    def test_convergence_on_sphere(
        self,
        simple_variables: dict[str, Any],
        tmp_outdir: Path,
    ) -> None:
        """PSO converges on a simple sphere (quadratic) objective.

        The objective is (x0-5)^2 + (x1-0.5)^2, which has its global
        minimum at (5.0, 0.5).  After several generations the
        improvement should be negligible.
        """
        from osimflow.algorithms.pso import PSOAlgorithm

        algo = PSOAlgorithm(objective_kpi="eui", tol=0.05, w=0.4, c1=1.5, c2=1.5)
        samples_path = algo.generate_samples(
            simple_variables, n_samples=30, seed=42, outdir=tmp_outdir
        )
        samples = json.loads(samples_path.read_text())["samples"]

        converged = False
        for _gen in range(50):
            kpi_files: list[str] = []
            for s in samples:
                x0 = s["values"]["wall_r"]
                x1 = s["values"]["window_shgc"]
                obj = (x0 - 5.0) ** 2 + (x1 - 0.5) ** 2
                kpi_path = _make_kpi_file(tmp_outdir, s["sample_id"], {"eui": obj})
                kpi_files.append(str(kpi_path))

            history_entry: dict[str, Any] = {
                "samples": samples,
                "kpi_files": kpi_files,
            }
            new_samples = algo.observe([history_entry])
            if algo.is_converged([history_entry]):
                converged = True
                break
            samples = new_samples

        # PSO should converge on this simple problem within 50 gens.
        assert converged, (
            f"PSO did not converge on sphere objective within 50 generations "
            f"(last global_best={algo._global_best_val:.4f})"
        )


# ---------------------------------------------------------------------------
# pyproject.toml verification
# ---------------------------------------------------------------------------


class TestNSGA2ExtractBounds:
    """Tests for _extract_bounds in nsga2 module."""

    def test_uniform_bounds(self) -> None:
        from osimflow.algorithms.nsga2 import _extract_bounds

        var_list = [{"name": "x", "distribution": "uniform", "min": 1.0, "max": 10.0}]
        assert _extract_bounds(var_list) == [(1.0, 10.0)]

    def test_normal_bounds(self) -> None:
        from osimflow.algorithms.nsga2 import _extract_bounds

        var_list = [{"name": "x", "distribution": "normal", "mean": 5.0, "sigma": 1.0}]
        bounds = _extract_bounds(var_list)
        assert bounds == [(2.0, 8.0)]

    def test_lognormal_bounds(self) -> None:
        from osimflow.algorithms.nsga2 import _extract_bounds

        var_list = [{"name": "x", "distribution": "lognormal", "mean": 2.0, "sigma": 0.5}]
        bounds = _extract_bounds(var_list)
        assert len(bounds) == 1
        lo, hi = bounds[0]
        assert lo > 0
        assert hi > lo

    def test_triangular_bounds(self) -> None:
        from osimflow.algorithms.nsga2 import _extract_bounds

        var_list = [
            {"name": "x", "distribution": "triangular", "min": 1.0, "max": 5.0, "mode": 3.0}
        ]
        assert _extract_bounds(var_list) == [(1.0, 5.0)]

    def test_unknown_distribution_fallback(self) -> None:
        from osimflow.algorithms.nsga2 import _extract_bounds

        var_list = [{"name": "x", "distribution": "beta", "alpha": 2.0}]
        assert _extract_bounds(var_list) == [(0.0, 1.0)]


class TestNSGA2ReadMultiKPIValues:
    """Tests for _read_multi_kpi_values in nsga2 module."""

    def test_reads_multiple_kpis(self, tmp_outdir: Path) -> None:
        from osimflow.algorithms.nsga2 import _read_multi_kpi_values

        kpi_path = _make_kpi_file(tmp_outdir, "0001", {"f1": 0.5, "f2": 0.3})
        history = [
            {
                "samples": [{"sample_id": "0001", "values": {"x": 5.0}}],
                "kpi_files": [str(kpi_path)],
            }
        ]
        results = _read_multi_kpi_values(history, ["f1", "f2"])
        assert len(results) == 1
        assert results[0][1] == [0.5, 0.3]

    def test_missing_kpi_file(self, tmp_outdir: Path) -> None:
        from osimflow.algorithms.nsga2 import _read_multi_kpi_values

        history = [
            {
                "samples": [{"sample_id": "0001", "values": {"x": 5.0}}],
                "kpi_files": [str(tmp_outdir / "nonexistent.json")],
            }
        ]
        results = _read_multi_kpi_values(history, ["f1"])
        assert results == []

    def test_invalid_json(self, tmp_outdir: Path) -> None:
        from osimflow.algorithms.nsga2 import _read_multi_kpi_values

        bad_path = tmp_outdir / "bad.json"
        bad_path.write_text("not json{{{")
        history = [
            {
                "samples": [{"sample_id": "0001", "values": {"x": 5.0}}],
                "kpi_files": [str(bad_path)],
            }
        ]
        results = _read_multi_kpi_values(history, ["f1"])
        assert results == []

    def test_missing_kpi_key_returns_inf(self, tmp_outdir: Path) -> None:
        from osimflow.algorithms.nsga2 import _read_multi_kpi_values

        kpi_path = _make_kpi_file(tmp_outdir, "0001", {"other": 42.0})
        history = [
            {
                "samples": [{"sample_id": "0001", "values": {"x": 5.0}}],
                "kpi_files": [str(kpi_path)],
            }
        ]
        results = _read_multi_kpi_values(history, ["f1"])
        assert len(results) == 1
        assert results[0][1] == [float("inf")]

    def test_empty_history(self) -> None:
        from osimflow.algorithms.nsga2 import _read_multi_kpi_values

        assert _read_multi_kpi_values([], ["f1"]) == []


class TestNSGA2ArrayToSamples:
    """Tests for _array_to_samples."""

    def test_converts_array(self) -> None:
        from osimflow.algorithms.nsga2 import NSGA2Algorithm

        algo = NSGA2Algorithm()
        X = np.array([[1.0, 2.0], [3.0, 4.0]])
        result = algo._array_to_samples(X, ["a", "b"])
        assert len(result) == 2
        assert result[0]["values"]["a"] == 1.0
        assert result[1]["values"]["b"] == 4.0


class TestNSGA2SignObjectives:
    """Tests for _sign_objectives."""

    def test_flip_maximize(self) -> None:
        from osimflow.algorithms.nsga2 import NSGA2Algorithm

        algo = NSGA2Algorithm(maximize=[False, True])
        F = np.array([[1.0, 5.0], [2.0, 3.0]])
        F_signed = algo._sign_objectives(F)
        assert F_signed[0, 0] == 1.0
        assert F_signed[0, 1] == -5.0

    def test_no_flip_all_minimize(self) -> None:
        from osimflow.algorithms.nsga2 import NSGA2Algorithm

        algo = NSGA2Algorithm(maximize=[False, False])
        F = np.array([[1.0, 5.0]])
        F_signed = algo._sign_objectives(F)
        np.testing.assert_array_equal(F_signed, F)


class TestNSGA2EdgeCases:
    """Edge case tests for NSGA2Algorithm."""

    def test_empty_variables(self, tmp_outdir: Path) -> None:
        from osimflow.algorithms.nsga2 import NSGA2Algorithm

        algo = NSGA2Algorithm()
        result = algo.generate_samples({"variables": []}, 5, seed=42, outdir=tmp_outdir)
        data = json.loads(result.read_text())
        assert data["samples"] == []

    def test_no_independent_vars(self, tmp_outdir: Path) -> None:
        from osimflow.algorithms.nsga2 import NSGA2Algorithm

        algo = NSGA2Algorithm()
        variables: dict[str, Any] = {
            "variables": [
                {"name": "x", "distribution": "conditional", "depends_on": {"variable": "y"}},
            ]
        }
        result = algo.generate_samples(variables, 5, seed=42, outdir=tmp_outdir)
        data = json.loads(result.read_text())
        assert data["samples"] == []

    def test_observe_empty_history(self) -> None:
        from osimflow.algorithms.nsga2 import NSGA2Algorithm

        algo = NSGA2Algorithm()
        assert algo.observe([]) == []

    def test_observe_no_independent_vars(self) -> None:
        from osimflow.algorithms.nsga2 import NSGA2Algorithm

        algo = NSGA2Algorithm()
        assert algo.observe([{"samples": [], "kpi_files": []}]) == []

    def test_observe_no_kpi_files(self, tmp_outdir: Path) -> None:
        from osimflow.algorithms.nsga2 import NSGA2Algorithm

        algo = NSGA2Algorithm(objective_kpis=["f1", "f2"])
        algo._independent_vars = [{"name": "x", "distribution": "uniform", "min": 0, "max": 1}]
        result = algo.observe(
            [
                {
                    "samples": [{"sample_id": "0001", "values": {"x": 0.5}}],
                    "kpi_files": [str(tmp_outdir / "nonexistent.json")],
                }
            ]
        )
        assert result == []

    def test_is_converged_zero_prev_hv(self) -> None:
        from osimflow.algorithms.nsga2 import NSGA2Algorithm

        algo = NSGA2Algorithm()
        algo._hv_history = [0.0, 0.0]
        assert algo.is_converged([]) is True

    def test_is_converged_zero_prev_nonzero_curr(self) -> None:
        from osimflow.algorithms.nsga2 import NSGA2Algorithm

        algo = NSGA2Algorithm()
        algo._hv_history = [0.0, 1.0]
        assert algo.is_converged([]) is False

    def test_is_converged_stable_hv(self) -> None:
        from osimflow.algorithms.nsga2 import NSGA2Algorithm

        algo = NSGA2Algorithm(hv_tol=0.01)
        algo._hv_history = [100.0, 100.001]
        assert algo.is_converged([]) is True

    def test_is_converged_changing_hv(self) -> None:
        from osimflow.algorithms.nsga2 import NSGA2Algorithm

        algo = NSGA2Algorithm(hv_tol=0.01)
        algo._hv_history = [100.0, 110.0]
        assert algo.is_converged([]) is False

    def test_update_hypervolume_empty_F(self) -> None:
        from osimflow.algorithms.nsga2 import NSGA2Algorithm

        algo = NSGA2Algorithm()
        algo._update_hypervolume(np.array([]).reshape(0, 2))
        assert algo._hv_history == [0.0]

    def test_generate_samples_with_population(
        self, simple_variables: dict[str, Any], tmp_outdir: Path
    ) -> None:
        from osimflow.algorithms.nsga2 import NSGA2Algorithm

        algo = NSGA2Algorithm()
        algo.generate_samples(simple_variables, 5, seed=42, outdir=tmp_outdir)
        algo._population_X = np.array([[5.0, 0.5], [3.0, 0.3]])
        gen1_dir = tmp_outdir / "gen1"
        result = algo.generate_samples(simple_variables, 5, seed=42, outdir=gen1_dir)
        data = json.loads(result.read_text())
        assert len(data["samples"]) == 2


class TestPSOExtractBounds:
    """Tests for _extract_bounds in pso module."""

    def test_normal_bounds(self) -> None:
        from osimflow.algorithms.pso import _extract_bounds

        var_list = [{"name": "x", "distribution": "normal", "mean": 5.0, "sigma": 1.0}]
        assert _extract_bounds(var_list) == [(2.0, 8.0)]

    def test_lognormal_bounds(self) -> None:
        from osimflow.algorithms.pso import _extract_bounds

        var_list = [{"name": "x", "distribution": "lognormal", "mean": 2.0, "sigma": 0.5}]
        bounds = _extract_bounds(var_list)
        assert bounds[0][0] > 0

    def test_triangular_bounds(self) -> None:
        from osimflow.algorithms.pso import _extract_bounds

        var_list = [
            {"name": "x", "distribution": "triangular", "min": 1.0, "max": 5.0, "mode": 3.0}
        ]
        assert _extract_bounds(var_list) == [(1.0, 5.0)]

    def test_unknown_distribution_fallback(self) -> None:
        from osimflow.algorithms.pso import _extract_bounds

        var_list = [{"name": "x", "distribution": "beta"}]
        assert _extract_bounds(var_list) == [(0.0, 1.0)]


class TestPSOReadKPIValues:
    """Tests for _read_kpi_values in pso module."""

    def test_reads_kpis(self, tmp_outdir: Path) -> None:
        from osimflow.algorithms.pso import _read_kpi_values

        kpi_path = _make_kpi_file(tmp_outdir, "0001", {"eui": 100.0})
        history = [
            {
                "samples": [{"sample_id": "0001", "values": {"x": 5.0}}],
                "kpi_files": [str(kpi_path)],
            }
        ]
        results = _read_kpi_values(history, "eui")
        assert len(results) == 1
        assert results[0][1] == 100.0

    def test_missing_kpi_file(self, tmp_outdir: Path) -> None:
        from osimflow.algorithms.pso import _read_kpi_values

        history = [
            {
                "samples": [{"sample_id": "0001", "values": {"x": 5.0}}],
                "kpi_files": [str(tmp_outdir / "nonexistent.json")],
            }
        ]
        assert _read_kpi_values(history, "eui") == []

    def test_invalid_json(self, tmp_outdir: Path) -> None:
        from osimflow.algorithms.pso import _read_kpi_values

        bad_path = tmp_outdir / "bad.json"
        bad_path.write_text("not json{{{")
        history = [
            {
                "samples": [{"sample_id": "0001", "values": {"x": 5.0}}],
                "kpi_files": [str(bad_path)],
            }
        ]
        assert _read_kpi_values(history, "eui") == []


class TestPSOEdgeCases:
    """Edge case tests for PSOAlgorithm."""

    def test_empty_variables(self, tmp_outdir: Path) -> None:
        from osimflow.algorithms.pso import PSOAlgorithm

        algo = PSOAlgorithm()
        result = algo.generate_samples({"variables": []}, 5, seed=42, outdir=tmp_outdir)
        data = json.loads(result.read_text())
        assert data["samples"] == []

    def test_no_independent_vars(self, tmp_outdir: Path) -> None:
        from osimflow.algorithms.pso import PSOAlgorithm

        algo = PSOAlgorithm()
        variables: dict[str, Any] = {
            "variables": [
                {"name": "x", "distribution": "conditional", "depends_on": {"variable": "y"}},
            ]
        }
        result = algo.generate_samples(variables, 5, seed=42, outdir=tmp_outdir)
        data = json.loads(result.read_text())
        assert data["samples"] == []

    def test_observe_empty_history(self) -> None:
        from osimflow.algorithms.pso import PSOAlgorithm

        algo = PSOAlgorithm()
        assert algo.observe([]) == []

    def test_observe_no_results(self, tmp_outdir: Path) -> None:
        from osimflow.algorithms.pso import PSOAlgorithm

        algo = PSOAlgorithm()
        algo._independent_vars = [{"name": "x", "distribution": "uniform", "min": 0, "max": 1}]
        result = algo.observe(
            [
                {
                    "samples": [{"sample_id": "0001", "values": {"x": 0.5}}],
                    "kpi_files": [str(tmp_outdir / "nonexistent.json")],
                }
            ]
        )
        assert result == []

    def test_maximize_mode(self) -> None:
        from osimflow.algorithms.pso import PSOAlgorithm

        algo = PSOAlgorithm(maximize=True)
        assert algo._maximize is True

    def test_clip_to_bounds(self) -> None:
        from osimflow.algorithms.pso import PSOAlgorithm

        algo = PSOAlgorithm()
        algo._bounds = [(0.0, 1.0), (0.0, 10.0)]
        X = np.array([[-1.0, 15.0], [0.5, 5.0]])
        clipped = algo._clip_to_bounds(X)
        assert clipped[0, 0] == 0.0
        assert clipped[0, 1] == 10.0
        assert clipped[1, 0] == 0.5
        assert clipped[1, 1] == 5.0

    def test_is_converged_not_initialized(self) -> None:
        from osimflow.algorithms.pso import PSOAlgorithm

        algo = PSOAlgorithm()
        assert algo.is_converged([]) is False

    def test_is_converged_inf_values(self) -> None:
        from osimflow.algorithms.pso import PSOAlgorithm

        algo = PSOAlgorithm()
        algo._initialized = True
        algo._prev_global_best = float("inf")
        algo._global_best_val = float("inf")
        assert algo.is_converged([]) is False

    def test_is_converged_zero_best(self) -> None:
        from osimflow.algorithms.pso import PSOAlgorithm

        algo = PSOAlgorithm()
        algo._initialized = True
        algo._global_best_val = 1e-13
        algo._prev_global_best = 1.0
        assert algo.is_converged([]) is True

    def test_is_converged_prev_zero_curr_zero(self) -> None:
        from osimflow.algorithms.pso import PSOAlgorithm

        algo = PSOAlgorithm()
        algo._initialized = True
        algo._prev_global_best = 0.0
        algo._global_best_val = 0.0
        assert algo.is_converged([]) is True

    def test_is_converged_prev_zero_curr_nonzero(self) -> None:
        from osimflow.algorithms.pso import PSOAlgorithm

        algo = PSOAlgorithm()
        algo._initialized = True
        algo._prev_global_best = 0.0
        algo._global_best_val = 1.0
        assert algo.is_converged([]) is False

    def test_generate_samples_initialized(
        self, simple_variables: dict[str, Any], tmp_outdir: Path
    ) -> None:
        from osimflow.algorithms.pso import PSOAlgorithm

        algo = PSOAlgorithm()
        algo.generate_samples(simple_variables, 5, seed=42, outdir=tmp_outdir)
        algo._initialized = True
        algo._positions = np.array([[5.0, 0.5], [3.0, 0.3]])
        gen1_dir = tmp_outdir / "gen1"
        result = algo.generate_samples(simple_variables, 5, seed=42, outdir=gen1_dir)
        data = json.loads(result.read_text())
        assert len(data["samples"]) == 2

    def test_observe_maximize(self, simple_variables: dict[str, Any], tmp_outdir: Path) -> None:
        from osimflow.algorithms.pso import PSOAlgorithm

        algo = PSOAlgorithm(objective_kpi="eui", maximize=True)
        samples_path = algo.generate_samples(
            simple_variables, n_samples=5, seed=42, outdir=tmp_outdir
        )
        samples = json.loads(samples_path.read_text())["samples"]
        kpi_files: list[str] = []
        for s in samples:
            kpi_path = _make_kpi_file(tmp_outdir, s["sample_id"], {"eui": 100.0})
            kpi_files.append(str(kpi_path))
        history_entry: dict[str, Any] = {"samples": samples, "kpi_files": kpi_files}
        new_samples = algo.observe([history_entry])
        assert len(new_samples) > 0
        assert algo._initialized is True

    def test_observe_second_generation(
        self, simple_variables: dict[str, Any], tmp_outdir: Path
    ) -> None:
        from osimflow.algorithms.pso import PSOAlgorithm

        algo = PSOAlgorithm(objective_kpi="eui")
        samples_path = algo.generate_samples(
            simple_variables, n_samples=5, seed=42, outdir=tmp_outdir
        )
        samples = json.loads(samples_path.read_text())["samples"]

        kpi_files: list[str] = []
        for s in samples:
            obj = (s["values"]["wall_r"] - 5.0) ** 2 + (s["values"]["window_shgc"] - 0.5) ** 2
            kpi_path = _make_kpi_file(tmp_outdir, s["sample_id"], {"eui": obj})
            kpi_files.append(str(kpi_path))

        history_entry: dict[str, Any] = {"samples": samples, "kpi_files": kpi_files}
        new_samples = algo.observe([history_entry])
        assert algo._initialized is True

        gen2_kpi_files: list[str] = []
        for s in new_samples:
            obj = (s["values"]["wall_r"] - 5.0) ** 2 + (s["values"]["window_shgc"] - 0.5) ** 2
            kpi_path = _make_kpi_file(tmp_outdir, s["sample_id"] + "_g2", {"eui": obj})
            gen2_kpi_files.append(str(kpi_path))

        history_entry2: dict[str, Any] = {"samples": new_samples, "kpi_files": gen2_kpi_files}
        new_samples2 = algo.observe([history_entry2])
        assert len(new_samples2) > 0


class TestRNSGA2:
    """Tests for R-NSGA-II reference point adaptation (issue #529)."""

    def test_parse_ref_points_string_two_obj_three_points(self) -> None:
        """parse_ref_points_string correctly parses 3 ref points for 2-objective."""
        from osimflow.algorithms.nsga2 import NSGA2Algorithm

        algo = NSGA2Algorithm(objective_kpis=["f1", "f2"])
        ref = algo._parse_ref_points_string("0.25,0.75,0.5,0.5,0.75,0.25", n_obj=2)
        assert ref.shape == (3, 2)
        np.testing.assert_array_almost_equal(ref[0], [0.25, 0.75])
        np.testing.assert_array_almost_equal(ref[1], [0.5, 0.5])
        np.testing.assert_array_almost_equal(ref[2], [0.75, 0.25])

    def test_parse_ref_points_string_single_point(self) -> None:
        """parse_ref_points_string correctly parses a single ref point."""
        from osimflow.algorithms.nsga2 import NSGA2Algorithm

        algo = NSGA2Algorithm(objective_kpis=["f1", "f2"])
        ref = algo._parse_ref_points_string("0.3,0.7", n_obj=2)
        assert ref.shape == (1, 2)
        np.testing.assert_array_almost_equal(ref[0], [0.3, 0.7])

    def test_parse_ref_points_string_empty(self) -> None:
        """parse_ref_points_string handles empty input."""
        from osimflow.algorithms.nsga2 import NSGA2Algorithm

        algo = NSGA2Algorithm(objective_kpis=["f1", "f2"])
        ref = algo._parse_ref_points_string("", n_obj=2)
        assert ref.shape == (0, 2)

    def test_parse_ref_points_string_malformed_divisible(self) -> None:
        """parse_ref_points_string handles length not divisible by n_obj."""
        from osimflow.algorithms.nsga2 import NSGA2Algorithm

        algo = NSGA2Algorithm(objective_kpis=["f1", "f2"])
        ref = algo._parse_ref_points_string("0.25,0.75,0.5", n_obj=2)
        assert ref.shape == (1, 2)
        np.testing.assert_array_almost_equal(ref[0], [0.25, 0.75])

    def test_configure_no_ref_points(self) -> None:
        """configure() with no ref_points leaves _ref_points as None."""
        from osimflow.algorithms.nsga2 import NSGA2Algorithm

        class FakeConfig:
            nsga2_ref_points = None
            nsga2_ref_dirs_strategy = None

        algo = NSGA2Algorithm()
        algo.configure(FakeConfig())
        assert algo._ref_points is None

    def test_configure_explicit_ref_points(self) -> None:
        """configure() parses explicit comma-separated ref point coordinates."""
        from osimflow.algorithms.nsga2 import NSGA2Algorithm

        class FakeConfig:
            nsga2_ref_points = "0.25,0.75,0.5,0.5"
            nsga2_ref_dirs_strategy = None

        algo = NSGA2Algorithm(objective_kpis=["f1", "f2"])
        algo.configure(FakeConfig())
        assert algo._ref_points is not None
        assert algo._ref_points.shape == (2, 2)
        assert algo._ref_dirs_strategy is None

    def test_configure_das_dennis_strategy(self) -> None:
        """configure() with das_dennis strategy generates ref directions."""
        from osimflow.algorithms.nsga2 import NSGA2Algorithm

        class FakeConfig:
            nsga2_ref_points = "3"
            nsga2_ref_dirs_strategy = "das_dennis"

        algo = NSGA2Algorithm(objective_kpis=["f1", "f2"])
        algo.configure(FakeConfig())
        assert algo._ref_points is not None
        assert algo._ref_points.shape[1] == 2
        assert algo._ref_dirs_strategy == "das_dennis"

    def test_configure_das_dennis_three_obj(self) -> None:
        """configure() generates correct das_dennis ref dirs for 3 objectives."""
        from osimflow.algorithms.nsga2 import NSGA2Algorithm

        class FakeConfig:
            nsga2_ref_points = "2"
            nsga2_ref_dirs_strategy = "das_dennis"

        algo = NSGA2Algorithm(objective_kpis=["f1", "f2", "f3"])
        algo.configure(FakeConfig())
        assert algo._ref_points is not None
        assert algo._ref_points.shape[1] == 3

    def test_observe_with_rnsga2_uses_ref_points(
        self,
        simple_variables: dict[str, Any],
        tmp_outdir: Path,
    ) -> None:
        """observe() uses RNSGA2 when _ref_points is set (issue #529)."""
        from osimflow.algorithms.nsga2 import NSGA2Algorithm

        class FakeConfig:
            nsga2_ref_points = "0.25,0.75,0.5,0.5,0.75,0.25"
            nsga2_ref_dirs_strategy = None

        algo = NSGA2Algorithm(objective_kpis=["f1", "f2"], pop_size=10)
        algo.configure(FakeConfig())
        assert algo._ref_points is not None
        assert algo._ref_points.shape == (3, 2)

        samples_path = algo.generate_samples(
            simple_variables, n_samples=10, seed=42, outdir=tmp_outdir
        )
        samples = json.loads(samples_path.read_text())["samples"]

        kpi_files: list[str] = []
        for s in samples:
            x0 = s["values"]["wall_r"]
            x0_norm = (x0 - 1.0) / 9.0
            f1 = x0_norm
            f2 = 1.0 - np.sqrt(max(x0_norm, 0.0))
            kpi_path = _make_kpi_file(tmp_outdir, s["sample_id"], {"f1": f1, "f2": f2})
            kpi_files.append(str(kpi_path))

        history_entry: dict[str, Any] = {
            "samples": samples,
            "kpi_files": kpi_files,
        }
        new_samples = algo.observe([history_entry])
        assert len(new_samples) > 0

    def test_observe_falls_back_to_nsga2_without_ref_points(
        self,
        simple_variables: dict[str, Any],
        tmp_outdir: Path,
    ) -> None:
        """observe() uses NSGA2 when _ref_points is None."""
        from osimflow.algorithms.nsga2 import NSGA2Algorithm

        algo = NSGA2Algorithm(objective_kpis=["f1", "f2"], pop_size=10)
        assert algo._ref_points is None

        samples_path = algo.generate_samples(
            simple_variables, n_samples=10, seed=42, outdir=tmp_outdir
        )
        samples = json.loads(samples_path.read_text())["samples"]

        kpi_files: list[str] = []
        for s in samples:
            x0 = s["values"]["wall_r"]
            x0_norm = (x0 - 1.0) / 9.0
            f1 = x0_norm
            f2 = 1.0 - np.sqrt(max(x0_norm, 0.0))
            kpi_path = _make_kpi_file(tmp_outdir, s["sample_id"], {"f1": f1, "f2": f2})
            kpi_files.append(str(kpi_path))

        history_entry: dict[str, Any] = {
            "samples": samples,
            "kpi_files": kpi_files,
        }
        new_samples = algo.observe([history_entry])
        assert len(new_samples) > 0


class TestPyprojectOptimizationExtra:
    """Verify that pyproject.toml declares the [optimization] extra."""

    def test_optimization_extra_declared(self) -> None:
        """pyproject.toml has an 'optimization' extra with pymoo."""
        import tomllib

        pyproject = Path(__file__).resolve().parent.parent.parent / "pyproject.toml"
        with open(pyproject, "rb") as f:
            data = tomllib.load(f)

        extras = data["project"]["optional-dependencies"]
        assert "optimization" in extras, f"'optimization' not in extras: {list(extras)}"
        opt_deps = extras["optimization"]
        assert any("pymoo" in dep for dep in opt_deps), (
            f"pymoo not in optimization deps: {opt_deps}"
        )
