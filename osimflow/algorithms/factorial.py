"""Full factorial and grid sampling algorithms for Design of Experiments (issue #272).

Provides two DOE strategies:

- **FullFactorialAlgorithm** — cartesian product of discrete levels per
  variable.  The total sample count equals the product of all level
  counts; the caller's ``n_samples`` is advisory only (a warning is
  logged when it doesn't match the actual count).

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
import math
from pathlib import Path
from typing import Any

from osimflow.algorithms import (
    BaseAlgorithm,
    _normalise_var_list,
    _partition_variables,
    _resolve_conditional,
    _write_empty_samples,
)

log = logging.getLogger("osimflow.algorithms.factorial")


# ======================================================================
# Full Factorial
# ======================================================================


class FullFactorialAlgorithm(BaseAlgorithm):
    """Full factorial (cartesian product) over discrete levels.

    Each variable must declare a ``levels`` key — a list of values to
    sweep.  The algorithm produces every combination exactly once, so the
    total sample count is ``product(len(levels) for each variable)``.
    ``n_samples`` is **not** used to drive sampling; a warning is logged
    if the supplied ``n_samples`` differs from the actual cartesian count.

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

        # Extract levels per variable.  Each independent variable must
        # have a ``levels`` key.
        level_lists: list[list[Any]] = []
        var_names: list[str] = []
        for var_def in independent_vars:
            var_name = var_def["name"]
            var_names.append(var_name)
            levels = var_def.get("levels")
            if levels is None:
                raise ValueError(
                    f"FullFactorialAlgorithm requires a 'levels' key for variable "
                    f"'{var_name}'. Got keys: {sorted(var_def.keys())}"
                )
            if not isinstance(levels, list) or len(levels) == 0:
                raise ValueError(
                    f"'levels' for variable '{var_name}' must be a non-empty list, "
                    f"got {levels!r}"
                )
            level_lists.append(list(levels))

        # Cartesian product of all level lists.
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
