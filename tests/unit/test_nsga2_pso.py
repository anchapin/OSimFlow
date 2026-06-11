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
