"""Tests for SPEA2Algorithm multi-objective optimizer (issue #271).

Covers:
- _extract_bounds for all distribution types
- _read_multi_kpi_values
- generate_samples (gen 0 LHS population, edge cases)
- _array_to_samples
- _sign_objectives (weights and maximize/minimize)
- observe() with pymoo (if installed)
- _update_hypervolume
- is_converged (hypervolume convergence)
- name, is_iterative, is_multi_objective
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from osimflow.algorithms.spea2 import (
    SPEA2Algorithm,
    _extract_bounds,
    _read_multi_kpi_values,
)

_HAS_PYMOO = True
try:
    import pymoo  # noqa: F401
except ImportError:
    _HAS_PYMOO = False

pymoo_required = pytest.mark.skipif(not _HAS_PYMOO, reason="pymoo not installed")


# ======================================================================
# _extract_bounds
# ======================================================================


class TestExtractBounds:
    def test_uniform(self) -> None:
        vars_def = [{"name": "x", "distribution": "uniform", "min": 1.0, "max": 10.0}]
        assert _extract_bounds(vars_def) == [(1.0, 10.0)]

    def test_normal(self) -> None:
        vars_def = [{"name": "x", "distribution": "normal", "mean": 5.0, "sigma": 1.0}]
        bounds = _extract_bounds(vars_def)
        assert bounds == [(2.0, 8.0)]  # mean ± 3σ

    def test_lognormal(self) -> None:
        vars_def = [{"name": "x", "distribution": "lognormal", "mean": 5.0, "sigma": 1.0}]
        bounds = _extract_bounds(vars_def)
        assert len(bounds) == 1
        assert bounds[0][1] == 8.0  # 5 + 3*1

    def test_triangular(self) -> None:
        vars_def = [{"name": "x", "distribution": "triangular", "min": 0.0, "max": 10.0}]
        assert _extract_bounds(vars_def) == [(0.0, 10.0)]

    def test_unknown_fallback(self) -> None:
        vars_def = [{"name": "x", "distribution": "custom"}]
        assert _extract_bounds(vars_def) == [(0.0, 1.0)]

    def test_mixed(self) -> None:
        vars_def = [
            {"name": "a", "distribution": "uniform", "min": 1.0, "max": 5.0},
            {"name": "b", "distribution": "normal", "mean": 10.0, "sigma": 2.0},
        ]
        bounds = _extract_bounds(vars_def)
        assert len(bounds) == 2


# ======================================================================
# _read_multi_kpi_values
# ======================================================================


class TestReadMultiKpiValues:
    def test_basic(self, tmp_path: Path) -> None:
        kpi_file = tmp_path / "kpi_0001.json"
        kpi_file.write_text(json.dumps({"kpis": {"eui": 100.0, "cost": 50.0}}))
        history = [
            {
                "samples": [{"values": {"x1": 0.5}}],
                "kpi_files": [str(kpi_file)],
            }
        ]
        results = _read_multi_kpi_values(history, ["eui", "cost"])
        assert len(results) == 1
        params, obj_values, penalty = results[0]
        assert params == [0.5]
        assert obj_values == [100.0, 50.0]
        assert penalty == 0.0

    def test_missing_kpi_file(self, tmp_path: Path) -> None:
        history = [
            {
                "samples": [{"values": {"x1": 0.5}}],
                "kpi_files": [str(tmp_path / "nonexistent.json")],
            }
        ]
        results = _read_multi_kpi_values(history, ["eui"])
        assert len(results) == 0

    def test_constraint_violation(self, tmp_path: Path) -> None:
        kpi_file = tmp_path / "kpi_0001.json"
        kpi_file.write_text(json.dumps({"kpis": {"eui": 100.0, "peak": 200.0}}))
        history = [
            {
                "samples": [{"values": {"x1": 0.5}}],
                "kpi_files": [str(kpi_file)],
            }
        ]
        constraints = [{"name": "peak", "max": 150.0}]
        results = _read_multi_kpi_values(history, ["eui"], constraints)
        assert len(results) == 1
        assert results[0][2] == 1e9  # penalty

    def test_constraint_satisfied(self, tmp_path: Path) -> None:
        kpi_file = tmp_path / "kpi_0001.json"
        kpi_file.write_text(json.dumps({"kpis": {"eui": 100.0, "peak": 100.0}}))
        history = [
            {
                "samples": [{"values": {"x1": 0.5}}],
                "kpi_files": [str(kpi_file)],
            }
        ]
        constraints = [{"name": "peak", "max": 150.0}]
        results = _read_multi_kpi_values(history, ["eui"], constraints)
        assert results[0][2] == 0.0

    def test_empty_history(self) -> None:
        assert _read_multi_kpi_values([], ["eui"]) == []

    def test_malformed_kpi(self, tmp_path: Path) -> None:
        kpi_file = tmp_path / "kpi_bad.json"
        kpi_file.write_text("NOT JSON")
        history = [
            {
                "samples": [{"values": {"x1": 0.5}}],
                "kpi_files": [str(kpi_file)],
            }
        ]
        results = _read_multi_kpi_values(history, ["eui"])
        assert len(results) == 0


# ======================================================================
# SPEA2Algorithm — generate_samples
# ======================================================================


_VARIABLES_2D: dict[str, Any] = {
    "variables": [
        {"name": "wall_r", "distribution": "uniform", "min": 1.0, "max": 10.0},
        {"name": "window_shgc", "distribution": "uniform", "min": 0.1, "max": 0.9},
    ]
}


class TestGenerateSamples:
    def test_gen0_lhs_population(self, tmp_path: Path) -> None:
        algo = SPEA2Algorithm()
        result = algo.generate_samples(_VARIABLES_2D, n_samples=8, seed=42, outdir=tmp_path)
        data = json.loads(result.read_text())
        assert len(data["samples"]) == 8
        for s in data["samples"]:
            assert "wall_r" in s["values"]
            assert "window_shgc" in s["values"]

    def test_empty_variables(self, tmp_path: Path) -> None:
        algo = SPEA2Algorithm()
        result = algo.generate_samples({"variables": []}, n_samples=5, seed=42, outdir=tmp_path)
        data = json.loads(result.read_text())
        assert data["samples"] == []

    def test_no_independent_variables(self, tmp_path: Path) -> None:
        algo = SPEA2Algorithm()
        variables: dict[str, Any] = {
            "variables": [
                {"name": "x", "distribution": "conditional", "depends_on": {"variable": "y"}},
            ]
        }
        result = algo.generate_samples(variables, n_samples=5, seed=42, outdir=tmp_path)
        data = json.loads(result.read_text())
        assert data["samples"] == []

    def test_seed_reproducible(self, tmp_path: Path) -> None:
        algo = SPEA2Algorithm()
        r1 = algo.generate_samples(_VARIABLES_2D, n_samples=6, seed=123, outdir=tmp_path / "r1")
        r2 = algo.generate_samples(_VARIABLES_2D, n_samples=6, seed=123, outdir=tmp_path / "r2")
        assert json.loads(r1.read_text()) == json.loads(r2.read_text())

    def test_creates_outdir(self, tmp_path: Path) -> None:
        algo = SPEA2Algorithm()
        nested = tmp_path / "deep" / "nested"
        algo.generate_samples(_VARIABLES_2D, n_samples=4, seed=0, outdir=nested)
        assert nested.is_dir()


# ======================================================================
# SPEA2Algorithm — _array_to_samples
# ======================================================================


class TestArrayToSamples:
    def test_conversion(self) -> None:
        algo = SPEA2Algorithm()
        X = np.array([[1.0, 2.0], [3.0, 4.0]])
        samples = algo._array_to_samples(X, ["a", "b"])
        assert len(samples) == 2
        assert samples[0]["values"] == {"a": 1.0, "b": 2.0}
        assert samples[1]["values"] == {"a": 3.0, "b": 4.0}
        assert samples[0]["sample_id"] == "0001"


# ======================================================================
# SPEA2Algorithm — _sign_objectives
# ======================================================================


class TestSignObjectives:
    def test_minimize(self) -> None:
        algo = SPEA2Algorithm(objective_kpis=["eui", "cost"], maximize=[False, False])
        F = np.array([[100.0, 50.0], [80.0, 60.0]])
        F_signed = algo._sign_objectives(F)
        np.testing.assert_array_equal(F_signed, F)

    def test_maximize_flips_sign(self) -> None:
        algo = SPEA2Algorithm(objective_kpis=["eui", "cost"], maximize=[True, False])
        F = np.array([[100.0, 50.0]])
        F_signed = algo._sign_objectives(F)
        np.testing.assert_array_equal(F_signed, [[-100.0, 50.0]])

    def test_weights_applied(self) -> None:
        algo = SPEA2Algorithm(
            objective_kpis=["eui", "cost"], maximize=[False, False], weights=[2.0, 1.0]
        )
        F = np.array([[100.0, 50.0]])
        F_signed = algo._sign_objectives(F)
        np.testing.assert_array_equal(F_signed, [[200.0, 50.0]])


# ======================================================================
# SPEA2Algorithm — observe (requires pymoo)
# ======================================================================


@pymoo_required
class TestObserve:
    def _make_history(self, tmp_path: Path, n: int = 10) -> list[dict[str, Any]]:
        """Create a history with n samples + KPI files."""
        samples: list[dict[str, Any]] = []
        kpi_files: list[str] = []
        for i in range(n):
            x1 = 1.0 + 9.0 * (i / n)
            x2 = 0.1 + 0.8 * ((i % 3) / 3)
            samples.append(
                {"sample_id": f"{i + 1:04d}", "values": {"wall_r": x1, "window_shgc": x2}}
            )
            kpi_path = tmp_path / f"kpi_{i + 1:04d}.json"
            # Simple synthetic KPIs: eui increases with x1, cost decreases.
            eui = 100.0 + x1 * 5
            cost = 200.0 - x1 * 3
            kpi_path.write_text(json.dumps({"kpis": {"eui": eui, "cost": cost}}))
            kpi_files.append(str(kpi_path))
        return [{"samples": samples, "kpi_files": kpi_files}]

    def test_observe_returns_samples(self, tmp_path: Path) -> None:
        algo = SPEA2Algorithm(objective_kpis=["eui", "cost"], pop_size=6)
        algo.generate_samples(_VARIABLES_2D, n_samples=10, seed=42, outdir=tmp_path / "gen0")
        history = self._make_history(tmp_path, 10)
        result = algo.observe(history)
        # observe() may return new proposed samples from SPEA-II
        assert isinstance(result, list)

    def test_observe_empty_history(self) -> None:
        algo = SPEA2Algorithm()
        assert algo.observe([]) == []

    def test_observe_no_vars(self, tmp_path: Path) -> None:
        algo = SPEA2Algorithm()
        # Don't call generate_samples first, so _independent_vars is empty
        assert algo.observe([{"samples": [], "kpi_files": []}]) == []


# ======================================================================
# SPEA2Algorithm — _update_hypervolume
# ======================================================================


@pymoo_required
class TestUpdateHypervolume:
    def test_empty_F(self) -> None:
        algo = SPEA2Algorithm()
        algo._update_hypervolume(np.array([]).reshape(0, 2))
        assert algo._hv_history[-1] == 0.0

    def test_with_values(self) -> None:
        algo = SPEA2Algorithm()
        F = np.array([[1.0, 2.0], [3.0, 1.0]])
        algo._update_hypervolume(F)
        assert len(algo._hv_history) == 1
        assert algo._hv_history[0] > 0.0


# ======================================================================
# SPEA2Algorithm — is_converged
# ======================================================================


class TestIsConverged:
    def test_no_history(self) -> None:
        algo = SPEA2Algorithm()
        assert algo.is_converged([]) is False

    def test_one_entry(self) -> None:
        algo = SPEA2Algorithm()
        algo._hv_history = [1.0]
        assert algo.is_converged([]) is False

    def test_converged(self) -> None:
        algo = SPEA2Algorithm(hv_tol=1e-3)
        algo._hv_history = [100.0, 100.001]  # tiny change
        assert algo.is_converged([]) is True

    def test_not_converged(self) -> None:
        algo = SPEA2Algorithm(hv_tol=1e-3)
        algo._hv_history = [100.0, 110.0]  # 10% change
        assert algo.is_converged([]) is False

    def test_prev_zero_current_zero(self) -> None:
        algo = SPEA2Algorithm()
        algo._hv_history = [0.0, 0.0]
        assert algo.is_converged([]) is True

    def test_prev_zero_current_nonzero(self) -> None:
        algo = SPEA2Algorithm()
        algo._hv_history = [0.0, 1.0]
        assert algo.is_converged([]) is False


# ======================================================================
# SPEA2Algorithm — metadata methods
# ======================================================================


class TestMetadata:
    def test_name(self) -> None:
        assert SPEA2Algorithm().name() == "spea2"

    def test_is_iterative(self) -> None:
        assert SPEA2Algorithm().is_iterative() is True

    def test_is_multi_objective(self) -> None:
        assert SPEA2Algorithm().is_multi_objective() is True
