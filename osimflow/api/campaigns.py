"""Campaign CRUD and per-sample result endpoints (issue #267, #395).

Provides:
  - GET  /api/v1/campaigns                           — list all campaigns
  - POST /api/v1/campaigns                           — create (and optionally launch) a campaign
  - GET  /api/v1/campaigns/compare                   — compare two campaigns (legacy, issue #386)
  - POST /api/v1/campaigns/compare                   — multi-campaign comparison (issue #404)
  - GET  /api/v1/campaigns/{campaign_id}             — campaign status
  - GET  /api/v1/campaigns/{campaign_id}/download    — download campaign artifacts zip (issue #555)
  - GET  /api/v1/campaigns/{campaign_id}/samples     — per-sample results
  - GET  /api/v1/campaigns/{campaign_id}/samples/{sample_id} — individual sample
  - GET  /api/v1/campaigns/{campaign_id}/samples/{sample_id}/results/{filename} — download result file
  - DELETE /api/v1/campaigns/{campaign_id}/samples/{sample_id}/results/{filename} — delete result file
  - POST /api/v1/campaigns/{campaign_id}/cancel      — cancel running campaign
  - POST /api/v1/campaigns/{campaign_id}/pause      — pause a running campaign (soft-stop, issue #553)
  - POST /api/v1/campaigns/{campaign_id}/resume     — resume a paused campaign (issue #553)
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import logging
import threading
import time
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response, StreamingResponse

if TYPE_CHECKING:
    from osimflow.executors.base import BaseExecutor

from osimflow.api.auth import get_user_permission
from osimflow.api.schemas import (
    BatchUploadRequest,
    BatchUploadResponse,
    CampaignCancelResponse,
    CampaignComparisonEntry,
    CampaignComparisonResponse,
    CampaignCreateRequest,
    CampaignCreateResponse,
    CampaignDetailResponse,
    CampaignListResponse,
    CampaignPauseResponse,
    CampaignResumeResponse,
    CampaignSummary,
    CompareCampaignsPostRequest,
    KpiComparisonRow,
    KpiMetricStats,
    MultiCampaignComparisonResponse,
    SampleDetailResponse,
    SampleListResponse,
    SampleRequeueResponse,
    SampleSummary,
)
from osimflow.audit import AuditLogger, api_actor_from_request
from osimflow.validation import (
    ValidationError,
    sanitize_filename,
    sanitize_sample_id,
    validate_path_within_base,
)

log = logging.getLogger("osimflow.api.campaigns")

campaigns_router = APIRouter()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _campaigns_base_dir(request: Request) -> Path:
    """Return the campaigns base directory from app state.

    Falls back to ``outdir`` when ``campaigns_base_dir`` is not set
    (backward compatibility for single-campaign setups).
    """
    base: Path | None = getattr(request.app.state, "campaigns_base_dir", None)
    if base is not None:
        return base
    outdir: Path | None = request.app.state.outdir
    if outdir is not None:
        return outdir
    raise HTTPException(status_code=503, detail="No output directory configured")


def _load_campaign_json(campaign_dir: Path) -> dict[str, Any]:
    """Load ``run.json`` from a campaign directory.

    Raises :class:`HTTPException` with 404 if the file does not exist.
    """
    run_json_path = campaign_dir / "run.json"
    if not run_json_path.exists():
        raise HTTPException(status_code=404, detail="Campaign run.json not found")
    try:
        raw: Any = json.loads(run_json_path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to read run.json: {exc}",
        ) from exc
    return raw  # type: ignore[no-any-return]


def _derive_status(data: dict[str, Any]) -> str:
    """Derive a human-readable campaign status from run.json data."""
    if data.get("finished_at") is not None:
        return "completed"
    # If started_at exists but no finished_at, it's running.
    if data.get("started_at") is not None:
        return "running"
    return "unknown"


def _campaign_dir_from_id(base: Path, campaign_id: str) -> Path:
    """Resolve a campaign_id to its on-disk directory.

    The campaign_id is the directory name under the campaigns base dir.
    """
    # Prevent directory traversal
    if "/" in campaign_id or "\\" in campaign_id or ".." in campaign_id:
        raise HTTPException(status_code=400, detail="Invalid campaign_id")
    campaign_dir = base / campaign_id
    if not campaign_dir.is_dir():
        raise HTTPException(
            status_code=404,
            detail=f"Campaign '{campaign_id}' not found",
        )
    return campaign_dir


# ---------------------------------------------------------------------------
# GET /api/v1/campaigns — list all campaigns
# ---------------------------------------------------------------------------


@campaigns_router.get("/api/v1/campaigns", response_model=CampaignListResponse)
async def list_campaigns(request: Request) -> CampaignListResponse:
    """List all campaigns by scanning the campaigns base directory for run.json files."""
    base = _campaigns_base_dir(request)
    campaigns: list[CampaignSummary] = []

    if not base.is_dir():
        return CampaignListResponse(campaigns=[], total=0)

    for entry in sorted(base.iterdir()):
        if not entry.is_dir():
            continue
        run_json_path = entry / "run.json"
        if not run_json_path.exists():
            continue
        try:
            data = json.loads(run_json_path.read_text())
        except (json.JSONDecodeError, OSError):
            log.warning("skipping corrupt run.json in %s", entry)
            continue

        summary_data = data.get("summary", {})
        campaigns.append(
            CampaignSummary(
                campaign_id=data.get("campaign_id", entry.name),
                status=_derive_status(data),
                started_at=data.get("started_at"),
                finished_at=data.get("finished_at"),
                n_samples=summary_data.get("n_samples", 0),
                n_succeeded=summary_data.get("n_succeeded", 0),
                n_failed=summary_data.get("n_failed", 0),
            )
        )

    return CampaignListResponse(campaigns=campaigns, total=len(campaigns))


# ---------------------------------------------------------------------------
# POST /api/v1/campaigns — create a campaign
# ---------------------------------------------------------------------------


@campaigns_router.post("/api/v1/campaigns", response_model=CampaignCreateResponse, status_code=201)
async def create_campaign(
    body: CampaignCreateRequest,
    request: Request,
) -> CampaignCreateResponse:
    """Create a new campaign directory and optionally launch it.

    When ``auto_start`` is ``True`` the campaign is launched in a
    background thread.  The user must have admin role (issue #395).
    """
    if not get_user_permission(request, "admin"):
        raise HTTPException(
            status_code=403,
            detail="Admin permission required for campaign creation",
        )

    base = _campaigns_base_dir(request)
    base.mkdir(parents=True, exist_ok=True)

    # Generate campaign ID and output directory
    campaign_id = f"campaign-{uuid.uuid4().hex[:8]}"
    outdir = Path(body.outdir).resolve() if body.outdir is not None else base / campaign_id

    outdir.mkdir(parents=True, exist_ok=True)

    # Audit log: campaign created via API (issue #439)
    audit = AuditLogger(outdir=outdir)
    actor = api_actor_from_request(request)
    audit.api_campaign_created(
        campaign_id=campaign_id,
        actor=actor,
        executor=body.executor or "local",
        n_samples=body.n_samples,
    )

    # Write a config stub so the campaign is discoverable
    config_stub: dict[str, Any] = {
        "campaign_id": campaign_id,
        "input_variables": body.input_variables,
        "template_sim_package": body.template_sim_package,
        "n_samples": body.n_samples,
        "openstudio_version": body.openstudio_version,
        "executor": body.executor,
        "algorithm": body.algorithm,
        "archive_intermediates": body.archive_intermediates,
    }
    config_path = outdir / "campaign_config.json"
    config_path.write_text(json.dumps(config_stub, indent=2))

    status = "created"

    if body.auto_start:
        # Write an initial run.json to make the campaign discoverable
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

        # Launch the campaign in a background thread
        _launch_campaign_background(request, campaign_id, body, outdir)
        status = "running"

    return CampaignCreateResponse(
        campaign_id=campaign_id,
        outdir=str(outdir),
        status=status,
    )


def _build_executor_from_request(body: CampaignCreateRequest) -> BaseExecutor:
    """Build the correct executor from a campaign creation request body.

    Mirrors the CLI ``_build_executor`` logic in ``osimflow.__main__``.
    """
    from osimflow.executors import (  # noqa: PLC0415
        AWSBatchExecutor,
        AzureBatchExecutor,
        DaskJobQueueExecutor,
        GoogleBatchExecutor,
        KubernetesExecutor,
        LocalExecutor,
        NomadExecutor,
        PBSExecutor,
        SlurmExecutor,
    )

    _executors: dict[str, tuple[type[BaseExecutor], dict[str, object]]] = {
        "local": (
            LocalExecutor,
            {"max_workers": body.max_workers},
        ),
        "slurm": (
            SlurmExecutor,
            {
                "partition": body.slurm_partition or "short",
                "account": body.slurm_account,
                "cpus_per_task": 2,
                "mem_gb": 4,
                "time_h": 2,
                "debug": not body.slurm_real,
                "qos": body.slurm_qos,
                "constraint": body.slurm_constraint,
                "gres": body.slurm_gres,
            },
        ),
        "aws_batch": (
            AWSBatchExecutor,
            {
                "job_queue": body.aws_batch_queue or "osimflow-batch-queue",
                "job_definition": body.aws_batch_job_definition,
                "max_spot_price_usd": body.aws_batch_max_spot_price_usd,
                "fallback_to_on_demand": body.aws_batch_fallback_to_on_demand,
                "max_retries": body.aws_batch_max_retries,
                "ecr_repository": body.ecr_repository,
            },
        ),
        "azure_batch": (
            AzureBatchExecutor,
            {
                "account_name": body.azure_batch_account_name,
                "account_url": body.azure_batch_account_url,
                "pool_id": body.azure_batch_pool_id or "osimflow-pool",
                "location": body.azure_batch_location or "eastus",
                "use_spot": body.azure_use_spot,
                "fallback_to_on_demand": body.azure_fallback_to_on_demand,
                "max_retries": body.azure_max_retries,
            },
        ),
        "google_batch": (
            GoogleBatchExecutor,
            {
                "project_id": body.google_batch_project_id,
                "region": body.google_batch_region or "us-central1",
                "batch_service_account": body.google_batch_service_account,
                "use_spot": body.google_use_spot,
                "fallback_to_on_demand": body.google_fallback_to_on_demand,
                "max_retries": body.google_max_retries,
            },
        ),
        "kubernetes": (
            KubernetesExecutor,
            {
                "namespace": body.kubernetes_namespace or "default",
                "poll_interval_s": body.kubernetes_poll_interval_s or 5.0,
                "max_poll_interval_s": body.kubernetes_max_poll_interval_s or 60.0,
            },
        ),
        "nomad": (
            NomadExecutor,
            {
                "address": body.nomad_address,
                "datacentre": body.nomad_datacentre or "dc1",
                "verify_tls": body.nomad_tls_verify,
                "tls": body.nomad_tls,
                "cert": Path(body.nomad_cert) if body.nomad_cert else None,
                "key": Path(body.nomad_key) if body.nomad_key else None,
                "ca_cert": Path(body.nomad_ca_cert) if body.nomad_ca_cert else None,
            },
        ),
        "pbs": (
            PBSExecutor,
            {
                "server": body.pbs_server,
                "queue": body.pbs_queue,
                "debug": not body.pbs_real,
            },
        ),
        "dask_jobqueue": (
            DaskJobQueueExecutor,
            {
                "cluster_type": body.dask_cluster_type or "slurm",
                "min_workers": body.dask_min_workers or 0,
                "max_workers": body.dask_max_workers or 10,
                "cpus_per_worker": body.dask_cpus_per_worker or 2,
                "memory_per_worker": body.dask_memory_per_worker or "4GiB",
                "walltime": body.dask_walltime or "02:00:00",
                "queue": body.dask_queue,
                "project": body.dask_project,
            },
        ),
    }

    entry = _executors.get(body.executor)
    if entry is None:
        raise ValueError(f"unknown executor: {body.executor}")
    executor_cls, kwargs = entry
    return executor_cls(**kwargs)


def _launch_campaign_background(
    request: Request,
    campaign_id: str,
    body: CampaignCreateRequest,
    outdir: Path,
) -> None:
    """Launch a campaign in a background thread (fire-and-forget).

    This is deliberately lightweight — it starts the campaign but does
    not stream results back.  Clients monitor progress via SSE or polling.
    """

    def _run() -> None:
        try:
            from osimflow import Campaign  # noqa: PLC0415
            from osimflow.config import CampaignConfig  # noqa: PLC0415

            cfg = CampaignConfig(
                input_variables=Path(body.input_variables).resolve(),
                template_sim_package=Path(body.template_sim_package).resolve(),
                n_samples=body.n_samples,
                outdir=outdir,
                openstudio_version=body.openstudio_version,
                archive_intermediates=body.archive_intermediates,
                algorithm=body.algorithm,
                slurm_qos=body.slurm_qos,
                slurm_constraint=body.slurm_constraint,
                slurm_gres=body.slurm_gres,
                aws_batch_max_spot_price_usd=body.aws_batch_max_spot_price_usd,
                aws_batch_fallback_to_on_demand=body.aws_batch_fallback_to_on_demand,
                aws_batch_max_retries=body.aws_batch_max_retries,
                ecr_repository=body.ecr_repository,
                azure_batch_account_name=body.azure_batch_account_name,
                azure_batch_account_url=body.azure_batch_account_url,
                azure_batch_pool_id=body.azure_batch_pool_id or "osimflow-pool",
                azure_batch_location=body.azure_batch_location or "eastus",
                azure_use_spot=body.azure_use_spot,
                azure_fallback_to_on_demand=body.azure_fallback_to_on_demand,
                azure_max_retries=body.azure_max_retries,
                google_batch_project_id=body.google_batch_project_id,
                google_batch_region=body.google_batch_region or "us-central1",
                google_batch_service_account=body.google_batch_service_account,
                google_use_spot=body.google_use_spot,
                google_fallback_to_on_demand=body.google_fallback_to_on_demand,
                google_max_retries=body.google_max_retries,
                nomad_tls=body.nomad_tls,
                nomad_cert=Path(body.nomad_cert) if body.nomad_cert else None,
                nomad_key=Path(body.nomad_key) if body.nomad_key else None,
                nomad_ca_cert=Path(body.nomad_ca_cert) if body.nomad_ca_cert else None,
            )
            executor = _build_executor_from_request(body)
            campaign = Campaign(cfg, executor=executor)
            campaign.run()
        except Exception:
            log.exception("background campaign %s failed", campaign_id)

    thread = threading.Thread(target=_run, daemon=True, name=f"campaign-{campaign_id}")
    thread.start()
    log.info("launched campaign %s in background thread", campaign_id)


# ---------------------------------------------------------------------------
# GET /api/v1/campaigns/compare — compare two campaigns side by side
# ---------------------------------------------------------------------------


@campaigns_router.get(
    "/api/v1/campaigns/compare",
    response_model=CampaignComparisonResponse,
)
async def compare_campaigns(
    request: Request,
    id1: str = Query(..., description="First campaign ID"),
    id2: str = Query(..., description="Second campaign ID"),
) -> CampaignComparisonResponse:
    """Compare two campaigns side by side by their campaign IDs.

    Both campaigns must exist under the campaigns base directory.
    Returns ``null`` for whichever campaign is not found.
    """
    base = _campaigns_base_dir(request)

    left: CampaignDetailResponse | None = None
    right: CampaignDetailResponse | None = None

    # Load left campaign
    try:
        left_dir = _campaign_dir_from_id(base, id1)
        left_data = _load_campaign_json(left_dir)
        left = CampaignDetailResponse(
            campaign_id=left_data.get("campaign_id", id1),
            status=_derive_status(left_data),
            started_at=left_data.get("started_at"),
            finished_at=left_data.get("finished_at"),
            elapsed_s=left_data.get("elapsed_s"),
            config=left_data.get("config") or left_data.get("config_summary"),
            summary=left_data.get("summary"),
            quality_summary=left_data.get("quality_summary"),
            baseline_comparison=left_data.get("baseline_comparison"),
            total_cost_usd=left_data.get("total_cost_usd"),
            spot_savings_usd=left_data.get("spot_savings_usd"),
            steps=left_data.get("steps", []),
        )
    except HTTPException:
        pass  # Campaign not found — return None

    # Load right campaign
    try:
        right_dir = _campaign_dir_from_id(base, id2)
        right_data = _load_campaign_json(right_dir)
        right = CampaignDetailResponse(
            campaign_id=right_data.get("campaign_id", id2),
            status=_derive_status(right_data),
            started_at=right_data.get("started_at"),
            finished_at=right_data.get("finished_at"),
            elapsed_s=right_data.get("elapsed_s"),
            config=right_data.get("config") or right_data.get("config_summary"),
            summary=right_data.get("summary"),
            quality_summary=right_data.get("quality_summary"),
            baseline_comparison=right_data.get("baseline_comparison"),
            total_cost_usd=right_data.get("total_cost_usd"),
            spot_savings_usd=right_data.get("spot_savings_usd"),
            steps=right_data.get("steps", []),
        )
    except HTTPException:
        pass  # Campaign not found — return None

    return CampaignComparisonResponse(left=left, right=right)


# ---------------------------------------------------------------------------
# POST /api/v1/campaigns/compare — multi-campaign comparison (issue #404)
# ---------------------------------------------------------------------------


def _resolve_by_outdir(outdir: str) -> Path | None:
    """Resolve an explicit outdir path to a campaign directory."""
    path = Path(outdir)
    if not path.is_absolute():
        path = Path.cwd() / path
    return path if path.is_dir() else None


def _resolve_by_campaign_id(request: Request, campaign_id: str) -> Path | None:
    """Resolve a campaign_id via the registry, then campaigns base dir.

    Resolution order:
    1. Campaign registry (if configured in ``app.state.registry``).
    2. ``campaigns_base_dir / campaign_id``.
    3. ``outdir / campaign_id`` (single-campaign fallback).
    """
    # Try the registry first (if configured).
    registry: Any = getattr(request.app.state, "registry", None)
    if registry is not None:
        try:
            record = registry.get_campaign(campaign_id)
            if record is not None and record.outdir:
                reg_outdir = Path(record.outdir)
                if reg_outdir.is_dir():
                    return reg_outdir
        except Exception:  # noqa: BLE001
            log.debug("registry lookup failed for %s", campaign_id, exc_info=True)

    # Fall back to campaigns base directory lookup.
    candidates: list[Path] = []
    base: Path | None = getattr(request.app.state, "campaigns_base_dir", None)
    if base is not None:
        candidates.append(base / campaign_id)
    outdir_state: Path | None = request.app.state.outdir
    if outdir_state is not None:
        candidates.append(outdir_state / campaign_id)

    for candidate in candidates:
        if candidate.is_dir():
            return candidate

    return None


def _resolve_campaign_dir(
    request: Request,
    campaign_id: str | None,
    outdir: str | None,
) -> Path | None:
    """Resolve a campaign identifier to its on-disk directory.

    If ``outdir`` is given, resolve that path directly.
    If ``campaign_id`` is given, look it up via the registry or the
    campaigns base directory.

    Returns ``None`` when the campaign cannot be found.
    """
    # Outdir takes precedence — direct path resolution.
    if outdir is not None:
        return _resolve_by_outdir(outdir)

    if campaign_id is None:
        return None

    # Prevent directory traversal in campaign_id.
    if "/" in campaign_id or "\\" in campaign_id or ".." in campaign_id:
        return None

    return _resolve_by_campaign_id(request, campaign_id)


def _extract_sample_summary(data: dict[str, Any]) -> dict[str, Any]:
    """Extract sample counts and success rate from run.json data.

    When ``per_sample`` is present and non-empty, counts are derived
    from it (authoritative source).  Otherwise the ``summary`` field
    is used.
    """
    per_sample: list[dict[str, Any]] = data.get("per_sample", [])
    if per_sample:
        n_total = len(per_sample)
        n_succeeded = sum(1 for s in per_sample if s.get("status") == "ok")
        n_failed = n_total - n_succeeded
    else:
        summary = data.get("summary") or {}
        n_total = summary.get("n_samples", 0)
        n_succeeded = summary.get("n_succeeded", 0)
        n_failed = summary.get("n_failed", 0)

    success_rate: float | None = None
    if n_total > 0:
        success_rate = round(n_succeeded / n_total, 4)
    return {
        "n_samples": n_total,
        "n_succeeded": n_succeeded,
        "n_failed": n_failed,
        "success_rate": success_rate,
    }


def _compute_kpi_stats(campaign_dir: Path) -> dict[str, KpiMetricStats]:
    """Read aggregated_results.csv and compute per-column statistics.

    Only numeric columns are included.  Non-numeric columns (e.g.
    ``sample_id``) are skipped.
    """
    csv_path = campaign_dir / "aggregated_results.csv"
    if not csv_path.exists():
        return {}

    try:
        import pandas as pd  # noqa: PLC0415
    except ImportError:
        return {}

    try:
        df = pd.read_csv(csv_path)
    except Exception:  # noqa: BLE001
        log.debug("failed to read %s", csv_path, exc_info=True)
        return {}

    stats: dict[str, KpiMetricStats] = {}
    for col in df.columns:
        if col.lower() in ("sample_id", "sample", "id", "Unnamed: 0"):
            continue
        series = df[col]
        if not pd.api.types.is_numeric_dtype(series):
            continue
        clean = series.dropna()
        if clean.empty:
            continue
        stats[col] = KpiMetricStats(
            mean=round(float(clean.mean()), 6),
            min=round(float(clean.min()), 6),
            max=round(float(clean.max()), 6),
            std=round(float(clean.std()), 6) if len(clean) > 1 else 0.0,
            count=int(len(clean)),
        )
    return stats


def _build_kpi_comparison(
    entries: list[CampaignComparisonEntry],
) -> list[KpiComparisonRow]:
    """Align KPI means across campaigns for easy comparison.

    Collects the union of all KPI metric names across all campaigns and
    produces one ``KpiComparisonRow`` per metric.  ``values[i]`` is the
    mean of that KPI in ``entries[i]``, or ``None`` if not available.
    """
    all_metrics: list[str] = []
    seen: set[str] = set()
    for entry in entries:
        for metric in entry.kpi_stats:
            if metric not in seen:
                seen.add(metric)
                all_metrics.append(metric)

    rows: list[KpiComparisonRow] = []
    for metric in all_metrics:
        values: list[float | None] = []
        for entry in entries:
            stat = entry.kpi_stats.get(metric)
            values.append(stat.mean if stat is not None else None)
        rows.append(KpiComparisonRow(metric=metric, values=values))
    return rows


@campaigns_router.post(
    "/api/v1/campaigns/compare",
    response_model=MultiCampaignComparisonResponse,
)
async def compare_campaigns_post(
    body: CompareCampaignsPostRequest,
    request: Request,
) -> MultiCampaignComparisonResponse:
    """Compare two or more campaigns by registry ID, campaign directory name,
    or explicit outdir path (issue #404).

    Each entry in the request body may specify ``campaign_id`` (resolved
    via the registry or campaigns base directory) **or** ``outdir`` (a
    direct filesystem path to the campaign output directory).

    The response includes per-campaign metadata, step timing, sample
    counts and success rates, per-KPI aggregated statistics, and an
    aligned KPI comparison table for easy charting.

    Campaigns that cannot be found are included in the response with
    ``found=False`` and an ``error`` message — the endpoint never raises
    404 so callers can compare even when some campaigns are missing.
    """
    entries: list[CampaignComparisonEntry] = []

    for identifier in body.campaigns:
        label = identifier.campaign_id or identifier.outdir or "<empty>"

        if identifier.campaign_id is None and identifier.outdir is None:
            entries.append(
                CampaignComparisonEntry(
                    identifier=label,
                    found=False,
                    error="No campaign_id or outdir provided",
                )
            )
            continue

        campaign_dir = _resolve_campaign_dir(
            request,
            identifier.campaign_id,
            identifier.outdir,
        )

        if campaign_dir is None:
            entries.append(
                CampaignComparisonEntry(
                    identifier=label,
                    found=False,
                    error=f"Campaign '{label}' not found",
                )
            )
            continue

        # Load run.json.
        run_json_path = campaign_dir / "run.json"
        if not run_json_path.exists():
            entries.append(
                CampaignComparisonEntry(
                    identifier=label,
                    found=False,
                    error=f"run.json not found in {campaign_dir}",
                )
            )
            continue

        try:
            data: dict[str, Any] = json.loads(run_json_path.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            entries.append(
                CampaignComparisonEntry(
                    identifier=label,
                    found=True,
                    error=f"Failed to read run.json: {exc}",
                )
            )
            continue

        kpi_stats = _compute_kpi_stats(campaign_dir)

        entries.append(
            CampaignComparisonEntry(
                identifier=label,
                found=True,
                campaign_id=data.get("campaign_id", identifier.campaign_id),
                status=_derive_status(data),
                started_at=data.get("started_at"),
                finished_at=data.get("finished_at"),
                elapsed_s=data.get("elapsed_s"),
                config=data.get("config") or data.get("config_summary"),
                step_timing=data.get("steps", []),
                sample_summary=_extract_sample_summary(data),
                kpi_stats=kpi_stats,
            )
        )

    kpi_comparison = _build_kpi_comparison(entries)

    return MultiCampaignComparisonResponse(
        campaigns=entries,
        kpi_comparison=kpi_comparison,
        total=len(entries),
    )


# ---------------------------------------------------------------------------
# GET /api/v1/campaigns/{campaign_id} — campaign status
# ---------------------------------------------------------------------------


@campaigns_router.get("/api/v1/campaigns/{campaign_id}", response_model=CampaignDetailResponse)
async def get_campaign_status(campaign_id: str, request: Request) -> CampaignDetailResponse:
    """Get detailed status for a specific campaign."""
    base = _campaigns_base_dir(request)
    campaign_dir = _campaign_dir_from_id(base, campaign_id)
    data = _load_campaign_json(campaign_dir)

    return CampaignDetailResponse(
        campaign_id=data.get("campaign_id", campaign_id),
        status=_derive_status(data),
        started_at=data.get("started_at"),
        finished_at=data.get("finished_at"),
        elapsed_s=data.get("elapsed_s"),
        config=data.get("config") or data.get("config_summary"),
        summary=data.get("summary"),
        quality_summary=data.get("quality_summary"),
        baseline_comparison=data.get("baseline_comparison"),
        total_cost_usd=data.get("total_cost_usd"),
        spot_savings_usd=data.get("spot_savings_usd"),
        steps=data.get("steps", []),
    )


# ---------------------------------------------------------------------------
# GET /api/v1/campaigns/{campaign_id}/samples — per-sample results
# ---------------------------------------------------------------------------


@campaigns_router.get("/api/v1/campaigns/{campaign_id}/samples", response_model=SampleListResponse)
async def list_campaign_samples(
    campaign_id: str,
    request: Request,
    page: int = Query(1, ge=1, description="Page number (1-indexed)"),
    per_page: int = Query(50, ge=1, le=500, description="Items per page (max 500)"),
) -> SampleListResponse:
    """Get paginated per-sample results for a campaign."""
    base = _campaigns_base_dir(request)
    campaign_dir = _campaign_dir_from_id(base, campaign_id)
    data = _load_campaign_json(campaign_dir)

    all_samples: list[dict[str, Any]] = data.get("per_sample", [])
    total = len(all_samples)
    start = (page - 1) * per_page
    end = start + per_page
    page_samples = all_samples[start:end]

    samples = [
        SampleSummary(
            sample_id=s.get("sample_id", ""),
            status=s.get("status", "unknown"),
            elapsed_s=s.get("elapsed_s", 0.0),
            error_summary=s.get("error_summary"),
            generation=s.get("generation"),
            worker_id=s.get("worker_id"),
            cost_usd=s.get("cost_usd"),
        )
        for s in page_samples
    ]

    return SampleListResponse(
        samples=samples,
        total=total,
        page=page,
        per_page=per_page,
    )


# ---------------------------------------------------------------------------
# GET /api/v1/campaigns/{campaign_id}/samples/{sample_id} — individual sample
# ---------------------------------------------------------------------------


@campaigns_router.get(
    "/api/v1/campaigns/{campaign_id}/samples/{sample_id}",
    response_model=SampleDetailResponse,
)
async def get_campaign_sample(
    campaign_id: str,
    sample_id: str,
    request: Request,
) -> SampleDetailResponse:
    """Get detailed results for a single sample, including KPIs and log paths."""
    base = _campaigns_base_dir(request)
    campaign_dir = _campaign_dir_from_id(base, campaign_id)
    data = _load_campaign_json(campaign_dir)

    # Find the sample in per_sample
    sample: dict[str, Any] | None = None
    for s in data.get("per_sample", []):
        if s.get("sample_id") == sample_id:
            sample = s
            break

    if sample is None:
        raise HTTPException(
            status_code=404,
            detail=f"Sample '{sample_id}' not found in campaign '{campaign_id}'",
        )

    # Load KPIs and log paths from the sim directory
    kpis: dict[str, Any] | None = None
    log_files: dict[str, str] = {}
    sim_dir = campaign_dir / "work" / "sim" / sample_id
    if sim_dir.is_dir():
        for kpi_name in ("kpi.json", "kpis.json"):
            kpi_path = sim_dir / kpi_name
            if kpi_path.exists():
                with contextlib.suppress(json.JSONDecodeError, OSError):
                    kpis = json.loads(kpi_path.read_text())
                break
        for log_name in ("stdout.log", "stderr.log"):
            log_path = sim_dir / log_name
            if log_path.exists():
                log_files[log_name] = str(log_path)

    return SampleDetailResponse(
        sample_id=sample_id,
        status=sample.get("status", "unknown"),
        elapsed_s=sample.get("elapsed_s", 0.0),
        kpis=kpis,
        log_files=log_files,
        apply_exit_code=sample.get("apply_exit_code", 0),
        sim_exit_code=sample.get("sim_exit_code", 0),
        extract_exit_code=sample.get("extract_exit_code", 0),
        eplusout_sql=sample.get("eplusout_sql"),
        error_summary=sample.get("error_summary"),
        generation=sample.get("generation"),
        worker_id=sample.get("worker_id"),
        cost_usd=sample.get("cost_usd"),
    )


# ---------------------------------------------------------------------------
# POST /api/v1/campaigns/{campaign_id}/cancel — cancel a campaign
# ---------------------------------------------------------------------------


@campaigns_router.post(
    "/api/v1/campaigns/{campaign_id}/cancel",
    response_model=CampaignCancelResponse,
)
async def cancel_campaign(
    campaign_id: str,
    request: Request,
) -> CampaignCancelResponse:
    """Cancel a running campaign by writing a ``.stop`` flag file.

    Returns 403 if the user lacks write permission (issue #395).
    Returns 409 if the campaign is not currently running.
    """
    if not get_user_permission(request, "readwrite"):
        raise HTTPException(
            status_code=403,
            detail="read-only mode: write permission required for campaign cancellation",
        )

    base = _campaigns_base_dir(request)
    campaign_dir = _campaign_dir_from_id(base, campaign_id)

    # Check campaign status — only running campaigns can be cancelled
    run_json_path = campaign_dir / "run.json"
    if run_json_path.exists():
        data = json.loads(run_json_path.read_text())
        if data.get("finished_at") is not None:
            raise HTTPException(
                status_code=409,
                detail=f"Campaign '{campaign_id}' has already completed",
            )

    # Write the .stop flag
    stop_file = campaign_dir / ".stop"
    stop_file.write_text(json.dumps({"requested_at": time.time()}))
    log.info("stop flag written to %s", stop_file)

    # Audit log: campaign cancelled via API (issue #439)
    audit = AuditLogger(outdir=campaign_dir)
    actor = api_actor_from_request(request)
    audit.api_campaign_cancelled(campaign_id=campaign_id, actor=actor)

    return CampaignCancelResponse(
        campaign_id=campaign_id,
        status="stopping",
    )


# ---------------------------------------------------------------------------
# POST /api/v1/campaigns/{campaign_id}/pause — pause a campaign (issue #553)
# ---------------------------------------------------------------------------


@campaigns_router.post(
    "/api/v1/campaigns/{campaign_id}/pause",
    response_model=CampaignPauseResponse,
)
async def pause_campaign(
    campaign_id: str,
    request: Request,
) -> CampaignPauseResponse:
    """Pause a running campaign by writing a ``.pause`` flag file.

    Running samples complete normally; only new submissions are skipped.
    This is a soft-stop mechanism that allows the campaign to be resumed
    later from where it left off.

    Returns 403 if the user lacks write permission (issue #395).
    Returns 409 if the campaign is not currently running.
    """
    if not get_user_permission(request, "readwrite"):
        raise HTTPException(
            status_code=403,
            detail="read-only mode: write permission required for campaign pause",
        )

    base = _campaigns_base_dir(request)
    campaign_dir = _campaign_dir_from_id(base, campaign_id)

    # Check campaign status — only running campaigns can be paused
    run_json_path = campaign_dir / "run.json"
    if run_json_path.exists():
        data = json.loads(run_json_path.read_text())
        if data.get("finished_at") is not None:
            raise HTTPException(
                status_code=409,
                detail=f"Campaign '{campaign_id}' has already completed",
            )
        if data.get("status") == "paused":
            raise HTTPException(
                status_code=409,
                detail=f"Campaign '{campaign_id}' is already paused",
            )

    # Write the .pause flag
    pause_file = campaign_dir / ".pause"
    pause_file.write_text(json.dumps({"requested_at": time.time()}))
    log.info("pause flag written to %s", pause_file)

    # Update run.json with paused status
    trace = {"status": "paused", "paused_at": time.time()}
    run_json_path.write_text(json.dumps(trace))
    log.info("paused trace written to run.json")

    # Audit log: campaign paused via API (issue #439, #553)
    audit = AuditLogger(outdir=campaign_dir)
    actor = api_actor_from_request(request)
    audit.api_campaign_paused(campaign_id=campaign_id, actor=actor)

    return CampaignPauseResponse(
        campaign_id=campaign_id,
        status="paused",
    )


# ---------------------------------------------------------------------------
# POST /api/v1/campaigns/{campaign_id}/resume — resume a paused campaign (issue #553)
# ---------------------------------------------------------------------------


@campaigns_router.post(
    "/api/v1/campaigns/{campaign_id}/resume",
    response_model=CampaignResumeResponse,
)
async def resume_campaign(
    campaign_id: str,
    request: Request,
) -> CampaignResumeResponse:
    """Resume a paused campaign by removing the ``.pause`` flag file.

    Clears the paused status and allows new sample submissions to proceed.

    Returns 403 if the user lacks write permission (issue #395).
    Returns 409 if the campaign is not currently paused.
    """
    if not get_user_permission(request, "readwrite"):
        raise HTTPException(
            status_code=403,
            detail="read-only mode: write permission required for campaign resume",
        )

    base = _campaigns_base_dir(request)
    campaign_dir = _campaign_dir_from_id(base, campaign_id)

    # Check campaign status — only paused campaigns can be resumed
    run_json_path = campaign_dir / "run.json"
    if run_json_path.exists():
        data = json.loads(run_json_path.read_text())
        if data.get("finished_at") is not None:
            raise HTTPException(
                status_code=409,
                detail=f"Campaign '{campaign_id}' has already completed",
            )
        if data.get("status") != "paused":
            raise HTTPException(
                status_code=409,
                detail=f"Campaign '{campaign_id}' is not currently paused",
            )

    # Remove the .pause flag
    pause_file = campaign_dir / ".pause"
    if pause_file.exists():
        pause_file.unlink()
        log.info("pause flag removed from %s", pause_file)

    # Update run.json with running status
    run_json_path.write_text(json.dumps({"status": "running", "paused_at": None}))
    log.info("resume trace written to run.json")

    # Audit log: campaign resumed via API (issue #439, #553)
    audit = AuditLogger(outdir=campaign_dir)
    actor = api_actor_from_request(request)
    audit.api_campaign_resumed(campaign_id=campaign_id, actor=actor)

    return CampaignResumeResponse(
        campaign_id=campaign_id,
        status="running",
    )


# ---------------------------------------------------------------------------
# GET /api/v1/campaigns/{campaign_id}/download — campaign artifact bundle
# ---------------------------------------------------------------------------

# Files included in the bundle (evaluated at request time).
_BUNDLE_ROOT_FILES = frozenset(
    {"run.json", "samples.json", "aggregated_results.csv", "failed_simulations.csv"}
)
_BUNDLE_PLOT_GLOB = "*.png"
_BUNDLE_SQL_GLOB = "eplusout.sql"


def _iter_sample_bundle(sample_dir: Path, include_sql: bool) -> tuple[tuple[str, Path], ...]:
    """Yield (archive_path, file_path) pairs for a single sample directory."""
    results: list[tuple[str, Path]] = []
    for kpi_name in ("kpi.json", "kpis.json"):
        kpi_file = sample_dir / kpi_name
        if kpi_file.is_file():
            results.append((f"samples/{sample_dir.name}/{kpi_name}", kpi_file))
    if include_sql:
        sql_file = sample_dir / _BUNDLE_SQL_GLOB
        if sql_file.is_file():
            results.append((f"samples/{sample_dir.name}/{_BUNDLE_SQL_GLOB}", sql_file))
    return tuple(results)


def _iter_campaign_bundle(
    campaign_dir: Path,
    *,
    include_sql: bool = False,
) -> tuple[tuple[str, Path], ...]:
    """Iterate over files to include in the campaign bundle.

    Yields ``(archive_path, file_path)`` pairs for streaming into a zip.
    Skips missing files silently.  Per-sample KPI JSONs and plot files
    are discovered by glob.
    """
    # Root-level artifacts
    for name in _BUNDLE_ROOT_FILES:
        f = campaign_dir / name
        if f.is_file():
            yield (name, f)

    # Per-sample KPI JSONs and eplusout.sql
    work_dir = campaign_dir / "work"
    sim_dir = work_dir / "sim"
    if sim_dir.is_dir():
        for sample_dir in sim_dir.iterdir():
            if sample_dir.is_dir():
                yield from _iter_sample_bundle(sample_dir, include_sql)

    # Plot files — campaign root
    for plot_file in campaign_dir.glob(_BUNDLE_PLOT_GLOB):
        if plot_file.is_file() and plot_file.name != "run.json":
            yield (plot_file.name, plot_file)
    # Plot files — plots/ subdirectory
    plots_dir = campaign_dir / "plots"
    if plots_dir.is_dir():
        yield from (
            (f"plots/{p.name}", p) for p in plots_dir.glob(_BUNDLE_PLOT_GLOB) if p.is_file()
        )


@campaigns_router.get("/api/v1/campaigns/{campaign_id}/download")
async def download_campaign_bundle(
    campaign_id: str,
    request: Request,
    include_sql: bool = Query(
        False,
        description=(
            "Include eplusout.sql files from per-sample directories. "
            "Only present when --archive_intermediates was used during the campaign run."
        ),
    ),
) -> StreamingResponse:
    """Download a bundled ZIP archive of all campaign artifacts.

    The bundle includes:
    - ``run.json`` — monitoring trace
    - ``samples.json`` — variable mappings
    - ``aggregated_results.csv`` — KPI table
    - ``failed_simulations.csv`` — failure summary
    - Per-sample KPI JSON files (``kpi.json`` / ``kpis.json``)
    - PNG plot files from the campaign root and ``plots/`` subdirectory
    - ``eplusout.sql`` files when ``include_sql=true`` (requires
      ``--archive_intermediates`` during the campaign run)

    Returns a ZIP with ``Content-Disposition: attachment;
    filename="campaign-{campaign_id}.zip"``.
    """
    base = _campaigns_base_dir(request)
    campaign_dir = _campaign_dir_from_id(base, campaign_id)

    safe_name = "".join(c for c in campaign_id if c.isalnum() or c in ("-", "_"))
    archive_name = f"campaign-{safe_name}.zip"

    def generate_zip() -> io.BytesIO:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            for archive_path, file_path in _iter_campaign_bundle(
                campaign_dir, include_sql=include_sql
            ):
                try:
                    with file_path.open("rb") as f:
                        data = f.read()
                    zf.writestr(archive_path, data)
                except (OSError, zipfile.BadZipFile) as exc:
                    log.warning("failed to add %s to bundle: %s", file_path, exc)
        buf.seek(0)
        return buf

    loop = asyncio.get_running_loop()
    with ThreadPoolExecutor(max_workers=1) as pool:
        zip_bytes = await loop.run_in_executor(pool, lambda: generate_zip().read())

    return StreamingResponse(
        io.BytesIO(zip_bytes),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{archive_name}"',
            "Content-Length": str(len(zip_bytes)),
        },
    )


# ---------------------------------------------------------------------------
# GET /api/v1/campaigns/{campaign_id}/samples/{sample_id}/results/{filename}
# DELETE /api/v1/campaigns/{campaign_id}/samples/{sample_id}/results/{filename}
# ---------------------------------------------------------------------------


def _result_file_media_type(filename: str) -> str:
    """Return the media type for a result filename based on its extension."""
    if filename.endswith(".sql"):
        return "application/x-sqlite3"
    if filename.endswith((".err", ".log", ".osw")):
        return "text/plain; charset=utf-8"
    return "application/octet-stream"


@campaigns_router.get(
    "/api/v1/campaigns/{campaign_id}/samples/{sample_id}/results/{filename:path}",
)
async def download_sample_result_file(
    campaign_id: str,
    sample_id: str,
    filename: str,
    request: Request,
) -> Response:
    """Download a per-sample result file.

    Result files live at ``{campaign_outdir}/work/sim/{sample_id}/{filename}``.
    Returns 400 if the filename contains path traversal sequences.
    Returns 404 if the campaign, sample, or file is not found.
    """
    base = _campaigns_base_dir(request)
    campaign_dir = _campaign_dir_from_id(base, campaign_id)

    # Validate sample_id to prevent path traversal.
    try:
        safe_sample_id = sanitize_sample_id(sample_id)
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # Validate filename to prevent path traversal.
    try:
        safe_filename = sanitize_filename(filename)
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # Resolve the result file path.
    result_file = campaign_dir / "work" / "sim" / safe_sample_id / safe_filename
    try:
        validate_path_within_base(result_file.resolve(), campaign_dir.resolve())
    except ValidationError:
        raise HTTPException(status_code=400, detail="Invalid result file path") from None

    if not result_file.is_file():
        raise HTTPException(
            status_code=404,
            detail=f"Result file '{safe_filename}' not found for sample '{safe_sample_id}'",
        )

    media_type = _result_file_media_type(safe_filename)
    return FileResponse(
        result_file,
        media_type=media_type,
        filename=safe_filename,
    )


@campaigns_router.delete(
    "/api/v1/campaigns/{campaign_id}/samples/{sample_id}/results/{filename:path}",
)
async def delete_sample_result_file(
    campaign_id: str,
    sample_id: str,
    filename: str,
    request: Request,
) -> Response:
    """Delete a per-sample result file.

    Result files live at ``{campaign_outdir}/work/sim/{sample_id}/{filename}``.
    Returns 400 if the filename contains path traversal sequences.
    Returns 403 if the server is in read-only mode.
    Returns 404 if the campaign, sample, or file is not found.
    """
    if not get_user_permission(request, "readwrite"):
        raise HTTPException(
            status_code=403,
            detail="read-only mode: write permission required for file deletion",
        )

    base = _campaigns_base_dir(request)
    campaign_dir = _campaign_dir_from_id(base, campaign_id)

    # Validate sample_id to prevent path traversal.
    try:
        safe_sample_id = sanitize_sample_id(sample_id)
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # Validate filename to prevent path traversal.
    try:
        safe_filename = sanitize_filename(filename)
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # Resolve the result file path.
    result_file = campaign_dir / "work" / "sim" / safe_sample_id / safe_filename
    try:
        validate_path_within_base(result_file.resolve(), campaign_dir.resolve())
    except ValidationError:
        raise HTTPException(status_code=400, detail="Invalid result file path") from None

    if not result_file.is_file():
        raise HTTPException(
            status_code=404,
            detail=f"Result file '{safe_filename}' not found for sample '{safe_sample_id}'",
        )

    result_file.unlink()
    log.info("deleted result file: %s", result_file)
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# POST /api/v1/campaigns/{campaign_id}/samples/batch_upload — batch upload
# ---------------------------------------------------------------------------


@campaigns_router.post(
    "/api/v1/campaigns/{campaign_id}/samples/batch_upload",
    response_model=BatchUploadResponse,
)
async def batch_upload_samples(
    campaign_id: str,
    body: BatchUploadRequest,
    request: Request,
) -> BatchUploadResponse:
    """Upload a batch of pre-generated datapoints to a campaign.

    Accepts a JSON body with a ``samples`` array. Each entry must provide
    a ``values`` dict (variable name → value). ``kpi_values`` and
    ``generation`` are optional.

    Each sample is assigned a new unique ``sample_id`` and appended to the
    campaign's ``run.json`` ``per_sample`` list. The campaign's
    ``summary.n_samples`` is updated accordingly.

    This endpoint requires ``readwrite`` permission (issue #395).
    """
    if not get_user_permission(request, "readwrite"):
        raise HTTPException(
            status_code=403,
            detail="read-only mode: write permission required for batch upload",
        )

    base = _campaigns_base_dir(request)
    campaign_dir = _campaign_dir_from_id(base, campaign_id)

    # Load existing run.json
    run_json_path = campaign_dir / "run.json"
    if not run_json_path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"Campaign '{campaign_id}' has no run.json — campaign may not have started",
        )

    try:
        data: dict[str, Any] = json.loads(run_json_path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to read run.json: {exc}",
        ) from exc

    # Determine the next sample number suffix
    per_sample: list[dict[str, Any]] = data.get("per_sample", [])
    existing_ids = {s.get("sample_id", "") for s in per_sample}
    max_num = 0
    for sid in existing_ids:
        # Match sample_0001, sample_001, etc.
        for part in sid.split("_"):
            if part.isdigit():
                max_num = max(max_num, int(part))
                break

    # Build new sample entries
    now = time.time()
    new_sample_ids: list[str] = []
    for idx, item in enumerate(body.samples):
        sample_num = max_num + idx + 1
        # Generate a unique sample_id
        new_id = f"sample_{sample_num:04d}"
        while new_id in existing_ids:
            sample_num += 1
            new_id = f"sample_{sample_num:04d}"
            existing_ids.add(new_id)  # prevent further collisions in this batch

        sample_entry: dict[str, Any] = {
            "sample_id": new_id,
            "status": "pending",
            "generation": item.generation if item.generation is not None else 1,
            "created_at": now,
        }
        if item.kpi_values is not None:
            sample_entry["kpi_values"] = item.kpi_values
        per_sample.append(sample_entry)
        new_sample_ids.append(new_id)

    # Update run.json
    data["per_sample"] = per_sample
    # Update summary counts
    summary: dict[str, Any] = data.get("summary", {})
    summary["n_samples"] = len(per_sample)
    data["summary"] = summary

    try:
        run_json_path.write_text(json.dumps(data, indent=2))
    except OSError as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to write run.json: {exc}",
        ) from exc

    log.info(
        "batch_upload: added %d samples to campaign %s: %s",
        len(new_sample_ids),
        campaign_id,
        new_sample_ids,
    )

    return BatchUploadResponse(
        campaign_id=campaign_id,
        added=len(new_sample_ids),
        sample_ids=new_sample_ids,
        detail=f"Added {len(new_sample_ids)} sample(s) to campaign '{campaign_id}'",
    )


# ---------------------------------------------------------------------------
# POST /api/v1/campaigns/{campaign_id}/samples/{sample_id}/requeue
# ---------------------------------------------------------------------------


@campaigns_router.post(
    "/api/v1/campaigns/{campaign_id}/samples/{sample_id}/requeue",
    response_model=SampleRequeueResponse,
)
async def requeue_sample(
    campaign_id: str,
    sample_id: str,
    request: Request,
) -> SampleRequeueResponse:
    """Mark a completed or failed sample for re-running.

    Creates a new ``pending`` sample derived from the specified sample's
    parameters. The new sample is appended to ``run.json.per_sample`` and
    can be picked up by the executor on the campaign's next run.

    Only ``COMPLETED`` or ``FAILED`` samples can be requeued. Returns 404
    if the sample does not exist, and 422 if the sample is not in a
    requeueable state.

    This endpoint requires ``readwrite`` permission (issue #395).
    """
    if not get_user_permission(request, "readwrite"):
        raise HTTPException(
            status_code=403,
            detail="read-only mode: write permission required for requeue",
        )

    base = _campaigns_base_dir(request)
    campaign_dir = _campaign_dir_from_id(base, campaign_id)

    # Load run.json
    run_json_path = campaign_dir / "run.json"
    if not run_json_path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"Campaign '{campaign_id}' has no run.json",
        )

    try:
        data: dict[str, Any] = json.loads(run_json_path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to read run.json: {exc}",
        ) from exc

    # Find the source sample
    per_sample: list[dict[str, Any]] = data.get("per_sample", [])
    source_sample: dict[str, Any] | None = None
    for s in per_sample:
        if s.get("sample_id") == sample_id:
            source_sample = s
            break

    if source_sample is None:
        raise HTTPException(
            status_code=404,
            detail=f"Sample '{sample_id}' not found in campaign '{campaign_id}'",
        )

    status = source_sample.get("status", "")
    if status not in ("ok", "completed", "failed"):
        raise HTTPException(
            status_code=422,
            detail=(
                f"Sample '{sample_id}' has status '{status}' — "
                "only COMPLETED/FAILED samples can be requeued"
            ),
        )

    # Build new sample id: {original}_reanalyze_{n}
    reanalyze_count = source_sample.get("reanalyze_count", 0)
    new_id = f"{sample_id}_reanalyze_{reanalyze_count + 1}"

    # Update source sample's reanalyze_count
    source_sample["reanalyze_count"] = reanalyze_count + 1

    # Build new sample entry
    new_entry: dict[str, Any] = {
        "sample_id": new_id,
        "status": "pending",
        "generation": source_sample.get("generation", 1),
        "created_at": time.time(),
        "original_sample_id": sample_id,
    }

    per_sample.append(new_entry)
    data["per_sample"] = per_sample
    # Update summary counts
    summary: dict[str, Any] = data.get("summary", {})
    summary["n_samples"] = len(per_sample)
    data["summary"] = summary

    try:
        run_json_path.write_text(json.dumps(data, indent=2))
    except OSError as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to write run.json: {exc}",
        ) from exc

    log.info(
        "requeue: campaign=%s original=%s new=%s",
        campaign_id,
        sample_id,
        new_id,
    )

    return SampleRequeueResponse(
        campaign_id=campaign_id,
        original_sample_id=sample_id,
        new_sample_id=new_id,
        status="pending",
        detail=f"Created reanalysis sample '{new_id}' derived from '{sample_id}'",
    )
