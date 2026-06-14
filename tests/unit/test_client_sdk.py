"""Tests for the osimflow_client Python SDK (issue #349)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from osimflow_client import (
    CampaignClient,
    CampaignCreateResult,
    CampaignDetail,
    CampaignNotFoundError,
    ResultsClient,
    ResultsNotFoundError,
)
from osimflow_client.exceptions import (
    APIError,
    CampaignCreationError,
    CampaignDeletionError,
    OSimFlowClientError,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def base_url() -> str:
    return "http://localhost:8000"


@pytest.fixture
def campaign_client(base_url: str) -> CampaignClient:
    return CampaignClient(base_url=base_url)


@pytest.fixture
def results_client(base_url: str) -> ResultsClient:
    return ResultsClient(base_url=base_url)


# ---------------------------------------------------------------------------
# CampaignClient tests
# ---------------------------------------------------------------------------


class TestCampaignClientInit:
    """Tests for CampaignClient initialisation."""

    def test_base_url_strips_trailing_slash(self) -> None:
        client = CampaignClient(base_url="http://localhost:8000/")
        assert client.base_url == "http://localhost:8000"

    def test_api_key_sets_header(self) -> None:
        client = CampaignClient(base_url="http://localhost:8000", api_key="secret")
        assert client._session.headers["X-API-Key"] == "secret"

    def test_default_timeout(self, campaign_client: CampaignClient) -> None:
        assert campaign_client.timeout == 30


class TestCampaignClientList:
    """Tests for CampaignClient.list()."""

    def test_list_returns_campaign_summaries(
        self,
        campaign_client: CampaignClient,
    ) -> None:
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = {
            "campaigns": [
                {
                    "campaign_id": "camp-001",
                    "status": "completed",
                    "started_at": 1000.0,
                    "finished_at": 2000.0,
                    "n_samples": 10,
                    "n_succeeded": 9,
                    "n_failed": 1,
                },
                {
                    "campaign_id": "camp-002",
                    "status": "running",
                    "started_at": 3000.0,
                    "finished_at": None,
                    "n_samples": 5,
                    "n_succeeded": 2,
                    "n_failed": 0,
                },
            ],
            "total": 2,
        }

        with patch.object(campaign_client._session, "get", return_value=mock_resp):
            result = campaign_client.list()

        assert len(result) == 2
        assert result[0].campaign_id == "camp-001"
        assert result[0].status == "completed"
        assert result[0].n_succeeded == 9
        assert result[1].campaign_id == "camp-002"
        assert result[1].status == "running"

    def test_list_empty(self, campaign_client: CampaignClient) -> None:
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = {"campaigns": [], "total": 0}

        with patch.object(campaign_client._session, "get", return_value=mock_resp):
            result = campaign_client.list()

        assert result == []


class TestCampaignClientCreate:
    """Tests for CampaignClient.create()."""

    def test_create_returns_result(
        self,
        campaign_client: CampaignClient,
    ) -> None:
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = {
            "campaign_id": "camp-new",
            "outdir": "/tmp/camp-new",
            "status": "created",
        }

        with patch.object(campaign_client._session, "post", return_value=mock_resp):
            result = campaign_client.create(
                input_variables="/tmp/vars.yml",
                template_sim_package="/tmp/pkg",
                n_samples=10,
            )

        assert isinstance(result, CampaignCreateResult)
        assert result.campaign_id == "camp-new"
        assert result.outdir == "/tmp/camp-new"
        assert result.status == "created"

    def test_create_with_auto_start(
        self,
        campaign_client: CampaignClient,
    ) -> None:
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = {
            "campaign_id": "camp-auto",
            "outdir": "/tmp/camp-auto",
            "status": "running",
        }

        with patch.object(campaign_client._session, "post", return_value=mock_resp) as mock_post:
            campaign_client.create(
                input_variables="/tmp/vars.yml",
                template_sim_package="/tmp/pkg",
                n_samples=5,
                auto_start=True,
            )
            mock_post.assert_called_once()
            call_kwargs = mock_post.call_args.kwargs
            assert call_kwargs["json"]["auto_start"] is True

    def test_create_forbidden_raises(
        self,
        campaign_client: CampaignClient,
    ) -> None:
        mock_resp = MagicMock()
        mock_resp.ok = False
        mock_resp.status_code = 403
        mock_resp.text = "Forbidden"
        mock_resp.__class__ = type("MockResponse", (), {})()

        with patch.object(campaign_client._session, "post", return_value=mock_resp):
            with pytest.raises(CampaignCreationError, match="enable-writes"):
                campaign_client.create(
                    input_variables="/tmp/vars.yml",
                    template_sim_package="/tmp/pkg",
                    n_samples=5,
                )


class TestCampaignClientStatus:
    """Tests for CampaignClient.status()."""

    def test_status_returns_detail(
        self,
        campaign_client: CampaignClient,
    ) -> None:
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = {
            "campaign_id": "camp-001",
            "status": "completed",
            "started_at": 1000.0,
            "finished_at": 2000.0,
            "elapsed_s": 1000.0,
            "config": {"executor": "local", "n_samples": 10},
            "summary": {"n_samples": 10, "n_succeeded": 9, "n_failed": 1},
            "steps": [],
        }

        with patch.object(campaign_client._session, "get", return_value=mock_resp):
            result = campaign_client.status("camp-001")

        assert isinstance(result, CampaignDetail)
        assert result.campaign_id == "camp-001"
        assert result.status == "completed"
        assert result.elapsed_s == 1000.0
        assert result.summary == {"n_samples": 10, "n_succeeded": 9, "n_failed": 1}

    def test_status_not_found_raises(
        self,
        campaign_client: CampaignClient,
    ) -> None:
        mock_resp = MagicMock()
        mock_resp.ok = False
        mock_resp.status_code = 404
        mock_resp.text = "Not Found"

        with patch.object(campaign_client._session, "get", return_value=mock_resp):
            with pytest.raises(CampaignNotFoundError, match="camp-missing"):
                campaign_client.status("camp-missing")


class TestCampaignClientDelete:
    """Tests for CampaignClient.delete()."""

    def test_delete_succeeds(
        self,
        campaign_client: CampaignClient,
    ) -> None:
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = {"campaign_id": "camp-001", "status": "deleted"}

        with patch.object(campaign_client._session, "delete", return_value=mock_resp):
            campaign_client.delete("camp-001")

    def test_delete_not_found_raises(
        self,
        campaign_client: CampaignClient,
    ) -> None:
        mock_resp = MagicMock()
        mock_resp.ok = False
        mock_resp.status_code = 404
        mock_resp.text = "Not Found"

        with patch.object(campaign_client._session, "delete", return_value=mock_resp):
            with pytest.raises(CampaignNotFoundError, match="camp-missing"):
                campaign_client.delete("camp-missing")

    def test_delete_forbidden_raises(
        self,
        campaign_client: CampaignClient,
    ) -> None:
        mock_resp = MagicMock()
        mock_resp.ok = False
        mock_resp.status_code = 403
        mock_resp.text = "Forbidden"

        with patch.object(campaign_client._session, "delete", return_value=mock_resp):
            with pytest.raises(CampaignDeletionError, match="enable-writes"):
                campaign_client.delete("camp-001")


# ---------------------------------------------------------------------------
# ResultsClient tests
# ---------------------------------------------------------------------------


class TestResultsClientInit:
    """Tests for ResultsClient initialisation."""

    def test_base_url_strips_trailing_slash(self) -> None:
        client = ResultsClient(base_url="http://localhost:8000/")
        assert client.base_url == "http://localhost:8000"

    def test_api_key_sets_header(self) -> None:
        client = ResultsClient(base_url="http://localhost:8000", api_key="secret")
        assert client._session.headers["X-API-Key"] == "secret"


class TestResultsClientGet:
    """Tests for ResultsClient.get()."""

    def test_get_returns_results_list(
        self,
        results_client: ResultsClient,
    ) -> None:
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = [
            {"sample_id": "s1", "status": "success", "elapsed_s": 10.0},
            {"sample_id": "s2", "status": "failed", "elapsed_s": 5.0},
        ]

        with patch.object(results_client._session, "get", return_value=mock_resp):
            result = results_client.get("camp-001")

        assert isinstance(result, list)
        assert len(result) == 2
        assert result[0]["sample_id"] == "s1"

    def test_get_not_found_raises(
        self,
        results_client: ResultsClient,
    ) -> None:
        mock_resp = MagicMock()
        mock_resp.ok = False
        mock_resp.status_code = 404
        mock_resp.text = "Not Found"

        with patch.object(results_client._session, "get", return_value=mock_resp):
            with pytest.raises(ResultsNotFoundError):
                results_client.get("camp-missing")


class TestResultsClientGetSamples:
    """Tests for ResultsClient.get_samples()."""

    def test_get_samples_returns_list(
        self,
        results_client: ResultsClient,
    ) -> None:
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = {
            "samples": [
                {
                    "sample_id": "s1",
                    "status": "success",
                    "elapsed_s": 10.0,
                    "error_summary": None,
                    "generation": 1,
                    "worker_id": "worker-1",
                    "cost_usd": 0.05,
                },
            ],
            "total": 1,
            "page": 1,
            "per_page": 50,
        }

        with patch.object(results_client._session, "get", return_value=mock_resp):
            result = results_client.get_samples("camp-001")

        assert len(result) == 1
        assert result[0].sample_id == "s1"
        assert result[0].status == "success"
        assert result[0].cost_usd == 0.05


class TestResultsClientListMeasures:
    """Tests for ResultsClient.list_measures()."""

    def test_list_measures_returns_empty_list(
        self,
        results_client: ResultsClient,
    ) -> None:
        result = results_client.list_measures("camp-001")
        assert result == []


# ---------------------------------------------------------------------------
# Exception tests
# ---------------------------------------------------------------------------


class TestExceptions:
    """Tests for exception classes."""

    def test_api_error_includes_status_and_message(self) -> None:
        exc = APIError(404, "Not found")
        assert exc.status_code == 404
        assert "404" in str(exc)
        assert "Not found" in str(exc)

    def test_campaign_not_found_error(self) -> None:
        exc = CampaignNotFoundError("camp-123")
        assert "camp-123" in str(exc)

    def test_results_not_found_error(self) -> None:
        exc = ResultsNotFoundError("results not found")
        assert "results not found" in str(exc)

    def test_client_error_inheritance(self) -> None:
        assert issubclass(CampaignNotFoundError, OSimFlowClientError)
        assert issubclass(CampaignCreationError, OSimFlowClientError)
        assert issubclass(CampaignDeletionError, OSimFlowClientError)
        assert issubclass(ResultsNotFoundError, OSimFlowClientError)
        assert issubclass(APIError, OSimFlowClientError)
