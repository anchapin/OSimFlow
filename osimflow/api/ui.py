"""Web UI for campaign setup and results visualization (issue #337).

Provides:
  - GET  /ui/                   — campaign setup page
  - GET  /ui/designer/           — visual variable designer (issue #381)
  - GET  /ui/api/campaigns       — list all campaigns with their status
  - POST /ui/api/setup           — create and start a new campaign
  - GET  /ui/api/campaigns/<id>  — get campaign status / run.json
  - GET  /ui/api/campaigns/<id>/results — get aggregated_results.csv as JSON
  - POST /ui/api/campaigns/<id>/stop    — stop a running campaign
"""

from __future__ import annotations

import contextlib
import json
import logging
import threading
import uuid
from pathlib import Path
from typing import Any

import pandas as pd
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from osimflow.campaign import Campaign
from osimflow.config import CampaignConfig
from osimflow.executors import LocalExecutor

log = logging.getLogger("osimflow.api.ui")

ui_router = APIRouter(prefix="/ui", tags=["ui"])

_UI_CAMPAIGNS: dict[str, dict[str, Any]] = {}
_CAMPAIGNS_LOCK = threading.Lock()


def _campaign_to_json(campaign_id: str, data: dict[str, Any]) -> dict[str, Any]:
    """Return a serialisable dict for a campaign."""
    return {
        "campaign_id": campaign_id,
        "status": data.get("status", "unknown"),
        "config_summary": data.get("config_summary", {}),
        "started_at": data.get("started_at"),
        "finished_at": data.get("finished_at"),
        "elapsed_s": data.get("elapsed_s"),
        "n_samples": data.get("config_summary", {}).get("n_samples"),
        "executor": data.get("config_summary", {}).get("executor"),
        "outdir": data.get("outdir"),
    }


def _read_run_json(outdir: Path) -> dict[str, Any]:
    """Read run.json safely, returning empty dict if absent."""
    path = outdir / "run.json"
    if not path.exists():
        return {}
    try:
        data: dict[str, Any] = json.loads(path.read_text())
        return data
    except (json.JSONDecodeError, OSError):
        return {}


def _read_aggregated_csv(outdir: Path) -> list[dict[str, Any]]:
    """Read aggregated_results.csv safely, returning empty list if absent."""
    csv_path = outdir / "aggregated_results.csv"
    if not csv_path.exists():
        return []
    try:
        df = pd.read_csv(csv_path)
        records: list[dict[str, Any]] = json.loads(df.to_json(orient="records"))
        return records
    except Exception:
        return []


# ---------------------------------------------------------------------------
# GET /ui/  — serve the campaign setup page
# ---------------------------------------------------------------------------


@ui_router.get("/", response_class=HTMLResponse)
async def get_ui_index() -> HTMLResponse:
    """Serve the OSimFlow campaign setup web UI."""
    html_path = Path(__file__).parent / "templates" / "index.html"
    if html_path.exists():
        return HTMLResponse(content=html_path.read_text(), status_code=200)
    raise HTTPException(status_code=404, detail="UI not found")


# ---------------------------------------------------------------------------
# GET /ui/designer/  — serve the variable designer page (issue #381)
# ---------------------------------------------------------------------------


@ui_router.get("/designer/", response_class=HTMLResponse)
async def get_variable_designer() -> HTMLResponse:
    """Serve the OSimFlow visual variable designer page (issue #381)."""
    html_path = Path(__file__).parent / "templates" / "variable_designer.html"
    if html_path.exists():
        return HTMLResponse(content=html_path.read_text(), status_code=200)
    raise HTTPException(status_code=404, detail="Variable designer not found")


# ---------------------------------------------------------------------------
# GET /ui/api/campaigns — list all campaigns
# ---------------------------------------------------------------------------


@ui_router.get("/api/campaigns")
async def list_campaigns() -> list[dict[str, Any]]:
    """List all campaigns known to the UI layer."""
    result: list[dict[str, Any]] = []
    with _CAMPAIGNS_LOCK:
        for cid, data in _UI_CAMPAIGNS.items():
            outdir = data.get("outdir")
            if outdir is not None:
                run_data = _read_run_json(Path(outdir))
                result.append(_campaign_to_json(cid, {**data, **run_data}))
            else:
                result.append(_campaign_to_json(cid, data))
    return result


# ---------------------------------------------------------------------------
# POST /ui/api/setup — create and start a new campaign
# ---------------------------------------------------------------------------


async def _run_campaign_in_thread(cfg: CampaignConfig, campaign_id: str) -> None:
    """Run a campaign in a background thread and update state on completion."""
    try:
        executor = LocalExecutor(max_workers=cfg.n_samples)
        campaign = Campaign(cfg, executor, max_workers=cfg.n_samples)
        _ = campaign.run()
        status = "success"
    except Exception as exc:
        log.error("campaign %s failed: %s", campaign_id, exc)
        status = "failure"
    finally:
        with contextlib.suppress(Exception):
            executor.shutdown()

    with _CAMPAIGNS_LOCK:
        if campaign_id in _UI_CAMPAIGNS:
            _UI_CAMPAIGNS[campaign_id]["status"] = status
            _UI_CAMPAIGNS[campaign_id]["finished_at"] = _CAMPAIGNS_LOCK  # placeholder


