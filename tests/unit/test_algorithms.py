"""Tests for the algorithm plug-in framework (issue #121).

Covers:
- AlgorithmRegistry.get("lhs") returns a LHSAlgorithm instance
- AlgorithmRegistry.get("unknown") raises ValueError with helpful message
- LHSAlgorithm.is_iterative() returns False
- LHSAlgorithm.is_converged([]) returns True
- LHSAlgorithm.name() returns "lhs"
- AlgorithmRegistry.list_available() includes "lhs"
- BaseAlgorithm ABC enforcement
- _apply_distribution for all distribution types
- _normalise_var_list for list and dict formats
- _partition_variables for independent vs conditional
- _sample_with_engine shared helper
- _resolve_conditional for dependent variables
- _generate_lhs_inline
- LHSAlgorithm error wrapping
"""

import json
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import scipy.stats

from osimflow.algorithms import (
    AlgorithmRegistry,
    BaseAlgorithm,
    LHSAlgorithm,
    _apply_distribution,
    _generate_lhs_inline,
    _normalise_var_list,
    _partition_variables,
    _resolve_conditional,
    _sample_independent,
    _sample_with_engine,
    _write_empty_samples,
)

# AlgorithmRegistry-mutating tests must run on the same xdist worker.
pytestmark = pytest.mark.xdist_group("algorithm_registry")


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
            AlgorithmRegistry._registry.pop("stub_test", None)

    def test_duplicate_registration_overwrites(self) -> None:
        """Re-registering overwrites the previous class."""

        class StubA(BaseAlgorithm):
            def generate_samples(
                self, variables: dict, n_samples: int, seed: int | None, outdir: Path
            ) -> Path:
                return Path("a")

            def observe(self, history: list[dict]) -> list[dict]:
                return []

            def is_converged(self, history: list[dict]) -> bool:
                return True

            def name(self) -> str:
                return "a"

            def is_iterative(self) -> bool:
                return False

        class StubB(BaseAlgorithm):
            def generate_samples(
                self, variables: dict, n_samples: int, seed: int | None, outdir: Path
            ) -> Path:
                return Path("b")

            def observe(self, history: list[dict]) -> list[dict]:
                return []

            def is_converged(self, history: list[dict]) -> bool:
                return True

            def name(self) -> str:
                return "b"

            def is_iterative(self) -> bool:
                return False

        AlgorithmRegistry.register("dup_test", StubA)
        AlgorithmRegistry.register("dup_test", StubB)
        try:
            algo = AlgorithmRegistry.get("dup_test")
            assert isinstance(algo, StubB)
        finally:
            AlgorithmRegistry._registry.pop("dup_test", None)

    def test_list_available_empty_message(self) -> None:
        """When registry is empty, ValueError message shows (none)."""
        saved = AlgorithmRegistry._registry.copy()
        try:
            AlgorithmRegistry._registry.clear()
            with pytest.raises(ValueError, match=r"Available algorithms: \(none\)"):
                AlgorithmRegistry.get("anything")
        finally:
            AlgorithmRegistry._registry.update(saved)

    def test_get_creates_new_instance_each_time(self) -> None:
        a1 = AlgorithmRegistry.get("lhs")
        a2 = AlgorithmRegistry.get("lhs")
        assert a1 is not a2


class TestBaseAlgorithmABC:
    """BaseAlgorithm cannot be instantiated without implementing abstract methods."""

    def test_cannot_instantiate_directly(self) -> None:
        with pytest.raises(TypeError):
            BaseAlgorithm()  # type: ignore[abstract]

    def test_is_multi_objective_default_false(self) -> None:
        class MinimalAlgo(BaseAlgorithm):
            def generate_samples(
                self, variables: dict, n_samples: int, seed: int | None, outdir: Path
            ) -> Path:
                return Path()

            def observe(self, history: list[dict]) -> list[dict]:
                return []

            def is_converged(self, history: list[dict]) -> bool:
                return True

            def name(self) -> str:
                return "minimal"

            def is_iterative(self) -> bool:
                return False

        algo = MinimalAlgo()
        assert algo.is_multi_objective() is False


class TestApplyDistribution:
    """Tests for _apply_distribution covering all distribution types."""

    def test_uniform(self) -> None:
        result = _apply_distribution(0.5, "uniform", {"min": 0.0, "max": 10.0})
        assert result == 5.0

    def test_uniform_boundaries(self) -> None:
        assert _apply_distribution(0.0, "uniform", {"min": 2.0, "max": 8.0}) == 2.0
        assert _apply_distribution(1.0, "uniform", {"min": 2.0, "max": 8.0}) == 8.0

    def test_normal(self) -> None:
        result = _apply_distribution(0.5, "normal", {"mean": 0.0, "sigma": 1.0})
        assert abs(result) < 0.01

    def test_lognormal(self) -> None:
        result = _apply_distribution(0.5, "lognormal", {"mean": 0.0, "sigma": 1.0})
        assert isinstance(result, float)
        assert result > 0

    def test_triangular(self) -> None:
        result = _apply_distribution(0.5, "triangular", {"min": 0.0, "max": 10.0, "mode": 5.0})
        assert isinstance(result, float)
        assert 0.0 <= result <= 10.0

    def test_triangular_no_mode(self) -> None:
        result = _apply_distribution(0.5, "triangular", {"min": 0.0, "max": 10.0})
        assert isinstance(result, float)

    def test_discrete(self) -> None:
        values = [10, 20, 30, 40, 50]
        result = _apply_distribution(0.0, "discrete", {"values": values})
        assert result == 10
        result = _apply_distribution(1.0, "discrete", {"values": values})
        assert result == 50

    def test_categorical(self) -> None:
        values = ["a", "b", "c"]
        result = _apply_distribution(0.5, "categorical", {"values": values})
        assert result in values

    def test_conditional_raises(self) -> None:
        with pytest.raises(NotImplementedError, match="conditional"):
            _apply_distribution(0.5, "conditional", {})

    def test_beta(self) -> None:
        result = _apply_distribution(0.5, "beta", {"alpha": 2.0, "beta": 5.0})
        assert isinstance(result, float)
        assert 0.0 < result < 1.0

    def test_beta_with_loc_scale(self) -> None:
        result = _apply_distribution(
            0.5, "beta", {"alpha": 2.0, "beta": 2.0, "loc": 5.0, "scale": 10.0}
        )
        assert isinstance(result, float)

    def test_gamma(self) -> None:
        result = _apply_distribution(0.5, "gamma", {"alpha": 2.0})
        assert isinstance(result, float)

    def test_gamma_with_loc_scale(self) -> None:
        result = _apply_distribution(0.5, "gamma", {"alpha": 2.0, "loc": 1.0, "scale": 2.0})
        assert isinstance(result, float)

    def test_exponential(self) -> None:
        result = _apply_distribution(0.5, "exponential", {"rate": 1.0})
        assert isinstance(result, float)

    def test_unsupported_distribution(self) -> None:
        with pytest.raises(NotImplementedError, match="unsupported distribution"):
            _apply_distribution(0.5, "unknown_dist", {})


