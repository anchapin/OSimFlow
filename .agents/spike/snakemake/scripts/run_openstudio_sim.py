"""Snakemake wrapper for run_openstudio_sim — stub. The real implementation
would call `openstudio.cli run -w workflow.osw` inside the container."""
import time
from pathlib import Path

sim_dir = Path(snakemake.output.sim_dir)  # noqa: F821
sim_dir.mkdir(parents=True, exist_ok=True)
# Simulate a small amount of work so the spike has visible wall-clock.
time.sleep(2)
(sim_dir / "eplusout.sql").write_text("-- placeholder sql")
(sim_dir / "eplusout.err").write_text("")  # success
print(f"simulated -> {sim_dir}")
