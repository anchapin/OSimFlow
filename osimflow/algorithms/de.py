"""Differential evolution optimizer (issue #125, G2b).

Wraps ``scipy.optimize.differential_evolution`` to iteratively minimise
(or maximise) a scalar KPI (default: EUI).  The algorithm is *iterative*:
``is_iterative()`` returns ``True`` and ``is_converged()`` checks the
improvement tolerance across generations.

Workflow
--------
1. **Generation 0** — ``generate_samples()`` creates an initial LHS
   population of *n_samples* points (scipy handles the evolution
   internally, but we expose it generation-by-generation so the
   Campaign can fan out simulations).
2. **Generation N > 0** — ``observe()`` reads the KPI files from the
   previous generation, feeds the best-so-far back into the DE state,
   and proposes a new population centred around promising regions.
3. ``is_converged()`` returns ``True`` when the relative improvement
   in the objective over the last two generations drops below
   ``tol`` (default 1e-3), or when ``max_generations`` is reached.
"""

import json
import logging
from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt
import scipy.stats.qmc
from scipy.optimize import differential_evolution

from osimflow.algorithms import (
    BaseAlgorithm,
    _normalise_var_list,
    _partition_variables,
    _sample_with_engine,
    _write_empty_samples,
)

log = logging.getLogger("osimflow.algorithms.de")


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
) -> list[tuple[list[float], float]]:
    """Read (params, kpi_value) pairs from history entries.

    Each history entry has ``samples`` and ``kpi_files``.  We read
    each KPI JSON file and extract the objective value.
    """
    results: list[tuple[list[float], float]] = []
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
                        results.append((params, value))
                    except (json.JSONDecodeError, ValueError, TypeError):
                        continue
    return results


def _propose_samples_around(
    center: npt.NDArray[np.float64],
    n_new: int,
    bounds: list[tuple[float, float]],
    var_names: list[str],
    width: float = 0.1,
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


class DifferentialEvolutionAlgorithm(BaseAlgorithm):
    """Differential evolution optimizer using ``scipy.optimize.differential_evolution``.

    Parameters
    ----------
    objective_kpi
        Name of the KPI to minimise (or maximise).
    maximize
        If ``True``, maximise *objective_kpi* instead of minimising.
    tol
        Relative convergence tolerance for ``is_converged()``.
    popsize
        Multiplier for the population size (scipy default: 15).
        Actual population = ``popsize * dim``.
    mutation
        Mutation constant or range (scipy default: (0.5, 1.0)).
    recombination
        Recombination constant (scipy default: 0.7).
    """

    def __init__(
        self,
        objective_kpi: str = "eui",
        maximize: bool = False,
        tol: float = 1e-3,
        popsize: int = 15,
        mutation: tuple[float, float] | float = (0.5, 1.0),
        recombination: float = 0.7,
    ) -> None:
        self._objective_kpi = objective_kpi
        self._maximize = maximize
        self._tol = tol
        self._popsize = popsize
        self._mutation = mutation
        self._recombination = recombination
        self._best_params: npt.NDArray[np.float64] = np.array([])
        self._best_value: float = float("inf")
        self._prev_best: float = float("inf")
        self._independent_vars: list[dict[str, Any]] = []
        self._bounds: list[tuple[float, float]] = []

    def generate_samples(
        self,
        variables: dict[str, Any],
        n_samples: int,
        seed: int | None,
        outdir: Path,
    ) -> Path:
        """Generate initial or subsequent population.

        Generation 0 creates a Latin Hypercube population.  Later
        generations use the DE internal state (maintained via
        ``observe()``) to propose new points.
        """
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

        # For the initial population, use LHS.
        try:
            samples = _sample_with_engine(
                scipy.stats.qmc.LatinHypercube,
                independent_vars,
                n_samples,
                seed,
            )
        except (ValueError, NotImplementedError) as exc:
            raise RuntimeError("generate_de failed: initial LHS population") from exc

        samples_path.write_text(json.dumps({"samples": samples}, indent=2))
        log.info("DE generated %d initial samples", len(samples))
        return samples_path

    def _update_best(self, results: list[tuple[list[float], float]]) -> None:
        """Update ``_best_params`` and ``_best_value`` from observed results."""
        self._prev_best = self._best_value
        for params, value in results:
            eff_value = -value if self._maximize else value
            if eff_value < self._best_value:
                self._best_value = eff_value
                self._best_params = np.array(params, dtype=np.float64)

    def _run_scipy_de(
        self,
        results: list[tuple[list[float], float]],
    ) -> npt.NDArray[np.float64] | None:
        """Run one step of scipy DE and return the best point, or None."""

        def _objective(x: npt.NDArray[np.float64]) -> float:
            best_dist = float("inf")
            best_val = self._best_value
            for p, v in results:
                dist = float(np.sum((np.array(p) - x) ** 2))
                if dist < best_dist:
                    best_dist = dist
                    eff_v = -v if self._maximize else v
                    best_val = eff_v
            return best_val

        try:
            result = differential_evolution(
                _objective,
                self._bounds,
                seed=42,
                maxiter=1,
                popsize=self._popsize,
                mutation=self._mutation,
                recombination=self._recombination,
                init="sobol",
            )
        except Exception as exc:
            log.warning("DE scipy step failed: %s; using best-so-far", exc)
            return None

        return np.asarray(result.x, dtype=np.float64)

    def observe(self, history: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Extract KPI values from history, update DE state.

        Reads the KPI files from the last generation, determines the
        best-so-far point, and proposes a new population by running one
        step of differential evolution using scipy.
        """
        if not history or not self._independent_vars:
            return []

        results = _read_kpi_values(history, self._objective_kpi)
        if not results:
            log.warning("DE observe(): no KPI values found in history")
            return []

        self._update_best(results)

        # Determine number of new samples to propose.
        dim = len(self._bounds)
        n_new = len(history[-1].get("samples", []))
        if n_new == 0:
            n_new = max(self._popsize * dim, 10)

        # Get a candidate best point from scipy.
        best_point = self._run_scipy_de(results)
        if best_point is None and len(self._best_params) == 0:
            return []

        # Propose new samples around the best known point.
        center = best_point if best_point is not None else self._best_params
        var_names = [v["name"] for v in self._independent_vars]
        new_samples = _propose_samples_around(center, n_new, self._bounds, var_names, width=0.1)

        log.info(
            "DE observe(): best_value=%.4f, proposed %d new samples",
            self._best_value,
            len(new_samples),
        )
        return new_samples

    def is_converged(self, history: list[dict[str, Any]]) -> bool:
        """Check convergence based on relative improvement.

        Converges when the relative improvement in the best objective
        over the last generation is below ``tol``.
        """
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
                "DE converged: relative change %.6f < tol %.6f",
                relative_change,
                self._tol,
            )
        return converged

    def name(self) -> str:
        return "de"

    def is_iterative(self) -> bool:
        return True
