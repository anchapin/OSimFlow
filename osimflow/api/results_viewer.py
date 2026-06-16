"""OSimFlowResultsViewer — interactive Plotly.js results browser (issue #533).

Provides:
  - GET  /results/{campaign_id}                    — campaign summary + column schema
  - GET  /results/{campaign_id}/scatter             — scatter plot data (variables vs KPIs)
  - GET  /results/{campaign_id}/histogram            — histogram data for a KPI
  - GET  /results/{campaign_id}/timeseries          — aggregated hourly/daily/monthly time-series
  - GET  /results/{campaign_id}/export             — CSV / JSON / Parquet export

Served at ``/results/`` when the FastAPI app is created with
``results_viewer=True`` (set automatically when ``--dashboard`` is passed
on the CLI).

The frontend is a self-contained HTML/JS file that loads Plotly.js from CDN
and fetches data from these endpoints, rendering:
  - EUI histogram (interactive, zoomable)
  - Variable vs KPI scatter plot (with dropdowns)
  - Parallel coordinates plot
  - Sensitivity analysis results (when available)
  - Time-series browser (for hourly data)
  - Failed simulations table with error details
  - Export buttons (CSV, JSON, Parquet)
"""

from __future__ import annotations

import io
import json
import logging
from pathlib import Path
from typing import Any

import pandas as pd
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response

from osimflow.api.campaigns import _campaign_dir_from_id, _load_campaign_json

log = logging.getLogger("osimflow.api.results_viewer")

results_viewer_router = APIRouter()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _results_csv(campaign_dir: Path) -> Path:
    """Path to aggregated_results.csv for a campaign."""
    return campaign_dir / "aggregated_results.csv"


def _failed_csv(campaign_dir: Path) -> Path:
    """Path to failed_simulations.csv for a campaign."""
    return campaign_dir / "failed_simulations.csv"


def _load_results_df(campaign_dir: Path) -> pd.DataFrame:
    """Load aggregated_results.csv as a DataFrame. Raises 404 if absent."""
    csv_path = _results_csv(campaign_dir)
    if not csv_path.exists():
        raise HTTPException(
            status_code=404,
            detail="aggregated_results.csv not found for this campaign",
        )
    return pd.read_csv(csv_path)


def _load_failed_df(campaign_dir: Path) -> pd.DataFrame | None:
    """Load failed_simulations.csv as a DataFrame. Returns None if absent."""
    csv_path = _failed_csv(campaign_dir)
    if not csv_path.exists():
        return None
    return pd.read_csv(csv_path)


def _numeric_columns(df: pd.DataFrame) -> list[str]:
    """Return numeric column names from a DataFrame, excluding ID-like columns."""
    return [
        c
        for c in df.columns
        if df[c].dtype in ("float64", "int64", "float32", "int32")
        and "sample_id" not in c.lower()
        and "id" != c.lower()
    ]


def _infer_kpi_columns(df: pd.DataFrame) -> list[str]:
    """Heuristic: columns that look like KPI/output metrics (not LHS variables)."""
    kpi_markers = {
        "eui",
        "cost",
        "energy",
        "peak",
        "power",
        "total_",
        "hourly",
        "annual",
        "site",
        "source",
        "thermal",
        "comfort",
        "lux",
        "glazing",
        "shading",
        "latitude",
        "longitude",
        "orientation",
        "aspect",
        "wwr",
        "u_value",
        "r_value",
        "shgc",
        "cda",
        "cop",
        "chiller",
        "boiler",
        "co2",
        "carbon",
        "emissions",
    }
    return [c for c in _numeric_columns(df) if any(m in c.lower() for m in kpi_markers)]


def _infer_lhs_columns(df: pd.DataFrame) -> list[str]:
    """Heuristic: columns that look like LHS input variables (not KPIs)."""
    kpi_cols = set(_infer_kpi_columns(df))
    return [c for c in _numeric_columns(df) if c not in kpi_cols]