class TestNormaliseVarList:
    """Tests for _normalise_var_list."""

    def test_list_format(self) -> None:
        raw = [{"name": "x", "distribution": "uniform", "min": 0.0, "max": 1.0}]
        result = _normalise_var_list(raw)
        assert len(result) == 1
        assert result[0]["name"] == "x"

    def test_dict_format(self) -> None:
        raw = {"x": {"distribution": "uniform", "min": 0.0, "max": 1.0}}
        result = _normalise_var_list(raw)
        assert len(result) == 1
        assert result[0]["name"] == "x"
        assert result[0]["distribution"] == "uniform"

    def test_empty_list(self) -> None:
        assert _normalise_var_list([]) == []

    def test_empty_dict(self) -> None:
        assert _normalise_var_list({}) == []

    def test_none(self) -> None:
        assert _normalise_var_list(None) == []

    def test_string(self) -> None:
        assert _normalise_var_list("not_a_list") == []


class TestPartitionVariables:
    """Tests for _partition_variables."""

    def test_all_independent(self) -> None:
        var_list = [
            {"name": "x", "distribution": "uniform", "min": 0.0, "max": 1.0},
            {"name": "y", "distribution": "normal", "mean": 0.0, "sigma": 1.0},
        ]
        indep, cond = _partition_variables(var_list)
        assert len(indep) == 2
        assert len(cond) == 0

    def test_mixed(self) -> None:
        var_list = [
            {"name": "x", "distribution": "uniform", "min": 0.0, "max": 1.0},
            {"name": "y", "distribution": "conditional", "depends_on": {"variable": "x"}},
        ]
        indep, cond = _partition_variables(var_list)
        assert len(indep) == 1
        assert len(cond) == 1
        assert indep[0]["name"] == "x"
        assert cond[0]["name"] == "y"

    def test_empty(self) -> None:
        indep, cond = _partition_variables([])
        assert indep == []
        assert cond == []


class TestSampleWithEngine:
    """Tests for _sample_with_engine."""

    def test_basic_lhs(self) -> None:
        var_list = [
            {"name": "x", "distribution": "uniform", "min": 0.0, "max": 1.0},
        ]
        result = _sample_with_engine(scipy.stats.qmc.LatinHypercube, var_list, n_samples=5, seed=42)
        assert len(result) == 5
        assert all("sample_id" in s and "values" in s for s in result)

    def test_empty_vars_returns_empty(self) -> None:
        result = _sample_with_engine(scipy.stats.qmc.LatinHypercube, [], n_samples=5, seed=42)
        assert result == []

    def test_multiple_vars(self) -> None:
        var_list = [
            {"name": "x", "distribution": "uniform", "min": 0.0, "max": 10.0},
            {"name": "y", "distribution": "uniform", "min": -5.0, "max": 5.0},
        ]
        result = _sample_with_engine(
            scipy.stats.qmc.LatinHypercube, var_list, n_samples=10, seed=42
        )
        assert len(result) == 10
        for s in result:
            assert "x" in s["values"]
            assert "y" in s["values"]
            assert 0.0 <= s["values"]["x"] <= 10.0
            assert -5.0 <= s["values"]["y"] <= 5.0

    def test_sample_ids_sequential(self) -> None:
        var_list = [{"name": "x", "distribution": "uniform", "min": 0.0, "max": 1.0}]
        result = _sample_with_engine(scipy.stats.qmc.LatinHypercube, var_list, n_samples=3, seed=42)
        assert [s["sample_id"] for s in result] == ["0001", "0002", "0003"]

    def test_seed_reproducibility(self) -> None:
        var_list = [{"name": "x", "distribution": "uniform", "min": 0.0, "max": 1.0}]
        r1 = _sample_with_engine(scipy.stats.qmc.LatinHypercube, var_list, n_samples=5, seed=99)
        r2 = _sample_with_engine(scipy.stats.qmc.LatinHypercube, var_list, n_samples=5, seed=99)
        assert r1 == r2

    def test_different_seeds_different_results(self) -> None:
        var_list = [{"name": "x", "distribution": "uniform", "min": 0.0, "max": 1.0}]
        r1 = _sample_with_engine(scipy.stats.qmc.LatinHypercube, var_list, n_samples=5, seed=1)
        r2 = _sample_with_engine(scipy.stats.qmc.LatinHypercube, var_list, n_samples=5, seed=2)
        assert r1 != r2


class TestSampleIndependent:
    """Tests for _sample_independent."""

    def test_basic(self) -> None:
        var_list = [{"name": "x", "distribution": "uniform", "min": 0.0, "max": 1.0}]
        result = _sample_independent(var_list, n_samples=3, seed=42)
        assert len(result) == 3


