"""Campaign CRUD and per-sample result endpoints (issue #267).

Provides:
  - GET  /api/v1/campaigns                     — list all campaigns
  - POST /api/v1/campaigns                     — create (and optionally launch) a campaign
  - GET  /api/v1/campaigns/{campaign_id}       — campaign status
  - GET  /api/v1/campaigns/{campaign_id}/samples    — per-sample results
  - GET  /api/v1/campaigns/{campaign_id}/samples/{sample_id} — individual sample
  - POST /api/v1/campaigns/{campaign_id}/cancel — cancel running campaign
"""

from __future__ import annotations

import contextlib
import json
import logging
import threading
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, Query, Request

if TYPE_CHECKING:
    from osimflow.executors.base import BaseExecutor

from osimflow.api.schemas import (
    CampaignCancelResponse,
    CampaignComparisonResponse,
    CampaignCreateRequest,
    CampaignCreateResponse,
    CampaignDetailResponse,
    CampaignListResponse,
    CampaignSummary,
    SampleDetailResponse,
    SampleListResponse,
    SampleSummary,
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
    background thread.  The server must be running with
    ``read_only=False`` (``--enable-writes``) to use auto_start.
    """
    if getattr(request.app.state, "read_only", True):
        raise HTTPException(
            status_code=403,
            detail="Campaign creation requires --enable-writes mode",
        )

    base = _campaigns_base_dir(request)
    base.mkdir(parents=True, exist_ok=True)

    # Generate campaign ID and output directory
    campaign_id = f"campaign-{uuid.uuid4().hex[:8]}"
    outdir = Path(body.outdir).resolve() if body.outdir is not None else base / campaign_id

    outdir.mkdir(parents=True, exist_ok=True)

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

    Returns 403 in read-only mode.  Returns 409 if the campaign is not
    currently running.
    """
    if getattr(request.app.state, "read_only", True):
        raise HTTPException(
            status_code=403,
            detail="Campaign cancellation requires --enable-writes mode",
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

    return CampaignCancelResponse(
        campaign_id=campaign_id,
        status="stopping",
    )
