"""Dual annealing optimizer (issue #125, G2b).

Wraps ``scipy.optimize.dual_annealing`` to iteratively minimise (or
maximise) a scalar KPI (default: EUI).  Dual annealing combines
simulated annealing with a local search, making it effective for
non-convex optimisation landscapes.

Workflow
--------
1. **Generation 0** — ``generate_samples()`` creates an initial set of
   exploration points (LHS-based).
2. **Generation N > 0** — ``observe()`` reads KPI files, updates the
   best-so-far, and proposes new points via dual annealing's
   exploration strategy.
3. ``is_converged()`` returns ``True`` when the relative improvement
   drops below ``tol``.
"""

import json
import logging
from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt
import scipy.stats.qmc
from scipy.optimize import dual_annealing

from osimflow.algorithms import (
    BaseAlgorithm,
    _normalise_var_list,
    _partition_variables,
    _sample_with_engine,
    _write_empty_samples,
)

log = logging.getLogger("osimflow.algorithms.da")


def _extract_bounds(independent_vars: list[dict[str, Any]]) -> list[tuple[float, float]]:
    """Extract (min, max) bounds from variable definitions.

    Only *uniform* distributions have natural bounds.  For other
    distributions we use a ±3σ or [0, 1] fallback so scipy has a
    finite search space.
    """
    bounds: list[tuple[float, float]] = []
    for var_def in independent_vars:
        dist = var_def.get("distribution", "uniform")
        if dist == "uniform":
            bounds.append((float(var_def["min"]), float(var_def["max"])))
        elif dist == "normal":
            mu = float(var_def["mean"])
            sigma = float(var_def["sigma"])
            bounds.append((mu - 3 * sigma, mu + 3 * sigma))
        elif dist == "lognormal":
            log_mu = float(var_def["mean"])
            sigma = float(var_def["sigma"])
            bounds.append((max(1e-10, log_mu - 3 * sigma), log_mu + 3 * sigma))
        elif dist == "triangular":
            bounds.append((float(var_def["min"]), float(var_def["max"])))
        else:
            bounds.append((0.0, 1.0))
    return bounds


def _read_kpi_values(
    history: list[dict[str, Any]],
    objective_kpi: str,
    constraints: list[dict[str, Any]] | None = None,
) -> list[tuple[list[float], float, float]]:
    """Read (params, kpi_value, constraint_penalty) triples from history entries."""
    results: list[tuple[list[float], float, float]] = []
    for entry in history:
        samples: list[dict[str, Any]] = entry.get("samples", [])
        kpi_files: list[str] = entry.get("kpi_files", [])
        for i, sample in enumerate(samples):
            if i < len(kpi_files):
                kpi_path = Path(kpi_files[i])
                if kpi_path.exists():
                    try:
                        kpi_data = json.loads(kpi_path.read_text())
                        kpis = kpi_data.get("kpis", {})
                        value = float(kpis.get(objective_kpi, float("inf")))
                        params = list(sample.get("values", {}).values())
                        penalty = 0.0
                        if constraints:
                            for c in constraints:
                                name = c["name"]
                                val = kpis.get(name, 0.0)
                                max_val = c.get("max", float("inf"))
                                min_val = c.get("min", float("-inf"))
                                if val > max_val or val < min_val:
                                    penalty += 1e9
                        results.append((params, value, penalty))
                    except (json.JSONDecodeError, ValueError, TypeError):
                        continue
    return results


def _propose_samples_around(
    center: npt.NDArray[np.float64],
    n_new: int,
    bounds: list[tuple[float, float]],
    var_names: list[str],
    width: float = 0.15,
) -> list[dict[str, Any]]:
    """Propose *n_new* sample dicts near *center* with Gaussian perturbation.

    Each candidate is clipped to *bounds* and returned as a
    ``{"sample_id": ..., "values": {...}}`` dict.
    """
    dim = len(bounds)
    rng = np.random.default_rng(seed=42)
    new_samples: list[dict[str, Any]] = []
    for i in range(n_new):
        perturbation = rng.normal(0, width, size=dim)
        candidate = center + perturbation
        for j, (lo, hi) in enumerate(bounds):
            candidate[j] = np.clip(candidate[j], lo, hi)
        values: dict[str, Any] = {name: float(candidate[j]) for j, name in enumerate(var_names)}
        new_samples.append({"sample_id": f"{i + 1:04d}", "values": values})
    return new_samples


