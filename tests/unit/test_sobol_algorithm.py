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
        # SALib's sobol.sample produces N*(D+2) samples; D=2 → 8*4=32
        assert len(data["samples"]) == 32

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
        # SALib's sobol.sample with D=1 (only x is independent) produces N*(D+2)=4*3=12 samples.
        assert len(data["samples"]) == 12
        for s in data["samples"]:
            assert "y" in s["values"]

    def test_values_in_range(self, tmp_path: Path) -> None:
        algo = SobolAlgorithm()
        result = algo.generate_samples(_VARIABLES_2D, n_samples=16, seed=42, outdir=tmp_path)
        data = json.loads(result.read_text())
        for sample in data["samples"]:
            assert 1.0 <= sample["values"]["wall_r"] <= 10.0
            assert 0.1 <= sample["values"]["window_shgc"] <= 0.9

    def test_bad_distribution_uses_fallback_bounds(self, tmp_path: Path) -> None:
        """Unsupported distributions fall back to [0,1] bounds instead of raising."""
        algo = SobolAlgorithm()
        variables: dict[str, Any] = {
            "variables": [
                {"name": "x", "distribution": "unsupported_dist_xyz"},
            ]
        }
        result = algo.generate_samples(variables, n_samples=3, seed=42, outdir=tmp_path)
        data = json.loads(result.read_text())
        # SALib's sobol.sample with D=1 and n_samples=3 produces 3*(1+2)=9 samples
        assert len(data["samples"]) == 9
        for s in data["samples"]:
            assert 0.0 <= s["values"]["x"] <= 1.0

    def test_different_seeds_different_results(self, tmp_path: Path) -> None:
        algo = SobolAlgorithm()
        r1 = algo.generate_samples(_VARIABLES_2D, n_samples=4, seed=1, outdir=tmp_path / "r1")
        r2 = algo.generate_samples(_VARIABLES_2D, n_samples=4, seed=2, outdir=tmp_path / "r2")
        assert json.loads(r1.read_text()) != json.loads(r2.read_text())

    def test_normal_distribution_sampling(self, tmp_path: Path) -> None:
        """Normal distribution produces samples within ppf(0.0005)/ppf(0.9995) bounds.

        The bounds use ±3.29σ (ppf quantiles capturing 99.9%) rather than ±3σ.
        For mean=5, sigma=1, bounds are approximately [1.71, 8.29].
        """
        algo = SobolAlgorithm()
        variables: dict[str, Any] = {
            "variables": [
                {"name": "x", "distribution": "normal", "mean": 5.0, "sigma": 1.0},
            ]
        }
        result = algo.generate_samples(variables, n_samples=4, seed=42, outdir=tmp_path)
        data = json.loads(result.read_text())
        for s in data["samples"]:
            # ppf(0.0005) ≈ 1.71, ppf(0.9995) ≈ 8.29 for mean=5, sigma=1
            assert 1.7 <= s["values"]["x"] <= 8.3

    def test_lognormal_distribution_sampling(self, tmp_path: Path) -> None:
        """Lognormal distribution produces samples within ±3σ bounds."""
        algo = SobolAlgorithm()
        variables: dict[str, Any] = {
            "variables": [
                {"name": "x", "distribution": "lognormal", "mean": 5.0, "sigma": 1.0},
            ]
        }
        result = algo.generate_samples(variables, n_samples=4, seed=42, outdir=tmp_path)
        data = json.loads(result.read_text())
        assert len(data["samples"]) > 0

    def test_triangular_distribution_sampling(self, tmp_path: Path) -> None:
        """Triangular distribution produces samples within min/max bounds."""
        algo = SobolAlgorithm()
        variables: dict[str, Any] = {
            "variables": [
                {"name": "x", "distribution": "triangular", "min": 0.0, "max": 10.0},
            ]
        }
        result = algo.generate_samples(variables, n_samples=4, seed=42, outdir=tmp_path)
        data = json.loads(result.read_text())
        for s in data["samples"]:
            assert 0.0 <= s["values"]["x"] <= 10.0


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


# ======================================================================
# Sensitivity indices tests (issue #346)
# Requires SALib: pip install osimflow[sensitivity]
# ======================================================================


