"""Real-time HTML dashboard for live campaign monitoring (issue #586).

Provides:
  - GET /dashboard              — real-time campaign status HTML page
  - GET /api/v1/dashboard/status — JSON snapshot for the dashboard

The HTML page connects to the existing SSE endpoint (``/api/v1/events``)
for live updates and polls ``/api/v1/health`` for the initial status.

Served at ``/dashboard`` when the FastAPI app is created with
``dashboard=True`` (set automatically when ``--dashboard`` is passed
on the CLI).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

log = logging.getLogger("osimflow.api.dashboard")

dashboard_router = APIRouter()


def _load_run_json(request: Request) -> dict[str, Any]:
    """Load and return run.json from outdir."""
    if request.app.state.outdir is None:
        raise HTTPException(status_code=503, detail="No output directory configured")
    run_json_path = request.app.state.outdir / "run.json"
    if not run_json_path.exists():
        raise HTTPException(
            status_code=404,
            detail="run.json not found — campaign may not have started",
        )
    raw: Any = json.loads(run_json_path.read_text())
    return raw  # type: ignore[no-any-return]


def _compute_campaign_status(data: dict[str, Any]) -> str:
    """Derive an overall health status from run.json data."""
    status = data.get("status", "unknown")
    per_sample = data.get("per_sample", [])
    n_failed = sum(1 for s in per_sample if s.get("status") == "failed")

    if status == "running":
        return "degraded" if n_failed > 0 else "healthy"
    if status in ("success", "completed"):
        if not per_sample:
            return "healthy"
        if n_failed == 0:
            return "healthy"
        return "degraded" if n_failed < len(per_sample) else "unhealthy"
    if status in ("failed", "cancelled"):
        return "unhealthy"
    return "unknown"


@dashboard_router.get("/dashboard")
async def get_dashboard_html() -> HTMLResponse:
    """Return the real-time HTML dashboard page."""
    html_path = Path(__file__).parent / "static" / "dashboard.html"
    return HTMLResponse(content=html_path.read_text(), status_code=200)


@dashboard_router.get("/api/v1/dashboard/status")
async def get_dashboard_status(request: Request) -> dict[str, Any]:
    """JSON status snapshot for the dashboard.

    Returns the same shape as ``GET /api/v1/health`` so existing
    clients can consume it without changes.
    """
    data = _load_run_json(request)

    per_sample = data.get("per_sample", [])
    n_total = len(per_sample)
    n_success = sum(1 for s in per_sample if s.get("status") == "ok")
    n_failed = sum(1 for s in per_sample if s.get("status") == "failed")
    n_cached = sum(1 for s in per_sample if s.get("status") == "cached")
    n_running = n_total - n_success - n_failed - n_cached

    steps: list[dict[str, Any]] = data.get("steps", [])
    step_statuses: list[dict[str, Any]] = [
        {
            "step": s.get("step", ""),
            "status": "ok" if s.get("exit_code", 1) == 0 else "failed",
            "elapsed_s": s.get("elapsed_s", 0.0),
            "cache": s.get("cache", ""),
        }
        for s in steps
    ]

    return {
        "campaign_id": data.get("campaign_id"),
        "overall_status": _compute_campaign_status(data),
        "campaign_status": data.get("status", "unknown"),
        "steps": step_statuses,
        "samples": {
            "total": n_total,
            "success": n_success,
            "failed": n_failed,
            "cached": n_cached,
            "running": n_running,
        },
        "started_at": data.get("started_at"),
        "finished_at": data.get("finished_at"),
        "elapsed_s": data.get("elapsed_s"),
    }
