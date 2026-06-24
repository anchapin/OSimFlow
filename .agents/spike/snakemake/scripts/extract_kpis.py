"""Snakemake wrapper for extract_kpis — calls the real bin/ stub."""
import subprocess
import sys
from pathlib import Path

BIN = Path("/home/alex/Projects/OSimFlow/bin")
sim_dir = Path(snakemake.input.sim_dir)  # noqa: F821
out = Path(snakemake.output.kpi_json)  # noqa: F821
out.parent.mkdir(parents=True, exist_ok=True)

result = subprocess.run([
    sys.executable, str(BIN / "extract_kpis.py"),
    "--simulation_dir", str(sim_dir),
    "--sample_id", out.stem.replace("kpi_", ""),
    "--out", str(out),
], check=True, capture_output=True, text=True)
print(f"extracted kpis -> {out}")
