"""Campaign management client for the OSimFlow REST API.

:class:`CampaignClient` provides methods for:
  - Listing campaigns
  - Creating a new campaign
  - Getting campaign status
  - Deleting a campaign
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import requests

from osimflow_client.exceptions import (
    APIError,
    CampaignCreationError,
    CampaignDeletionError,
    CampaignNotFoundError,
    OSimFlowClientError,
)

log = logging.getLogger(__name__)


@dataclass
class CampaignSummary:
    """Lightweight campaign summary returned by list operations."""

    campaign_id: str
    status: str
    started_at: float | None = None
    finished_at: float | None = None
    n_samples: int = 0
    n_succeeded: int = 0
    n_failed: int = 0


@dataclass
class CampaignDetail:
    """Full campaign detail returned by status operations."""

    campaign_id: str
    status: str
    started_at: float | None = None
    finished_at: float | None = None
    elapsed_s: float | None = None
    config: dict[str, Any] | None = None
    summary: dict[str, Any] | None = None
    quality_summary: dict[str, Any] | None = None
    baseline_comparison: dict[str, Any] | None = None
    total_cost_usd: float | None = None
    spot_savings_usd: float | None = None
    steps: list[dict[str, Any]] | None = None


@dataclass
class CampaignCreateResult:
    """Result of a campaign creation operation."""

    campaign_id: str
    outdir: str
    status: str


class CampaignClient:
    """Client for OSimFlow campaign management endpoints.

    Parameters
    ----------
    base_url
        Base URL of the OSimFlow API server (e.g. ``"http://localhost:8000"``).
    api_key
        Optional API key for authentication.  If provided, the
        ``X-API-Key`` header is sent with every request.
    timeout
        Request timeout in seconds (default 30).
    """

    def __init__(
        self,
        base_url: str,
        api_key: str | None = None,
        timeout: int = 30,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self._session = requests.Session()
        if api_key is not None:
            self._session.headers["X-API-Key"] = api_key

    # -------------------------------------------------------------------------
    # Private helpers
    # -------------------------------------------------------------------------

    def _get(self, path: str, **kwargs: Any) -> dict[str, Any]:
        """Send a GET request and return parsed JSON."""
        url = f"{self.base_url}{path}"
        try:
            resp = self._session.get(url, timeout=self.timeout, **kwargs)
        except requests.RequestException as exc:
            raise OSimFlowClientError(f"Request failed: {exc}") from exc
        if resp.status_code == 404:
            raise CampaignNotFoundError(f"Resource not found at {path}")
        if not resp.ok:
            raise APIError(resp.status_code, resp.text)
        return resp.json()  # type: ignore[no-any-return]

    def _post(self, path: str, **kwargs: Any) -> dict[str, Any]:
        """Send a POST request and return parsed JSON."""
        url = f"{self.base_url}{path}"
        try:
            resp = self._session.post(url, timeout=self.timeout, **kwargs)
        except requests.RequestException as exc:
            raise OSimFlowClientError(f"Request failed: {exc}") from exc
        if not resp.ok:
            raise APIError(resp.status_code, resp.text)
        return resp.json()  # type: ignore[no-any-return]

    def _delete(self, path: str, **kwargs: Any) -> dict[str, Any]:
        """Send a DELETE request and return parsed JSON."""
        url = f"{self.base_url}{path}"
        try:
            resp = self._session.delete(url, timeout=self.timeout, **kwargs)
        except requests.RequestException as exc:
            raise OSimFlowClientError(f"Request failed: {exc}") from exc
        if not resp.ok:
            raise APIError(resp.status_code, resp.text)
        return resp.json()  # type: ignore[no-any-return]

    # -------------------------------------------------------------------------
    # Public API
    # -------------------------------------------------------------------------

    def list(self) -> list[CampaignSummary]:
        """List all campaigns on the server.

        Returns
        -------
        list[CampaignSummary]
            List of campaign summaries.
        """
        data = self._get("/api/v1/campaigns")
        campaigns: list[dict[str, Any]] = data.get("campaigns", [])
        return [
            CampaignSummary(
                campaign_id=c["campaign_id"],
                status=c["status"],
                started_at=c.get("started_at"),
                finished_at=c.get("finished_at"),
                n_samples=c.get("n_samples", 0),
                n_succeeded=c.get("n_succeeded", 0),
                n_failed=c.get("n_failed", 0),
            )
            for c in campaigns
        ]

    def create(
        self,
        input_variables: str,
        template_sim_package: str,
        n_samples: int,
        *,
        openstudio_version: str = "3.11.0",
        executor: str = "local",
        algorithm: str = "lhs",
        outdir: str | None = None,
        archive_intermediates: bool = False,
        auto_start: bool = False,
    ) -> CampaignCreateResult:
        """Create a new campaign.

        Parameters
        ----------
        input_variables
            Path to the ``variables.yml`` file.
        template_sim_package
            Path to the template simulation package directory.
        n_samples
            Number of LHS samples to generate.
        openstudio_version
            OpenStudio version string (default ``"3.11.0"``).
        executor
            Executor backend to use (default ``"local"``).
        algorithm
            Sampling algorithm (default ``"lhs"``).
        outdir
            Output directory path.  If omitted, the server auto-generates one.
        archive_intermediates
            Whether to archive intermediate files (default ``False``).
        auto_start
            Whether to immediately launch the campaign (default ``False``).

        Returns
        -------
        CampaignCreateResult
            Campaign creation result with ID and output directory.
        """
        payload: dict[str, Any] = {
            "input_variables": input_variables,
            "template_sim_package": template_sim_package,
            "n_samples": n_samples,
            "openstudio_version": openstudio_version,
            "executor": executor,
            "algorithm": algorithm,
            "archive_intermediates": archive_intermediates,
            "auto_start": auto_start,
        }
        if outdir is not None:
            payload["outdir"] = outdir

        try:
            data = self._post("/api/v1/campaigns", json=payload)
        except APIError as exc:
            if exc.status_code == 403:
                raise CampaignCreationError(
                    "Campaign creation requires --enable-writes mode on the server"
                ) from exc
            raise CampaignCreationError(str(exc)) from exc

        return CampaignCreateResult(
            campaign_id=data["campaign_id"],
            outdir=data["outdir"],
            status=data["status"],
        )

    def status(self, campaign_id: str) -> CampaignDetail:
        """Get detailed status for a specific campaign.

        Parameters
        ----------
        campaign_id
            The campaign identifier.

        Returns
        -------
        CampaignDetail
            Full campaign detail.
        """
        try:
            data = self._get(f"/api/v1/campaigns/{campaign_id}")
        except APIError as exc:
            if exc.status_code == 404:
                raise CampaignNotFoundError(
                    f"Campaign '{campaign_id}' not found"
                ) from exc
            raise
        return CampaignDetail(
            campaign_id=data["campaign_id"],
            status=data["status"],
            started_at=data.get("started_at"),
            finished_at=data.get("finished_at"),
            elapsed_s=data.get("elapsed_s"),
            config=data.get("config"),
            summary=data.get("summary"),
            quality_summary=data.get("quality_summary"),
            baseline_comparison=data.get("baseline_comparison"),
            total_cost_usd=data.get("total_cost_usd"),
            spot_savings_usd=data.get("spot_savings_usd"),
            steps=data.get("steps", []),
        )

    def delete(self, campaign_id: str) -> None:
        """Delete a campaign.

        Parameters
        ----------
        campaign_id
            The campaign identifier to delete.

        Raises
        ------
        CampaignNotFoundError
            If the campaign does not exist.
        CampaignDeletionError
            If the server refuses the deletion (e.g. read-only mode).
        """
        try:
            self._delete(f"/api/v1/campaigns/{campaign_id}")
        except APIError as exc:
            if exc.status_code == 404:
                raise CampaignNotFoundError(
                    f"Campaign '{campaign_id}' not found"
                ) from exc
            if exc.status_code == 403:
                raise CampaignDeletionError(
                    "Campaign deletion requires --enable-writes mode on the server"
                ) from exc
            raise CampaignDeletionError(str(exc)) from exc

    def cancel(self, campaign_id: str) -> None:
        """Cancel a running campaign.

        Parameters
        ----------
        campaign_id
            The campaign identifier to cancel.

        Raises
        ------
        CampaignNotFoundError
            If the campaign does not exist.
        APIError
            If the campaign is not currently running.
        """
        try:
            self._post(f"/api/v1/campaigns/{campaign_id}/cancel")
        except APIError as exc:
            if exc.status_code == 404:
                raise CampaignNotFoundError(
                    f"Campaign '{campaign_id}' not found"
                ) from exc
            if exc.status_code == 409:
                raise APIError(
                    exc.status_code,
                    f"Campaign '{campaign_id}' is not currently running",
                ) from exc
            raise

