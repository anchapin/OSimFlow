"""Island-model parallel Genetic Algorithm (GAISL) optimizer (issue #549).

Implements the island-model parallel GA that was previously available in
openstudio-server's ``gaisl.rb`` with R ``NRELmoo`` library. Multiple
subpopulations (islands) evolve independently and exchange individuals
periodically via migration.

Key parameters (mirroring openstudio-server):
- ``numIslands`` — number of subpopulations (default: 5)
- ``migrationRate`` — fraction of population that migrates (default: 0.1)
- ``migrationInterval`` — generations between migrations (default: 10)

Migration topology: ring (each island sends to next, receives from previous).

Workflow
--------
1. **Generation 0** — ``generate_samples()`` creates *numIslands* initial
   LHS populations, one per island.
2. **Generation N > 0** — ``observe()`` reads KPI files, evaluates fitness
   per island, runs one GA generation per island, then performs migration
   when ``generation % migrationInterval == 0``.
3. ``is_converged()`` returns ``True`` when the best fitness across all
   islands shows relative improvement below ``tol``.

DEAP is a required dependency — the module raises ``ImportError`` at
import time when DEAP is not installed.
"""

import json
import logging
from pathlib import Path
from typing import Any

import numpy as np
import scipy.stats.qmc

from osimflow.algorithms import (
    BaseAlgorithm,
    _normalise_var_list,
    _partition_variables,
    _sample_with_engine,
    _write_empty_samples,
)

log = logging.getLogger("osimflow.algorithms.gaisl")

# DEAP is a required dependency for GAISL — import eagerly so the error
# is clear at module load time rather than at runtime.
from deap import base as deap_base  # noqa: E402
from deap import creator as deap_creator  # noqa: E402
from deap import tools as deap_tools  # noqa: E402


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


