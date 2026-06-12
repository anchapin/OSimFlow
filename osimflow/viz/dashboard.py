"""Streamlit-based local dashboard for OSimFlow campaign results.

This module is self-contained: it reads ``${outdir}/aggregated_results.csv``
and ``${outdir}/run.json``, builds standard building-science visualisations,
and terminates when the user closes the browser tab or presses Ctrl-C.

It is **never** imported by the main campaign loop — the dashboard is a
separate process spawned by the ``osimflow dashboard`` CLI subcommand.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any

import pandas as pd

log = logging.getLogger("osimflow.viz.dashboard")


class DashboardData:
    """Load and hold campaign output data for the dashboard."""

    def __init__(self, outdir: Path) -> None:
        self.outdir = outdir.resolve()
        self.results_csv = self.outdir / "aggregated_results.csv"
        self.run_json_path = self.outdir / "run.json"
        self.failed_csv = self.outdir / "failed_simulations.csv"

        self.results_df: pd.DataFrame | None = None
        self.run_trace: dict[str, Any] | None = None
        self.failed_df: pd.DataFrame | None = None

        self._load()

    def _load(self) -> None:
        if self.results_csv.exists():
            self.results_df = pd.read_csv(self.results_csv)
            log.info("Loaded %d rows from %s", len(self.results_df), self.results_csv)
        else:
            log.warning("No aggregated_results.csv found at %s", self.results_csv)

        if self.run_json_path.exists():
            raw = json.loads(self.run_json_path.read_text(encoding="utf-8"))
            self.run_trace = raw
            log.info("Loaded run.json (schema_version=%s)", raw.get("schema_version"))
        else:
            log.warning("No run.json found at %s", self.run_json_path)

        if self.failed_csv.exists():
            self.failed_df = pd.read_csv(self.failed_csv)

    @property
    def has_results(self) -> bool:
        return self.results_df is not None and not self.results_df.empty

    @property
    def has_run_trace(self) -> bool:
        return self.run_trace is not None

    @property
    def sample_count(self) -> int:
        if self.has_run_trace and self.run_trace is not None:
            per_sample: list[dict[str, Any]] = self.run_trace.get("per_sample", [])
            return len(per_sample)
        if self.has_results and self.results_df is not None:
            return len(self.results_df)
        return 0

    @property
    def failure_count(self) -> int:
        if self.failed_df is not None:
            return len(self.failed_df)
        if self.has_run_trace and self.run_trace is not None:
            per_sample: list[dict[str, Any]] = self.run_trace.get("per_sample", [])
            return sum(1 for s in per_sample if s.get("status") == "failed")
        return 0

    @property
    def success_count(self) -> int:
        total = self.sample_count
        if total == 0:
            return 0
        return total - self.failure_count

    def eui_column(self) -> str | None:
        """Heuristic to find the EUI column in the results DataFrame."""
        if not self.has_results:
            return None
        df = self.results_df
        if df is None:
            return None
        for candidate in ("eui_kwh_m2_yr", "eui", "EUI", "eui_kbtu_ft2_yr"):
            if candidate in df.columns:
                return candidate
        # Fallback: any column containing 'eui'
        for col in df.columns:
            if "eui" in col.lower():
                return str(col)
        return None

    def numeric_lhs_columns(self) -> list[str]:
        """Return columns that look like LHS variable values (numeric, not KPI)."""
        if not self.has_results:
            return []
        df = self.results_df
        if df is None:
            return []
        kpi_like = {
            "eui",
            "eui_kwh_m2_yr",
            "eui_kbtu_ft2_yr",
            "cost_usd",
            "total_energy_kwh",
            "sample_id",
        }
        cols: list[str] = []
        for col in df.columns:
            if col.lower() in kpi_like:
                continue
            if df[col].dtype in ("float64", "int64", "float32", "int32"):
                cols.append(col)
        return cols


def create_dashboard_app(
    outdir: Path,
    port: int = 8501,
) -> None:
    """Launch the Streamlit dashboard for the given campaign output directory.

    This function **blocks** until the user terminates the Streamlit server
    (Ctrl-C or closing the browser).

    Parameters
    ----------
    outdir
        Path to the campaign output directory containing
        ``aggregated_results.csv`` and ``run.json``.
    port
        Port to serve the dashboard on (default 8501).
    """
    try:
        import streamlit.web.bootstrap as st_bootstrap  # noqa: PLC0415
        from streamlit.web.cli import main as st_main  # noqa: PLC0415, F401
    except ImportError as exc:
        print(
            "Error: osimflow[viz] extra required. Install with: pip install osimflow[viz]",
            file=sys.stderr,
        )
        raise SystemExit(1) from exc

    # Build a thin wrapper script that Streamlit will run.
    # We write it to a temp file so Streamlit's file-watcher doesn't
    # interfere with the main process.
    import tempfile  # noqa: PLC0415

    script_content = _dashboard_script(outdir.resolve())
    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".py",
        prefix="osimflow_dashboard_",
        delete=False,
    ) as tmp:
        tmp.write(script_content)
        tmp.flush()
        tmp_name = tmp.name

    try:
        st_bootstrap.run(tmp_name, False, [], flag_options={"server.port": port})
    finally:
        Path(tmp_name).unlink(missing_ok=True)


def _dashboard_script(outdir: Path) -> str:
    """Return a self-contained Streamlit script as a string.

    The script is written to a temporary file and executed by the Streamlit
    bootstrap runtime. It imports only from streamlit, pandas, json, and
    pathlib — no osimflow internals.
    """
    outdir_str = str(outdir)
    return f'''\
"""OSimFlow Campaign Dashboard — auto-generated ephemeral script."""

import json
from pathlib import Path

import pandas as pd
import streamlit as st

OUTDIR = Path("{outdir_str}")


# ── Data loading ────────────────────────────────────────────────────────

@st.cache_data
def load_data():
    results_csv = OUTDIR / "aggregated_results.csv"
    run_json_path = OUTDIR / "run.json"
    failed_csv = OUTDIR / "failed_simulations.csv"

    df = pd.read_csv(results_csv) if results_csv.exists() else None
    trace = json.loads(run_json_path.read_text()) if run_json_path.exists() else None
    failed = pd.read_csv(failed_csv) if failed_csv.exists() else None
    return df, trace, failed


results_df, run_trace, failed_df = load_data()

st.set_page_config(page_title="OSimFlow Dashboard", layout="wide")
st.title("OSimFlow Campaign Dashboard")
st.caption(f"Campaign directory: `{{OUTDIR}}`")


# ── Campaign overview ───────────────────────────────────────────────────

if run_trace is not None:
    st.header("Campaign Overview")
    col1, col2, col3, col4 = st.columns(4)

    per_sample = run_trace.get("per_sample", [])
    total = len(per_sample)
    ok = sum(1 for s in per_sample if s.get("status") == "ok")
    cached = sum(1 for s in per_sample if s.get("status") == "cached")
    failed_count = sum(1 for s in per_sample if s.get("status") == "failed")

    col1.metric("Total Samples", total)
    col2.metric("Succeeded", ok + cached)
    col3.metric("Failed", failed_count)
    col4.metric("Cached", cached)

    elapsed = run_trace.get("elapsed_s")
    if elapsed is not None:
        st.info(f"Total wall time: {{elapsed:.1f}} s")

    steps = run_trace.get("steps", [])
    if steps:
        st.subheader("Step Timings")
        step_df = pd.DataFrame(steps)
        st.dataframe(step_df[["step", "cache", "elapsed_s", "exit_code"]])


# ── EUI Distribution ───────────────────────────────────────────────────

if results_df is not None and not results_df.empty:
    st.header("Results")

    # Find EUI column
    eui_col = None
    for c in ("eui_kwh_m2_yr", "eui", "EUI", "eui_kbtu_ft2_yr"):
        if c in results_df.columns:
            eui_col = c
            break
    if eui_col is None:
        for c in results_df.columns:
            if "eui" in c.lower():
                eui_col = c
                break

    if eui_col is not None:
        st.subheader(f"EUI Distribution ({{eui_col}})")
        st.bar_chart(results_df, x=results_df.columns[0], y=eui_col)

        col_a, col_b = st.columns(2)
        col_a.metric("Mean EUI", f"{{results_df[eui_col].mean():.2f}}")
        col_b.metric("Std Dev", f"{{results_df[eui_col].std():.2f}}")

    # ── LHS variable vs KPI scatter ─────────────────────────────────
    numeric_cols = [
        c for c in results_df.columns
        if results_df[c].dtype in ("float64", "int64", "float32", "int32")
    ]
    kpi_cols_set = {{eui_col}} if eui_col else set()
    lhs_cols = [c for c in numeric_cols if c not in kpi_cols_set and "sample_id" not in c.lower()]

    if lhs_cols and eui_col:
        st.subheader("Parameter vs EUI")
        param = st.selectbox("Select parameter", lhs_cols, key="scatter_param")
        if param:
            st.scatter_chart(results_df, x=param, y=eui_col)

    # ── Raw data table ──────────────────────────────────────────────
    st.subheader("Raw Results")
    st.dataframe(results_df)


# ── Failure Summary ────────────────────────────────────────────────────

if failed_df is not None and not failed_df.empty:
    st.header("Failure Summary")
    st.dataframe(failed_df)
elif run_trace is not None:
    per_sample = run_trace.get("per_sample", [])
    failed_samples = [s for s in per_sample if s.get("status") == "failed"]
    if failed_samples:
        st.header("Failure Summary")
        st.dataframe(pd.DataFrame(failed_samples))


# ── Cost Distribution (if available) ───────────────────────────────────

if results_df is not None:
    cost_col = None
    for c in ("cost_usd", "cost", "total_cost_usd"):
        if c in results_df.columns:
            cost_col = c
            break
    if cost_col is not None:
        st.header("Cost Distribution")
        st.bar_chart(results_df, x=results_df.columns[0], y=cost_col)
        st.metric("Total Cost", f"${{results_df[cost_col].sum():,.2f}}")


# ── No data ─────────────────────────────────────────────────────────────

if results_df is None and run_trace is None:
    st.warning(
        "No campaign data found. Run a campaign first, then: "
        "`osimflow dashboard <outdir>`"
    )
'''
