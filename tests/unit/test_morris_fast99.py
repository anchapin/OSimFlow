"""Tests for Morris and FAST99 sensitivity analysis samplers (issue #136).

Both algorithms depend on SALib (the ``[sensitivity]`` extra).  All
tests skip gracefully when SALib is not installed.
"""

import json
from pathlib import Path

import numpy as np
import pytest

salib = pytest.importorskip("SALib")  # noqa: F841 — used as skip gate

import osimflow.algorithms as _alg_mod  # noqa: E402

AlgorithmRegistry = _alg_mod.AlgorithmRegistry


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

ISHIGAMI_VARIABLES: dict[str, object] = {
    "variables": [
        {"name": "x1", "distribution": "uniform", "min": -3.14159265, "max": 3.14159265},
        {"name": "x2", "distribution": "uniform", "min": -3.14159265, "max": 3.14159265},
        {"name": "x3", "distribution": "uniform", "min": -3.14159265, "max": 3.14159265},
    ],
}

SIMPLE_VARIABLES: dict[str, object] = {
    "variables": [
        {"name": "a", "distribution": "uniform", "min": 0.0, "max": 1.0},
        {"name": "b", "distribution": "uniform", "min": 0.0, "max": 10.0},
    ],
}


# ---------------------------------------------------------------------------
# Registration tests
# ---------------------------------------------------------------------------


class TestRegistration:
    """AlgorithmRegistry should contain morris and fast99 when SALib is available."""

    def test_morris_registered(self) -> None:
        algo = AlgorithmRegistry.get("morris")
        assert algo.name() == "morris"

    def test_fast99_registered(self) -> None:
        algo = AlgorithmRegistry.get("fast99")
        assert algo.name() == "fast99"

    def test_morris_in_available_list(self) -> None:
        assert "morris" in AlgorithmRegistry.list_available()

    def test_fast99_in_available_list(self) -> None:
        assert "fast99" in AlgorithmRegistry.list_available()


# ---------------------------------------------------------------------------
# Morris tests
# ---------------------------------------------------------------------------


class TestMorris:
    """MorrisAlgorithm correctness tests."""

    def test_generates_samples_file(self, tmp_path: Path) -> None:
        algo = AlgorithmRegistry.get("morris")
        result = algo.generate_samples(SIMPLE_VARIABLES, n_samples=4, seed=42, outdir=tmp_path)
        assert result == tmp_path / "samples.json"
        assert result.exists()
        data = json.loads(result.read_text())
        assert "samples" in data
        assert len(data["samples"]) > 0

    def test_sample_count_is_n_trajectories_times_d_plus_1(self, tmp_path: Path) -> None:
        """Morris produces N*(D+1) sample points for N trajectories and D variables."""
        from osimflow.algorithms.morris import MorrisAlgorithm

        algo = MorrisAlgorithm()
        n_trajectories = 4
        d = len(SIMPLE_VARIABLES["variables"])  # type: ignore[arg-type]
        expected_count = n_trajectories * (d + 1)

        result = algo.generate_samples(
            SIMPLE_VARIABLES, n_samples=n_trajectories, seed=42, outdir=tmp_path
        )
        data = json.loads(result.read_text())
        assert len(data["samples"]) == expected_count

    def test_sample_values_within_bounds(self, tmp_path: Path) -> None:
        from osimflow.algorithms.morris import MorrisAlgorithm

        algo = MorrisAlgorithm()
        result = algo.generate_samples(SIMPLE_VARIABLES, n_samples=4, seed=42, outdir=tmp_path)
        data = json.loads(result.read_text())

        for sample in data["samples"]:
            a_val = sample["values"]["a"]
            b_val = sample["values"]["b"]
            assert -0.5 <= a_val <= 1.5, f"a={a_val} outside extended bounds"
            assert -0.5 <= b_val <= 10.5, f"b={b_val} outside extended bounds"

    def test_is_single_shot(self) -> None:
        from osimflow.algorithms.morris import MorrisAlgorithm

        algo = MorrisAlgorithm()
        assert algo.is_iterative() is False
        assert algo.is_converged([]) is True

    def test_observe_returns_last(self, tmp_path: Path) -> None:
        from osimflow.algorithms.morris import MorrisAlgorithm

        algo = MorrisAlgorithm()
        history = [{"samples": [{"sample_id": "0001", "values": {"a": 0.5}}]}]
        result = algo.observe(history)
        assert len(result) == 1
        assert result[0]["sample_id"] == "0001"

    def test_empty_variables_produces_empty_samples(self, tmp_path: Path) -> None:
        from osimflow.algorithms.morris import MorrisAlgorithm

        algo = MorrisAlgorithm()
        result = algo.generate_samples({"variables": []}, n_samples=4, seed=42, outdir=tmp_path)
        data = json.loads(result.read_text())
        assert data["samples"] == []


# ---------------------------------------------------------------------------
# FAST99 tests
# ---------------------------------------------------------------------------


