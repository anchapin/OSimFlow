#!/usr/bin/env python3
"""generate_plots.py — Render 1–3 static summary plots from aggregated results.

See docs/OSimFlow.md §4.2 (PROCESS_GENERATE_BASIC_PLOTS) for the contract.

Also generates interactive HTML reports using Plotly (issue #388) for
stakeholder exploration of results.
"""

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

from osimflow.algorithms.doe_analysis import DOEAnalysis

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("generate_plots")

# Plotly is optional — graceful degradation if not installed (issue #388).
try:
    import plotly.express as px
    import plotly.graph_objects as go

    PLOTLY_AVAILABLE = True
except ImportError:
    px = None  # type: ignore[assignment, misc]
    go = None  # type: ignore[assignment, misc]
    PLOTLY_AVAILABLE = False
    log.warning("plotly not installed; interactive HTML reports will not be generated")


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

    # 5. DOE analysis and visualization (issue #405)
    _generate_doe_plots(args.results_csv, args.outdir)

    # 6. GAP-012: Radar/spider plot, EuiDistribution (histogram+CDF), density heatmap
    _generate_eui_distribution_plot(results, args.outdir, baseline_eui)
    _generate_radar_plot(results, args.outdir)
    _generate_density_heatmap(results, args.outdir)

    # 7. Interactive HTML report (issue #388)
    _generate_interactive_report(args.outdir, results, failed, baseline_eui, pareto_dir)

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


# ---------------------------------------------------------------------------
# DOE Analysis Plots (issue #405)
# ---------------------------------------------------------------------------


def _generate_doe_plots(results_csv: Path, outdir: Path) -> list[Path]:
    """Generate DOE analysis plots: main effects, interaction matrix, sensitivity.

    Returns list of generated plot file paths.
    """
    plots: list[Path] = []
    if not results_csv.exists():
        log.warning("Results CSV not found for DOE plots: %s", results_csv)
        return plots

    try:
        analyzer = DOEAnalysis(results_csv)
        analyzer.compute_main_effects()
        analyzer.compute_interaction_effects()
        analyzer.compute_factor_sensitivity()
        analyzer.write_json(outdir)

        main_effects = analyzer._main_effects
        interaction_effects = analyzer._interaction_effects
        factor_sensitivity = analyzer._factor_sensitivity

        if main_effects:
            main_path = _plot_main_effects(main_effects, outdir)
            if main_path:
                plots.append(main_path)

        if len(interaction_effects) > 0:
            interaction_path = _plot_interaction_matrix(interaction_effects, outdir)
            if interaction_path:
                plots.append(interaction_path)

        if factor_sensitivity:
            sensitivity_path = _plot_factor_sensitivity(factor_sensitivity, outdir)
            if sensitivity_path:
                plots.append(sensitivity_path)

    except Exception as exc:
        log.warning("DOE analysis failed: %s", exc)

    return plots


def _plot_main_effects(
    main_effects: list,
    outdir: Path,
) -> Path | None:
    """Generate main effects plot (factor levels vs. response mean).

    One subplot per factor showing the response mean at each level.
    """
    if not main_effects:
        return None

    n_factors = len(main_effects)
    n_cols = min(3, n_factors)
    n_rows = (n_factors + n_cols - 1) // n_cols

    fig, axes = plt.subplots(n_rows, n_cols, figsize=(6 * n_cols, 4 * n_rows))
    if n_factors == 1:
        axes = np.array([axes])
    axes = np.asarray(axes).flatten()

    for idx, me in enumerate(main_effects):
        ax = axes[idx]
        levels = me.levels
        means = me.means
        std_devs = me.std_devs

        ax.errorbar(
            range(len(levels)),
            means,
            yerr=std_devs,
            fmt="o-",
            capsize=4,
            color="steelblue",
            markersize=6,
        )
        ax.set_xticks(range(len(levels)))
        ax.set_xticklabels([f"{level:.2g}" for level in levels], rotation=45, ha="right")
        ax.set_xlabel(me.factor)
        ax.set_ylabel("Response Mean")
        sig_marker = "**" if me.p_value < 0.01 else ("*" if me.p_value < 0.05 else "")
        ax.set_title(f"{me.factor} {sig_marker} (p={me.p_value:.3f})")
        ax.grid(True, alpha=0.3)

    for idx in range(n_factors, len(axes)):
        axes[idx].set_visible(False)

    plt.suptitle("DOE Main Effects", fontsize=14, fontweight="bold")
    plt.tight_layout()
    path = outdir / "doe_main_effects.png"
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    log.info("Generated DOE main effects plot: %s", path)
    return path