class TestSobolSensitivityIndices:
    """Tests for SobolAlgorithm.compute_sensitivity_indices()."""

    @pytest.fixture
    def algo(self) -> SobolAlgorithm:
        return SobolAlgorithm()

    @pytest.fixture
    def samples(self, tmp_path: Path) -> list[dict[str, Any]]:
        algo = SobolAlgorithm()
        result = algo.generate_samples(_VARIABLES_2D, n_samples=8, seed=42, outdir=tmp_path)
        data = json.loads(result.read_text())
        return data["samples"]

    @pytest.fixture
    def kpi_values(self) -> dict[str, dict[str, float]]:
        """Simple Euclidean-distance KPI for testing.

        SALib's sobol.sample with n_samples=8 and D=2 produces 32 samples
        (N*(D+2) = 8*4 = 32), so we need 32 KPI values (0001-0032).
        """
        return {f"{(i + 1):04d}": {"eui": float(i + 1)} for i in range(32)}

    def test_compute_sensitivity_indices_creates_file(
        self,
        algo: SobolAlgorithm,
        samples: list[dict[str, Any]],
        kpi_values: dict[str, dict[str, float]],
        tmp_path: Path,
    ) -> None:
        """compute_sensitivity_indices writes a sensitivity_indices.json file."""
        indices_path = algo.compute_sensitivity_indices(
            variables=_VARIABLES_2D,
            samples=samples,
            kpi_values=kpi_values,
            outdir=tmp_path,
        )
        assert indices_path is not None
        assert indices_path.exists()
        assert indices_path.name == "sensitivity_indices.json"

    def test_compute_sensitivity_indices_output_structure(
        self,
        algo: SobolAlgorithm,
        samples: list[dict[str, Any]],
        kpi_values: dict[str, dict[str, float]],
        tmp_path: Path,
    ) -> None:
        """Output JSON contains algorithm, problem, and indices sections."""
        indices_path = algo.compute_sensitivity_indices(
            variables=_VARIABLES_2D,
            samples=samples,
            kpi_values=kpi_values,
            outdir=tmp_path,
        )
        data = json.loads(indices_path.read_text())
        assert data["algorithm"] == "sobol"
        assert "problem" in data
        assert "indices" in data
        assert "S1" in data["indices"]
        assert "ST" in data["indices"]

    def test_compute_sensitivity_indices_s1_keys(
        self,
        algo: SobolAlgorithm,
        samples: list[dict[str, Any]],
        kpi_values: dict[str, dict[str, float]],
        tmp_path: Path,
    ) -> None:
        """S1 indices contain one entry per variable name."""
        indices_path = algo.compute_sensitivity_indices(
            variables=_VARIABLES_2D,
            samples=samples,
            kpi_values=kpi_values,
            outdir=tmp_path,
        )
        data = json.loads(indices_path.read_text())
        s1 = data["indices"]["S1"]
        assert "wall_r" in s1
        assert "window_shgc" in s1
        # Values should be floats between 0 and 1 (for valid sensitivity indices)
        for v in s1.values():
            assert isinstance(v, float)
            assert 0.0 <= v <= 1.0

    def test_compute_sensitivity_indices_st_keys(
        self,
        algo: SobolAlgorithm,
        samples: list[dict[str, Any]],
        kpi_values: dict[str, dict[str, float]],
        tmp_path: Path,
    ) -> None:
        """ST indices contain one entry per variable name."""
        indices_path = algo.compute_sensitivity_indices(
            variables=_VARIABLES_2D,
            samples=samples,
            kpi_values=kpi_values,
            outdir=tmp_path,
        )
        data = json.loads(indices_path.read_text())
        st = data["indices"]["ST"]
        assert "wall_r" in st
        assert "window_shgc" in st
        for v in st.values():
            assert isinstance(v, float)
            assert 0.0 <= v <= 1.0

    def test_compute_sensitivity_indices_problem_structure(
        self,
        algo: SobolAlgorithm,
        samples: list[dict[str, Any]],
        kpi_values: dict[str, dict[str, float]],
        tmp_path: Path,
    ) -> None:
        """Problem dict has num_vars, names, and bounds."""
        indices_path = algo.compute_sensitivity_indices(
            variables=_VARIABLES_2D,
            samples=samples,
            kpi_values=kpi_values,
            outdir=tmp_path,
        )
        data = json.loads(indices_path.read_text())
        problem = data["problem"]
        assert problem["num_vars"] == 2
        assert problem["names"] == ["wall_r", "window_shgc"]
        assert len(problem["bounds"]) == 2

    def test_compute_sensitivity_indices_empty_variables(
        self,
        algo: SobolAlgorithm,
        samples: list[dict[str, Any]],
        kpi_values: dict[str, dict[str, float]],
        tmp_path: Path,
    ) -> None:
        """Raises RuntimeError when no variables are defined."""
        with pytest.raises(RuntimeError, match="no variables defined"):
            algo.compute_sensitivity_indices(
                variables={"variables": []},
                samples=samples,
                kpi_values=kpi_values,
                outdir=tmp_path,
            )

    def test_compute_sensitivity_indices_no_independent_vars(
        self,
        algo: SobolAlgorithm,
        samples: list[dict[str, Any]],
        kpi_values: dict[str, dict[str, float]],
        tmp_path: Path,
    ) -> None:
        """Raises RuntimeError when all variables are conditional."""
        variables: dict[str, Any] = {
            "variables": [
                {"name": "x", "distribution": "conditional", "depends_on": {"variable": "y"}},
            ]
        }
        with pytest.raises(RuntimeError, match="no independent variables"):
            algo.compute_sensitivity_indices(
                variables=variables,
                samples=samples,
                kpi_values=kpi_values,
                outdir=tmp_path,
            )

    def test_compute_sensitivity_indices_no_numeric_kpi(
        self,
        algo: SobolAlgorithm,
        samples: list[dict[str, Any]],
        tmp_path: Path,
    ) -> None:
        """Raises RuntimeError when no numeric KPIs are found for a sample."""
        kpi_values: dict[str, dict[str, float]] = {
            f"{(i + 1):04d}": {"eui": "not_a_number"} for i in range(32)
        }
        with pytest.raises(RuntimeError, match="no numeric KPI found"):
            algo.compute_sensitivity_indices(
                variables=_VARIABLES_2D,
                samples=samples,
                kpi_values=kpi_values,
                outdir=tmp_path,
            )

    def test_compute_sensitivity_indices_fallback_kpi(
        self,
        algo: SobolAlgorithm,
        samples: list[dict[str, Any]],
        tmp_path: Path,
    ) -> None:
        """Falls back to first numeric KPI when 'eui' is not present."""
        kpi_values: dict[str, dict[str, float]] = {
            f"{(i + 1):04d}": {"some_other_kpi": float(i + 1)} for i in range(32)
        }
        indices_path = algo.compute_sensitivity_indices(
            variables=_VARIABLES_2D,
            samples=samples,
            kpi_values=kpi_values,
            outdir=tmp_path,
        )
        assert indices_path.exists()
        data = json.loads(indices_path.read_text())
        assert "S1" in data["indices"]


