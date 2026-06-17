"""Diag Algorithm — one-at-a-time (OAT) diagnostic analysis (issue #581).

Implements the diagonal / OAT sampling strategy used by openstudio-server's
``diag.rb``.  For each variable, the algorithm draws a sample from that
variable's distribution while holding all other variables at their baseline
(mode) values.  This produces ``n_samples × n_variables`` total samples
and is useful for diagnostic sensitivity analysis where the contribution
of each individual variable is isolated.

Uses ``scipy.stats.triang`` for triangular distribution sampling, matching
the approach of R's ``triangle`` package used in the original OSS implementation.

The algorithm name is ``diag`` and the ``experiment_type`` written to
``samples.json`` is ``diagonal``.
"""

import json
import logging
from pathlib import Path
from typing import Any

import numpy as np

from osimflow.algorithms import (
    BaseAlgorithm,
    _apply_distribution,
    _normalise_var_list,
    _partition_variables,
    _resolve_conditional,
    _write_empty_samples,
)

log = logging.getLogger("osimflow.algorithms.diag")


def _baseline_value(var_def: dict[str, Any]) -> float:
    """Return the baseline (mode / most-likely) value for a variable definition.

    For triangular distributions the mode is explicitly stored in ``mode``.
    For uniform distributions the midpoint ``(min + max) / 2`` is used.
    For normal/lognormal the mean is used.  For discrete/categorical the
    first value in the ``values`` list is used.
    """
    dist = var_def.get("distribution", "uniform")
    left = float(var_def.get("min", 0.0))
    right = float(var_def.get("max", 1.0))
    mid = float((left + right) / 2.0)

    result: float
    if dist == "triangular":
        mode = var_def.get("mode")
        result = float(mode) if mode is not None else mid
    elif dist == "uniform":
        result = mid
    elif dist in ("normal", "lognormal"):
        result = float(var_def.get("mean", 0.0))
    elif dist in ("discrete", "categorical"):
        values: list[Any] = var_def.get("values", [])
        result = float(values[0]) if (values and isinstance(values[0], (int, float))) else 0.0
    elif dist == "beta":
        alpha = var_def.get("alpha", 1.0)
        beta_param = var_def.get("beta", 1.0)
        result = float(alpha / (alpha + beta_param))
    elif dist == "gamma":
        result = float(var_def.get("alpha", 1.0))
    elif dist == "exponential":
        result = float(1.0 / var_def.get("rate", 1.0))
    else:
        result = mid
    return result


class DiagAlgorithm(BaseAlgorithm):
    """One-at-a-time (OAT) diagnostic analysis sampler.

    The Diag algorithm varies **one variable at a time** while holding all
    others at their baseline (mode) values.  It is a diagnostic sensitivity
    analysis method — distinct from Morris (which is a screening method that
    computes elementary effects across trajectories).

    The ``n_samples`` parameter controls how many distinct sample points are
    drawn per variable.  The total number of samples is
    ``n_samples × n_variables``.

    The ``experiment_type`` written to ``samples.json`` is ``diagonal``,
    matching the openstudio-server convention.

    Single-shot: ``is_iterative()`` returns ``False``,
    ``is_converged()`` always returns ``True``.
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

        rng = np.random.default_rng(seed)

        samples: list[dict[str, Any]] = []
        sample_counter = 0

        for var_idx, var_def in enumerate(independent_vars):
            dist_name = var_def.get("distribution", "uniform")
            params = {k: v for k, v in var_def.items() if k not in ("distribution", "name")}

            for _samp_idx in range(n_samples):
                values: dict[str, Any] = {}

                for j, other_def in enumerate(independent_vars):
                    other_name = other_def["name"]
                    if j == var_idx:
                        u = rng.random()
                        values[other_name] = _apply_distribution(
                            u, dist_name, params
                        )
                    else:
                        values[other_name] = _baseline_value(other_def)

                sample_counter += 1
                samples.append(
                    {"sample_id": f"{sample_counter:04d}", "values": values}
                )

        if conditional_vars:
            _resolve_conditional(samples, conditional_vars, len(samples))

        payload = {
            "experiment_type": "diagonal",
            "samples": samples,
        }
        samples_path.write_text(json.dumps(payload, indent=2))

        log.info(
            "DiagAlgorithm generated %d sample points "
            "(%d variables × %d samples/variable)",
            len(samples),
            len(independent_vars),
            n_samples,
        )
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
        return "diag"

    def is_iterative(self) -> bool:
        return False
