#!/usr/bin/env python3
"""generate_plots.py — Render 1–3 static summary plots from aggregated results.

See docs/OSimFlow.md §4.2 (PROCESS_GENERATE_BASIC_PLOTS) for the contract.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
from pathlib import Path
from typing import Any

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import seaborn as sns

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("generate_plots")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results_csv", required=True, type=Path)
    parser.add_argument("--failed_csv", required=True, type=Path)
    parser.add_argument("--outdir", required=True, type=Path)
    parser.add_argument(
        "--baseline_sample_id",
        default=None,
        help="Sample ID of the baseline (for reference line on EUI histogram).",
    )
    parser.add_argument(
        "--pareto_dir",
        default=None,
        type=Path,
        help="Directory containing per-generation Pareto JSON files (gen_N.json).",
    )
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

    # Resolve baseline EUI for the reference line (issue #64).
    baseline_eui: float | None = None
    if (
        args.baseline_sample_id
        and not results.empty
        and "sample_id" in results.columns
        and "eui_kwh_m2_yr" in results.columns
    ):
        baseline_rows = results[results["sample_id"] == args.baseline_sample_id]
        if not baseline_rows.empty:
            baseline_eui = float(baseline_rows.iloc[0]["eui_kwh_m2_yr"])

    # 1. EUI Histogram
    if "eui_kwh_m2_yr" in results.columns and not results["eui_kwh_m2_yr"].isna().all():
        plt.figure(figsize=(8, 5))
        sns.histplot(results["eui_kwh_m2_yr"].dropna(), kde=True)
        # Add baseline reference line when available (issue #64).
        if baseline_eui is not None and pd.notna(baseline_eui):
            plt.axvline(
                baseline_eui,
                color="red",
                linestyle="--",
                linewidth=1.5,
                label=f"Baseline EUI ({baseline_eui:.1f})",
            )
            plt.legend()
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

    # 4. Pareto front plots (issue #124)
    pareto_dir = args.pareto_dir or (args.outdir / "pareto")
    _generate_pareto_plots(pareto_dir, args.outdir)

    return 0


def _generate_pareto_plots(pareto_dir: Path, outdir: Path) -> list[Path]:
    """Generate Pareto front scatter and hypervolume convergence plots.

    Returns list of generated plot file paths.
    """
    plots: list[Path] = []
    if not pareto_dir.is_dir():
        return plots

    gen_files = sorted(pareto_dir.glob("gen_*.json"))
    if not gen_files:
        return plots

    # Load all generation data
    all_gen_data: list[tuple[int, list[dict[str, Any]]]] = []
    for gf in gen_files:
        try:
            data = json.loads(gf.read_text())
            gen_num = data.get("generation", 0)
            solutions = data.get("solutions", [])
            if solutions:
                all_gen_data.append((gen_num, solutions))
        except (json.JSONDecodeError, OSError) as e:
            log.warning(f"Failed to read Pareto gen file {gf}: {e}")

    if not all_gen_data:
        return plots

    # Determine objective names from first solution
    first_objs = all_gen_data[0][1][0].get("objectives", {})
    obj_names = list(first_objs.keys())
    if len(obj_names) < 2:
        log.info("Pareto data has <2 objectives; skipping Pareto plots.")
        return plots

    # 4a. Pareto front scatter plot (colored by generation)
    fig, ax = plt.subplots(figsize=(10, 8))
    cmap = plt.get_cmap("viridis")
    n_gens = len(all_gen_data)
    for idx, (gen_num, solutions) in enumerate(all_gen_data):
        x = [s.get("objectives", {}).get(obj_names[0], 0) for s in solutions]
        y = [s.get("objectives", {}).get(obj_names[1], 0) for s in solutions]
        color = cmap(idx / max(n_gens - 1, 1))
        ax.scatter(x, y, label=f"Gen {gen_num}", alpha=0.7, color=color)

    ax.set_xlabel(obj_names[0])
    ax.set_ylabel(obj_names[1])
    ax.set_title("Pareto Front")
    ax.legend()
    path = outdir / "pareto_front.png"
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    plots.append(path)
    log.info(f"Generated Pareto front scatter: {path}")

    # 4b. Convergence plot (hypervolume per generation) + CSV output
    if n_gens >= 1:
        hv_values: list[float] = []
        gen_numbers: list[int] = []
        # Simple hypervolume: product of (reference_point - objective_value)
        # per non-dominated solution, summed.
        ref_x = (
            max(
                s.get("objectives", {}).get(obj_names[0], 0)
                for _, sols in all_gen_data
                for s in sols
            )
            * 1.1
        )
        ref_y = (
            max(
                s.get("objectives", {}).get(obj_names[1], 0)
                for _, sols in all_gen_data
                for s in sols
            )
            * 1.1
        )
        ref_point = [ref_x, ref_y]

        for gen_num, solutions in all_gen_data:
            if len(obj_names) == 2:
                pts = np.array(
                    [
                        [
                            s.get("objectives", {}).get(obj_names[0], ref_x),
                            s.get("objectives", {}).get(obj_names[1], ref_y),
                        ]
                        for s in solutions
                    ]
                )
                hv = _hypervolume_2d_simple(pts, np.array(ref_point))
            else:
                hv = 0.0
                for s in solutions:
                    vol = 1.0
                    for k, name in enumerate(obj_names):
                        ref_val = ref_point[k] if k < len(ref_point) else 0.0
                        delta = ref_val - s.get("objectives", {}).get(name, ref_val)
                        vol *= max(0.0, delta)
                    hv += vol
            hv_values.append(hv)
            gen_numbers.append(gen_num)

        # Write hypervolume_per_gen.csv (issue #106).
        pareto_out = outdir / "pareto"
        pareto_out.mkdir(parents=True, exist_ok=True)
        hv_csv = pareto_out / "hypervolume_per_gen.csv"
        with open(hv_csv, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["generation", "hypervolume"])
            for g, h in zip(gen_numbers, hv_values, strict=True):
                writer.writerow([g, h])
        log.info(f"Wrote hypervolume CSV: {hv_csv}")

        # Plot convergence when multiple generations.
        if n_gens > 1:
            fig2, ax2 = plt.subplots(figsize=(8, 5))
            ax2.plot(gen_numbers, hv_values, "o-", color="steelblue")
            ax2.set_xlabel("Generation")
            ax2.set_ylabel("Hypervolume Indicator")
            ax2.set_title("Pareto Front Convergence")
            path2 = outdir / "pareto_convergence.png"
            fig2.savefig(path2, dpi=150, bbox_inches="tight")
            plt.close(fig2)
            plots.append(path2)
            log.info(f"Generated Pareto convergence plot: {path2}")

    return plots


def _hypervolume_2d_simple(points: np.ndarray, ref_point: np.ndarray) -> float:
    """Simple 2D hypervolume (area) for minimisation objectives.

    Sweeps points sorted by first objective, accumulating dominated area.
    """
    if len(points) == 0:
        return 0.0
    pts = points.copy().astype(float)
    ref = ref_point.copy().astype(float)
    mask = np.all(pts <= ref, axis=1)
    if not mask.any():
        return 0.0
    pts = pts[mask]
    order = np.argsort(pts[:, 0])
    pts = pts[order]
    hv = 0.0
    for i in range(len(pts)):
        dx = (pts[i + 1, 0] if i + 1 < len(pts) else ref[0]) - pts[i, 0]
        dy = ref[1] - pts[i, 1]
        hv += max(0.0, dx) * max(0.0, dy)
    return hv


if __name__ == "__main__":
    sys.exit(main())