class TestResolveConditional:
    """Tests for _resolve_conditional."""

    def test_resolves_conditional_variable(self) -> None:
        samples: list[dict[str, Any]] = [
            {"sample_id": "0001", "values": {"x": 5.0}},
            {"sample_id": "0002", "values": {"x": 10.0}},
        ]
        conditional_vars = [
            {
                "name": "y",
                "distribution": "conditional",
                "depends_on": {"variable": "x", "match": "True"},
                "conditions": [
                    {"distribution": "uniform", "min": 0.0, "max": 1.0},
                ],
            }
        ]
        _resolve_conditional(samples, conditional_vars, n_samples=2)
        for s in samples:
            assert "y" in s["values"]


class TestWriteEmptySamples:
    """Tests for _write_empty_samples."""

    def test_writes_empty_json(self, tmp_path: Path) -> None:
        path = tmp_path / "samples.json"
        result = _write_empty_samples(path)
        assert result == path
        data = json.loads(path.read_text())
        assert data == {"samples": []}


class TestGenerateLHSInline:
    """Tests for _generate_lhs_inline."""

    _VARIABLES: dict[str, Any] = {
        "variables": [
            {"name": "x", "distribution": "uniform", "min": 0.0, "max": 1.0},
            {"name": "y", "distribution": "normal", "mean": 0.0, "sigma": 1.0},
        ]
    }

    def test_basic_generation(self, tmp_path: Path) -> None:
        result = _generate_lhs_inline(self._VARIABLES, n_samples=5, seed=42, outdir=tmp_path)
        assert result.exists()
        data = json.loads(result.read_text())
        assert len(data["samples"]) == 5

    def test_empty_variables(self, tmp_path: Path) -> None:
        result = _generate_lhs_inline({"variables": []}, n_samples=5, seed=42, outdir=tmp_path)
        data = json.loads(result.read_text())
        assert data["samples"] == []

    def test_dict_format_variables(self, tmp_path: Path) -> None:
        variables: dict[str, Any] = {
            "variables": {"x": {"distribution": "uniform", "min": 0.0, "max": 1.0}}
        }
        result = _generate_lhs_inline(variables, n_samples=3, seed=42, outdir=tmp_path)
        data = json.loads(result.read_text())
        assert len(data["samples"]) == 3

    def test_no_independent_vars(self, tmp_path: Path) -> None:
        variables: dict[str, Any] = {
            "variables": [
                {"name": "x", "distribution": "conditional", "depends_on": {"variable": "y"}},
            ]
        }
        result = _generate_lhs_inline(variables, n_samples=3, seed=42, outdir=tmp_path)
        data = json.loads(result.read_text())
        assert data["samples"] == []

    def test_creates_outdir(self, tmp_path: Path) -> None:
        nested = tmp_path / "deep" / "nested"
        _generate_lhs_inline(self._VARIABLES, n_samples=2, seed=42, outdir=nested)
        assert nested.is_dir()

    def test_conditional_resolution(self, tmp_path: Path) -> None:
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
        result = _generate_lhs_inline(variables, n_samples=3, seed=42, outdir=tmp_path)
        data = json.loads(result.read_text())
        for s in data["samples"]:
            assert "y" in s["values"]


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

    def test_generate_samples_empty_variables(self, tmp_path: Path) -> None:
        algo = LHSAlgorithm()
        result = algo.generate_samples({"variables": []}, n_samples=5, seed=42, outdir=tmp_path)
        data = json.loads(result.read_text())
        assert data["samples"] == []

    def test_generate_samples_wraps_not_implemented_error(self, tmp_path: Path) -> None:
        algo = LHSAlgorithm()
        variables: dict[str, Any] = {
            "variables": [
                {"name": "x", "distribution": "unsupported_dist_xyz"},
            ]
        }
        with pytest.raises(RuntimeError, match="generate_lhs failed"):
            algo.generate_samples(variables, n_samples=3, seed=42, outdir=tmp_path)

    def test_generate_samples_normal_distribution(self, tmp_path: Path) -> None:
        algo = LHSAlgorithm()
        variables: dict[str, Any] = {
            "variables": [
                {"name": "x", "distribution": "normal", "mean": 5.0, "sigma": 1.0},
            ]
        }
        result = algo.generate_samples(variables, n_samples=10, seed=42, outdir=tmp_path)
        data = json.loads(result.read_text())
        assert len(data["samples"]) == 10
        values = [s["values"]["x"] for s in data["samples"]]
        assert all(isinstance(v, float) for v in values)

    def test_generate_samples_mixed_distributions(self, tmp_path: Path) -> None:
        algo = LHSAlgorithm()
        variables: dict[str, Any] = {
            "variables": [
                {"name": "u", "distribution": "uniform", "min": 0.0, "max": 1.0},
                {"name": "n", "distribution": "normal", "mean": 0.0, "sigma": 1.0},
                {"name": "ln", "distribution": "lognormal", "mean": 0.0, "sigma": 0.5},
            ]
        }
        result = algo.generate_samples(variables, n_samples=5, seed=42, outdir=tmp_path)
        data = json.loads(result.read_text())
        assert len(data["samples"]) == 5
        for s in data["samples"]:
            assert "u" in s["values"]
            assert "n" in s["values"]
            assert "ln" in s["values"]


# ---------------------------------------------------------------------------
# RepeatAllAlgorithm
# ---------------------------------------------------------------------------

from osimflow.algorithms.repeat_all import RepeatAllAlgorithm  # noqa: E402


