"""qdiscrete — inverse-CDF (quantile) sampling for discrete distributions.

Provides :func:`qdiscrete`, which is the Python equivalent of R's
``DoE.base::qdiscrete()``: given a probability mass function (PMF) as a
``{value: probability}`` dict, it draws samples by inverting the cumulative
distribution function (CDF).

This enables probability-weighted sampling for discrete variables, which is
what openstudio-server's R code uses when running parametric campaigns with
non-uniform discrete distributions.

Example
-------
::

    >>> pmf = {"low": 0.2, "medium": 0.5, "high": 0.3}
    >>> qdiscrete(pmf, seed=42)   # single draw
    'medium'
    >>> qdiscrete(pmf, n=1000, seed=42)  # 1000 draws
    ['medium', 'high', 'low', ...]

Normalisation: the input probabilities are normalised to sum to 1.0, so
the caller does not need to pre-normalise.

Supports all distribution types that appear in ``variables.yml``:

- ``uniform``  — equal probability over ``values`` list
- ``normal``   — mean + sigma, converted to a discrete PMF over ``values``
- ``lognormal`` — mean + sigma of the underlying normal, converted to a
  discrete PMF over ``values``
- ``triangular`` — mode + min/max, converted to a discrete PMF over ``values``
- ``discrete`` — explicit ``{value: probability}`` PMF dict
"""

import bisect
import json
import logging
import math
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar

import numpy as np

if TYPE_CHECKING:
    from osimflow.algorithms import BaseAlgorithm

from osimflow.algorithms import BaseAlgorithm  # noqa: E402
from osimflow.errors import OSimFlowValueError

log = logging.getLogger("osimflow.algorithms.qdiscrete")

__all__ = ["qdiscrete", "pmf_from_distribution", "QDError", "QDiscreteAlgorithm"]


class QDError(OSimFlowValueError):
    """Raised when qdiscrete receives invalid input."""


def _check_pmf(pmf: dict[Any, float]) -> None:
    if not pmf:
        raise QDError("pmf must be non-empty")
    if any(p < 0 for p in pmf.values()):
        raise QDError(f"probabilities must be non-negative, got: {pmf!r}")
    total = sum(pmf.values())
    if not math.isfinite(total):
        raise QDError(f"pmf probabilities must sum to a finite value, got: {total}")


def qdiscrete(
    pmf: dict[Any, float],
    *,
    n: int = 1,
    seed: int | None = None,
) -> list[Any]:
    """Draw *n* samples from the inverse CDF of a discrete distribution.

    Parameters
    ----------
    pmf
        Probability mass function as a `` {value: probability} `` dict.
        Probabilities need not sum to 1 — they are normalised automatically.
        The keys (``value``) may be any hashable type (int, float, str, …).
    n
        Number of samples to draw.  Default is 1.
    seed
        Optional RNG seed for reproducibility.

    Returns
    -------
    list[Any]
        A list of *n* samples drawn from the distribution.  When *n* is 1
        the same list is returned (no scalar is returned to avoid type
        ambiguity with the key type).

    Raises
    ------
    QDError
        When *pmf* is empty, contains negative probabilities, or has a
        non-finite total probability.

    Examples
    --------
    Weighted discrete:

    >>> qdiscrete({"a": 0.1, "b": 0.9}, n=3, seed=0)
    ['b', 'b', 'b']

    Equal-probability uniform over three values:

    >>> qdiscrete({"x": 1, "y": 1, "z": 1}, n=3, seed=0)
    ['z', 'x', 'y']
    """
    if n < 1:
        raise QDError(f"n must be >= 1, got {n}")

    _check_pmf(pmf)

    values = list(pmf.keys())
    probs = np.array(list(pmf.values()), dtype=float)
    probs = probs / probs.sum()

    cdf = np.cumsum(probs)
    if cdf[-1] != 1.0:
        cdf = cdf / cdf[-1]

    rng = np.random.default_rng(seed=seed)
    u = rng.random(size=n)

    indices = [bisect.bisect_right(cdf, ui) for ui in u]
    return [values[i] if i < len(values) else values[-1] for i in indices]


# ---------------------------------------------------------------------------
# PMF constructors for each distribution type
# ---------------------------------------------------------------------------


def _uniform_pmf(values: list[Any]) -> dict[Any, float]:
    n = len(values)
    if n == 0:
        raise QDError("uniform distribution requires at least one value")
    return {v: 1.0 for v in values}


