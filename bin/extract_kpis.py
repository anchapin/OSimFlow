#!/usr/bin/env python3
"""extract_kpis.py — Parse one sample's simulation outputs into a KPI JSON.

See docs/OSimFlow.md §4.2 (PROCESS_EXTRACT_KPIS) for the contract.

This is a SKELETON. Implementation TODO:

  1. Open the simulation_dir containing eplusout.sql (+ optional report.csv,
     eplusout.err).
  2. Run user-defined KPI queries against the SQLite database. Use the
     openstudio_reporting_api where possible, otherwise raw sqlite3.
  3. Support `--custom_kpi_extractor` (BYOS): if provided, import the user
     function and call it with a structured `ctx` dict.
  4. Output a single JSON file per sample with the shape:
        {
          "sample_id": "0001",
          "openstudio_version": "3.4.0",
          "kpis": {
            "eui_kwh_m2_yr": 142.7,
            "total_site_energy_gj": 18.4,
            ...
          }
        }

Run with `--help` once implemented.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("extract_kpis")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--simulation_dir", required=True, type=Path)
    parser.add_argument("--sample_id", required=True)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--custom_kpi_extractor", type=Path, default=None)
    args = parser.parse_args()

    log.warning("extract_kpis.py is a stub — emitting empty KPIs")
    args.out.write_text(
        json.dumps(
            {"sample_id": args.sample_id, "openstudio_version": None, "kpis": {}},
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