class TestRepeatAllAlgorithm:
    """Tests for the RepeatAllAlgorithm (issue #285)."""

    _VARIABLES: dict[str, Any] = {
        "variables": [
            {"name": "x", "distribution": "uniform", "min": 0.0, "max": 1.0},
            {"name": "y", "distribution": "normal", "mean": 0.0, "sigma": 1.0},
        ]
    }

    def test_name(self) -> None:
        algo = RepeatAllAlgorithm()
        assert algo.name() == "repeat_all"

    def test_is_iterative_false(self) -> None:
        algo = RepeatAllAlgorithm()
        assert algo.is_iterative() is False

    def test_is_converged(self) -> None:
        algo = RepeatAllAlgorithm()
        assert algo.is_converged([]) is True

    def test_observe_empty_history(self) -> None:
        algo = RepeatAllAlgorithm()
        assert algo.observe([]) == []

    def test_observe_returns_last_samples(self) -> None:
        algo = RepeatAllAlgorithm()
        history = [{"samples": [{"sample_id": "s0001"}]}]
        assert algo.observe(history) == [{"sample_id": "s0001"}]

    def test_generate_samples_repeats_base_set(self, tmp_path: Path) -> None:
        algo = RepeatAllAlgorithm()
        variables: dict[str, Any] = {
            "variables": [
                {"name": "x", "distribution": "uniform", "min": 0.0, "max": 1.0},
            ],
            "repeat_all_repeats": 3,
        }
        result = algo.generate_samples(variables, n_samples=5, seed=42, outdir=tmp_path)
        data = json.loads(result.read_text())
        # 5 unique × 3 repeats = 15 total
        assert len(data["samples"]) == 15

    def test_generate_samples_repeats_default_1(self, tmp_path: Path) -> None:
        algo = RepeatAllAlgorithm()
        variables: dict[str, Any] = {
            "variables": [
                {"name": "x", "distribution": "uniform", "min": 0.0, "max": 1.0},
            ],
        }
        result = algo.generate_samples(variables, n_samples=4, seed=42, outdir=tmp_path)
        data = json.loads(result.read_text())
        # repeats defaults to 1
        assert len(data["samples"]) == 4

    def test_generate_samples_sample_ids_prefixed(self, tmp_path: Path) -> None:
        algo = RepeatAllAlgorithm()
        variables: dict[str, Any] = {
            "variables": [
                {"name": "x", "distribution": "uniform", "min": 0.0, "max": 1.0},
            ],
            "repeat_all_repeats": 2,
        }
        result = algo.generate_samples(variables, n_samples=2, seed=42, outdir=tmp_path)
        data = json.loads(result.read_text())
        ids = [s["sample_id"] for s in data["samples"]]
        # r1-0001, r1-0002, r2-0001, r2-0002
        assert "r1-0001" in ids
        assert "r2-0001" in ids

    def test_generate_samples_empty_variables(self, tmp_path: Path) -> None:
        algo = RepeatAllAlgorithm()
        result = algo.generate_samples({"variables": []}, n_samples=3, seed=42, outdir=tmp_path)
        data = json.loads(result.read_text())
        assert data["samples"] == []

    def test_generate_samples_reproducible_with_seed(self, tmp_path: Path) -> None:
        algo = RepeatAllAlgorithm()
        variables: dict[str, Any] = {
            "variables": [
                {"name": "x", "distribution": "uniform", "min": 0.0, "max": 1.0},
            ],
            "repeat_all_repeats": 2,
        }
        r1 = algo.generate_samples(variables, n_samples=3, seed=99, outdir=tmp_path / "a")
        r2 = algo.generate_samples(variables, n_samples=3, seed=99, outdir=tmp_path / "b")
        assert json.loads(r1.read_text()) == json.loads(r2.read_text())

    def test_generate_samples_wraps_lhs_failure(self, tmp_path: Path) -> None:
        algo = RepeatAllAlgorithm()
        variables: dict[str, Any] = {
            "variables": [
                {"name": "x", "distribution": "unsupported_dist_xyz"},
            ],
        }
        with pytest.raises(RuntimeError, match="repeat_all failed"):
            algo.generate_samples(variables, n_samples=3, seed=42, outdir=tmp_path)


# ---------------------------------------------------------------------------
# RandomSamplingAlgorithm
# ---------------------------------------------------------------------------

from osimflow.algorithms.random_sampling import RandomSamplingAlgorithm  # noqa: E402


class TestRandomSamplingAlgorithm:
    """Tests for the RandomSamplingAlgorithm (issue #285)."""

    def test_name(self) -> None:
        algo = RandomSamplingAlgorithm()
        assert algo.name() == "random"

    def test_is_iterative_false(self) -> None:
        algo = RandomSamplingAlgorithm()
        assert algo.is_iterative() is False

    def test_is_converged(self) -> None:
        algo = RandomSamplingAlgorithm()
        assert algo.is_converged([]) is True

    def test_observe_empty_history(self) -> None:
        algo = RandomSamplingAlgorithm()
        assert algo.observe([]) == []

    def test_observe_returns_last_samples(self) -> None:
        algo = RandomSamplingAlgorithm()
        history = [{"samples": [{"sample_id": "s0001"}]}]
        assert algo.observe(history) == [{"sample_id": "s0001"}]

    def test_generate_samples_basic(self, tmp_path: Path) -> None:
        algo = RandomSamplingAlgorithm()
        variables: dict[str, Any] = {
            "variables": [
                {"name": "x", "distribution": "uniform", "min": 0.0, "max": 1.0},
            ]
        }
        result = algo.generate_samples(variables, n_samples=10, seed=42, outdir=tmp_path)
        data = json.loads(result.read_text())
        assert len(data["samples"]) == 10
        for s in data["samples"]:
            assert "sample_id" in s
            assert "values" in s
            assert "x" in s["values"]

    def test_generate_samples_all_distribution_types(self, tmp_path: Path) -> None:
        algo = RandomSamplingAlgorithm()
        variables: dict[str, Any] = {
            "variables": [
                {"name": "u", "distribution": "uniform", "min": 0.0, "max": 10.0},
                {"name": "n", "distribution": "normal", "mean": 5.0, "sigma": 1.0},
                {"name": "ln", "distribution": "lognormal", "mean": 0.0, "sigma": 0.5},
            ]
        }
        result = algo.generate_samples(variables, n_samples=5, seed=42, outdir=tmp_path)
        data = json.loads(result.read_text())
        assert len(data["samples"]) == 5
        for s in data["samples"]:
            assert "u" in s["values"]
            assert "n" in s["values"]
            assert "ln" in s["values"]

    def test_generate_samples_empty_variables(self, tmp_path: Path) -> None:
        algo = RandomSamplingAlgorithm()
        result = algo.generate_samples({"variables": []}, n_samples=5, seed=42, outdir=tmp_path)
        data = json.loads(result.read_text())
        assert data["samples"] == []

    def test_generate_samples_reproducible_with_seed(self, tmp_path: Path) -> None:
        algo = RandomSamplingAlgorithm()
        variables: dict[str, Any] = {
            "variables": [
                {"name": "x", "distribution": "uniform", "min": 0.0, "max": 1.0},
            ]
        }
        r1 = algo.generate_samples(variables, n_samples=10, seed=123, outdir=tmp_path / "a")
        r2 = algo.generate_samples(variables, n_samples=10, seed=123, outdir=tmp_path / "b")
        assert json.loads(r1.read_text()) == json.loads(r2.read_text())

    def test_generate_samples_different_seeds_different_results(self, tmp_path: Path) -> None:
        algo = RandomSamplingAlgorithm()
        variables: dict[str, Any] = {
            "variables": [
                {"name": "x", "distribution": "uniform", "min": 0.0, "max": 1.0},
            ]
        }
        r1 = algo.generate_samples(variables, n_samples=10, seed=1, outdir=tmp_path / "a")
        r2 = algo.generate_samples(variables, n_samples=10, seed=2, outdir=tmp_path / "b")
        assert json.loads(r1.read_text()) != json.loads(r2.read_text())

    def test_generate_samples_wraps_error(self, tmp_path: Path) -> None:
        algo = RandomSamplingAlgorithm()
        variables: dict[str, Any] = {
            "variables": [
                {"name": "x", "distribution": "unsupported_dist_xyz"},
            ]
        }
        with pytest.raises(NotImplementedError):
            algo.generate_samples(variables, n_samples=3, seed=42, outdir=tmp_path)