def _plot_interaction_matrix(
    interaction_effects: list,
    outdir: Path,
) -> Path | None:
    """Generate 2-way interaction effects matrix heatmap.

    Shows F-statistic or p-value for each factor pair interaction.
    """
    if not interaction_effects:
        return None

    factors: set[str] = set()
    for ie in interaction_effects:
        factors.add(ie.factor_a)
        factors.add(ie.factor_b)

    factor_list = sorted(factors)
    n = len(factor_list)
    if n < 2:
        return None

    f_matrix = np.full((n, n), np.nan)
    p_matrix = np.full((n, n), np.nan)

    for ie in interaction_effects:
        i = factor_list.index(ie.factor_a)
        j = factor_list.index(ie.factor_b)
        f_matrix[i, j] = ie.f_statistic
        f_matrix[j, i] = ie.f_statistic
        p_matrix[i, j] = ie.p_value
        p_matrix[j, i] = ie.p_value

    fig, ax = plt.subplots(figsize=(max(8, n), max(6, n * 0.8)))
    mask = np.isnan(f_matrix)
    f_display = np.ma.array(f_matrix, mask=mask)

    im = ax.imshow(f_display, cmap="YlOrRd", aspect="auto")
    cbar = plt.colorbar(im, ax=ax, label="F-statistic")
    cbar.ax.tick_params(labelsize=8)

    ax.set_xticks(range(n))
    ax.set_yticks(range(n))
    ax.set_xticklabels(factor_list, rotation=45, ha="right", fontsize=8)
    ax.set_yticklabels(factor_list, fontsize=8)

    for i in range(n):
        for j in range(n):
            if not mask[i, j]:
                p_val = p_matrix[i, j]
                text = f"{f_matrix[i, j]:.1f}\n(p={p_val:.2f})"
                color = "white" if f_matrix[i, j] > f_matrix.max() * 0.6 else "black"
                ax.text(j, i, text, ha="center", va="center", fontsize=7, color=color)

    ax.set_title("DOE 2-Way Interaction Effects\n(F-statistic, p-value)")
    plt.tight_layout()
    path = outdir / "doe_interaction_matrix.png"
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    log.info("Generated DOE interaction matrix: %s", path)
    return path


def _plot_factor_sensitivity(
    factor_sensitivity: list,
    outdir: Path,
) -> Path | None:
    """Generate factor sensitivity Pareto chart (bar chart of percent contribution)."""
    if not factor_sensitivity:
        return None

    factors = [fs.factor for fs in factor_sensitivity]
    contributions = [fs.percent_contribution for fs in factor_sensitivity]

    fig, ax = plt.subplots(figsize=(max(8, len(factors) * 0.6), 5))
    colors = plt.cm.RdYlGn_r(np.linspace(0.2, 0.8, len(factors)))
    bars = ax.barh(range(len(factors)), contributions, color=colors)

    ax.set_yticks(range(len(factors)))
    ax.set_yticklabels(factors, fontsize=9)
    ax.set_xlabel("Percent Contribution to Variance (%)")
    ax.set_title("DOE Factor Sensitivity (Pareto Chart)")
    ax.invert_yaxis()

    for bar, contrib in zip(bars, contributions, strict=True):
        ax.text(
            bar.get_width() + 0.5,
            bar.get_y() + bar.get_height() / 2,
            f"{contrib:.1f}%",
            va="center",
            fontsize=8,
        )

    ax.set_xlim(0, max(contributions) * 1.15)
    ax.grid(True, axis="x", alpha=0.3)
    plt.tight_layout()
    path = outdir / "doe_factor_sensitivity.png"
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    log.info("Generated DOE factor sensitivity chart: %s", path)
    return path


