"""Snakemake wrapper for generate_plots — calls the real bin/ stub."""
import subprocess
import sys
from pathlib import Path

BIN = Path("/home/alex/Projects/OSimFlow/bin")
plots_dir = Path(snakemake.output.plots_dir)  # noqa: F821
plots_dir.mkdir(parents=True, exist_ok=True)

result = subprocess.run([
    sys.executable, str(BIN / "generate_plots.py"),
    "--results_csv", snakemake.input.csv,  # noqa: F821
    "--failed_csv", snakemake.input.failed,  # noqa: F821
    "--outdir", str(plots_dir),
], check=True, capture_output=True, text=True)
print("plots generated")
