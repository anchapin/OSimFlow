"""Halton quasi-random sequence sampler (issue #139).

Wraps ``scipy.stats.qmc.Halton`` to produce low-discrepancy samples.
The Halton sequence uses the van der Corput sequence in different bases
for each dimension, providing good uniformity for low-to-moderate
dimensional spaces.
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

log = logging.getLogger("osimflow.algorithms.halton")


class HaltonAlgorithm(BaseAlgorithm):
    """Halton quasi-random sequence sampler using ``scipy.stats.qmc.Halton``.

    The Halton sequence generalises the van der Corput sequence to
    multiple dimensions by using co-prime bases (2, 3, 5, 7, …).  It
    performs well in low-to-moderate dimensions but may show correlation
    artefacts in very high dimensions (d > ~20); Sobol is generally
    preferred for those cases.

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
                scipy.stats.qmc.Halton,
                independent_vars,
                n_samples,
                seed,
            )
        except (ValueError, NotImplementedError) as exc:
            raise RuntimeError("generate_halton failed") from exc

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
        return "halton"

    def is_iterative(self) -> bool:
        return False
