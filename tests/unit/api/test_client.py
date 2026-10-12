"""Unit tests for the OSimFlow async Python client (issue #433).

Tests use ``httpx.MockTransport`` to simulate the REST API without
starting a real server, giving fast, deterministic, offline tests.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

pytest.importorskip("httpx", reason="httpx required for client tests")
pytest.importorskip("fastapi", reason="osimflow[api] extra required")

from osimflow.client import (
    AuthenticationError,
    CampaignResponse,
    CampaignStopResponse,
    Event,
    HealthResponse,
    NotFoundError,
    OSimFlowAPIError,
    OSimFlowClient,
    RateLimitError,
    ReadyResponse,
    ResultRow,
    SampleDetailResponse,
    SamplesResponse,
    ServerError,
    StepsResponse,
)

# ---------------------------------------------------------------------------
# Helpers: mock transport
# ---------------------------------------------------------------------------


def _make_app_json() -> dict[str, object]:
    """Sample run.json content used across tests."""
    return {
        "schema_version": 1,
        "campaign_id": "test-campaign-001",
        "started_at": 1000.0,
        "finished_at": 2000.0,
        "config_summary": {"executor": "local", "n_samples": 5},
        "steps": [
            {"step": "GENERATE_LHS_SAMPLES", "cache": "MISS", "elapsed_s": 0.5, "exit_code": 0},
            {"step": "RUN_OPENSTUDIO_SIM", "cache": "MISS", "elapsed_s": 100.0, "exit_code": 0},
        ],
        "per_sample": [
            {"sample_id": "s_0000", "status": "ok", "elapsed_s": 12.3},
            {"sample_id": "s_0001", "status": "failed", "elapsed_s": 5.0, "error_summary": "boom"},
        ],
    }


def _mock_handler(request: httpx.Request) -> httpx.Response:
    """Default mock handler that serves a full happy-path API."""
    path = request.url.path
    method = request.method

    # --- health / ready ---
    if path == "/health":
        return httpx.Response(200, json={"status": "alive"})
    if path == "/ready":
        return httpx.Response(
            200,
            json={"status": "ready", "campaign_id": "test-campaign-001"},
        )

    # --- campaign ---
    if path == "/api/v1/campaign":
        data = _make_app_json()
        return httpx.Response(
            200,
            json={
                "campaign_id": data["campaign_id"],
                "config_summary": data["config_summary"],
                "started_at": data["started_at"],
                "finished_at": data["finished_at"],
                "baseline_comparison": None,
            },
        )

    # --- steps ---
    if path == "/api/v1/steps":
        data = _make_app_json()
        return httpx.Response(200, json={"steps": data["steps"], "total_steps": 2})

    # --- samples ---
    if path == "/api/v1/samples":
        data = _make_app_json()
        page = int(request.url.params.get("page", "1"))
        per_page = int(request.url.params.get("per_page", "50"))
        all_samples = data["per_sample"]
        start = (page - 1) * per_page
        end = start + per_page
        return httpx.Response(
            200,
            json={
                "samples": all_samples[start:end],
                "total": len(all_samples),
                "page": page,
                "per_page": per_page,
            },
        )

    # --- single sample ---
    if path.startswith("/api/v1/samples/") and "/logs/" not in path:
        sid = path.rsplit("/", 1)[-1]
        data = _make_app_json()
        for s in data["per_sample"]:
            if s["sample_id"] == sid:
                merged = {**s, "kpis": {"eui": 120.5}, "log_files": {}}
                return httpx.Response(200, json=merged)
        return httpx.Response(404, json={"detail": f"Sample '{sid}' not found"})

    # --- results ---
    if path == "/api/v1/results":
        return httpx.Response(
            200,
            json=[{"sample_id": "s_0000", "eui": 120.5}, {"sample_id": "s_0001", "eui": None}],
        )

    # --- failures ---
    if path == "/api/v1/failures":
        return httpx.Response(200, json=[{"sample_id": "s_0001", "error": "boom"}])

    # --- plots ---
    if path == "/api/v1/plots":
        return httpx.Response(
            200, json={"plots": [{"name": "eui_hist.png", "size": 4096}], "total": 1}
        )

    # --- pareto ---
    if path == "/api/v1/pareto":
        return httpx.Response(
            200,
            json={"generations": [{"_file": "gen_0.json", "front": []}], "total_generations": 1},
        )

    # --- campaign stop ---
    if path == "/api/v1/campaign/stop" and method == "POST":
        return httpx.Response(200, json={"status": "stopping"})

    return httpx.Response(404, json={"detail": "Not found"})


def _make_client(
    handler: httpx.MockTransport | None = None,
    *,
    api_key: str | None = None,
    base_url: str = "http://test",
) -> OSimFlowClient:
    """Create an OSimFlowClient backed by a mock transport."""
    transport = handler or httpx.MockTransport(_mock_handler)
    return OSimFlowClient(base_url, api_key=api_key, transport=transport)


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_context_manager_connects_and_closes() -> None:
    client = _make_client()
    assert client._client is None  # noqa: SLF001 — not yet connected
    async with client:
        assert client.http_client is not None
    assert client._client is None  # noqa: SLF001 — closed


@pytest.mark.anyio
async def test_http_client_raises_when_not_connected() -> None:
    client = _make_client()
    with pytest.raises(RuntimeError, match="not connected"):
        _ = client.http_client


# ---------------------------------------------------------------------------
# Health & ready
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_health() -> None:
    async with _make_client() as client:
        result = await client.health()
    assert isinstance(result, HealthResponse)
    assert result.status == "alive"


@pytest.mark.anyio
async def test_ready() -> None:
    async with _make_client() as client:
        result = await client.ready()
    assert isinstance(result, ReadyResponse)
    assert result.status == "ready"
    assert result.campaign_id == "test-campaign-001"


# ---------------------------------------------------------------------------
# Campaign & steps
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_get_campaign() -> None:
    async with _make_client() as client:
        result = await client.get_campaign()
    assert isinstance(result, CampaignResponse)
    assert result.campaign_id == "test-campaign-001"
    assert result.config_summary["executor"] == "local"


@pytest.mark.anyio
async def test_get_steps() -> None:
    async with _make_client() as client:
        result = await client.get_steps()
    assert isinstance(result, StepsResponse)
    assert result.total_steps == 2
    assert result.steps[0].step == "GENERATE_LHS_SAMPLES"


@pytest.mark.anyio
async def test_stop_campaign() -> None:
    async with _make_client() as client:
        result = await client.stop_campaign()
    assert isinstance(result, CampaignStopResponse)
    assert result.status == "stopping"


# ---------------------------------------------------------------------------
# Samples
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_get_samples() -> None:
    async with _make_client() as client:
        result = await client.get_samples(page=1, per_page=10)
    assert isinstance(result, SamplesResponse)
    assert result.total == 2
    assert len(result.samples) == 2
    assert result.page == 1


@pytest.mark.anyio
async def test_get_sample_detail() -> None:
    async with _make_client() as client:
        result = await client.get_sample("s_0000")
    assert isinstance(result, SampleDetailResponse)
    assert result.sample_id == "s_0000"
    assert result.status == "ok"
    assert result.kpis is not None
    assert result.kpis["eui"] == 120.5


@pytest.mark.anyio
async def test_get_sample_not_found() -> None:
    async with _make_client() as client:
        with pytest.raises(NotFoundError) as exc_info:
            await client.get_sample("nonexistent")
    assert exc_info.value.status_code == 404


# ---------------------------------------------------------------------------
# Results & failures
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_get_results() -> None:
    async with _make_client() as client:
        result = await client.get_results()
    assert len(result) == 2
    assert isinstance(result[0], ResultRow)
    assert result[0].sample_id == "s_0000"


@pytest.mark.anyio
async def test_get_failures() -> None:
    async with _make_client() as client:
        result = await client.get_failures()
    assert len(result) == 1
    assert result[0].sample_id == "s_0001"


# ---------------------------------------------------------------------------
# Plots & pareto
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_get_plots() -> None:
    async with _make_client() as client:
        result = await client.get_plots()
    assert result.total == 1
    assert result.plots[0].name == "eui_hist.png"


@pytest.mark.anyio
async def test_get_pareto() -> None:
    async with _make_client() as client:
        result = await client.get_pareto()
    assert result.total_generations == 1


# ---------------------------------------------------------------------------
# Error mapping
# ---------------------------------------------------------------------------


def _error_handler(status: int) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"detail": f"Error {status}"})

    return httpx.MockTransport(handler)


@pytest.mark.anyio
async def test_authentication_error() -> None:
    client = _make_client(_error_handler(401))
    async with client:
        with pytest.raises(AuthenticationError) as exc_info:
            await client.health()
    assert exc_info.value.status_code == 401


@pytest.mark.anyio
async def test_forbidden_maps_to_auth_error() -> None:
    client = _make_client(_error_handler(403))
    async with client:
        with pytest.raises(AuthenticationError) as exc_info:
            await client.stop_campaign()
    assert exc_info.value.status_code == 403


@pytest.mark.anyio
async def test_not_found_error() -> None:
    client = _make_client(_error_handler(404))
    async with client:
        with pytest.raises(NotFoundError) as exc_info:
            await client.get_campaign()
    assert exc_info.value.status_code == 404


@pytest.mark.anyio
async def test_rate_limit_error_with_retry_after() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            json={"detail": "Rate limit exceeded"},
            headers={"Retry-After": "5"},
        )

    client = _make_client(httpx.MockTransport(handler))
    async with client:
        with pytest.raises(RateLimitError) as exc_info:
            await client.health()
    assert exc_info.value.status_code == 429
    assert exc_info.value.retry_after == 5


@pytest.mark.anyio
async def test_server_error() -> None:
    client = _make_client(_error_handler(500))
    async with client:
        with pytest.raises(ServerError) as exc_info:
            await client.get_campaign()
    assert exc_info.value.status_code == 500


@pytest.mark.anyio
async def test_generic_4xx_error() -> None:
    client = _make_client(_error_handler(422))
    async with client:
        with pytest.raises(OSimFlowAPIError) as exc_info:
            await client.get_campaign()
    assert exc_info.value.status_code == 422


# ---------------------------------------------------------------------------
# Connection errors
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_connection_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused")

    client = _make_client(httpx.MockTransport(handler))
    async with client:
        with pytest.raises(OSimFlowAPIError, match="Connection failed"):
            await client.health()


# ---------------------------------------------------------------------------
# API key header
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_api_key_header_sent() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(request.headers)
        return httpx.Response(200, json={"status": "alive"})

    client = _make_client(httpx.MockTransport(handler), api_key="my-secret-key")
    async with client:
        await client.health()
    assert captured.get("x-api-key") == "my-secret-key"


@pytest.mark.anyio
async def test_no_api_key_header_when_unset() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(request.headers)
        return httpx.Response(200, json={"status": "alive"})

    client = _make_client(httpx.MockTransport(handler))
    async with client:
        await client.health()
    assert "x-api-key" not in captured


# ---------------------------------------------------------------------------
# SSE events
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_events_stream() -> None:
    sse_payload = (
        "event: sample.started\n"
        'data: {"sample_id": "s_0000", "status": "running"}\n'
        "\n"
        "event: campaign.completed\n"
        'data: {"campaign_id": "test-campaign-001", "finished_at": 2000.0}\n'
        "\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=sse_payload,
            headers={"content-type": "text/event-stream"},
        )

    client = _make_client(httpx.MockTransport(handler))
    async with client:
        events: list[Event] = []
        async for evt in client.events():
            events.append(evt)
            if len(events) >= 2:
                break

    assert len(events) == 2
    assert events[0].event == "sample.started"
    assert events[0].data["sample_id"] == "s_0000"
    assert events[1].event == "campaign.completed"
    assert events[1].data["campaign_id"] == "test-campaign-001"


# ---------------------------------------------------------------------------
# Sample log
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_get_sample_log() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "/logs/stdout.log" in request.url.path:
            return httpx.Response(
                200, content="line1\nline2\n", headers={"content-type": "text/plain"}
            )
        return httpx.Response(404, json={"detail": "not found"})

    client = _make_client(httpx.MockTransport(handler))
    async with client:
        log_text = await client.get_sample_log("s_0000", "stdout.log")
    assert log_text == "line1\nline2\n"


# ---------------------------------------------------------------------------
# Error diagnosis
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_get_sample_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/api/v1/errors/"):
            return httpx.Response(
                200,
                json={
                    "sample_id": "s_0001",
                    "error_summary": "Severe error",
                    "failure_category": "convergence",
                    "diagnosis_suggestion": "Increase iterations",
                },
            )
        return httpx.Response(404, json={"detail": "not found"})

    client = _make_client(httpx.MockTransport(handler))
    async with client:
        result = await client.get_sample_error("s_0001")
    assert result["sample_id"] == "s_0001"
    assert result["failure_category"] == "convergence"


# ---------------------------------------------------------------------------
# Real server integration (uses FastAPI TestClient via ASGI transport)
# ---------------------------------------------------------------------------


@pytest.fixture
def real_outdir(tmp_path: Path) -> Path:
    """Create a temporary output directory with a sample run.json."""
    run_json = _make_app_json()
    (tmp_path / "run.json").write_text(json.dumps(run_json))
    return tmp_path


@pytest.mark.anyio
async def test_against_real_app(real_outdir: Path) -> None:
    """End-to-end test against the actual FastAPI app via ASGI transport."""
    from osimflow.api import create_app

    app = create_app(outdir=real_outdir)
    transport = httpx.ASGITransport(app=app)
    async with OSimFlowClient("http://test", transport=transport) as client:
        # Health
        health = await client.health()
        assert health.status == "alive"

        # Ready
        ready = await client.ready()
        assert ready.status == "ready"

        # Campaign
        campaign = await client.get_campaign()
        assert campaign.campaign_id == "test-campaign-001"

        # Steps
        steps = await client.get_steps()
        assert steps.total_steps == 2

        # Samples
        samples = await client.get_samples()
        assert samples.total == 2

        # Single sample
        detail = await client.get_sample("s_0000")
        assert detail.sample_id == "s_0000"


@pytest.mark.anyio
async def test_real_app_auth_error(real_outdir: Path) -> None:
    """Test that API key enforcement works end-to-end."""
    from osimflow.api import create_app

    app = create_app(outdir=real_outdir, api_key="secret-key")
    transport = httpx.ASGITransport(app=app)

    # Without API key → 401
    async with OSimFlowClient("http://test", transport=transport) as client:
        with pytest.raises(AuthenticationError):
            await client.get_campaign()

    # With correct API key → 200
    async with OSimFlowClient("http://test", api_key="secret-key", transport=transport) as client:
        result = await client.get_campaign()
        assert result.campaign_id == "test-campaign-001"


if __name__ == "__main__":
    # Allow running this file directly for quick manual checks
    asyncio.run(pytest.main([__file__, "-v"]))
