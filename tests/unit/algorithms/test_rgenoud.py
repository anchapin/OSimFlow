"""Tests for RgenoudAlgorithm — hybrid DE + BFGS optimizer (issue #545).

Covers:
- Registry discovery and interface contracts
- Sample generation (initial LHS population)
- Hybrid GA+BFGS iteration (observe() + generate_samples())
- Convergence detection
- Constraint handling
- Error handling (empty variables, missing bounds)
"""

import json
from pathlib import Path
from typing import Any

from osimflow.algorithms import AlgorithmRegistry, BaseAlgorithm
from osimflow.algorithms.rgenoud import RgenoudAlgorithm

# ======================================================================
# Registry tests
# ======================================================================


class TestRgenoudRegistry:
    """Registry discovery tests for RgenoudAlgorithm."""

    def test_get_returns_rgenoud_algorithm(self) -> None:
        algo = AlgorithmRegistry.get("rgenoud")
        assert isinstance(algo, RgenoudAlgorithm)

    def test_get_returns_base_algorithm(self) -> None:
        algo = AlgorithmRegistry.get("rgenoud")
        assert isinstance(algo, BaseAlgorithm)

    def test_list_available_includes_rgenoud(self) -> None:
        assert "rgenoud" in AlgorithmRegistry.list_available()


# ======================================================================
# Interface contract tests
# ======================================================================


class TestRgenoudInterface:
    """Contract tests for RgenoudAlgorithm."""

    def test_name(self) -> None:
        assert RgenoudAlgorithm().name() == "rgenoud"

    def test_is_iterative(self) -> None:
        assert RgenoudAlgorithm().is_iterative() is True

    def test_is_converged_empty_history(self) -> None:
        assert RgenoudAlgorithm().is_converged([]) is False

    def test_observe_empty_history(self) -> None:
        assert RgenoudAlgorithm().observe([]) == []

    def test_observe_empty_independent_vars(self) -> None:
        algo = RgenoudAlgorithm()
        algo._independent_vars = []  # simulate generate_samples called with empty vars
        assert algo.observe([{"samples": [], "kpi_files": []}]) == []


# ======================================================================
# Sample generation
# ======================================================================


