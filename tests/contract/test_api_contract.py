"""Schemathesis contract tests for the REST API (issue #434).

These tests verify that the running API conforms to the OpenAPI spec
at ``docs/openapi.json``.  They use hypothesis-based property testing
to generate random valid inputs for query parameters and validate
that responses match the schema.

Run with:
    python -m pytest tests/contract/test_api_contract.py -v
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

schemathesis = pytest.importorskip("schemathesis", reason="schemathesis>=3.19 required")  # noqa: E402
pytest.importorskip("fastapi", reason="osimflow[api] extra required")  # noqa: E402
pytest.importorskip("slowapi", reason="osimflow[api] extra required")  # noqa: E402
pytest.importorskip("sse_starlette", reason="osimflow[api] extra required")  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402
from hypothesis import HealthCheck, given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402

from osimflow.api import create_app  # noqa: E402


@pytest.fixture
def tmp_outdir(tmp_path: Path) -> Path:
    """Temporary output directory with a run.json for the API to serve."""
    run_json = {
        "schema_version": 1,
        "campaign_id": "contract-test-campaign",
        "started_at": 1000.0,
        "finished_at": 2000.0,
        "config_summary": {"executor": "local", "n_samples": 5},
        "steps": [
            {
                "step": "GENERATE_LHS_SAMPLES",
                "cache": "MISS",
                "elapsed_s": 0.5,
                "exit_code": 0,
            },
            {
                "step": "RUN_OPENSTUDIO_SIM",
                "cache": "MISS",
                "elapsed_s": 100.0,
                "exit_code": 0,
            },
            {
                "step": "EXTRACT_KPIS",
                "cache": "MISS",
                "elapsed_s": 5.0,
                "exit_code": 0,
            },
            {
                "step": "AGGREGATE_RESULTS",
                "cache": "MISS",
                "elapsed_s": 2.0,
                "exit_code": 0,
            },
        ],
        "per_sample": [
            {
                "sample_id": "sample_0",
                "status": "ok",
                "elapsed_s": 45.0,
                "kpis": {"eui": 150.5, "total_cost": 1200.0},
            },
            {
                "sample_id": "sample_1",
                "status": "failed",
                "elapsed_s": 0.0,
                "error_summary": "Surface geometry error: non-convex surface detected",
            },
        ],
    }
    (tmp_path / "run.json").write_text(json.dumps(run_json))
    return tmp_path


@pytest.fixture
def client(tmp_outdir: Path) -> TestClient:
    """Test client with a real app and a real outdir."""
    app = create_app(outdir=tmp_outdir)
    return TestClient(app)


@pytest.fixture
def api_schema() -> schemathesis.openapi.OpenApiSchema:
    """Load the OpenAPI spec from docs/openapi.json."""
    spec_path = Path(__file__).parents[2] / "docs" / "openapi.json"
    return schemathesis.openapi.from_path(str(spec_path))


# ---------------------------------------------------------------------------
# Endpoint-level contract tests
# ---------------------------------------------------------------------------


class TestHealthContract:
    """Contract tests for GET /health."""

    def test_health_returns_200_with_string_status(self, client: TestClient) -> None:
        """The /health endpoint must return HTTP 200 and a JSON object
        with a ``status`` string field."""
        response = client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert "status" in data
        assert isinstance(data["status"], str)

    def test_health_schema_compliance(
        self, client: TestClient, api_schema: schemathesis.openapi.OpenApiSchema
    ) -> None:
        """Response must conform to the OpenAPI schema for /health."""
        response = client.get("/health")
        assert response.status_code == 200
        operation = api_schema["/health"]["GET"]
        operation.validate_response(response)


class TestReadyContract:
    """Contract tests for GET /ready."""

    def test_ready_returns_200(self, client: TestClient) -> None:
        """The /ready endpoint must return HTTP 200."""
        response = client.get("/ready")
        assert response.status_code == 200

    def test_ready_returns_object(self, client: TestClient) -> None:
        """Response must be a JSON object."""
        data = client.get("/ready").json()
        assert isinstance(data, dict)

    def test_ready_schema_compliance(
        self, client: TestClient, api_schema: schemathesis.openapi.OpenApiSchema
    ) -> None:
        """Response must conform to the OpenAPI schema for /ready."""
        response = client.get("/ready")
        assert response.status_code == 200
        operation = api_schema["/ready"]["GET"]
        operation.validate_response(response)


class TestCampaignContract:
    """Contract tests for GET /api/v1/campaign."""

    def test_campaign_returns_200(self, client: TestClient) -> None:
        response = client.get("/api/v1/campaign")
        assert response.status_code == 200

    def test_campaign_returns_expected_fields(self, client: TestClient) -> None:
        """Response must contain the campaign fields defined in the API."""
        data = client.get("/api/v1/campaign").json()
        assert "campaign_id" in data
        assert "config_summary" in data
        assert "started_at" in data
        assert "finished_at" in data

    def test_campaign_schema_compliance(
        self, client: TestClient, api_schema: schemathesis.openapi.OpenApiSchema
    ) -> None:
        """Response must conform to the OpenAPI schema for /api/v1/campaign."""
        response = client.get("/api/v1/campaign")
        assert response.status_code == 200
        operation = api_schema["/api/v1/campaign"]["GET"]
        operation.validate_response(response)


class TestStepsContract:
    """Contract tests for GET /api/v1/steps."""

    def test_steps_returns_200(self, client: TestClient) -> None:
        response = client.get("/api/v1/steps")
        assert response.status_code == 200

    def test_steps_returns_total_steps(self, client: TestClient) -> None:
        data = client.get("/api/v1/steps").json()
        assert "total_steps" in data
        assert "steps" in data
        assert isinstance(data["steps"], list)

    def test_steps_schema_compliance(
        self, client: TestClient, api_schema: schemathesis.openapi.OpenApiSchema
    ) -> None:
        """Response must conform to the OpenAPI schema for /api/v1/steps."""
        response = client.get("/api/v1/steps")
        assert response.status_code == 200
        operation = api_schema["/api/v1/steps"]["GET"]
        operation.validate_response(response)


# ---------------------------------------------------------------------------
# Query parameter property tests using hypothesis
# ---------------------------------------------------------------------------


class TestSamplesQueryParams:
    """Property-based tests for GET /api/v1/samples query parameters."""

    @given(page=st.integers(min_value=1, max_value=1000), per_page=st.integers(min_value=1, max_value=500))
    @settings(max_examples=50, suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_samples_pagination_bounds(self, client: TestClient, page: int, per_page: int) -> None:
        """page >= 1 and per_page in [1, 500] must return HTTP 200."""
        response = client.get("/api/v1/samples", params={"page": page, "per_page": per_page})
        assert response.status_code == 200, f"page={page}, per_page={per_page}"
        data = response.json()
        assert "samples" in data
        assert "total" in data
        assert "page" in data
        assert "per_page" in data

    def test_samples_page_zero_rejected(self, client: TestClient) -> None:
        """page=0 is invalid and should return 422 (FastAPI validation)."""
        response = client.get("/api/v1/samples", params={"page": 0})
        assert response.status_code == 422

    def test_samples_per_page_exceeding_max_rejected(self, client: TestClient) -> None:
        """per_page > 500 is invalid and should return 422."""
        response = client.get("/api/v1/samples", params={"per_page": 501})
        assert response.status_code == 422

    @given(page=st.integers(max_value=0))
    @settings(max_examples=20, suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_samples_negative_page_rejected(self, client: TestClient, page: int) -> None:
        """page < 1 is invalid and should return 422."""
        response = client.get("/api/v1/samples", params={"page": page})
        assert response.status_code == 422

    @given(per_page=st.integers(max_value=0))
    @settings(max_examples=20, suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_samples_negative_per_page_rejected(self, client: TestClient, per_page: int) -> None:
        """per_page < 1 is invalid and should return 422."""
        response = client.get("/api/v1/samples", params={"per_page": per_page})
        assert response.status_code == 422

    @given(page=st.integers(min_value=1, max_value=1000))
    @settings(max_examples=20, suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_samples_defaults(self, client: TestClient, page: int) -> None:
        """When only page is provided, per_page defaults to 50."""
        response = client.get("/api/v1/samples", params={"page": page})
        assert response.status_code == 200
        data = response.json()
        assert data["page"] == page
        assert data["per_page"] == 50


# ---------------------------------------------------------------------------
# SSE events endpoint contract test
# ---------------------------------------------------------------------------


class TestEventsContract:
    """Contract tests for GET /api/v1/events (SSE stream)."""

    def test_events_returns_200(self, client: TestClient) -> None:
        """The SSE endpoint must return HTTP 200 (EventSourceResponse)."""
        from unittest.mock import patch

        async def finite_generator(request, poll_interval=1.0, max_iterations=1):
            yield {"event": "campaign.completed", "data": "{}"}

        with patch("osimflow.api.events._event_generator", finite_generator):
            resp = client.get("/api/v1/events", timeout=10)
        assert resp.status_code == 200

    def test_events_is_sse(self, client: TestClient) -> None:
        """Response must have text/event-stream content type."""
        from unittest.mock import patch

        async def finite_generator(request, poll_interval=1.0, max_iterations=1):
            yield {"event": "campaign.completed", "data": "{}"}

        with patch("osimflow.api.events._event_generator", finite_generator):
            resp = client.get("/api/v1/events", timeout=10)
        content_type = resp.headers.get("content-type", "")
        assert "text/event-stream" in content_type


# ---------------------------------------------------------------------------
# OpenAPI spec completeness checks
# ---------------------------------------------------------------------------


class TestOpenAPISpecCompleteness:
    """Verify the OpenAPI spec covers all implemented endpoints."""

    def test_spec_defines_health(self, api_schema: schemathesis.openapi.OpenApiSchema) -> None:
        assert "/health" in api_schema

    def test_spec_defines_ready(self, api_schema: schemathesis.openapi.OpenApiSchema) -> None:
        assert "/ready" in api_schema

    def test_spec_defines_campaign(self, api_schema: schemathesis.openapi.OpenApiSchema) -> None:
        assert "/api/v1/campaign" in api_schema

    def test_spec_defines_steps(self, api_schema: schemathesis.openapi.OpenApiSchema) -> None:
        assert "/api/v1/steps" in api_schema

    def test_spec_defines_events(self, api_schema: schemathesis.openapi.OpenApiSchema) -> None:
        assert "/api/v1/events" in api_schema

    def test_spec_defines_samples(self, api_schema: schemathesis.openapi.OpenApiSchema) -> None:
        assert "/api/v1/samples" in api_schema

    def test_health_get_has_200_response(self, api_schema: schemathesis.openapi.OpenApiSchema) -> None:
        operation = api_schema["/health"]["GET"]
        assert "200" in operation.definition.raw["responses"]

    def test_campaign_get_has_200_response(self, api_schema: schemathesis.openapi.OpenApiSchema) -> None:
        operation = api_schema["/api/v1/campaign"]["GET"]
        assert "200" in operation.definition.raw["responses"]

    def test_steps_get_has_200_response(self, api_schema: schemathesis.openapi.OpenApiSchema) -> None:
        operation = api_schema["/api/v1/steps"]["GET"]
        assert "200" in operation.definition.raw["responses"]

    def test_events_get_has_200_response(self, api_schema: schemathesis.openapi.OpenApiSchema) -> None:
        operation = api_schema["/api/v1/events"]["GET"]
        assert "200" in operation.definition.raw["responses"]


# ---------------------------------------------------------------------------
# Response shape property tests
# ---------------------------------------------------------------------------


class TestResponseShapeProperties:
    """Validate structural properties of API responses."""

    def test_health_response_has_only_status_key(self, client: TestClient) -> None:
        """The /health response must contain only a 'status' key."""
        data = client.get("/health").json()
        assert list(data.keys()) == ["status"]

    def test_campaign_response_campaign_id_is_string(self, client: TestClient) -> None:
        data = client.get("/api/v1/campaign").json()
        assert isinstance(data["campaign_id"], str)

    def test_steps_response_total_steps_is_integer(self, client: TestClient) -> None:
        data = client.get("/api/v1/steps").json()
        assert isinstance(data["total_steps"], int)

    def test_samples_pagination_response_fields(self, client: TestClient) -> None:
        data = client.get("/api/v1/samples", params={"page": 1, "per_page": 10}).json()
        required_fields = {"samples", "total", "page", "per_page"}
        assert required_fields.issubset(data.keys())

    def test_samples_pagination_page_count(self, client: TestClient) -> None:
        data = client.get("/api/v1/samples", params={"page": 1, "per_page": 10}).json()
        assert data["page"] == 1
        assert data["per_page"] == 10
        assert data["total"] == 2  # from tmp_outdir fixture