def _eui_column(df: pd.DataFrame) -> str | None:
    """Find the EUI column in a results DataFrame."""
    for candidate in ("eui_kwh_m2_yr", "eui", "EUI", "eui_kbtu_ft2_yr"):
        if candidate in df.columns:
            return candidate
    for col in df.columns:
        if "eui" in col.lower():
            return col
    return None


def _classify_failure(error_text: str) -> str:
    """Classify a failure based on error text content."""
    error_lower = error_text.lower()
    if "convergence" in error_lower or "did not converge" in error_lower:
        return "convergence"
    if "surface" in error_lower and ("intersection" in error_lower or "non.convex" in error_lower):
        return "surface_geometry"
    if "autosize" in error_lower or "plant" in error_lower:
        return "hvac_sizing"
    if "schedule" in error_lower:
        return "schedule"
    if "material" in error_lower or "construction" in error_lower:
        return "material_construction"
    if "weather" in error_lower or ".epw" in error_lower:
        return "weather_file"
    if "memory" in error_lower or "timeout" in error_lower:
        return "memory_timeout"
    if "temperature" in error_lower and "out" in error_lower:
        return "timestep_instability"
    return "generic_severe"


# ---------------------------------------------------------------------------
# GET /results/{campaign_id} — campaign summary
# ---------------------------------------------------------------------------


@results_viewer_router.get("/results/{campaign_id}")  # type: ignore[untyped-decorator]
async def results_campaign_summary(
    campaign_id: str,
    request: Request,
) -> dict[str, Any]:
    """Return campaign summary for the Results Viewer.

    Includes:
    - Campaign metadata (id, status, elapsed, sample counts)
    - Column schema: lhs_variables, kpi_columns, all_numeric_columns
    - EUI column hint
    - Available plot types (histogram, scatter, timeseries, export)
    - Failed simulation count
    """
    from osimflow.api.campaigns import _campaigns_base_dir  # noqa: PLC0415

    base = _campaigns_base_dir(request)
    campaign_dir = _campaign_dir_from_id(base, campaign_id)
    _load_campaign_json(campaign_dir)

    run_data = _load_campaign_json(campaign_dir)
    per_sample: list[dict[str, Any]] = run_data.get("per_sample", [])
    n_total = len(per_sample)
    n_success = sum(1 for s in per_sample if s.get("status") == "ok")
    n_failed = sum(1 for s in per_sample if s.get("status") == "failed")
    n_cached = sum(1 for s in per_sample if s.get("status") == "cached")

    results_df: pd.DataFrame | None = None
    try:
        results_df = _load_results_df(campaign_dir)
    except HTTPException:
        pass

    lhs_columns: list[str] = []
    kpi_columns: list[str] = []
    all_numeric: list[str] = []
    eui_col: str | None = None
    has_results = False

    if results_df is not None and not results_df.empty:
        has_results = True
        lhs_columns = _infer_lhs_columns(results_df)
        kpi_columns = _infer_kpi_columns(results_df)
        all_numeric = _numeric_columns(results_df)
        eui_col = _eui_column(results_df)

    failed_df = _load_failed_df(campaign_dir)
    n_failures = 0
    if failed_df is not None:
        n_failures = len(failed_df)

    return {
        "campaign_id": campaign_id,
        "status": run_data.get("status", "unknown"),
        "started_at": run_data.get("started_at"),
        "finished_at": run_data.get("finished_at"),
        "elapsed_s": run_data.get("elapsed_s"),
        "samples": {
            "total": n_total,
            "success": n_success + n_cached,
            "failed": n_failed,
            "cached": n_cached,
        },
        "has_results": has_results,
        "schema": {
            "lhs_variables": lhs_columns,
            "kpi_columns": kpi_columns,
            "all_numeric": all_numeric,
        },
        "eui_column": eui_col,
        "n_failures": n_failures,
        "available_plots": {
            "histogram": has_results and len(kpi_columns) > 0,
            "scatter": has_results and len(lhs_columns) > 0 and len(kpi_columns) > 0,
            "timeseries": True,
            "export": has_results or n_failures > 0,
        },
    }


