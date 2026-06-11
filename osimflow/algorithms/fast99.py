"""Fourier Amplitude Sensitivity Test (FAST99) sampler (issue #136).

Wraps ``SALib.sample.fast_sampler.sample`` to produce samples for global
sensitivity analysis using the extended FAST method (FAST99).  FAST99
decomposes the total variance of model output into first-order effects
attributable to each input factor using Fourier transforms.

Requires the ``[sensitivity]`` extra::

    pip install osimflow[sensitivity]
"""

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
from osimflow.algorithms.morris import _build_salib_problem

log = logging.getLogger("osimflow.algorithms.fast99")


class FAST99Algorithm(BaseAlgorithm):
    """FAST99 global sensitivity analysis sampler using SALib.

    The extended Fourier Amplitude Sensitivity Test (FAST99) uses
    Fourier transforms to decompose output variance into first-order
    and total-effect sensitivity indices.  It provides a variance-based
    decomposition similar to Sobol indices but uses a different
    sampling strategy based on search curves driven by characteristic
    frequencies.

    The ``n_samples`` parameter maps to SALib's ``N`` (number of
    sample points per factor).

    Single-shot: ``is_iterative()`` returns ``False``, ``is_converged()``
    always returns ``True``.

    Requires the ``[sensitivity]`` extra (``SALib >= 1.4``).
    """

    def generate_samples(
        self,
        variables: dict[str, Any],
        n_samples: int,
        seed: int | None,
        outdir: Path,
    ) -> Path:
        from SALib.sample.fast_sampler import sample as fast_sample  # noqa: PLC0415

        outdir.mkdir(parents=True, exist_ok=True)
        samples_path = outdir / "samples.json"

        var_list = _normalise_var_list(variables.get("variables", []))
        if not var_list:
            return _write_empty_samples(samples_path)

        independent_vars, conditional_vars = _partition_variables(var_list)
        if not independent_vars:
            return _write_empty_samples(samples_path)

        problem = _build_salib_problem(independent_vars)

        try:
            raw_samples = fast_sample(problem, N=n_samples, seed=seed)
        except (ValueError, NotImplementedError) as exc:
            raise RuntimeError("generate_fast99 failed") from exc

        # Convert the numpy array to OSimFlow sample dicts.
        samples: list[dict[str, Any]] = []
        for i in range(raw_samples.shape[0]):
            values: dict[str, Any] = {}
            for j, var_def in enumerate(independent_vars):
                values[var_def["name"]] = float(raw_samples[i, j])
            samples.append({"sample_id": f"{i + 1:04d}", "values": values})

        if conditional_vars:
            _resolve_conditional(samples, conditional_vars, len(samples))

        samples_path.write_text(json.dumps({"samples": samples}, indent=2))
        log.info(
            "FAST99 generated %d sample points for %d variables",
            raw_samples.shape[0],
            len(independent_vars),
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
        return "fast99"

    def is_iterative(self) -> bool:
        return False
