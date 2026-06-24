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

Phase 4 scope (issue #604):
  - notification_email and sns_topic_arn fields in handoff payload
  - GET /campaigns/{id}/poll-array — poll AWS Batch for array job completion
  - GET /campaigns/{id}/results — fetch aggregated result files from S3
  - POST /campaigns/{id}/notify — send SNS notification on completion
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import secrets
import tempfile
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import boto3
from fastapi import APIRouter, Header, HTTPException, Request

from osimflow.aggregation import compile_aggregation, parse_manifest
from osimflow.api.auth import get_user_permission
from osimflow.api.schemas import (
    CoordinatorAggregateResponse,
    CoordinatorArrayCompleteEvent,
    CoordinatorArrayCompleteResponse,
    CoordinatorArraySubmitRequest,
    CoordinatorArraySubmitResponse,
    CoordinatorCampaignRecord,
    CoordinatorHandoffPayload,
    CoordinatorHandoffResponse,
    CoordinatorNotifyRequest,
    CoordinatorNotifyResponse,
    CoordinatorPollArrayResponse,
    CoordinatorResultFile,
    CoordinatorResultsResponse,
    CoordinatorSampleRecord,
    CoordinatorSamplesResponse,
)
from osimflow.handoff_record import IDEMPOTENCY_KEY_HEADER
from osimflow.notify import (
    EmailNotifyBackend,
    NotifyBackend,
    NullNotifyBackend,
    SNSNotifyBackend,
    WebhookNotifyBackend,
    build_notify_backend,
)

if TYPE_CHECKING:
    # Type-only imports to avoid importing the storage/aggregation graph at
    # module load (S3/GCS/Azure clients are lazily constructed on first use;
    # ``AggregatedManifest`` appears only in a local annotation).
    from osimflow.aggregation import AggregatedManifest
    from osimflow.storage import ResultStorage

log = logging.getLogger("osimflow.api.coordinator")

coordinator_router = APIRouter(prefix="/api/v1/coordinator", tags=["coordinator"])

# In-memory store for Phase 2. Future phases will replace this with a
# proper database (e.g., DynamoDB on AWS, or the SQLite document store).
# This is intentionally minimal — just enough to pass the Phase 2 acceptance
# criteria: "CLI returns within seconds after uploading the config."
_campaigns: dict[str, dict[str, Any]] = {}

# Idempotency-key index for POST /handoff (issue #630). Maps the client-supplied
# ``Idempotency-Key`` header value to the ``campaign_id`` it first produced, so a
# retried/duplicate handoff for the same key returns the original campaign
# instead of minting a second one. Lives alongside ``_campaigns`` and is cleared
# by the same test fixtures. A request with *no* key always creates a new
# campaign (preserves the pre-idempotency behaviour for callers that don't send
# the header).
_idempotency_keys: dict[str, str] = {}

# Header name + the absolute poll path returned in ``status_url``. The header
# constant is imported from the dependency-light :mod:`osimflow.handoff_record`
# so the CLI and server agree on it without the CLI importing FastAPI.
_CAMPAIGN_STATUS_PATH = "/api/v1/coordinator/campaigns/{campaign_id}"


# ---------------------------------------------------------------------------
# Array-completion detection helpers (issue #626, Epic #624)
# ---------------------------------------------------------------------------
#
# Two paths declare an array job complete:
#   (a) EventBridge webhook -> POST /campaigns/{id}/array-complete
#   (b) poll_array_job() exponential-backoff polling fallback
#
# Both paths funnel through ``_parse_array_completion`` +
# ``_apply_completion_transition`` so the ``running -> aggregating`` flip is
# provably idempotent and consistent regardless of which path fires first.

# Campaign statuses that mean the running->aggregating transition has already
# happened (or moved past it). A late webhook arriving after one of these is
# an idempotent no-op.
_TERMINAL_OR_AGGREGATING = ("aggregating", "complete", "completed", "failed")

# Expected EventBridge envelope values for a Batch job state-change event.
_EXPECTED_EVENT_SOURCE = "aws.batch"
_EXPECTED_EVENT_DETAIL_TYPE = "Batch Job State Change"

# Shared-secret header + env var name. Production MUST set
# OSIMFLOW_EVENTBRIDGE_WEBHOOK_SECRET; an unset secret fails closed (401).
_EVENTBRIDGE_SECRET_HEADER = "X-OSimFLOW-Webhook-Secret"
_EVENTBRIDGE_SECRET_ENV = "OSIMFLOW_EVENTBRIDGE_WEBHOOK_SECRET"


class ArrayJobLookupError(RuntimeError):
    """Raised when the array parent job cannot be described from AWS Batch."""


@dataclass(frozen=True)
class ArrayCompletion:
    """Per-child terminal-state breakdown of an array parent job.

    ``complete`` is ``True`` once every child reached a terminal state
    (``SUCCEEDED`` **or** ``FAILED``) — i.e. ``succeeded + failed ==
    arrayProperties.size``. A partially-failed job is still *complete* (not
    *succeeded*); the aggregator (a later step) decides the final
    ``complete`` vs ``failed`` campaign status from the recorded split.
    """

    complete: bool
    succeeded: int
    failed: int
    pending: int
    total: int


def _batch_client() -> Any:
    """Return a ``boto3.client("batch")`` handle.

    Raises :class:`ArrayJobLookupError` if the AWS SDK cannot build a client
    (no credentials / no region). Centralised so the HTTP endpoints and the
    polling fallback share one connection path.
    """
    try:
        return boto3.client("batch")
    except Exception as exc:  # pragma: no cover - boto3 raises botocore exceptions
        raise ArrayJobLookupError(f"Cannot connect to AWS Batch: {exc}") from exc


def _describe_array_job(array_job_id: str, *, client: Any | None = None) -> dict[str, Any]:
    """Describe the array parent job.

    Returns the raw ``jobs[0]`` dict from ``describe_jobs``. The dict is the
    source of truth for ``arrayProperties.size`` and ``statusSummary``. Raises
    :class:`ArrayJobLookupError` if the call fails or the job is absent.
    """
    batch = client if client is not None else _batch_client()
    try:
        resp = batch.describe_jobs(jobs=[array_job_id])
    except Exception as exc:  # pragma: no cover - botocore exceptions
        raise ArrayJobLookupError(f"AWS Batch describe_jobs failed: {exc}") from exc
    jobs: list[dict[str, Any]] = resp.get("jobs") or []
    if not jobs:
        raise ArrayJobLookupError(f"Array job {array_job_id} not found")
    return jobs[0]


def _parse_array_completion(job: dict[str, Any]) -> ArrayCompletion:
    """Compute the terminal-state breakdown of an array parent job.

    Reads ``arrayProperties.size`` (the declared child count — the value both
    paths must verify against per the issue) and ``statusSummary`` (a map of
    child status -> count). ``statusSummary`` is read top-level on the job
    (canonical ``describe_jobs`` shape for array parents) with a fallback to
    ``jobSummary.statusSummary`` for the alternate/older shape.

    The job is *complete* once ``SUCCEEDED + FAILED >= arrayProperties.size``.
    Per the issue's partial-failure rule this is "complete (not succeeded)" —
    even when ``FAILED > 0``.
    """
    array_props = job.get("arrayProperties") or {}
    size = int(array_props.get("size", 0) or 0)
    summary = job.get("statusSummary") or (job.get("jobSummary") or {}).get("statusSummary") or {}
    succeeded = int(summary.get("SUCCEEDED", 0) or 0)
    failed = int(summary.get("FAILED", 0) or 0)
    total = size if size > 0 else succeeded + failed
    terminal = succeeded + failed
    complete = size > 0 and terminal >= size
    pending = max(total - terminal, 0)
    return ArrayCompletion(
        complete=complete,
        succeeded=succeeded,
        failed=failed,
        pending=pending,
        total=total,
    )


def _apply_completion_transition(
    rec: dict[str, Any],
    completion: ArrayCompletion,
    *,
    source: str,
) -> bool:
    """Flip a campaign ``running -> aggregating`` once the array is complete.

    Idempotent: once the campaign is already ``aggregating``/``complete``/
    ``failed`` this is a no-op returning ``False``. Returns ``True`` only when
    *this* call performed the transition. Partial failures (``failed > 0``)
    still transition to ``aggregating``; the succeeded/failed split is recorded
    on the record as ``array_completion`` for the downstream aggregator.
    """
    current = rec.get("status", "pending")
    if current in _TERMINAL_OR_AGGREGATING:
        return False
    if not completion.complete:
        return False
    now = time.time()
    rec["status"] = "aggregating"
    rec["updated_at"] = now
    rec["array_completion"] = {
        "succeeded": completion.succeeded,
        "failed": completion.failed,
        "total": completion.total,
        "completed_at": now,
        "source": source,
    }
    log.info(
        "Campaign %s array job complete (source=%s) — %d succeeded, %d failed of %d; "
        "transitioned running -> aggregating",
        rec.get("campaign_id"),
        source,
        completion.succeeded,
        completion.failed,
        completion.total,
    )
    return True


def _job_id_matches(parent_job_id: str, reported_job_id: str) -> bool:
    """Return True if *reported_job_id* is the array parent or one of its children.

    AWS Batch array children have jobIds of the form ``<parentJobId>:<index>``
    (e.g. ``abc123:0``). EventBridge may fire a state-change event for a child
    rather than the parent, so we accept both.
    """
    if not reported_job_id:
        return False
    if reported_job_id == parent_job_id:
        return True
    return reported_job_id.startswith(f"{parent_job_id}:")


def _verify_eventbridge_signature(
    request: Request,
    event: CoordinatorArrayCompleteEvent,
) -> None:
    """Authenticate + sanity-check an EventBridge webhook call.

    Security model (issue #626 + AGENTS.md §10):

    * **Shared secret.** The caller must send ``X-OSimFLOW-Webhook-Secret``
      matching ``$OSIMFLOW_EVENTBRIDGE_WEBHOOK_SECRET`` (constant-time compare).
      The EventBridge rule's API-destination target is configured with this
      header. If the env var is **unset**, the endpoint fails **closed**
      (HTTP 401) — unauthenticated state transitions are never accepted in
      production. Tests set the env var explicitly.

    * **Source / detail-type.** ``source`` must be ``aws.batch`` and
      ``detail-type`` must be ``"Batch Job State Change"``. This rejects
      mis-routed or replayed events from other rules.

    Production hardening (documented, not enforced here): additionally verify
    the AWS SigV4 signature on the raw body (e.g. via an API Gateway Lambda
    authoriser in front of this endpoint), or terminate the EventBridge target
    on a private service with VPC/mTLS. The shared-secret header is the
    minimum bar enforced in-process.
    """
    expected = os.environ.get(_EVENTBRIDGE_SECRET_ENV)
    if not expected:
        log.error(
            "EventBridge webhook rejected: %s is not set (fail-closed). "
            "Set it to the shared secret configured on the EventBridge target.",
            _EVENTBRIDGE_SECRET_ENV,
        )
        raise HTTPException(
            status_code=401,
            detail="EventBridge webhook shared secret is not configured on the server.",
        )
    provided = request.headers.get(_EVENTBRIDGE_SECRET_HEADER)
    if not provided or not secrets.compare_digest(provided, expected):
        log.warning("EventBridge webhook rejected: invalid or missing shared secret")
        raise HTTPException(status_code=401, detail="Invalid EventBridge webhook signature.")

    source = event.source
    detail_type = event.detail_type
    if source != _EXPECTED_EVENT_SOURCE or detail_type != _EXPECTED_EVENT_DETAIL_TYPE:
        log.warning(
            "EventBridge webhook rejected: source=%r detail-type=%r (expected %r / %r)",
            source,
            detail_type,
            _EXPECTED_EVENT_SOURCE,
            _EXPECTED_EVENT_DETAIL_TYPE,
        )
        raise HTTPException(
            status_code=400,
            detail=(
                f"Unexpected EventBridge event: source={source!r}, detail-type={detail_type!r}."
            ),
        )


def _build_complete_response(
    rec: dict[str, Any],
    array_job_id: str,
    completion: ArrayCompletion,
    transitioned: bool,
) -> CoordinatorArrayCompleteResponse:
    """Build the response shared by the webhook and the polling fallback."""
    status = rec.get("status", "pending")
    response_status = (
        "aggregating"
        if transitioned
        else ("already_aggregating" if status == "aggregating" else status)
    )
    if not completion.complete and not transitioned:
        response_status = "pending"
    return CoordinatorArrayCompleteResponse(
        campaign_id=rec.get("campaign_id", ""),
        array_job_id=array_job_id,
        status=response_status,
        succeeded=completion.succeeded,
        failed=completion.failed,
        total=completion.total,
        transitioned=transitioned,
        message=(
            f"Array {array_job_id}: {completion.succeeded} succeeded, "
            f"{completion.failed} failed of {completion.total} "
            f"({'transitioned to aggregating' if transitioned else 'no-op'})"
        ),
    )


def _lookup_http_array_job(array_job_id: str) -> dict[str, Any]:
    """Describe an array job for an HTTP handler, translating lookup errors.

    ``ArrayJobLookupError("...not found")`` -> HTTP 404; any other lookup
    error -> HTTP 502 (bad gateway from Batch). Connection errors surface as
    502 as well since the caller already passed the auth gate.
    """
    try:
        return _describe_array_job(array_job_id)
    except ArrayJobLookupError as exc:
        message = str(exc)
        code = 404 if "not found" in message else 502
        raise HTTPException(status_code=code, detail=message) from exc


def _campaign_or_404(campaign_id: str) -> dict[str, Any]:
    """Return the campaign record or raise HTTP 404."""
    rec = _campaigns.get(campaign_id)
    if rec is None:
        raise HTTPException(status_code=404, detail=f"Campaign {campaign_id} not found")
    return rec


def _presign_aggregated_get(bucket: str | None, key: str | None, expires: int = 3600) -> str | None:
    """Return a short-lived presigned GET URL for the aggregated-results object.

    Used by ``GET /campaigns/{id}/results`` (issue #630) so the CLI's
    ``osimflow download`` can fetch *only* the final aggregated CSV via a URL
    the Coordinator's IAM role signs — the downloading client needs no AWS
    credentials of its own.

    Returns ``None`` (rather than raising) when there is nothing to sign or
    S3 is unavailable, so the results listing still succeeds.
    """
    if not bucket or not key:
        return None
    try:
        return str(
            boto3.client("s3").generate_presigned_url(
                "get_object",
                Params={"Bucket": bucket, "Key": key},
                ExpiresIn=expires,
            )
        )
    except Exception as exc:  # pragma: no cover - botocore exceptions
        log.warning(
            "Could not presign aggregated results URL for s3://%s/%s: %s",
            bucket,
            key,
            exc,
        )
        return None


@coordinator_router.post(
    "/handoff",
    response_model=CoordinatorHandoffResponse,
    status_code=202,
    summary="Handoff a campaign to the Coordinator",
    description=(
        "Accepts a campaign configuration and returns immediately (HTTP 202) "
        "with a campaign_id. The Coordinator takes ownership of the campaign "
        "lifecycle. Use GET /api/v1/coordinator/campaigns/{campaign_id} to "
        "poll status.\n\n"
        "**Idempotent** on the `Idempotency-Key` header (issue #630): a "
        "duplicate handoff carrying the same key as a prior, accepted request "
        "returns the *original* campaign_id and status_url instead of creating "
        "a second campaign. Callers that omit the header get the legacy "
        "behaviour (a new campaign every time)."
    ),
)
async def coordinator_handoff(
    payload: CoordinatorHandoffPayload,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias=IDEMPOTENCY_KEY_HEADER),
) -> CoordinatorHandoffResponse:
    """Handle campaign handoff from the CLI.

    Validates the payload, assigns a campaign_id, stores the campaign
    metadata, and returns immediately so the CLI can exit cleanly. The
    response carries an absolute ``status_url`` the CLI persists to its local
    handoff record for later reconnection.

    Idempotency (issue #630): if an ``Idempotency-Key`` header is supplied and
    a campaign was already created for that key, the original campaign is
    returned unchanged. This makes a CLI retry after a lost response safe —
    the user never ends up with two campaigns for one intended run.
    """
    if not get_user_permission(request, "write"):
        raise HTTPException(status_code=403, detail="Insufficient permissions for campaign handoff")

    # --- Idempotent replay: same key -> same campaign (issue #630) ---
    if idempotency_key and idempotency_key in _idempotency_keys:
        existing_id = _idempotency_keys[idempotency_key]
        existing = _campaigns.get(existing_id)
        if existing is not None:
            status_url = str(existing.get("status_url") or "")
            log.info(
                "Idempotent handoff replay for key=%s -> returning existing campaign %s",
                idempotency_key,
                existing_id,
            )
            return CoordinatorHandoffResponse(
                campaign_id=existing_id,
                status=existing.get("status", "pending"),
                message=(
                    f"Campaign '{existing.get('name', '')}' already accepted "
                    f"(idempotent replay of key {idempotency_key})."
                ),
                status_url=status_url or None,
            )
        # Fallthrough guard: the index pointed at a since-removed campaign
        # (only possible via direct store manipulation in tests). Drop the
        # stale entry and create a fresh campaign below.
        _idempotency_keys.pop(idempotency_key, None)

    campaign_id = str(uuid.uuid4())
    now = time.time()

    # Absolute status URL built from the request's scheme + host so it is
    # correct behind proxies and usable by TestClient. ``request.base_url``
    # carries a trailing slash while the path carries a leading slash, so we
    # strip one to avoid a ``//`` join.
    status_url = (
        f"{str(request.base_url).rstrip('/')}"
        f"{_CAMPAIGN_STATUS_PATH.format(campaign_id=campaign_id)}"
    )

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
        "notification_email": payload.notification_email,
        "sns_topic_arn": payload.sns_topic_arn,
        "webhook_url": payload.webhook_url,
        "result_storage_bucket": payload.result_storage_bucket,
        "array_job_id": None,
        "result_status": "unavailable",
        "aggregated_results_key": None,
        # Persisted on the record so an idempotent replay can rebuild the
        # exact same response (incl. status_url) without re-deriving the URL.
        "status_url": status_url,
        "idempotency_key": idempotency_key,
    }

    _campaigns[campaign_id] = record
    if idempotency_key:
        _idempotency_keys[idempotency_key] = campaign_id

    log.info(
        "Campaign %s handed off: name=%s, n_samples=%d, idempotency_key=%s",
        campaign_id,
        payload.name,
        payload.n_samples,
        idempotency_key or "(none)",
    )

    return CoordinatorHandoffResponse(
        campaign_id=campaign_id,
        status="pending",
        message=f"Campaign '{payload.name}' accepted. Use GET {status_url} to poll status.",
        status_url=status_url,
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


@coordinator_router.get(
    "/campaigns/{campaign_id}/poll-array",
    response_model=CoordinatorPollArrayResponse,
    summary="Poll AWS Batch for array job completion status",
)
async def poll_array_job(
    campaign_id: str,
    request: Request,
) -> CoordinatorPollArrayResponse:
    """Poll AWS Batch for the status of an array job submitted via POST /submit-array.

    Returns per-child counts: succeeded, failed, pending. A job is *complete*
    (not *succeeded*) once ``succeeded + failed == arrayProperties.size`` —
    partial failures still advance the campaign to ``aggregating`` and the
    succeeded/failed split is recorded for the aggregator. This shares the
    exact same transition logic as the EventBridge webhook
    (``POST /array-complete``), so the two paths are idempotent: whichever
    fires first flips ``running -> aggregating``; the other is a no-op.

    Requires ``admin`` permission and a stored ``array_job_id`` on the campaign.
    """
    if not get_user_permission(request, "admin"):
        raise HTTPException(status_code=403, detail="Insufficient permissions")
    rec = _campaign_or_404(campaign_id)

    array_job_id: str | None = rec.get("array_job_id")
    if not array_job_id:
        raise HTTPException(
            status_code=400,
            detail=f"Campaign {campaign_id} has no associated array job",
        )

    job = _lookup_http_array_job(array_job_id)
    completion = _parse_array_completion(job)
    # Shared transition: advances running -> aggregating when complete.
    _apply_completion_transition(rec, completion, source="poll")

    parent_status: str = job.get("status", "UNKNOWN")
    return CoordinatorPollArrayResponse(
        campaign_id=campaign_id,
        array_job_id=array_job_id,
        status="complete" if completion.complete else parent_status.lower(),
        succeeded=completion.succeeded,
        failed=completion.failed,
        pending=completion.pending,
        total=completion.total,
        result_bucket=rec.get("result_storage_bucket"),
        message=(
            f"Array job {array_job_id}: {completion.succeeded} succeeded, "
            f"{completion.failed} failed, {completion.pending} pending of "
            f"{completion.total} total."
        ),
    )


@coordinator_router.get(
    "/campaigns/{campaign_id}/results",
    response_model=CoordinatorResultsResponse,
    summary="Fetch aggregated and per-sample result files for a campaign",
)
async def get_campaign_results(
    campaign_id: str,
    request: Request,
) -> CoordinatorResultsResponse:
    """Return references to all result files for a campaign.

    For Phase 4, result files are stored in the campaign's S3 bucket under keys:

    - ``results/aggregated_results.csv`` — combined CSV of all sample KPIs
    - ``results/kpi_<index>.json`` — per-sample KPI JSON files

    Workers upload their results directly to S3; this endpoint lets the user or
    a downstream aggregator enumerate and download them without going through the
    Coordinator process.

    Returns ``status: unavailable`` if no result bucket is configured.
    """
    get_user_permission(request, "read")  # authenticate
    rec = _campaigns.get(campaign_id)
    if rec is None:
        raise HTTPException(status_code=404, detail=f"Campaign {campaign_id} not found")

    bucket = rec.get("result_storage_bucket")
    if not bucket:
        return CoordinatorResultsResponse(
            campaign_id=campaign_id,
            status="unavailable",
            result_bucket=None,
            aggregated_results_key=None,
            kpi_files=[],
            message="No result_storage_bucket configured for this campaign.",
        )

    result_status = rec.get("result_status", "unavailable")
    aggregated_key = rec.get("aggregated_results_key")

    # Short-lived presigned GET URL for the aggregated CSV (issue #630). Signed
    # by the Coordinator's IAM role so the downloading CLI needs no AWS
    # credentials of its own. Only present when an aggregated object exists;
    # any S3 failure degrades gracefully to ``None`` rather than failing the
    # whole results listing.
    aggregated_url = _presign_aggregated_get(bucket, aggregated_key)

    kpi_files: list[CoordinatorResultFile] = []
    try:
        s3 = boto3.client("s3")
        paginator = s3.get_paginator("list_objects_v2")
        pages = paginator.paginate(
            Bucket=bucket, Prefix="results/kpi_", PaginationConfig={"page_size": 100}
        )
        for page in pages:
            for obj in page.get("Contents", []):
                key: str = obj["Key"]
                size: int = obj.get("Size", 0)
                basename = key.rsplit("/", 1)[-1]
                parts = basename.replace("kpi_", "").replace(".json", "").split("_")
                sample_idx: int | None = int(parts[0]) if parts and parts[0].isdigit() else None
                kpi_files.append(
                    CoordinatorResultFile(
                        sample_index=sample_idx,
                        file_key=key,
                        file_type="json",
                        size_bytes=size,
                    )
                )
    except Exception as exc:
        log.warning("Failed to list S3 results for campaign %s: %s", campaign_id, exc)

    return CoordinatorResultsResponse(
        campaign_id=campaign_id,
        status=result_status,
        result_bucket=bucket,
        aggregated_results_key=aggregated_key,
        aggregated_results_url=aggregated_url,
        kpi_files=kpi_files,
        message=(
            f"Found {len(kpi_files)} KPI files in s3://{bucket}/results/"
            if bucket
            else "No result storage configured."
        ),
    )


@coordinator_router.post(
    "/campaigns/{campaign_id}/notify",
    response_model=CoordinatorNotifyResponse,
    summary="Trigger a completion notification for a campaign",
)
async def notify_campaign(
    campaign_id: str,
    notify_req: CoordinatorNotifyRequest,
    request: Request,
) -> CoordinatorNotifyResponse:
    """Dispatch a completion notification via the configured backend (issue #628).

    Selects a :class:`osimflow.notify.NotifyBackend` from the request's
    ``notification_type`` (``sns`` / ``email`` / ``webhook``) and the
    campaign's stored ``sns_topic_arn`` / ``notification_email`` /
    ``webhook_url``. The §3.5 payload carries the presigned
    ``download_url`` whose lifetime is ``expires_in_seconds`` (mirrors
    ``--s3-artifact-presigned-url-expiration``).

    Failures in the backend are logged with ``exc_info=True`` and never
    propagate: a notification mishap cannot flip a succeeded campaign
    back to failed (issue #628 criterion #4 — best-effort).
    """
    if not get_user_permission(request, "admin"):
        raise HTTPException(status_code=403, detail="Insufficient permissions")
    rec = _campaigns.get(campaign_id)
    if rec is None:
        raise HTTPException(status_code=404, detail=f"Campaign {campaign_id} not found")

    expires_in = max(60, int(notify_req.expires_in_seconds or 3600))
    subject = notify_req.subject or f"OSimFlow campaign '{rec.get('name', campaign_id)}' complete"

    aggregated_key = rec.get("aggregated_results_key")
    bucket = rec.get("result_storage_bucket")
    download_url = _presign_aggregated_get(bucket, aggregated_key, expires=expires_in)

    payload = _build_notify_payload(
        rec,
        download_url=download_url,
        expires_in_seconds=expires_in,
    )

    backend = build_notify_backend(
        sns_topic_arn=rec.get("sns_topic_arn"),
        notification_email=rec.get("notification_email"),
        webhook_url=rec.get("webhook_url"),
        notification_type=notify_req.notification_type,
        subject=subject,
    )

    return _dispatch_notify(
        backend,
        event="campaign.succeeded",
        payload=payload,
        rec=rec,
        notification_type=notify_req.notification_type,
    )


# ---------------------------------------------------------------------------
# Notification dispatch helpers (issue #628)
# ---------------------------------------------------------------------------
#
# The pure dispatch logic is split out of the HTTP handler so the
# aggregation endpoint can re-use it to auto-fire notifications on
# completion (criterion #3) without going through the network. Both
# paths share the same payload shape (§3.5) and the same best-effort
# contract (criterion #4).


def _build_notify_payload(
    rec: dict[str, Any],
    *,
    download_url: str | None,
    expires_in_seconds: int,
) -> dict[str, Any]:
    """Build the §3.5 ``campaign.succeeded`` payload for a campaign record."""
    return {
        "campaign_id": rec.get("campaign_id", ""),
        "name": rec.get("name"),
        "status": rec.get("status", "unknown"),
        "download_url": download_url,
        "expires_in_seconds": expires_in_seconds,
        "aggregated_results_key": rec.get("aggregated_results_key"),
        "result_storage_bucket": rec.get("result_storage_bucket"),
        "osimflow_version": _osimflow_version(),
    }


def _osimflow_version() -> str:
    """Return the installed OSimFlow version, or 'unknown'."""
    try:
        from importlib.metadata import version  # noqa: PLC0415

        return version("osimflow")
    except Exception:
        return "unknown"


def _dispatch_notify(
    backend: NotifyBackend,
    *,
    event: str,
    payload: dict[str, Any],
    rec: dict[str, Any],
    notification_type: str,
) -> CoordinatorNotifyResponse:
    """Send through *backend*; never raise (criterion #4).

    Belt-and-braces: backends are themselves contract-bound to swallow
    errors, but this wrapper still catches any leaked exception so a
    buggy backend can never reach the HTTP layer / aggregation flow.
    """
    campaign_id = rec.get("campaign_id", "")
    if isinstance(backend, NullNotifyBackend):
        return CoordinatorNotifyResponse(
            campaign_id=campaign_id,
            notification_type=notification_type,
            status="skipped",
            message="No notification channel configured for this campaign.",
        )
    try:
        backend.send(event, payload)
    except Exception as exc:
        log.warning(
            "notify: %s.send raised (campaign %s, event %s): %s",
            type(backend).__name__,
            campaign_id,
            event,
            exc,
            exc_info=True,
        )
        return CoordinatorNotifyResponse(
            campaign_id=campaign_id,
            notification_type=notification_type,
            status="failed",
            message=f"Notification dispatch failed: {exc}",
        )
    return CoordinatorNotifyResponse(
        campaign_id=campaign_id,
        notification_type=notification_type,
        status="sent",
        message=f"Notification dispatched via {type(backend).__name__}.",
    )


def _notify_campaign_completion(rec: dict[str, Any]) -> None:
    """Auto-fire completion notifications through every configured channel.

    Called from the aggregation endpoint after the campaign status flips
    to ``complete`` (issue #628 criterion #3). Iterates the channels
    configured on the campaign record (SNS topic ARN → notification
    email → webhook URL) and dispatches the §3.5 ``campaign.succeeded``
    payload to each. Best-effort: a failure in any channel is logged
    with ``exc_info=True`` and does not affect the others or the
    campaign status (criterion #4).
    """
    campaign_id = rec.get("campaign_id", "")
    # Default lifetime mirrors --s3-artifact-presigned-url-expiration.
    expires_in = 3600

    bucket = rec.get("result_storage_bucket")
    aggregated_key = rec.get("aggregated_results_key")
    download_url = _presign_aggregated_get(bucket, aggregated_key, expires=expires_in)

    payload = _build_notify_payload(
        rec,
        download_url=download_url,
        expires_in_seconds=expires_in,
    )

    configured: list[tuple[str, NotifyBackend]] = []
    sns_arn = rec.get("sns_topic_arn")
    if sns_arn:
        configured.append(("sns", SNSNotifyBackend(topic_arn=sns_arn)))
    email = rec.get("notification_email")
    if email:
        configured.append(("email", EmailNotifyBackend(recipient=email)))
    webhook = rec.get("webhook_url")
    if webhook:
        configured.append(("webhook", WebhookNotifyBackend(url=webhook)))

    if not configured:
        log.info(
            "Campaign %s completed but no notification channels are configured "
            "— skipping auto-notify.",
            campaign_id,
        )
        return

    log.info(
        "Campaign %s auto-notifying via %d channel(s): %s",
        campaign_id,
        len(configured),
        ", ".join(ntype for ntype, _ in configured),
    )
    for ntype, backend in configured:
        _dispatch_notify(
            backend,
            event="campaign.succeeded",
            payload=payload,
            rec=rec,
            notification_type=ntype,
        )


# ---------------------------------------------------------------------------
# Array-completion detection (issue #626, Epic #624)
# ---------------------------------------------------------------------------


@coordinator_router.post(
    "/campaigns/{campaign_id}/array-complete",
    response_model=CoordinatorArrayCompleteResponse,
    summary="EventBridge webhook: declare an array job complete",
    description=(
        "Target of an EventBridge rule firing on a Batch array-job state "
        "change. Validates the EventBridge signature/source, confirms via "
        "`describe_jobs` that every array child reached a terminal state "
        "(SUCCEEDED or FAILED), and flips the campaign `running -> "
        "aggregating`. Idempotent with the polling fallback: a late webhook "
        "after a poll-driven transition is a 200 no-op."
    ),
)
async def array_complete(
    campaign_id: str,
    event: CoordinatorArrayCompleteEvent,
    request: Request,
    # Header declared explicitly so it shows up in the OpenAPI spec and so
    # callers know the webhook is authenticated. The actual constant-time
    # comparison happens inside ``_verify_eventbridge_signature``.
    _webhook_secret: str | None = Header(default=None, alias=_EVENTBRIDGE_SECRET_HEADER),
) -> CoordinatorArrayCompleteResponse:
    """EventBridge webhook that declares a campaign's array job 100% complete.

    Auth/security: see :func:`_verify_eventbridge_signature`. The request MUST
    carry the ``X-OSimFLOW-Webhook-Secret`` header matching
    ``$OSIMFLOW_EVENTBRIDGE_WEBHOOK_SECRET``, and the event ``source`` must be
    ``aws.batch`` with ``detail-type == "Batch Job State Change"``. An unset
    server secret fails **closed** (401) — never accept unauthenticated state
    transitions in production.

    The handler does **not** trust the event payload's status fields. It
    re-queries ``describe_jobs`` for the array parent stored on the campaign
    and verifies ``succeeded + failed == arrayProperties.size`` before
    transitioning. Partial failures (``failed > 0``) still transition to
    ``aggregating``; the succeeded/failed split is recorded for the aggregator.
    """
    # 1. Authenticate + validate EventBridge envelope (raises on failure).
    _verify_eventbridge_signature(request, event)

    # 2. Load campaign + validate the event's jobId belongs to it.
    rec = _campaign_or_404(campaign_id)
    parent_job_id: str | None = rec.get("array_job_id")
    if not parent_job_id:
        raise HTTPException(
            status_code=400,
            detail=f"Campaign {campaign_id} has no associated array job",
        )
    reported_job_id = str(event.detail.get("jobId", ""))
    if not _job_id_matches(parent_job_id, reported_job_id):
        log.warning(
            "Campaign %s: EventBridge jobId %r does not match stored array_job_id %r",
            campaign_id,
            reported_job_id,
            parent_job_id,
        )
        raise HTTPException(
            status_code=409,
            detail=(
                f"EventBridge jobId {reported_job_id!r} does not match the "
                f"array job tracked by campaign {campaign_id}."
            ),
        )

    # 3. Source of truth: re-query Batch for the parent array job.
    job = _lookup_http_array_job(parent_job_id)
    completion = _parse_array_completion(job)

    # 4. Idempotent transition (running -> aggregating) if all children terminal.
    transitioned = _apply_completion_transition(rec, completion, source="eventbridge")

    return _build_complete_response(rec, parent_job_id, completion, transitioned)


# ---------------------------------------------------------------------------
# Terminal aggregation (issue #627, Epic #624)
# ---------------------------------------------------------------------------
#
# Once every array child reached a terminal state (S2/S5 ``array_complete``),
# the Coordinator lists the per-sample ``_manifest.json`` files the workers
# published (issue #625), compiles the campaign-level ``aggregated_results.csv``
# + ``failed_simulations.csv`` (column-for-column compatible with the local
# ``bin/aggregate_results.py`` path), optionally computes a Pareto front for
# multi-objective algorithms, writes everything to ``_aggregated/``, and flips
# the campaign ``aggregating -> complete``.
#
# The storage-heavy logic lives in the pure, storage-agnostic
# :mod:`osimflow.aggregation` module so it is unit-testable without S3.  The
# endpoint below is a thin adapter: it resolves the :class:`ResultStorage`
# backend, wires a ``kpi_fetcher`` that downloads + parses each ``kpis.json``
# through the ABC, uploads the produced artifacts, and performs the status
# transition.

#: Terminal statuses that mean aggregation has already run.  A second
#: ``/aggregate`` call against one of these is rejected with HTTP 409 (the
#: contract allows only ``aggregating`` to advance).
_AGGREGATION_DONE = ("complete", "completed", "failed", "succeeded")


def _storage_from_campaign(rec: dict[str, Any]) -> ResultStorage | None:
    """Build the campaign's :class:`ResultStorage` from its handoff payload.

    Returns ``None`` when no ``result_storage_bucket`` is configured (the
    endpoint then 409s with a helpful message — aggregation is impossible
    without object storage).  Workers embed the ``campaign_id`` in every key
    (``{campaign_id}/samples/{sample_id}/...``), so the backend is built with
    an empty prefix and campaign-relative keys are used throughout.
    """
    payload = rec.get("payload") or {}
    backend = (payload.get("result_storage_backend") or "s3").lower()
    bucket = rec.get("result_storage_bucket") or payload.get("result_storage_bucket")
    if not bucket:
        return None
    endpoint = (
        payload.get("extra", {}).get("result_storage_endpoint")
        if isinstance(payload.get("extra"), dict)
        else None
    )
    from osimflow.storage import build_result_storage  # noqa: PLC0415

    try:
        return build_result_storage(
            backend=backend,
            bucket=str(bucket),
            prefix="",
            endpoint_url=endpoint,
        )
    except ValueError as exc:
        log.warning("aggregate: unknown result_storage_backend %r: %s", backend, exc)
        return None


def _make_kpi_fetcher(
    storage: ResultStorage, tmp_root: Path
) -> Callable[[str], dict[str, Any] | None]:
    """Build a ``kpi_fetcher`` for :func:`compile_aggregation`.

    Downloads the referenced ``kpis.json`` object to a unique temp file under
    *tmp_root* and parses it.  Returns ``None`` (rather than raising) on any
    download/parse error so the aggregation core can honour the criterion-#5
    robustness contract (ok manifest + missing kpis → counted as failed, not a
    crash).
    """

    def fetch(key: str) -> dict[str, Any] | None:
        if not key:
            return None
        # Sanitize the (unique) object key into a stable local filename so
        # distinct samples never collide on the same temp file.
        local = tmp_root / key.replace("/", "_")
        try:
            storage.download_file(key, local)
        except OSError as exc:
            log.warning("aggregate: could not download kpis.json at %s: %s", key, exc)
            return None
        try:
            data = json.loads(local.read_text())
        except (OSError, ValueError) as exc:
            log.warning("aggregate: could not parse kpis.json at %s: %s", key, exc)
            return None
        finally:
            with contextlib.suppress(OSError):
                local.unlink(missing_ok=True)
        if not isinstance(data, dict):
            log.warning("aggregate: kpis.json at %s is not a JSON object", key)
            return None
        return data

    return fetch


def _write_aggregated_object(
    storage: ResultStorage, remote_key: str, payload: str, tmp_root: Path
) -> None:
    """Stage *payload* (a string) to a temp file and upload it via the ABC.

    :class:`ResultStorage` only exposes path-based ``upload_file``, so every
    artifact is written to a temp file under *tmp_root* first.  For
    :class:`~osimflow.storage.LocalStorage` the upload is a no-op; callers that
    need the bytes materialised on disk should use a real remote backend in
    production.
    """
    tmp = tmp_root / remote_key.replace("/", "_")
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(payload, encoding="utf-8")
    storage.upload_file(tmp, remote_key)


def _apply_aggregation_transition(
    rec: dict[str, Any],
    *,
    ok_count: int,
    failed_count: int,
    aggregated_key: str,
    failed_key: str | None,
    pareto_key: str | None,
) -> None:
    """Flip the campaign ``aggregating -> complete`` and record artifact keys.

    Mirrors the idempotent-transition shape of
    :func:`_apply_completion_transition`: the record is mutated in place
    (single-process store under the GIL).  The transition is *not* guarded
    here — the endpoint enforces the precondition (``status == aggregating``)
    before calling, and a duplicate call would have already returned 409.
    """
    now = time.time()
    rec["status"] = "complete"
    rec["updated_at"] = now
    rec["completed_at"] = now
    rec["result_status"] = "available"
    rec["aggregated_results_key"] = aggregated_key
    rec["failed_simulations_key"] = failed_key
    rec["pareto_front_key"] = pareto_key
    rec["aggregation_summary"] = {
        "ok": ok_count,
        "failed": failed_count,
        "total": ok_count + failed_count,
        "completed_at": now,
    }
    log.info(
        "Campaign %s aggregation complete — %d ok, %d failed; aggregated_results=%s",
        rec.get("campaign_id"),
        ok_count,
        failed_count,
        aggregated_key,
    )


@coordinator_router.post(
    "/campaigns/{campaign_id}/aggregate",
    response_model=CoordinatorAggregateResponse,
    status_code=202,
    summary="Compile terminal aggregated_results.csv + failed_simulations.csv from manifests",
    description=(
        "Terminal aggregation step of Epic #624.  Lists every "
        "`{campaign_id}/samples/*/_manifest.json`, reads each referenced "
        "`kpis.json`, and compiles `aggregated_results.csv` (same column "
        "contract as `bin/aggregate_results.py`) plus `failed_simulations.csv` "
        "(first `  * Severe` line per failed manifest).  When the "
        "campaign algorithm is multi-objective (nsga2/pso) a Pareto-front JSON "
        "is also written.  Artifacts land under `{campaign_id}/_aggregated/` "
        "and the campaign status flips `aggregating -> complete`.\n\n"
        "**Precondition**: campaign status MUST be `aggregating` (i.e. the "
        "array job was declared complete via `/array-complete`).  A call "
        "against any other status returns HTTP 409.  For the MVP the "
        "aggregation runs synchronously inside the endpoint and returns 202 "
        "with a synthetic `aggregator_job_id`."
    ),
)
async def aggregate_campaign_results(
    campaign_id: str,
    request: Request,
) -> CoordinatorAggregateResponse:
    """Compile + publish the terminal campaign artifacts from sample manifests.

    Robustness contract (issue #627 criterion #5): a manifest that claims
    ``status="ok"`` but whose ``kpis.json`` is missing is logged with
    ``exc_info=True`` and counted as **failed** — it never crashes the
    aggregation or blocks the status transition.
    """
    if not get_user_permission(request, "write"):
        raise HTTPException(
            status_code=403, detail="Insufficient permissions to aggregate campaign results"
        )

    rec = _campaign_or_404(campaign_id)

    status = str(rec.get("status", "pending"))
    if status in _AGGREGATION_DONE:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Campaign {campaign_id} is already {status!r} — aggregation "
                f"has run (or the campaign failed). Re-running is not supported."
            ),
        )
    if status != "aggregating":
        raise HTTPException(
            status_code=409,
            detail=(
                f"Campaign {campaign_id} is {status!r}; aggregation requires "
                f"status 'aggregating' (call POST /array-complete first)."
            ),
        )

    storage = _storage_from_campaign(rec)
    if storage is None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Campaign {campaign_id} has no result_storage_bucket configured "
                f"— cannot aggregate without object storage."
            ),
        )

    # Discover every manifest the workers published under this campaign.
    manifest_prefix = f"{campaign_id}/samples/"
    try:
        candidate_keys = storage.list_results(manifest_prefix)
    except OSError as exc:
        raise HTTPException(  # pragma: no cover - storage listing failure
            status_code=502,
            detail=f"Failed to list sample manifests: {exc}",
        ) from exc

    manifest_keys = [k for k in candidate_keys if k.endswith("/_manifest.json")]
    if not manifest_keys:
        raise HTTPException(
            status_code=409,
            detail=(
                f"No sample manifests found under {manifest_prefix} — the array "
                f"job may not have produced any results yet."
            ),
        )

    # Download + parse each manifest, then compile the campaign artifacts.
    with tempfile.TemporaryDirectory(prefix="osimflow_agg_") as tmp_dir_str:
        tmp_root = Path(tmp_dir_str)
        manifests: list[AggregatedManifest] = []
        for key in manifest_keys:
            local = tmp_root / ("manifest_" + key.replace("/", "_"))
            try:
                storage.download_file(key, local)
                raw = json.loads(local.read_text())
            except (OSError, ValueError) as exc:
                log.warning(
                    "aggregate: skipping unparseable manifest at %s: %s",
                    key,
                    exc,
                    exc_info=True,
                )
                continue
            finally:
                with contextlib.suppress(OSError):
                    local.unlink(missing_ok=True)
            manifests.append(parse_manifest(raw))

        payload = rec.get("payload") or {}
        algorithm = payload.get("algorithm")
        # Optional per-KPI objective map (e.g. {"eui": "minimize"}).  Passed
        # through verbatim; the aggregation core defaults to all-minimize.
        kpi_objectives_raw = (
            payload.get("extra", {}).get("kpi_objectives")
            if isinstance(payload.get("extra"), dict)
            else None
        )

        result = compile_aggregation(
            manifests,
            _make_kpi_fetcher(storage, tmp_root),
            algorithm=algorithm,
            kpi_objectives=kpi_objectives_raw,
        )

        # Surface criterion-#5 downgrades explicitly (one log line per sample).
        for sid in result.degraded_ok_samples:
            log.warning(
                "aggregate: campaign %s sample %s claimed status=ok but its "
                "kpis.json was missing/unreadable — counted as failed",
                campaign_id,
                sid,
            )

        aggregated_key = f"{campaign_id}/_aggregated/aggregated_results.csv"
        failed_key = f"{campaign_id}/_aggregated/failed_simulations.csv"
        pareto_key: str | None = None
        _write_aggregated_object(storage, aggregated_key, result.aggregated_results_csv, tmp_root)
        _write_aggregated_object(storage, failed_key, result.failed_simulations_csv, tmp_root)
        if result.pareto_json is not None:
            pareto_key = f"{campaign_id}/_aggregated/pareto_front.json"
            _write_aggregated_object(storage, pareto_key, result.pareto_json, tmp_root)

    _apply_aggregation_transition(
        rec,
        ok_count=result.ok_count,
        failed_count=result.failed_count,
        aggregated_key=aggregated_key,
        failed_key=failed_key,
        pareto_key=pareto_key,
    )

    # Auto-fire completion notifications (issue #628 criterion #3).
    # Best-effort: a notification failure is logged and never reverts
    # the status transition above (criterion #4). The wrapper catches
    # defensively in addition to each backend's own error handling.
    try:
        _notify_campaign_completion(rec)
    except Exception as exc:  # pragma: no cover — defensive double net
        log.warning(
            "Campaign %s auto-notify raised (suppressed, status unchanged): %s",
            campaign_id,
            exc,
            exc_info=True,
        )

    n_total = result.ok_count + result.failed_count
    return CoordinatorAggregateResponse(
        campaign_id=campaign_id,
        aggregator_job_id=f"{campaign_id}-aggregator",
        status="complete",
        ok_count=result.ok_count,
        failed_count=result.failed_count,
        total_count=n_total,
        aggregated_results_key=aggregated_key,
        failed_simulations_key=failed_key,
        pareto_front_key=pareto_key,
        message=(
            f"Aggregated {n_total} samples: {result.ok_count} ok, "
            f"{result.failed_count} failed. Artifacts written to "
            f"{campaign_id}/_aggregated/."
        ),
    )


