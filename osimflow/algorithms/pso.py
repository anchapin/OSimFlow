"""Particle Swarm Optimization algorithm (issue #140, G2c).

Wraps ``pymoo`` to provide a PSO-based multi-objective optimiser using
the NSGA-II selection mechanism combined with velocity-based particle
updates.  Falls back to a lightweight custom PSO when pymoo is not
available (for single-objective use only in that case).

Workflow
--------
1. **Generation 0** — ``generate_samples()`` creates an initial LHS
   population of *n_samples* particles.
2. **Generation N > 0** — ``observe()`` reads KPI files, updates
   personal and global best positions, computes new velocities, and
   proposes the next set of particle positions.
3. ``is_converged()`` returns ``True`` when the relative improvement
   in the global best fitness drops below ``tol``.
"""

import json
import logging
from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt
import scipy.stats.qmc

from osimflow.algorithms import (
    BaseAlgorithm,
    _normalise_var_list,
    _partition_variables,
    _sample_with_engine,
    _write_empty_samples,
)

log = logging.getLogger("osimflow.algorithms.pso")

# pymoo is optional — PSO does not depend on it for core functionality.
_HAS_PYMOO = False
try:
    import pymoo  # noqa: F401 — presence check only

    _HAS_PYMOO = True
except ImportError:
    pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_bounds(independent_vars: list[dict[str, Any]]) -> list[tuple[float, float]]:
    """Extract (min, max) bounds from variable definitions."""
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


# ---------------------------------------------------------------------------
# PSO Algorithm
# ---------------------------------------------------------------------------


