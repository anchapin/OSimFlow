"""Pydantic response models for the OSimFlow REST API (issue #267).

Provides type-safe response schemas for campaign CRUD, per-sample
result, and file management endpoints.  Models are read-only projections
of the data stored in ``run.json`` and on-disk artefacts.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Campaign list / status
# ---------------------------------------------------------------------------


class CampaignSummary(BaseModel):
    """Lightweight campaign entry returned by ``GET /api/v1/campaigns``."""

    campaign_id: str
    status: str = Field(description="running | completed | unknown")
    started_at: float | None = None
    finished_at: float | None = None
    n_samples: int = 0
    n_succeeded: int = 0
    n_failed: int = 0


class CampaignListResponse(BaseModel):
    """Envelope for the campaign list endpoint."""

    campaigns: list[CampaignSummary]
    total: int


class CampaignDetailResponse(BaseModel):
    """Full campaign status returned by ``GET /api/v1/campaigns/{id}``."""

    campaign_id: str
    status: str
    started_at: float | None = None
    finished_at: float | None = None
    elapsed_s: float | None = None
    config: dict[str, Any] | None = Field(default=None, description="config_summary from run.json")
    summary: dict[str, Any] | None = Field(
        default=None, description="n_samples / n_succeeded / n_failed"
    )
    quality_summary: dict[str, Any] | None = None
    baseline_comparison: dict[str, Any] | None = None
    total_cost_usd: float | None = None
    spot_savings_usd: float | None = None
    steps: list[dict[str, Any]] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Per-sample results
# ---------------------------------------------------------------------------


class SampleSummary(BaseModel):
    """One row in the per-sample results list."""

    sample_id: str
    status: str
    elapsed_s: float
    error_summary: str | None = None
    generation: int | None = None
    worker_id: str | None = None
    cost_usd: float | None = None


class SampleListResponse(BaseModel):
    """Paginated envelope for sample list endpoint."""

    samples: list[SampleSummary]
    total: int
    page: int
    per_page: int


class SampleDetailResponse(BaseModel):
    """Full per-sample detail including KPIs and log file paths."""

    sample_id: str
    status: str
    elapsed_s: float
    kpis: dict[str, Any] | None = None
    log_files: dict[str, str] = Field(default_factory=dict)
    apply_exit_code: int = 0
    sim_exit_code: int = 0
    extract_exit_code: int = 0
    eplusout_sql: str | None = None
    error_summary: str | None = None
    generation: int | None = None
    worker_id: str | None = None
    cost_usd: float | None = None


# ---------------------------------------------------------------------------
# Campaign creation
# ---------------------------------------------------------------------------


class CampaignCreateRequest(BaseModel):
    """Request body for ``POST /api/v1/campaigns``.

    Mirrors the subset of ``CampaignConfig`` fields that are sensible to
    set via the API.  Paths are relative to the server's working directory
    or absolute.

    Executor selection is controlled by the ``executor`` field.  When
    ``executor`` is not ``"local"``, the corresponding executor-specific
    fields must also be provided.
    """

    input_variables: str = Field(description="Path to variables.yml")
    template_sim_package: str = Field(description="Path to the template simulation package")
    n_samples: int = Field(ge=1, description="Number of LHS samples")
    openstudio_version: str = Field(default="3.11.0")
    executor: str = Field(
        default="local",
        description="Executor type: local | slurm | aws_batch | azure_batch | google_batch | dask_jobqueue | nomad | pbs | kubernetes",
    )
    algorithm: str = Field(default="lhs")
    outdir: str | None = Field(default=None, description="Output dir (auto-generated if omitted)")
    archive_intermediates: bool = False
    auto_start: bool = Field(
        default=False, description="Launch campaign immediately after creation"
    )
    max_workers: int = Field(
        default=4, description="Parallelism for local executor (ignored for remote executors)"
    )
    # Slurm parameters (used when executor="slurm")
    slurm_partition: str | None = Field(
        default=None, description="Slurm partition (e.g. short, gpu)"
    )
    slurm_account: str | None = Field(default=None, description="Slurm account/project")
    slurm_qos: str | None = Field(default=None, description="Slurm QoS (requires submitit >= 1.5)")
    slurm_constraint: str | None = Field(default=None, description="Slurm constraint (e.g. gpu)")
    slurm_gres: str | None = Field(default=None, description="Slurm generic resources (e.g. gpu:1)")
    slurm_real: bool = Field(
        default=False, description="Submit to real Slurm cluster (default: debug mode)"
    )
    # AWS Batch parameters (used when executor="aws_batch")
    aws_batch_queue: str | None = Field(default=None, description="AWS Batch job queue")
    aws_batch_job_definition: str | None = Field(
        default=None, description="AWS Batch job definition name"
    )
    aws_batch_max_spot_price_usd: float | None = Field(
        default=None, description="Spot price ceiling (USD/vCPU-hour)"
    )
    aws_batch_fallback_to_on_demand: bool = Field(
        default=False, description="Fall back to on-demand when Spot price exceeds ceiling"
    )
    aws_batch_max_retries: int = Field(default=3, description="Max Spot interruption retries")
    ecr_repository: str | None = Field(
        default=None, description="ECR repository URI for OpenStudio images"
    )
    # Azure Batch parameters (used when executor="azure_batch")
    azure_batch_account_name: str | None = Field(
        default=None, description="Azure Batch account name"
    )
    azure_batch_account_url: str | None = Field(default=None, description="Azure Batch account URL")
    azure_batch_pool_id: str | None = Field(default=None, description="Azure Batch pool ID")
    azure_batch_location: str | None = Field(default=None, description="Azure region/location")
    azure_use_spot: bool = Field(default=False, description="Use Azure Spot/low-priority VMs")
    azure_fallback_to_on_demand: bool = Field(
        default=False, description="Fall back to on-demand when Spot retries exhausted"
    )
    azure_max_retries: int = Field(default=3, description="Max Azure Spot VM retries")
    # Google Cloud Batch parameters (used when executor="google_batch")
    google_batch_project_id: str | None = Field(default=None, description="Google Cloud project ID")
    google_batch_region: str | None = Field(default=None, description="Google Cloud region")
    google_batch_service_account: str | None = Field(
        default=None, description="Google Cloud service account email"
    )
    google_use_spot: bool = Field(default=False, description="Use Google Spot/preemptible VMs")
    google_fallback_to_on_demand: bool = Field(
        default=False, description="Fall back to on-demand when preemptible retries exhausted"
    )
    google_max_retries: int = Field(default=3, description="Max Google preemptible VM retries")
    # Kubernetes parameters (used when executor="kubernetes")
    kubernetes_namespace: str | None = Field(default=None, description="Kubernetes namespace")
    kubernetes_poll_interval_s: float | None = Field(
        default=None, description="K8s Job poll interval (seconds)"
    )
    kubernetes_max_poll_interval_s: float | None = Field(
        default=None, description="K8s Job max poll interval (seconds)"
    )
    # Nomad parameters (used when executor="nomad")
    nomad_address: str | None = Field(default=None, description="Nomad cluster HTTP address")
    nomad_datacentre: str | None = Field(
        default=None, description="Nomad datacentre (default: dc1)"
    )
    nomad_tls: bool = Field(default=False, description="Enable TLS for Nomad HTTP API")
    nomad_tls_verify: bool = Field(
        default=True, description="Verify TLS certificates for Nomad (default: True)"
    )
    nomad_cert: str | None = Field(
        default=None, description="Path to client certificate PEM for Nomad mTLS"
    )
    nomad_key: str | None = Field(
        default=None, description="Path to client private key PEM for Nomad mTLS"
    )
    nomad_ca_cert: str | None = Field(
        default=None, description="Path to CA certificate PEM for Nomad mTLS"
    )
    # PBS parameters (used when executor="pbs")
    pbs_server: str | None = Field(default=None, description="PBS server/cluster address")
    pbs_queue: str | None = Field(default=None, description="PBS queue name")
    pbs_real: bool = Field(
        default=False, description="Submit to real PBS cluster (default: debug mode)"
    )
    # Dask-JobQueue parameters (used when executor="dask_jobqueue")
    dask_cluster_type: str | None = Field(
        default=None, description="Dask cluster backend: slurm | pbs | kubernetes"
    )
    dask_min_workers: int | None = Field(default=None, description="Minimum Dask workers")
    dask_max_workers: int | None = Field(default=None, description="Maximum Dask workers")
    dask_cpus_per_worker: int | None = Field(default=None, description="CPUs per Dask worker")
    dask_memory_per_worker: str | None = Field(
        default=None, description="Memory per Dask worker (e.g. 4GiB)"
    )
    dask_walltime: str | None = Field(
        default=None, description="Walltime for Dask cluster jobs (e.g. 02:00:00)"
    )
    dask_queue: str | None = Field(default=None, description="HPC queue/partition for Dask workers")
    dask_project: str | None = Field(
        default=None, description="HPC project/account for Dask workers"
    )


class CampaignCreateResponse(BaseModel):
    """Response for campaign creation."""

    campaign_id: str
    outdir: str
    status: str = Field(description="created | running")


# ---------------------------------------------------------------------------
# Campaign cancellation
# ---------------------------------------------------------------------------


class CampaignCancelResponse(BaseModel):
    """Response for campaign cancellation."""

    campaign_id: str
    status: str = Field(description="stopping")


# ---------------------------------------------------------------------------
# File management (issue #273)
# ---------------------------------------------------------------------------


class FileInfo(BaseModel):
    """Metadata for a single uploaded file."""

    file_id: str = Field(description="Unique file identifier (UUID)")
    filename: str = Field(description="Original filename")
    category: str = Field(description="File category: seed_model | measure | weather | config")
    size_bytes: int = Field(description="File size in bytes")
    path: str = Field(description="Relative storage path under uploads/")


class FileUploadResponse(BaseModel):
    """Response for a successful file upload."""

    file_id: str
    filename: str
    category: str
    size_bytes: int
    path: str


class FileListResponse(BaseModel):
    """Envelope for the file listing endpoint."""

    files: list[FileInfo]
    total: int


class FileDeleteResponse(BaseModel):
    """Response for file deletion."""

    file_id: str
    status: str = Field(description="deleted")


# ---------------------------------------------------------------------------
# Variable management (issue #347)
# ---------------------------------------------------------------------------


class VariableSummary(BaseModel):
    """Lightweight variable entry returned by ``GET /api/v1/variables``."""

    name: str
    distribution: str
    description: str | None = None


class VariableListResponse(BaseModel):
    """Envelope for the variable list endpoint."""

    variables: list[VariableSummary]
    total: int


class VariableDetailResponse(BaseModel):
    """Full variable detail returned by ``GET /api/v1/variables/{name}``."""

    name: str
    distribution: str
    description: str | None = None
    min: float | None = None
    max: float | None = None
    mean: float | None = None
    sigma: float | None = None
    mode: float | None = None
    values: list[Any] | None = None
    alpha: float | None = None
    beta: float | None = None
    rate: float | None = None
    target: str | None = None
    mapping: dict[str, Any] | None = None


class VariableUpdateRequest(BaseModel):
    """Request body for ``PUT /api/v1/variables/{name}``.

    All fields are optional — only provided fields are updated.
    """

    name: str | None = Field(default=None, description="New variable name")
    distribution: str | None = Field(default=None, description="Distribution type")
    description: str | None = Field(default=None)
    min: float | None = None
    max: float | None = None
    mean: float | None = None
    sigma: float | None = None
    mode: float | None = None
    values: list[Any] | None = None
    alpha: float | None = None
    beta: float | None = None
    rate: float | None = None
    target: str | None = None
    mapping: dict[str, Any] | None = None


class VariableDeleteResponse(BaseModel):
    """Response for variable deletion."""

    name: str
    status: str = Field(description="deleted")


# Measure management (issue #348)
# ---------------------------------------------------------------------------


class MeasureArgument(BaseModel):
    """A single argument accepted by a measure."""

    name: str = Field(description="Argument variable name")
    display_name: str = Field(description="Human-readable argument name")
    description: str | None = Field(default=None, description="What this argument controls")
    argument_type: str = Field(
        default="String",
        description="OpenStudio argument type: String | Double | Integer | Boolean | Choice",
    )
    default_value: Any = Field(default=None, description="Default value in the workflow")
    required: bool = Field(default=False, description="Whether this argument is required")
    valid_choices: list[str] | None = Field(
        default=None, description="Valid choices for Choice arguments"
    )
    units: str | None = Field(default=None, description="Physical units, if applicable")
    measure_dir_name: str = Field(
        default="", description="Measure directory name this argument belongs to"
    )


class MeasureInfo(BaseModel):
    """Summary information for a single measure."""

    measure_dir_name: str = Field(description="Directory name of the measure")
    display_name: str = Field(description="Human-readable measure name")
    description: str | None = Field(default=None, description="What the measure does")
    measure_type: str = Field(
        default="Model",
        description="OpenStudio measure type: Model | EnergyPlus | Reporting",
    )
    arguments: list[MeasureArgument] = Field(
        default_factory=list, description="Arguments accepted by this measure"
    )


class MeasureListResponse(BaseModel):
    """Envelope for the measure list endpoint."""

    measures: list[MeasureInfo] = Field(default_factory=list)
    total: int
    source: str = Field(
        description="Source of measure information: workflow.osw | template_package"
    )
