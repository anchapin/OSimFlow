"""Results query and export API endpoints (issue #585).

Provides:
  - GET  /api/v1/campaigns/{campaign_id}/results/query  — query results with filters
  - GET  /api/v1/campaigns/{campaign_id}/results/export  — export results to CSV/JSON

The pure-Python helpers (CSV loader, MongoDB-style filter, and the
``osimflow query-results`` / ``osimflow export-results`` CLI entry
points) used to live in this module, but that forced every install to
pull :mod:`fastapi` at import time — breaking the ``query-results``
and ``export-results`` CLI subcommands on installs without the
optional ``[api]`` extra (issue #1699). They now live in
:mod:`osimflow.results_query` and this module imports them for use
inside the route handlers.
"""

from __future__ import annotations

import io
import json
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response

from osimflow.api.campaigns import (
    _campaign_dir_from_id,
    _campaigns_base_dir,
)
from osimflow.results_query import (
    apply_filter,
    load_aggregated_results,
)

log = logging.getLogger("osimflow.api.results_query")

results_query_router = APIRouter()

# Module-local aliases for the shared helpers. The canonical home is
# :mod:`osimflow.results_query`; these names are kept here as thin
# bindings so the route handlers below read naturally without
# importing private symbols from another module.
_apply_filter = apply_filter
_load_aggregated_results = load_aggregated_results


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
            headers={"Content-Disposition": f'attachment; filename="{campaign_id}_results.json"'},
        )

    # CSV format
    output = io.StringIO()
    df.to_csv(output, index=False)
    csv_content = output.getvalue()
    return Response(
        content=csv_content,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{campaign_id}_results.csv"'},
    )
