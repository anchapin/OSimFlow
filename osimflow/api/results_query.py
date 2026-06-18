"""Results query and export API endpoints (issue #585).

Provides:
  - GET  /api/v1/campaigns/{campaign_id}/results/query  — query results with filters
  - GET  /api/v1/campaigns/{campaign_id}/results/export  — export results to CSV/JSON

CLI helpers:
  - query_results_cli()
  - export_results_cli()

These are used by the ``query-results`` and ``export-results`` CLI subcommands
in ``osimflow.__main__``.
"""

from __future__ import annotations

import csv
import io
import json
import logging
from pathlib import Path
from typing import Any

import pandas as pd
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response

from osimflow.api.campaigns import (
    _campaign_dir_from_id,
    _campaigns_base_dir,
    _load_campaign_json,
)

log = logging.getLogger("osimflow.api.results_query")

results_query_router = APIRouter()


def _load_aggregated_results(campaign_dir: Path) -> pd.DataFrame:
    """Load aggregated_results.csv from a campaign directory.

    Returns an empty DataFrame if the file does not exist.
    """
    csv_path = campaign_dir / "aggregated_results.csv"
    if not csv_path.exists():
        return pd.DataFrame()
    try:
        return pd.read_csv(csv_path)
    except Exception:
        log.warning("failed to read aggregated_results.csv in %s", campaign_dir)
        return pd.DataFrame()


def _apply_filter(df: pd.DataFrame, filter_spec: dict[str, Any]) -> pd.DataFrame:
    """Apply a MongoDB-style filter spec to a DataFrame.

    Supports:
      - Top-level equality: ``{"status": "ok"}``
      - Comparison operators: ``{"kpi.eui": {"$gt": 100}}``
      - ``$in``, ``$nin`` for array membership
      - ``$exists`` for field presence
    """
    if not filter_spec:
        return df

    for key, value in filter_spec.items():
        if key.startswith("$"):
            continue

        if isinstance(value, dict):
            for op, op_val in value.items():
                if op == "$eq":
                    df = df[df[key] == op_val]
                elif op == "$ne":
                    df = df[df[key] != op_val]
                elif op == "$gt":
                    df = df[df[key] > op_val]
                elif op == "$gte":
                    df = df[df[key] >= op_val]
                elif op == "$lt":
                    df = df[df[key] < op_val]
                elif op == "$lte":
                    df = df[df[key] <= op_val]
                elif op == "$in":
                    df = df[df[key].isin(op_val)]
                elif op == "$nin":
                    df = df[~df[key].isin(op_val)]
                elif op == "$exists":
                    if op_val:
                        df = df[df[key].notna()]
                    else:
                        df = df[df[key].isna()]
                else:
                    log.warning("unknown filter operator: %s", op)
        else:
            df = df[df[key] == value]

    return df


# ---------------------------------------------------------------------------
# GET /api/v1/campaigns/{campaign_id}/results/query
# ---------------------------------------------------------------------------


@results_query_router.get("/api/v1/campaigns/{campaign_id}/results/query")
async def query_campaign_results(
    campaign_id: str,
    request: Request,
    page: int = Query(1, ge=1, description="Page number (1-indexed)"),
    per_page: int = Query(50, ge=1, le=1000, description="Items per page (max 1000)"),
    status: str | None = Query(None, description="Filter by sample status (ok/failed/running)"),
) -> dict[str, Any]:
    """Query aggregated results for a campaign with optional filters.

    Reads ``aggregated_results.csv`` from the campaign directory and applies
    server-side filtering and pagination. Supports MongoDB-style filter
    operators in the ``filter`` query parameter as a JSON object.

    Filter examples::

        ?filter={"status": "ok"}
        ?filter={"kpi.eui": {"$gt": 100}}
        ?filter={"kpi.eui": {"$gte": 50, "$lte": 200}}

    Returns a paginated list of result rows with total count.
    """
    base = _campaigns_base_dir(request)
    campaign_dir = _campaign_dir_from_id(base, campaign_id)

    df = _load_aggregated_results(campaign_dir)
    if df.empty:
        return {"rows": [], "total": 0, "page": page, "per_page": per_page}

    # Apply status filter from query param
    if status is not None:
        if "status" in df.columns:
            df = df[df["status"] == status]
        else:
            log.warning("status column not found in aggregated_results.csv")

    # Parse filter JSON from query param
    filter_param = request.query_params.get("filter")
    if filter_param:
        try:
            filter_spec = json.loads(filter_param)
            if isinstance(filter_spec, dict):
                df = _apply_filter(df, filter_spec)
        except (json.JSONDecodeError, TypeError) as exc:
            raise HTTPException(status_code=400, detail=f"Invalid filter JSON: {exc}") from exc

    total = len(df)

    # Paginate
    start = (page - 1) * per_page
    end = start + per_page
    page_df = df.iloc[start:end]

    rows: list[dict[str, Any]] = json.loads(page_df.to_json(orient="records"))

    return {
        "rows": rows,
        "total": total,
        "page": page,
        "per_page": per_page,
        "campaign_id": campaign_id,
    }


