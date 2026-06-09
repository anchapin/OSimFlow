#!/usr/bin/env python3
"""generate_lhs.py — Latin Hypercube Sampling for OSimFlow.

Reads `variables.yml` and emits N parameter sets as JSON. See docs/OSimFlow.md
§4.2 (PROCESS_GENERATE_LHS_SAMPLES) for the contract.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
import math

import numpy as np
import scipy.stats
import scipy.stats.qmc
import yaml

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("generate_lhs")


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
        args.out.write_text(json.dumps({"n_samples": args.n_samples, "variables": [], "samples": [{"sample_id": f"{i+1:04d}", "values": {}} for i in range(args.n_samples)]}, indent=2))
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

            if dist == "uniform":
                min_val = v["min"]
                max_val = v["max"]
                values[name] = float(min_val + u * (max_val - min_val))
            elif dist == "lognormal":
                mean = v["mean"]
                sigma = v["sigma"]
                # PPF (percent point function) of lognormal distribution
                # which maps from [0,1] to lognormal values.
                # Since scipy.stats.lognorm expects shape, loc, scale
                # The normal lognorm parametarization corresponding to mu and sigma of the underlying normal
                # is s=sigma, scale=exp(mu)
                values[name] = float(scipy.stats.lognorm.ppf(u, s=sigma, scale=math.exp(mean)))
            else:
                raise NotImplementedError(f"distribution {dist!r} not in MVP yet")

        samples.append({"sample_id": f"{i+1:04d}", "values": values})

        # ALSO write per-sample parameter files (one per sample) so the nextflow
        # downstream process can tuple() them up.
        param_file = args.out.parent / f"{i+1:04d}.params.json"
        param_file.write_text(json.dumps(values, indent=2))

    args.out.write_text(json.dumps({
        "n_samples": args.n_samples,
        "variables": variables,
        "samples": samples,
    }, indent=2))

    return 0


if __name__ == "__main__":
    sys.exit(main())
