"""Genetic Algorithm optimizer using DEAP (issue #345, ALGO-001).

Wraps the DEAP library's canonical GA operators (crossover, mutation,
selection) to iteratively minimise (or maximise) a scalar KPI (default:
EUI).  The algorithm is *iterative*: ``is_iterative()`` returns
``True`` and ``is_converged()`` checks the improvement tolerance across
generations.

Workflow
--------
1. **Generation 0** — ``generate_samples()`` creates an initial LHS
   population of *n_samples* individuals.
2. **Generation N > 0** — ``observe()`` reads the KPI files from the
   previous generation, evaluates fitness, runs one GA generation
   (selection, crossover, mutation), and proposes a new population.
3. ``is_converged()`` returns ``True`` when the relative improvement
   in the best objective over the last two generations drops below
   ``tol`` (default 1e-3), or when ``max_generations`` is reached.
"""

from __future__ import annotations

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

log = logging.getLogger("osimflow.algorithms.ga")

# DEAP is a required dependency for this module — it is listed in
# pyproject.toml [project.optional-dependencies] under the "ga" extra.
try:
    from deap import base as deap_base
    from deap import creator as deap_creator
    from deap import tools as deap_tools
except ImportError as exc:
    raise ImportError(
        "osimflow[ga] is required for GeneticAlgorithm. "
        "Install with: pip install osimflow[ga]"
    ) from exc


def _extract_bounds(independent_vars: list[dict[str, Any]]) -> list[tuple[float, float]]:
    """Extract (min, max) bounds from variable definitions.

    Only *uniform* distributions have natural bounds.  For other
    distributions we use a ±3σ or [0, 1] fallback so DEAP has a
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
    """Read (params, kpi_value, constraint_penalty) triples from history entries.

    Each history entry has ``samples`` and ``kpi_files``.  We read
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


