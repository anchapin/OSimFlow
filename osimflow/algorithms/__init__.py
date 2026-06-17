"""Algorithm plug-in framework for OSimFlow sampling strategies.

Provides the ``BaseAlgorithm`` abstract base class, the
``AlgorithmRegistry`` singleton for discovery/instantiation, and the
built-in ``LHSAlgorithm``, ``SobolAlgorithm``, ``HaltonAlgorithm``,
``MorrisAlgorithm``, ``FAST99Algorithm``, ``DifferentialEvolutionAlgorithm``,
``DualAnnealingAlgorithm``, ``GeneticAlgorithm``, ``IslandModelGAAlgorithm``,
``NSGA2Algorithm``, ``PSOAlgorithm``,
``FullFactorialAlgorithm``, ``GridSamplingAlgorithm``, ``RepeatAllAlgorithm``,
``RandomSamplingAlgorithm``, and ``SequentialSearchAlgorithm`` implementations.

Adding a new algorithm (Bayesian optimisation, …) requires only:

1.  Subclass ``BaseAlgorithm``.
2.  Call ``AlgorithmRegistry.register("name", MyAlgorithm)`` at import
    time (typically at the bottom of this module or in a separate plugin
    module under ``osimflow/algorithms/``).

**Third-party plug-ins (issue #432):** packages can also register
algorithms via ``entry_points`` so they are auto-discovered without
importing them manually.  Add this to your ``pyproject.toml``::

    [project.entry-points."osimflow.algorithms"]
    my_algo = "my_package.algorithms:MyAlgoClass"

The entry-point ``name`` becomes the algorithm name registered in the
``AlgorithmRegistry``.  Discovery runs automatically at module import
time via ``AlgorithmRegistry.discover_plugins()``; import errors in
individual plug-ins are logged and skipped.

The ``Campaign`` class dispatches through the registry via
``AlgorithmRegistry.get(config.algorithm)`` so the orchestration layer
stays decoupled from the sampling strategy.
"""

import abc
import json
import logging
import math
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any

import scipy.stats
import scipy.stats.qmc

log = logging.getLogger("osimflow.algorithms")

#: Entry-point group for third-party algorithm plug-ins (issue #432).
ALGORITHM_ENTRY_POINT_GROUP = "osimflow.algorithms"


# ======================================================================
# Abstract base class
# ======================================================================


