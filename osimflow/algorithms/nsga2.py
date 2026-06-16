"""NSGA-II multi-objective optimizer (issue #140, G2c).

Wraps ``pymoo.algorithms.moo.nsga2.NSGA2`` to iteratively optimise
multiple KPIs simultaneously (e.g. EUI vs. cost).  The algorithm is
*iterative*: ``is_iterative()`` returns ``True`` and ``is_converged()``
checks hypervolume improvement across generations.

R-NSGA-II Support (issue #529)
-------------------------------
When ``ref_points`` or ``ref_dirs`` are provided, the algorithm uses
reference-based survival selection similar to R-NSGA-II / R-NSGA-III.
Reference points can be:
  - Predefined aspiration points (Pareto-front fractions)
  - Adaptive (updated each generation based on population)

Reference directions can be:
  - ``das-dennis`` — Das-Dennis structured points
  - ``energy`` — Riesz s-Energy well-spaced points
  - ``wedge`` — Wedge/decomposition-based adaptation

Workflow
--------
1. **Generation 0** — ``generate_samples()`` creates an initial LHS
   population of *n_samples* points for evaluation.
2. **Generation N > 0** — ``observe()`` reads the multi-objective KPIs
   from the previous generation, feeds them into pymoo's NSGA-II, and
   proposes a new population using the non-dominated sorting + crowding
   distance mechanism.
3. ``is_converged()`` returns ``True`` when the hypervolume improvement
   over the last two generations drops below ``hv_tol``.
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

log = logging.getLogger("osimflow.algorithms.nsga2")

# pymoo is an optional dependency — import lazily so the module can be
# loaded for static analysis even when pymoo is not installed.
try:
    from pymoo.algorithms.moo.nsga2 import NSGA2
    from pymoo.algorithms.moo.rnsga2 import RNSGA2
    from pymoo.core.problem import Problem
    from pymoo.indicators.hv import HV
    from pymoo.operators.crossover.sbx import SBX
    from pymoo.operators.mutation.pm import PM
    from pymoo.operators.sampling.rnd import FloatRandomSampling
    from pymoo.optimize import minimize as pymoo_minimize
    from pymoo.termination import get_termination
    from pymoo.util.reference_direction import das_dennis

    _HAS_PYMOO = True
except ImportError:
    _HAS_PYMOO = False
    RNSGA2 = None  # type: ignore[assignment]
    NSGA2 = None  # type: ignore[assignment]

try:
    from pymoo.util.ref_dirs import get_reference_directions

    _HAS_PYMOO_REF_DIRS = True
except ImportError:
    _HAS_PYMOO_REF_DIRS = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_bounds(independent_vars: list[dict[str, Any]]) -> list[tuple[float, float]]:
    """Extract (min, max) bounds from variable definitions.

    Only *uniform* distributions have natural bounds.  For other
    distributions we use a +/- 3sigma or [0, 1] fallback.
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


def _read_multi_kpi_values(
    history: list[dict[str, Any]],
    objective_kpis: list[str],
    constraints: list[dict[str, Any]] | None = None,
) -> list[tuple[list[float], list[float], float]]:
    """Read (params, [kpi_values], constraint_penalty) triples from history.

    Each history entry has ``samples`` and ``kpi_files``.  We read each
    KPI JSON file and extract the multiple objective values and the
    constraint penalty (issue #282).
    """
    results: list[tuple[list[float], list[float], float]] = []
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
                        obj_values = [
                            float(kpis.get(kpi_name, float("inf"))) for kpi_name in objective_kpis
                        ]
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
                        results.append((params, obj_values, penalty))
                    except (json.JSONDecodeError, ValueError, TypeError):
                        continue
    return results


class _SurrogateProblem:
    """Wraps the NSGA-II optimisation state as a pymoo Problem."""

    pass


# ---------------------------------------------------------------------------
# Reference Direction Helpers (R-NSGA-II support, issue #529)
# ---------------------------------------------------------------------------


