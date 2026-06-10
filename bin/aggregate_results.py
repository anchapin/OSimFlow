#!/usr/bin/env python3
"""aggregate_results.py — Collect per-sample KPIs and identify failures.

See docs/OSimFlow.md §4.2 (PROCESS_AGGREGATE_RESULTS) for the contract.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

import pandas as pd

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("aggregate_results")


def parse_kpi_json(kpi_path: Path) -> dict:
    try:
        data = json.loads(kpi_path.read_text())
        res = {"sample_id": data.get("sample_id", kpi_path.stem.replace("kpi_", ""))}
        kpis = data.get("kpis", {})
        res.update(kpis)
        return res
    except Exception as e:
        log.warning(f"Failed to parse KPI JSON {kpi_path}: {e}")
        return {"sample_id": kpi_path.stem.replace("kpi_", "")}


def extract_failure(sim_dir: Path) -> dict | None:
    # First look at eplusout.err
    err_path = sim_dir / "eplusout.err"
    err_summary = None
    if err_path.exists() and err_path.stat().st_size > 0:
        with err_path.open() as f:
            for line in f:
                if "  * Severe" in line or "** Severe" in line:
                    err_summary = line.strip()
                    break

    # We cannot reliably get exit_code from inside this script because
    # it's the executor/work.py that ran the openstudio.cli
    # But if there's an error summary or no sql file, we consider it failed
    sql_path = sim_dir / "eplusout.sql"
    if err_summary or not sql_path.exists():
        return {
            "sample_id": sim_dir.name,
            "error_summary": err_summary or "eplusout.sql missing",
            "exit_code": 1 if err_summary or not sql_path.exists() else 0,
            "log_path": str(err_path) if err_path.exists() else "",
        }
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kpis", required=True, nargs="+", type=Path)
    parser.add_argument("--simulation_dirs", required=True, nargs="+", type=Path)
    parser.add_argument("--out_csv", required=True, type=Path)
    parser.add_argument("--out_parquet", type=Path, default=None)
    parser.add_argument("--out_failed", required=True, type=Path)
    args = parser.parse_args()

    args.out_csv.parent.mkdir(parents=True, exist_ok=True)
    args.out_failed.parent.mkdir(parents=True, exist_ok=True)

    # 1. Read KPIs into wide-form DataFrame
    all_kpis = []
    for kpi_path in args.kpis:
        if kpi_path.exists():
            all_kpis.append(parse_kpi_json(kpi_path))

    if all_kpis:
        df = pd.DataFrame(all_kpis)
        df.to_csv(args.out_csv, index=False)
        if args.out_parquet:
            df.to_parquet(args.out_parquet, index=False)
    else:
        # Empty DataFrame fallback
        df = pd.DataFrame(columns=["sample_id"])
        df.to_csv(args.out_csv, index=False)
        if args.out_parquet:
            df.to_parquet(args.out_parquet, index=False)

    # 2. Extract failures
    failures = []
    for sim_dir in args.simulation_dirs:
        if sim_dir.exists():
            f = extract_failure(sim_dir)
            if f:
                failures.append(f)

    if failures:
        fail_df = pd.DataFrame(failures)
        # Ensure column order
        cols = ["sample_id", "error_summary", "exit_code", "log_path"]
        for c in cols:
            if c not in fail_df.columns:
                fail_df[c] = None
        fail_df = fail_df[cols]
        fail_df.to_csv(args.out_failed, index=False)
    else:
        args.out_failed.write_text("sample_id,error_summary,exit_code,log_path\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
