"""Full factorial and grid sampling algorithms for Design of Experiments (issue #272).

Provides two DOE strategies:

- **FullFactorialAlgorithm** — cartesian product of discrete levels per
  variable.  The total sample count equals the product of all level
  counts; the caller's ``n_samples`` is advisory only (a warning is
  logged when it doesn't match the actual count).

  When a variable defines ``discrete_distribution`` with a ``pmf`` key,
  the algorithm uses :func:`osimflow.algorithms.qdiscrete.qdiscrete` to draw
  ``n_samples`` values weighted by the probability mass function instead
  of the exhaustive cartesian product over ``levels``.  This matches
  R's ``DoE.base::qdiscrete()`` weighted discrete variable behaviour
  (issue #579).

- **GridSamplingAlgorithm** — evenly-spaced grid over continuous
  parameter ranges.  Each variable specifies ``grid_points`` (default 5)
  and ``min``/``max`` bounds.  The total sample count is
  ``grid_points_per_dim ** n_dims`` when all variables share the same
  ``grid_points`` value.

Both are single-shot (``is_iterative() == False``) and always converged.
"""

import itertools
import json
import logging
from pathlib import Path
from typing import Any

from osimflow.algorithms import (
    BaseAlgorithm,
    _normalise_var_list,
    _partition_variables,
    _resolve_conditional,
    _write_empty_samples,
)
from osimflow.algorithms.qdiscrete import qdiscrete

log = logging.getLogger("osimflow.algorithms.factorial")


# ======================================================================
# Full Factorial
# ======================================================================


def _fullfact_samples(
    var_defs: list[dict[str, Any]],
    n_samples: int,
    seed: int | None,
) -> list[dict[str, Any]]:
    """Draw samples using qdiscrete when discrete_distribution/pmf is present.

    For each variable that carries a ``discrete_distribution`` entry with
    a ``pmf`` key, :func:`osimflow.algorithms.qdiscrete.qdiscrete` is used
    to draw ``n_samples`` values weighted by the probability mass function.
    For variables without a ``discrete_distribution`` pmf, the standard
    cartesian product over ``levels`` is used.

    Parameters
    ----------
    var_defs
        List of normalised variable definition dicts.
    n_samples
        Number of samples to produce (used as the qdiscrete draw count).
    seed
        RNG seed passed through to qdiscrete.

    Returns
    -------
    list[dict[str, Any]]
        List of sample dicts with ``sample_id`` and ``values`` keys.
    """
    has_pmf: list[bool] = []
    var_names: list[str] = []
    level_lists: list[list[Any]] = []
    discrete_pmf_vars: list[dict[Any, float] | None] = []

    for var_def in var_defs:
        var_name = var_def["name"]
        var_names.append(var_name)
        levels = var_def.get("levels")
        discrete_dist = var_def.get("discrete_distribution")
        pmf: dict[Any, float] | None = None
        if discrete_dist is not None and isinstance(discrete_dist, dict):
            pmf = discrete_dist.get("pmf")
        if pmf is not None:
            has_pmf.append(True)
            level_lists.append(list(pmf.keys()))
            discrete_pmf_vars.append(pmf)
        else:
            if levels is None:
                raise ValueError(
                    f"FullFactorialAlgorithm requires a 'levels' key for variable "
                    f"'{var_name}'. Got keys: {sorted(var_def.keys())}"
                )
            if not isinstance(levels, list) or len(levels) == 0:
                raise ValueError(
                    f"'levels' for variable '{var_name}' must be a non-empty list, got {levels!r}"
                )
            has_pmf.append(False)
            level_lists.append(list(levels))
            discrete_pmf_vars.append(None)

    has_any_pmf = any(has_pmf)
    if has_any_pmf:
        return _qdiscrete_weighted_samples(
            var_names=var_names,
            level_lists=level_lists,
            has_pmf=has_pmf,
            discrete_pmf_vars=discrete_pmf_vars,
            n_samples=n_samples,
            seed=seed,
        )

    cartesian = list(itertools.product(*level_lists))
    actual_count = len(cartesian)

    if n_samples != actual_count:
        log.warning(
            "FullFactorialAlgorithm: n_samples=%d ignored; actual cartesian "
            "product size is %d (product of levels: %s)",
            n_samples,
            actual_count,
            " × ".join(str(len(ll)) for ll in level_lists),
        )

    samples: list[dict[str, Any]] = []
    for i, combo in enumerate(cartesian):
        values: dict[str, Any] = {}
        for j, var_name in enumerate(var_names):
            values[var_name] = combo[j]
        samples.append({"sample_id": f"{i + 1:04d}", "values": values})
    return samples


def _qdiscrete_weighted_samples(
    var_names: list[str],
    level_lists: list[list[Any]],
    has_pmf: list[bool],
    discrete_pmf_vars: list[dict[Any, float] | None],
    n_samples: int,
    seed: int | None,
) -> list[dict[str, Any]]:
    """Draw samples using qdiscrete inverse-CDF weighted sampling.

    For each variable with ``has_pmf[i] is True``, draws ``n_samples`` values
    from the PMF via :func:`osimflow.algorithms.qdiscrete.qdiscrete`.
    For variables with ``has_pmf[i] is False``, draws ``n_samples`` values
    by cycling through the ``level_lists[i]`` cartesian product.
    """
    per_var_samples: list[list[Any]] = []

    for i, _var_name in enumerate(var_names):
        if has_pmf[i]:
            pmf = discrete_pmf_vars[i]
            assert pmf is not None
            drawn = qdiscrete(pmf, n=n_samples, seed=seed)
            per_var_samples.append(drawn)
        else:
            axis = level_lists[i]
            if n_samples <= len(axis):
                per_var_samples.append(list(axis[:n_samples]))
            else:
                repeats = (n_samples + len(axis) - 1) // len(axis)
                cycled = (axis * repeats)[:n_samples]
                per_var_samples.append(cycled)

    if n_samples != len(per_var_samples[0]):
        log.warning(
            "FullFactorialAlgorithm: qdiscrete-weighted sampling produced "
            "%d samples (n_samples=%d)",
            len(per_var_samples[0]),
            n_samples,
        )

    samples: list[dict[str, Any]] = []
    for i in range(len(per_var_samples[0])):
        values: dict[str, Any] = {}
        for j, var_name in enumerate(var_names):
            values[var_name] = per_var_samples[j][i]
        samples.append({"sample_id": f"{i + 1:04d}", "values": values})
    return samples