class _Island:
    """Represents one subpopulation in the island model.

    Each island maintains its own DEAP population, fitness map, and
    hall-of-fame.  Migration updates are applied externally by the
    parent ``IslandModelGAAlgorithm``.
    """

    def __init__(
        self,
        island_id: int,
        pop_size: int,
        bounds: list[tuple[float, float]],
        maximize: bool,
        crossover_prob: float,
        mutation_prob: float,
        eta_cx: float,
        eta_mut: float,
        seed: int | None,
    ) -> None:
        self.island_id = island_id
        self._pop_size = pop_size
        self._bounds = bounds
        self._maximize = maximize
        self._crossover_prob = crossover_prob
        self._mutation_prob = mutation_prob
        self._eta_cx = eta_cx
        self._eta_mut = eta_mut
        self._seed = seed

        # Per-island RNG with a distinct seed.
        self._rng = np.random.default_rng(seed=seed + island_id if seed is not None else None)

        # State that persists across generations.
        self._population: list[Any] = []
        self._hof: Any = None
        self._best_value: float = float("inf")
        self._best_params: list[float] = []
        self._generation: int = 0

        self._toolbox: Any | None = None
        self._fitness_map: dict[tuple[float, ...], float] = {}

    def _build_toolbox(self) -> Any:
        """Build and cache a DEAP toolbox for this island."""
        if self._toolbox is not None:
            return self._toolbox

        # DEAP creator is stateful — we re-create it per-island to avoid conflicts.
        for name in ("FitnessMax", "FitnessMin", "Individual"):
            if hasattr(deap_creator, name):
                delattr(deap_creator, name)

        if self._maximize:
            deap_creator.create("FitnessMax", deap_base.Fitness, weights=(1.0,))
            fitness_cls = deap_creator.FitnessMax
        else:
            deap_creator.create("FitnessMin", deap_base.Fitness, weights=(-1.0,))
            fitness_cls = deap_creator.FitnessMin

        deap_creator.create("Individual", list, fitness=fitness_cls)

        def _init_individual() -> deap_creator.Individual:
            return deap_creator.Individual(
                [self._rng.uniform(lo, hi) for lo, hi in self._bounds]
            )

        def _mate(a: list[float], b: list[float]) -> tuple[list[float], list[float]]:
            for i, (alo, ahi) in enumerate(self._bounds):
                child1, child2 = deap_tools.cxSimulatedBinaryBounded(
                    [a[i]], [b[i]], self._eta_cx, alo, ahi
                )
                a[i], b[i] = child1[0], child2[0]
            return a, b

        def _mutate(ind: list[float]) -> tuple[list[float]]:
            for i, (lo, hi) in enumerate(self._bounds):
                ind[i] = deap_creator.Individual(
                    deap_tools.mutPolynomialBounded(
                        [ind[i]], self._eta_mut, lo, hi, indpb=0.9
                    )[0]
                )[0]
            return (ind,)

        toolbox = deap_base.Toolbox()
        toolbox.register("individual", _init_individual)
        toolbox.register(
            "population",
            deap_tools.initRepeat,
            list,
            toolbox.individual,
        )
        toolbox.register("select", deap_tools.selTournament, tournsize=3)
        toolbox.register("mate", _mate)
        toolbox.register("mutate", _mutate)

        self._toolbox = toolbox
        return toolbox

    def _eval_fitness(
        self,
        individual: list[float],
        fitness_map: dict[tuple[float, ...], float],
        penalty: float,
    ) -> tuple[float]:
        """Return the fitness for an individual using nearest-neighbour lookup."""
        key = tuple(float(x) for x in individual)
        best_dist = float("inf")
        best_val = float("inf")
        for params, val in fitness_map.items():
            dist = sum((a - b) ** 2 for a, b in zip(key, params, strict=True))
            if dist < best_dist:
                best_dist = dist
                best_val = val
        eff = (-best_val if self._maximize else best_val) + penalty
        return (eff,)

    def _initialize_population(self) -> None:
        """Bootstrap the island's initial random population."""
        toolbox = self._build_toolbox()
        self._population = toolbox.population(n=self._pop_size)
        # Evaluate initial individuals.
        for ind in self._population:
            ind.fitness.values = self._eval_fitness(list(ind), self._fitness_map, 0.0)
        self._hof = deap_tools.HallOfFame(1)
        self._hof.update(self._population)
        self._update_best()

    def _update_best(self) -> None:
        """Update best individual from hall-of-fame."""
        if self._hof is None or len(self._hof) == 0:
            return
        best = list(self._hof[0])
        params = best
        fitness_raw = -self._hof[0].fitness.values[0] if self._maximize else self._hof[0].fitness.values[0]
        eff = fitness_raw
        if eff < self._best_value:
            self._best_value = eff
            self._best_params = params

    def _run_one_generation(
        self,
        fitness_map: dict[tuple[float, ...], float],
        penalty: float,
    ) -> None:
        """Run one GA generation: select, crossover, mutate, evaluate."""
        toolbox = self._build_toolbox()

        # Selection.
        offspring = toolbox.select(self._population, len(self._population))

        # Crossover + mutation.
        new_pop: list[Any] = []
        for i in range(0, len(offspring), 2):
            if i + 1 >= len(offspring):
                new_pop.append(offspring[i])
                break
            ind1 = list(offspring[i])
            ind2 = list(offspring[i + 1])
            if self._rng.random() < self._crossover_prob:
                ind1, ind2 = toolbox.mate(ind1, ind2)
            if self._rng.random() < self._mutation_prob:
                ind1 = toolbox.mutate(list(ind1))[0]
            if self._rng.random() < self._mutation_prob:
                ind2 = toolbox.mutate(list(ind2))[0]
            new_pop.append(deap_creator.Individual(ind1))
            new_pop.append(deap_creator.Individual(ind2))

        # Clip to bounds.
        for ind in new_pop:
            for j, (lo, hi) in enumerate(self._bounds):
                ind[j] = float(np.clip(ind[j], lo, hi))

        # Evaluate fitness.
        for ind in new_pop:
            ind.fitness.values = self._eval_fitness(list(ind), fitness_map, penalty)

        self._population = new_pop
        self._hof.update(new_pop)
        self._update_best()
        self._generation += 1

    def update_fitness_map(
        self,
        fitness_map: dict[tuple[float, ...], float],
    ) -> None:
        """Update this island's view of evaluated fitness."""
        self._fitness_map = fitness_map

    def inject_individuals(self, individuals: list[list[float]]) -> None:
        """Inject migrated individuals into this island's population.

        Replaces the worst individuals with the migrated ones (elitist merge).
        """
        if not individuals or not self._population:
            return

        n_inject = min(len(individuals), len(self._population))

        # Sort current population by fitness (worst first).
        sorted_pop = sorted(
            self._population,
            key=lambda ind: ind.fitness.values[0],
            reverse=self._maximize,
        )

        # Replace worst individuals with migrants.
        for i in range(n_inject):
            migrant = deap_creator.Individual(individuals[i])
            # Evaluate the migrant with current fitness map.
            migrant.fitness.values = self._eval_fitness(
                list(migrant), self._fitness_map, 0.0
            )
            sorted_pop[i] = migrant

        self._population = sorted_pop
        self._hof.update(self._population)

    def get_emigrants(self, n_emigrate: int) -> list[list[float]]:
        """Return the best *n_emigrate* individuals for migration to another island."""
        if not self._population or n_emigrate <= 0:
            return []

        # Select best individuals (lowest fitness for minimization).
        sorted_pop = sorted(
            self._population,
            key=lambda ind: ind.fitness.values[0],
            reverse=self._maximize,
        )
        return [list(ind) for ind in sorted_pop[:n_emigrate]]

    def get_best_individual(self) -> list[float] | None:
        """Return the best individual's parameter list, or None if uninitialized."""
        if self._hof is None or len(self._hof) == 0:
            return None
        return list(self._hof[0])

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def best_value(self) -> float:
        return self._best_value


