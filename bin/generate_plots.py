#!/usr/bin/env python3
"""generate_plots.py — Render 1–3 static summary plots from aggregated results.

See docs/OSimFlow.md §4.2 (PROCESS_GENERATE_BASIC_PLOTS) for the contract.

This is a SKELETON. Implementation TODO:

  1. Read aggregated_results.csv.
  2. Render at minimum:
       a) eui_histogram.{png,pdf}     — distribution of EUI across samples
       b) top_var_vs_eui.{png,pdf}    — scatter of most-variable design dim
                                          against EUI
       c) failure_summary.{png,pdf}   — bar chart of failure reasons from
                                          failed_simulations.csv (if any)
  3. Use a neutral accessible color palette (e.g., seaborn "colorblind").
  4. Save both PNG (for quick viewing) and PDF (for publication-quality).

Run with `--help` once implemented.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("generate_plots")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results_csv", required=True, type=Path)
    parser.add_argument("--failed_csv", required=True, type=Path)
    parser.add_argument("--outdir", required=True, type=Path)
    args = parser.parse_args()

    log.warning("generate_plots.py is a stub — writing placeholder PNG")
    args.outdir.mkdir(parents=True, exist_ok=True)
    (args.outdir / "eui_histogram.png").write_bytes(b"")
    return 0


if __name__ == "__main__":
    sys.exit(main())