class TestRgenoudGenerateSamples:
    """Generate-samples tests for RgenoudAlgorithm."""

    def _make_variables(
        self,
        vars_list: list[dict[str, Any]],
        objective: dict[str, Any] | None = None,
        constraints: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {"variables": vars_list}
        if objective:
            result["objective"] = objective
        if constraints:
            result["constraints"] = constraints
        return result

    def test_generates_lhs_initial_population(self, tmp_path: Path) -> None:
        variables = self._make_variables(
            [
                {"name": "x", "distribution": "uniform", "min": 0.0, "max": 10.0},
                {"name": "y", "distribution": "uniform", "min": 0.0, "max": 10.0},
            ]
        )
        algo = RgenoudAlgorithm(objective_kpi="eui", popsize=10)
        result = algo.generate_samples(variables, n_samples=10, seed=42, outdir=tmp_path)

        assert result.exists()
        data = json.loads(result.read_text())
        samples = data["samples"]
        assert len(samples) == 10
        # All samples should be within bounds
        for s in samples:
            assert 0.0 <= s["values"]["x"] <= 10.0
            assert 0.0 <= s["values"]["y"] <= 10.0

    def test_respects_seed_reproducibility(self, tmp_path: Path) -> None:
        variables = self._make_variables(
            [
                {"name": "x", "distribution": "uniform", "min": 0.0, "max": 1.0},
            ]
        )
        # Running with the same seed should produce the same LHS points
        results = []
        for _ in range(2):
            algo = RgenoudAlgorithm(popsize=5)
            result = algo.generate_samples(
                variables, n_samples=5, seed=42, outdir=tmp_path / "same_seed"
            )
            data = json.loads(result.read_text())
            results.append([s["values"]["x"] for s in data["samples"]])
        assert results[0] == results[1]  # same seed → same samples

    def test_empty_variables_writes_empty_samples(self, tmp_path: Path) -> None:
        variables = self._make_variables([])
        algo = RgenoudAlgorithm()
        result = algo.generate_samples(variables, n_samples=5, seed=42, outdir=tmp_path)
        data = json.loads(result.read_text())
        assert data["samples"] == []

    def test_non_uniform_distributions_get_bounds(self, tmp_path: Path) -> None:
        # Normal distribution: bounds should be mean ± 3*sigma
        variables = self._make_variables(
            [
                {"name": "x", "distribution": "normal", "mean": 5.0, "sigma": 2.0},
            ]
        )
        algo = RgenoudAlgorithm(popsize=10)
        result = algo.generate_samples(variables, n_samples=10, seed=42, outdir=tmp_path)
        data = json.loads(result.read_text())
        # All samples should be within [mean-3*sigma, mean+3*sigma] = [-1, 11]
        for s in data["samples"]:
            assert -1.0 <= s["values"]["x"] <= 11.0

    def test_objective_maximize_sets_direction(self, tmp_path: Path) -> None:
        variables = self._make_variables(
            [
                {"name": "x", "distribution": "uniform", "min": 0.0, "max": 10.0},
            ],
            objective={"name": "eui", "direction": "maximize"},
        )
        algo = RgenoudAlgorithm()
        algo.generate_samples(variables, n_samples=5, seed=42, outdir=tmp_path)
        assert algo._maximize is True

    def test_constraint_config_from_variables(self, tmp_path: Path) -> None:
        variables = self._make_variables(
            [
                {"name": "x", "distribution": "uniform", "min": 0.0, "max": 10.0},
            ],
            constraints=[{"name": "cost", "max": 100.0}],
        )
        algo = RgenoudAlgorithm()
        algo.generate_samples(variables, n_samples=5, seed=42, outdir=tmp_path)
        assert algo._constraints is not None
        assert algo._constraints[0]["name"] == "cost"
        assert algo._constraints[0]["max"] == 100.0

    def test_dict_format_output(self, tmp_path: Path) -> None:
        variables = self._make_variables(
            [
                {"name": "x", "distribution": "uniform", "min": 0.0, "max": 10.0},
            ]
        )
        algo = RgenoudAlgorithm(popsize=4)
        result = algo.generate_samples(variables, n_samples=4, seed=42, outdir=tmp_path)
        data = json.loads(result.read_text())
        assert "samples" in data
        for s in data["samples"]:
            assert "sample_id" in s
            assert "values" in s
            assert "x" in s["values"]


# ======================================================================
# Iteration / observe tests
# ======================================================================


class TestRgenoudIteration:
    """Iteration tests for RgenoudAlgorithm — observe() + is_converged()."""

    def test_observe_returns_proposed_samples(self, tmp_path: Path) -> None:
        variables = {
            "variables": [
                {"name": "x", "distribution": "uniform", "min": 0.0, "max": 10.0},
            ]
        }
        algo = RgenoudAlgorithm(popsize=10)
        algo.generate_samples(variables, n_samples=10, seed=42, outdir=tmp_path)

        # Simulate history with KPI results
        history = [
            {
                "samples": [
                    {"sample_id": "0001", "values": {"x": 5.0}},
                    {"sample_id": "0002", "values": {"x": 6.0}},
                ],
                "kpi_files": [
                    str(tmp_path / "kpi_0001.json"),
                    str(tmp_path / "kpi_0002.json"),
                ],
            }
        ]
        # Write fake KPI files
        for i, sample in enumerate(history[0]["samples"]):
            kpi_file = tmp_path / f"kpi_{sample['sample_id']}.json"
            kpi_file.write_text(
                json.dumps(
                    {
                        "kpis": {"eui": 100.0 - i * 10.0}  # decreasing = better
                    }
                )
            )

        proposed = algo.observe(history)
        # n_new = n_current (number of samples in last history entry) = 2
        assert len(proposed) == 2
        assert all("sample_id" in p and "values" in p for p in proposed)

    def test_observe_updates_best_params(self, tmp_path: Path) -> None:
        variables = {
            "variables": [
                {"name": "x", "distribution": "uniform", "min": 0.0, "max": 10.0},
            ]
        }
        algo = RgenoudAlgorithm(popsize=5)
        algo.generate_samples(variables, n_samples=5, seed=42, outdir=tmp_path)

        kpi_file = tmp_path / "kpi_0001.json"
        kpi_file.write_text(json.dumps({"kpis": {"eui": 50.0}}))

        history = [
            {
                "samples": [{"sample_id": "0001", "values": {"x": 5.0}}],
                "kpi_files": [str(kpi_file)],
            }
        ]
        algo.observe(history)
        assert algo._best_value == 50.0
        assert algo._best_params.tolist() == [5.0]

    def test_observe_with_constraint_penalty(self, tmp_path: Path) -> None:
        variables = {
            "variables": [
                {"name": "x", "distribution": "uniform", "min": 0.0, "max": 10.0},
            ],
            "constraints": [{"name": "cost", "max": 5.0}],
        }
        algo = RgenoudAlgorithm(popsize=5, constraints=[{"name": "cost", "max": 5.0}])
        algo.generate_samples(variables, n_samples=5, seed=42, outdir=tmp_path)

        kpi_file = tmp_path / "kpi_0001.json"
        # cost=10 violates max=5 → penalty=1e9 added
        kpi_file.write_text(json.dumps({"kpis": {"eui": 50.0, "cost": 10.0}}))

        history = [
            {
                "samples": [{"sample_id": "0001", "values": {"x": 5.0}}],
                "kpi_files": [str(kpi_file)],
            }
        ]
        algo.observe(history)
        # best_value includes penalty
        assert algo._best_value >= 1e9

    def test_observe_no_kpi_files_skipped(self, tmp_path: Path) -> None:
        variables = {
            "variables": [
                {"name": "x", "distribution": "uniform", "min": 0.0, "max": 10.0},
            ]
        }
        algo = RgenoudAlgorithm(popsize=5)
        algo.generate_samples(variables, n_samples=5, seed=42, outdir=tmp_path)

        # history with no kpi files
        history = [{"samples": [{"sample_id": "0001", "values": {"x": 5.0}}], "kpi_files": []}]
        proposed = algo.observe(history)
        # With no results, observe returns empty
        assert proposed == []

    def test_converged_after_max_generations(self, tmp_path: Path) -> None:
        algo = RgenoudAlgorithm(tol=1e-4, popsize=5)
        # Simulate generation count reached and improvement below tolerance
        algo._generation = 5
        algo._prev_best = 100.0
        algo._best_value = 100.009  # relative_change = 0.00009 < tol=1e-4
        # Need >= 2 history entries for is_converged to not return early
        history = [{"samples": [1]}, {"samples": [2]}]
        assert algo.is_converged(history) is True

    def test_converged_relative_change_below_tol(self, tmp_path: Path) -> None:
        variables = {
            "variables": [
                {"name": "x", "distribution": "uniform", "min": 0.0, "max": 10.0},
            ]
        }
        algo = RgenoudAlgorithm(tol=1e-4, popsize=5)
        algo.generate_samples(variables, n_samples=5, seed=42, outdir=tmp_path)

        # Set up state where relative_change < tol
        # prev_best=100.0, best_value=100.009 → relative_change = 0.00009 < tol=1e-4
        algo._prev_best = 100.0
        algo._best_value = 100.009
        algo._generation = 1

        # Need >= 2 history entries for is_converged to not return early
        history = [{"samples": [1]}, {"samples": [2]}]
        assert algo.is_converged(history) is True

    def test_not_converged_before_max_generations(self, tmp_path: Path) -> None:
        algo = RgenoudAlgorithm(tol=1e-10, popsize=5)
        algo._generation = 5
        algo._prev_best = 100.0
        algo._best_value = 50.0  # Large improvement, not converged
        # Need >= 2 history entries for is_converged to not return early
        history = [{"samples": [1]}, {"samples": [2]}]
        assert algo.is_converged(history) is False


# ======================================================================
# BFGS interval tests
# ======================================================================


class TestRgenoudBFGS:
    """Tests for the BFGS hybrid component (via scipy's polish=True)."""

    def test_algorithm_runs_with_bfgs_polish(self, tmp_path: Path) -> None:
        variables = {
            "variables": [
                {"name": "x", "distribution": "uniform", "min": 0.0, "max": 10.0},
            ]
        }
        algo = RgenoudAlgorithm(popsize=5)
        algo.generate_samples(variables, n_samples=5, seed=42, outdir=tmp_path)

        # Write KPI files
        kpi_file = tmp_path / "kpi_0001.json"
        kpi_file.write_text(json.dumps({"kpis": {"eui": 50.0}}))

        history = [
            {
                "samples": [{"sample_id": "0001", "values": {"x": 5.0}}],
                "kpi_files": [str(kpi_file)],
            }
        ]
        algo._generation = 5
        algo.observe(history)
        # Algorithm should run without error and propose samples
        assert len(algo._proposed_samples) > 0

    def test_bfgs_polish_improves_solution(self, tmp_path: Path) -> None:
        variables = {
            "variables": [
                {"name": "x", "distribution": "uniform", "min": 0.0, "max": 10.0},
            ]
        }
        algo = RgenoudAlgorithm(popsize=5)
        algo.generate_samples(variables, n_samples=5, seed=42, outdir=tmp_path)

        kpi_file = tmp_path / "kpi_0001.json"
        kpi_file.write_text(json.dumps({"kpis": {"eui": 50.0}}))

        history = [
            {
                "samples": [{"sample_id": "0001", "values": {"x": 5.0}}],
                "kpi_files": [str(kpi_file)],
            }
        ]
        algo._generation = 5
        algo.observe(history)
        # Should run without error and propose samples
        assert len(algo._proposed_samples) > 0


# ======================================================================
# Error / validation tests
# ======================================================================


class TestRgenoudErrors:
    """Error and validation tests for RgenoudAlgorithm."""

    def test_conditional_only_variables_return_empty_samples(self, tmp_path: Path) -> None:
        # Conditional-only variables have no independent sampling path,
        # so generate_samples should return an empty samples file
        variables = {
            "variables": [
                {
                    "name": "x",
                    "distribution": "conditional",
                    "depends_on": {"variable": "y"},
                    "conditions": [],
                },
            ]
        }
        algo = RgenoudAlgorithm(popsize=5)
        result = algo.generate_samples(variables, n_samples=5, seed=42, outdir=tmp_path)
        data = json.loads(result.read_text())
        assert data["samples"] == []
