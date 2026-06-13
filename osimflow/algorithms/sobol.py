"""Sobol quasi-random sequence sampler (issue #139).

Wraps ``scipy.stats.qmc.Sobol`` to produce low-discrepancy samples.
Sobol sequences provide better uniformity than pseudo-random sampling
for moderate-to-high dimensional spaces and are particularly effective
when the sample count is a power of 2.

After sample generation and KPI extraction, use
:meth:`compute_sensitivity_indices` to compute first-order (S1) and
total-effect (ST) sensitivity indices via SALib's ``sobol.analyze()``.
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
    _resolve_conditional,
    _sample_with_engine,
    _write_empty_samples,
)

log = logging.getLogger("osimflow.algorithms.sobol")


def _build_salib_problem(
    independent_vars: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build a SALib problem dict from OSimFlow variable definitions.

    SALib requires ``{"num_vars": N, "names": [...], "bounds": [...]}``.
    Only *uniform* bounds are used for the problem spec; other
    distributions are applied as a post-processing step on the unit
    samples.
    """
    names: list[str] = []
    bounds: list[tuple[float, float]] = []
    for var_def in independent_vars:
        var_name = var_def["name"]
        dist = var_def.get("distribution", "uniform")
        names.append(var_name)
        if dist == "uniform":
            bounds.append((float(var_def["min"]), float(var_def["max"])))
        elif dist == "normal":
            # Use ±3σ as the sampling bounds for SALib
            mu = float(var_def["mean"])
            sigma = float(var_def["sigma"])
            bounds.append((mu - 3 * sigma, mu + 3 * sigma))
        elif dist == "lognormal":
            # Use ±3σ around the log-mean for sampling bounds
            log_mu = float(var_def["mean"])
            sigma = float(var_def["sigma"])
            lower = max(1e-10, log_mu - 3 * sigma)
            upper = log_mu + 3 * sigma
            bounds.append((lower, upper))
        elif dist == "triangular":
            bounds.append((float(var_def["min"]), float(var_def["max"])))
        else:
            # Fallback: use a [0, 1] bound; the _apply_distribution
            # step will handle the mapping.
            bounds.append((0.0, 1.0))
    return {"num_vars": len(names), "names": names, "bounds": bounds}