class BaseAlgorithm(abc.ABC):
    """Interface that every sampling algorithm must implement.

    Single-shot algorithms (LHS, Sobol) return ``is_iterative() ==
    False`` and ``is_converged()`` always ``True``.  Iterative /
    optimization algorithms (NSGA-II, Bayesian optimisation) return
    ``is_iterative() == True`` and implement a real convergence check.
    """

    # Objective configuration parsed from variables.yml (issue #282).
    # Set by _configure_from_variables() called from generate_samples().
    _objective: dict[str, Any] | None = None
    _constraints: list[dict[str, Any]] | None = None
    # Explicit feedback loop storage (issue #332). Iterative algorithms
    # Explicit feedback-loop slot: observe() writes proposed samples here,
    # generate_samples() reads from here first (issue #332).
    # This makes the contract visible and validatable rather than relying
    # on opaque internal state (_proposed_samples, _positions, _population_X).
    _pending_proposed_samples: list[dict[str, Any]] = []

    def _configure_from_variables(self, variables: dict[str, Any]) -> None:
        """Extract objective and constraints from the variables dict.

        Called at the start of ``generate_samples()`` so that algorithms
        can read direction / weight / constraints for objective
        sign-flipping and penalty evaluation.
        """
        self._objective = None
        self._constraints = None
        if not isinstance(variables, dict):
            return
        raw_obj = variables.get("objective")
        if isinstance(raw_obj, dict):
            self._objective = {
                "name": str(raw_obj.get("name", "eui")),
                "direction": str(raw_obj.get("direction", "minimize")),
                "weight": float(raw_obj.get("weight", 1.0)),
                "target": float(raw_obj["target"]) if "target" in raw_obj else None,
                "scaling_factor": float(raw_obj["scaling_factor"])
                if "scaling_factor" in raw_obj
                else None,
            }
        raw_constraints = variables.get("constraints")
        if isinstance(raw_constraints, list):
            self._constraints = []
            for c in raw_constraints:
                if isinstance(c, dict):
                    entry: dict[str, Any] = {
                        "name": str(c.get("name", "")),
                        "max": float(c["max"]) if "max" in c else float("inf"),
                    }
                    if "min" in c:
                        entry["min"] = float(c["min"])
                    self._constraints.append(entry)

    def _apply_constraints(self, kpi_values: dict[str, float]) -> float:
        """Return a penalty for violated constraints.

        Sums a large positive value (1e9) for each violated constraint.
        The penalty is added to the objective so that minimisation
        algorithms treat infeasible solutions as worse.
        """
        if not self._constraints:
            return 0.0
        penalty = 0.0
        for c in self._constraints:
            name = c["name"]
            val = kpi_values.get(name, 0.0)
            max_val = c.get("max", float("inf"))
            min_val = c.get("min", float("-inf"))
            if val > max_val or val < min_val:
                penalty += 1e9
        return penalty

    @abc.abstractmethod
    def generate_samples(
        self,
        variables: dict[str, Any],
        n_samples: int,
        seed: int | None,
        outdir: Path,
    ) -> Path:
        """Generate ``samples.json`` and return its path.

        Parameters
        ----------
        variables
            Parsed ``variables.yml`` dict (``{name: {distribution, …}}``).
            May contain top-level ``objective`` and ``constraints`` keys
            for optimisation configuration (issue #282).
        n_samples
            Number of parameter sets to produce.
        seed
            Optional RNG seed for reproducibility.
        outdir
            Directory to write ``samples.json`` into (created if absent).

        Returns
        -------
        Path
            Absolute path to the written ``samples.json``.
        """

    @abc.abstractmethod
    def observe(self, history: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Observe KPI history, return updated sample set.

        For single-shot algorithms this is a no-op (returns the last
        entry's samples).  Iterative algorithms use it to propose new
        samples based on past results.
        """

    @abc.abstractmethod
    def is_converged(self, history: list[dict[str, Any]]) -> bool:
        """Return ``True`` when the algorithm has converged.

        Single-shot algorithms always return ``True``.
        """

    @abc.abstractmethod
    def name(self) -> str:
        """Return the algorithm's canonical name (e.g. ``"lhs"``)."""

    @abc.abstractmethod
    def is_iterative(self) -> bool:
        """Return ``True`` for iterative / optimization algorithms."""

    def is_multi_objective(self) -> bool:
        """Return ``True`` for multi-objective algorithms (e.g. NSGA-II).

        The default is ``False``.  Algorithms that produce a Pareto front
        should override this to return ``True`` so the Campaign persists
        per-generation Pareto data (issue #141).
        """
        return False

    def configure(self, config: Any) -> None:  # noqa: B027
        """Configure the algorithm with campaign-level settings (issue #529).

        Called by ``Campaign`` after algorithm instantiation but before
        the first ``generate_samples()`` call.  Algorithms that need
        campaign-level configuration (e.g. R-NSGA-II reference points)
        can override this method.

        The default implementation is a no-op.

        Parameters
        ----------
        config
            The ``CampaignConfig`` instance containing algorithm-specific
            settings (e.g. ``nsga2_ref_points``, ``nsga2_ref_dirs_strategy``).
        """
        pass  # noqa: B027

    def compute_sensitivity_indices(
        self,
        variables: dict[str, Any],
        samples: list[dict[str, Any]],
        kpi_values: dict[str, dict[str, float]],
        outdir: Path,
        calc_second_order: bool = False,
    ) -> Path:
        """Compute sensitivity indices (Sobol). Raises NotImplementedError by default.

        Only ``SobolAlgorithm`` implements this method. Other algorithms
        that do not support sensitivity analysis will raise ``NotImplementedError``.
        """
        raise NotImplementedError(
            f"{self.__class__.__name__} does not implement compute_sensitivity_indices"
        )

    def compute_uq_indices(
        self,
        variables: dict[str, Any],
        samples: list[dict[str, Any]],
        kpi_values: dict[str, dict[str, float]],
        outdir: Path,
        failure_thresholds: dict[str, tuple[float, str]] | None = None,
        confidence: float = 0.95,
    ) -> Path:
        """Compute UQ indices (POF, CIs). Raises NotImplementedError by default.

        Only ``UncertaintyQuantification`` implements this method. Other algorithms
        that do not support UQ analysis will raise ``NotImplementedError``.
        """
        raise NotImplementedError(
            f"{self.__class__.__name__} does not implement compute_uq_indices"
        )


# ======================================================================
# Registry
# ======================================================================


class AlgorithmRegistry:
    """Global registry that maps algorithm names to their classes.

    Typical usage::

        algo = AlgorithmRegistry.get("lhs")
        samples_path = algo.generate_samples(variables, n, seed, outdir)
    """

    _registry: dict[str, type[BaseAlgorithm]] = {}

    @classmethod
    def register(cls, name: str, algo_cls: type[BaseAlgorithm]) -> None:
        """Register *algo_cls* under *name*."""
        cls._registry[name] = algo_cls
        log.debug("registered algorithm %s -> %s", name, algo_cls.__qualname__)

    @classmethod
    def get(cls, name: str, **kwargs: Any) -> BaseAlgorithm:
        """Instantiate and return the algorithm registered under *name*.

        Parameters
        ----------
        name
            Algorithm name to look up.
        **kwargs
            Additional keyword arguments passed to the algorithm constructor.
            Useful for algorithm-specific parameters like NSGA2's
            ``ref_points`` and ``ref_dirs`` (issue #529).

        Raises
        ------
        ValueError
            If *name* is not registered, with a helpful message listing
            available algorithms.
        """
        if name not in cls._registry:
            available = ", ".join(sorted(cls._registry)) or "(none)"
            raise ValueError(f"unknown algorithm '{name}'. Available algorithms: {available}")
        return cls._registry[name](**kwargs)

    @classmethod
    def list_available(cls) -> list[str]:
        """Return the sorted list of registered algorithm names."""
        return sorted(cls._registry)

    # ------------------------------------------------------------------
    # Entry-point plug-in discovery (issue #432)
    # ------------------------------------------------------------------

    @classmethod
    def discover_plugins(cls) -> int:
        """Discover and auto-register algorithms from installed entry points.

        Scans the ``osimflow.algorithms`` entry point group and loads each
        entry point.  Loaded objects that are ``BaseAlgorithm`` subclasses are
        registered under the entry-point ``name``.

        The method is **safe** — if no plug-ins are found it silently returns
        ``0``.  Import or type errors for individual plug-ins are logged at
        ``WARNING`` level and skipped so a single broken plug-in never breaks
        the registry.

        Returns
        -------
        int
            The number of plug-ins successfully registered.
        """
        try:
            eps = list(entry_points(group=ALGORITHM_ENTRY_POINT_GROUP))
        except Exception:  # noqa: BLE001 — never crash on metadata issues
            return 0

        if not eps:
            return 0

        count = 0
        for ep in eps:
            try:
                obj = ep.load()
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "failed to load algorithm plug-in '%s' (%s): %s",
                    ep.name,
                    ep.value,
                    exc,
                )
                continue

            if not (isinstance(obj, type) and issubclass(obj, BaseAlgorithm)):
                log.warning(
                    "algorithm plug-in '%s' (%s) is not a BaseAlgorithm subclass — skipping",
                    ep.name,
                    ep.value,
                )
                continue

            cls.register(ep.name, obj)
            log.info("discovered algorithm plug-in '%s' -> %s", ep.name, ep.value)
            count += 1

        return count


# ======================================================================
# Built-in: LHS
# ======================================================================


def _apply_distribution(u: float, dist: str, params: dict[str, Any]) -> float | dict[str, Any]:
    """Map a unit-sample value *u* ∈ [0, 1] through the named distribution PPF.

    This is the same logic that lives in ``bin/generate_lhs.py``.  The
    inline copy avoids a subprocess call so the algorithm can run
    directly inside the executor's work function if desired.
    """
    if dist == "uniform":
        return float(params["min"] + u * (params["max"] - params["min"]))

    if dist == "lognormal":
        return float(scipy.stats.lognorm.ppf(u, s=params["sigma"], scale=math.exp(params["mean"])))

    if dist == "normal":
        return float(scipy.stats.norm.ppf(u, loc=params["mean"], scale=params["sigma"]))

    if dist == "triangular":
        left = params["min"]
        right = params["max"]
        mode = params.get("mode")
        c = (mode - left) / (right - left) if mode is not None else 0.5
        return float(scipy.stats.triang.ppf(u, c, loc=left, scale=right - left))

    if dist in ("discrete", "categorical"):
        values_list: list[Any] = params["values"]
        idx = int(round(u * (len(values_list) - 1)))
        chosen: float | dict[str, Any] = values_list[idx]
        return chosen

    if dist == "conditional":
        raise NotImplementedError("conditional distributions require dependency resolution")

    # Beta / gamma / exponential share a common pattern — build shape
    # params lazily so we never touch keys for the *wrong* distribution.
    if dist == "beta":
        ppf_params = {"a": params["alpha"], "b": params["beta"]}
    elif dist == "gamma":
        ppf_params = {"a": params["alpha"]}
    elif dist == "exponential":
        ppf_params = {"scale": params["rate"]}
    else:
        raise NotImplementedError(f"unsupported distribution: {dist!r}")

    # scipy dist objects keyed by name.
    _scipy_dist = {
        "beta": scipy.stats.beta,
        "gamma": scipy.stats.gamma,
        "exponential": scipy.stats.expon,
    }
    if dist in ("beta", "gamma"):
        ppf_params["loc"] = params.get("loc", 0.0)
        ppf_params["scale"] = params.get("scale", 1.0)

    return float(_scipy_dist[dist].ppf(u, **ppf_params))


def _normalise_var_list(raw: Any) -> list[dict[str, Any]]:
    """Return a list-of-dicts variable definition from the parsed YAML.

    Accepts both the canonical list format and a dict-of-dicts fallback.
    Returns an empty list when no variables are defined.
    """
    var_list: Any = raw
    if isinstance(var_list, list) and var_list and isinstance(var_list[0], dict):
        return var_list
    if isinstance(var_list, dict):
        return [{"name": k, **v} for k, v in var_list.items()]
    return []


def _partition_variables(
    var_list: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split *var_list* into (independent, conditional)."""
    independent: list[dict[str, Any]] = []
    conditional: list[dict[str, Any]] = []
    for var_def in var_list:
        if var_def.get("distribution") == "conditional":
            conditional.append(var_def)
        else:
            independent.append(var_def)
    return independent, conditional


def _sample_with_engine(
    engine_cls: type,
    independent_vars: list[dict[str, Any]],
    n_samples: int,
    seed: int | None,
    **engine_kwargs: Any,
) -> list[dict[str, Any]]:
    """Generate samples using any ``scipy.stats.qmc`` engine class.

    Parameters
    ----------
    engine_cls
        A ``scipy.stats.qmc.QMCEngine`` subclass (e.g.
        ``LatinHypercube``, ``Sobol``, ``Halton``).
    independent_vars
        Variable definitions to sample.
    n_samples
        Number of sample points.
    seed
        Optional RNG seed for reproducibility.
    **engine_kwargs
        Additional keyword arguments forwarded to the engine constructor.

    Returns
    -------
    list[dict[str, Any]]
        List of ``{"sample_id": ..., "values": {...}}`` dicts.
    """
    dim = len(independent_vars)
    if dim == 0:
        return []
    rng = engine_cls(d=dim, seed=seed, **engine_kwargs)
    unit_samples = rng.random(n=n_samples)
    samples: list[dict[str, Any]] = []
    for i in range(n_samples):
        values: dict[str, Any] = {}
        for j, var_def in enumerate(independent_vars):
            var_name = var_def["name"]
            dist_name = str(var_def["distribution"])
            params = {k: v for k, v in var_def.items() if k not in ("distribution", "name")}
            values[var_name] = _apply_distribution(unit_samples[i, j], dist_name, params)
        samples.append({"sample_id": f"{i + 1:04d}", "values": values})
    return samples


def _sample_independent(
    independent_vars: list[dict[str, Any]],
    n_samples: int,
    seed: int | None,
) -> list[dict[str, Any]]:
    """Run LHS over *independent_vars* and return sample dicts."""
    return _sample_with_engine(
        scipy.stats.qmc.LatinHypercube,
        independent_vars,
        n_samples,
        seed,
    )


def _resolve_conditional(
    samples: list[dict[str, Any]],
    conditional_vars: list[dict[str, Any]],
    n_samples: int,
) -> None:
    """Resolve conditional/dependent variables in-place."""
    from collections import deque  # noqa: PLC0415

    queue: deque[int] = deque(range(len(conditional_vars)))
    max_iter = len(conditional_vars) * 2
    iteration = 0
    while queue and iteration < max_iter:
        idx = queue.popleft()
        var_def = conditional_vars[idx]
        var_name = var_def["name"]
        cond = var_def.get("depends_on", {})
        parent = cond.get("variable", "")
        resolved_count = 0
        for sample in samples:
            parent_val = sample["values"].get(parent)
            if parent_val is None:
                continue
            for rule in var_def.get("conditions", []):
                if eval(  # noqa: S307
                    str(cond.get("match", "True")),
                    {"__builtins__": {}},
                    {"val": parent_val},
                ):
                    rule_dist = rule.get("distribution", "uniform")
                    rule_params = {k: v for k, v in rule.items() if k != "distribution"}
                    sample["values"][var_name] = _apply_distribution(0.5, rule_dist, rule_params)
                    resolved_count += 1
                    break
        if resolved_count < n_samples:
            queue.append(idx)
        iteration += 1


def _write_empty_samples(path: Path) -> Path:
    """Write an empty samples file and return its path."""
    path.write_text(json.dumps({"samples": []}, indent=2))
    return path


def _generate_lhs_inline(
    variables: dict[str, Any],
    n_samples: int,
    seed: int | None,
    outdir: Path,
) -> Path:
    """Generate LHS samples *inline* (no subprocess).

    Mirrors the logic of ``bin/generate_lhs.py``.  The *variables*
    parameter is the parsed ``variables.yml`` dict.  The actual variable
    list lives under the ``"variables"`` key and is a list of dicts
    where each dict has a ``"name"`` field and distribution info.
    """
    outdir.mkdir(parents=True, exist_ok=True)
    samples_path = outdir / "samples.json"

    var_list = _normalise_var_list(variables.get("variables", []))
    if not var_list:
        return _write_empty_samples(samples_path)

    independent_vars, conditional_vars = _partition_variables(var_list)
    if not independent_vars:
        return _write_empty_samples(samples_path)

    samples = _sample_independent(independent_vars, n_samples, seed)
    if conditional_vars:
        _resolve_conditional(samples, conditional_vars, n_samples)

    samples_path.write_text(json.dumps({"samples": samples}, indent=2))
    return samples_path


class LHSAlgorithm(BaseAlgorithm):
    """Latin Hypercube Sampling — the default single-shot algorithm.

    Wraps the existing LHS generation logic (``scipy.stats.qmc.LatinHypercube``)
    that was previously hard-coded in ``Campaign.step_generate_lhs()``.
    """

    def generate_samples(
        self,
        variables: dict[str, Any],
        n_samples: int,
        seed: int | None,
        outdir: Path,
    ) -> Path:
        outdir.mkdir(parents=True, exist_ok=True)
        samples_path = outdir / "samples.json"

        # Delegate to the inline generator — same logic as
        # bin/generate_lhs.py but without a subprocess round-trip.
        try:
            result = _generate_lhs_inline(variables, n_samples, seed, outdir)
        except (ValueError, NotImplementedError) as exc:
            # Preserve the error-chain contract that the old subprocess path
            # established: callers (and tests) expect RuntimeError with a
            # "generate_lhs failed" prefix.
            raise RuntimeError("generate_lhs failed") from exc

        # If the inline generator wrote to a different filename (e.g. on
        # edge-case paths), copy to the canonical location.
        if result != samples_path and result.exists():
            import shutil  # noqa: PLC0415

            shutil.copy2(result, samples_path)

        return samples_path

    def observe(self, history: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Single-shot: return the samples from the last iteration."""
        if not history:
            return []
        last = history[-1].get("samples", [])
        last_samples: list[dict[str, Any]] = list(last)
        return last_samples

    def is_converged(self, history: list[dict[str, Any]]) -> bool:
        """Single-shot algorithms are always converged."""
        return True

    def name(self) -> str:
        return "lhs"

    def is_iterative(self) -> bool:
        return False


# ======================================================================
# Register built-in algorithms
# ======================================================================

AlgorithmRegistry.register("lhs", LHSAlgorithm)

from osimflow.algorithms.factorial import (  # noqa: E402
    FullFactorialAlgorithm,
    GridSamplingAlgorithm,
)
from osimflow.algorithms.halton import HaltonAlgorithm  # noqa: E402
from osimflow.algorithms.sobol import SobolAlgorithm  # noqa: E402

AlgorithmRegistry.register("sobol", SobolAlgorithm)
AlgorithmRegistry.register("halton", HaltonAlgorithm)
AlgorithmRegistry.register("full_factorial", FullFactorialAlgorithm)
AlgorithmRegistry.register("grid", GridSamplingAlgorithm)

from osimflow.algorithms.da import DualAnnealingAlgorithm  # noqa: E402
from osimflow.algorithms.de import DifferentialEvolutionAlgorithm  # noqa: E402

AlgorithmRegistry.register("de", DifferentialEvolutionAlgorithm)
AlgorithmRegistry.register("dual_annealing", DualAnnealingAlgorithm)

try:
    from osimflow.algorithms.fast99 import FAST99Algorithm  # noqa: E402
    from osimflow.algorithms.morris import MorrisAlgorithm  # noqa: E402

    AlgorithmRegistry.register("morris", MorrisAlgorithm)
    AlgorithmRegistry.register("fast99", FAST99Algorithm)
except ImportError:
    # SALib is an optional dependency — Morris and FAST99 are only
    # available when the [sensitivity] extra is installed.
    pass

try:
    from osimflow.algorithms.nsga2 import NSGA2Algorithm  # noqa: E402
    from osimflow.algorithms.pso import PSOAlgorithm  # noqa: E402
    from osimflow.algorithms.spea2 import SPEA2Algorithm  # noqa: E402

    AlgorithmRegistry.register("nsga2", NSGA2Algorithm)
    AlgorithmRegistry.register("pso", PSOAlgorithm)
    AlgorithmRegistry.register("spea2", SPEA2Algorithm)
except ImportError:
    # pymoo is an optional dependency — NSGA-II, PSO, and SPEA-II are only
    # available when the [optimization] extra is installed.
    pass

try:
    from osimflow.algorithms.ga import GeneticAlgorithm  # noqa: F401, E402

    AlgorithmRegistry.register("ga", GeneticAlgorithm)
except ImportError:
    # deap is a required dependency for GeneticAlgorithm — only available
    # when the [ga] extra is installed.
    pass

try:
    from osimflow.algorithms.gaisl import IslandModelGAAlgorithm  # noqa: F401, E402

    AlgorithmRegistry.register("gaisl", IslandModelGAAlgorithm)
except ImportError:
    # deap is a required dependency for IslandModelGAAlgorithm — only available
    # when the [ga] extra is installed.
    pass

from osimflow.algorithms.random_sampling import RandomSamplingAlgorithm  # noqa: E402
from osimflow.algorithms.repeat_all import RepeatAllAlgorithm  # noqa: E402

AlgorithmRegistry.register("repeat_all", RepeatAllAlgorithm)
AlgorithmRegistry.register("random", RandomSamplingAlgorithm)

from osimflow.algorithms.custom import CustomDOEAlgorithm  # noqa: E402

AlgorithmRegistry.register("custom", CustomDOEAlgorithm)

from osimflow.algorithms.uq import UncertaintyQuantification  # noqa: E402

AlgorithmRegistry.register("uq", UncertaintyQuantification)

from osimflow.algorithms.calibration import (  # noqa: E402
    BM25CalibrationAlgorithm,
)

AlgorithmRegistry.register("calibration", BM25CalibrationAlgorithm)

from osimflow.algorithms.sequential_search import (  # noqa: E402
    SequentialSearchAlgorithm,
)

AlgorithmRegistry.register("sequential_search", SequentialSearchAlgorithm)

# ======================================================================
# Entry-point plug-in discovery (issue #432)
# ======================================================================
# Discover and register third-party algorithm plug-ins that declare an
# entry point in the ``osimflow.algorithms`` group.  This is a no-op
# when no plug-ins are installed.  Import errors are caught and logged
# so a broken plug-in never blocks the registry.
AlgorithmRegistry.discover_plugins()