def _generate_interactive_report(
    outdir: Path,
    results: pd.DataFrame,
    failed: pd.DataFrame,
    baseline_eui: float | None,
    pareto_dir: Path,
) -> list[Path]:
    """Generate interactive HTML report using Plotly (issue #388).

    Creates a standalone interactive HTML report that stakeholders can
    open in any browser for exploration of campaign results.

    Returns list of generated HTML file paths.
    """
    plots: list[Path] = []
    if not PLOTLY_AVAILABLE:
        return plots

    # Build a multi-section HTML report with interactive Plotly charts.
    sections: list[str] = []

    # ── Campaign Overview ────────────────────────────────────────────────
    if not results.empty:
        n_samples = len(results)
        eui_col = _find_eui_column(results)
        mean_eui = results[eui_col].mean() if eui_col else None
        std_eui = results[eui_col].std() if eui_col else None

        overview_html = f"""
        <div class="section">
        <h2>Campaign Overview</h2>
        <table>
            <tr><th>Total Samples</th><td>{n_samples}</td></tr>
            <tr><th>Mean EUI</th><td>{mean_eui:.2f} kWh/m²/yr</td></tr>
            <tr><th>Std Dev EUI</th><td>{std_eui:.2f} kWh/m²/yr</td></tr>
        </table>
        </div>
        """
        sections.append(overview_html)

    # ── EUI Distribution ────────────────────────────────────────────────
    if not results.empty and "eui_kwh_m2_yr" in results.columns:
        eui_col = "eui_kwh_m2_yr"
        fig_eui = px.histogram(
            results,
            x=eui_col,
            nbins=30,
            title="EUI Distribution (kWh/m²/yr)",
            labels={eui_col: "EUI (kWh/m²/yr)", "count": "Count"},
        )
        if baseline_eui is not None:
            fig_eui.add_vline(
                x=baseline_eui,
                line_color="red",
                line_dash="dash",
                annotation_text=f"Baseline: {baseline_eui:.1f}",
            )
        fig_eui.update_layout(template="plotly_white")
        sections.append(
            f'<div class="section"><h2>EUI Distribution</h2>{fig_eui.to_html(full_html=False, include_plotlyjs="cdn")}</div>'
        )

    # ── Parameter vs EUI Scatter ─────────────────────────────────────
    if not results.empty:
        numeric_cols = results.select_dtypes(include="number").columns
        design_vars = [c for c in numeric_cols if c not in ("sample_id", "eui_kwh_m2_yr")]
        if design_vars:
            variances = results[design_vars].var()
            if not variances.isna().all() and variances.sum() > 0:
                top_var = variances.idxmax()
                fig_scatter = px.scatter(
                    results,
                    x=top_var,
                    y="eui_kwh_m2_yr",
                    title=f"EUI vs {top_var}",
                    labels={top_var: top_var, "eui_kwh_m2_yr": "EUI (kWh/m²/yr)"},
                    hover_data=results.columns.tolist(),
                )
                fig_scatter.update_layout(template="plotly_white")
                sections.append(
                    f'<div class="section"><h2>Parameter vs EUI</h2>{fig_scatter.to_html(full_html=False, include_plotlyjs="cdn")}</div>'
                )

    # ── Parallel Coordinates (all numeric vars) ─────────────────────
    if not results.empty:
        numeric_df = results.select_dtypes(include="number")
        if len(numeric_df.columns) > 1:
            cols_for_parallel = [c for c in numeric_df.columns if c != "sample_id"]
            if len(cols_for_parallel) > 1:
                fig_parallel = px.parallel_coordinates(
                    results[[c for c in cols_for_parallel if c in results.columns]],
                    color="eui_kwh_m2_yr" if "eui_kwh_m2_yr" in results.columns else None,
                    title="Parallel Coordinates (all numeric parameters)",
                )
                fig_parallel.update_layout(template="plotly_white")
                sections.append(
                    f'<div class="section"><h2>Parallel Coordinates</h2>{fig_parallel.to_html(full_html=False, include_plotlyjs="cdn")}</div>'
                )

    # ── Failure Summary ───────────────────────────────────────────────
    if not failed.empty and "error_summary" in failed.columns:
        counts = failed["error_summary"].value_counts()
        if not counts.empty:
            fig_fail = px.bar(
                x=counts.values,
                y=counts.index,
                orientation="h",
                title="Failure Reasons",
                labels={"x": "Count", "y": "Error Summary"},
            )
            fig_fail.update_layout(template="plotly_white")
            sections.append(
                f'<div class="section"><h2>Failure Summary</h2>{fig_fail.to_html(full_html=False, include_plotlyjs="cdn")}</div>'
            )

    # ── Pareto Front (if available) ─────────────────────────────────
    if pareto_dir.is_dir():
        pareto_html = _generate_interactive_pareto_report(pareto_dir)
        if pareto_html:
            sections.append(pareto_html)

    # Assemble the full HTML report.
    if sections:
        html_report = _assemble_html_report(sections)
        report_path = outdir / "interactive_report.html"
        report_path.write_text(html_report)
        plots.append(report_path)
        log.info("Generated interactive HTML report: %s", report_path)

    return plots