# ---------------------------------------------------------------------------
# GET /api/v1/campaigns/{campaign_id}/results/export
# ---------------------------------------------------------------------------


@results_query_router.get("/api/v1/campaigns/{campaign_id}/results/export")
async def export_campaign_results(
    campaign_id: str,
    request: Request,
    format: str = Query("csv", description="Export format: csv or json"),
    status: str | None = Query(None, description="Filter by sample status"),
    include_failed: bool = Query(True, description="Include failed simulations"),
) -> Response:
    """Export aggregated results for a campaign as CSV or JSON.

    Optionally filters by status. Returns the full result set (no pagination)
    for export purposes.

    Query parameters:
      - ``format``: ``csv`` (default) or ``json``
      - ``status``: filter by sample status (``ok``, ``failed``)
      - ``include_failed``: include failed simulations (default True)
    """
    base = _campaigns_base_dir(request)
    campaign_dir = _campaign_dir_from_id(base, campaign_id)

    df = _load_aggregated_results(campaign_dir)
    if df.empty:
        raise HTTPException(
            status_code=404,
            detail="aggregated_results.csv not found for this campaign",
        )

    # Apply status filter
    if status is not None and "status" in df.columns:
        df = df[df["status"] == status]

    if not include_failed and "status" in df.columns:
        df = df[df["status"] != "failed"]

    # Apply filter from query param
    filter_param = request.query_params.get("filter")
    if filter_param:
        try:
            filter_spec = json.loads(filter_param)
            if isinstance(filter_spec, dict):
                df = _apply_filter(df, filter_spec)
        except (json.JSONDecodeError, TypeError) as exc:
            raise HTTPException(status_code=400, detail=f"Invalid filter JSON: {exc}") from exc

    if format == "json":
        records = json.loads(df.to_json(orient="records"))
        content = json.dumps({"campaign_id": campaign_id, "rows": records}, indent=2, default=str)
        return Response(
            content=content,
            media_type="application/json",
            headers={
                "Content-Disposition": f'attachment; filename="{campaign_id}_results.json"'
            },
        )

    # CSV format
    output = io.StringIO()
    df.to_csv(output, index=False)
    csv_content = output.getvalue()
    return Response(
        content=csv_content,
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="{campaign_id}_results.csv"'
        },
    )


# ---------------------------------------------------------------------------
# CLI helpers (called from osimflow.__main__)
# ---------------------------------------------------------------------------