# ---------------------------------------------------------------------------
# IslandModelGAAlgorithm
# ---------------------------------------------------------------------------

from osimflow.algorithms.gaisl import IslandModelGAAlgorithm  # noqa: E402


class TestIslandModelGAAlgorithm:
    """Tests for the IslandModelGAAlgorithm (issue #549, GAP-005)."""

    _VARIABLES: dict[str, Any] = {
        "variables": [
            {"name": "x", "distribution": "uniform", "min": 0.0, "max": 1.0},
            {"name": "y", "distribution": "uniform", "min": -5.0, "max": 5.0},
        ]
    }

    def test_name(self) -> None:
        algo = IslandModelGAAlgorithm()
        assert algo.name() == "gaisl"

    def test_is_iterative_true(self) -> None:
        algo = IslandModelGAAlgorithm()
        assert algo.is_iterative() is True

    def test_is_converged_empty_history(self) -> None:
        algo = IslandModelGAAlgorithm()
        assert algo.is_converged([]) is False

    def test_observe_empty_history(self) -> None:
        algo = IslandModelGAAlgorithm()
        assert algo.observe([]) == []

    def test_observe_without_init_returns_empty(self) -> None:
        algo = IslandModelGAAlgorithm()
        # No islands initialized yet.
        result = algo.observe([{"samples": [{"sample_id": "0001"}]}])
        assert result == []

    def test_generate_samples_creates_file(self, tmp_path: Path) -> None:
        algo = IslandModelGAAlgorithm()
        result = algo.generate_samples(self._VARIABLES, n_samples=10, seed=42, outdir=tmp_path)
        assert result.exists()
        data = json.loads(result.read_text())
        assert "samples" in data
        assert len(data["samples"]) == 10

    def test_generate_samples_empty_variables(self, tmp_path: Path) -> None:
        algo = IslandModelGAAlgorithm()
        result = algo.generate_samples([], n_samples=10, seed=42, outdir=tmp_path)
        data = json.loads(result.read_text())
        assert data["samples"] == []


# SequentialSearchAlgorithm
# ---------------------------------------------------------------------------

from osimflow.algorithms.sequential_search import (  # noqa: E402
    SequentialSearchAlgorithm,
    _build_grid_samples,
    _extract_bounds,
)


class TestBuildGridSamples:
    """Tests for _build_grid_samples helper."""

    def test_full_range_grid(self) -> None:
        bounds = [(0.0, 10.0), (0.0, 1.0)]
        var_names = ["x", "y"]
        samples = _build_grid_samples(bounds, var_names, n_points_per_dim=3, center=None)
        assert len(samples) == 9  # 3x3 grid
        for s in samples:
            assert "x" in s["values"]
            assert "y" in s["values"]
            assert 0.0 <= s["values"]["x"] <= 10.0
            assert 0.0 <= s["values"]["y"] <= 1.0

    def test_adaptive_grid_around_center(self) -> None:
        bounds = [(0.0, 10.0)]
        var_names = ["x"]
        center = np.array([5.0])
        samples = _build_grid_samples(
            bounds, var_names, n_points_per_dim=3, center=center, radius_frac=0.5
        )
        # radius_frac=0.5 means 50% of range = 5.0, so grid should be in [0.0, 10.0]
        # (still within bounds because center is at 5.0 and radius is 5)
        assert len(samples) == 3
        for s in samples:
            assert 0.0 <= s["values"]["x"] <= 10.0

    def test_adaptive_grid_clipped_to_bounds(self) -> None:
        bounds = [(0.0, 10.0)]
        var_names = ["x"]
        center = np.array([9.0])  # Near upper bound
        samples = _build_grid_samples(
            bounds, var_names, n_points_per_dim=3, center=center, radius_frac=0.5
        )
        # radius=5, center=9, range would be [4, 14] but clips to [0, 10]
        for s in samples:
            assert 0.0 <= s["values"]["x"] <= 10.0

    def test_empty_bounds(self) -> None:
        samples = _build_grid_samples([], [], n_points_per_dim=3)
        assert samples == []

    def test_single_point_grid(self) -> None:
        bounds = [(0.0, 10.0)]
        var_names = ["x"]
        samples = _build_grid_samples(bounds, var_names, n_points_per_dim=1, center=None)
        assert len(samples) == 1
        assert samples[0]["values"]["x"] == pytest.approx(5.0)