# ---------------------------------------------------------------------------
# GET /results/{campaign_id}/scatter — scatter plot data
# ---------------------------------------------------------------------------


@results_viewer_router.get("/results/{campaign_id}/scatter")  # type: ignore[untyped-decorator]
async def results_scatter(
    campaign_id: str,
    request: Request,
    variable: str = Query(..., description="LHS variable column name"),
    kpi: str = Query(..., description="KPI column name"),
) -> dict[str, Any]:
    """Return scatter-plot data for a (variable, KPI) pair.

    Response shape::

        {
          "variable": "wall_u_value",
          "kpi": "eui_kwh_m2_yr",
          "n_points": 250,
          "data": [
            {"sample_id": "0000", "variable_value": 0.5, "kpi_value": 120.3},
            ...
          ],
          "correlation": 0.42,
          "r_squared": 0.18
        }
    """
    from osimflow.api.campaigns import _campaigns_base_dir  # noqa: PLC0415

    base = _campaigns_base_dir(request)
    campaign_dir = _campaign_dir_from_id(base, campaign_id)
    _load_campaign_json(campaign_dir)

    df = _load_results_df(campaign_dir)

    if variable not in df.columns:
        raise HTTPException(status_code=400, detail=f"Variable {variable!r} not found in results")
    if kpi not in df.columns:
        raise HTTPException(status_code=400, detail=f"KPI {kpi!r} not found in results")

    # Filter to rows where both values are non-null
    sub = df[["sample_id", variable, kpi]].dropna()
    data = [
        {"sample_id": str(row["sample_id"]), "variable_value": float(row[variable]), "kpi_value": float(row[kpi])}
        for row in sub.to_dict(orient="records")
    ]

    # Compute Pearson correlation
    corr = float(sub[variable].corr(sub[kpi])) if len(sub) > 1 else 0.0
    r_squared = corr ** 2

    return {
        "variable": variable,
        "kpi": kpi,
        "n_points": len(data),
        "data": data,
        "correlation": corr,
        "r_squared": r_squared,
    }


# ---------------------------------------------------------------------------
# GET /results/{campaign_id}/histogram — histogram data
# ---------------------------------------------------------------------------


@results_viewer_router.get("/results/{campaign_id}/histogram")  # type: ignore[untyped-decorator]
async def results_histogram(
    campaign_id: str,
    request: Request,
    kpi: str = Query(..., description="KPI column name for histogram"),
    bins: int = Query(20, ge=5, le=100, description="Number of bins (default 20)"),
) -> dict[str, Any]:
    """Return histogram data for a KPI column.

    Response shape::

        {
          "kpi": "eui_kwh_m2_yr",
          "units": "kWh/m²/yr",
          "n_points": 250,
          "bins": [50.0, 75.0, 100.0, ...],
          "counts": [5, 18, 42, ...],
          "mean": 123.4,
          "median": 119.8,
          "std": 22.1,
          "p5": 98.2,
          "p95": 167.5,
          "min": 61.3,
          "max": 210.8
        }
    """
    from osimflow.api.campaigns import _campaigns_base_dir  # noqa: PLC0415

    base = _campaigns_base_dir(request)
    campaign_dir = _campaign_dir_from_id(base, campaign_id)
    _load_campaign_json(campaign_dir)

    df = _load_results_df(campaign_dir)

    if kpi not in df.columns:
        raise HTTPException(status_code=400, detail=f"KPI {kpi!r} not found in results")

    series = pd.to_numeric(df[kpi], errors="coerce").dropna()

    if series.empty:
        raise HTTPException(status_code=404, detail=f"No numeric data for KPI {kpi!r}")

    hist_counts, bin_edges = pd.cut(series, bins=bins, retbins=True)
    counts = hist_counts.value_counts(sort=False).tolist()
    bin_centers = [(bin_edges[i] + bin_edges[i + 1]) / 2 for i in range(len(bin_edges) - 1)]

    return {
        "kpi": kpi,
        "n_points": int(len(series)),
        "bins": bin_centers,
        "counts": counts,
        "mean": float(series.mean()),
        "median": float(series.median()),
        "std": float(series.std()) if len(series) > 1 else 0.0,
        "p5": float(series.quantile(0.05)),
        "p95": float(series.quantile(0.95)),
        "min": float(series.min()),
        "max": float(series.max()),
    }