class DualAnnealingAlgorithm(BaseAlgorithm):
    """Dual annealing optimizer using ``scipy.optimize.dual_annealing``.

    Dual annealing combines classical simulated annealing with a local
    search strategy.  It is effective for non-convex, multimodal
    objective functions and does not require gradient information.

    Parameters
    ----------
    objective_kpi
        Name of the KPI to minimise (or maximise).
    maximize
        If ``True``, maximise *objective_kpi* instead of minimising.
    tol
        Relative convergence tolerance for ``is_converged()``.
    maxiter
        Maximum number of global search iterations per scipy call.
    initial_temp
        Initial temperature for the annealing process.
    restart_temp_ratio
        Temperature ratio at which restart is triggered.
    constraints
        Optional list of constraint definitions from variables.yml
        (issue #282).
    """

    def __init__(
        self,
        objective_kpi: str = "eui",
        maximize: bool = False,
        tol: float = 1e-3,
        maxiter: int = 100,
        initial_temp: float = 5230.0,
        restart_temp_ratio: float = 2e-05,
        constraints: list[dict[str, Any]] | None = None,
    ) -> None:
        self._objective_kpi = objective_kpi
        self._maximize = maximize
        self._tol = tol
        self._maxiter = maxiter
        self._initial_temp = initial_temp
        self._restart_temp_ratio = restart_temp_ratio
        self._constraints = constraints
        self._best_params: npt.NDArray[np.float64] = np.array([])
        self._best_value: float = float("inf")
        self._prev_best: float = float("inf")
        self._independent_vars: list[dict[str, Any]] = []
        self._bounds: list[tuple[float, float]] = []
        # Proposed samples from observe() — used by generate_samples()
        # on subsequent generations (issue #270).
        self._proposed_samples: list[dict[str, Any]] = []

    def generate_samples(
        self,
        variables: dict[str, Any],
        n_samples: int,
        seed: int | None,
        outdir: Path,
    ) -> Path:
        """Generate initial exploration points via LHS.

        Generation 0 creates an LHS population.  Subsequent generations
        use the internal state from ``observe()`` to propose new points
        around the current best.
        """
        # Extract objective direction and constraints from variables.yml
        # (issue #282).
        self._configure_from_variables(variables)
        if self._objective:
            self._objective_kpi = str(self._objective.get("name", self._objective_kpi))
            self._maximize = self._objective.get("direction", "minimize") == "maximize"
        if self._constraints:
            self._constraints = self._constraints

        outdir.mkdir(parents=True, exist_ok=True)
        samples_path = outdir / "samples.json"

        var_list = _normalise_var_list(variables.get("variables", []))
        if not var_list:
            return _write_empty_samples(samples_path)

        independent_vars, conditional_vars = _partition_variables(var_list)
        if not independent_vars:
            return _write_empty_samples(samples_path)

        # Cache for later use.
        self._independent_vars = independent_vars
        self._bounds = _extract_bounds(independent_vars)

        # Check the explicit feedback-loop slot first (issue #332).
        if self._pending_proposed_samples:
            samples = self._pending_proposed_samples
            self._pending_proposed_samples = []  # consume
            self._proposed_samples = []  # also consume the redundant slot
            samples_path.write_text(json.dumps({"samples": samples}, indent=2))
            log.info("Dual annealing proposed %d samples from explicit slot", len(samples))
            return samples_path

        # Fallback to internal _proposed_samples state (legacy path).
        if self._proposed_samples:
            samples = self._proposed_samples
            self._proposed_samples = []  # consume
            samples_path.write_text(json.dumps({"samples": samples}, indent=2))
            log.info("Dual annealing proposed %d samples from optimizer state", len(samples))
            return samples_path

        # Initial population via LHS.
        try:
            samples = _sample_with_engine(
                scipy.stats.qmc.LatinHypercube,
                independent_vars,
                n_samples,
                seed,
            )
        except (ValueError, NotImplementedError) as exc:
            raise RuntimeError("generate_dual_annealing failed: initial LHS population") from exc

        samples_path.write_text(json.dumps({"samples": samples}, indent=2))
        log.info("Dual annealing generated %d initial samples", len(samples))
        return samples_path

    def _update_best(self, results: list[tuple[list[float], float, float]]) -> None:
        """Update ``_best_params`` and ``_best_value`` from observed results.

        Constraint penalty is added to the effective value (issue #282).
        """
        self._prev_best = self._best_value
        for params, value, penalty in results:
            eff_value = (-value if self._maximize else value) + penalty
            if eff_value < self._best_value:
                self._best_value = eff_value
                self._best_params = np.array(params, dtype=np.float64)

    def _run_scipy_da(
        self,
        results: list[tuple[list[float], float, float]],
    ) -> npt.NDArray[np.float64] | None:
        """Run one step of dual annealing and return the best point, or None."""

        def _objective(x: npt.NDArray[np.float64]) -> float:
            best_dist = float("inf")
            best_val = self._best_value
            for p, v, penalty in results:
                dist = float(np.sum((np.array(p) - x) ** 2))
                if dist < best_dist:
                    best_dist = dist
                    eff_v = (-v if self._maximize else v) + penalty
                    best_val = eff_v
            return best_val

        x0 = self._best_params if len(self._best_params) > 0 else None
        try:
            result = dual_annealing(
                _objective,
                self._bounds,
                seed=42,
                maxiter=self._maxiter,
                initial_temp=self._initial_temp,
                restart_temp_ratio=self._restart_temp_ratio,
                x0=x0,
            )
        except Exception as exc:
            log.warning("Dual annealing scipy step failed: %s; using best-so-far", exc)
            return None

        return np.asarray(result.x, dtype=np.float64)

    def observe(self, history: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Extract KPI values, run one dual annealing step, propose new samples."""
        if not history or not self._independent_vars:
            return []

        results = _read_kpi_values(history, self._objective_kpi, self._constraints)
        if not results:
            log.warning("Dual annealing observe(): no KPI values found in history")
            return []

        self._update_best(results)

        # Determine number of new samples.
        n_new = len(history[-1].get("samples", []))
        if n_new == 0:
            n_new = 10

        # Get best point from scipy.
        best_point = self._run_scipy_da(results)
        if best_point is None and len(self._best_params) == 0:
            return []

        center = best_point if best_point is not None else self._best_params
        var_names = [v["name"] for v in self._independent_vars]
        new_samples = _propose_samples_around(center, n_new, self._bounds, var_names, width=0.15)

        # Store proposed samples so generate_samples() can use them
        # on the next call (issue #270). Dual-write: internal state +
        # explicit slot for verifiable observe→generateSamples contract
        # (issue #332).
        self._proposed_samples = new_samples
        # Explicit feedback-loop slot for Campaign validation (issue #332).
        self._pending_proposed_samples = list(new_samples)

        log.info(
            "Dual annealing observe(): best_value=%.4f, proposed %d new samples",
            self._best_value,
            len(new_samples),
        )
        return new_samples

    def is_converged(self, history: list[dict[str, Any]]) -> bool:
        """Check convergence based on relative improvement."""
        if len(history) < 2:
            return False

        if self._prev_best == float("inf") or self._best_value == float("inf"):
            return False

        if self._prev_best == 0.0:
            return self._best_value == 0.0

        relative_change = abs(self._best_value - self._prev_best) / abs(self._prev_best)
        converged = relative_change < self._tol
        if converged:
            log.info(
                "Dual annealing converged: relative change %.6f < tol %.6f",
                relative_change,
                self._tol,
            )
        return converged

    def name(self) -> str:
        return "dual_annealing"

    def is_iterative(self) -> bool:
        return True
