"""Query and export historical results across campaigns (issue #585).

Provides:
  - GET  /api/v1/results/query            — query results with filters
  - GET  /api/v1/results/export           — export results to CSV/JSON
  - GET  /api/v1/results/schema            — available KPI columns from aggregated_results.csv

The primary data source is ``aggregated_results.csv`` in each campaign's
output directory.  Campaign resolution uses the registry (when available)
or the campaigns base directory.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import re
from pathlib import Path
from typing import Any

import pandas as pd
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel

from osimflow.api.campaigns import (
    _campaigns_base_dir,
    _load_campaign_json,
    _resolve_campaign_dir,
)

log = logging.getLogger("osimflow.api.results_query")

results_query_router = APIRouter()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _campaigns_base_dir_from_request(request: Request) -> Path:
    """Return the campaigns base directory from app state."""
    base = _campaigns_base_dir(request)
    return base


def _load_aggregated_csv(campaign_dir: Path) -> pd.DataFrame:
    """Load aggregated_results.csv from a campaign directory.

    Returns an empty DataFrame if the file does not exist or cannot be parsed.
    """
    csv_path = campaign_dir / "aggregated_results.csv"
    if not csv_path.exists():
        return pd.DataFrame()
    try:
        return pd.read_csv(csv_path)
    except Exception:  # noqa: BLE001
        log.debug("failed to read %s", csv_path, exc_info=True)
        return pd.DataFrame()


def _load_failed_csv(campaign_dir: Path) -> pd.DataFrame:
    """Load failed_simulations.csv from a campaign directory.

    Returns an empty DataFrame if the file does not exist or cannot be parsed.
    """
    csv_path = campaign_dir / "failed_simulations.csv"
    if not csv_path.exists():
        return pd.DataFrame()
    try:
        return pd.read_csv(csv_path)
    except Exception:  # noqa: BLE001
        log.debug("failed to read %s", csv_path, exc_info=True)
        return pd.DataFrame()


def _parse_filter_expr(filter_expr: str) -> tuple[str, str, Any]:
    """Parse a filter expression like ``kpi.eui>100`` into (column, op, value).

    Supports operators: ``>``, ``<``, ``>=``, ``<=``, ``==``, ``!=``.
    Returns (column, operator_str, parsed_value).
    Raises ValueError on malformed input.
    """
    match = re.match(r"^(.+?)\s*(>=|<=|==|!=|>|<)\s*(.+)$", filter_expr.strip())
    if not match:
        raise ValueError(
            f"Invalid filter expression: {filter_expr!r}. "
            "Expected format: column op value (e.g., 'eui > 100', 'status == ok')"
        )
    col = match.group(1).strip()
    op = match.group(2)
    raw_val = match.group(3).strip()

    # Try to parse as float first, then int, then keep as string
    try:
        value: Any = float(raw_val)
        if value.is_integer():
            value = int(value)
    except ValueError:
        value = raw_val

    return col, op, value


def _apply_filter(df: pd.DataFrame, filter_expr: str) -> pd.DataFrame:
    """Apply a single filter expression to a DataFrame.

    Supports numeric and string comparisons.
    Returns the filtered DataFrame (may be empty).
    """
    try:
        col, op, value = _parse_filter_expr(filter_expr)
    except ValueError:
        raise HTTPException(status_code=400, detail=str(ValueError)) from None

    if col not in df.columns:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown column: {col!r}. Available columns: {list(df.columns)}",
        )

    col_series = df[col]
    if op == ">":
        mask = col_series > value
    elif op == "<":
        mask = col_series < value
    elif op == ">=":
        mask = col_series >= value
    elif op == "<=":
        mask = col_series <= value
    elif op == "==":
        mask = col_series == value
    elif op == "!=":
        mask = col_series != value
    else:
        raise HTTPException(status_code=400, detail=f"Unknown operator: {op!r}")

    return df.loc[mask]


def _resolve_campaigns(
    request: Request,
    campaign_ids: list[str] | None = None,
    outdirs: list[str] | None = None,
) -> list[tuple[str, Path]]:
    """Resolve a list of campaign identifiers to (campaign_id, directory) pairs.

    Resolution order:
    1. ``outdirs`` — direct path resolution (if provided)
    2. ``campaign_ids`` — registry lookup then campaigns base dir lookup

    Returns a list of (resolved_label, directory) for campaigns that exist.
    """
    results: list[tuple[str, Path]] = []
    seen: set[str] = set()

    if outdirs:
        for od in outdirs:
            path = Path(od)
            if not path.is_absolute():
                path = Path.cwd() / path
            if path.is_dir():
                label = path.name
                if label not in seen:
                    results.append((label, path))
                    seen.add(label)

    if campaign_ids:
        for cid in campaign_ids:
            if cid in seen:
                continue
            campaign_dir = _resolve_campaign_dir(request, cid, None)
            if campaign_dir is not None:
                results.append((cid, campaign_dir))
                seen.add(cid)

    return results


# ---------------------------------------------------------------------------
# GET /api/v1/results/schema — available columns from aggregated_results.csv
# ---------------------------------------------------------------------------


class ResultsSchemaResponse(BaseModel):
    """Response for GET /api/v1/results/schema."""

    columns: list[str]
    campaigns: list[dict[str, Any]]
    total_campaigns: int


@results_query_router.get("/api/v1/results/schema", response_model=ResultsSchemaResponse)
async def get_results_schema(
    request: Request,
    campaign_ids: str | None = Query(
        None,
        description="Comma-separated campaign IDs to include (default: all found)",
    ),
    outdirs: str | None = Query(
        None,
        description="Comma-separated output directory paths (alternative to campaign_ids)",
    ),
) -> ResultsSchemaResponse:
    """Return the union of all KPI/parameter columns from aggregated_results.csv
    across the specified campaigns.

    This endpoint helps clients discover which columns are available for
    filtering before issuing a query.
    """
    campaigns_base = _campaigns_base_dir_from_request(request)
    parsed_ids = [c.strip() for c in campaign_ids.split(",")] if campaign_ids else []
    parsed_outdirs = [o.strip() for o in outdirs.split(",")] if outdirs else []

    resolved = _resolve_campaigns(request, parsed_ids, parsed_outdirs)
    if not resolved:
        # Fall back to scanning all campaigns under the base dir
        resolved = _scan_all_campaigns(campaigns_base)

    all_columns: set[str] = set()
    campaign_info: list[dict[str, Any]] = []
    total = 0

    for label, camp_dir in resolved:
        csv_path = camp_dir / "aggregated_results.csv"
        if not csv_path.exists():
            continue
        total += 1
        try:
            df = pd.read_csv(csv_path)
            all_columns.update(df.columns.tolist())
            n_rows = len(df)
        except Exception:  # noqa: BLE001
            n_rows = 0
            log.debug("failed to read columns from %s", csv_path)
        campaign_info.append({"campaign_id": label, "n_rows": n_rows})

    return ResultsSchemaResponse(
        columns=sorted(all_columns),
        campaigns=campaign_info,
        total_campaigns=total,
    )


def _scan_all_campaigns(base: Path) -> list[tuple[str, Path]]:
    """Scan campaigns base directory for all campaign directories with run.json."""
    results: list[tuple[str, Path]] = []
    if not base.is_dir():
        return results
    for entry in sorted(base.iterdir()):
        if not entry.is_dir():
            continue
        run_json = entry / "run.json"
        if run_json.exists():
            results.append((entry.name, entry))
    return results


# ---------------------------------------------------------------------------
# GET /api/v1/results/query — query results across campaigns
# ---------------------------------------------------------------------------


def _load_campaign_rows(
    campaign_id: str,
    campaign_dir: Path,
    include_failed: bool = True,
) -> tuple[set[str], list[dict[str, Any]]]:
    """Load all result rows for a single campaign.

    Returns (all_columns, rows) where rows are dicts with campaign_id,
    sample_id, status, elapsed_s, cost_usd, error_summary, and KPI columns.
    """
    all_columns: set[str] = {
        "campaign_id",
        "sample_id",
        "status",
        "elapsed_s",
        "cost_usd",
        "error_summary",
    }
    agg_df = _load_aggregated_csv(campaign_dir)
    has_agg = not agg_df.empty

    if has_agg and "campaign_id" not in agg_df.columns:
        agg_df = agg_df.copy()
        agg_df["campaign_id"] = campaign_id
    if has_agg:
        all_columns.update(agg_df.columns.tolist())

    per_sample_map: dict[str, dict[str, Any]] = {}
    try:
        run_data = _load_campaign_json(campaign_dir)
        for sp in run_data.get("per_sample", []):
            sid = str(sp.get("sample_id", ""))
            if sid:
                per_sample_map[sid] = sp
    except Exception:  # noqa: BLE001
        log.debug("could not load run.json from %s", campaign_dir)

    if not has_agg:
        return all_columns, _load_failed_rows_only(
            campaign_id, campaign_dir, per_sample_map, include_failed
        )

    failed_map: dict[str, dict[str, Any]] = {}
    if include_failed:
        failed_df = _load_failed_csv(campaign_dir)
        if not failed_df.empty and "sample_id" in failed_df.columns:
            for _, row in failed_df.iterrows():
                failed_map[str(row["sample_id"])] = row.to_dict()

    all_rows: list[dict[str, Any]] = []
    for _, row in agg_df.iterrows():
        sid = str(row.get("sample_id", ""))
        row_dict = row.to_dict()
        ps = per_sample_map.get(sid)
        if ps:
            row_dict.setdefault("status", ps.get("status"))
            row_dict.setdefault("elapsed_s", ps.get("elapsed_s"))
            row_dict.setdefault("cost_usd", ps.get("cost_usd"))
        fd = failed_map.get(sid)
        if fd:
            row_dict.setdefault("error_summary", fd.get("error_summary"))
        all_rows.append(row_dict)

    return all_columns, all_rows


def _load_failed_rows_only(
    campaign_id: str,
    campaign_dir: Path,
    per_sample_map: dict[str, dict[str, Any]],
    include_failed: bool,
) -> list[dict[str, Any]]:
    """Load failed-only rows for a campaign (no aggregated_results.csv)."""
    if not include_failed:
        return []
    failed_df = _load_failed_csv(campaign_dir)
    if failed_df.empty or "sample_id" not in failed_df.columns:
        return []
    all_rows: list[dict[str, Any]] = []
    for _, row in failed_df.iterrows():
        sid = str(row["sample_id"])
        row_dict: dict[str, Any] = {
            "campaign_id": campaign_id,
            "sample_id": sid,
            "status": "failed",
            "error_summary": row.to_dict().get("error_summary"),
        }
        ps = per_sample_map.get(sid)
        if ps:
            row_dict.setdefault("elapsed_s", ps.get("elapsed_s"))
            row_dict.setdefault("cost_usd", ps.get("cost_usd"))
        all_rows.append(row_dict)
    return all_rows


class QueryResultsResponse(BaseModel):
    """Response envelope for the query results endpoint."""

    rows: list[dict[str, Any]]
    total: int
    columns: list[str]
    campaigns_queried: int


@results_query_router.get("/api/v1/results/query", response_model=QueryResultsResponse)
async def query_results(
    request: Request,
    campaign_ids: str | None = Query(
        None,
        description="Comma-separated campaign IDs to query (default: all found)",
    ),
    outdirs: str | None = Query(
        None,
        description="Comma-separated output directory paths (alternative to campaign_ids)",
    ),
    filter_expr: str | None = Query(
        None,
        description=(
            "Filter expression in 'column op value' format, e.g., 'eui > 100'. "
            "Supports: >, <, >=, <=, ==, !=. "
            "Can be specified multiple times; all filters are ANDed together."
        ),
    ),
    page: int = Query(1, ge=1, description="Page number (1-indexed)"),
    per_page: int = Query(100, ge=1, le=1000, description="Items per page (max 1000)"),
) -> QueryResultsResponse:
    """Query aggregated results across multiple campaigns with optional filters.

    Each row in the response includes the campaign_id, sample_id, status,
    elapsed_s, cost_usd, error_summary (from failed_simulations.csv), and
    all KPI columns from aggregated_results.csv.

    Filters are applied in AND fashion — a row must match ALL filter expressions.

    Results are paginated. The ``total`` field in the response reflects the
    total number of matching rows before pagination.
    """
    campaigns_base = _campaigns_base_dir_from_request(request)
    parsed_ids = [c.strip() for c in campaign_ids.split(",")] if campaign_ids else []
    parsed_outdirs = [o.strip() for o in outdirs.split(",")] if outdirs else []

    if parsed_ids or parsed_outdirs:
        resolved = _resolve_campaigns(request, parsed_ids, parsed_outdirs)
    else:
        resolved = _scan_all_campaigns(campaigns_base)

    if not resolved:
        return QueryResultsResponse(rows=[], total=0, columns=[], campaigns_queried=0)

    all_rows: list[dict[str, Any]] = []
    all_columns: set[str] = {
        "campaign_id",
        "sample_id",
        "status",
        "elapsed_s",
        "cost_usd",
        "error_summary",
    }

    # Parse filter expressions once
    filter_exprs: list[str] = []
    if filter_expr:
        filter_exprs = [f.strip() for f in filter_expr.split(";") if f.strip()]

    for campaign_id, campaign_dir in resolved:
        cols, rows = _load_campaign_rows(campaign_id, campaign_dir, include_failed=True)
        all_columns.update(cols)
        all_rows.extend(rows)

    # Apply filters
    for expr in filter_exprs:
        try:
            filtered: list[dict[str, Any]] = []
            for row in all_rows:
                # Build a temporary DataFrame from the single row for _apply_filter
                temp_df = pd.DataFrame([row])
                try:
                    result = _apply_filter(temp_df, expr)
                    if not result.empty:
                        filtered.append(row)
                except HTTPException:
                    # Column not in this row — filter out
                    continue
            all_rows = filtered
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=400, detail=f"Invalid filter expression: {exc}"
            ) from None

    total = len(all_rows)

    # Paginate
    start = (page - 1) * per_page
    end = start + per_page
    page_rows = all_rows[start:end]

    # Sort columns: fixed first, then dynamic
    fixed_cols = ["campaign_id", "sample_id", "status", "elapsed_s", "cost_usd", "error_summary"]
    dynamic_cols = sorted(c for c in all_columns if c not in fixed_cols)
    ordered_cols = [c for c in fixed_cols if c in all_columns] + dynamic_cols

    return QueryResultsResponse(
        rows=[{k: v for k, v in row.items() if k in ordered_cols} for row in page_rows],
        total=total,
        columns=[c for c in ordered_cols if c in all_columns],
        campaigns_queried=len(resolved),
    )


# ---------------------------------------------------------------------------
# GET /api/v1/results/export — export results to CSV or JSON
# ---------------------------------------------------------------------------


@results_query_router.get("/api/v1/results/export")
async def export_results(
    request: Request,
    campaign_ids: str | None = Query(
        None,
        description="Comma-separated campaign IDs to export (default: all found)",
    ),
    outdirs: str | None = Query(
        None,
        description="Comma-separated output directory paths (alternative to campaign_ids)",
    ),
    filter_expr: str | None = Query(
        None,
        description=(
            "Filter expression in 'column op value' format. "
            "Can be specified multiple times; all filters are ANDed together."
        ),
    ),
    format: str = Query(
        "csv",
        description="Export format: 'csv' (default) or 'json'",
    ),
    include_failed: bool = Query(
        True,
        description="Include failed simulations in the export (default: True)",
    ),
) -> Response:
    """Export aggregated results across multiple campaigns to CSV or JSON.

    The response is streamed for large result sets.  The Content-Disposition
    header is set to ``attachment; filename="results_export.<format>"``.
    """
    campaigns_base = _campaigns_base_dir_from_request(request)
    parsed_ids = [c.strip() for c in campaign_ids.split(",")] if campaign_ids else []
    parsed_outdirs = [o.strip() for o in outdirs.split(",")] if outdirs else []

    if parsed_ids or parsed_outdirs:
        resolved = _resolve_campaigns(request, parsed_ids, parsed_outdirs)
    else:
        resolved = _scan_all_campaigns(campaigns_base)

    if not resolved:
        return Response(
            content="",
            media_type="text/csv" if format == "csv" else "application/json",
            headers={"Content-Disposition": f'attachment; filename="results_export.{format}"'},
        )

    all_rows: list[dict[str, Any]] = []
    all_columns: set[str] = set()

    # Parse filter expressions
    filter_exprs = [f.strip() for f in filter_expr.split(";")] if filter_expr else []

    for campaign_id, campaign_dir in resolved:
        cols, rows = _load_campaign_rows(campaign_id, campaign_dir, include_failed=include_failed)
        all_columns.update(cols)
        all_rows.extend(rows)

    # Apply filters
    for expr in filter_exprs:
        filtered: list[dict[str, Any]] = []
        for row in all_rows:
            temp_df = pd.DataFrame([row])
            try:
                result = _apply_filter(temp_df, expr)
                if not result.empty:
                    filtered.append(row)
            except HTTPException:
                continue
        all_rows = filtered

    # Build column order
    fixed_cols = ["campaign_id", "sample_id", "status", "elapsed_s", "cost_usd", "error_summary"]
    dynamic_cols = sorted(c for c in all_columns if c not in fixed_cols)
    ordered_cols = [c for c in fixed_cols if c in all_columns] + dynamic_cols

    if format == "json":
        output_rows = [{k: v for k, v in row.items() if k in ordered_cols} for row in all_rows]
        content = json.dumps(output_rows, indent=2)
        return Response(
            content=content,
            media_type="application/json",
            headers={"Content-Disposition": 'attachment; filename="results_export.json"'},
        )
    else:
        # CSV
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=ordered_cols, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(all_rows)
        content = output.getvalue()
        return Response(
            content=content,
            media_type="text/csv",
            headers={"Content-Disposition": 'attachment; filename="results_export.csv"'},
        )


# ---------------------------------------------------------------------------
# CLI helper functions (used by __main__.py)
# ---------------------------------------------------------------------------


def query_results_cli(
    campaign_ids: list[str] | None = None,
    outdirs: list[str] | None = None,
    filter_expr: str | None = None,
    page: int = 1,
    per_page: int = 100,
    format: str = "table",
) -> dict[str, Any]:
    """CLI helper for querying results.

    Returns a dict with ``rows``, ``total``, ``columns``, ``campaigns_queried``.
    """
    campaigns_base = Path.cwd()
    resolved = _resolve_campaigns_cli(campaign_ids, outdirs, campaigns_base)

    if not resolved:
        return {"rows": [], "total": 0, "columns": [], "campaigns_queried": 0}

    all_rows: list[dict[str, Any]] = []
    all_columns: set[str] = set()

    filter_exprs = [f.strip() for f in filter_expr.split(";")] if filter_expr else []

    for campaign_id, campaign_dir in resolved:
        cols, rows = _load_campaign_rows(campaign_id, campaign_dir, include_failed=True)
        all_columns.update(cols)
        all_rows.extend(rows)

    # Apply filters
    all_rows = _apply_filters(all_rows, filter_exprs)

    total = len(all_rows)
    start = (page - 1) * per_page
    end = start + per_page
    page_rows = all_rows[start:end]

    fixed_cols = ["campaign_id", "sample_id", "status", "elapsed_s", "cost_usd", "error_summary"]
    dynamic_cols = sorted(c for c in all_columns if c not in fixed_cols)
    ordered_cols = [c for c in fixed_cols if c in all_columns] + dynamic_cols

    return {
        "rows": [{k: v for k, v in row.items() if k in ordered_cols} for row in page_rows],
        "total": total,
        "columns": [c for c in ordered_cols if c in all_columns],
        "campaigns_queried": len(resolved),
    }


def _resolve_campaigns_from_paths(
    campaign_ids: list[str],
    outdirs: list[str],
    base: Path,
) -> list[tuple[str, Path]]:
    """Resolve campaign IDs and outdir paths to (label, directory) pairs for CLI use."""
    results: list[tuple[str, Path]] = []
    seen: set[str] = set()

    for od in outdirs:
        path = Path(od)
        if not path.is_absolute():
            path = base / path
        if path.is_dir():
            label = path.name
            if label not in seen:
                results.append((label, path))
                seen.add(label)

    for cid in campaign_ids:
        if cid in seen:
            continue
        candidate = base / cid
        if candidate.is_dir():
            results.append((cid, candidate))
            seen.add(cid)

    return results


def _resolve_campaigns_cli(
    campaign_ids: list[str] | None,
    outdirs: list[str] | None,
    base: Path,
) -> list[tuple[str, Path]]:
    """Resolve campaigns for CLI use: handles both IDs and outdir paths."""
    parsed_outdirs: list[str] = []
    if outdirs:
        for od in outdirs:
            path = Path(od).resolve()
            if path.is_dir():
                parsed_outdirs.append(str(path))
    if campaign_ids or parsed_outdirs:
        return _resolve_campaigns_from_paths(campaign_ids or [], parsed_outdirs, base)
    return _scan_all_campaigns(base)


def _apply_filters(
    rows: list[dict[str, Any]],
    filter_exprs: list[str],
) -> list[dict[str, Any]]:
    """Apply filter expressions to rows, returning filtered list."""
    for expr in filter_exprs:
        filtered: list[dict[str, Any]] = []
        for row in rows:
            temp_df = pd.DataFrame([row])
            try:
                result = _apply_filter(temp_df, expr)
                if not result.empty:
                    filtered.append(row)
            except HTTPException:
                continue
        rows = filtered
    return rows


def export_results_cli(
    campaign_ids: list[str] | None = None,
    outdirs: list[str] | None = None,
    filter_expr: str | None = None,
    format: str = "csv",
    output_path: Path | None = None,
    include_failed: bool = True,
) -> int:
    """CLI helper for exporting results.

    Writes to output_path if provided, otherwise prints to stdout.
    Returns 0 on success, 1 on error.
    """
    campaigns_base = Path.cwd()
    resolved = _resolve_campaigns_cli(campaign_ids, outdirs, campaigns_base)

    if not resolved:
        content = "" if format == "csv" else "[]"
        if output_path:
            output_path.write_text(content)
        return 0

    all_rows: list[dict[str, Any]] = []
    all_columns: set[str] = set()

    filter_exprs = [f.strip() for f in filter_expr.split(";")] if filter_expr else []

    for campaign_id, campaign_dir in resolved:
        cols, rows = _load_campaign_rows(campaign_id, campaign_dir, include_failed=include_failed)
        all_columns.update(cols)
        all_rows.extend(rows)

    # Apply filters
    for expr in filter_exprs:
        filtered = _apply_filters(all_rows, [expr])
        all_rows = filtered

    fixed_cols = ["campaign_id", "sample_id", "status", "elapsed_s", "cost_usd", "error_summary"]
    dynamic_cols = sorted(c for c in all_columns if c not in fixed_cols)
    ordered_cols = [c for c in fixed_cols if c in all_columns] + dynamic_cols

    if format == "json":
        output_rows = [{k: v for k, v in row.items() if k in ordered_cols} for row in all_rows]
        content = json.dumps(output_rows, indent=2)
    else:
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=ordered_cols, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(all_rows)
        content = output.getvalue()

    if output_path:
        output_path.write_text(content)

    return 0