def query_results_cli(
    campaign_ids: list[str] | None = None,
    outdirs: list[str] | None = None,
    filter_expr: str | None = None,
    page: int = 1,
    per_page: int = 50,
    format: str = "table",
) -> dict[str, Any]:
    """CLI helper for ``osimflow query-results``.

    Parameters
    ----------
    campaign_ids
        List of campaign IDs to query (resolved via campaigns base dir).
    outdirs
        List of explicit output directory paths to query.
    filter_expr
        JSON filter expression as a string.
    page
        Page number (1-indexed).
    per_page
        Items per page.
    format
        Output format: ``table`` or ``json``.

    Returns
    -------
    dict
        Keys: ``rows``, ``total``, ``columns``, ``campaigns_queried``.
    """
    if not campaign_ids and not outdirs:
        return {"rows": [], "total": 0, "columns": [], "campaigns_queried": 0}

    all_rows: list[dict[str, Any]] = []
    all_columns: set[str] = set()
    campaigns_queried = 0

    filter_spec: dict[str, Any] = {}
    if filter_expr:
        try:
            filter_spec = json.loads(filter_expr)
        except json.JSONDecodeError as exc:
            log.error("Invalid filter expression: %s", exc)
            return {"rows": [], "total": 0, "columns": [], "campaigns_queried": 0}

    # Collect all outdirs to query
    paths_to_query: list[tuple[Path, str]] = []

    if outdirs:
        for outdir in outdirs:
            p = Path(outdir)
            if p.is_dir():
                paths_to_query.append((p, p.name))
            else:
                log.warning("Outdir not found, skipping: %s", outdir)

    if campaign_ids:
        for cid in campaign_ids:
            base = Path.cwd()
            campaign_dir = base / cid
            if campaign_dir.is_dir():
                paths_to_query.append((campaign_dir, cid))

    for campaign_path, label in paths_to_query:
        csv_path = campaign_path / "aggregated_results.csv"
        if not csv_path.exists():
            continue

        try:
            df = pd.read_csv(csv_path)
        except Exception as exc:
            log.warning("Failed to read %s: %s", csv_path, exc)
            continue

        if filter_spec:
            df = _apply_filter(df, filter_spec)

        if df.empty:
            continue

        campaigns_queried += 1

        for col in df.columns:
            if col not in ("sample_id",):
                all_columns.add(col)

        page_df = df.iloc[(page - 1) * per_page : page * per_page]
        rows = json.loads(page_df.to_json(orient="records"))
        for row in rows:
            row["_campaign"] = label
        all_rows.extend(rows)

    columns = sorted(all_columns)
    if not all_rows:
        return {"rows": [], "total": 0, "columns": columns, "campaigns_queried": campaigns_queried}

    return {
        "rows": all_rows,
        "total": len(all_rows),
        "columns": columns,
        "campaigns_queried": campaigns_queried,
    }


def export_results_cli(
    campaign_ids: list[str] | None = None,
    outdirs: list[str] | None = None,
    filter_expr: str | None = None,
    format: str = "csv",
    output_path: str | None = None,
    include_failed: bool = True,
) -> int:
    """CLI helper for ``osimflow export-results``.

    Parameters
    ----------
    campaign_ids
        List of campaign IDs to export.
    outdirs
        List of explicit output directory paths to export.
    filter_expr
        JSON filter expression as a string.
    format
        Export format: ``csv`` or ``json``.
    output_path
        Output file path. If None, prints to stdout.
    include_failed
        Include failed simulations in export.

    Returns
    -------
    int
        Exit code (0 = success, 1 = error).
    """
    filter_spec: dict[str, Any] = {}
    if filter_expr:
        try:
            filter_spec = json.loads(filter_expr)
        except json.JSONDecodeError as exc:
            log.error("Invalid filter expression: %s", exc)
            return 1

    paths_to_query: list[tuple[Path, str]] = []

    if outdirs:
        for outdir in outdirs:
            p = Path(outdir)
            if p.is_dir():
                paths_to_query.append((p, p.name))
            else:
                log.warning("Outdir not found, skipping: %s", outdir)

    if campaign_ids:
        for cid in campaign_ids:
            base = Path.cwd()
            campaign_dir = base / cid
            if campaign_dir.is_dir():
                paths_to_query.append((campaign_dir, cid))

    if not paths_to_query:
        log.error("No valid campaign directories found")
        return 1

    all_dfs: list[pd.DataFrame] = []

    for campaign_path, label in paths_to_query:
        csv_path = campaign_path / "aggregated_results.csv"
        if not csv_path.exists():
            log.warning("No aggregated_results.csv found in %s", campaign_path)
            continue

        try:
            df = pd.read_csv(csv_path)
        except Exception as exc:
            log.warning("Failed to read %s: %s", csv_path, exc)
            continue

        if not include_failed and "status" in df.columns:
            df = df[df["status"] != "failed"]

        if filter_spec:
            df = _apply_filter(df, filter_spec)

        if not df.empty:
            df["_campaign"] = label
            all_dfs.append(df)

    if not all_dfs:
        log.error("No results to export")
        return 1

    combined = pd.concat(all_dfs, ignore_index=True)

    if format == "json":
        records = json.loads(combined.to_json(orient="records"))
        content = json.dumps({"campaigns": [label for _, label in paths_to_query], "rows": records}, indent=2, default=str)
    else:
        output = io.StringIO()
        combined.to_csv(output, index=False)
        content = output.getvalue()

    if output_path:
        Path(output_path).write_text(content)
        print(f"Exported {len(combined)} rows to {output_path}")
    else:
        print(content)

    return 0
