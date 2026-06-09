#!/usr/bin/env python3
"""generate_lhs.py — Latin Hypercube Sampling for OSimFlow.

Reads `variables.yml` and emits N parameter sets as JSON. See docs/OSimFlow.md
§4.2 (PROCESS_GENERATE_LHS_SAMPLES) for the contract.

This is a SKELETON. Implementation TODO:

  1. Parse variables.yml schema. Expected shape (illustrative):
        variables:
          - name: window_u_value
            distribution: uniform
            min: 1.0
            max: 5.0
          - name: infiltration_rate
            distribution: lognormal
            mean: 0.5
            sigma: 0.2
          - name: hvac_setpoint
            distribution: uniform
            min: 20.0
            max: 24.0
  2. Use scipy.stats.qmc.LatinHypercube to draw N samples in [0, 1]^d.
  3. Map [0, 1] -> the requested distribution per variable.
  4. Write JSON with shape:
        {
          "n_samples": N,
          "variables": [...],
          "samples": [
            {"sample_id": "0001", "values": {...}},
            ...
          ]
        }
  5. ALSO write per-sample parameter files (one per sample) so the nextflow
     downstream process can tuple() them up.

Run with `--help` once implemented.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("generate_lhs")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--variables_yml", required=True, type=Path)
    parser.add_argument("--n_samples", required=True, type=int)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()

    # TODO(impl): wire up scipy.stats.qmc.LatinHypercube + distribution mapping.
    log.warning("generate_lhs.py is a stub — emitting empty sample set")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps({"n_samples": 0, "variables": [], "samples": []}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