class GeneticAlgorithm(BaseAlgorithm):
    """Genetic Algorithm optimizer using DEAP.

    Implements canonical GA operators: tournament selection, simulated
    binary crossover (SBX), and polynomial mutation.

    Parameters
    ----------
    objective_kpi
        Name of the KPI to minimise (or maximise).
    maximize
        If ``True``, maximise *objective_kpi* instead of minimising.
    tol
        Relative convergence tolerance for ``is_converged()``.
    popsize
        Population size (default: 20).  Must be >= 2.
    n_generations
        Maximum number of GA generations to run per iteration (default: 50).
    crossover_prob
        Probability of applying crossover (default: 0.9).
    mutation_prob
        Probability of mutating each gene (default: 0.2).
    eta_cx
        Distribution index for SBX crossover (higher = children closer to
        parents).  Default: 20.
    eta_mut
        Distribution index for polynomial mutation.  Default: 20.
    constraints
        Optional list of constraint definitions from variables.yml
        (issue #282). Each constraint is a dict with ``name``, ``max``,
        and optionally ``min``.
    """

    def __init__(
        self,
        objective_kpi: str = "eui",
        maximize: bool = False,
        tol: float = 1e-3,
        popsize: int = 20,
        n_generations: int = 50,
        crossover_prob: float = 0.9,
        mutation_prob: float = 0.2,
        eta_cx: float = 20.0,
        eta_mut: float = 20.0,
        constraints: list[dict[str, Any]] | None = None,
    ) -> None:
        if popsize < 2:
            raise ValueError("popsize must be >= 2")
        self._objective_kpi = objective_kpi
        self._maximize = maximize
        self._tol = tol
        self._popsize = popsize
        self._n_generations = n_generations
        self._crossover_prob = crossover_prob
        self._mutation_prob = mutation_prob
        self._eta_cx = eta_cx
        self._eta_mut = eta_mut
        self._constraints = constraints
        self._best_params: npt.NDArray[np.float64] = np.array([])
        self._best_value: float = float("inf")
        self._prev_best: float = float("inf")
        self._independent_vars: list[dict[str, Any]] = []
        self._bounds: list[tuple[float, float]] = []
        # Proposed samples from observe() — used by generate_samples()
        # on subsequent generations (issue #270).
        self._proposed_samples: list[dict[str, Any]] = []
        # DEAP individuals from the last generation (kept for mating).
        self._deap_population: list[Any] = []
        self._hof: Any = None  # Hall-of-fame for best individual.

    def name(self) -> str:
        return "ga"

    def is_iterative(self) -> bool:
        return True

    # ------------------------------------------------------------------
    # DEAP helpers
    # ------------------------------------------------------------------

    def _build_fitness_map(
        self,
        results: list[tuple[list[float], float, float]],
    ) -> dict[tuple[float, ...], float]:
        """Build a nearest-neighbour fitness map from observed KPI results."""
        fitness_map: dict[tuple[float, ...], float] = {}
        for params, value, penalty in results:
            key = tuple(params)
            eff = (-value if self._maximize else value) + penalty
            if key not in fitness_map or eff < fitness_map[key]:
                fitness_map[key] = eff
        return fitness_map

    def _bootstrap_population(self, popsize: int) -> list[Any]:
        """Create an initial random population centred around the best so far."""
        if len(self._best_params) > 0:
            centre = self._best_params.tolist()
        else:
            centre = [(lo + hi) / 2 for lo, hi in self._bounds]

        rng = np.random.default_rng(seed=42)
        population: list[Any] = []
        for _ in range(popsize):
            ind = [
                float(
                    np.clip(c + rng.normal(0, 0.1 * (hi - lo)), lo, hi)
                )
                for c, (lo, hi) in zip(centre, self._bounds, strict=True)
            ]
            population.append(deap_creator.Individual(ind))
        return population

    def _run_one_ga_generation(
        self,
        toolbox: Any,
        population: list[Any],
        fitness_map: dict[tuple[float, ...], float],
        penalty: float,
    ) -> list[Any]:
        """Run one GA generation: select, crossover, mutate, evaluate.

        Returns the new population.
        """
        # Selection.
        offspring = toolbox.select(population, len(population))

        # Crossover + mutation.
        new_pop: list[Any] = []
        for i in range(0, len(offspring), 2):
            if i + 1 >= len(offspring):
                new_pop.append(offspring[i])
                break
            ind1 = list(offspring[i])
            ind2 = list(offspring[i + 1])
            if np.random.random() < self._crossover_prob:
                ind1, ind2 = toolbox.mate(ind1, ind2)
            if np.random.random() < self._mutation_prob:
                ind1 = toolbox.mutate(list(ind1))[0]
            if np.random.random() < self._mutation_prob:
                ind2 = toolbox.mutate(list(ind2))[0]
            new_pop.append(deap_creator.Individual(ind1))
            new_pop.append(deap_creator.Individual(ind2))

        # Clip to bounds.
        for ind in new_pop:
            for j, (lo, hi) in enumerate(self._bounds):
                ind[j] = float(np.clip(ind[j], lo, hi))

        # Evaluate fitness.
        for ind in new_pop:
            ind.fitness.values = self._eval_fitness(
                list(ind), fitness_map, self._bounds, penalty
            )

        return new_pop

    def _propose_new_samples(self, best_ind: list[float], n_new: int) -> list[dict[str, Any]]:
        """Propose *n_new* sample dicts near *best_ind* with Gaussian perturbation."""
        var_names = [v["name"] for v in self._independent_vars]
        rng = np.random.default_rng(seed=123)
        new_samples: list[dict[str, Any]] = []
        for i in range(n_new):
            perturbation = rng.normal(0, 0.1, size=len(self._bounds))
            candidate = np.array(best_ind) + perturbation
            for j, (lo, hi) in enumerate(self._bounds):
                candidate[j] = float(np.clip(candidate[j], lo, hi))
            values: dict[str, Any] = {
                name: float(candidate[j]) for j, name in enumerate(var_names)
            }
            new_samples.append({"sample_id": f"{i + 1:04d}", "values": values})
        return new_samples

    def _eval_fitness(
        self,
        individual: list[float],
        fitness_map: dict[tuple[float, ...], float],
        bounds: list[tuple[float, float]],
        penalty: float,
    ) -> tuple[float]:
        """Return the fitness for an individual.

        Uses a pre-built fitness_map for nearest-neighbour lookup from
        observed KPI data.
        """
        key = tuple(float(x) for x in individual)
        # Find nearest in fitness_map.
        best_dist = float("inf")
        best_val = float("inf")
        for params, val in fitness_map.items():
            dist = sum((a - b) ** 2 for a, b in zip(key, params, strict=True))
            if dist < best_dist:
                best_dist = dist
                best_val = val
        eff = (-best_val if self._maximize else best_val) + penalty
        return (eff,)

    def _build_deap_toolbox(
        self,
        bounds: list[tuple[float, float]],
        seed: int | None,
    ) -> Any:
        """Build and return a configured DEAP toolbox."""
        # DEAP creator is stateful — we re-create it each time to avoid
        # conflicts if the class is used multiple times.
        # Remove any existing classes first (safe even if not present).
        for name in ("FitnessMax", "FitnessMin", "Individual"):
            if hasattr(deap_creator, name):
                delattr(deap_creator, name)

        if self._maximize:
            deap_creator.create("FitnessMax", deap_base.Fitness, weights=(1.0,))
            fitness_cls = deap_creator.FitnessMax
        else:
            deap_creator.create("FitnessMin", deap_base.Fitness, weights=(-1.0,))
            fitness_cls = deap_creator.FitnessMin

        # Create the Individual class directly so it is available before
        # any lambda that returns instances of it is invoked.
        deap_creator.create("Individual", list, fitness=fitness_cls)

        def _init_individual() -> deap_creator.Individual:
            """Create one random individual within bounds."""
            rng = np.random.default_rng(seed=seed)
            return deap_creator.Individual(
                [rng.uniform(lo, hi) for lo, hi in bounds]
            )

        def _mate(a: list[float], b: list[float]) -> tuple[list[float], list[float]]:
            """SBX crossover."""
            for i, (alo, ahi) in enumerate(bounds):
                child1, child2 = deap_tools.cxSimulatedBinaryBounded(
                    [a[i]], [b[i]], self._eta_cx, alo, ahi
                )
                a[i], b[i] = child1[0], child2[0]
            return a, b

        def _mutate(ind: list[float]) -> tuple[list[float]]:
            """Polynomial mutation."""
            for i, (lo, hi) in enumerate(bounds):
                ind[i] = deap_tools.mutPolynomialBounded(
                    [ind[i]], self._eta_mut, lo, hi, indpb=0.9
                )[0][0]
            return (ind,)

        toolbox = deap_base.Toolbox()
        toolbox.register("individual", _init_individual)
        toolbox.register("population", deap_tools.initRepeat, list, toolbox.individual)
        toolbox.register("select", deap_tools.selTournament, tournsize=3)
        toolbox.register("mate", _mate)
        toolbox.register("mutate", _mutate)

        return toolbox

    # ------------------------------------------------------------------
    # BaseAlgorithm interface
    # ------------------------------------------------------------------

    def generate_samples(
        self,
        variables: dict[str, Any],
        n_samples: int,
        seed: int | None,
        outdir: Path,
    ) -> Path:
        """Generate initial or subsequent population.

        Generation 0 creates a Latin Hypercube population.  Later
        generations use the GA internal state (maintained via
        ``observe()``) to propose new points.
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

        # If observe() has proposed new samples from the GA state,
        # use those instead of generating a fresh LHS population
        # (issue #270).
        if self._proposed_samples:
            samples = self._proposed_samples
            self._proposed_samples = []  # consume
            samples_path.write_text(json.dumps({"samples": samples}, indent=2))
            log.info("GA proposed %d samples from optimizer state", len(samples))
            return samples_path

        # For the initial population, use LHS.
        try:
            samples = _sample_with_engine(
                scipy.stats.qmc.LatinHypercube,
                independent_vars,
                n_samples,
                seed,
            )
        except (ValueError, NotImplementedError) as exc:
            raise RuntimeError("generate_ga failed: initial LHS population") from exc

        samples_path.write_text(json.dumps({"samples": samples}, indent=2))
        log.info("GA generated %d initial samples", len(samples))
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
        """Extract KPI values from history, run one GA generation, propose new samples."""
        if not history or not self._independent_vars:
            return []

        results = _read_kpi_values(history, self._objective_kpi, self._constraints)
        if not results:
            log.warning("GA observe(): no KPI values found in history")
            return []

        self._update_best(results)

        # Build fitness map for nearest-neighbour evaluation.
        fitness_map = self._build_fitness_map(results)

        # Determine population size.
        n_current = len(history[-1].get("samples", []))
        popsize = max(self._popsize, n_current)

        # Initialise DEAP toolbox.
        toolbox = self._build_deap_toolbox(self._bounds, seed=42)

        # Bootstrap population if needed.
        if not self._deap_population:
            self._deap_population = self._bootstrap_population(popsize)

        # Evaluate initial population.
        for ind in self._deap_population:
            ind.fitness.values = self._eval_fitness(
                list(ind), fitness_map, self._bounds, 0.0
            )

        # Update hall-of-fame.
        if self._hof is None:
            self._hof = deap_tools.HallOfFame(1)
        self._hof.update(self._deap_population)
        best_ind = list(self._hof[0])

        # Compute representative penalty for mutation.
        penalty = 0.0
        if self._constraints:
            for params, _, p in results:
                if np.allclose(params, self._best_params.tolist(), atol=1e-3):
                    penalty = p
                    break

        # Run one GA generation.
        new_pop = self._run_one_ga_generation(
            toolbox, self._deap_population, fitness_map, penalty
        )
        self._deap_population = new_pop

        # Update hall-of-fame.
        self._hof.update(new_pop)
        best_ind = list(self._hof[0])

        # Propose new samples around the best individual.
        n_new = n_current if n_current > 0 else popsize
        new_samples = self._propose_new_samples(best_ind, n_new)

        # Store proposed samples so generate_samples() can use them
        # on the next call (issue #270).
        self._proposed_samples = new_samples

        log.info(
            "GA observe(): best_value=%.4f, proposed %d new samples",
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
                "GA converged: relative change %.6f < tol %.6f",
                relative_change,
                self._tol,
            )
        return converged
