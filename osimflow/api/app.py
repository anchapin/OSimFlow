"""FastAPI application for OSimFlow campaign monitoring."""

import json
import logging
from pathlib import Path
from typing import Any

import pandas as pd
from fastapi import FastAPI, HTTPException, Query

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

    @app.get("/health")  # type: ignore[untyped-decorator]
    async def health() -> dict[str, str]:
        """Liveness probe."""
        return {"status": "alive"}

    @app.get("/ready")  # type: ignore[untyped-decorator]
    async def ready() -> dict[str, Any]:
        """Readiness probe — checks if run.json is accessible."""
        try:
            data = _load_run_json()
            return {"status": "ready", "campaign_id": data.get("campaign_id")}
        except HTTPException:
            return {"status": "not_ready", "reason": "run.json not available"}

    @app.get("/api/v1/campaign")  # type: ignore[untyped-decorator]
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

    @app.get("/api/v1/steps")  # type: ignore[untyped-decorator]
    async def get_steps() -> dict[str, Any]:
        """Get step traces from run.json."""
        data = _load_run_json()
        return {
            "steps": data.get("steps", []),
            "total_steps": len(data.get("steps", [])),
        }

    # ------------------------------------------------------------------
    # Sample + results + failures + pareto endpoints (issue #147)
    # ------------------------------------------------------------------

    @app.get("/api/v1/samples")  # type: ignore[untyped-decorator]
    async def get_samples(
        page: int = Query(1, ge=1, description="Page number (1-indexed)"),
        per_page: int = Query(50, ge=1, le=500, description="Items per page (max 500)"),
    ) -> dict[str, Any]:
        """Get paginated per-sample traces from run.json."""
        data = _load_run_json()
        all_samples: list[dict[str, Any]] = data.get("per_sample", [])
        total = len(all_samples)
        start = (page - 1) * per_page
        end = start + per_page
        page_samples = all_samples[start:end]
        return {
            "samples": page_samples,
            "total": total,
            "page": page,
            "per_page": per_page,
        }

    @app.get("/api/v1/samples/{sid}")  # type: ignore[untyped-decorator]
    async def get_sample_detail(sid: str) -> dict[str, Any]:
        """Get detail for a single sample, including KPIs and log files."""
        data = _load_run_json()
        all_samples: list[dict[str, Any]] = data.get("per_sample", [])
        sample: dict[str, Any] | None = None
        for s in all_samples:
            if s.get("sample_id") == sid:
                sample = s
                break
        if sample is None:
            raise HTTPException(status_code=404, detail=f"Sample '{sid}' not found")

        # Attempt to load KPI JSON from outdir/work/sim/{sid}/
        kpis: dict[str, Any] | None = None
        log_files: dict[str, str] = {}
        if app.state.outdir is not None:
            sim_dir = app.state.outdir / "work" / "sim" / sid
            # Look for kpi JSON files (kpi.json or similar)
            for kpi_name in ("kpi.json", "kpis.json"):
                kpi_path = sim_dir / kpi_name
                if kpi_path.exists():
                    kpis = json.loads(kpi_path.read_text())
                    break
            # Collect log file paths
            for log_name in ("stdout.log", "stderr.log"):
                log_path = sim_dir / log_name
                if log_path.exists():
                    log_files[log_name] = str(log_path)

        return {
            "sample_id": sid,
            "kpis": kpis,
            "log_files": log_files,
            **sample,
        }

    @app.get("/api/v1/results")  # type: ignore[untyped-decorator]
    async def get_results() -> list[dict[str, Any]]:
        """Read aggregated_results.csv and return as JSON array."""
        if app.state.outdir is None:
            raise HTTPException(status_code=503, detail="No output directory configured")
        csv_path: Path = app.state.outdir / "aggregated_results.csv"
        if not csv_path.exists():
            raise HTTPException(
                status_code=404,
                detail="aggregated_results.csv not found",
            )
        df = pd.read_csv(csv_path)
        # NaN → None for clean JSON
        return json.loads(df.to_json(orient="records"))

    @app.get("/api/v1/failures")  # type: ignore[untyped-decorator]
    async def get_failures() -> list[dict[str, Any]]:
        """Read failed_simulations.csv and return as JSON array."""
        if app.state.outdir is None:
            raise HTTPException(status_code=503, detail="No output directory configured")
        csv_path: Path = app.state.outdir / "failed_simulations.csv"
        if not csv_path.exists():
            raise HTTPException(
                status_code=404,
                detail="failed_simulations.csv not found",
            )
        df = pd.read_csv(csv_path)
        return json.loads(df.to_json(orient="records"))

    @app.get("/api/v1/pareto")  # type: ignore[untyped-decorator]
    async def get_pareto() -> dict[str, Any]:
        """Read pareto front data from outdir/pareto/gen_*.json files."""
        if app.state.outdir is None:
            raise HTTPException(status_code=503, detail="No output directory configured")
        pareto_dir: Path = app.state.outdir / "pareto"
        if not pareto_dir.exists():
            raise HTTPException(
                status_code=404,
                detail="No pareto data found",
            )
        gen_files = sorted(pareto_dir.glob("gen_*.json"))
        if not gen_files:
            raise HTTPException(
                status_code=404,
                detail="No pareto data found",
            )
        generations: list[dict[str, Any]] = []
        for gf in gen_files:
            gen_data = json.loads(gf.read_text())
            gen_data["_file"] = gf.name
            generations.append(gen_data)
        return {
            "generations": generations,
            "total_generations": len(generations),
        }

    return app
