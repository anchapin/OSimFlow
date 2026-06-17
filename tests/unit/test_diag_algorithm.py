"""Tests for DiagAlgorithm — one-at-a-time diagnostic analysis (issue #581)."""

import json

import numpy as np
import pytest

from osimflow.algorithms import AlgorithmRegistry, DiagAlgorithm
from osimflow.algorithms.diag import _baseline_value


class TestBaselineValue:
    """Tests for _baseline_value helper."""

    def test_triangular_with_mode(self) -> None:
        var_def = {"name": "x", "distribution": "triangular", "min": 0.0, "max": 10.0, "mode": 4.0}
        assert _baseline_value(var_def) == 4.0

    def test_triangular_without_mode_uses_midpoint(self) -> None:
        var_def = {"name": "x", "distribution": "triangular", "min": 0.0, "max": 10.0}
        assert _baseline_value(var_def) == 5.0

    def test_uniform_uses_midpoint(self) -> None:
        var_def = {"name": "x", "distribution": "uniform", "min": 0.0, "max": 10.0}
        assert _baseline_value(var_def) == 5.0

    def test_normal_uses_mean(self) -> None:
        var_def = {"name": "x", "distribution": "normal", "mean": 7.5, "sigma": 1.0}
        assert _baseline_value(var_def) == 7.5

    def test_lognormal_uses_mean(self) -> None:
        var_def = {"name": "x", "distribution": "lognormal", "mean": 2.0, "sigma": 0.5}
        assert _baseline_value(var_def) == 2.0

    def test_discrete_uses_first_value(self) -> None:
        var_def = {"name": "x", "distribution": "discrete", "values": [3, 5, 7]}
        assert _baseline_value(var_def) == 3.0

    def test_categorical_uses_first_value_float(self) -> None:
        var_def = {"name": "x", "distribution": "categorical", "values": [1.5, 2.5, 3.5]}
        assert _baseline_value(var_def) == 1.5

    def test_categorical_uses_zero_for_non_float(self) -> None:
        var_def = {"name": "x", "distribution": "categorical", "values": ["a", "b", "c"]}
        assert _baseline_value(var_def) == 0.0

    def test_beta_uses_alpha_over_alpha_plus_beta(self) -> None:
        var_def = {"name": "x", "distribution": "beta", "alpha": 2.0, "beta": 3.0}
        assert _baseline_value(var_def) == pytest.approx(2.0 / 5.0)

    def test_gamma_uses_alpha(self) -> None:
        var_def = {"name": "x", "distribution": "gamma", "alpha": 3.5}
        assert _baseline_value(var_def) == 3.5

    def test_exponential_uses_one_over_rate(self) -> None:
        var_def = {"name": "x", "distribution": "exponential", "rate": 2.0}
        assert _baseline_value(var_def) == pytest.approx(0.5)


