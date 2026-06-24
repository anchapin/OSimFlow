#!/usr/bin/env python3
"""End-to-end spike runner.

Runs the custom-Python driver and the Snakemake spike against the same
5-sample workload and prints a side-by-side comparison of:
  * wall-clock time
  * number of files produced
  * line-count of the framework code
  * cache state

Usage:
    python run_spike.py
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

SPIKE_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = SPIKE_ROOT.parent.parent  # .agents/spike/ -> .agents/ -> OSimFlow/
WORK = SPIKE_ROOT / "work"
VENV = SPIKE_ROOT / ".venv"
FIXTURES = WORK / "fixtures"
VARIABLES_YML = FIXTURES / "variables.yml"
TEMPLATE = FIXTURES / "template_sim_package"


def _reset_workdir(p: Path) -> None:
    if p.exists():
        shutil.rmtree(p)
    p.mkdir(parents=True)


def _count_lines(paths: list[Path], exts: tuple[str, ...] = (".py",)) -> dict:
    out = {}
    for p in paths:
        if p.is_file() and p.suffix in exts:
            out[str(p.relative_to(SPIKE_ROOT))] = sum(
                1 for _ in p.read_text().splitlines() if _.strip()
            )
        elif p.is_dir():
            for child in p.rglob("*"):
                if child.is_file() and child.suffix in exts:
                    out[str(child.relative_to(SPIKE_ROOT))] = sum(
                        1 for _ in child.read_text().splitlines() if _.strip()
                    )
    return out


def _framework_size() -> dict:
    """Count non-blank, non-comment lines of the actual framework code.

    For Nextflow, we use the existing modules/ directory (7 files,
    ~300 lines total). For custom-Python, we count osimflow/*.py +
    run_campaign.py. For Snakemake, we count the Snakefile + scripts/.

    The point is: how much code must a contributor read to understand
    the framework's behavior? Lower is better.
    """
    nfx = 0
    for p in [PROJECT_ROOT / "main.nf"]:
        nfx += sum(1 for line in p.read_text().splitlines() if line.strip())
    for p in (PROJECT_ROOT / "modules").glob("*.nf"):
        nfx += sum(1 for line in p.read_text().splitlines() if line.strip())
    for p in [PROJECT_ROOT / "nextflow.config"]:
        nfx += sum(1 for line in p.read_text().splitlines() if line.strip())
    for p in (PROJECT_ROOT / "conf").glob("*.config"):
        nfx += sum(1 for line in p.read_text().splitlines() if line.strip())

    cpy = 0
    # Framework code only (osimflow/ + run_campaign.py). Excludes tests/
    # because tests are project hygiene, not framework surface area.
    for p in (SPIKE_ROOT / "custom_python" / "osimflow").rglob("*.py"):
        cpy += sum(1 for line in p.read_text().splitlines() if line.strip())
    for p in [(SPIKE_ROOT / "custom_python" / "run_campaign.py")]:
        cpy += sum(1 for line in p.read_text().splitlines() if line.strip())

    smk = 0
    for p in [(SPIKE_ROOT / "snakemake" / "Snakefile")]:
        smk += sum(1 for line in p.read_text().splitlines() if line.strip())
    for p in (SPIKE_ROOT / "snakemake" / "scripts").glob("*.py"):
        smk += sum(1 for line in p.read_text().splitlines() if line.strip())

    return {
        "Nextflow (existing skeleton)": nfx,
        "Custom Python (new)": cpy,
        "Snakemake (new)": smk,
    }


def run_custom_python(n_samples: int = 5) -> dict:
    outdir = WORK / "custom_python_out"
    _reset_workdir(outdir)
    cmd = [
        str(VENV / "bin" / "python"), str(SPIKE_ROOT / "custom_python" / "run_campaign.py"),
        "--executor", "local",
        "--max-workers", "4",
        "--input_variables", str(VARIABLES_YML),
        "--template_sim_package", str(TEMPLATE),
        "--n_samples", str(n_samples),
        "--outdir", str(outdir),
        "--openstudio_version", "3.11.0",
        "--log_level", "WARNING",
    ]
    env = os.environ.copy()
    env["OSIMFLOW_PROJECT_ROOT"] = str(PROJECT_ROOT)
    t0 = time.time()
    result = subprocess.run(cmd, env=env, capture_output=True, text=True)
    elapsed = time.time() - t0
    return {
        "name": "custom_python",
        "elapsed_s": elapsed,
        "returncode": result.returncode,
        "stdout_tail": result.stdout[-2000:],
        "stderr_tail": result.stderr[-2000:],
        "outdir": outdir,
    }


def run_snakemake(n_samples: int = 5) -> dict:
    outdir = WORK / "snakemake_out"
    _reset_workdir(outdir)
    # Update the config to point at the right paths.
    cfg_path = SPIKE_ROOT / "snakemake" / "config" / "config.yaml"
    cfg = {
        "input_variables": str(VARIABLES_YML),
        "template_sim_package": str(TEMPLATE),
        "n_samples": n_samples,
        "outdir": str(outdir),
        "openstudio_version": "3.11.0",
        "archive_intermediates": False,
    }
    import yaml
    cfg_path.write_text(yaml.dump(cfg))
    cmd = [
        str(VENV / "bin" / "snakemake"),
        "--cores", "4",
        "--snakefile", str(SPIKE_ROOT / "snakemake" / "Snakefile"),
        "--directory", str(SPIKE_ROOT / "snakemake"),
        "--rerun-triggers", "mtime",
        "--quiet",
    ]
    env = os.environ.copy()
    env["PATH"] = str(VENV / "bin") + ":" + env["PATH"]
    t0 = time.time()
    result = subprocess.run(cmd, env=env, capture_output=True, text=True)
    elapsed = time.time() - t0
    return {
        "name": "snakemake",
        "elapsed_s": elapsed,
        "returncode": result.returncode,
        "stdout_tail": result.stdout[-2000:],
        "stderr_tail": result.stderr[-2000:],
        "outdir": outdir,
    }


def main() -> int:
    print("=" * 70)
    print("OSimFlow framework spike — 5-sample workload, local executor")
    print("=" * 70)
    sizes = _framework_size()
    print("\n[Framework size, non-blank lines]")
    for k, v in sizes.items():
        print(f"  {k:35s} {v:6d}")

    print("\n[Run 1/2] custom-Python driver")
    cp = run_custom_python()
    print(f"  elapsed:  {cp['elapsed_s']:.2f}s")
    print(f"  rc:       {cp['returncode']}")
    if cp["returncode"] != 0:
        print("  stderr tail:", cp["stderr_tail"])

    print("\n[Run 2/2] Snakemake")
    sm = run_snakemake()
    print(f"  elapsed:  {sm['elapsed_s']:.2f}s")
    print(f"  rc:       {sm['returncode']}")
    if sm["returncode"] != 0:
        print("  stderr tail:", sm["stderr_tail"])

    print("\n[Artifacts produced]")
    for label, p in [("custom_python", cp["outdir"]), ("snakemake", sm["outdir"])]:
        n_files = sum(1 for _ in p.rglob("*") if _.is_file())
        has_csv = (p / "aggregated_results.csv").exists()
        has_failed = (p / "failed_simulations.csv").exists()
        has_plots = (p / "plots").exists() and any((p / "plots").glob("*.png"))
        print(f"  {label:15s} files={n_files:4d}  csv={has_csv}  failed={has_failed}  plots={has_plots}")

    print("\n[Side-by-side]")
    print(f"  {'framework':<15s} {'elapsed_s':>10s} {'returncode':>12s}")
    print(f"  {'custom_python':<15s} {cp['elapsed_s']:>10.2f} {cp['returncode']:>12d}")
    print(f"  {'snakemake':<15s} {sm['elapsed_s']:>10.2f} {sm['returncode']:>12d}")

    return 0 if (cp["returncode"] == 0 and sm["returncode"] == 0) else 1


if __name__ == "__main__":
    sys.exit(main())