class SobolAlgorithm(BaseAlgorithm):
    """Sobol quasi-random sequence sampler using ``scipy.stats.qmc.Sobol``.

    Sobol sequences offer superior space-filling properties compared to
    pseudo-random sampling, with discrepancy decreasing as O(N⁻¹ logᵈN)
    versus O(N⁻¹/²) for Monte Carlo.  Best results are achieved when
    the sample count is a power of 2, but any positive integer works.

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

        try:
            samples = _sample_with_engine(
                scipy.stats.qmc.Sobol,
                independent_vars,
                n_samples,
                seed,
            )
        except (ValueError, NotImplementedError) as exc:
            raise RuntimeError("generate_sobol failed") from exc

        if conditional_vars:
            _resolve_conditional(samples, conditional_vars, n_samples)

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
        return "sobol"

    def is_iterative(self) -> bool:
        return False

    def compute_sensitivity_indices(  # noqa: PLR0912
        self,
        variables: dict[str, Any],
        samples: list[dict[str, Any]],
        kpi_values: dict[str, dict[str, float]],
        outdir: Path,
        calc_second_order: bool = False,
    ) -> Path:
        """Compute Sobol sensitivity indices from KPI values.

        Uses SALib's ``sobol.analyze()`` to compute first-order (S1) and
        total-effect (ST) sensitivity indices after KPI extraction.
        This method is called by the Campaign after the
        ``EXTRACT_KPIS`` step when running a Sobol analysis.

        Parameters
        ----------
        variables
            Parsed ``variables.yml`` dict (same as ``generate_samples``).
        samples
            List of sample dicts produced by ``generate_samples()``.
        kpi_values
            Dict mapping ``sample_id`` -> dict of KPI name -> value.
            The primary KPI used for sensitivity analysis is "eui"
            by default; any numeric KPI can be used.
        outdir
            Directory to write the sensitivity indices JSON.
        calc_second_order
            Whether to compute second-order (S2) indices. Requires
            ``N*(2D+2)`` samples where D is the number of variables.
            Defaults to ``False`` (requires only ``N*(D+2)`` samples).
        outdir
            Directory to write the sensitivity indices JSON.

        Returns
        -------
        Path
            Path to the written ``sensitivity_indices.json`` file.

        Raises
        ------
        RuntimeError
            When SALib's ``sobol.analyze()`` fails or no valid KPI
            values are provided.

        Requires the ``[sensitivity]`` extra (``SALib >= 1.4``).
        """
        from SALib.analyze import sobol as sobol_analyze  # noqa: PLC0415

        outdir.mkdir(parents=True, exist_ok=True)
        indices_path = outdir / "sensitivity_indices.json"

        var_list = _normalise_var_list(variables.get("variables", []))
        if not var_list:
            raise RuntimeError("compute_sensitivity_indices: no variables defined")

        independent_vars, _ = _partition_variables(var_list)
        if not independent_vars:
            raise RuntimeError("compute_sensitivity_indices: no independent variables")

        problem = _build_salib_problem(independent_vars)

        # Build the Y array: model outputs in the same order as samples.
        # SALib's sobol.analyze expects Y with shape (N,) where N is the
        # number of samples. We use the "eui" KPI by default; if not
        # available, fall back to the first numeric KPI found.
        y_list: list[float] = []
        for sample in samples:
            sid = str(sample["sample_id"])
            kpis = kpi_values.get(sid, {})
            # Try "eui" first, then first numeric KPI
            value: float | None = None
            if "eui" in kpis and isinstance(kpis["eui"], (int, float)):
                value = float(kpis["eui"])
            else:
                for _k, v in kpis.items():
                    if isinstance(v, (int, float)):
                        value = float(v)
                        break
            if value is None:
                raise RuntimeError(
                    f"compute_sensitivity_indices: no numeric KPI found for sample {sid}"
                )
            y_list.append(value)

        try:
            result = sobol_analyze.analyze(problem, np.array(y_list), calc_second_order=calc_second_order)
        except (ValueError, NotImplementedError) as exc:
            raise RuntimeError("compute_sensitivity_indices: SALib sobol.analyze failed") from exc

        # Extract scalar indices from SALib result dict.
        # SALib returns numpy arrays; convert to plain Python floats.
        def _to_float(val: Any) -> float:
            if hasattr(val, "__float__"):
                return float(val)
            return float(val)

        s1 = {n: _to_float(v) for n, v in zip(problem["names"], result["S1"], strict=True)}
        st = {n: _to_float(v) for n, v in zip(problem["names"], result["ST"], strict=True)}
        s2: dict[str, dict[str, float]] = {}
        if result.get("S2") is not None:
            s2_names = result.get("S2_names", [])
            s2_vals = result["S2"]
            for i, name_i in enumerate(problem["names"]):
                for j, name_j in enumerate(problem["names"]):
                    if j > i:
                        key = f"{name_i},{name_j}"
                        if key in s2_names:
                            idx = s2_names.index(key)
                            s2.setdefault(name_i, {})[name_j] = _to_float(s2_vals[idx])

        output: dict[str, Any] = {
            "algorithm": "sobol",
            "problem": problem,
            "indices": {
                "S1": s1,  # first-order sensitivity indices
                "ST": st,  # total-effect sensitivity indices
                "S2": s2,  # second-order sensitivity indices (optional)
            },
            "kpi_used": "eui" if "eui" in next(iter(kpi_values.values()), {}) else "first_numeric",
        }

        indices_path.write_text(json.dumps(output, indent=2))
        log.info(
            "Sobol sensitivity indices computed for %d variables, %d samples",
            len(problem["names"]),
            len(y_list),
        )
        return indices_path
