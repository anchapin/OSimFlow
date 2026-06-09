#!/usr/bin/env python3
"""aggregate_results.py — Collect per-sample KPIs and identify failures.

See docs/OSimFlow.md §4.2 (PROCESS_AGGREGATE_RESULTS) for the contract.

This is a SKELETON. Implementation TODO:

  1. Read every `kpis/<sample_id>.json` file → wide-form DataFrame.
  2. Write aggregated_results.csv and aggregated_results.parquet.
  3. For each simulation_dir, check whether eplusout.err exists and is
     non-empty. If so, extract the FIRST "Severe Error" line:
        grep -m 1 "  * Severe" eplusout.err
     plus the exit code of the openstudio.cli invocation.
  4. Write failed_simulations.csv with columns:
        sample_id, error_summary, exit_code, log_path
  5. (Optional) detect additional failure modes: eplusout.sql missing,
     .err file present but no "Severe", EnergyPlus crash signature, etc.

Run with `--help` once implemented.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("aggregate_results")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kpis", required=True, nargs="+", type=Path)
    parser.add_argument("--simulation_dirs", required=True, nargs="+", type=Path)
    parser.add_argument("--out_csv", required=True, type=Path)
    parser.add_argument("--out_parquet", type=Path, default=None)
    parser.add_argument("--out_failed", required=True, type=Path)
    args = parser.parse_args()

    log.warning("aggregate_results.py is a stub — writing empty outputs")

    args.out_csv.parent.mkdir(parents=True, exist_ok=True)
    args.out_csv.write_text("sample_id\n")
    args.out_failed.write_text("sample_id,error_summary,exit_code,log_path\n")
    if args.out_parquet:
        # Real implementation writes a pyarrow Table here.
        args.out_parquet.touch()
    return 0


if __name__ == "__main__":
    sys.exit(main())