def _find_eui_column(df: pd.DataFrame) -> str | None:
    """Find the EUI column name in the DataFrame."""
    for candidate in ("eui_kwh_m2_yr", "eui", "EUI", "eui_kbtu_ft2_yr"):
        if candidate in df.columns:
            return candidate
    for col in df.columns:
        if "eui" in col.lower():
            return str(col)
    return None


def _generate_interactive_pareto_report(pareto_dir: Path) -> str | None:
    """Generate interactive Pareto front HTML section.

    Returns HTML string for the Pareto section, or None if no data.
    """
    if not PLOTLY_AVAILABLE:
        return None

    gen_files = sorted(pareto_dir.glob("gen_*.json"))
    if not gen_files:
        return None

    all_gen_data: list[tuple[int, list[dict[str, Any]]]] = []
    for gf in gen_files:
        try:
            data = json.loads(gf.read_text())
            gen_num = data.get("generation", 0)
            solutions = data.get("solutions", [])
            if solutions:
                all_gen_data.append((gen_num, solutions))
        except (json.JSONDecodeError, OSError):
            pass

    if not all_gen_data:
        return None

    first_objs = all_gen_data[0][1][0].get("objectives", {})
    obj_names = list(first_objs.keys())
    if len(obj_names) < 2:
        return None

    # Pareto front scatter colored by generation.
    all_x: list[float] = []
    all_y: list[float] = []
    all_gen: list[int] = []
    for gen_num, solutions in all_gen_data:
        for s in solutions:
            all_x.append(s.get("objectives", {}).get(obj_names[0], 0))
            all_y.append(s.get("objectives", {}).get(obj_names[1], 0))
            all_gen.append(gen_num)

    df_pareto = pd.DataFrame({"generation": all_gen, obj_names[0]: all_x, obj_names[1]: all_y})

    fig_pareto = px.scatter(
        df_pareto,
        x=obj_names[0],
        y=obj_names[1],
        color="generation",
        title="Pareto Front (colored by generation)",
        labels={obj_names[0]: obj_names[0], obj_names[1]: obj_names[1]},
        hover_data=[obj_names[0], obj_names[1], "generation"],
    )
    fig_pareto.update_layout(template="plotly_white")
    return f'<div class="section"><h2>Pareto Front</h2>{fig_pareto.to_html(full_html=False, include_plotlyjs="cdn")}</div>'


def _assemble_html_report(sections: list[str]) -> str:
    """Assemble a complete standalone HTML report from section HTML strings."""
    sections_html = "\n".join(sections)
    return f"""\
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>OSimFlow Interactive Report</title>
    <script src="https://cdn.plot.ly/plotly-2.27.0.min.js"></script>
    <style>
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            margin: 0;
            padding: 20px;
            background: #f5f5f5;
        }}
        .section {{
            background: white;
            border-radius: 8px;
            padding: 20px;
            margin-bottom: 20px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }}
        h1 {{
            color: #1a1a2e;
            border-bottom: 2px solid #0066cc;
            padding-bottom: 10px;
        }}
        h2 {{
            color: #2d2d44;
            margin-top: 0;
        }}
        table {{
            border-collapse: collapse;
            width: 100%;
            max-width: 400px;
        }}
        th, td {{
            text-align: left;
            padding: 8px 12px;
            border-bottom: 1px solid #eee;
        }}
        th {{
            color: #666;
            font-weight: normal;
        }}
    </style>
</head>
<body>
    <h1>OSimFlow Interactive Report</h1>
    {sections_html}
</body>
</html>
"""