def _normal_pmf(
    values: list[float],
    mean: float,
    sigma: float,
) -> dict[float, float]:
    if sigma <= 0:
        raise QDError(f"normal sigma must be > 0, got {sigma}")
    float_values = [float(v) for v in values]
    z = [(v - mean) / sigma for v in float_values]
    densities = [math.exp(-0.5 * zi * zi) for zi in z]
    total = sum(densities)
    if total == 0:
        log.warning("normal PMF has zero total density; using uniform over values")
        return _uniform_pmf(values)
    return {v: d / total for v, d in zip(values, densities, strict=True)}


def _lognormal_pmf(
    values: list[float],
    mean: float,
    sigma: float,
) -> dict[float, float]:
    if sigma <= 0:
        raise QDError(f"lognormal sigma must be > 0, got {sigma}")
    float_values = [float(v) for v in values]
    log_values = [math.log(v) for v in float_values if v > 0]
    if len(log_values) != len(values):
        raise QDError("lognormal values must all be positive")
    log_mean = sum(log_values) / len(log_values) if log_values else 0.0
    z = [(lv - log_mean) / sigma for lv in log_values]
    densities = [math.exp(-0.5 * zi * zi) for zi in z]
    total = sum(densities)
    if total == 0:
        log.warning("lognormal PMF has zero total density; using uniform over values")
        return _uniform_pmf(values)
    return {v: d / total for v, d in zip(values, densities, strict=True)}


def _triangular_pmf(
    values: list[float],
    min_val: float,
    max_val: float,
    mode: float | None = None,
) -> dict[float, float]:
    if min_val >= max_val:
        raise QDError(f"triangular min must be < max, got {min_val}, {max_val}")
    float_values = sorted(float(v) for v in values)
    c_mode = (min_val + max_val) / 2.0 if mode is None else float(mode)
    if not (min_val <= c_mode <= max_val):
        raise QDError(f"triangular mode {c_mode} must be within [min, max]=[{min_val}, {max_val}]")
    c = (c_mode - min_val) / (max_val - min_val)
    densities: list[float] = []
    for v in float_values:
        t = (float(v) - min_val) / (max_val - min_val)
        if t in {0, 1}:
            density = 0.0
        elif t < c:
            density = 2 * t / c
        else:
            density = 2 * (1 - t) / (1 - c)
        densities.append(density)
    total = sum(densities)
    if total == 0:
        log.warning("triangular PMF has zero total density; using uniform over values")
        return _uniform_pmf(values)
    return {v: d / total for v, d in zip(values, densities, strict=True)}


# ---------------------------------------------------------------------------
# Top-level factory
# ---------------------------------------------------------------------------

_DIST_PMF_BUILDERS: dict[str, Callable[..., dict[Any, float]]] = {}


_PMFBuilderFunc = TypeVar("_PMFBuilderFunc", bound=Callable[..., dict[Any, float]])


def _register_pmf_builder(name: str) -> Callable[[_PMFBuilderFunc], _PMFBuilderFunc]:
    def decorator(func: _PMFBuilderFunc) -> _PMFBuilderFunc:
        _DIST_PMF_BUILDERS[name] = func
        return func

    return decorator


@_register_pmf_builder("uniform")
def _build_uniform(var_def: dict[str, Any]) -> dict[Any, float]:
    values = var_def.get("values")
    if values is None:
        raise QDError("uniform distribution requires 'values' key")
    return _uniform_pmf(list(values))


@_register_pmf_builder("normal")
def _build_normal(var_def: dict[str, Any]) -> dict[Any, float]:
    mean = var_def.get("mean")
    sigma = var_def.get("sigma")
    values = var_def.get("values")
    if mean is None or sigma is None:
        raise QDError("normal distribution requires 'mean' and 'sigma' keys")
    if values is None:
        raise QDError("normal distribution requires 'values' key")
    return _normal_pmf(list(values), float(mean), float(sigma))


@_register_pmf_builder("lognormal")
def _build_lognormal(var_def: dict[str, Any]) -> dict[Any, float]:
    mean = var_def.get("mean")
    sigma = var_def.get("sigma")
    values = var_def.get("values")
    if mean is None or sigma is None:
        raise QDError("lognormal distribution requires 'mean' and 'sigma' keys")
    if values is None:
        raise QDError("lognormal distribution requires 'values' key")
    return _lognormal_pmf(list(values), float(mean), float(sigma))


