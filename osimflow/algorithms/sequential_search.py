"""SequentialSearch (parameter sweep) algorithm (issue #550, GAP-006).

Deterministic parameter sweep with optional adaptive sampling.
SequentialSearch performs a structured sweep across all variable ranges.
When ``adaptive_sampling=True``, it iteratively refines the search space
around the best-performing samples based on observed KPI values.

Workflow
--------
1. **Iteration 0** — ``generate_samples()`` creates an initial deterministic
   grid of samples spanning all variable ranges.
2. **Iteration N > 0** — ``observe()`` reads KPI values from the previous
   iteration, identifies the best-performing samples, and proposes a
   refined grid centred around those regions.
3. ``is_converged()`` returns ``True`` when the relative improvement in
   the best KPI value between iterations falls below ``convergence_threshold``,
   or when ``n_iterations`` is exhausted.
"""

import json
import logging
from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt

from osimflow.algorithms import (
    BaseAlgorithm,
    _normalise_var_list,
    _partition_variables,
    _resolve_conditional,
    _write_empty_samples,
)

log = logging.getLogger("osimflow.algorithms.sequential_search")


def _extract_bounds(
    independent_vars: list[dict[str, Any]],
) -> list[tuple[float, float]]:
    """Extract (min, max) bounds from variable definitions.

    Only *uniform* distributions have natural bounds. For other
    distributions we use a ±3σ or [0, 1] fallback so the algorithm
    has a finite search space.
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
    """Read (params, kpi_value, constraint_penalty) triples from history entries.

    Each history entry has ``samples`` and ``kpi_files``. We read
    each KPI JSON file and extract the objective value and constraint penalty.
    """
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
                        # Compute constraint penalty.
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


def _build_grid_samples(
    bounds: list[tuple[float, float]],
    var_names: list[str],
    n_points_per_dim: int,
    center: npt.NDArray[np.float64] | None = None,
    radius_frac: float = 0.5,
) -> list[dict[str, Any]]:
    """Build a deterministic grid of samples.

    Parameters
    ----------
    bounds
        List of (min, max) tuples per dimension.
    var_names
        Variable names in order.
    n_points_per_dim
        Number of grid points per dimension.
    center
        Optional center point for adaptive refinement. When provided,
        the grid is centred around this point with radius ``radius_frac``.
    radius_frac
        Fraction of the full range to use as radius around centre
        when adaptive sampling (default 0.5 = 50% of range).

    Returns
    -------
    list[dict[str, Any]]
        List of ``{"sample_id": ..., "values": {...}}`` dicts.
    """
    dim = len(bounds)
    if dim == 0:
        return []

    # Build per-dimension grid lines.
    grid_axes: list[npt.NDArray[np.float64]] = []
    for j, (lo, hi) in enumerate(bounds):
        if center is not None:
            # Adaptive grid: centre around the best point with radius.
            rng = (hi - lo) * radius_frac
            c = float(center[j])
            lo_ad = max(lo, c - rng)
            hi_ad = min(hi, c + rng)
            if n_points_per_dim == 1:
                grid_axes.append(np.array([c]))
            else:
                grid_axes.append(np.linspace(lo_ad, hi_ad, n_points_per_dim))
        elif n_points_per_dim == 1:
            # Full-range grid, single point.
            grid_axes.append(np.array([(lo + hi) / 2.0]))
        else:
            # Full-range grid.
            grid_axes.append(np.linspace(lo, hi, n_points_per_dim))

    # Cartesian product of all grid axes.
    cartesian = np.array(list(np.meshgrid(*grid_axes, indexing="ij"))).reshape(dim, -1).T

    samples: list[dict[str, Any]] = []
    for i, point in enumerate(cartesian):
        values: dict[str, Any] = {name: float(point[j]) for j, name in enumerate(var_names)}
        samples.append({"sample_id": f"{i + 1:04d}", "values": values})
    return samples


class SequentialSearchAlgorithm(BaseAlgorithm):
    """Deterministic parameter sweep with optional adaptive refinement.

    SequentialSearch performs a structured sweep across all variable ranges.
    It is *iterative* when ``adaptive_sampling=True``:
    ``is_iterative()`` returns ``True`` and ``is_converged()`` checks for
    convergence based on KPI improvement.

    Without adaptive sampling (``adaptive_sampling=False``), the algorithm
    is effectively a deterministic grid sweep and returns ``is_iterative() == False``.

    Parameters
    ----------
    objective_kpi
        Name of the KPI to use for adaptive refinement (default: "eui").
    maximize
        If ``True``, maximise *objective_kpi* instead of minimising.
    n_iterations
        Maximum number of iterations (generations) for adaptive sampling.
    adaptive_sampling
        If ``True``, enable iterative refinement around best-performing
        samples. If ``False``, run a single deterministic grid sweep.
    convergence_threshold
        Relative improvement threshold for convergence in adaptive mode.
        Converges when ``|prev_best - curr_best| / |prev_best| < threshold``.
    grid_points
        Number of grid points per dimension for the initial sweep and
        subsequent refinements.
    constraints
        Optional list of constraint definitions from variables.yml
        (issue #282). Each constraint is a dict with ``name``, ``max``,
        and optionally ``min``.
    """

    def __init__(
        self,
        objective_kpi: str = "eui",
        maximize: bool = False,
        n_iterations: int = 1,
        adaptive_sampling: bool = False,
        convergence_threshold: float = 1e-3,
        grid_points: int = 3,
        constraints: list[dict[str, Any]] | None = None,
    ) -> None:
        if n_iterations < 1:
            raise ValueError(
                f"n_iterations must be >= 1, got {n_iterations!r}. "
                "Use adaptive_sampling=False for a single deterministic sweep."
            )
        self._objective_kpi = objective_kpi
        self._maximize = maximize
        self._n_iterations = n_iterations
        self._adaptive_sampling = adaptive_sampling
        self._convergence_threshold = convergence_threshold
        self._grid_points = grid_points
        self._constraints = constraints

        self._independent_vars: list[dict[str, Any]] = []
        self._bounds: list[tuple[float, float]] = []
        self._best_params: npt.NDArray[np.float64] = np.array([])
        self._best_value: float = float("inf")
        self._prev_best: float = float("inf")
        self._iteration: int = 0
        self._proposed_samples: list[dict[str, Any]] = []

    def generate_samples(
        self,
        variables: dict[str, Any],
        n_samples: int,
        seed: int | None,
        outdir: Path,
    ) -> Path:
        """Generate samples for the current iteration.

        Iteration 0 creates a full-range deterministic grid.
        Later iterations use adaptive refinement around the best known point.
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

        # Cache for later use in observe / is_converged.
        self._independent_vars = independent_vars
        self._bounds = _extract_bounds(independent_vars)
        var_names = [v["name"] for v in independent_vars]

        # Check the explicit feedback-loop slot first (issue #332).
        if self._proposed_samples:
            samples = self._proposed_samples
            self._proposed_samples = []  # consume
            samples_path.write_text(json.dumps({"samples": samples}, indent=2))
            log.info(
                "SequentialSearch proposed %d samples from explicit slot (iteration %d)",
                len(samples),
                self._iteration,
            )
            return samples_path

        # Build grid samples.
        if self._iteration == 0 or not self._adaptive_sampling:
            # Full-range grid sweep (non-adaptive or first iteration).
            samples = _build_grid_samples(
                self._bounds,
                var_names,
                self._grid_points,
                center=None,
            )
        else:
            # Adaptive refinement around the best known point.
            center = self._best_params if len(self._best_params) > 0 else None
            samples = _build_grid_samples(
                self._bounds,
                var_names,
                self._grid_points,
                center=center,
                radius_frac=0.5,
            )

        if conditional_vars:
            _resolve_conditional(samples, conditional_vars, len(samples))

        samples_path.write_text(json.dumps({"samples": samples}, indent=2))
        log.info(
            "SequentialSearch generated %d samples (iteration %d, adaptive=%s)",
            len(samples),
            self._iteration,
            self._adaptive_sampling,
        )
        return samples_path

    def _update_best(
        self,
        results: list[tuple[list[float], float, float]],
    ) -> None:
        """Update ``_best_params`` and ``_best_value`` from observed results.

        The constraint penalty is added to the effective value so that
        constraint violations are penalised (issue #282).
        """
        self._prev_best = self._best_value
        for params, value, penalty in results:
            eff_value = (-value if self._maximize else value) + penalty
            if eff_value < self._best_value:
                self._best_value = eff_value
                self._best_params = np.array(params, dtype=np.float64)

    def observe(self, history: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Extract KPI values from history, update best point, propose next grid.

        For non-adaptive mode, this is a no-op (returns the last samples).
        For adaptive mode, this reads KPI values, updates the best-so-far
        point, and proposes a refined grid around it for the next iteration.
        """
        if not history:
            return []

        if not self._adaptive_sampling:
            # Non-adaptive: return last samples unchanged.
            last = history[-1].get("samples", [])
            return list(last)

        if not self._independent_vars:
            return []

        results = _read_kpi_values(history, self._objective_kpi, self._constraints)
        if not results:
            log.warning("SequentialSearch observe(): no KPI values found in history")
            return []

        self._update_best(results)

        # Increment iteration counter.
        self._iteration += 1

        # Check if we've exhausted n_iterations.
        if self._iteration >= self._n_iterations:
            log.info(
                "SequentialSearch observe(): exhausted %d iterations (n_iterations=%d)",
                self._iteration,
                self._n_iterations,
            )
            self._proposed_samples = []
            return []

        # Propose adaptive grid around the best known point.
        center = self._best_params if len(self._best_params) > 0 else None
        var_names = [v["name"] for v in self._independent_vars]
        new_samples = _build_grid_samples(
            self._bounds,
            var_names,
            self._grid_points,
            center=center,
            radius_frac=0.5,
        )

        self._proposed_samples = new_samples
        log.info(
            "SequentialSearch observe(): best_value=%.4f, proposed %d new samples (iteration %d/%d)",
            self._best_value,
            len(new_samples),
            self._iteration,
            self._n_iterations,
        )
        return new_samples

    def is_converged(self, history: list[dict[str, Any]]) -> bool:
        """Check convergence based on relative KPI improvement.

        For non-adaptive mode, always returns ``True`` (single-shot).
        For adaptive mode, returns ``True`` when the relative improvement
        in the best objective over the last iteration is below
        ``convergence_threshold``, or when ``n_iterations`` is exhausted.
        """
        if not self._adaptive_sampling:
            return True

        if self._iteration == 0:
            return False

        if self._prev_best == float("inf") or self._best_value == float("inf"):
            return False

        if self._prev_best == 0.0:
            return self._best_value == 0.0

        relative_change = abs(self._best_value - self._prev_best) / abs(self._prev_best)
        converged = relative_change < self._convergence_threshold
        if converged:
            log.info(
                "SequentialSearch converged: relative change %.6f < threshold %.6f",
                relative_change,
                self._convergence_threshold,
            )
        return converged

    def name(self) -> str:
        return "sequential_search"

    def is_iterative(self) -> bool:
        return self._adaptive_sampling
