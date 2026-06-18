"""Coordinator API for fire-and-forget campaign handoff (issue #602).

Provides endpoints for handing off a campaign to a cloud Coordinator service
so the user can disconnect their local machine while the campaign runs.

Phase 2 scope (issue #602):
  - Accept campaign handoff via POST /api/v1/coordinator/handoff
  - Store campaign metadata and return a campaign_id immediately
  - Provide GET /api/v1/coordinator/campaigns/{campaign_id} for status

Phase 3 scope (issue #603):
  - Store LHS sample parameter sets when campaign is handed off
  - GET /campaigns/{id}/samples/{index} — workers fetch their parameters
  - POST /campaigns/{id}/submit-array — submit an AWS Batch array job
    with one child per sample (1 API call for N samples)

Future phase (604) will add:
  - Result aggregation and user notifications
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from typing import Any

import boto3
from fastapi import APIRouter, HTTPException, Request

from osimflow.api.auth import get_user_permission
from osimflow.api.schemas import (
    CoordinatorArraySubmitRequest,
    CoordinatorArraySubmitResponse,
    CoordinatorCampaignRecord,
    CoordinatorHandoffPayload,
    CoordinatorHandoffResponse,
    CoordinatorSampleRecord,
    CoordinatorSamplesResponse,
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
    samples: list[dict[str, Any]] = payload.samples or []
    n_samples = len(samples) if samples else payload.n_samples

    record: dict[str, Any] = {
        "campaign_id": campaign_id,
        "name": payload.name,
        "status": "pending",  # pending → running → aggregating → complete/failed
        "created_at": now,
        "updated_at": now,
        "created_by": user_id,
        "payload": payload.model_dump(exclude_none=True),
        "n_samples": n_samples,
        "executor": payload.executor,
        "openstudio_version": payload.openstudio_version,
        "samples": samples,
    }

    _campaigns[campaign_id] = record
    log.info(
        "Campaign %s handed off: name=%s, n_samples=%d",
        campaign_id,
        payload.name,
        payload.n_samples,
    )

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


@coordinator_router.get(
    "/campaigns/{campaign_id}/samples",
    response_model=CoordinatorSamplesResponse,
    summary="List all sample parameter sets for a campaign",
)
async def list_campaign_samples(
    campaign_id: str,
    request: Request,
) -> CoordinatorSamplesResponse:
    """Return all sample parameter sets for a campaign.

    Used by the Coordinator to inspect all samples before submitting an array job,
    and by array job children to enumerate available sample indices.
    """
    get_user_permission(request, "read")  # authenticate
    rec = _campaigns.get(campaign_id)
    if rec is None:
        raise HTTPException(status_code=404, detail=f"Campaign {campaign_id} not found")
    samples = rec.get("samples", [])
    return CoordinatorSamplesResponse(
        campaign_id=campaign_id,
        samples=[
            CoordinatorSampleRecord(index=i, parameters=params, status="pending")
            for i, params in enumerate(samples)
        ],
    )


@coordinator_router.get(
    "/campaigns/{campaign_id}/samples/{index}",
    response_model=CoordinatorSampleRecord,
    summary="Get a specific sample's parameters by index",
)
async def get_campaign_sample(
    campaign_id: str,
    index: int,
    request: Request,
) -> CoordinatorSampleRecord:
    """Return the parameter set for a specific sample index.

    This is the endpoint that array job children call to retrieve their
    parameters. The child reads AWS_BATCH_JOB_ARRAY_INDEX from its environment
    and uses that as the {index}.

    Returns 404 if the campaign does not exist or the index is out of range.
    """
    get_user_permission(request, "read")  # authenticate
    rec = _campaigns.get(campaign_id)
    if rec is None:
        raise HTTPException(status_code=404, detail=f"Campaign {campaign_id} not found")
    samples = rec.get("samples", [])
    if index < 0 or index >= len(samples):
        raise HTTPException(
            status_code=404,
            detail=f"Sample index {index} out of range (0..{len(samples) - 1})",
        )
    return CoordinatorSampleRecord(
        index=index,
        parameters=samples[index],
        status=rec.get("status", "pending"),
    )


@coordinator_router.post(
    "/campaigns/{campaign_id}/submit-array",
    response_model=CoordinatorArraySubmitResponse,
    summary="Submit a campaign as an AWS Batch array job",
)
async def submit_campaign_array_job(
    campaign_id: str,
    submit_req: CoordinatorArraySubmitRequest,
    request: Request,
) -> CoordinatorArraySubmitResponse:
    """Submit a campaign as an AWS Batch array job.

    Constructs a single ``submit_job`` call with ``arrayProperties.size`` equal
    to the number of samples. Each array child will set
    ``AWS_BATCH_JOB_ARRAY_INDEX`` to its zero-based index, then call
    ``GET /campaigns/{campaign_id}/samples/{index}`` to retrieve its
    parameter set.

    This replaces N individual ``submit_job`` calls with a single call,
    satisfying the Phase 3 acceptance criterion: *one submission API call for
    a 50,000-run campaign*.

    Requires AWS credentials with permissions to call ``batch.submit-job``.
    """
    if not get_user_permission(request, "admin"):
        raise HTTPException(status_code=403, detail="Insufficient permissions")
    rec = _campaigns.get(campaign_id)
    if rec is None:
        raise HTTPException(status_code=404, detail=f"Campaign {campaign_id} not found")

    array_size = submit_req.array_size
    if array_size < 1:
        raise HTTPException(status_code=400, detail="array_size must be >= 1")

    try:
        batch = boto3.client("batch")
    except Exception as exc:
        raise HTTPException(  # noqa: B904
            status_code=503,
            detail=f"Cannot connect to AWS Batch: {exc}",
        )

    try:
        response = batch.submit_job(
            jobName=f"osimflow-{campaign_id[:8]}",
            jobQueue=submit_req.job_queue,
            jobDefinition=submit_req.job_definition,
            arrayProperties={"size": array_size},
            containerOverrides={
                "environment": [
                    {"name": "OSIMFLOW_CAMPAIGN_ID", "value": campaign_id},
                    {
                        "name": "OSIMFLOW_COORDINATOR_URL",
                        "value": os.environ.get(
                            "OSIMFLOW_COORDINATOR_URL",
                            "http://localhost:8000",
                        ),
                    },
                ],
            },
            timeout={
                "attemptDurationSeconds": int(rec.get("payload", {}).get("timeout_seconds", 14400))
            },
        )
    except Exception as exc:
        raise HTTPException(  # noqa: B904
            status_code=502,
            detail=f"AWS Batch submit-job failed: {exc}",
        )

    array_job_id: str = str(response["jobId"])
    rec["status"] = "pending"
    rec["array_job_id"] = array_job_id
    rec["updated_at"] = time.time()
    log.info(
        "Campaign %s submitted as array job %s (size=%d)",
        campaign_id,
        array_job_id,
        array_size,
    )
    return CoordinatorArraySubmitResponse(
        campaign_id=campaign_id,
        array_job_id=array_job_id,
        status="pending",
        message=f"Array job {array_job_id} submitted with {array_size} children.",
    )