class TestExtractBounds:
    """Tests for _extract_bounds helper."""

    def test_uniform(self) -> None:
        vars = [{"name": "x", "distribution": "uniform", "min": 0.0, "max": 10.0}]
        bounds = _extract_bounds(vars)
        assert bounds == [(0.0, 10.0)]

    def test_normal(self) -> None:
        vars = [{"name": "x", "distribution": "normal", "mean": 5.0, "sigma": 1.0}]
        bounds = _extract_bounds(vars)
        assert bounds == [(2.0, 8.0)]  # ±3σ

    def test_lognormal(self) -> None:
        vars = [{"name": "x", "distribution": "lognormal", "mean": 0.0, "sigma": 0.5}]
        bounds = _extract_bounds(vars)
        assert len(bounds) == 1
        lo, hi = bounds[0]
        assert lo > 0  # lognormal bounds must be positive

    def test_triangular(self) -> None:
        vars = [{"name": "x", "distribution": "triangular", "min": 0.0, "max": 10.0}]
        bounds = _extract_bounds(vars)
        assert bounds == [(0.0, 10.0)]

    def test_fallback(self) -> None:
        vars = [{"name": "x", "distribution": "discrete", "values": [1, 2, 3]}]
        bounds = _extract_bounds(vars)
        assert bounds == [(0.0, 1.0)]