# ---------------------------------------------------------------------------
# GET /results/{campaign_id}/timeseries — aggregated time-series
# ---------------------------------------------------------------------------


@results_viewer_router.get("/results/{campaign_id}/timeseries")  # type: ignore[untyped-decorator]
async def results_timeseries(
    campaign_id: str,
    request: Request,
    variable: str = Query(..., description="Variable name to aggregate across samples"),
    resolution: str = Query(
        "hourly", description="Aggregation resolution: hourly, daily, or monthly"
    ),
) -> dict[str, Any]:
    """Return aggregated time-series for a variable across all samples.

    Averages the variable's time-series across all samples that have it,
    grouped by the specified resolution.

    Response shape::

        {
          "variable": "zone_air_temperature",
          "resolution": "hourly",
          "n_samples": 42,
          "n_timesteps": 8760,
          "timestamps": ["2024-01-01T01:00:00", ...],
          "mean": [20.5, 20.3, ...],
          "std": [1.2, 1.4, ...],
          "p5": [18.1, 17.9, ...],
          "p95": [23.1, 22.8, ...]
        }
    """
    from osimflow.api.campaigns import _campaigns_base_dir  # noqa: PLC0415

    base = _campaigns_base_dir(request)
    campaign_dir = _campaign_dir_from_id(base, campaign_id)
    run_data = _load_campaign_json(campaign_dir)

    per_sample: list[dict[str, Any]] = run_data.get("per_sample", [])
    sim_dirs = [
        (s["sample_id"], campaign_dir / "work" / "sim" / s["sample_id"])
        for s in per_sample
        if s.get("status") == "ok" and (campaign_dir / "work" / "sim" / s["sample_id"]).exists()
    ]

    if not sim_dirs:
        raise HTTPException(status_code=404, detail="No completed samples with simulation data found")

    import sqlite3

    all_series: list[dict[str, Any]] = []
    for sid, sim_dir in sim_dirs:
        sql_path = sim_dir / "eplusout.sql"
        if not sql_path.exists():
            continue
        try:
            conn = sqlite3.connect(str(sql_path))
            conn.row_factory = sqlite3.Row
        except Exception:
            continue

        try:
            # Try to find the variable with flexible matching
            rows = conn.execute(
                """
                SELECT VariableName, KeyName, Units
                FROM reportmetadata
                WHERE VariableName LIKE ?
                LIMIT 1
                """,
                (f"%{variable}%",),
            ).fetchall()
            if not rows:
                continue
            var_name = rows[0]["VariableName"]
            units = rows[0]["Units"] or ""

            if resolution == "hourly":
                group_fmt = "%Y-%m-%d %H:00"
                select_fmt = "%Y-%m-%dT%H:00:00"
            elif resolution == "daily":
                group_fmt = "%Y-%m-%d"
                select_fmt = "%Y-%m-%d"
            elif resolution == "monthly":
                group_fmt = "%Y-%m"
                select_fmt = "%Y-%m"
            else:
                raise HTTPException(
                    status_code=400,
                    detail="resolution must be hourly, daily, or monthly",
                )

            ts_rows = conn.execute(
                f"""
                SELECT
                    strftime('{select_fmt}',
                        td.Month, td.Day,
                        CASE WHEN td.Hour = 24 THEN 0 ELSE td.Hour END,
                        '00', '00') AS ts,
                    AVG(rd.Value) AS value
                FROM reportdata rd
                JOIN timedata td ON rd.TimeIndex = td.TimeIndex
                WHERE rd.VariableName = ?
                GROUP BY strftime('{group_fmt}',
                        td.Month, td.Day,
                        CASE WHEN td.Hour = 24 THEN 0 ELSE td.Hour END,
                        '00', '00')
                ORDER BY ts
                """,
                (var_name,),
            ).fetchall()
            all_series.append(
                {row["ts"]: float(row["value"]) for row in ts_rows}
            )
        finally:
            conn.close()

    if not all_series:
        raise HTTPException(
            status_code=404,
            detail=f"No time-series data found for variable {variable!r}",
        )

    # Align all series to a common timeline
    all_timestamps: set[str] = set()
    for s in all_series:
        all_timestamps.update(s.keys())
    sorted_ts = sorted(all_timestamps)

    values_by_ts: dict[str, list[float]] = {ts: [] for ts in sorted_ts}
    for s in all_series:
        for ts in sorted_ts:
            values_by_ts[ts].append(s.get(ts))

    import numpy as np

    mean_vals = [float(np.nanmean(values_by_ts[ts])) for ts in sorted_ts]
    std_vals = [float(np.nanstd(values_by_ts[ts])) for ts in sorted_ts]
    p5_vals = [float(np.nanpercentile(values_by_ts[ts], 5)) for ts in sorted_ts]
    p95_vals = [float(np.nanpercentile(values_by_ts[ts], 95)) for ts in sorted_ts]

    return {
        "variable": variable,
        "resolution": resolution,
        "n_samples": len(all_series),
        "n_timesteps": len(sorted_ts),
        "timestamps": sorted_ts,
        "mean": mean_vals,
        "std": std_vals,
        "p5": p5_vals,
        "p95": p95_vals,
    }