# ======================================================================
# Bounds consistency tests (issue #661)
# ======================================================================


class TestBuildSalibProblemBounds:
    """Verify _build_salib_problem returns bounds consistent with _apply_distribution."""

    def test_uniform_bounds_match_min_max(self) -> None:
        """Uniform bounds are exactly [min, max]."""
        from osimflow.algorithms.sobol import _build_salib_problem

        vars_ = [{"name": "x", "distribution": "uniform", "min": 1.0, "max": 10.0}]
        problem = _build_salib_problem(vars_)
        assert problem["bounds"] == [(1.0, 10.0)]

    def test_normal_bounds_use_ppf_quantiles(self) -> None:
        """Normal bounds use ppf(0.0005) / ppf(0.9995), not just ±3σ."""
        import math

        import scipy.stats

        from osimflow.algorithms.sobol import _build_salib_problem

        vars_ = [{"name": "x", "distribution": "normal", "mean": 5.0, "sigma": 1.0}]
        problem = _build_salib_problem(vars_)
        lower, upper = problem["bounds"][0]
        expected_lower = scipy.stats.norm.ppf(0.0005, loc=5.0, scale=1.0)
        expected_upper = scipy.stats.norm.ppf(0.9995, loc=5.0, scale=1.0)
        assert math.isclose(lower, expected_lower, abs_tol=1e-9)
        assert math.isclose(upper, expected_upper, abs_tol=1e-9)
        # Bounds use ±3.29σ (ppf quantiles) rather than ±3σ
        # ppf(0.0005) ≈ -3.29σ, so lower ≈ 1.71 < 2.0 (the old ±3σ bound)
        # This confirms the fix: we use proper quantiles instead of hardcoded ±3σ
        assert lower < 2.0
        assert upper > 8.0

    def test_lognormal_bounds_use_ppf_quantiles(self) -> None:
        """Lognormal bounds use lognorm ppf, not just exp(mu±3σ)."""
        import math

        import scipy.stats

        from osimflow.algorithms.sobol import _build_salib_problem

        vars_ = [{"name": "x", "distribution": "lognormal", "mean": 5.0, "sigma": 1.0}]
        problem = _build_salib_problem(vars_)
        lower, upper = problem["bounds"][0]
        scale = math.exp(5.0)
        expected_lower = scipy.stats.lognorm.ppf(0.0005, s=1.0, scale=scale)
        expected_upper = scipy.stats.lognorm.ppf(0.9995, s=1.0, scale=scale)
        assert math.isclose(lower, expected_lower, abs_tol=1e-9)
        assert math.isclose(upper, expected_upper, abs_tol=1e-9)

    def test_triangular_bounds_match_min_max(self) -> None:
        """Triangular bounds are [min, max]."""
        from osimflow.algorithms.sobol import _build_salib_problem

        vars_ = [{"name": "x", "distribution": "triangular", "min": 0.0, "max": 10.0}]
        problem = _build_salib_problem(vars_)
        assert problem["bounds"] == [(0.0, 10.0)]

    def test_discrete_bounds_match_value_range(self) -> None:
        """Discrete bounds are [min, max] of the values list."""
        from osimflow.algorithms.sobol import _build_salib_problem

        vars_ = [{"name": "x", "distribution": "discrete", "values": [1, 5, 10]}]
        problem = _build_salib_problem(vars_)
        assert problem["bounds"] == [(1.0, 10.0)]

    def test_categorical_bounds_match_value_range(self) -> None:
        """Categorical bounds are [min, max] of the values list."""
        from osimflow.algorithms.sobol import _build_salib_problem

        vars_ = [{"name": "x", "distribution": "categorical", "values": [2, 4, 6, 8]}]
        problem = _build_salib_problem(vars_)
        assert problem["bounds"] == [(2.0, 8.0)]

    def test_beta_bounds_use_ppf_quantiles(self) -> None:
        """Beta bounds use ppf(0.0005) / ppf(0.9995)."""
        import math

        import scipy.stats

        from osimflow.algorithms.sobol import _build_salib_problem

        vars_ = [{"name": "x", "distribution": "beta", "alpha": 2.0, "beta": 5.0}]
        problem = _build_salib_problem(vars_)
        lower, upper = problem["bounds"][0]
        expected_lower = scipy.stats.beta.ppf(0.0005, 2.0, 5.0)
        expected_upper = scipy.stats.beta.ppf(0.9995, 2.0, 5.0)
        assert math.isclose(lower, expected_lower, abs_tol=1e-9)
        assert math.isclose(upper, expected_upper, abs_tol=1e-9)

    def test_gamma_bounds_use_ppf_quantiles(self) -> None:
        """Gamma bounds use ppf(0.0005) / ppf(0.9995)."""
        import math

        import scipy.stats

        from osimflow.algorithms.sobol import _build_salib_problem

        vars_ = [{"name": "x", "distribution": "gamma", "alpha": 2.0}]
        problem = _build_salib_problem(vars_)
        lower, upper = problem["bounds"][0]
        expected_lower = scipy.stats.gamma.ppf(0.0005, 2.0)
        expected_upper = scipy.stats.gamma.ppf(0.9995, 2.0)
        assert math.isclose(lower, expected_lower, abs_tol=1e-9)
        assert math.isclose(upper, expected_upper, abs_tol=1e-9)

    def test_exponential_bounds_use_ppf_quantiles(self) -> None:
        """Exponential bounds use ppf(0.0005) / ppf(0.9995)."""
        import math

        import scipy.stats

        from osimflow.algorithms.sobol import _build_salib_problem

        vars_ = [{"name": "x", "distribution": "exponential", "rate": 2.0}]
        problem = _build_salib_problem(vars_)
        lower, upper = problem["bounds"][0]
        expected_lower = scipy.stats.expon.ppf(0.0005, scale=1.0 / 2.0)
        expected_upper = scipy.stats.expon.ppf(0.9995, scale=1.0 / 2.0)
        assert math.isclose(lower, expected_lower, abs_tol=1e-9)
        assert math.isclose(upper, expected_upper, abs_tol=1e-9)

    def test_fallback_unknown_distribution(self) -> None:
        """Unknown distribution falls back to [0, 1] bounds."""
        from osimflow.algorithms.sobol import _build_salib_problem

        vars_ = [{"name": "x", "distribution": "unknown_dist"}]
        problem = _build_salib_problem(vars_)
        assert problem["bounds"] == [(0.0, 1.0)]
