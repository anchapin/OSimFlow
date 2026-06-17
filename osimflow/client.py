"""Typed async Python client for the OSimFlow REST API (issue #433).

This module provides a high-level, fully-typed async client wrapper around
the OSimFlow REST API using ``httpx``.  It is designed for external
integrators who want to monitor campaigns programmatically without
manually constructing HTTP calls.

Key features:

* **Async-first** — all methods are ``async`` and use ``httpx.AsyncClient``.
* **Typed responses** — Pydantic models mirror the server schemas, giving
  IDE autocompletion and static-type safety.
* **API-key auth** — pass ``api_key=`` and the client adds the
  ``X-API-Key`` header to every request automatically.
* **Sensible error mapping** — HTTP 4xx/5xx responses are mapped to
  :class:`OSimFlowAPIError` subclasses (authentication, not-found,
  rate-limit, server error) so callers can ``except`` precisely.

Quickstart::

    import asyncio
    from osimflow.client import OSimFlowClient

    async def main():
        async with OSimFlowClient("http://localhost:8000") as client:
            health = await client.health()
            print(health.status)

    asyncio.run(main())

See :class:`OSimFlowClient` for the full method catalogue.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, Field

log = logging.getLogger("osimflow.client")

__all__ = [
    "OSimFlowClient",
    "OSimFlowAPIError",
    "AuthenticationError",
    "NotFoundError",
    "RateLimitError",
    "ServerError",
    # Response models
    "HealthResponse",
    "ReadyResponse",
    "CampaignResponse",
    "StepsResponse",
    "SamplesResponse",
    "SampleDetailResponse",
    "ResultRow",
    "PlotFile",
    "PlotsResponse",
    "ParetoGeneration",
    "ParetoResponse",
    "CampaignStopResponse",
    "CampaignPauseResponse",
    "CampaignResumeResponse",
    "Event",
    "CampaignHealthResponse",
    "StepHealth",
    "SampleCounts",
    "ValidateConfigRequest",
    "ValidateConfigResponse",
    "VariableBatchUpdateItem",
    "VariableBatchUpdateError",
    "VariableBatchUpdateResponse",
]

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class OSimFlowAPIError(Exception):
    """Base exception for all API errors.

    Carries the HTTP status code and the response body (if any).
    """

    def __init__(self, message: str, *, status_code: int, body: str = "") -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class AuthenticationError(OSimFlowAPIError):
    """Raised on HTTP 401 — invalid or missing API key."""


class NotFoundError(OSimFlowAPIError):
    """Raised on HTTP 404 — resource not found."""


class RateLimitError(OSimFlowAPIError):
    """Raised on HTTP 429 — rate limit exceeded.

    ``retry_after`` is parsed from the ``Retry-After`` header when present.
    """

    def __init__(
        self, message: str, *, status_code: int, body: str = "", retry_after: int | None = None
    ) -> None:
        super().__init__(message, status_code=status_code, body=body)
        self.retry_after = retry_after


class ServerError(OSimFlowAPIError):
    """Raised on HTTP 5xx — server-side failure."""


# ---------------------------------------------------------------------------
# Pydantic response models
# ---------------------------------------------------------------------------


class HealthResponse(BaseModel):
    """Response from ``GET /health``."""

    status: str = Field(description="Always 'alive' for a healthy server.")


class ReadyResponse(BaseModel):
    """Response from ``GET /ready``."""

    status: str = Field(description="'ready' or 'not_ready'")
    campaign_id: str | None = None
    reason: str | None = Field(default=None, description="Present when status is 'not_ready'")


class CampaignResponse(BaseModel):
    """Response from ``GET /api/v1/campaign`` — campaign metadata from run.json."""

    campaign_id: str | None = None
    config_summary: dict[str, Any] = Field(default_factory=dict)
    started_at: float | None = None
    finished_at: float | None = None
    baseline_comparison: dict[str, Any] | None = None


class StepTrace(BaseModel):
    """A single DAG step trace entry."""

    step: str | None = None
    cache: str | None = None
    elapsed_s: float | None = None
    exit_code: int | None = None


class StepsResponse(BaseModel):
    """Response from ``GET /api/v1/steps``."""

    steps: list[StepTrace] = Field(default_factory=list)
    total_steps: int = 0


class SampleSummary(BaseModel):
    """A single per-sample summary row."""

    sample_id: str | None = None
    status: str | None = None
    elapsed_s: float | None = None
    error_summary: str | None = None
    generation: int | None = None
    worker_id: str | None = None
    cost_usd: float | None = None


class SamplesResponse(BaseModel):
    """Response from ``GET /api/v1/samples``."""

    samples: list[SampleSummary] = Field(default_factory=list)
    total: int = 0
    page: int = 1
    per_page: int = 50


class SampleDetailResponse(BaseModel):
    """Response from ``GET /api/v1/samples/{sample_id}`` — full sample detail."""

    sample_id: str
    status: str | None = None
    elapsed_s: float | None = None
    kpis: dict[str, Any] | None = None
    log_files: dict[str, str] = Field(default_factory=dict)
    error_summary: str | None = None
    generation: int | None = None
    worker_id: str | None = None
    cost_usd: float | None = None
    apply_exit_code: int = 0
    sim_exit_code: int = 0
    extract_exit_code: int = 0
    eplusout_sql: str | None = None


class ResultRow(BaseModel):
    """A single row from ``GET /api/v1/results`` (aggregated_results.csv).

    The schema is intentionally permissive — columns vary by campaign.
    """

    model_config = {"extra": "allow"}

    sample_id: str | None = None


class PlotFile(BaseModel):
    """Metadata for a single plot file."""

    name: str
    size: int = 0


class PlotsResponse(BaseModel):
    """Response from ``GET /api/v1/plots``."""

    plots: list[PlotFile] = Field(default_factory=list)
    total: int = 0


class ParetoGeneration(BaseModel):
    """A single Pareto front generation entry."""

    model_config = {"extra": "allow"}

    _file: str | None = None


class ParetoResponse(BaseModel):
    """Response from ``GET /api/v1/pareto``."""

    generations: list[ParetoGeneration] = Field(default_factory=list)
    total_generations: int = 0


class CampaignStopResponse(BaseModel):
    """Response from ``POST /api/v1/campaign/stop``."""

    status: str = Field(description="Always 'stopping' on success.")


class CampaignPauseResponse(BaseModel):
    """Response from ``POST /api/v1/campaigns/{campaign_id}/pause``."""

    campaign_id: str
    status: str = Field(description="Always 'paused' on success.")


class CampaignResumeResponse(BaseModel):
    """Response from ``POST /api/v1/campaigns/{campaign_id}/resume``."""

    campaign_id: str
    status: str = Field(description="Always 'running' on success.")


class Event(BaseModel):
    """A single SSE event from ``GET /api/v1/events``.

    ``data`` is parsed as JSON when possible, otherwise kept as a string.
    """

    event: str
    data: Any = None


class StepHealth(BaseModel):
    """A single step status in the campaign health response."""

    step: str | None = None
    cache: str | None = None
    status: str | None = None
    elapsed_s: float | None = None


class SampleCounts(BaseModel):
    """Sample counts in the campaign health response."""

    total: int = 0
    success: int = 0
    failed: int = 0
    cached: int = 0
    running: int = 0


class CampaignHealthResponse(BaseModel):
    """Response from ``GET /api/v1/health`` — campaign health dashboard."""

    campaign_id: str | None = None
    overall_status: str = Field(description="'healthy', 'degraded', or 'unknown'")
    campaign_status: str = Field(description="'running', 'success', 'failed', 'cancelled', ...")
    steps: list[StepHealth] = Field(default_factory=list)
    samples: SampleCounts = Field(default_factory=SampleCounts)
    started_at: float | None = None
    finished_at: float | None = None
    elapsed_s: float | None = None


class ValidateConfigRequest(BaseModel):
    """Request body for ``POST /api/v1/validate``."""

    input_variables: str
    template_sim_package: str
    n_samples: int = 1
    openstudio_version: str = ""
    outdir: str | None = None
    archive_intermediates: bool = False
    algorithm: str = "lhs"
    max_generations: int = 1
    dry_run: bool = False
    skip_preflight: bool = False
    custom_apply_script: str | None = None
    custom_kpi_extractor: str | None = None
    init_script: str | None = None
    finalize_script: str | None = None
    weather_dir: str = "weather"
    project: str = ""
    mlflow_tracking_uri: str | None = None
    redis_url: str | None = None
    slurm_qos: str | None = None
    slurm_constraint: str | None = None
    slurm_gres: str | None = None
    baseline: dict[str, object] | None = None
    objective: dict[str, object] | None = None
    constraints: list[dict[str, object]] | None = None
    max_sample_retries: int = 3
    offline: bool = False
    offline_bundle: str | None = None
    webhook_url: str | None = None
    nomad_tls: bool = False
    nomad_cert: str | None = None
    nomad_key: str | None = None
    nomad_ca_cert: str | None = None
    byos_trust_level: str = "subprocess"
    byos_resource_limits: dict[str, int] | None = None
    observability: str = "none"
    cloudwatch_namespace: str = "OSimFlow"
    cloudwatch_log_group: str | None = None
    log_aggregation_url: str | None = None
    prometheus_port: int = 9090
    otel_endpoint: str | None = None
    registry_path: str | None = None
    task_queue: str = "none"
    dask_scheduler_address: str | None = None
    result_storage_backend: str = "local"
    result_storage_bucket: str = ""
    result_storage_endpoint: str | None = None
    track_costs: bool = False
    aws_batch_max_spot_price_usd: float | None = None
    aws_batch_fallback_to_on_demand: bool = False
    aws_batch_max_retries: int = 3
    aws_batch_on_demand_price: float | None = None
    aws_batch_spot_price: float | None = None
    ecr_repository: str | None = None
    azure_batch_account_name: str | None = None
    azure_batch_account_url: str | None = None
    azure_batch_pool_id: str = "osimflow-pool"
    azure_batch_location: str = "eastus"
    azure_use_spot: bool = False
    azure_fallback_to_on_demand: bool = False
    azure_max_retries: int = 3
    google_batch_project_id: str | None = None
    google_batch_region: str = "us-central1"
    google_batch_service_account: str | None = None
    google_use_spot: bool = False
    google_fallback_to_on_demand: bool = False
    google_max_retries: int = 3
    slurm_cost_per_node_hour: float = 0.10


class ValidateConfigResponse(BaseModel):
    """Response from ``POST /api/v1/validate``."""

    valid: bool
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class VariableBatchUpdateItem(BaseModel):
    """One item in a batch variable update request."""

    name: str
    rename_to: str | None = None
    distribution: str | None = None
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


class VariableBatchUpdateError(BaseModel):
    """Error detail for a single failed variable update in a batch."""

    name: str
    error: str


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


class VariableBatchUpdateResponse(BaseModel):
    """Response from ``POST /api/v1/variables/batch_update``."""

    updated: list[VariableDetailResponse] = Field(default_factory=list)
    errors: list[VariableBatchUpdateError] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

_DEFAULT_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
_DEFAULT_USER_AGENT = "osimflow-client/0.1"


class OSimFlowClient:
    """Typed async client for the OSimFlow REST API.

    Parameters
    ----------
    base_url
        Base URL of the OSimFlow API server, e.g. ``"http://localhost:8000"``.
    api_key
        Optional API key for authentication.  When set, the ``X-API-Key``
        header is added to every request.  Ignored when the server has
        no API keys configured.
    timeout
        Request timeout.  Defaults to 30s connect+read.
    **kwargs
        Additional keyword arguments forwarded to :class:`httpx.AsyncClient`.

    Examples
    --------
    As a context manager (recommended)::

        async with OSimFlowClient("http://localhost:8000", api_key="secret") as c:
            data = await c.get_campaign()

    Manual lifecycle::

        client = OSimFlowClient("http://localhost:8000")
        await client.connect()
        try:
            data = await client.get_campaign()
        finally:
            await client.close()
    """

    def __init__(
        self,
        base_url: str,
        *,
        api_key: str | None = None,
        timeout: httpx.Timeout | float = _DEFAULT_TIMEOUT,
        **kwargs: Any,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout
        self._kwargs = kwargs
        self._client: httpx.AsyncClient | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def _build_headers(self) -> dict[str, str]:
        """Construct the default headers (API key + user-agent)."""
        headers: dict[str, str] = {"User-Agent": _DEFAULT_USER_AGENT}
        if self._api_key:
            headers["X-API-Key"] = self._api_key
        return headers

    async def connect(self) -> None:
        """Create the underlying :class:`httpx.AsyncClient`.

        Called automatically when used as an async context manager.
        Safe to call multiple times — subsequent calls are no-ops.
        """
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                headers=self._build_headers(),
                timeout=self._timeout,
                **self._kwargs,
            )

    async def close(self) -> None:
        """Close the underlying HTTP client and release resources."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self) -> OSimFlowClient:
        await self.connect()
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        await self.close()

    @property
    def http_client(self) -> httpx.AsyncClient:
        """Return the underlying httpx client, connecting lazily if needed."""
        if self._client is None:
            raise RuntimeError(
                "Client is not connected. Use 'async with OSimFlowClient(...)' "
                "or call 'await client.connect()' first."
            )
        return self._client

    # ------------------------------------------------------------------
    # Internal request helper
    # ------------------------------------------------------------------

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> httpx.Response:
        """Execute an HTTP request and map errors to Python exceptions."""
        client = self.http_client
        try:
            resp = await client.request(method, path, params=params, json=json_body)
        except httpx.ConnectError as exc:
            raise OSimFlowAPIError(f"Connection failed: {exc}", status_code=0, body="") from exc
        except httpx.TimeoutException as exc:
            raise OSimFlowAPIError(f"Request timed out: {exc}", status_code=0, body="") from exc

        if resp.status_code >= 400:
            self._raise_for_status(resp)
        return resp

    @staticmethod
    def _raise_for_status(resp: httpx.Response) -> None:
        """Map an HTTP error response to the appropriate exception."""
        body_text = resp.text
        try:
            detail_json = resp.json()
            detail = detail_json.get("detail", body_text)
        except Exception:
            detail = body_text

        status = resp.status_code
        msg = f"HTTP {status}: {detail}"

        if status == 401:
            raise AuthenticationError(msg, status_code=status, body=body_text)
        if status == 403:
            raise AuthenticationError(msg, status_code=status, body=body_text)
        if status == 404:
            raise NotFoundError(msg, status_code=status, body=body_text)
        if status == 429:
            retry_after = resp.headers.get("Retry-After")
            retry_val: int | None = None
            if retry_after:
                try:
                    retry_val = int(retry_after)
                except ValueError:
                    retry_val = None
            raise RateLimitError(msg, status_code=status, body=body_text, retry_after=retry_val)
        if status >= 500:
            raise ServerError(msg, status_code=status, body=body_text)
        # Other 4xx errors
        raise OSimFlowAPIError(msg, status_code=status, body=body_text)

    # ------------------------------------------------------------------
    # Health & readiness
    # ------------------------------------------------------------------

    async def health(self) -> HealthResponse:
        """``GET /health`` — liveness probe."""
        resp = await self._request("GET", "/health")
        return HealthResponse.model_validate(resp.json())

    async def ready(self) -> ReadyResponse:
        """``GET /ready`` — readiness probe."""
        resp = await self._request("GET", "/ready")
        return ReadyResponse.model_validate(resp.json())

    # ------------------------------------------------------------------
    # Campaign health (issue #437)
    # ------------------------------------------------------------------

    async def get_campaign_health(self) -> CampaignHealthResponse:
        """``GET /api/v1/health`` — campaign health dashboard.

        Returns overall status (healthy/degraded/unknown), per-step status,
        sample counts (total/success/failed/running), and timestamps.
        """
        resp = await self._request("GET", "/api/v1/health")
        return CampaignHealthResponse.model_validate(resp.json())

    async def get_campaign_health_details(self) -> dict[str, Any]:
        """``GET /api/v1/health/details`` — full run.json contents."""
        resp = await self._request("GET", "/api/v1/health/details")
        return dict[str, Any](resp.json())

    # ------------------------------------------------------------------
    # Campaign metadata
    # ------------------------------------------------------------------

    async def get_campaign(self) -> CampaignResponse:
        """``GET /api/v1/campaign`` — campaign metadata from run.json."""
        resp = await self._request("GET", "/api/v1/campaign")
        return CampaignResponse.model_validate(resp.json())

    async def get_steps(self) -> StepsResponse:
        """``GET /api/v1/steps`` — DAG step traces."""
        resp = await self._request("GET", "/api/v1/steps")
        return StepsResponse.model_validate(resp.json())

    async def stop_campaign(self) -> CampaignStopResponse:
        """``POST /api/v1/campaign/stop`` — request campaign cancellation.

        Requires read-write permission on the server.
        """
        resp = await self._request("POST", "/api/v1/campaign/stop")
        return CampaignStopResponse.model_validate(resp.json())

    async def pause_campaign(self, campaign_id: str) -> CampaignPauseResponse:
        """``POST /api/v1/campaigns/{campaign_id}/pause`` — pause a running campaign.

        Pauses a running campaign using a soft-stop mechanism. Running samples
        complete normally; only new submissions are skipped.

        Parameters
        ----------
        campaign_id
            The unique campaign identifier.

        Requires read-write permission on the server.
        """
        path = f"/api/v1/campaigns/{campaign_id}/pause"
        resp = await self._request("POST", path)
        return CampaignPauseResponse.model_validate(resp.json())

    async def resume_campaign(self, campaign_id: str) -> CampaignResumeResponse:
        """``POST /api/v1/campaigns/{campaign_id}/resume`` — resume a paused campaign.

        Resumes a paused campaign by removing the pause flag.

        Parameters
        ----------
        campaign_id
            The unique campaign identifier.

        Requires read-write permission on the server.
        """
        path = f"/api/v1/campaigns/{campaign_id}/resume"
        resp = await self._request("POST", path)
        return CampaignResumeResponse.model_validate(resp.json())

    # ------------------------------------------------------------------
    # Samples
    # ------------------------------------------------------------------

    async def get_samples(self, *, page: int = 1, per_page: int = 50) -> SamplesResponse:
        """``GET /api/v1/samples`` — paginated per-sample traces.

        Parameters
        ----------
        page
            Page number (1-indexed).
        per_page
            Items per page (1–500).
        """
        resp = await self._request(
            "GET", "/api/v1/samples", params={"page": page, "per_page": per_page}
        )
        return SamplesResponse.model_validate(resp.json())

    async def get_sample(self, sample_id: str) -> SampleDetailResponse:
        """``GET /api/v1/samples/{sample_id}`` — single sample detail."""
        resp = await self._request("GET", f"/api/v1/samples/{sample_id}")
        return SampleDetailResponse.model_validate(resp.json())

    async def get_sample_log(self, sample_id: str, log_name: str) -> str:
        """``GET /api/v1/samples/{sample_id}/logs/{log_name}`` — raw log content.

        *log_name* must be ``"stdout.log"`` or ``"stderr.log"``.
        Returns the log file content as a string.
        """
        resp = await self._request("GET", f"/api/v1/samples/{sample_id}/logs/{log_name}")
        return resp.text

    # ------------------------------------------------------------------
    # Per-sample result files (issue #559)
    # ------------------------------------------------------------------

    async def download_sample_result_file(
        self,
        campaign_id: str,
        sample_id: str,
        filename: str,
    ) -> httpx.Response:
        """``GET /api/v1/campaigns/{campaign_id}/samples/{sample_id}/results/{filename}`` — download a result file.

        Returns the raw :class:`httpx.Response` so callers can access ``.content``
        directly.  Content-type is determined by the file extension:
        ``.sql`` → ``application/x-sqlite3``, ``.err``/``.log``/``.osw`` →
        ``text/plain; charset=utf-8``, everything else → ``application/octet-stream``.
        """
        path = f"/api/v1/campaigns/{campaign_id}/samples/{sample_id}/results/{filename}"
        resp = await self._request("GET", path)
        return resp

    async def delete_sample_result_file(
        self,
        campaign_id: str,
        sample_id: str,
        filename: str,
    ) -> None:
        """``DELETE /api/v1/campaigns/{campaign_id}/samples/{sample_id}/results/{filename}`` — delete a result file.

        Requires the server to have write permission enabled.
        """
        path = f"/api/v1/campaigns/{campaign_id}/samples/{sample_id}/results/{filename}"
        await self._request("DELETE", path)

    # ------------------------------------------------------------------
    # Results & failures
    # ------------------------------------------------------------------

    async def get_results(self) -> list[ResultRow]:
        """``GET /api/v1/results`` — aggregated results as a list of rows."""
        resp = await self._request("GET", "/api/v1/results")
        return [ResultRow.model_validate(row) for row in resp.json()]

    async def get_failures(self) -> list[ResultRow]:
        """``GET /api/v1/failures`` — failed simulations as a list of rows."""
        resp = await self._request("GET", "/api/v1/failures")
        return [ResultRow.model_validate(row) for row in resp.json()]

    # ------------------------------------------------------------------
    # Pareto & plots
    # ------------------------------------------------------------------

    async def get_pareto(self) -> ParetoResponse:
        """``GET /api/v1/pareto`` — Pareto front generations."""
        resp = await self._request("GET", "/api/v1/pareto")
        return ParetoResponse.model_validate(resp.json())

    async def get_plots(self) -> PlotsResponse:
        """``GET /api/v1/plots`` — list available plot files."""
        resp = await self._request("GET", "/api/v1/plots")
        return PlotsResponse.model_validate(resp.json())

    async def get_plot(self, filename: str) -> bytes:
        """``GET /api/v1/plots/{filename}`` — download a plot PNG.

        Returns the raw image bytes.
        """
        resp = await self._request("GET", f"/api/v1/plots/{filename}")
        return resp.content

    # ------------------------------------------------------------------
    # Error diagnosis
    # ------------------------------------------------------------------

    async def get_sample_error(self, sample_id: str) -> dict[str, Any]:
        """``GET /api/v1/errors/{sample_id}`` — error diagnosis for a failed sample.

        Returns the raw JSON dict.  Keys vary by failure category.
        """
        resp = await self._request("GET", f"/api/v1/errors/{sample_id}")
        data: dict[str, Any] = resp.json()
        return data

    # ------------------------------------------------------------------
    # Campaign artifact bundle (issue #555)
    # ------------------------------------------------------------------

    async def download_campaign(
        self,
        campaign_id: str,
        output_path: Path | str,
        *,
        include_sql: bool = False,
    ) -> None:
        """``GET /api/v1/campaigns/{campaign_id}/download`` — download campaign artifact bundle.

        Fetches the bundled ZIP of all campaign artifacts and saves it to
        *output_path*.  The bundle includes ``run.json``, ``samples.json``,
        ``aggregated_results.csv``, ``failed_simulations.csv``, per-sample
        KPI JSONs, and PNG plot files.  When *include_sql* is ``True``,
        ``eplusout.sql`` files from per-sample directories are also included
        (requires the campaign was run with ``--archive_intermediates``).

        Parameters
        ----------
        campaign_id
            The campaign identifier (directory name under the campaigns
            base directory, or a registry-resolved ID).
        output_path
            Local filesystem path where the ZIP archive will be written.
            Parent directories are created if they do not exist.
        include_sql
            When ``True``, include ``eplusout.sql`` files from per-sample
            directories.  Only set this when the campaign used
            ``--archive_intermediates``, otherwise these files will not
            be present in the bundle.
        """
        params: dict[str, Any] = {}
        if include_sql:
            params["include_sql"] = "1"
        resp = await self.http_client.get(
            f"/api/v1/campaigns/{campaign_id}/download",
            params=params,
            follow_redirects=True,
        )
        if resp.status_code >= 400:
            self._raise_for_status(resp)
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(resp.content)

    # ------------------------------------------------------------------
    # Pre-flight config validation (issue #398)
    # ------------------------------------------------------------------

    async def validate_config(
        self,
        input_variables: str,
        template_sim_package: str,
        n_samples: int,
        openstudio_version: str,
        **kwargs: Any,
    ) -> ValidateConfigResponse:
        """``POST /api/v1/validate`` — pre-flight config validation.

        Validates the supplied config fields without running a campaign.
        Checks variables.yml schema, template package structure, version
        format, sample counts, and script paths.

        Parameters
        ----------
        input_variables
            Path to ``variables.yml``.
        template_sim_package
            Path to the template simulation package directory.
        n_samples
            Number of samples (must be >= 1).
        openstudio_version
            OpenStudio version string (e.g. ``"3.11.0"``).
        **kwargs
            Additional config fields forwarded as-is in the request body.
        """
        body = ValidateConfigRequest(
            input_variables=input_variables,
            template_sim_package=template_sim_package,
            n_samples=n_samples,
            openstudio_version=openstudio_version,
            **kwargs,
        )
        resp = await self._request(
            "POST",
            "/api/v1/validate",
            json_body=body.model_dump(mode="json"),
        )
        return ValidateConfigResponse.model_validate(resp.json())

    # ------------------------------------------------------------------
    # SSE events stream
    # ------------------------------------------------------------------

    async def events(self) -> AsyncIterator[Event]:
        """``GET /api/v1/events`` — Server-Sent Events stream.

        Yields :class:`Event` objects as they arrive.  This is an
        asynchronous generator — iterate with ``async for``.

        The stream stays open until the server closes it or the client
        disconnects.  Use ``break`` or cancel the task to stop.

        Example::

            async for evt in client.events():
                if evt.event == "campaign.completed":
                    print("Done!")
                    break
        """
        client = self.http_client
        async with client.stream("GET", "/api/v1/events") as resp:
            if resp.status_code >= 400:
                await resp.aread()
                self._raise_for_status(resp)

            current_event: str | None = None
            data_lines: list[str] = []

            async for line in resp.aiter_lines():
                if line == "":
                    # Blank line = event delimiter
                    if current_event is not None:
                        raw_data = "\n".join(data_lines) if data_lines else None
                        parsed: Any = raw_data
                        if raw_data:
                            try:
                                parsed = json.loads(raw_data)
                            except (json.JSONDecodeError, TypeError):
                                parsed = raw_data
                        yield Event(event=current_event, data=parsed)
                    current_event = None
                    data_lines = []
                    continue
                if line.startswith("event:"):
                    current_event = line[len("event:") :].strip()
                elif line.startswith("data:"):
                    data_lines.append(line[len("data:") :].strip())
                # Ignore comments (lines starting with ':') and other fields

    # ------------------------------------------------------------------
    # Variable management (issue #557)
    # ------------------------------------------------------------------

    async def batch_update_variables(
        self,
        variables: list[VariableBatchUpdateItem],
    ) -> VariableBatchUpdateResponse:
        """``POST /api/v1/variables/batch_update`` — atomic batch variable update.

        Updates multiple variables atomically.  All variables are validated
        before any are updated.  If any variable is invalid, the entire batch
        is rejected with error details for each failed variable.

        Parameters
        ----------
        variables
            List of variable update items, each with ``name`` and the fields
            to update.

        Returns
        -------
        VariableBatchUpdateResponse
            ``updated`` contains successfully updated variables.
            ``errors`` contains error details for failed updates.
        """
        body = {"variables": [v.model_dump(mode="json") for v in variables]}
        resp = await self._request(
            "POST",
            "/api/v1/variables/batch_update",
            json_body=body,
        )
        return VariableBatchUpdateResponse.model_validate(resp.json())