class IslandModelGAAlgorithm(BaseAlgorithm):
    """Island-model parallel Genetic Algorithm optimizer.

    Multiple subpopulations (``numIslands``) evolve independently.
    Every ``migrationInterval`` generations, the best individuals
    (``migrationRate * pop_size``) migrate in a ring topology:
    island *i* sends to island *(i+1) mod numIslands*.

    Parameters
    ----------
    numIslands
        Number of subpopulations / islands (default: 5).
    migrationRate
        Fraction of each island's population that migrates
        per migration event (default: 0.1).
    migrationInterval
        Number of generations between migration events (default: 10).
    objective_kpi
        Name of the KPI to minimise (or maximise).  Default: ``"eui"``.
    maximize
        If ``True``, maximise *objective_kpi* instead of minimising.
    tol
        Relative convergence tolerance for ``is_converged()``.
    popsize
        Population size per island (default: 20).  Must be >= 2.
    n_generations
        Maximum number of GA generations to run per iteration (default: 50).
    crossover_prob
        Probability of applying crossover (default: 0.9).
    mutation_prob
        Probability of mutating each gene (default: 0.2).
    eta_cx
        Distribution index for SBX crossover (default: 20).
    eta_mut
        Distribution index for polynomial mutation (default: 20).
    constraints
        Optional list of constraint definitions from variables.yml.
        Each constraint is a dict with ``name``, ``max``, and optionally ``min``.
    """

    def __init__(
        self,
        numIslands: int = 5,
        migrationRate: float = 0.1,
        migrationInterval: int = 10,
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
        if numIslands < 1:
            raise ValueError("numIslands must be >= 1")
        if not 0.0 < migrationRate <= 1.0:
            raise ValueError("migrationRate must be in (0, 1]")
        if migrationInterval < 1:
            raise ValueError("migrationInterval must be >= 1")
        if popsize < 2:
            raise ValueError("popsize must be >= 2")

        self._numIslands = numIslands
        self._migrationRate = migrationRate
        self._migrationInterval = migrationInterval
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

        self._independent_vars: list[dict[str, Any]] = []
        self._bounds: list[tuple[float, float]] = []
        self._islands: list[_Island] = []
        self._generation: int = 0
        self._prev_best: float = float("inf")
        self._best_value: float = float("inf")

        # Proposed samples from observe() — used by generate_samples()
        # on subsequent generations.
        self._proposed_samples: list[dict[str, Any]] = []

    def name(self) -> str:
        return "gaisl"

    def is_iterative(self) -> bool:
        return True

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

    def _initialize_islands(self, seed: int | None) -> None:
        """Create all islands and initialize their populations."""
        self._islands = [
            _Island(
                island_id=i,
                pop_size=self._popsize,
                bounds=self._bounds,
                maximize=self._maximize,
                crossover_prob=self._crossover_prob,
                mutation_prob=self._mutation_prob,
                eta_cx=self._eta_cx,
                eta_mut=self._eta_mut,
                seed=seed,
            )
            for i in range(self._numIslands)
        ]
        for island in self._islands:
            island._initialize_population()

    def _run_migration(self) -> None:
        """Perform ring migration between islands.

        Each island sends its best individuals to the next island
        (ring topology: island i -> island (i+1) % numIslands).
        """
        n_emigrate = max(1, int(round(self._migrationRate * self._popsize)))
        log.debug(
            "GAISL migration: %d emigrants per island (rate=%.2f)",
            n_emigrate,
            self._migrationRate,
        )

        # Collect emigrants from each island.
        emigrants: list[list[list[float]]] = []
        for island in self._islands:
            emigrants.append(island.get_emigrants(n_emigrate))

        # Inject into receiving island (ring: i receives from (i-1)).
        for i, island in enumerate(self._islands):
            sender_idx = (i - 1) % self._numIslands
            island.inject_individuals(emigrants[sender_idx])

    def _get_combined_best_value(self) -> float:
        """Return the best fitness value across all islands."""
        return min(island.best_value for island in self._islands)

    def _propose_new_samples(
        self,
        var_names: list[str],
        n_total: int,
    ) -> list[dict[str, Any]]:
        """Propose new samples distributed across islands.

        Each island contributes roughly ``popsize`` samples from its
        current best individual with Gaussian perturbation.
        """
        samples: list[dict[str, Any]] = []
        per_island = max(1, n_total // self._numIslands)

        for island in self._islands:
            best = island.get_best_individual()
            if best is None:
                # Fallback: random individuals from this island.
                rng = np.random.default_rng(seed=123 + island.island_id)
                for _ in range(per_island):
                    ind = island._population[rng.integers(len(island._population))]
                    best = list(ind)
                    break
                else:
                    continue

            rng = np.random.default_rng(seed=456 + island.island_id)
            for _i in range(per_island):
                perturbation = rng.normal(0, 0.1, size=len(self._bounds))
                candidate = np.array(best) + perturbation
                for j, (lo, hi) in enumerate(self._bounds):
                    candidate[j] = float(np.clip(candidate[j], lo, hi))
                values: dict[str, Any] = {
                    name: float(candidate[k]) for k, name in enumerate(var_names)
                }
                samples.append({"sample_id": f"{len(samples) + 1:04d}", "values": values})

        # If we have fewer samples than requested, fill from the first island.
        if len(samples) < n_total:
            best = self._islands[0].get_best_individual()
            if best:
                rng = np.random.default_rng(seed=789)
                for _ in range(n_total - len(samples)):
                    perturbation = rng.normal(0, 0.1, size=len(self._bounds))
                    candidate = np.array(best) + perturbation
                    for j, (lo, hi) in enumerate(self._bounds):
                        candidate[j] = float(np.clip(candidate[j], lo, hi))
                    values = {name: float(candidate[k]) for k, name in enumerate(var_names)}
                    samples.append({"sample_id": f"{len(samples) + 1:04d}", "values": values})

        return samples[:n_total]

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

        Generation 0 creates *numIslands* Latin Hypercube populations,
        one per island.  Later generations use island state from
        ``observe()`` to propose new points.
        """
        # Extract objective direction and constraints from variables.yml.
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

        # If observe() has proposed new samples, use those.
        if self._proposed_samples:
            samples = self._proposed_samples
            self._proposed_samples = []  # consume
            samples_path.write_text(json.dumps({"samples": samples}, indent=2))
            log.info("GAISL proposed %d samples from island state", len(samples))
            return samples_path

        # For the initial population, use LHS distributed across islands.
        # Each island gets roughly n_samples / numIslands individuals.
        try:
            lhs_samples = _sample_with_engine(
                scipy.stats.qmc.LatinHypercube,
                independent_vars,
                n_samples,
                seed,
            )
        except (ValueError, NotImplementedError) as exc:
            raise RuntimeError("generate_gaisl failed: initial LHS population") from exc

        samples_path.write_text(json.dumps({"samples": lhs_samples}, indent=2))
        log.info("GAISL generated %d initial samples across %d islands", n_samples, self._numIslands)
        return samples_path

    def observe(self, history: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Extract KPI values, run island GA generations, perform migration, propose new samples."""
        if not history or not self._independent_vars:
            return []

        results = _read_kpi_values(history, self._objective_kpi, self._constraints)
        if not results:
            log.warning("GAISL observe(): no KPI values found in history")
            return []

        # Build global fitness map.
        fitness_map = self._build_fitness_map(results)

        # Update best value.
        self._prev_best = self._best_value
        for _, value, penalty in results:
            eff = (-value if self._maximize else value) + penalty
            self._best_value = min(self._best_value, eff)

        # Initialize islands if needed.
        if not self._islands:
            self._initialize_islands(seed=42)

        # Update each island's fitness map and run one generation.
        for island in self._islands:
            island.update_fitness_map(fitness_map)

            # Compute representative penalty for this island.
            penalty = 0.0
            best_ind = island.get_best_individual()
            if best_ind is not None:
                for params, _, p in results:
                    if np.allclose(params, best_ind, atol=1e-3):
                        penalty = p
                        break

            island._run_one_generation(fitness_map, penalty)

        # Increment global generation counter.
        self._generation += 1

        # Perform migration at migrationInterval boundaries.
        if self._migrationInterval > 0 and self._generation % self._migrationInterval == 0:
            log.info(
                "GAISL performing migration at generation %d",
                self._generation,
            )
            self._run_migration()

        # Determine number of samples for next generation.
        n_current = len(history[-1].get("samples", []))
        n_propose = n_current if n_current > 0 else self._popsize * self._numIslands

        var_names = [v["name"] for v in self._independent_vars]
        new_samples = self._propose_new_samples(var_names, n_propose)

        # Store proposed samples so generate_samples() can use them.
        self._proposed_samples = new_samples

        log.info(
            "GAISL observe(): best_value=%.4f, generation=%d, proposed %d samples",
            self._best_value,
            self._generation,
            len(new_samples),
        )
        return new_samples

    def is_converged(self, history: list[dict[str, Any]]) -> bool:
        """Check convergence based on relative improvement in best island fitness.

        Converges when the relative improvement in the best objective
        across all islands over the last two generations drops below ``tol``.
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
                "GAISL converged: relative change %.6f < tol %.6f",
                relative_change,
                self._tol,
            )
        return converged
