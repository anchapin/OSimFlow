"""Snakemake wrapper for aggregate_results — calls the real bin/ stub."""
import subprocess
import sys
from pathlib import Path

BIN = Path("/home/alex/Projects/OSimFlow/bin")
result = subprocess.run([
    sys.executable, str(BIN / "aggregate_results.py"),
    "--kpis", *snakemake.input.kpis,  # noqa: F821
    "--simulation_dirs", *snakemake.input.sims,  # noqa: F821
    "--out_csv", snakemake.output.csv,  # noqa: F821
    "--out_parquet", snakemake.output.parquet,  # noqa: F821
    "--out_failed", snakemake.output.failed,  # noqa: F821
], check=True, capture_output=True, text=True)
print("aggregated")
