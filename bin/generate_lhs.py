#!/usr/bin/env python3
"""generate_lhs.py — Latin Hypercube Sampling for OSimFlow.

Reads `variables.yml` and emits N parameter sets as JSON. See docs/OSimFlow.md
§4.2 (PROCESS_GENERATE_LHS_SAMPLES) for the contract.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import sys
from pathlib import Path
from typing import Any

import scipy.stats
import scipy.stats.qmc
import yaml

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("generate_lhs")

SUPPORTED_DISTRIBUTIONS = (
    "uniform",
    "lognormal",
    "normal",
    "triangular",
    "beta",
    "gamma",
    "exponential",
    "categorical",
)


def _apply_distribution(u: float, dist: str, params: dict[str, Any]) -> float:
    """Map a unit-sample value *u* ∈ [0, 1] through the named distribution PPF.

    Parameters
    ----------
    u : float
        Uniform sample in [0, 1] from the LHS engine.
    dist : str
        Distribution name (e.g. ``"uniform"``, ``"normal"``).
    params : dict
        Distribution-specific parameters read from ``variables.yml``.

    Returns
    -------
    float
        The transformed sample value.

    Raises
    ------
    ValueError
        If *dist* is not one of :data:`SUPPORTED_DISTRIBUTIONS`.
    KeyError
        If a required parameter is missing from *params*.
    """
    if dist == "uniform":
        min_val = params["min"]
        max_val = params["max"]
        return float(min_val + u * (max_val - min_val))

    if dist == "lognormal":
        mean = params["mean"]
        sigma = params["sigma"]
        return float(scipy.stats.lognorm.ppf(u, s=sigma, scale=math.exp(mean)))

    if dist == "normal":
        mean = params["mean"]
        sigma = params["sigma"]
        return float(scipy.stats.norm.ppf(u, loc=mean, scale=sigma))

    if dist == "triangular":
        left = params["min"]
        right = params["max"]
        # scipy triang c-parameter is the normalised peak position.
        # OSimFlow does not expose a peak/mode parameter, so we default
        # to a symmetric triangle (c = 0.5).
        mode = params.get("mode")
        if mode is not None:
            c = (mode - left) / (right - left)
        else:
            c = 0.5
        return float(scipy.stats.triang.ppf(u, c, loc=left, scale=right - left))

    if dist == "beta":
        alpha = params["alpha"]
        beta_val = params["beta"]
        loc = params.get("loc", 0.0)
        scale = params.get("scale", 1.0)
        return float(scipy.stats.beta.ppf(u, a=alpha, b=beta_val, loc=loc, scale=scale))

    if dist == "gamma":
        alpha = params["alpha"]
        loc = params.get("loc", 0.0)
        scale = params.get("scale", 1.0)
        return float(scipy.stats.gamma.ppf(u, a=alpha, loc=loc, scale=scale))

    if dist == "exponential":
        rate = params["rate"]
        return float(scipy.stats.expon.ppf(u, scale=rate))

    if dist == "categorical":
        values = params["values"]
        if not values:
            raise ValueError("categorical distribution requires a non-empty 'values' list")
        idx = min(int(u * len(values)), len(values) - 1)
        return values[idx]

    raise ValueError(
        f"unsupported distribution {dist!r}; choose from {', '.join(SUPPORTED_DISTRIBUTIONS)}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--variables_yml", required=True, type=Path)
    parser.add_argument("--n_samples", required=True, type=int)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()

    args.out.parent.mkdir(parents=True, exist_ok=True)

    if args.n_samples == 0:
        args.out.write_text(json.dumps({"n_samples": 0, "variables": [], "samples": []}, indent=2))
        return 0

    with args.variables_yml.open() as f:
        config = yaml.safe_load(f)
    variables = config.get("variables", [])
    if not variables:
        for i in range(args.n_samples):
            param_file = args.out.parent / f"{i + 1:04d}.params.json"
            param_file.write_text(json.dumps({}, indent=2))
        args.out.write_text(
            json.dumps(
                {
                    "n_samples": args.n_samples,
                    "variables": [],
                    "samples": [
                        {"sample_id": f"{i + 1:04d}", "values": {}} for i in range(args.n_samples)
                    ],
                },
                indent=2,
            )
        )
        return 0

    d = len(variables)
    sampler = scipy.stats.qmc.LatinHypercube(d=d, seed=0)
    lhs_samples = sampler.random(n=args.n_samples)

    samples = []
    for i in range(args.n_samples):
        values = {}
        for j, v in enumerate(variables):
            u = lhs_samples[i, j]
            dist = v.get("distribution")
            name = v["name"]

            values[name] = _apply_distribution(u, dist, v)

        samples.append({"sample_id": f"{i + 1:04d}", "values": values})

        # ALSO write per-sample parameter files (one per sample) so the nextflow
        # downstream process can tuple() them up.
        param_file = args.out.parent / f"{i + 1:04d}.params.json"
        param_file.write_text(json.dumps(values, indent=2))

    args.out.write_text(
        json.dumps(
            {
                "n_samples": args.n_samples,
                "variables": variables,
                "samples": samples,
            },
            indent=2,
        )
    )

    return 0


if __name__ == "__main__":
    sys.exit(main())
