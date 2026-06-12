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
    """

    input_variables: str = Field(description="Path to variables.yml")
    template_sim_package: str = Field(description="Path to the template simulation package")
    n_samples: int = Field(ge=1, description="Number of LHS samples")
    openstudio_version: str = Field(default="3.11.0")
    executor: str = Field(default="local")
    algorithm: str = Field(default="lhs")
    outdir: str | None = Field(default=None, description="Output dir (auto-generated if omitted)")
    archive_intermediates: bool = False
    auto_start: bool = Field(
        default=False, description="Launch campaign immediately after creation"
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
