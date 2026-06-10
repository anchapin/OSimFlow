#!/usr/bin/env python3
"""generate_plots.py — Render 1–3 static summary plots from aggregated results.

See docs/OSimFlow.md §4.2 (PROCESS_GENERATE_BASIC_PLOTS) for the contract.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd
import seaborn as sns

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("generate_plots")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results_csv", required=True, type=Path)
    parser.add_argument("--failed_csv", required=True, type=Path)
    parser.add_argument("--outdir", required=True, type=Path)
    args = parser.parse_args()

    args.outdir.mkdir(parents=True, exist_ok=True)

    # Read data
    try:
        results = pd.read_csv(args.results_csv)
    except Exception as e:
        log.error(f"Failed to read results CSV {args.results_csv}: {e}")
        results = pd.DataFrame()

    try:
        failed = pd.read_csv(args.failed_csv)
    except Exception as e:
        log.error(f"Failed to read failed simulations CSV {args.failed_csv}: {e}")
        failed = pd.DataFrame()

    sns.set_theme(style="whitegrid", palette="colorblind")

    # 1. EUI Histogram
    if "eui_kwh_m2_yr" in results.columns and not results["eui_kwh_m2_yr"].isna().all():
        plt.figure(figsize=(8, 5))
        sns.histplot(results["eui_kwh_m2_yr"].dropna(), kde=True)
        plt.title("Distribution of EUI (kWh/m²/yr)")
        plt.xlabel("EUI (kWh/m²/yr)")
        plt.ylabel("Count")
        plt.tight_layout()
        plt.savefig(args.outdir / "eui_histogram.png", dpi=300)
        plt.savefig(args.outdir / "eui_histogram.pdf")
        plt.close()

    # 2. Scatter of top variable vs EUI
    # We find the column (excluding sample_id and eui) with highest variance
    if not results.empty and "eui_kwh_m2_yr" in results.columns:
        numeric_cols = results.select_dtypes(include="number").columns
        design_vars = [c for c in numeric_cols if c not in ("sample_id", "eui_kwh_m2_yr")]
        if design_vars:
            variances = results[design_vars].var()
            if not variances.isna().all():
                top_var = variances.idxmax()

                plt.figure(figsize=(8, 5))
                sns.scatterplot(data=results, x=top_var, y="eui_kwh_m2_yr", alpha=0.7)
                plt.title(f"EUI vs Most Variable Parameter ({top_var})")
                plt.xlabel(top_var)
                plt.ylabel("EUI (kWh/m²/yr)")
                plt.tight_layout()
                plt.savefig(args.outdir / "top_var_vs_eui.png", dpi=300)
                plt.savefig(args.outdir / "top_var_vs_eui.pdf")
                plt.close()

    # 3. Failure Summary
    if not failed.empty and "error_summary" in failed.columns:
        counts = failed["error_summary"].value_counts()
        if not counts.empty:
            plt.figure(figsize=(10, 6))
            counts.plot(kind="barh", color=sns.color_palette("colorblind")[0])
            plt.title("Failure Reasons")
            plt.xlabel("Count")
            plt.ylabel("Error Summary")
            plt.tight_layout()
            plt.savefig(args.outdir / "failure_summary.png", dpi=300)
            plt.savefig(args.outdir / "failure_summary.pdf")
            plt.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
