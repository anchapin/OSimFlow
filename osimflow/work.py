"""Per-step work functions — what each logical process actually does.

These are the things the executor runs. They call the existing `bin/*.py`
stubs so the framework exercises the real CLI surface even though the
stubs themselves are no-ops.

The BYOS extension story falls out of this naturally: a user-supplied
`apply_parameters(template, params, sample_id, out)` function has the
same signature as `default_apply_parameters` below. The Campaign
discovers it via `inspect.signature` and calls it directly — no second
CLI surface to maintain.
"""
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

log = logging.getLogger("osimflow.work")

# Resolve the project root from the package location so the work layer
# does not hardcode a developer's local path. The convention is:
#   <project>/osimflow/work.py  →  <project>/
PROJECT_ROOT = Path(__file__).resolve().parent.parent
BIN = PROJECT_ROOT / "bin"


# ---------------------------------------------------------------------------
# BYOS contract: apply_parameters
# ---------------------------------------------------------------------------
def default_apply_parameters(
    template: Path,
    parameters: dict,
    sample_id: str,
    out: Path,
) -> Path:
    """Default parameter-application logic.

    A real implementation parses `template` (an .osm or .osw) and writes
    a modified copy to `out/<sample_id>/`. The current stub is the same
    one shipped in `bin/apply_params_to_model.py`; we call it as a
    subprocess to keep the BYOS contract identical: same argv, same exit
    code, same logging format.
    """
    out_dir = out / sample_id
    out_dir.mkdir(parents=True, exist_ok=True)
    param_file = out / f"{sample_id}.params.json"
    param_file.write_text(json.dumps(parameters, sort_keys=True))
    result = subprocess.run(
        [
            sys.executable, str(BIN / "apply_params_to_model.py"),
            "--template", str(template),
            "--parameter_set", str(param_file),
            "--sample_id", sample_id,
            "--out", str(out_dir),
        ],
        check=False, capture_output=True, text=True,
    )
    if result.returncode != 0:
        log.error("apply_params failed for %s: %s", sample_id, result.stderr)
        raise RuntimeError(f"apply_params failed for {sample_id}")
    return out_dir


# ---------------------------------------------------------------------------
# Simulation work: the heavy step. STUB.
# ---------------------------------------------------------------------------
# This stub is a placeholder for the real implementation, which will
# invoke `openstudio.cli run -w workflow.osw` inside the
# `openstudio_cli_image:<version>` container. The container is selected
# by the executor via the `container` parameter on submit; the function
# itself only sees the work directory.
#
# The stub simulates work with a short sleep and writes placeholder
# eplusout.sql / eplusout.err so the downstream extract step has
# something to consume in tests. When wired to a real container, the
# body becomes `subprocess.run(["openstudio.cli", "run", ...])` with
# logging redirected to the per-sample log files written by the
# Campaign.

def run_openstudio_sim(
    modified_sim_package: Path,
    sample_id: str,
    openstudio_version: str,
    out: Path,
    simulate_work_s: float = 2.0,
) -> Path:
    """Run the OpenStudio simulation.

    Args:
        modified_sim_package: per-sample modified package from APPLY_PARAMETERS.
        sample_id: the sample's identifier (e.g. "0001").
        openstudio_version: pinned OpenStudio version (selects container tag).
        out: directory where simulation outputs are written.
        simulate_work_s: how long the stub sleeps to simulate work.

    Returns:
        Path to the simulation output directory (eplusout.sql inside).
    """
    sim_out = out / sample_id
    sim_out.mkdir(parents=True, exist_ok=True)
    log.info("simulating sample=%s version=%s -> %s",
             sample_id, openstudio_version, sim_out)
    # STUB: replace with `subprocess.run(["openstudio.cli", "run", ...])`
    # inside the openstudio_cli_image:<version> container.
    time.sleep(simulate_work_s)
    (sim_out / "eplusout.sql").write_text("-- placeholder sql")
    (sim_out / "eplusout.err").write_text("")  # success: empty err
    return sim_out


# ---------------------------------------------------------------------------
# KPI extraction: parses eplusout.sql into a JSON.
# ---------------------------------------------------------------------------
def extract_kpis(simulation_dir: Path, sample_id: str, out: Path) -> Path:
    """Run the default KPI extractor. Returns path to the kpi JSON file."""
    out.mkdir(parents=True, exist_ok=True)
    kpi_path = out / f"kpi_{sample_id}.json"
    result = subprocess.run(
        [
            sys.executable, str(BIN / "extract_kpis.py"),
            "--simulation_dir", str(simulation_dir),
            "--sample_id", sample_id,
            "--out", str(kpi_path),
        ],
        check=False, capture_output=True, text=True,
    )
    if result.returncode != 0:
        log.error("extract_kpis failed for %s: %s", sample_id, result.stderr)
        raise RuntimeError(f"extract_kpis failed for {sample_id}")
    return kpi_path


# ---------------------------------------------------------------------------
# Aggregation & plotting
# ---------------------------------------------------------------------------
def aggregate_results(kpi_files: list[Path], sim_dirs: list[Path], out: Path) -> dict:
    """Aggregate per-sample KPIs into CSV/Parquet/failed-CSV. Returns paths."""
    out.mkdir(parents=True, exist_ok=True)
    csv_path = out / "aggregated_results.csv"
    failed_path = out / "failed_simulations.csv"
    parquet_path = out / "aggregated_results.parquet"
    result = subprocess.run(
        [
            sys.executable, str(BIN / "aggregate_results.py"),
            "--kpis", *(str(p) for p in kpi_files),
            "--simulation_dirs", *(str(p) for p in sim_dirs),
            "--out_csv", str(csv_path),
            "--out_parquet", str(parquet_path),
            "--out_failed", str(failed_path),
        ],
        check=False, capture_output=True, text=True,
    )
    if result.returncode != 0:
        log.error("aggregate_results failed: %s", result.stderr)
        raise RuntimeError("aggregate_results failed")
    return {
        "csv": csv_path,
        "parquet": parquet_path,
        "failed": failed_path,
    }


def generate_plots(csv_path: Path, failed_path: Path, out: Path) -> list[Path]:
    """Render summary plots from the aggregated CSV. Returns list of plot files."""
    out.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [
            sys.executable, str(BIN / "generate_plots.py"),
            "--results_csv", str(csv_path),
            "--failed_csv", str(failed_path),
            "--outdir", str(out),
        ],
        check=False, capture_output=True, text=True,
    )
    if result.returncode != 0:
        log.error("generate_plots failed: %s", result.stderr)
        raise RuntimeError("generate_plots failed")
    return sorted(out.glob("*.png")) + sorted(out.glob("*.pdf"))
