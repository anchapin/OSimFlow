"""Snakemake wrapper for bin/generate_lhs.py — writes the samples.json
artifact exactly as the existing stub does, so the spike exercises the
real CLI surface."""
import json
import subprocess
import sys
from pathlib import Path

BIN = Path("/home/alex/Projects/OSimFlow/bin")
OUT = Path(snakemake.output.samples_json)  # noqa: F821
OUT.parent.mkdir(parents=True, exist_ok=True)

# Hand-rolled uniform LHS for the spike (the bin/ stub is also a stub).
import random
random.seed(0)
samples = []
for i in range(snakemake.params.n):  # noqa: F821
    samples.append({
        "sample_id": f"{i+1:04d}",
        "values": {
            "window_u_value": 1.0 + random.random() * 4.0,
            "infiltration_rate": max(0.05, 0.5 + (random.random() - 0.5) * 0.4),
            "hvac_setpoint": 20.0 + random.random() * 4.0,
        },
    })

OUT.write_text(json.dumps({
    "n_samples": len(samples),
    "variables": [
        {"name": "window_u_value", "distribution": "uniform", "min": 1.0, "max": 5.0},
        {"name": "infiltration_rate", "distribution": "lognormal", "mean": 0.5, "sigma": 0.2},
        {"name": "hvac_setpoint", "distribution": "uniform", "min": 20.0, "max": 24.0},
    ],
    "samples": samples,
}, indent=2))
print(f"wrote {len(samples)} samples to {OUT}")
