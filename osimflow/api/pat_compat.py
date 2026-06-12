"""PAT (Parametric Analysis Tool) compatibility API shim layer (issue #265).

Provides PAT-compatible REST endpoints that translate PAT's API surface
into OSimFlow campaign operations.  This allows PAT users to transition
to OSimFlow without rewriting their automation tooling.

PAT API concepts mapped to OSimFlow:

    PAT analysis       → OSimFlow campaign
    PAT data point     → OSimFlow sample
    PAT status         → OSimFlow campaign/sample status

Endpoints
---------

``POST /api/v1/pat/analyses``
    Create a new analysis (campaign) from a PAT-style JSON payload.
    Accepts both the raw PAT ``analysis.json`` format and a simplified
    ``{osa_path, template_sim_package, n_samples}`` body that triggers
    OSA import internally.

``GET /api/v1/pat/analyses/{analysis_id}/status``
    PAT-style status polling.  Returns analysis-level status with
    per-data-point progress counts.

``GET /api/v1/pat/analyses/{analysis_id}/data_points``
    PAT-style data point listing.  Returns all samples (data points)
    for a given analysis.

All endpoints are available in **read-only** mode for status/data_point
queries; the POST creation endpoint requires ``--enable-writes``.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from osimflow.api.campaigns import (
    _campaign_dir_from_id,
    _campaigns_base_dir,
    _derive_status,
    _load_campaign_json,
)

log = logging.getLogger("osimflow.api.pat_compat")

pat_compat_router = APIRouter(prefix="/api/v1/pat", tags=["PAT Compatibility"])


# ---------------------------------------------------------------------------
# Pydantic models (PAT-style request/response)
# ---------------------------------------------------------------------------


class PATAnalysisRequest(BaseModel):
    """PAT-style analysis creation request.

    Supports two modes:

    1. **OSA import mode**: provide ``osa_path`` and
       ``template_sim_package``.  The OSA file is parsed and converted
       to a ``variables.yml`` in the campaign output directory.

    2. **Inline mode**: provide ``analysis`` (the raw OSA analysis JSON
       object) and ``template_sim_package``.  The analysis is converted
       inline without reading a file from disk.
    """

    osa_path: str | None = Field(
        default=None,
        description="Path to a .osa or analysis.json file to import",
    )
    analysis: dict[str, Any] | None = Field(
        default=None,
        description="Inline OSA analysis JSON object (alternative to osa_path)",
    )
    template_sim_package: str = Field(
        description="Path to the template simulation package directory",
    )
    n_samples: int = Field(
        default=10,
        ge=1,
        description="Number of samples (overrides OSA value if provided)",
    )
    openstudio_version: str = Field(
        default="3.11.0",
        description="OpenStudio CLI version for container tag",
    )
    outdir: str | None = Field(
        default=None,
        description="Output directory (auto-generated if omitted)",
    )
    auto_start: bool = Field(
        default=False,
        description="Launch the campaign immediately after creation",
    )


class PATAnalysisResponse(BaseModel):
    """Response for PAT-style analysis creation."""

    analysis_id: str
    status: str = Field(description="created | running")
    osimflow_campaign_id: str = Field(
        description="The underlying OSimFlow campaign ID",
    )
    outdir: str


class PATStatusResponse(BaseModel):
    """PAT-style status response."""

    analysis_id: str
    status: str = Field(description="not_started | running | completed | unknown")
    started_at: float | None = None
    finished_at: float | None = None
    elapsed_s: float | None = None
    data_points: dict[str, int] = Field(
        description="Counts: total, completed, failed, pending",
    )


class PATDataPointResponse(BaseModel):
    """PAT-style data point listing."""

    analysis_id: str
    data_points: list[dict[str, Any]]
    total: int


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _import_osa_to_campaign(
    osa_path: Path,
    campaign_outdir: Path,
) -> Path:
    """Import an OSA file and write variables.yml to the campaign outdir.

    Returns the path to the generated ``variables.yml``.
    """
    from osimflow.importers.osa import osa_to_variables_yml, parse_osa  # noqa: PLC0415

    osa_data = parse_osa(osa_path)
    variables_yml_path = campaign_outdir / "variables.yml"
    osa_to_variables_yml(osa_data, variables_yml_path)
    return variables_yml_path


def _import_inline_analysis(
    analysis_data: dict[str, Any],
    campaign_outdir: Path,
) -> Path:
    """Convert an inline OSA analysis dict and write variables.yml.

    Returns the path to the generated ``variables.yml``.
    """
    from osimflow.importers.osa import osa_to_variables_yml  # noqa: PLC0415

    variables_yml_path = campaign_outdir / "variables.yml"
    osa_to_variables_yml(analysis_data, variables_yml_path)
    return variables_yml_path


def _pat_status(osimflow_status: str) -> str:
    """Map OSimFlow campaign status to PAT-style status string."""
    mapping: dict[str, str] = {
        "completed": "completed",
        "running": "running",
        "unknown": "not_started",
    }
    return mapping.get(osimflow_status, "unknown")


def _count_data_points(data: dict[str, Any]) -> dict[str, int]:
    """Derive data-point counts from run.json per_sample data."""
    per_sample: list[dict[str, Any]] = data.get("per_sample", [])
    total = len(per_sample)
    completed = 0
    failed = 0
    for s in per_sample:
        status = s.get("status", "")
        if status in ("ok", "cached"):
            completed += 1
        elif status == "failed":
            failed += 1
    pending = total - completed - failed
    return {
        "total": total,
        "completed": completed,
        "failed": failed,
        "pending": pending,
    }


# ---------------------------------------------------------------------------
# POST /api/v1/pat/analyses
# ---------------------------------------------------------------------------


@pat_compat_router.post(
    "/analyses",
    response_model=PATAnalysisResponse,
    status_code=201,
)
async def create_pat_analysis(
    body: PATAnalysisRequest,
    request: Request,
) -> PATAnalysisResponse:
    """Create a PAT-style analysis (maps to an OSimFlow campaign).

    Accepts an OSA file path or inline analysis JSON, converts it to
    OSimFlow's ``variables.yml`` format, and optionally starts the
    campaign.
    """
    if getattr(request.app.state, "read_only", True):
        raise HTTPException(
            status_code=403,
            detail="Analysis creation requires --enable-writes mode",
        )

    if body.osa_path is None and body.analysis is None:
        raise HTTPException(
            status_code=422,
            detail="Provide either 'osa_path' or 'analysis' field",
        )

    base = _campaigns_base_dir(request)
    base.mkdir(parents=True, exist_ok=True)

    # Generate campaign ID
    campaign_id = f"pat-{uuid.uuid4().hex[:8]}"
    outdir = Path(body.outdir).resolve() if body.outdir is not None else base / campaign_id
    outdir.mkdir(parents=True, exist_ok=True)

    # Validate template_sim_package
    tsp = Path(body.template_sim_package).resolve()
    if not tsp.is_dir():
        raise HTTPException(
            status_code=422,
            detail=f"template_sim_package not found or not a directory: {tsp}",
        )

    # Import OSA → variables.yml
    variables_yml_path: Path
    try:
        if body.osa_path is not None:
            osa_path = Path(body.osa_path).resolve()
            if not osa_path.exists():
                raise HTTPException(
                    status_code=422,
                    detail=f"OSA file not found: {osa_path}",
                )
            variables_yml_path = _import_osa_to_campaign(osa_path, outdir)
        else:
            variables_yml_path = _import_inline_analysis(
                body.analysis,  # type: ignore[arg-type]
                outdir,
            )
    except Exception as exc:
        if isinstance(exc, HTTPException):
            raise
        raise HTTPException(
            status_code=422,
            detail=f"OSA import failed: {exc}",
        ) from exc

    log.info(
        "PAT analysis %s: imported variables.yml to %s",
        campaign_id,
        variables_yml_path,
    )

    # Write campaign config stub
    config_stub: dict[str, Any] = {
        "campaign_id": campaign_id,
        "input_variables": str(variables_yml_path),
        "template_sim_package": str(tsp),
        "n_samples": body.n_samples,
        "openstudio_version": body.openstudio_version,
        "executor": "local",
        "algorithm": "lhs",
        "source": "pat_compat",
    }
    config_path = outdir / "campaign_config.json"
    config_path.write_text(json.dumps(config_stub, indent=2))

    status = "created"

    if body.auto_start:
        # Write initial run.json for discoverability
        initial_run: dict[str, Any] = {
            "schema_version": 1,
            "campaign_id": campaign_id,
            "started_at": time.time(),
            "finished_at": None,
            "config_summary": config_stub,
            "summary": {"n_samples": body.n_samples, "n_succeeded": 0, "n_failed": 0},
            "steps": [],
            "per_sample": [],
        }
        (outdir / "run.json").write_text(json.dumps(initial_run, indent=2))

        # Launch in background
        _launch_pat_campaign(request, campaign_id, body, outdir, variables_yml_path, tsp)
        status = "running"

    return PATAnalysisResponse(
        analysis_id=campaign_id,
        status=status,
        osimflow_campaign_id=campaign_id,
        outdir=str(outdir),
    )


def _launch_pat_campaign(
    request: Request,
    campaign_id: str,
    body: PATAnalysisRequest,
    outdir: Path,
    variables_yml_path: Path,
    tsp: Path,
) -> None:
    """Launch a PAT-created campaign in a background thread."""
    import threading  # noqa: PLC0415

    def _run() -> None:
        try:
            from osimflow import Campaign, LocalExecutor  # noqa: PLC0415
            from osimflow.config import CampaignConfig  # noqa: PLC0415

            cfg = CampaignConfig(
                input_variables=variables_yml_path,
                template_sim_package=tsp,
                n_samples=body.n_samples,
                outdir=outdir,
                openstudio_version=body.openstudio_version,
            )
            executor = LocalExecutor(max_workers=1)
            campaign = Campaign(cfg, executor=executor)
            campaign.run()
        except Exception:
            log.exception("PAT background campaign %s failed", campaign_id)

    thread = threading.Thread(target=_run, daemon=True, name=f"pat-{campaign_id}")
    thread.start()
    log.info("launched PAT campaign %s in background thread", campaign_id)


# ---------------------------------------------------------------------------
# GET /api/v1/pat/analyses/{analysis_id}/status
# ---------------------------------------------------------------------------


@pat_compat_router.get(
    "/analyses/{analysis_id}/status",
    response_model=PATStatusResponse,
)
async def get_pat_analysis_status(
    analysis_id: str,
    request: Request,
) -> PATStatusResponse:
    """Get PAT-style analysis status with data-point counts."""
    base = _campaigns_base_dir(request)
    campaign_dir = _campaign_dir_from_id(base, analysis_id)
    data = _load_campaign_json(campaign_dir)

    osimflow_status = _derive_status(data)
    pat_status = _pat_status(osimflow_status)
    dp_counts = _count_data_points(data)

    return PATStatusResponse(
        analysis_id=analysis_id,
        status=pat_status,
        started_at=data.get("started_at"),
        finished_at=data.get("finished_at"),
        elapsed_s=data.get("elapsed_s"),
        data_points=dp_counts,
    )


# ---------------------------------------------------------------------------
# GET /api/v1/pat/analyses/{analysis_id}/data_points
# ---------------------------------------------------------------------------


@pat_compat_router.get(
    "/analyses/{analysis_id}/data_points",
    response_model=PATDataPointResponse,
)
async def get_pat_data_points(
    analysis_id: str,
    request: Request,
) -> PATDataPointResponse:
    """List PAT-style data points (samples) for an analysis."""
    base = _campaigns_base_dir(request)
    campaign_dir = _campaign_dir_from_id(base, analysis_id)
    data = _load_campaign_json(campaign_dir)

    per_sample: list[dict[str, Any]] = data.get("per_sample", [])
    # Map OSimFlow sample fields to PAT data_point conventions
    data_points: list[dict[str, Any]] = []
    for s in per_sample:
        dp: dict[str, Any] = {
            "data_point_id": s.get("sample_id", ""),
            "status": s.get("status", "unknown"),
            "elapsed_s": s.get("elapsed_s", 0.0),
        }
        # Include KPIs if available
        kpis = s.get("kpis")
        if kpis is not None:
            dp["results"] = kpis
        # Include error summary for failed points
        error = s.get("error_summary")
        if error is not None:
            dp["error_summary"] = error
        data_points.append(dp)

    return PATDataPointResponse(
        analysis_id=analysis_id,
        data_points=data_points,
        total=len(data_points),
    )