class TestFAST99:
    """FAST99Algorithm correctness tests."""

    def test_generates_samples_file(self, tmp_path: Path) -> None:
        algo = AlgorithmRegistry.get("fast99")
        result = algo.generate_samples(SIMPLE_VARIABLES, n_samples=100, seed=42, outdir=tmp_path)
        assert result == tmp_path / "samples.json"
        assert result.exists()
        data = json.loads(result.read_text())
        assert "samples" in data
        assert len(data["samples"]) > 0

    def test_sample_count(self, tmp_path: Path) -> None:
        """FAST99 produces N*D sample points (N per factor)."""
        from osimflow.algorithms.fast99 import FAST99Algorithm

        algo = FAST99Algorithm()
        n = 100
        d = len(SIMPLE_VARIABLES["variables"])  # type: ignore[arg-type]
        # SALib FAST produces N*D samples
        expected_count = n * d

        result = algo.generate_samples(SIMPLE_VARIABLES, n_samples=n, seed=42, outdir=tmp_path)
        data = json.loads(result.read_text())
        assert len(data["samples"]) == expected_count

    def test_sample_values_within_bounds(self, tmp_path: Path) -> None:
        from osimflow.algorithms.fast99 import FAST99Algorithm

        algo = FAST99Algorithm()
        result = algo.generate_samples(SIMPLE_VARIABLES, n_samples=100, seed=42, outdir=tmp_path)
        data = json.loads(result.read_text())

        for sample in data["samples"]:
            a_val = sample["values"]["a"]
            b_val = sample["values"]["b"]
            assert -0.5 <= a_val <= 1.5, f"a={a_val} outside extended bounds"
            assert -0.5 <= b_val <= 10.5, f"b={b_val} outside extended bounds"

    def test_is_single_shot(self) -> None:
        from osimflow.algorithms.fast99 import FAST99Algorithm

        algo = FAST99Algorithm()
        assert algo.is_iterative() is False
        assert algo.is_converged([]) is True

    def test_empty_variables_produces_empty_samples(self, tmp_path: Path) -> None:
        from osimflow.algorithms.fast99 import FAST99Algorithm

        algo = FAST99Algorithm()
        result = algo.generate_samples({"variables": []}, n_samples=100, seed=42, outdir=tmp_path)
        data = json.loads(result.read_text())
        assert data["samples"] == []


# ---------------------------------------------------------------------------
# Ishigami validation (Morris)
# ---------------------------------------------------------------------------


class TestMorrisIshigami:
    """Validate Morris on the Ishigami test function.

    The Ishigami function is a standard benchmark for sensitivity
    analysis::

        f(x1, x2, x3) = sin(x1) + 7*sin(x2)^2 + 0.1*x3^4*sin(x1)

    We verify that the elementary effects computed from Morris samples
    produce rankings consistent with the known sensitivity structure
    (x1 and x2 are the most influential, x3 has a small first-order
    effect but large total effect due to interaction with x1).
    """

    @staticmethod
    def _ishigami(x: np.ndarray) -> np.ndarray:
        """Evaluate the Ishigami function."""
        return np.sin(x[:, 0]) + 7.0 * np.sin(x[:, 1]) ** 2 + 0.1 * x[:, 2] ** 4 * np.sin(x[:, 0])

    def test_morris_ishigami_values(self, tmp_path: Path) -> None:
        """Morris elementary effects on Ishigami should rank x1 and x2 as important."""
        from SALib.analyze.morris import analyze as morris_analyze

        from osimflow.algorithms.morris import MorrisAlgorithm

        algo = MorrisAlgorithm()
        result = algo.generate_samples(ISHIGAMI_VARIABLES, n_samples=20, seed=42, outdir=tmp_path)
        data = json.loads(result.read_text())

        # Build the sample matrix for analysis
        samples_list = []
        for s in data["samples"]:
            samples_list.append([s["values"]["x1"], s["values"]["x2"], s["values"]["x3"]])
        X = np.array(samples_list)
        Y = self._ishigami(X)

        problem = {
            "num_vars": 3,
            "names": ["x1", "x2", "x3"],
            "bounds": [
                (-3.14159265, 3.14159265),
                (-3.14159265, 3.14159265),
                (-3.14159265, 3.14159265),
            ],
        }

        # Compute number of trajectories from the sample count
        # Morris produces N*(D+1) samples for N trajectories
        Si = morris_analyze(problem, X, Y, num_resamples=100)
        mu_star = np.abs(Si["mu_star"])

        # x1 and x2 should have higher mu_star than x3
        # (x3's effect is mostly through interaction, not main effect)
        x1_idx = problem["names"].index("x1")
        x2_idx = problem["names"].index("x2")
        x3_idx = problem["names"].index("x3")

        assert mu_star[x1_idx] > mu_star[x3_idx], (
            f"x1 mu_star ({mu_star[x1_idx]:.4f}) should exceed x3 ({mu_star[x3_idx]:.4f})"
        )
        assert mu_star[x2_idx] > mu_star[x3_idx], (
            f"x2 mu_star ({mu_star[x2_idx]:.4f}) should exceed x3 ({mu_star[x3_idx]:.4f})"
        )


# ---------------------------------------------------------------------------
# pyproject.toml validation
# ---------------------------------------------------------------------------


class TestPyprojectToml:
    """Verify the [sensitivity] extra is declared in pyproject.toml."""

    def test_sensitivity_extra_exists(self) -> None:
        """The ``[sensitivity]`` extra should declare SALib."""
        import tomllib
        from pathlib import Path

        pyproject = Path(__file__).resolve().parent.parent.parent / "pyproject.toml"
        with pyproject.open("rb") as f:
            data = tomllib.load(f)
        extras = data["project"]["optional-dependencies"]
        assert "sensitivity" in extras, f"Missing [sensitivity] extra. Found: {list(extras)}"
        # Check that SALib is listed
        salib_dep = [d for d in extras["sensitivity"] if "SALib" in d]
        assert salib_dep, f"No SALib dependency in [sensitivity]. Found: {extras['sensitivity']}"
