"""Random (Monte Carlo) sampling algorithm (issue #285).

``RandomSamplingAlgorithm`` uses simple random sampling — each
parameter value is drawn independently from the unit cube and mapped
through the distribution PPF.  Unlike LHS (stratified) or quasi-random
(Halton/Sobol) sequences, pure Monte Carlo sampling has no guarantee
of space-filling uniformity but is the simplest possible approach and
can be appropriate for very high-dimensional spaces.

Single-shot: ``is_iterative()`` returns ``False``,
``is_converged()`` always returns ``True``.
"""

import json
import logging
import random as _random_module
from pathlib import Path
from typing import Any

from osimflow.algorithms import (
    BaseAlgorithm,
    _apply_distribution,
    _normalise_var_list,
    _partition_variables,
    _resolve_conditional,
    _write_empty_samples,
)

log = logging.getLogger("osimflow.algorithms.random_sampling")


class RandomSamplingAlgorithm(BaseAlgorithm):
    """Pure random (Monte Carlo) sampling.

    Draws ``n_samples`` points uniformly from the unit hypercube and
    maps each through the variable's distribution PPF.  This is
    equivalent to classical Monte Carlo simulation.

    Single-shot: ``is_iterative()`` returns ``False``.
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

        rng = _random_module.Random(seed)

        samples: list[dict[str, Any]] = []
        for i in range(n_samples):
            values: dict[str, Any] = {}
            for var_def in independent_vars:
                var_name = var_def["name"]
                dist_name = str(var_def["distribution"])
                params = {k: v for k, v in var_def.items() if k not in ("distribution", "name")}
                u = rng.random()
                values[var_name] = _apply_distribution(u, dist_name, params)
            samples.append({"sample_id": f"{i + 1:04d}", "values": values})

        if conditional_vars:
            _resolve_conditional(samples, conditional_vars, n_samples)

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
        return "random"

    def is_iterative(self) -> bool:
        return False
