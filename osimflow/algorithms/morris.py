"""Morris method sensitivity analysis sampler (issue #136).

Wraps ``SALib.sample.morris.sample`` to produce trajectory-based samples
for Elementary Effects screening.  The Morris method is designed for
factor screening — identifying which input variables have a significant
influence on model outputs with a relatively small number of model
evaluations.

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

log = logging.getLogger("osimflow.algorithms.morris")


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


class MorrisAlgorithm(BaseAlgorithm):
    """Morris method sensitivity analysis sampler using SALib.

    The Morris method generates *trajectories* through the input space
    and computes elementary effects for each factor.  It is a
    one-at-a-time (OAT) screening method that identifies which inputs
    are important, unimportant, or have non-linear/interaction effects.

    The ``n_samples`` parameter maps to SALib's ``N`` (number of
    trajectories).  The actual number of model evaluations is
    ``N * (D + 1)`` where D is the number of input variables.

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
        from SALib.sample.morris import sample as morris_sample  # noqa: PLC0415

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
            raw_samples = morris_sample(
                problem,
                N=n_samples,
                num_levels=4,
                optimal_trajectories=None,
                seed=seed,
            )
        except (ValueError, NotImplementedError) as exc:
            raise RuntimeError("generate_morris failed") from exc

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
            "Morris generated %d sample points for %d variables",
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
        return "morris"

    def is_iterative(self) -> bool:
        return False