# ---------------------------------------------------------------------------
# GAP-012: Radar / EuiDistribution / Density Heatmap
# ---------------------------------------------------------------------------


def _generate_eui_distribution_plot(
    results: pd.DataFrame,
    outdir: Path,
    baseline_eui: float | None,
) -> Path | None:
    """Generate EUI histogram with overlaid empirical CDF (issue #554).

    Shows the full distribution shape — histogram for frequency and CDF for
    cumulative probability — on a single axes with a dual y-axis.
    A baseline reference line is drawn when available.
    """
    eui_col = _find_eui_column(results)
    if eui_col is None or results.empty or results[eui_col].isna().all():
        return None

    eui_values = results[eui_col].dropna()
    if len(eui_values) < 3:
        return None

    fig, ax1 = plt.subplots(figsize=(9, 6))

    # Histogram on primary axis
    color_hist = "steelblue"
    counts, bin_edges, patches = ax1.hist(
        eui_values,
        bins=30,
        color=color_hist,
        alpha=0.6,
        edgecolor="white",
        label="Frequency",
    )
    ax1.set_xlabel("EUI (kWh/m²/yr)", fontsize=11)
    ax1.set_ylabel("Count", fontsize=11, color=color_hist)
    ax1.tick_params(axis="y", labelcolor=color_hist)

    # Empirical CDF on secondary axis
    ax2 = ax1.twinx()
    eui_sorted = np.sort(eui_values)
    cdf_y = np.arange(1, len(eui_sorted) + 1) / len(eui_sorted)
    color_cdf = "tomato"
    ax2.plot(eui_sorted, cdf_y, color=color_cdf, linewidth=2.2, label="CDF")
    ax2.set_ylabel("Cumulative Probability", fontsize=11, color=color_cdf)
    ax2.tick_params(axis="y", labelcolor=color_cdf)
    ax2.set_ylim(0, 1.05)

    # Baseline reference line
    if baseline_eui is not None and pd.notna(baseline_eui):
        ax1.axvline(
            baseline_eui,
            color="red",
            linestyle="--",
            linewidth=1.5,
            label=f"Baseline ({baseline_eui:.1f})",
        )

    # Merge legends
    lines1, labels1 = ax1.get_legend_handles_labels()
    lines2, labels2 = ax2.get_legend_handles_labels()
    ax1.legend(lines1 + lines2, labels1 + labels2, loc="upper right", fontsize=9)

    ax1.set_title("EUI Distribution (Histogram + Empirical CDF)", fontsize=13, fontweight="bold")
    plt.tight_layout()
    path = outdir / "eui_distribution.png"
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    log.info("Generated EUI distribution plot: %s", path)
    return path


def _generate_radar_plot(results: pd.DataFrame, outdir: Path) -> Path | None:
    """Generate radar/spider plot for multi-objective comparison (issue #554).

    Normalises all numeric columns to [0,1] and renders them as axes on a
    spider/radar chart — one "petal" per objective/KPI.  If there are too few
    numeric columns (< 3) the plot is skipped.
    """
    if results.empty:
        return None

    numeric_cols = results.select_dtypes(include="number").columns.tolist()
    # Drop sample_id and any single-valued columns
    kpi_cols = [
        c
        for c in numeric_cols
        if c not in ("sample_id",) and results[c].std() > 1e-9
    ]
    if len(kpi_cols) < 3:
        log.info("Fewer than 3 varying KPI columns; skipping radar plot.")
        return None

    # Normalise each column to [0, 1]
    df_norm = results[kpi_cols].copy()
    for col in kpi_cols:
        col_min = df_norm[col].min()
        col_max = df_norm[col].max()
        if col_max - col_min > 1e-9:
            df_norm[col] = (df_norm[col] - col_min) / (col_max - col_min)
        else:
            df_norm[col] = 0.0

    n = len(kpi_cols)
    angles = np.linspace(0, 2 * np.pi, n, endpoint=False).tolist()
    angles += angles[:1]  # close the loop

    fig, ax = plt.subplots(figsize=(9, 9), subplot_kw=dict(polar=True))
    cmap = plt.get_cmap("tab20", len(results))

    for idx, (_, row) in enumerate(df_norm.iterrows()):
        values = row.tolist()
        values += values[:1]  # close the loop
        ax.plot(
            angles,
            values,
            color=cmap(idx % cmap.N),
            alpha=0.25,
            linewidth=0.8,
        )
        ax.fill(angles, values, color=cmap(idx % cmap.N), alpha=0.06)

    # Compute and plot the mean normalised profile
    mean_values = df_norm.mean().tolist()
    mean_values += mean_values[:1]
    ax.plot(angles, mean_values, color="tomato", linewidth=2.5, label="Mean profile")
    ax.fill(angles, mean_values, color="tomato", alpha=0.12)

    ax.set_xticks(angles[:-1])
    ax.set_xticklabels(kpi_cols, fontsize=9)
    ax.set_ylim(0, 1)
    ax.set_title("Radar / Spider Plot\n(Normalised KPIs)", fontsize=13, fontweight="bold", pad=20)
    ax.legend(loc="upper right", bbox_to_anchor=(1.3, 1.1), fontsize=8)

    plt.tight_layout()
    path = outdir / "radar_plot.png"
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    log.info("Generated radar plot: %s", path)
    return path