async def poll_array_job_to_completion(
    campaign_id: str,
    *,
    initial_delay: float = 5.0,
    max_delay: float = 60.0,
    max_attempts: int = 1000,
    batch_client: Any | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> CoordinatorArrayCompleteResponse:
    """Exponential-backoff polling fallback for array-completion detection.

    Used when the EventBridge webhook is absent (or as a belt-and-braces
    backup). Polls ``describe_jobs`` for the campaign's array parent with
    exponential backoff starting at ``initial_delay`` seconds, doubling each
    iteration up to ``max_delay`` seconds (default 5s -> 60s per issue #626).
    On the first poll that reports all children terminal, performs the exact
    same ``running -> aggregating`` transition as the webhook and returns.

    The two paths are idempotent: if the webhook already advanced the campaign,
    the first poll sees ``status == "aggregating"`` and returns immediately
    without re-transitioning.

    Parameters
    ----------
    campaign_id
        Campaign to poll. Must have a stored ``array_job_id``.
    initial_delay
        Seconds to wait before the first re-poll (default 5).
    max_delay
        Backoff cap in seconds (default 60).
    max_attempts
        Maximum number of poll iterations before giving up. At the default
        5s->60s schedule this is many hours; lower it in tests.
    batch_client
        Optional injected ``boto3`` Batch client (for tests). If ``None`` a
        client is built per poll via :func:`_batch_client`.
    sleep
        Async sleep function (for tests: inject an instant fake).

    Returns
    -------
    CoordinatorArrayCompleteResponse
        The completion breakdown + transition flag.

    Raises
    ------
    LookupError
        If the campaign or its array job is missing.
    TimeoutError
        If the array does not reach a terminal state within ``max_attempts``.
    """
    rec = _campaigns.get(campaign_id)
    if rec is None:
        raise LookupError(f"Campaign {campaign_id} not found")
    array_job_id: str | None = rec.get("array_job_id")
    if not array_job_id:
        raise LookupError(f"Campaign {campaign_id} has no associated array job")

    delay = initial_delay
    last_completion = ArrayCompletion(False, 0, 0, 0, 0)
    for attempt in range(1, max_attempts + 1):
        # Idempotency: if the webhook (or a prior poll) already advanced us,
        # stop immediately.
        if rec.get("status", "pending") in _TERMINAL_OR_AGGREGATING:
            job = _describe_array_job(array_job_id, client=batch_client)
            last_completion = _parse_array_completion(job)
            return _build_complete_response(rec, array_job_id, last_completion, False)

        try:
            job = _describe_array_job(array_job_id, client=batch_client)
        except ArrayJobLookupError as exc:
            # Transient AWS errors must not abort the poll loop; back off and retry.
            log.warning(
                "Campaign %s poll attempt %d failed: %s (retrying in %.1fs)",
                campaign_id,
                attempt,
                exc,
                delay,
            )
            last_completion = ArrayCompletion(False, 0, 0, 0, 0)
            await sleep(delay)
            delay = min(delay * 2, max_delay)
            continue

        last_completion = _parse_array_completion(job)
        transitioned = _apply_completion_transition(rec, last_completion, source="poll")
        if transitioned or last_completion.complete:
            return _build_complete_response(rec, array_job_id, last_completion, transitioned)

        log.debug(
            "Campaign %s array job %s not yet complete: %d succeeded, %d failed, "
            "%d pending of %d (attempt %d, next poll in %.1fs)",
            campaign_id,
            array_job_id,
            last_completion.succeeded,
            last_completion.failed,
            last_completion.pending,
            last_completion.total,
            attempt,
            delay,
        )
        await sleep(delay)
        delay = min(delay * 2, max_delay)

    raise TimeoutError(
        f"Campaign {campaign_id} array job {array_job_id} did not reach a "
        f"terminal state within {max_attempts} poll attempts."
    )
