"""Coordinator API for fire-and-forget campaign handoff (issue #602).

Provides endpoints for handing off a campaign to a cloud Coordinator service
so the user can disconnect their local machine while the campaign runs.

Phase 2 scope (issue #602):
  - Accept campaign handoff via POST /api/v1/coordinator/handoff
  - Store campaign metadata and return a campaign_id immediately
  - Provide GET /api/v1/coordinator/campaigns/{campaign_id} for status

Future phases (603, 604) will add:
  - Array job submission (Phase 3)
  - Result aggregation and user notifications (Phase 4)
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from osimflow.api.auth import get_user_permission
from osimflow.api.schemas import (
    CoordinatorCampaignRecord,
    CoordinatorHandoffPayload,
    CoordinatorHandoffResponse,
)

log = logging.getLogger("osimflow.api.coordinator")

coordinator_router = APIRouter(prefix="/api/v1/coordinator", tags=["coordinator"])

# In-memory store for Phase 2. Future phases will replace this with a
# proper database (e.g., DynamoDB on AWS, or the SQLite document store).
# This is intentionally minimal — just enough to pass the Phase 2 acceptance
# criteria: "CLI returns within seconds after uploading the config."
_campaigns: dict[str, dict[str, Any]] = {}


@coordinator_router.post(
    "/handoff",
    response_model=CoordinatorHandoffResponse,
    summary="Handoff a campaign to the Coordinator",
    description=(
        "Accepts a campaign configuration and returns immediately with a "
        "campaign_id. The Coordinator takes ownership of the campaign lifecycle. "
        "Use GET /api/v1/coordinator/campaigns/{campaign_id} to poll status."
    ),
)
async def coordinator_handoff(
    payload: CoordinatorHandoffPayload,
    request: Request,
) -> CoordinatorHandoffResponse:
    """Handle campaign handoff from the CLI.

    Validates the payload, assigns a campaign_id, stores the campaign
    metadata, and returns immediately. The CLI can exit after receiving
    the response.
    """
    if not get_user_permission(request, "write"):
        raise HTTPException(status_code=403, detail="Insufficient permissions for campaign handoff")

    campaign_id = str(uuid.uuid4())
    now = time.time()

    user_id: str = getattr(request.state, "user_id", None) or "anonymous"
    record: dict[str, Any] = {
        "campaign_id": campaign_id,
        "name": payload.name,
        "status": "pending",  # pending → running → aggregating → complete/failed
        "created_at": now,
        "updated_at": now,
        "created_by": user_id,
        "payload": payload.model_dump(exclude_none=True),
        "n_samples": payload.n_samples,
        "executor": payload.executor,
        "openstudio_version": payload.openstudio_version,
    }

    _campaigns[campaign_id] = record
    log.info("Campaign %s handed off: name=%s, n_samples=%d", campaign_id, payload.name, payload.n_samples)

    return CoordinatorHandoffResponse(
        campaign_id=campaign_id,
        status="pending",
        message=f"Campaign '{payload.name}' accepted. Use GET /api/v1/coordinator/campaigns/{campaign_id} to poll status.",
    )


@coordinator_router.get(
    "/campaigns",
    response_model=list[CoordinatorCampaignRecord],
    summary="List all Coordinator campaigns",
)
async def list_coordinator_campaigns(request: Request) -> list[CoordinatorCampaignRecord]:
    """Return all campaigns known to the Coordinator."""
    get_user_permission(request, "read")  # authenticate
    return [
        CoordinatorCampaignRecord(
            campaign_id=cid,
            name=rec.get("name", ""),
            status=rec.get("status", "unknown"),
            created_at=rec.get("created_at"),
            updated_at=rec.get("updated_at"),
            n_samples=rec.get("n_samples", 0),
            executor=rec.get("executor", ""),
            openstudio_version=rec.get("openstudio_version", ""),
        )
        for cid, rec in _campaigns.items()
    ]


@coordinator_router.get(
    "/campaigns/{campaign_id}",
    response_model=CoordinatorCampaignRecord,
    summary="Get Coordinator campaign status",
)
async def get_coordinator_campaign(
    campaign_id: str,
    request: Request,
) -> CoordinatorCampaignRecord:
    """Return the current status of a Coordinator campaign."""
    get_user_permission(request, "read")  # authenticate
    rec = _campaigns.get(campaign_id)
    if rec is None:
        raise HTTPException(status_code=404, detail=f"Campaign {campaign_id} not found")
    return CoordinatorCampaignRecord(
        campaign_id=campaign_id,
        name=rec.get("name", ""),
        status=rec.get("status", "unknown"),
        created_at=rec.get("created_at"),
        updated_at=rec.get("updated_at"),
        n_samples=rec.get("n_samples", 0),
        executor=rec.get("executor", ""),
        openstudio_version=rec.get("openstudio_version", ""),
    )


@coordinator_router.patch(
    "/campaigns/{campaign_id}/status",
    response_model=CoordinatorCampaignRecord,
    summary="Update Coordinator campaign status (internal)",
)
async def update_coordinator_campaign_status(
    campaign_id: str,
    status: str,
    request: Request,
) -> CoordinatorCampaignRecord:
    """Update the status of a Coordinator campaign.

    This endpoint is intended for internal use by the Coordinator's own
    worker processes (Phase 3/4). It is not exposed to the public API
    without authentication.
    """
    if not get_user_permission(request, "admin"):
        raise HTTPException(status_code=403, detail="Insufficient permissions")
    rec = _campaigns.get(campaign_id)
    if rec is None:
        raise HTTPException(status_code=404, detail=f"Campaign {campaign_id} not found")
    rec["status"] = status
    rec["updated_at"] = time.time()
    return CoordinatorCampaignRecord(
        campaign_id=campaign_id,
        name=rec.get("name", ""),
        status=rec.get("status", "unknown"),
        created_at=rec.get("created_at"),
        updated_at=rec.get("updated_at"),
        n_samples=rec.get("n_samples", 0),
        executor=rec.get("executor", ""),
        openstudio_version=rec.get("openstudio_version", ""),
    )