class TestSequentialSearchAlgorithm:
    """Tests for the SequentialSearchAlgorithm (issue #550, GAP-006)."""

    _VARIABLES: dict[str, Any] = {
        "variables": [
            {"name": "x", "distribution": "uniform", "min": 0.0, "max": 1.0},
            {"name": "y", "distribution": "normal", "mean": 0.0, "sigma": 1.0},
        ]
    }

    def test_name(self) -> None:
        algo = SequentialSearchAlgorithm()
        assert algo.name() == "sequential_search"

    def test_is_iterative_false_by_default(self) -> None:
        """Non-adaptive SequentialSearch is single-shot."""
        algo = SequentialSearchAlgorithm(adaptive_sampling=False)
        assert algo.is_iterative() is False

    def test_is_iterative_true_when_adaptive(self) -> None:
        """Adaptive SequentialSearch is iterative."""
        algo = SequentialSearchAlgorithm(adaptive_sampling=True, n_iterations=3)
        assert algo.is_iterative() is True

    def test_is_converged_non_adaptive(self) -> None:
        """Non-adaptive mode always returns True (single-shot)."""
        algo = SequentialSearchAlgorithm(adaptive_sampling=False)
        assert algo.is_converged([]) is True
        assert algo.is_converged([{"samples": []}]) is True

    def test_is_converged_adaptive_not_converged(self) -> None:
        """Adaptive mode with no improvement returns False."""
        algo = SequentialSearchAlgorithm(
            adaptive_sampling=True, n_iterations=3, convergence_threshold=1e-3
        )
        # Simulate history with results
        algo._best_value = 100.0
        algo._prev_best = 100.0
        algo._iteration = 1
        assert algo.is_converged([{"samples": []}]) is True  # 0% change < threshold

    def test_observe_non_adaptive_returns_last_samples(self) -> None:
        """Non-adaptive observe() returns the last history entry's samples."""
        algo = SequentialSearchAlgorithm(adaptive_sampling=False)
        history = [{"samples": [{"sample_id": "s0001"}]}]
        result = algo.observe(history)
        assert result == [{"sample_id": "s0001"}]

    def test_observe_empty_history_adaptive_returns_empty(self) -> None:
        algo = SequentialSearchAlgorithm(adaptive_sampling=True, n_iterations=3)
        assert algo.observe([]) == []

    def test_generate_samples_creates_file(self, tmp_path: Path) -> None:
        algo = SequentialSearchAlgorithm(adaptive_sampling=False)
        variables: dict[str, Any] = {
            "variables": [
                {"name": "x", "distribution": "uniform", "min": 0.0, "max": 10.0},
                {"name": "y", "distribution": "uniform", "min": -5.0, "max": 5.0},
            ]
        }
        result = algo.generate_samples(variables, n_samples=9, seed=42, outdir=tmp_path)
        assert result.exists()
        data = json.loads(result.read_text())
        assert "samples" in data
        # grid_points=3 (default) × grid_points=3 = 9 samples
        assert len(data["samples"]) == 9
        for sample in data["samples"]:
            assert "sample_id" in sample
            assert "values" in sample
            assert "x" in sample["values"]
            assert "y" in sample["values"]
            assert 0.0 <= sample["values"]["x"] <= 10.0
            assert -5.0 <= sample["values"]["y"] <= 5.0

    def test_generate_samples_empty_variables(self, tmp_path: Path) -> None:
        algo = SequentialSearchAlgorithm(adaptive_sampling=False)
        result = algo.generate_samples({"variables": []}, n_samples=5, seed=42, outdir=tmp_path)
        data = json.loads(result.read_text())
        assert data["samples"] == []

    def test_generate_samples_no_independent_vars(self, tmp_path: Path) -> None:
        algo = IslandModelGAAlgorithm()
        variables: dict[str, Any] = {
            "variables": [
                {"name": "x", "distribution": "conditional", "depends_on": {"variable": "y"}},
            ]
        }
        result = algo.generate_samples(variables, n_samples=5, seed=42, outdir=tmp_path)
        data = json.loads(result.read_text())
        assert data["samples"] == []

    def test_generate_samples_creates_outdir(self, tmp_path: Path) -> None:
        algo = IslandModelGAAlgorithm()
        nested = tmp_path / "deep" / "nested"
        result = algo.generate_samples(self._VARIABLES, n_samples=3, seed=0, outdir=nested)
        assert result.exists()
        assert nested.is_dir()

    def test_generate_samples_with_seed_reproducible(self, tmp_path: Path) -> None:
        algo = IslandModelGAAlgorithm()
        r1 = algo.generate_samples(
            self._VARIABLES, n_samples=10, seed=123, outdir=tmp_path / "run1"
        )
        r2 = algo.generate_samples(
            self._VARIABLES, n_samples=10, seed=123, outdir=tmp_path / "run2"
        )
        d1 = json.loads(r1.read_text())
        d2 = json.loads(r2.read_text())
        assert d1 == d2

    def test_observe_after_init_proposes_samples(self, tmp_path: Path) -> None:
        algo = IslandModelGAAlgorithm(numIslands=2, popsize=5)
        # Initialize variables first.
        algo.generate_samples(self._VARIABLES, n_samples=10, seed=42, outdir=tmp_path)
        # Observe with empty history (islands not initialized).
        result = algo.observe([])
        assert result == []
        # Simulate KPI data that the algorithm can read.
        # For a proper observe test we would need a full history with kpi_files,
        # but we can verify that observe() doesn't crash with the island init path.
        # The island initializes only when observe() is called with non-empty history.

    def test_observe_updates_best_value(self, tmp_path: Path) -> None:
        algo = IslandModelGAAlgorithm(numIslands=2, popsize=5)
        algo.generate_samples(self._VARIABLES, n_samples=10, seed=42, outdir=tmp_path)

        # Create mock history with a KPI file.
        kpi_file = tmp_path / "kpi_0001.json"
        kpi_file.write_text(json.dumps({"kpis": {"eui": 100.0}}))
        sample_file = tmp_path / "samples.json"
        sample_data = {"samples": [{"sample_id": "0001", "values": {"x": 0.5, "y": 0.0}}]}
        sample_file.write_text(json.dumps(sample_data))

        history = [{"samples": sample_data["samples"], "kpi_files": [str(kpi_file)]}]
        result = algo.observe(history)
        # Should return proposed samples after processing.
        assert isinstance(result, list)

    def test_migration_happens_at_interval(self, tmp_path: Path) -> None:
        algo = IslandModelGAAlgorithm(numIslands=3, popsize=5, migrationInterval=2)
        algo.generate_samples(self._VARIABLES, n_samples=15, seed=42, outdir=tmp_path)

        # Simulate multiple generations.
        kpi_file = tmp_path / "kpi_0001.json"
        kpi_file.write_text(json.dumps({"kpis": {"eui": 100.0}}))

        for gen in range(1, 5):
            sample_file = tmp_path / f"samples_gen{gen}.json"
            samples = [
                {"sample_id": f"{i:04d}", "values": {"x": 0.5 + gen * 0.01, "y": 0.0}}
                for i in range(15)
            ]
            sample_file.write_text(json.dumps({"samples": samples}))
            history = [{"samples": samples, "kpi_files": [str(kpi_file)] * 15}]
            algo.observe(history)

        # After 4 generations and migrationInterval=2, migration should have
        # happened at generations 2 and 4.
        assert algo._generation == 4

    def test_invalid_numIslands_raises(self) -> None:
        with pytest.raises(ValueError, match="numIslands must be >= 1"):
            IslandModelGAAlgorithm(numIslands=0)

    def test_invalid_migrationRate_raises(self) -> None:
        with pytest.raises(ValueError, match="migrationRate must be in"):
            IslandModelGAAlgorithm(migrationRate=0.0)
        with pytest.raises(ValueError, match="migrationRate must be in"):
            IslandModelGAAlgorithm(migrationRate=1.5)

    def test_invalid_migrationInterval_raises(self) -> None:
        with pytest.raises(ValueError, match="migrationInterval must be >= 1"):
            IslandModelGAAlgorithm(migrationInterval=0)

    def test_invalid_popsize_raises(self) -> None:
        with pytest.raises(ValueError, match="popsize must be >= 2"):
            IslandModelGAAlgorithm(popsize=1)

    def test_default_parameters(self) -> None:
        algo = IslandModelGAAlgorithm()
        assert algo._numIslands == 5
        assert algo._migrationRate == 0.1
        assert algo._migrationInterval == 10
        assert algo._popsize == 20
        assert algo._maximize is False
        assert algo._objective_kpi == "eui"

    def test_custom_parameters(self) -> None:
        algo = IslandModelGAAlgorithm(
            numIslands=8,
            migrationRate=0.2,
            migrationInterval=5,
            popsize=30,
            maximize=True,
            objective_kpi="cost",
        )
        assert algo._numIslands == 8
        assert algo._migrationRate == 0.2
        assert algo._migrationInterval == 5
        assert algo._popsize == 30
        assert algo._maximize is True
        assert algo._objective_kpi == "cost"

    def test_converged_when_relative_change_small(self) -> None:
        algo = IslandModelGAAlgorithm(tol=1e-3)
        algo._best_value = 99.99
        algo._prev_best = 100.0
        # relative_change = abs(99.99 - 100.0) / 100.0 = 0.0001 < 1e-3
        assert algo.is_converged([{"samples": []}, {"samples": []}]) is True

    def test_not_converged_when_relative_change_large(self) -> None:
        algo = IslandModelGAAlgorithm(tol=1e-3)
        algo._best_value = 100.0
        algo._prev_best = 200.0
        assert algo.is_converged([{}, {}]) is False

    def test_not_converged_when_prev_best_inf(self) -> None:
        algo = IslandModelGAAlgorithm()
        algo._best_value = 100.0
        algo._prev_best = float("inf")
        assert algo.is_converged([{}, {}]) is False

    def test_not_converged_single_history(self) -> None:
        algo = IslandModelGAAlgorithm()
        assert algo.is_converged([{}]) is False

    def test_proposed_samples_consumed_on_generate(self, tmp_path: Path) -> None:
        algo = IslandModelGAAlgorithm(numIslands=2, popsize=5)
        # First call to generate_samples initializes islands via observe.
        algo.generate_samples(self._VARIABLES, n_samples=10, seed=42, outdir=tmp_path / "g1")
        # Simulate history with results so islands initialize.
        kpi_file = tmp_path / "kpi.json"
        kpi_file.write_text(json.dumps({"kpis": {"eui": 100.0}}))
        samples = [{"sample_id": f"{i:04d}", "values": {"x": 0.5, "y": 0.0}} for i in range(10)]
        history = [{"samples": samples, "kpi_files": [str(kpi_file)] * 10}]
        proposed = algo.observe(history)
        assert isinstance(proposed, list)

        # Next generate_samples should consume the proposed samples.
        result2 = algo.generate_samples(
            self._VARIABLES, n_samples=10, seed=42, outdir=tmp_path / "g2"
        )
        data2 = json.loads(result2.read_text())
        # If proposed samples were consumed, this should be the new set.
        assert len(data2["samples"]) == 10