def _generate_density_heatmap(results: pd.DataFrame, outdir: Path) -> Path | None:
    """Generate a 2-D density heatmap for the two most variable parameters (issue #554).

    Uses kernel density estimation (scipy.stats.gaussian_kde) to render a
    colour-coded density surface with overlaid scatter points.
    Skipped when there are fewer than 2 varying numeric columns.
    """
    if results.empty:
        return None

    numeric_cols = results.select_dtypes(include="number").columns.tolist()
    design_vars = [c for c in numeric_cols if c not in ("sample_id", "eui_kwh_m2_yr")]
    design_vars = [c for c in design_vars if results[c].std() > 1e-9]

    if len(design_vars) < 2:
        log.info("Fewer than 2 varying design variables; skipping density heatmap.")
        return None

    # Pick the two most variable columns
    variances = results[design_vars].var()
    top2 = variances.nlargest(2).index.tolist()
    x_col, y_col = top2[0], top2[1]

    x = results[x_col].dropna().values
    y = results[y_col].dropna().values

    if len(x) < 10:
        log.info("Not enough non-NA samples for density heatmap.")
        return None

    # Compute KDE on a grid
    try:
        from scipy import stats

        xy = np.vstack([x, y])
        kernel = stats.gaussian_kde(xy)

        xmin, xmax = x.min() - 0.1 * (x.max() - x.min() or 1), x.max() + 0.1 * (x.max() - x.min() or 1)
        ymin, ymax = y.min() - 0.1 * (y.max() - y.min() or 1), y.max() + 0.1 * (y.max() - y.min() or 1)
        xx, yy = np.meshgrid(
            np.linspace(xmin, xmax, 200),
            np.linspace(ymin, ymax, 200),
        )
        z = kernel(np.vstack([xx.ravel(), yy.ravel()])).reshape(xx.shape)
    except Exception as exc:
        log.warning("KDE computation failed for density heatmap: %s", exc)
        return None

    fig, ax = plt.subplots(figsize=(9, 7))
    im = ax.imshow(
        z,
        origin="lower",
        extent=[xmin, xmax, ymin, ymax],
        cmap="YlOrRd",
        aspect="auto",
        interpolation="bilinear",
    )
    cbar = plt.colorbar(im, ax=ax, label="Density")
    cbar.ax.tick_params(labelsize=9)

    # Overlay scatter points (downsampled if > 500)
    if len(x) > 500:
        idx_sample = np.random.choice(len(x), 500, replace=False)
        x_sample = x[idx_sample]
        y_sample = y[idx_sample]
    else:
        x_sample = x
        y_sample = y

    ax.scatter(x_sample, y_sample, c="white", s=6, alpha=0.4, edgecolors="none")

    ax.set_xlabel(x_col, fontsize=11)
    ax.set_ylabel(y_col, fontsize=11)
    ax.set_title(f"Density Heatmap\n({x_col} vs {y_col})", fontsize=13, fontweight="bold")

    plt.tight_layout()
    path = outdir / "density_heatmap.png"
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    log.info("Generated density heatmap: %s", path)
    return path


if __name__ == "__main__":
    sys.exit(main())