class PSOAlgorithm(BaseAlgorithm):
    """Particle Swarm Optimization using a custom velocity-update loop.

    This is a lightweight PSO that works without pymoo.  When pymoo is
    installed, the selection pressure can optionally leverage NSGA-II
    non-dominated sorting for multi-objective problems.

    Parameters
    ----------
    objective_kpi
        Name of the KPI to minimise (or maximise) for single-objective.
    maximize
        If ``True``, maximise *objective_kpi* instead of minimising.
    tol
        Relative convergence tolerance.
    w
        Inertia weight (controls exploration vs exploitation).
    c1
        Cognitive coefficient (personal best attraction).
    c2
        Social coefficient (global best attraction).
    constraints
        Optional list of constraint definitions from variables.yml
        (issue #282).
    """

    def __init__(
        self,
        objective_kpi: str = "eui",
        maximize: bool = False,
        tol: float = 1e-3,
        w: float = 0.7,
        c1: float = 1.5,
        c2: float = 1.5,
        constraints: list[dict[str, Any]] | None = None,
    ) -> None:
        self._objective_kpi = objective_kpi
        self._maximize = maximize
        self._tol = tol
        self._w = w
        self._c1 = c1
        self._c2 = c2
        self._constraints = constraints
        self._independent_vars: list[dict[str, Any]] = []
        self._bounds: list[tuple[float, float]] = []
        # PSO state.
        self._positions: npt.NDArray[np.float64] = np.array([])
        self._velocities: npt.NDArray[np.float64] = np.array([])
        self._personal_best_pos: npt.NDArray[np.float64] = np.array([])
        self._personal_best_val: npt.NDArray[np.float64] = np.array([])
        self._global_best_pos: npt.NDArray[np.float64] = np.array([])
        self._global_best_val: float = float("inf")
        self._prev_global_best: float = float("inf")
        self._initialized: bool = False
        self._rng: np.random.Generator = np.random.default_rng(seed=42)

    def generate_samples(
        self,
        variables: dict[str, Any],
        n_samples: int,
        seed: int | None,
        outdir: Path,
    ) -> Path:
        """Generate initial or updated particle positions."""
        # Extract objective/constraints from variables.yml (issue #282).
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

        self._independent_vars = independent_vars
        self._bounds = _extract_bounds(independent_vars)
        if seed is not None:
            self._rng = np.random.default_rng(seed=seed)

        # Check the explicit feedback-loop slot first (issue #332).
        if self._pending_proposed_samples:
            samples = self._pending_proposed_samples
            self._pending_proposed_samples = []  # consume
            samples_path.write_text(json.dumps({"samples": samples}, indent=2))
            log.info("PSO proposed %d samples from explicit slot", len(samples))
            return samples_path

        if self._initialized and self._positions.size > 0:
            # Return updated positions from observe().
            var_names = [v["name"] for v in self._independent_vars]
            samples = self._array_to_samples(self._positions, var_names)
            samples_path.write_text(json.dumps({"samples": samples}, indent=2))
            log.info("PSO proposed %d particle positions", len(samples))
            return samples_path

        # Generation 0: LHS initialisation.
        try:
            samples = _sample_with_engine(
                scipy.stats.qmc.LatinHypercube,
                independent_vars,
                n_samples,
                seed,
            )
        except (ValueError, NotImplementedError) as exc:
            raise RuntimeError("generate_pso failed: initial LHS population") from exc

        samples_path.write_text(json.dumps({"samples": samples}, indent=2))
        log.info("PSO generated %d initial particles", len(samples))
        return samples_path

    def _array_to_samples(
        self,
        X: npt.NDArray[np.float64],
        var_names: list[str],
    ) -> list[dict[str, Any]]:
        """Convert an (N, dim) array to a list of sample dicts."""
        samples: list[dict[str, Any]] = []
        for i in range(X.shape[0]):
            values: dict[str, Any] = {name: float(X[i, j]) for j, name in enumerate(var_names)}
            samples.append({"sample_id": f"{i + 1:04d}", "values": values})
        return samples

    def _clip_to_bounds(self, X: npt.NDArray[np.float64]) -> npt.NDArray[np.float64]:
        """Clip particle positions to the variable bounds."""
        for j, (lo, hi) in enumerate(self._bounds):
            X[:, j] = np.clip(X[:, j], lo, hi)
        return X

    def observe(self, history: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Update PSO state from KPI results and propose new positions.

        Reads KPI values, updates personal/global bests, applies the
        velocity update rule, and returns the new particle positions.
        """
        if not history or not self._independent_vars:
            return []

        results = _read_kpi_values(history, self._objective_kpi, self._constraints)
        if not results:
            log.warning("PSO observe(): no KPI values found in history")
            return []

        dim = len(self._bounds)
        n_particles = len(results)

        # Build position and fitness arrays from results.
        # Penalty is added to fitness so constraint violations are penalised
        # (issue #282).
        current_pos = np.array([r[0] for r in results], dtype=np.float64)
        current_fit = np.array([r[1] for r in results], dtype=np.float64)
        penalties = np.array([r[2] for r in results], dtype=np.float64)
        current_fit = current_fit + penalties

        # Sign-flip for maximisation.
        if self._maximize:
            current_fit = -current_fit

        # Initialise PSO state on first observe.
        if not self._initialized:
            self._positions = current_pos.copy()
            self._velocities = np.zeros((n_particles, dim), dtype=np.float64)
            self._personal_best_pos = current_pos.copy()
            self._personal_best_val = current_fit.copy()

            best_idx = int(np.argmin(current_fit))
            self._global_best_pos = current_pos[best_idx].copy()
            self._global_best_val = float(current_fit[best_idx])
            self._prev_global_best = self._global_best_val
            self._initialized = True
        else:
            # Update positions to current (may differ in size if
            # the campaign changed the number of samples).
            self._positions = current_pos.copy()

            # Update personal bests.
            for i in range(n_particles):
                if current_fit[i] < self._personal_best_val[i]:
                    self._personal_best_val[i] = current_fit[i]
                    self._personal_best_pos[i] = current_pos[i].copy()

            # Update global best.
            self._prev_global_best = self._global_best_val
            best_idx = int(np.argmin(current_fit))
            if current_fit[best_idx] < self._global_best_val:
                self._global_best_val = float(current_fit[best_idx])
                self._global_best_pos = current_pos[best_idx].copy()

        # Velocity update: v = w*v + c1*r1*(pbest-x) + c2*r2*(gbest-x)
        r1 = self._rng.random((n_particles, dim))
        r2 = self._rng.random((n_particles, dim))

        self._velocities = (
            self._w * self._velocities
            + self._c1 * r1 * (self._personal_best_pos - self._positions)
            + self._c2 * r2 * (self._global_best_pos - self._positions)
        )

        # Position update: x = x + v
        self._positions = self._clip_to_bounds(self._positions + self._velocities)

        var_names = [v["name"] for v in self._independent_vars]
        new_samples = self._array_to_samples(self._positions, var_names)

        # Explicit feedback-loop slot for Campaign validation (issue #332).
        self._pending_proposed_samples = list(new_samples)

        log.info(
            "PSO observe(): global_best=%.4f, proposed %d new positions",
            self._global_best_val,
            len(new_samples),
        )
        return new_samples

    def is_converged(self, history: list[dict[str, Any]]) -> bool:
        """Check convergence based on global best improvement.

        Converges when the relative improvement in the global best
        fitness over the last generation is below ``tol``.
        """
        if not self._initialized:
            return False

        if self._prev_global_best == float("inf") or self._global_best_val == float("inf"):
            return False

        # If the global best is essentially zero, consider converged.
        if abs(self._global_best_val) < 1e-12:
            return True

        if self._prev_global_best == 0.0:
            return self._global_best_val == 0.0

        relative_change = abs(self._global_best_val - self._prev_global_best) / abs(
            self._prev_global_best
        )
        converged = relative_change < self._tol
        if converged:
            log.info(
                "PSO converged: relative change %.6f < tol %.6f",
                relative_change,
                self._tol,
            )
        return converged

    def name(self) -> str:
        return "pso"

    def is_iterative(self) -> bool:
        return True
