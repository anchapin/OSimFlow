"""FastAPI application for OSimFlow campaign monitoring."""

import json
import logging
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException

log = logging.getLogger("osimflow.api")


def create_app(outdir: Path | None = None, read_only: bool = True) -> FastAPI:
    """Create the FastAPI application.

    Parameters
    ----------
    outdir
        Path to the campaign output directory containing run.json.
    read_only
        If True, only GET endpoints are available (no campaign control).
    """
    app = FastAPI(
        title="OSimFlow API",
        version="0.1.0",
        description="REST API for monitoring OSimFlow campaigns",
    )

    app.state.outdir = outdir
    app.state.read_only = read_only

    def _load_run_json() -> dict[str, Any]:
        """Load and return run.json from outdir."""
        if app.state.outdir is None:
            raise HTTPException(status_code=503, detail="No output directory configured")
        run_json_path: Path = app.state.outdir / "run.json"
        if not run_json_path.exists():
            raise HTTPException(
                status_code=404,
                detail="run.json not found — campaign may not have started",
            )
        raw: Any = json.loads(run_json_path.read_text())
        return raw  # type: ignore[no-any-return]

    @app.get("/health")  # type: ignore[misc]
    async def health() -> dict[str, str]:
        """Liveness probe."""
        return {"status": "alive"}

    @app.get("/ready")  # type: ignore[misc]
    async def ready() -> dict[str, Any]:
        """Readiness probe — checks if run.json is accessible."""
        try:
            data = _load_run_json()
            return {"status": "ready", "campaign_id": data.get("campaign_id")}
        except HTTPException:
            return {"status": "not_ready", "reason": "run.json not available"}

    @app.get("/api/v1/campaign")  # type: ignore[misc]
    async def get_campaign() -> dict[str, Any]:
        """Get campaign metadata from run.json."""
        data = _load_run_json()
        return {
            "campaign_id": data.get("campaign_id"),
            "config_summary": data.get("config_summary", {}),
            "started_at": data.get("started_at"),
            "finished_at": data.get("finished_at"),
            "baseline_comparison": data.get("baseline_comparison"),
        }

    @app.get("/api/v1/steps")  # type: ignore[misc]
    async def get_steps() -> dict[str, Any]:
        """Get step traces from run.json."""
        data = _load_run_json()
        return {
            "steps": data.get("steps", []),
            "total_steps": len(data.get("steps", [])),
        }

    return app