class TestDiagAlgorithm:
    """Tests for DiagAlgorithm."""

    @pytest.fixture
    def two_var_variables(self) -> dict:
        return {
            "variables": [
                {"name": "x", "distribution": "uniform", "min": 0.0, "max": 10.0},
                {"name": "y", "distribution": "uniform", "min": -5.0, "max": 5.0},
            ]
        }

    def test_name(self) -> None:
        algo = DiagAlgorithm()
        assert algo.name() == "diag"

    def test_is_iterative(self) -> None:
        algo = DiagAlgorithm()
        assert algo.is_iterative() is False

    def test_is_converged(self) -> None:
        algo = DiagAlgorithm()
        assert algo.is_converged([]) is True

    def test_produces_n_samples_times_n_variables(
        self, two_var_variables: dict, tmp_path: pytest.TempPathFactory
    ) -> None:
        algo = DiagAlgorithm()
        n_samples = 4
        result = algo.generate_samples(two_var_variables, n_samples=n_samples, seed=42, outdir=tmp_path)
        with result.open() as f:
            data = json.load(f)
        assert len(data["samples"]) == n_samples * 2
        assert data["experiment_type"] == "diagonal"

    def test_varied_variable_differs_from_baseline(
        self, two_var_variables: dict, tmp_path: pytest.TempPathFactory
    ) -> None:
        algo = DiagAlgorithm()
        result = algo.generate_samples(two_var_variables, n_samples=1, seed=123, outdir=tmp_path)
        with result.open() as f:
            data = json.load(f)
        sample = data["samples"][0]["values"]
        baseline_x = _baseline_value(two_var_variables["variables"][0])
        baseline_y = _baseline_value(two_var_variables["variables"][1])
        assert sample["x"] != baseline_x or sample["y"] != baseline_y

    def test_non_varied_variables_stay_at_baseline(
        self, two_var_variables: dict, tmp_path: pytest.TempPathFactory
    ) -> None:
        algo = DiagAlgorithm()
        n_samples = 3
        result = algo.generate_samples(two_var_variables, n_samples=n_samples, seed=456, outdir=tmp_path)
        with result.open() as f:
            data = json.load(f)
        baseline_x = _baseline_value(two_var_variables["variables"][0])
        baseline_y = _baseline_value(two_var_variables["variables"][1])
        for sample in data["samples"]:
            vals = sample["values"]
            if vals["x"] != baseline_x:
                assert vals["y"] == baseline_y
            if vals["y"] != baseline_y:
                assert vals["x"] == baseline_x

    def test_triangular_distribution_sampling(
        self, tmp_path: pytest.TempPathFactory
    ) -> None:
        algo = DiagAlgorithm()
        variables = {
            "variables": [
                {
                    "name": "t",
                    "distribution": "triangular",
                    "min": 0.0,
                    "max": 10.0,
                    "mode": 6.0,
                }
            ]
        }
        result = algo.generate_samples(variables, n_samples=100, seed=789, outdir=tmp_path)
        with result.open() as f:
            data = json.load(f)
        vals = [s["values"]["t"] for s in data["samples"]]
        assert all(0.0 <= v <= 10.0 for v in vals)
        mean_val = float(np.mean(vals))
        assert 4.0 < mean_val < 8.0

    def test_reproducible_with_same_seed(
        self, two_var_variables: dict, tmp_path: pytest.TempPathFactory
    ) -> None:
        algo = DiagAlgorithm()
        result1 = algo.generate_samples(two_var_variables, n_samples=3, seed=999, outdir=tmp_path)
        with result1.open() as f:
            data1 = json.load(f)
        result2 = algo.generate_samples(two_var_variables, n_samples=3, seed=999, outdir=tmp_path)
        with result2.open() as f:
            data2 = json.load(f)
        assert [s["values"] for s in data1["samples"]] == [s["values"] for s in data2["samples"]]

    def test_different_seed_different_samples(
        self, two_var_variables: dict, tmp_path: pytest.TempPathFactory
    ) -> None:
        algo = DiagAlgorithm()
        result1 = algo.generate_samples(two_var_variables, n_samples=3, seed=111, outdir=tmp_path)
        with result1.open() as f:
            data1 = json.load(f)
        result2 = algo.generate_samples(two_var_variables, n_samples=3, seed=222, outdir=tmp_path)
        with result2.open() as f:
            data2 = json.load(f)
        vals1 = [s["values"] for s in data1["samples"]]
        vals2 = [s["values"] for s in data2["samples"]]
        assert vals1 != vals2

    def test_registers_in_algorithm_registry(self) -> None:
        available = AlgorithmRegistry.list_available()
        assert "diag" in available

    def test_retrievable_via_registry(self) -> None:
        algo = AlgorithmRegistry.get("diag")
        assert isinstance(algo, DiagAlgorithm)
        assert algo.name() == "diag"

    def test_experiment_type_in_output(
        self, two_var_variables: dict, tmp_path: pytest.TempPathFactory
    ) -> None:
        algo = DiagAlgorithm()
        result = algo.generate_samples(two_var_variables, n_samples=2, seed=42, outdir=tmp_path)
        with result.open() as f:
            data = json.load(f)
        assert data["experiment_type"] == "diagonal"
        assert all("sample_id" in s for s in data["samples"])

    def test_all_variables_get_varied_eventually(
        self, tmp_path: pytest.TempPathFactory
    ) -> None:
        algo = DiagAlgorithm()
        variables = {
            "variables": [
                {"name": "a", "distribution": "uniform", "min": 0.0, "max": 1.0},
                {"name": "b", "distribution": "uniform", "min": 0.0, "max": 1.0},
                {"name": "c", "distribution": "uniform", "min": 0.0, "max": 1.0},
            ]
        }
        result = algo.generate_samples(variables, n_samples=5, seed=42, outdir=tmp_path)
        with result.open() as f:
            data = json.load(f)
        baseline_a = _baseline_value(variables["variables"][0])
        baseline_b = _baseline_value(variables["variables"][1])
        baseline_c = _baseline_value(variables["variables"][2])
        varied_a = any(s["values"]["a"] != baseline_a for s in data["samples"])
        varied_b = any(s["values"]["b"] != baseline_b for s in data["samples"])
        varied_c = any(s["values"]["c"] != baseline_c for s in data["samples"])
        assert varied_a and varied_b and varied_c

    def test_sample_ids_are_sequential(
        self, tmp_path: pytest.TempPathFactory
    ) -> None:
        algo = DiagAlgorithm()
        variables = {
            "variables": [
                {"name": "x", "distribution": "uniform", "min": 0.0, "max": 1.0},
                {"name": "y", "distribution": "uniform", "min": 0.0, "max": 1.0},
            ]
        }
        result = algo.generate_samples(variables, n_samples=3, seed=42, outdir=tmp_path)
        with result.open() as f:
            data = json.load(f)
        ids = [s["sample_id"] for s in data["samples"]]
        assert ids == [f"{i:04d}" for i in range(1, len(ids) + 1)]
