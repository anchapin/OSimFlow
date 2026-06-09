#!/usr/bin/env python3
"""extract_kpis.py — Parse one sample's simulation outputs into a KPI JSON.

See docs/OSimFlow.md §4.2 (PROCESS_EXTRACT_KPIS) for the contract.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
import sqlite3
import importlib.util

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("extract_kpis")


def get_eui_kwh_m2_yr(sql_path: Path) -> float | None:
    if not sql_path.exists():
        return None
    try:
        conn = sqlite3.connect(sql_path)
        cur = conn.cursor()

        # EnergyPlus canonical EUI query
        cur.execute("""
            SELECT Value FROM TabularDataWithStrings
            WHERE ReportName = 'InitializationSummary'
              AND ReportForString = 'Entire Facility'
              AND TableName = 'Site and Source Energy'
              AND RowName = 'Total Site Energy'
              AND ColumnName = 'Energy Per Total Building Area'
              AND Units = 'MJ/m2'
        """)
        row = cur.fetchone()
        if row is not None:
            mj_m2_yr = float(row[0])
            # MJ to kWh conversion factor is 1/3.6
            return mj_m2_yr / 3.6

        # Fallback if TableName is slightly different in older versions
        return None
    except Exception as e:
        log.warning(f"Failed to read EUI from {sql_path}: {e}")
        return None
    finally:
        if 'conn' in locals():
            conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--simulation_dir", required=True, type=Path)
    parser.add_argument("--sample_id", required=True)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--custom_kpi_extractor", type=Path, default=None)
    args = parser.parse_args()

    args.out.parent.mkdir(parents=True, exist_ok=True)
    sql_path = args.simulation_dir / "eplusout.sql"

    kpis = {}

    if args.custom_kpi_extractor:
        if not args.custom_kpi_extractor.exists():
            log.error(f"Custom KPI extractor {args.custom_kpi_extractor} does not exist.")
            return 1
        spec = importlib.util.spec_from_file_location("custom_extractor", args.custom_kpi_extractor)
        if spec is None or spec.loader is None:
            log.error(f"Failed to load custom extractor from {args.custom_kpi_extractor}.")
            return 1

        custom_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(custom_mod)

        if not hasattr(custom_mod, "extract_kpis"):
            log.error(f"Custom extractor {args.custom_kpi_extractor} must define `extract_kpis(ctx)`.")
            return 1

        ctx = {
            "simulation_dir": args.simulation_dir,
            "sample_id": args.sample_id,
        }

        try:
            custom_kpis = custom_mod.extract_kpis(ctx)
            if isinstance(custom_kpis, dict):
                kpis.update(custom_kpis)
            else:
                log.error(f"Custom extractor returned {type(custom_kpis)}, expected dict.")
        except Exception as e:
            log.error(f"Custom extractor failed: {e}", exc_info=True)
            return 1
    else:
        eui = get_eui_kwh_m2_yr(sql_path)
        if eui is not None:
            kpis["eui_kwh_m2_yr"] = eui

    args.out.write_text(
        json.dumps(
            {
                "sample_id": args.sample_id,
                "openstudio_version": None,  # Not readily available without os bindings
                "kpis": kpis
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