@ui_router.post("/api/setup")
async def setup_campaign(request: Request) -> JSONResponse:
    """Create and start a new campaign from UI-provided config.

    Accepts JSON body:
    {
        "n_samples": int,
        "executor": str,
        "openstudio_version": str,
        "algorithm": str,
        "input_variables_yaml": str,
        "template_sim_package": str,
        "outdir": str,
    }

    Returns:
        {"campaign_id": str, "status": "running", "outdir": str}
    """
    body = await request.json()

    n_samples = int(body.get("n_samples", 10))
    executor_type = str(body.get("executor", "local"))
    openstudio_version = str(body.get("openstudio_version", "3.11.0"))
    algorithm = str(body.get("algorithm", "lhs"))
    input_variables_yaml = str(body.get("input_variables_yaml", ""))
    template_sim_package = str(body.get("template_sim_package", ""))
    outdir = str(body.get("outdir", ""))

    if not input_variables_yaml:
        raise HTTPException(status_code=400, detail="input_variables_yaml is required")
    if not template_sim_package:
        raise HTTPException(status_code=400, detail="template_sim_package is required")
    if not outdir:
        raise HTTPException(status_code=400, detail="outdir is required")

    campaign_id = str(uuid.uuid4())[:8]
    outdir_path = Path(outdir).resolve()
    outdir_path.mkdir(parents=True, exist_ok=True)

    variables_path = outdir_path / "variables.yml"
    variables_path.write_text(input_variables_yaml)

    cfg = CampaignConfig(
        input_variables=variables_path,
        template_sim_package=Path(template_sim_package),
        n_samples=n_samples,
        outdir=outdir_path,
        openstudio_version=openstudio_version,
        algorithm=algorithm,
    )

    with _CAMPAIGNS_LOCK:
        _UI_CAMPAIGNS[campaign_id] = {
            "status": "running",
            "outdir": str(outdir_path),
            "config_summary": {
                "executor": executor_type,
                "openstudio_version": openstudio_version,
                "n_samples": n_samples,
                "algorithm": algorithm,
            },
            "started_at": None,
            "finished_at": None,
        }

    thread = threading.Thread(target=_run_campaign_in_thread, args=(cfg, campaign_id), daemon=True)
    thread.start()

    return JSONResponse(
        content={
            "campaign_id": campaign_id,
            "status": "running",
            "outdir": str(outdir_path),
        }
    )


# ---------------------------------------------------------------------------
# GET /ui/api/campaigns/<id> — get campaign status
# ---------------------------------------------------------------------------


@ui_router.get("/api/campaigns/{campaign_id}")
async def get_campaign_status(campaign_id: str) -> JSONResponse:
    """Get the current status of a campaign."""
    with _CAMPAIGNS_LOCK:
        if campaign_id not in _UI_CAMPAIGNS:
            raise HTTPException(status_code=404, detail=f"Campaign {campaign_id} not found")
        data = dict(_UI_CAMPAIGNS[campaign_id])

    outdir = data.get("outdir")
    run_data: dict[str, Any] = {}
    if outdir is not None:
        run_data = _read_run_json(Path(outdir))

    return JSONResponse(content=_campaign_to_json(campaign_id, {**data, **run_data}))


# ---------------------------------------------------------------------------
# GET /ui/api/campaigns/<id>/results — get aggregated results
# ---------------------------------------------------------------------------


@ui_router.get("/api/campaigns/{campaign_id}/results")
async def get_campaign_results(campaign_id: str) -> JSONResponse:
    """Get the aggregated results CSV as a JSON array."""
    with _CAMPAIGNS_LOCK:
        if campaign_id not in _UI_CAMPAIGNS:
            raise HTTPException(status_code=404, detail=f"Campaign {campaign_id} not found")
        data = dict(_UI_CAMPAIGNS[campaign_id])

    outdir = data.get("outdir")
    if outdir is None:
        raise HTTPException(status_code=404, detail="Campaign outdir not available")

    results = _read_aggregated_csv(Path(outdir))
    return JSONResponse(content={"campaign_id": campaign_id, "results": results})


# ---------------------------------------------------------------------------
# POST /ui/api/campaigns/<id>/stop — stop a running campaign
# ---------------------------------------------------------------------------


@ui_router.post("/api/campaigns/{campaign_id}/stop")
async def stop_campaign(campaign_id: str) -> JSONResponse:
    """Request graceful cancellation of a running campaign."""
    with _CAMPAIGNS_LOCK:
        if campaign_id not in _UI_CAMPAIGNS:
            raise HTTPException(status_code=404, detail=f"Campaign {campaign_id} not found")
        data = _UI_CAMPAIGNS[campaign_id]

    if data.get("status") != "running":
        raise HTTPException(status_code=400, detail="Campaign is not running")

    outdir = data.get("outdir")
    if outdir is None:
        raise HTTPException(status_code=404, detail="Campaign outdir not available")

    stop_file = Path(outdir) / ".stop"
    stop_file.write_text(json.dumps({"requested_at": 0}))  # placeholder

    data["status"] = "stopping"
    return JSONResponse(content={"campaign_id": campaign_id, "status": "stopping"})