def _parse_ref_points(
    ref_points_spec: str | list[float] | None,
    n_obj: int,
) -> npt.NDArray[np.float64] | None:
    """Parse reference points from CLI string or list.

    Reference points can be specified as:
    - Comma-separated floats (e.g., "0.25,0.5,0.75" for 2 objectives)
    - List of floats
    - None (no reference points)

    Each value is a fraction [0, 1] representing position on the
    normalized Pareto front. Multiple reference points can be provided.

    Returns
    -------
    numpy.ndarray of shape (n_ref_points, n_obj) or None
    """
    if ref_points_spec is None:
        return None

    # Parse values from string or list
    if isinstance(ref_points_spec, str):
        # Parse comma-separated string like "0.25,0.5" or "0.25,0.5,0.75"
        try:
            values = [float(v.strip()) for v in ref_points_spec.split(",")]
        except ValueError:
            log.warning("Invalid ref_points string '%s', ignoring", ref_points_spec)
            return None
    elif isinstance(ref_points_spec, (list, tuple)):
        values = [float(v) for v in ref_points_spec]
    else:
        return None

    # Validate and reshape
    if not values:
        return None

    # Each group of n_obj values forms one reference point
    n_ref_points = len(values) // n_obj
    if len(values) % n_obj != 0 or n_ref_points == 0:
        log.warning(
            "ref_points length %d not divisible by n_obj=%d or results in 0 points, ignoring",
            len(values),
            n_obj,
        )
        return None

    ref_points = np.array(values, dtype=np.float64).reshape(n_ref_points, n_obj)
    return ref_points