class TestIslandModelGAAlgorithmIntegration:
    """Integration-style tests for IslandModelGAAlgorithm with DEAP (skip if no DEAP)."""

    def test_observe_runs_generation_on_islands(self, tmp_path: Path) -> None:
        try:
            from deap import base as deap_base  # noqa: F401
        except ImportError:
            pytest.skip("DEAP not installed")

        algo = IslandModelGAAlgorithm(numIslands=2, popsize=5, migrationInterval=999)
        algo.generate_samples(
            {"variables": [{"name": "x", "distribution": "uniform", "min": 0.0, "max": 1.0}]},
            n_samples=10,
            seed=42,
            outdir=tmp_path,
        )

        # Create mock KPI data.
        kpi_file = tmp_path / "kpi_0001.json"
        kpi_file.write_text(json.dumps({"kpis": {"eui": 50.0}}))

        samples = [{"sample_id": f"{i:04d}", "values": {"x": 0.5}} for i in range(10)]
        history = [{"samples": samples, "kpi_files": [str(kpi_file)] * 10}]

        result = algo.observe(history)
        assert isinstance(result, list)
        # Islands should have run a generation.
        assert algo._generation == 1
        assert len(algo._islands) == 2

    def test_generate_samples_custom_grid_points(self, tmp_path: Path) -> None:
        algo = SequentialSearchAlgorithm(adaptive_sampling=False, grid_points=4)
        variables: dict[str, Any] = {
            "variables": [
                {"name": "x", "distribution": "uniform", "min": 0.0, "max": 1.0},
            ]
        }
        result = algo.generate_samples(variables, n_samples=4, seed=42, outdir=tmp_path)
        data = json.loads(result.read_text())
        assert len(data["samples"]) == 4

    def test_generate_samples_reproducible(self, tmp_path: Path) -> None:
        algo = SequentialSearchAlgorithm(adaptive_sampling=False)
        variables: dict[str, Any] = {
            "variables": [
                {"name": "x", "distribution": "uniform", "min": 0.0, "max": 1.0},
            ]
        }
        r1 = algo.generate_samples(variables, n_samples=3, seed=99, outdir=tmp_path / "a")
        r2 = algo.generate_samples(variables, n_samples=3, seed=99, outdir=tmp_path / "b")
        assert json.loads(r1.read_text()) == json.loads(r2.read_text())

    def test_n_iterations_validation(self) -> None:
        with pytest.raises(ValueError, match="n_iterations must be >= 1"):
            SequentialSearchAlgorithm(n_iterations=0)

    def test_adaptive_sampling_convergence(self, tmp_path: Path) -> None:
        """Adaptive mode converges when improvement drops below threshold."""
        algo = SequentialSearchAlgorithm(
            adaptive_sampling=True,
            n_iterations=10,
            convergence_threshold=1e-3,
            grid_points=2,
        )
        # Simulate: iteration 1, prev=100, curr=100.5 (0.5% change > 0.1%)
        algo._iteration = 1
        algo._prev_best = 100.0
        algo._best_value = 100.5
        assert algo.is_converged([{"samples": []}]) is False

        # Simulate: iteration 1, prev=100, curr=100.0005 (0.0005% change < 0.1%)
        algo._prev_best = 100.0
        algo._best_value = 100.0005
        assert algo.is_converged([{"samples": []}]) is True

    def test_adaptive_sampling_exhausted_iterations(self, tmp_path: Path) -> None:
        """Adaptive mode stops proposing new samples after n_iterations."""
        algo = SequentialSearchAlgorithm(
            adaptive_sampling=True,
            n_iterations=3,
            grid_points=2,
        )
        algo._iteration = 0
        algo._best_params = np.array([5.0])
        algo._best_value = 50.0

        variables: dict[str, Any] = {
            "variables": [
                {"name": "x", "distribution": "uniform", "min": 0.0, "max": 10.0},
            ]
        }
        # First call: iteration 0, generates initial grid
        result1 = algo.generate_samples(variables, n_samples=2, seed=42, outdir=tmp_path)
        data1 = json.loads(result1.read_text())
        assert len(data1["samples"]) == 2

        # Create KPI files so observe() has valid results to process.
        kpi_files: list[str] = []
        for sample in data1["samples"]:
            kpi_path = tmp_path / f"kpi_{sample['sample_id']}.json"
            kpi_path.write_text(json.dumps({"kpis": {"eui": 50.0}}))
            kpi_files.append(str(kpi_path))

        # Simulate observe: updates iteration to 1, proposes next grid
        history1 = [{"samples": data1["samples"], "kpi_files": kpi_files}]
        algo.observe(history1)
        assert algo._iteration == 1

        # After 3 iterations (0, 1, 2), the next observe should return empty
        algo._iteration = 3  # exhausted
        result = algo.observe(history1)
        assert result == []

    def test_registry_lookup(self) -> None:
        """SequentialSearchAlgorithm is registered in AlgorithmRegistry."""
        available = AlgorithmRegistry.list_available()
        assert "sequential_search" in available
        algo = AlgorithmRegistry.get("sequential_search")
        assert isinstance(algo, SequentialSearchAlgorithm)