# ---------------------------------------------------------------------------
# GET /results/{campaign_id}/export — data export
# ---------------------------------------------------------------------------


@results_viewer_router.get("/results/{campaign_id}/export")  # type: ignore[untyped-decorator]
async def results_export(
    campaign_id: str,
    request: Request,
    fmt: str = Query("csv", description="Export format: csv, json, or parquet"),
    include_failed: bool = Query(True, description="Include failed simulation records"),
) -> Response:
    """Export campaign results as CSV, JSON, or Parquet.

    Returns the raw file content with the appropriate Content-Type:
      - ``csv``  → ``text/csv``
      - ``json`` → ``application/json``
      - ``parquet`` → ``application/octet-stream``
    """
    from osimflow.api.campaigns import _campaigns_base_dir  # noqa: PLC0415

    base = _campaigns_base_dir(request)
    campaign_dir = _campaign_dir_from_id(base, campaign_id)
    _load_campaign_json(campaign_dir)

    if fmt not in ("csv", "json", "parquet"):
        raise HTTPException(status_code=400, detail="format must be csv, json, or parquet")

    try:
        df = _load_results_df(campaign_dir)
    except HTTPException:
        df = pd.DataFrame()

    if include_failed:
        failed_df = _load_failed_df(campaign_dir)
        if failed_df is not None and not failed_df.empty:
            combined = pd.concat([df, failed_df], ignore_index=True) if not df.empty else failed_df
            df = combined

    if df.empty:
        raise HTTPException(status_code=404, detail="No results to export")

    if fmt == "csv":
        buf = io.StringIO()
        df.to_csv(buf, index=False)
        return Response(
            content=buf.getvalue(),
            media_type="text/csv",
            headers={
                "Content-Disposition": f'attachment; filename="{campaign_id}_results.csv"',
            },
        )
    elif fmt == "json":
        return Response(
            content=df.to_json(orient="records", indent=2),
            media_type="application/json",
            headers={
                "Content-Disposition": f'attachment; filename="{campaign_id}_results.json"',
            },
        )
    else:
        import pyarrow as pa
        import pyarrow.parquet as pq

        buf = io.BytesIO()
        table = pa.Table.from_pandas(df)
        pq.write_table(table, buf)
        return Response(
            content=buf.getvalue(),
            media_type="application/octet-stream",
            headers={
                "Content-Disposition": f'attachment; filename="{campaign_id}_results.parquet"',
            },
        )


