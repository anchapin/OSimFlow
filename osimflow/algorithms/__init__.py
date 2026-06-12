"""Algorithm plug-in framework for OSimFlow sampling strategies.

Provides the ``BaseAlgorithm`` abstract base class, the
``AlgorithmRegistry`` singleton for discovery/instantiation, and the
built-in ``LHSAlgorithm``, ``SobolAlgorithm``, ``HaltonAlgorithm``,
``MorrisAlgorithm``, ``FAST99Algorithm``, ``DifferentialEvolutionAlgorithm``,
``DualAnnealingAlgorithm``, ``NSGA2Algorithm``, ``PSOAlgorithm``, ``FullFactorialAlgorithm``, and
``GridSamplingAlgorithm`` implementations.

Adding a new algorithm (Bayesian optimisation, …) requires only:

1.  Subclass ``BaseAlgorithm``.
2.  Call ``AlgorithmRegistry.register("name", MyAlgorithm)`` at import
    time (typically at the bottom of this module or in a separate plugin
    module under ``osimflow/algorithms/``).

The ``Campaign`` class dispatches through the registry via
``AlgorithmRegistry.get(config.algorithm)`` so the orchestration layer
stays decoupled from the sampling strategy.
"""

import abc
import json
import logging
import math
from pathlib import Path
from typing import Any

import scipy.stats
import scipy.stats.qmc

log = logging.getLogger("osimflow.algorithms")


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
    def get(cls, name: str) -> BaseAlgorithm:
        """Instantiate and return the algorithm registered under *name*.

        Raises
        ------
        ValueError
            If *name* is not registered, with a helpful message listing
            available algorithms.
        """
        if name not in cls._registry:
            available = ", ".join(sorted(cls._registry)) or "(none)"
            raise ValueError(f"unknown algorithm '{name}'. Available algorithms: {available}")
        return cls._registry[name]()

    @classmethod
    def list_available(cls) -> list[str]:
        """Return the sorted list of registered algorithm names."""
        return sorted(cls._registry)


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

    AlgorithmRegistry.register("nsga2", NSGA2Algorithm)
    AlgorithmRegistry.register("pso", PSOAlgorithm)
except ImportError:
    # pymoo is an optional dependency — NSGA-II and PSO are only
    # available when the [optimization] extra is installed.
    pass