@_register_pmf_builder("triangular")
def _build_triangular(var_def: dict[str, Any]) -> dict[Any, float]:
    min_val = var_def.get("min")
    max_val = var_def.get("max")
    mode = var_def.get("mode")
    values = var_def.get("values")
    if min_val is None or max_val is None:
        raise QDError("triangular distribution requires 'min' and 'max' keys")
    if values is None:
        raise QDError("triangular distribution requires 'values' key")
    return _triangular_pmf(
        list(values), float(min_val), float(max_val), None if mode is None else float(mode)
    )


@_register_pmf_builder("discrete")
def _build_discrete(var_def: dict[str, Any]) -> dict[Any, float]:
    pmf = var_def.get("pmf")
    if pmf is not None:
        if not isinstance(pmf, dict):
            raise QDError(f"discrete 'pmf' must be a dict, got {type(pmf).__name__}")
        _check_pmf(pmf)
        return pmf
    values = var_def.get("values")
    if values is None:
        raise QDError("discrete distribution requires 'values' or 'pmf' key")
    return _uniform_pmf(list(values))


@_register_pmf_builder("categorical")
def _build_categorical(var_def: dict[str, Any]) -> dict[Any, float]:
    return _build_discrete(var_def)


class QDiscreteAlgorithm(BaseAlgorithm):
    """Inverse-CDF (quantile) sampling for discrete distributions.

    Wraps :func:`qdiscrete` (the Python equivalent of R's
    ``DoE.base::qdiscrete()``) to provide probability-weighted sampling
    for discrete variables.  Unlike LHS which uses QMC stratification,
    this algorithm draws samples by inverting the cumulative distribution
    function (CDF) for variables with explicit PMFs.

    Single-shot: ``is_iterative()`` returns ``False``,
    ``is_converged()`` always returns ``True``.

    Supported distribution types: ``uniform``, ``normal``, ``lognormal``,
    ``triangular``, ``discrete``, ``categorical``.
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

        var_list: Any = variables.get("variables", [])
        if isinstance(var_list, list) and var_list and isinstance(var_list[0], dict):
            var_defs = var_list
        elif isinstance(var_list, dict):
            var_defs = [{"name": k, **v} for k, v in var_list.items()]
        else:
            var_defs = []

        if not var_defs:
            samples_path.write_text(json.dumps({"samples": []}, indent=2))
            return samples_path

        independent_vars: list[dict[str, Any]] = []
        for var_def in var_defs:
            if var_def.get("distribution") != "conditional":
                independent_vars.append(var_def)

        samples: list[dict[str, Any]] = []
        for i in range(n_samples):
            values: dict[str, Any] = {}
            for var_def in independent_vars:
                var_name = var_def["name"]
                try:
                    pmf = pmf_from_distribution(var_def)
                    drawn = qdiscrete(pmf, n=1, seed=seed)
                    values[var_name] = drawn[0]
                except QDError:
                    values[var_name] = None
            samples.append({"sample_id": f"{i + 1:04d}", "values": values})

        samples_path.write_text(json.dumps({"samples": samples}, indent=2))
        return samples_path

    def observe(self, history: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Single-shot: return the samples from the last iteration."""
        if not history:
            return []
        last = history[-1].get("samples", [])
        return list(last)

    def is_converged(self, history: list[dict[str, Any]]) -> bool:
        """Single-shot algorithms are always converged."""
        return True

    def name(self) -> str:
        return "qdiscrete"

    def is_iterative(self) -> bool:
        return False


def pmf_from_distribution(var_def: dict[str, Any]) -> dict[Any, float]:
    """Build a normalised PMF dict from a variables.yml variable definition.

    Parameters
    ----------
    var_def
        A variable definition dict from ``variables.yml``.  Must contain
        a ``distribution`` key.  Distribution-specific keys (``values``,
        ``mean``, ``sigma``, ``min``, ``max``, ``mode``, ``pmf``) are
        extracted automatically.

    Returns
    -------
    dict[Any, float]
        A PMF dict mapping each value to its normalised probability.

    Raises
    ------
    QDError
        When the distribution is unknown or required keys are missing.

    Supported distributions: ``uniform``, ``normal``, ``lognormal``,
    ``triangular``, ``discrete``, ``categorical``.
    """
    dist = str(var_def.get("distribution", "uniform")).lower()
    builder = _DIST_PMF_BUILDERS.get(dist)
    if builder is None:
        raise QDError(f"unsupported distribution for qdiscrete PMF: {dist!r}")
    return builder(var_def)