# ---------------------------------------------------------------------------
# GET /results/{campaign_id}/failures — failure table
# ---------------------------------------------------------------------------


@results_viewer_router.get("/results/{campaign_id}/failures")  # type: ignore[untyped-decorator]
async def results_failures(campaign_id: str, request: Request) -> dict[str, Any]:
    """Return the failed simulations table with error classifications."""
    from osimflow.api.campaigns import _campaigns_base_dir  # noqa: PLC0415

    base = _campaigns_base_dir(request)
    campaign_dir = _campaign_dir_from_id(base, campaign_id)
    _load_campaign_json(campaign_dir)

    failed_df = _load_failed_df(campaign_dir)
    if failed_df is None or failed_df.empty:
        return {"total": 0, "failures": []}

    records = failed_df.to_dict(orient="records")
    enriched = []
    for row in records:
        error_text = str(row.get("error_summary", "") or row.get("message", ""))
        row["failure_category"] = _classify_failure(error_text)
        row["error_summary"] = error_text[:500] if error_text else ""
        enriched.append(row)

    return {
        "total": len(enriched),
        "failures": enriched,
    }


# ---------------------------------------------------------------------------
# GET /results/{campaign_id}/parallel_coordinates — parallel coords data
# ---------------------------------------------------------------------------


@results_viewer_router.get("/results/{campaign_id}/parallel_coordinates")  # type: ignore[untyped-decorator]
async def results_parallel_coordinates(campaign_id: str, request: Request) -> dict[str, Any]:
    """Return data for a parallel coordinates plot.

    Returns the top LHS variables and top KPIs for all samples,
    suitable for rendering with Plotly's parallel coordinates chart.

    Response shape::

        {
          "dimensions": [
            {"label": "wall_u_value", "values": [0.5, 0.3, ...]},
            {"label": "eui_kwh_m2_yr", "values": [120.3, 98.7, ...]}
          ],
          "samples": [
            {"sample_id": "0000", "color_value": 120.3},
            ...
          ],
          "color_column": "eui_kwh_m2_yr"
        }
    """
    from osimflow.api.campaigns import _campaigns_base_dir  # noqa: PLC0415

    base = _campaigns_base_dir(request)
    campaign_dir = _campaign_dir_from_id(base, campaign_id)
    _load_campaign_json(campaign_dir)

    df = _load_results_df(campaign_dir)

    lhs_cols = _infer_lhs_columns(df)
    kpi_cols = _infer_kpi_columns(df)

    # Limit to top 6 lhs + top 3 kpi for readability
    selected_cols = lhs_cols[:6] + kpi_cols[:3]
    color_col = kpi_cols[0] if kpi_cols else (lhs_cols[0] if lhs_cols else None)

    if not selected_cols:
        raise HTTPException(status_code=404, detail="No numeric columns found for parallel coordinates")

    sub = df[selected_cols + (["sample_id"] if "sample_id" in df.columns else [])].dropna()

    dimensions = []
    for col in selected_cols:
        dimensions.append({"label": col, "values": [float(v) for v in sub[col].tolist()]})

    sample_ids = [str(v) for v in (sub["sample_id"].tolist() if "sample_id" in sub.columns else range(len(sub)))]
    color_values = [float(v) for v in (sub[color_col].tolist() if color_col else [0] * len(sub))]

    return {
        "dimensions": dimensions,
        "samples": [{"sample_id": sid, "color_value": cv} for sid, cv in zip(sample_ids, color_values)],
        "color_column": color_col,
    }
