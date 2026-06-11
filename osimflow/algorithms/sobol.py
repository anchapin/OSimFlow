"""Sobol quasi-random sequence sampler (issue #139).

Wraps ``scipy.stats.qmc.Sobol`` to produce low-discrepancy samples.
Sobol sequences provide better uniformity than pseudo-random sampling
for moderate-to-high dimensional spaces and are particularly effective
when the sample count is a power of 2.
"""

import json
import logging
from pathlib import Path
from typing import Any

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
