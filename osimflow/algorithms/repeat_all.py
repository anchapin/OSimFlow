"""Repeat All algorithm — runs the same sample set N times (issue #285).

``RepeatAllAlgorithm`` generates a base set of samples (using Latin
Hypercube Sampling by default) and then repeats it *repeats* times.
This is useful for stochastic analysis where the same parameter
combinations must be evaluated multiple times to capture output
variability.

Single-shot: ``is_iterative()`` returns ``False``,
``is_converged()`` always returns ``True``.
"""

import json
import logging
from pathlib import Path
from typing import Any

from osimflow.algorithms import (
    BaseAlgorithm,
    LHSAlgorithm,
    _normalise_var_list,
    _partition_variables,
    _resolve_conditional,
    _write_empty_samples,
)

log = logging.getLogger("osimflow.algorithms.repeat_all")


class RepeatAllAlgorithm(BaseAlgorithm):
    """Repeat a base sample set N times for stochastic analysis.

    The base samples are generated using ``LHSAlgorithm`` (Latin
    Hypercube Sampling) to ensure good space-filling coverage.
    These base samples are then repeated ``repeats`` times, with each
    repeat assigned a distinct repetition index so that per-sample
    outputs can be disambiguated.

    ``n_samples`` controls the number of *unique* parameter
    combinations.  The total number of samples written to
    ``samples.json`` is ``n_samples × repeats``.

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

        repeats = int(variables.get("repeat_all_repeats", 1))
        repeats = max(repeats, 1)

        lhs_algo = LHSAlgorithm()
        try:
            base_path = lhs_algo.generate_samples(variables, n_samples, seed, outdir / "base")
        except RuntimeError as exc:
            raise RuntimeError("repeat_all failed to generate base samples") from exc

        base_data = json.loads(base_path.read_text())
        base_samples: list[dict[str, Any]] = base_data.get("samples", [])
        if not base_samples:
            return _write_empty_samples(samples_path)

        all_samples: list[dict[str, Any]] = []
        for rep in range(repeats):
            for s in base_samples:
                values_copy: dict[str, Any] = dict(s["values"])
                sample_id = f"r{rep + 1}-{s['sample_id']}"
                all_samples.append({"sample_id": sample_id, "values": values_copy})

        if conditional_vars:
            _resolve_conditional(all_samples, conditional_vars, len(all_samples))

        samples_path.write_text(json.dumps({"samples": all_samples}, indent=2))
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
        return "repeat_all"

    def is_iterative(self) -> bool:
        return False