def _generate_ref_dirs(
    ref_dirs_spec: str | None,
    n_obj: int,
    pop_size: int,
) -> npt.NDArray[np.float64] | None:
    """Generate reference directions from specification string.

    Reference direction strategies:
    - ``das-dennis`` or ``uniform`` — Das-Dennis structured points
    - ``energy`` — Riesz s-Energy well-spaced points
    - ``wedge`` — Wedge/decomposition-based (Das-Dennis with scaling)
    - ``incremental`` — Incremental method

    Parameters
    ----------
    ref_dirs_spec
        Strategy name or None
    n_obj
        Number of objectives
    pop_size
        Population size (used to determine number of directions)

    Returns
    -------
    numpy.ndarray of shape (n_dirs, n_obj) or None
    """
    if ref_dirs_spec is None or not _HAS_PYMOO_REF_DIRS:
        return None

    # Determine number of points based on pop_size
    n_points = max(pop_size // n_obj, 3)

    strategy = ref_dirs_spec.lower().strip()

    try:
        if strategy in ("das-dennis", "uniform"):
            # Das-Dennis method with adaptive partitions
            # Use n_partitions to get approximately n_points
            n_partitions = max(2, int(np.ceil(n_points ** (1.0 / (n_obj - 1)))))
            ref_dirs = get_reference_directions("uniform", n_obj, n_partitions=n_partitions)
        elif strategy == "energy":
            ref_dirs = get_reference_directions("energy", n_obj, n_points, seed=42)
        elif strategy == "wedge":
            # Wedge method: multiple layers with different scalings
            # Creates a bias towards the center for better coverage
            ref_dirs_outer = get_reference_directions(
                "uniform", n_obj, n_partitions=max(2, n_partitions // 2), scaling=1.0
            )
            ref_dirs_inner = get_reference_directions(
                "uniform", n_obj, n_partitions=max(1, n_partitions // 4), scaling=0.5
            )
            # Combine outer and inner
            ref_dirs = np.vstack([ref_dirs_outer, ref_dirs_inner])
        elif strategy == "incremental":
            ref_dirs = get_reference_directions("incremental", n_obj, n_points)
        else:
            log.warning("Unknown ref_dirs strategy '%s', ignoring", ref_dirs_spec)
            return None

        log.info(
            "Generated %d reference directions using '%s' strategy",
            len(ref_dirs),
            strategy,
        )
        return np.array(ref_dirs, dtype=np.float64)

    except Exception as exc:
        log.warning("Failed to generate ref_dirs '%s': %s", ref_dirs_spec, exc)
        return None


def _adaptive_ref_points(
    population_F: npt.NDArray[np.float64],
    ref_points: npt.NDArray[np.float64],
    extreme_points: npt.NDArray[np.float64],
    n_obj: int,
) -> npt.NDArray[np.float64]:
    """Adaptively update reference points based on current population.

    This implements the adaptive reference point generation from R-NSGA-II.
    Reference points are projected onto the Pareto front approximation
    formed by the current non-dominated solutions.

    Parameters
    ----------
    population_F
        Current population objective values (n_pop, n_obj)
    ref_points
        Previous reference points (n_ref, n_obj)
    extreme_points
        Extreme points of the Pareto front (n_obj, n_obj)
    n_obj
        Number of objectives

    Returns
    -------
    Updated reference points
    """
    if population_F.shape[0] == 0 or ref_points.shape[0] == 0:
        return ref_points

    # Normalize objectives using extreme points as anchors
    # Project reference points onto the Pareto front approximation
    try:
        # Simple projection: for each reference point, find closest
        # non-dominated solution in the population
        updated_refs = ref_points.copy()
        for i in range(ref_points.shape[0]):
            ref = ref_points[i]
            # Calculate perpendicular distance to each solution
            distances = np.linalg.norm(population_F - ref, axis=1)
            closest_idx = np.argmin(distances)
            # Update reference point: blend toward closest solution
            updated_refs[i] = 0.5 * ref + 0.5 * population_F[closest_idx]
        return updated_refs
    except Exception:
        return ref_points


# ---------------------------------------------------------------------------
# NSGA-II Algorithm
# ---------------------------------------------------------------------------


class NSGA2Algorithm(BaseAlgorithm):
    """NSGA-II multi-objective optimizer using ``pymoo``.

    Parameters
    ----------
    objective_kpis
        List of KPI names to optimise simultaneously.
    maximize
        Per-KPI flags: ``True`` to maximise, ``False`` to minimise.
        Length must match *objective_kpis*.
    weights
        Per-KPI aggregation weights for weighted objective functions
        (issue #282). Applied before sign-flipping so that a
        ``weight=2.0`` on a maximised objective contributes ``-2.0*F``.
    hv_tol
        Hypervolume convergence tolerance.  Converges when relative
        hypervolume improvement < *hv_tol*.
    pop_size
        Population size for each generation.
    constraints
        Optional list of constraint definitions from variables.yml
        (issue #282). Violations are penalised by adding 1e9 to each
        violated constraint's objective.
    ref_points
        Predefined reference points for R-NSGA-II (issue #529).
        Can be specified as:
        - Comma-separated string: "0.25,0.5,0.75" (fractions on Pareto front)
        - List of floats
        - None (no reference points, standard NSGA-II)
    ref_dirs
        Reference direction strategy for many-objective optimization (issue #529).
        Supported values:
        - ``das-dennis`` or ``uniform`` — Das-Dennis structured points
        - ``energy`` — Riesz s-Energy well-spaced points
        - ``wedge`` — Wedge/decomposition-based adaptation
        - ``incremental`` — Incremental method
        - None (no reference directions, standard NSGA-II)
    """

    def __init__(
        self,
        objective_kpis: list[str] | None = None,
        maximize: list[bool] | None = None,
        weights: list[float] | None = None,
        hv_tol: float = 1e-3,
        pop_size: int = 40,
        constraints: list[dict[str, Any]] | None = None,
        ref_points: str | list[float] | None = None,
        ref_dirs: str | None = None,
    ) -> None:
        self._objective_kpis = objective_kpis or ["eui", "cost"]
        self._maximize = maximize or [False, False]
        self._weights = weights or [1.0] * len(self._objective_kpis)
        self._hv_tol = hv_tol
        self._pop_size = pop_size
        self._constraints = constraints
        self._ref_points = ref_points
        self._ref_dirs = ref_dirs
        self._independent_vars: list[dict[str, Any]] = []
        self._bounds: list[tuple[float, float]] = []
        self._hv_history: list[float] = []
        self._population_X: npt.NDArray[np.float64] = np.array([])
        self._population_F: npt.NDArray[np.float64] = np.array([])
        # R-NSGA-II: parsed reference points (n_ref_points, n_obj)
        self._parsed_ref_points: npt.NDArray[np.float64] | None = None
        # R-NSGA-II: generated reference directions (n_dirs, n_obj)
        self._generated_ref_dirs: npt.NDArray[np.float64] | None = None
        self._ref_dirs_strategy: str | None = None

    def configure(self, config: Any) -> None:
        """Configure R-NSGA-II reference points from campaign config (issue #529).

        Parses ``config.nsga2_ref_points`` (comma-separated floats, e.g.
        ``"0.25,0.5,0.75"`` for 3 reference points on a 2-objective front)
        and optionally ``config.nsga2_ref_dirs_strategy`` (e.g. ``"das_dennis"``)
        to generate reference directions.

        When reference points are set, ``observe()`` uses ``pymoo.RNSGA2``
        instead of the standard ``NSGA2`` crowding-distance selection.
        """
        ref_points_str: str | None = getattr(config, "nsga2_ref_points", None)
        if not ref_points_str:
            return

        ref_dirs_strategy: str | None = getattr(config, "nsga2_ref_dirs_strategy", None)
        self._ref_dirs_strategy = ref_dirs_strategy

        n_obj = len(self._objective_kpis)

        if ref_dirs_strategy and ref_dirs_strategy.lower() == "das_dennis":
            try:
                n_partitions = int(ref_points_str)
                self._parsed_ref_points = das_dennis(n_partitions, n_obj)
                log.info(
                    "R-NSGA-II das_dennis ref dirs: %d partitions, %d objectives, %d points",
                    n_partitions,
                    n_obj,
                    self._parsed_ref_points.shape[0],
                )
            except ValueError:
                self._parsed_ref_points = self._parse_ref_points_string(ref_points_str, n_obj)
        else:
            self._parsed_ref_points = self._parse_ref_points_string(ref_points_str, n_obj)

        if self._parsed_ref_points is not None:
            log.info(
                "R-NSGA-II configured with %d reference points for %d objectives",
                self._parsed_ref_points.shape[0],
                n_obj,
            )

    def _parse_ref_points_string(
        self, s: str, n_obj: int
    ) -> npt.NDArray[np.float64]:
        """Parse a comma-separated reference point string into a numpy array.

        Examples:
        - "0.25,0.5,0.75" → 3 ref points for 2-objective problem
        - "0.25,0.5,0.5,0.75" → 2 ref points for 2-objective problem
        """
        parts = [float(p.strip()) for p in s.split(",") if p.strip()]
        if not parts:
            return np.array([]).reshape(0, n_obj)

        if len(parts) % n_obj != 0:
            log.warning(
                "R-NSGA-II ref_points length %d not divisible by n_obj %d; "
                "using first %d values as one ref point",
                len(parts),
                n_obj,
                n_obj,
            )
            parts = parts[: len(parts) // n_obj * n_obj]

        n_points = len(parts) // n_obj
        return np.array(parts, dtype=np.float64).reshape(n_points, n_obj)

    def generate_samples(
        self,
        variables: dict[str, Any],
        n_samples: int,
        seed: int | None,
        outdir: Path,
    ) -> Path:
        """Generate initial population via LHS.

        For generation 0, the population is a Latin Hypercube sample.
        For subsequent generations (when internal state exists from
        ``observe()``), the NSGA-II selection produces the next
        population.
        """
        # Extract objective/constraints from variables.yml (issue #282).
        self._configure_from_variables(variables)
        if self._objective:
            # Single objective specified: use its name and direction.
            obj_name = str(self._objective.get("name", "eui"))
            direction = self._objective.get("direction", "minimize")
            weight = self._objective.get("weight", 1.0)
            self._objective_kpis = [obj_name]
            self._maximize = [direction == "maximize"]
            self._weights = [weight]
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

        # Check the explicit feedback-loop slot first (issue #332).
        if self._pending_proposed_samples:
            samples = self._pending_proposed_samples
            self._pending_proposed_samples = []  # consume
            samples_path.write_text(json.dumps({"samples": samples}, indent=2))
            log.info("NSGA-II proposed %d samples from explicit slot", len(samples))
            return samples_path

        if self._population_X.size > 0:
            var_names = [v["name"] for v in self._independent_vars]
            samples = self._array_to_samples(self._population_X, var_names)
            samples_path.write_text(json.dumps({"samples": samples}, indent=2))
            log.info("NSGA-II proposed %d samples from Pareto front", len(samples))
            return samples_path

        # Generation 0: initial LHS population.
        try:
            samples = _sample_with_engine(
                scipy.stats.qmc.LatinHypercube,
                independent_vars,
                n_samples,
                seed,
            )
        except (ValueError, NotImplementedError) as exc:
            raise RuntimeError("generate_nsga2 failed: initial LHS population") from exc

        samples_path.write_text(json.dumps({"samples": samples}, indent=2))
        log.info("NSGA-II generated %d initial samples", len(samples))
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

    def _sign_objectives(self, F: npt.NDArray[np.float64]) -> npt.NDArray[np.float64]:
        """Apply weights and flip sign for maximised objectives (issue #282).

        Each objective j is scaled by ``weights[j]`` before sign-flipping
        so that weighted multi-objective aggregation works correctly.
        """
        F_signed = F.copy()
        for j, maximize in enumerate(self._maximize):
            weight = self._weights[j] if j < len(self._weights) else 1.0
            if maximize:
                F_signed[:, j] = -F_signed[:, j] * weight
            else:
                F_signed[:, j] = F_signed[:, j] * weight
        return F_signed

    def observe(self, history: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Extract multi-objective KPIs, run NSGA-II, propose next population.

        Reads KPI files from the last generation, builds the objective
        matrix, runs one generation of NSGA-II via pymoo, and stores
        the selected individuals for the next ``generate_samples()`` call.

        When ``ref_points`` or ``ref_dirs`` are provided (R-NSGA-II mode,
        issue #529), the algorithm uses reference-based survival selection.
        """
        if not _HAS_PYMOO:
            log.warning("pymoo not installed; NSGA-II observe() is a no-op")
            return []

        if not history or not self._independent_vars:
            return []

        results = _read_multi_kpi_values(history, self._objective_kpis, self._constraints)
        if not results:
            log.warning("NSGA-II observe(): no KPI values found in history")
            return []

        # Build design matrix X and objective matrix F.
        # Constraint penalty is added to each objective (issue #282).
        X_all = np.array([r[0] for r in results], dtype=np.float64)
        F_all = np.array([r[1] for r in results], dtype=np.float64)
        penalties = np.array([r[2] for r in results], dtype=np.float64)
        # Add penalty to each objective row.
        F_all = F_all + penalties[:, np.newaxis]

        # Sign-flip for maximisation objectives.
        F_signed = self._sign_objectives(F_all)

        n_obj = len(self._objective_kpis)
        dim = len(self._bounds)
        xl = np.array([b[0] for b in self._bounds], dtype=np.float64)
        xu = np.array([b[1] for b in self._bounds], dtype=np.float64)

        # Define a pymoo Problem that returns pre-computed objectives.
        _X_ref = X_all
        _F_ref = F_signed

        class _EvaluatedProblem(Problem):  # type: ignore[misc]
            """Problem that uses pre-evaluated data for NSGA-II."""

            def _evaluate(
                self, X: npt.NDArray[np.float64], out: dict[str, Any], *args: Any, **kwargs: Any
            ) -> None:
                n = X.shape[0]
                out["F"] = np.full((n, n_obj), 1e6, dtype=np.float64)
                for i in range(n):
                    for j in range(_X_ref.shape[0]):
                        if np.allclose(X[i], _X_ref[j], atol=1e-10):
                            out["F"][i] = _F_ref[j]
                            break

        problem = _EvaluatedProblem(n_var=dim, n_obj=n_obj, xl=xl, xu=xu)

# Set up NSGA-II.
        # Check if this is the first call to initialize ref_points and ref_dirs
        pop_size = min(self._pop_size, len(results))

        # Build NSGA2 kwargs - pass ref_dirs if available (R-NSGA-II, issue #529)
        nsga2_kwargs: dict[str, Any] = {
            "pop_size": pop_size,
            "sampling": FloatRandomSampling(),
            "crossover": SBX(prob=0.9, eta=15),
            "mutation": PM(eta=20),
            "eliminate_duplicates": True,
        }

        # Use RNSGA2 when reference points are configured (R-NSGA-II, issue #529)
        if self._parsed_ref_points is not None and RNSGA2 is not None:
            nsga2_kwargs["ref_points"] = self._parsed_ref_points
            log.info(
                "R-NSGA-II observe(): using RNSGA2 with %d ref points",
                self._parsed_ref_points.shape[0],
            )
            algorithm = RNSGA2(**nsga2_kwargs)
        else:
            algorithm = NSGA2(**nsga2_kwargs)

        # Use pymoo.minimize with a 1-gen termination.
        res = pymoo_minimize(
            problem,
            algorithm,
            get_termination("n_gen", 1),
            seed=42,
            verbose=False,
        )

        # Extract the final population.
        if res.pop is not None:
            self._population_X = res.pop.get("X")
            self._population_F = res.pop.get("F")
        else:
            # Fallback: use the best found population.
            self._population_X = res.algorithm.pop.get("X")
            self._population_F = res.algorithm.pop.get("F")

        # Adaptive reference point update (R-NSGA-II, issue #529)
        if (
            self._parsed_ref_points is not None
            and self._population_F is not None
            and self._population_F.shape[0] > 0
        ):
            # Extract extreme points for normalization
            extreme_points = self._extract_extreme_points(self._population_F, n_obj)
            self._parsed_ref_points = _adaptive_ref_points(
                self._population_F,
                self._parsed_ref_points,
                extreme_points,
                n_obj,
            )

        # Compute hypervolume for convergence tracking.
        self._update_hypervolume(F_signed)

        if self._population_X.size == 0:
            return []

        var_names = [v["name"] for v in self._independent_vars]
        new_samples = self._array_to_samples(self._population_X, var_names)

        # Explicit feedback-loop slot for Campaign validation (issue #332).
        self._pending_proposed_samples = list(new_samples)

        log.info(
            "NSGA-II observe(): proposed %d new samples, hypervolume=%.4f",
            len(new_samples),
            self._hv_history[-1] if self._hv_history else 0.0,
        )
        return new_samples

    def _extract_extreme_points(
        self, F: npt.NDArray[np.float64], n_obj: int
    ) -> npt.NDArray[np.float64]:
        """Extract extreme points of the Pareto front.

        Extreme points are used for normalizing reference points in R-NSGA-II.
        Each extreme point maximizes one objective while minimizing others.

        Parameters
        ----------
        F
            Population objective values (n_individuals, n_obj)
        n_obj
            Number of objectives

        Returns
        -------
        Extreme points array (n_obj, n_obj)
        """
        if F.shape[0] == 0 or n_obj == 0:
            return np.zeros((n_obj, n_obj))

        extreme_points = np.zeros((n_obj, n_obj))

        for j in range(n_obj):
            # Create weight vector that maximizes objective j
            weights = np.zeros(n_obj)
            weights[j] = 1.0

            # Find the solution that maximizes this weighted objective
            # (considering all objectives to be minimized)
            min_dist = float("inf")
            best_idx = 0

            for i in range(F.shape[0]):
                # ASF (Achievement Scalarizing Function)
                asf = np.max(np.abs(F[i]) * weights) + np.sum(np.abs(F[i]) * weights)
                if asf < min_dist:
                    min_dist = asf
                    best_idx = i

            extreme_points[j] = F[best_idx]

        return extreme_points

    def _update_hypervolume(self, F: npt.NDArray[np.float64]) -> None:
        """Compute and store the hypervolume indicator."""
        if not _HAS_PYMOO:
            return

        if F.shape[0] == 0:
            self._hv_history.append(0.0)
            return

        # Reference point: max observed F + margin.
        ref_point = np.max(F, axis=0) + 1.0
        try:
            hv_indicator = HV(ref_point=ref_point)
            hv = float(hv_indicator(F))
        except Exception:
            hv = 0.0
        self._hv_history.append(hv)

    def is_converged(self, history: list[dict[str, Any]]) -> bool:
        """Check convergence based on hypervolume improvement.

        Converges when the relative hypervolume improvement over the
        last two generations is below ``hv_tol``.
        """
        if len(self._hv_history) < 2:
            return False

        prev_hv = self._hv_history[-2]
        curr_hv = self._hv_history[-1]

        if prev_hv == 0.0:
            return curr_hv == 0.0

        relative_change = abs(curr_hv - prev_hv) / abs(prev_hv)
        converged = relative_change < self._hv_tol
        if converged:
            log.info(
                "NSGA-II converged: HV change %.6f < tol %.6f",
                relative_change,
                self._hv_tol,
            )
        return converged

    def name(self) -> str:
        return "nsga2"

    def is_iterative(self) -> bool:
        return True
