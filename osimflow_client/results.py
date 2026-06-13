"""Results and listing client for the OSimFlow REST API.

:class:`ResultsClient` provides methods for:
  - Retrieving aggregated results
  - Listing variables
  - Listing measures
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, cast

import requests

from osimflow_client.exceptions import (
    APIError,
    CampaignNotFoundError,
    OSimFlowClientError,
    ResultsNotFoundError,
)

log = logging.getLogger(__name__)


@dataclass
class SampleResult:
    """One row in the aggregated results."""

    sample_id: str
    status: str
    elapsed_s: float
    error_summary: str | None = None
    generation: int | None = None
    worker_id: str | None = None
    cost_usd: float | None = None


@dataclass
class FailureResult:
    """One row in the failed simulations report."""

    sample_id: str
    error_summary: str | None = None


class ResultsClient:
    """Client for OSimFlow results and listing endpoints.

    Parameters
    ----------
    base_url
        Base URL of the OSimFlow API server (e.g. ``"http://localhost:8000"``).
    api_key
        Optional API key for authentication.
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

    def _get(self, path: str, **kwargs: Any) -> Any:
        """Send a GET request and return parsed JSON."""
        url = f"{self.base_url}{path}"
        try:
            resp = self._session.get(url, timeout=self.timeout, **kwargs)
        except requests.RequestException as exc:
            raise OSimFlowClientError(f"Request failed: {exc}") from exc
        if resp.status_code == 404:
            raise ResultsNotFoundError(f"Results not found at {path}")
        if not resp.ok:
            raise APIError(resp.status_code, resp.text)
        return resp.json()

    # -------------------------------------------------------------------------
    # Public API
    # -------------------------------------------------------------------------

    def get(self, campaign_id: str) -> list[dict[str, Any]]:
        """Retrieve aggregated results for a campaign.

        Parameters
        ----------
        campaign_id
            The campaign identifier.

        Returns
        -------
        list[dict[str, Any]]
            List of result records as dictionaries.
        """
        try:
            data = self._get(f"/api/v1/campaigns/{campaign_id}/results")
            return cast(list[dict[str, Any]], data)
        except APIError as exc:
            if exc.status_code == 404:
                raise CampaignNotFoundError(
                    f"Campaign '{campaign_id}' not found"
                ) from exc
            raise

    def list_variables(self, campaign_id: str) -> list[str]:
        """List variable names for a campaign.

        Reads the ``variables.yml`` from the campaign directory to extract
        the variable names.

        Parameters
        ----------
        campaign_id
            The campaign identifier.

        Returns
        -------
        list[str]
            List of variable names.
        """
        # The API does not yet expose a dedicated variables endpoint,
        # so we derive it from the campaign config stored in run.json.
        try:
            resp = self._session.get(
                f"{self.base_url}/api/v1/campaigns/{campaign_id}",
                timeout=self.timeout,
            )
        except requests.RequestException as e:
            raise OSimFlowClientError(f"Request failed: {e}") from e
        if resp.status_code == 404:
            raise CampaignNotFoundError(f"Campaign '{campaign_id}' not found")
        if not resp.ok:
            raise APIError(resp.status_code, resp.text)
        data = resp.json()
        config = data.get("config") or {}
        variables_path = config.get("input_variables")
        if not variables_path:
            return []
        # Return the path as-is; the caller can load and parse it.
        return [variables_path]

    def list_measures(self, campaign_id: str) -> list[str]:
        """List measure names for a campaign.

        Parameters
        ----------
        campaign_id
            The campaign identifier.

        Returns
        -------
        list[str]
            List of measure names.
        """
        # The API does not yet expose a dedicated measures endpoint.
        # Return an empty list as a placeholder until the endpoint exists.
        return []

    def get_failures(self, campaign_id: str) -> list[FailureResult]:
        """Retrieve failed simulations for a campaign.

        Parameters
        ----------
        campaign_id
            The campaign identifier.

        Returns
        -------
        list[FailureResult]
            List of failed simulation records.
        """
        try:
            data = self._get(f"/api/v1/campaigns/{campaign_id}/failures")
            records = cast(list[dict[str, Any]], data)
            return [
                FailureResult(
                    sample_id=r.get("sample_id", ""),
                    error_summary=r.get("error_summary"),
                )
                for r in records
            ]
        except APIError as exc:
            if exc.status_code == 404:
                raise CampaignNotFoundError(
                    f"Campaign '{campaign_id}' not found"
                ) from exc
            raise

    def get_samples(
        self,
        campaign_id: str,
        page: int = 1,
        per_page: int = 50,
    ) -> list[SampleResult]:
        """Retrieve per-sample results for a campaign.

        Parameters
        ----------
        campaign_id
            The campaign identifier.
        page
            Page number (1-indexed, default 1).
        per_page
            Items per page (default 50, max 500).

        Returns
        -------
        list[SampleResult]
            List of sample result records.
        """
        try:
            data = self._get(
                f"/api/v1/campaigns/{campaign_id}/samples",
                params={"page": page, "per_page": per_page},
            )
            samples: list[dict[str, Any]] = data.get("samples", [])
            return [
                SampleResult(
                    sample_id=s["sample_id"],
                    status=s["status"],
                    elapsed_s=s.get("elapsed_s", 0.0),
                    error_summary=s.get("error_summary"),
                    generation=s.get("generation"),
                    worker_id=s.get("worker_id"),
                    cost_usd=s.get("cost_usd"),
                )
                for s in samples
            ]
        except APIError as exc:
            if exc.status_code == 404:
                raise CampaignNotFoundError(
                    f"Campaign '{campaign_id}' not found"
                ) from exc
            raise