class FullFactorialAlgorithm(BaseAlgorithm):
    """Full factorial (cartesian product) over discrete levels.

    Each variable must declare a ``levels`` key — a list of values to
    sweep.  The algorithm produces every combination exactly once, so the
    total sample count is ``product(len(levels) for each variable)``.
    ``n_samples`` is **not** used to drive sampling; a warning is logged
    if the supplied ``n_samples`` differs from the actual cartesian count.

    When a variable defines ``discrete_distribution`` with a ``pmf`` key,
    the algorithm uses :func:`osimflow.algorithms.qdiscrete.qdiscrete` to draw
    ``n_samples`` values weighted by the probability mass function instead
    of the exhaustive cartesian product over ``levels``.  This matches
    R's ``DoE.base::qdiscrete()`` weighted discrete variable behaviour
    (issue #579).

    Single-shot: ``is_iterative()`` returns ``False``, ``is_converged()``
    always returns ``True``.
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

        var_list = _normalise_var_list(variables.get("variables", []))
        if not var_list:
            return _write_empty_samples(samples_path)

        independent_vars, conditional_vars = _partition_variables(var_list)
        if not independent_vars:
            return _write_empty_samples(samples_path)

        samples = _fullfact_samples(independent_vars, n_samples, seed)
        actual_count = len(samples)

        if conditional_vars:
            _resolve_conditional(samples, conditional_vars, actual_count)

        samples_path.write_text(json.dumps({"samples": samples}, indent=2))
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
        return "full_factorial"

    def is_iterative(self) -> bool:
        return False


# ======================================================================
# Grid Sampling
# ======================================================================


class GridSamplingAlgorithm(BaseAlgorithm):
    """Evenly-spaced grid over continuous parameter ranges.

    Each variable specifies ``min``, ``max``, and optionally
    ``grid_points`` (default 5).  The algorithm creates
    ``grid_points`` evenly-spaced values in ``[min, max]`` for each
    dimension and returns their cartesian product.

    The total sample count is ``product(grid_points_i)`` across all
    variables.  ``n_samples`` is advisory only (a warning is logged when
    it doesn't match).

    Single-shot: ``is_iterative()`` returns ``False``, ``is_converged()``
    always returns ``True``.
    """

    _DEFAULT_GRID_POINTS = 5

    def generate_samples(
        self,
        variables: dict[str, Any],
        n_samples: int,
        seed: int | None,
        outdir: Path,
    ) -> Path:
        outdir.mkdir(parents=True, exist_ok=True)
        samples_path = outdir / "samples.json"

        var_list = _normalise_var_list(variables.get("variables", []))
        if not var_list:
            return _write_empty_samples(samples_path)

        independent_vars, conditional_vars = _partition_variables(var_list)
        if not independent_vars:
            return _write_empty_samples(samples_path)

        # Build evenly-spaced grid points per dimension.
        grid_axes: list[list[float]] = []
        var_names: list[str] = []
        for var_def in independent_vars:
            var_name = var_def["name"]
            var_names.append(var_name)

            vmin = var_def.get("min")
            vmax = var_def.get("max")
            if vmin is None or vmax is None:
                raise ValueError(
                    f"GridSamplingAlgorithm requires 'min' and 'max' for variable "
                    f"'{var_name}'. Got keys: {sorted(var_def.keys())}"
                )
            grid_pts = var_def.get("grid_points", self._DEFAULT_GRID_POINTS)
            if not isinstance(grid_pts, int) or grid_pts < 1:
                raise ValueError(
                    f"'grid_points' for variable '{var_name}' must be a positive "
                    f"integer, got {grid_pts!r}"
                )

            # linspace: include both endpoints
            if grid_pts == 1:
                axis = [float(vmin)]
            else:
                step = (vmax - vmin) / (grid_pts - 1)
                axis = [vmin + i * step for i in range(grid_pts)]
            grid_axes.append(axis)

        # Cartesian product of all grid axes.
        cartesian = list(itertools.product(*grid_axes))
        actual_count = len(cartesian)

        if n_samples != actual_count:
            log.warning(
                "GridSamplingAlgorithm: n_samples=%d ignored; actual grid "
                "size is %d (grid_points: %s)",
                n_samples,
                actual_count,
                " × ".join(str(len(ax)) for ax in grid_axes),
            )

        samples: list[dict[str, Any]] = []
        for i, combo in enumerate(cartesian):
            values: dict[str, Any] = {}
            for j, var_name in enumerate(var_names):
                values[var_name] = combo[j]
            samples.append({"sample_id": f"{i + 1:04d}", "values": values})

        if conditional_vars:
            _resolve_conditional(samples, conditional_vars, actual_count)

        samples_path.write_text(json.dumps({"samples": samples}, indent=2))
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
        return "grid"

    def is_iterative(self) -> bool:
        return False
