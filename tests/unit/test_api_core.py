"""Tests for osimflow/api/ core endpoints (issue #138) and security (issue #268)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="osimflow[api] extra required")
pytest.importorskip("slowapi", reason="osimflow[api] extra required")
from fastapi.testclient import TestClient

from osimflow.api import create_app, generate_api_key, validate_api_key


@pytest.fixture
def tmp_outdir(tmp_path: Path) -> Path:
    """Create a temporary output directory with a sample run.json."""
    run_json = {
        "schema_version": 1,
        "campaign_id": "test-campaign-001",
        "started_at": 1000.0,
        "finished_at": 2000.0,
        "config_summary": {"executor": "local", "n_samples": 5},
        "steps": [
            {"step": "GENERATE_LHS_SAMPLES", "cache": "MISS", "elapsed_s": 0.5, "exit_code": 0},
            {"step": "RUN_OPENSTUDIO_SIM", "cache": "MISS", "elapsed_s": 100.0, "exit_code": 0},
        ],
        "per_sample": [],
    }
    (tmp_path / "run.json").write_text(json.dumps(run_json))
    return tmp_path


@pytest.fixture
def client(tmp_outdir: Path) -> TestClient:
    app = create_app(outdir=tmp_outdir)
    return TestClient(app)


def test_health(client: TestClient) -> None:
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "alive"


def test_ready(client: TestClient) -> None:
    resp = client.get("/ready")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ready"


def test_campaign(client: TestClient) -> None:
    resp = client.get("/api/v1/campaign")
    assert resp.status_code == 200
    data = resp.json()
    assert data["campaign_id"] == "test-campaign-001"
    assert data["config_summary"]["executor"] == "local"


def test_steps(client: TestClient) -> None:
    resp = client.get("/api/v1/steps")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_steps"] == 2
    assert data["steps"][0]["step"] == "GENERATE_LHS_SAMPLES"


def test_no_outdir() -> None:
    app = create_app(outdir=None)
    client = TestClient(app)
    resp = client.get("/api/v1/campaign")
    assert resp.status_code == 503


def test_no_run_json(tmp_path: Path) -> None:
    app = create_app(outdir=tmp_path)
    client = TestClient(app)
    resp = client.get("/api/v1/campaign")
    assert resp.status_code == 404


class TestCreateApp:
    """Tests for the create_app factory."""

    def test_returns_fastapi_app(self) -> None:
        from fastapi import FastAPI

        app = create_app()
        assert isinstance(app, FastAPI)

    def test_app_title(self) -> None:
        app = create_app()
        assert app.title == "OSimFlow API"

    def test_read_only_default(self) -> None:
        app = create_app()
        assert app.state.read_only is True

    def test_read_only_false(self) -> None:
        app = create_app(read_only=False)
        assert app.state.read_only is False


class TestReadyEndpoint:
    """Tests for /ready readiness probe edge cases."""

    def test_ready_no_outdir(self) -> None:
        app = create_app(outdir=None)
        client = TestClient(app)
        resp = client.get("/ready")
        assert resp.status_code == 200
        assert resp.json()["status"] == "not_ready"

    def test_ready_no_run_json(self, tmp_path: Path) -> None:
        app = create_app(outdir=tmp_path)
        client = TestClient(app)
        resp = client.get("/ready")
        assert resp.status_code == 200
        assert resp.json()["status"] == "not_ready"

    def test_ready_with_run_json(self, tmp_outdir: Path) -> None:
        app = create_app(outdir=tmp_outdir)
        client = TestClient(app)
        resp = client.get("/ready")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ready"
        assert data["campaign_id"] == "test-campaign-001"


class TestCampaignEndpoint:
    """Tests for /api/v1/campaign endpoint."""

    def test_campaign_returns_baseline_comparison(self, tmp_outdir: Path) -> None:
        run_data = json.loads((tmp_outdir / "run.json").read_text())
        run_data["baseline_comparison"] = {"improvement_pct": 15.0}
        (tmp_outdir / "run.json").write_text(json.dumps(run_data))

        app = create_app(outdir=tmp_outdir)
        client = TestClient(app)
        resp = client.get("/api/v1/campaign")
        assert resp.status_code == 200
        assert resp.json()["baseline_comparison"] == {"improvement_pct": 15.0}

    def test_campaign_missing_fields(self, tmp_path: Path) -> None:
        (tmp_path / "run.json").write_text(json.dumps({"campaign_id": "minimal"}))
        app = create_app(outdir=tmp_path)
        client = TestClient(app)
        resp = client.get("/api/v1/campaign")
        data = resp.json()
        assert data["campaign_id"] == "minimal"
        assert data["config_summary"] == {}
        assert data["started_at"] is None
        assert data["finished_at"] is None


class TestStepsEndpoint:
    """Tests for /api/v1/steps endpoint."""

    def test_steps_empty(self, tmp_path: Path) -> None:
        (tmp_path / "run.json").write_text(json.dumps({"steps": []}))
        app = create_app(outdir=tmp_path)
        client = TestClient(app)
        resp = client.get("/api/v1/steps")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_steps"] == 0
        assert data["steps"] == []

    def test_steps_missing_key(self, tmp_path: Path) -> None:
        (tmp_path / "run.json").write_text(json.dumps({}))
        app = create_app(outdir=tmp_path)
        client = TestClient(app)
        resp = client.get("/api/v1/steps")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_steps"] == 0


class TestUnknownRoutes:
    """Tests for unknown route handling."""

    def test_unknown_route_returns_404(self, tmp_outdir: Path) -> None:
        app = create_app(outdir=tmp_outdir)
        client = TestClient(app)
        resp = client.get("/api/v1/nonexistent")
        assert resp.status_code == 404

    def test_unknown_root_route(self, tmp_outdir: Path) -> None:
        app = create_app(outdir=tmp_outdir)
        client = TestClient(app)
        resp = client.get("/unknown")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Security tests (issue #268)
# ---------------------------------------------------------------------------

TEST_API_KEY = "test-secret-key-12345"


class TestAPIKeyHelpers:
    """Unit tests for the pure auth helper functions."""

    def test_generate_api_key_returns_string(self) -> None:
        key = generate_api_key()
        assert isinstance(key, str)
        assert len(key) >= 32

    def test_generate_api_key_is_unique(self) -> None:
        keys = {generate_api_key() for _ in range(100)}
        assert len(keys) == 100  # all unique

    def test_validate_api_key_correct(self) -> None:
        assert validate_api_key("abc123", "abc123") is True

    def test_validate_api_key_wrong(self) -> None:
        assert validate_api_key("wrong", "abc123") is False

    def test_validate_api_key_none(self) -> None:
        assert validate_api_key(None, "abc123") is False


class TestAPIKeyAuth:
    """Tests for API key authentication on the running app."""

    def test_no_key_configured_allows_all(self, tmp_outdir: Path) -> None:
        """When api_key=None, authentication is disabled (backward compat)."""
        app = create_app(outdir=tmp_outdir, api_key=None)
        client = TestClient(app)
        resp = client.get("/api/v1/campaign")
        assert resp.status_code == 200

    def test_health_bypasses_auth(self, tmp_outdir: Path) -> None:
        """/health is always accessible, even with auth enabled."""
        app = create_app(outdir=tmp_outdir, api_key=TEST_API_KEY)
        client = TestClient(app)
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "alive"

    def test_protected_endpoint_without_key_returns_401(self, tmp_outdir: Path) -> None:
        app = create_app(outdir=tmp_outdir, api_key=TEST_API_KEY)
        client = TestClient(app)
        resp = client.get("/api/v1/campaign")
        assert resp.status_code == 401
        assert "API key" in resp.json()["detail"]

    def test_protected_endpoint_with_wrong_key_returns_401(self, tmp_outdir: Path) -> None:
        app = create_app(outdir=tmp_outdir, api_key=TEST_API_KEY)
        client = TestClient(app)
        resp = client.get("/api/v1/campaign", headers={"X-API-Key": "wrong-key"})
        assert resp.status_code == 401

    def test_protected_endpoint_with_correct_header_key(self, tmp_outdir: Path) -> None:
        app = create_app(outdir=tmp_outdir, api_key=TEST_API_KEY)
        client = TestClient(app)
        resp = client.get("/api/v1/campaign", headers={"X-API-Key": TEST_API_KEY})
        assert resp.status_code == 200

    def test_protected_endpoint_with_correct_query_key(self, tmp_outdir: Path) -> None:
        app = create_app(outdir=tmp_outdir, api_key=TEST_API_KEY)
        client = TestClient(app)
        resp = client.get(f"/api/v1/campaign?api_key={TEST_API_KEY}")
        assert resp.status_code == 200

    def test_ready_endpoint_requires_auth(self, tmp_outdir: Path) -> None:
        app = create_app(outdir=tmp_outdir, api_key=TEST_API_KEY)
        client = TestClient(app)
        resp = client.get("/ready")
        assert resp.status_code == 401

    def test_ready_endpoint_with_auth(self, tmp_outdir: Path) -> None:
        app = create_app(outdir=tmp_outdir, api_key=TEST_API_KEY)
        client = TestClient(app)
        resp = client.get("/ready", headers={"X-API-Key": TEST_API_KEY})
        assert resp.status_code == 200

    def test_api_key_stored_in_state(self) -> None:
        app = create_app(api_key=TEST_API_KEY)
        assert app.state.api_key == TEST_API_KEY

    def test_api_key_default_is_none(self) -> None:
        app = create_app()
        assert app.state.api_key is None

    def test_health_trailing_slash_bypasses_auth(self, tmp_outdir: Path) -> None:
        """/health/ (with trailing slash) should also bypass auth."""
        app = create_app(outdir=tmp_outdir, api_key=TEST_API_KEY)
        client = TestClient(app)
        resp = client.get("/health/")
        # FastAPI may redirect or handle; the middleware should not block it.
        assert resp.status_code in (200, 307)


class TestCORSMiddleware:
    """Tests for CORS configuration."""

    def test_no_cors_no_allow_origin_header(self, tmp_outdir: Path) -> None:
        app = create_app(outdir=tmp_outdir)
        client = TestClient(app)
        resp = client.get(
            "/api/v1/campaign",
            headers={"Origin": "http://evil.example.com"},
        )
        assert "access-control-allow-origin" not in resp.headers

    def test_cors_allowed_origin(self, tmp_outdir: Path) -> None:
        origin = "http://localhost:3000"
        app = create_app(outdir=tmp_outdir, cors_origins=[origin])
        client = TestClient(app)
        resp = client.get("/api/v1/campaign", headers={"Origin": origin})
        assert resp.headers.get("access-control-allow-origin") == origin

    def test_cors_wildcard_origin(self, tmp_outdir: Path) -> None:
        app = create_app(outdir=tmp_outdir, cors_origins=["*"])
        client = TestClient(app)
        resp = client.get(
            "/api/v1/campaign",
            headers={"Origin": "http://example.com"},
        )
        assert resp.status_code == 200

    def test_cors_preflight_options(self, tmp_outdir: Path) -> None:
        origin = "http://localhost:3000"
        app = create_app(outdir=tmp_outdir, cors_origins=[origin])
        client = TestClient(app)
        resp = client.options(
            "/api/v1/campaign",
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "X-API-Key",
            },
        )
        assert resp.status_code == 200
        assert resp.headers.get("access-control-allow-origin") == origin


class TestRateLimiting:
    """Tests for rate limiting via slowapi."""

    def test_rate_limit_allows_under_limit(self, tmp_outdir: Path) -> None:
        """A few requests under the limit should succeed."""
        app = create_app(outdir=tmp_outdir, rate_limit="10/minute")
        client = TestClient(app)
        for _ in range(5):
            resp = client.get("/health")
            assert resp.status_code == 200

    def test_rate_limit_blocks_over_limit(self, tmp_path: Path) -> None:
        """Exceeding the rate limit should return 429."""
        (tmp_path / "run.json").write_text(json.dumps({"campaign_id": "x"}))
        app = create_app(outdir=tmp_path, rate_limit="2/minute")
        client = TestClient(app)
        # First two requests succeed.
        assert client.get("/health").status_code == 200
        assert client.get("/health").status_code == 200
        # Third request should be rate limited.
        resp = client.get("/health")
        assert resp.status_code == 429

    def test_rate_limit_uses_x_forwarded_for(self, tmp_path: Path) -> None:
        """X-Forwarded-For header should be used for per-client rate limiting.

        When behind a load balancer, the real client IP is passed via
        X-Forwarded-For.  The rate limiter must use this header to enforce
        per-client limits correctly in horizontal scaling deployments.
        """
        (tmp_path / "run.json").write_text(json.dumps({"campaign_id": "x"}))
        app = create_app(outdir=tmp_path, rate_limit="2/minute")
        client = TestClient(app)

        # Simulate two different clients via X-Forwarded-For.
        # Each should get its own 2/minute limit.
        resp1 = client.get("/health", headers={"X-Forwarded-For": "192.168.1.100"})
        assert resp1.status_code == 200
        resp2 = client.get("/health", headers={"X-Forwarded-For": "192.168.1.100"})
        assert resp2.status_code == 200
        # Third request from same IP should be rate limited.
        resp3 = client.get("/health", headers={"X-Forwarded-For": "192.168.1.100"})
        assert resp3.status_code == 429

        # A different client (different X-Forwarded-For) should not be
        # affected by the first client's rate limit.
        resp4 = client.get("/health", headers={"X-Forwarded-For": "10.0.0.1"})
        assert resp4.status_code == 200

    def test_rate_limit_x_forwarded_for_with_port(self, tmp_path: Path) -> None:
        """X-Forwarded-For may contain port numbers; only the IP is used."""
        (tmp_path / "run.json").write_text(json.dumps({"campaign_id": "x"}))
        app = create_app(outdir=tmp_path, rate_limit="1/minute")
        client = TestClient(app)

        # X-Forwarded-For with port should still identify the client correctly.
        resp1 = client.get("/health", headers={"X-Forwarded-For": "192.168.1.100:8080"})
        assert resp1.status_code == 200
        # Second request from same IP:port combo is rate limited.
        resp2 = client.get("/health", headers={"X-Forwarded-For": "192.168.1.100:8080"})
        assert resp2.status_code == 429


class TestReadOnlyDefault:
    """Tests for the secure read-only default."""

    def test_read_only_default_true(self) -> None:
        app = create_app()
        assert app.state.read_only is True

    def test_read_only_false_when_disabled(self) -> None:
        app = create_app(read_only=False)
        assert app.state.read_only is False

    def test_limiter_stored_in_state(self) -> None:
        app = create_app()
        assert hasattr(app.state, "limiter")

    def test_rate_limit_configurable(self) -> None:
        app = create_app(rate_limit="100/minute")
        # The limiter should be created with the given default limit.
        assert app.state.limiter is not None


class TestCLIArgumentChanges:
    """Tests that the serve subcommand CLI arguments changed (issue #268)."""

    def test_serve_has_enable_writes_flag(self) -> None:
        from osimflow.__main__ import _build_parser

        parser = _build_parser()
        # --enable-writes should be accepted.
        args = parser.parse_args(["serve", "--outdir", "/tmp/x", "--enable-writes"])
        assert args.enable_writes is True

    def test_serve_enable_writes_defaults_false(self) -> None:
        from osimflow.__main__ import _build_parser

        parser = _build_parser()
        args = parser.parse_args(["serve", "--outdir", "/tmp/x"])
        assert args.enable_writes is False

    def test_serve_host_defaults_localhost(self) -> None:
        from osimflow.__main__ import _build_parser

        parser = _build_parser()
        args = parser.parse_args(["serve", "--outdir", "/tmp/x"])
        assert args.host == "127.0.0.1"

    def test_serve_api_key_flag(self) -> None:
        from osimflow.__main__ import _build_parser

        parser = _build_parser()
        args = parser.parse_args(["serve", "--outdir", "/tmp/x", "--api-key", "mykey"])
        assert args.api_key == "mykey"

    def test_serve_cors_origins_flag(self) -> None:
        from osimflow.__main__ import _build_parser

        parser = _build_parser()
        args = parser.parse_args(
            ["serve", "--outdir", "/tmp/x", "--cors-origins", "http://a.com,http://b.com"]
        )
        assert args.cors_origins == "http://a.com,http://b.com"

    def test_serve_rate_limit_flag(self) -> None:
        from osimflow.__main__ import _build_parser

        parser = _build_parser()
        args = parser.parse_args(["serve", "--outdir", "/tmp/x", "--rate-limit", "120/minute"])
        assert args.rate_limit == "120/minute"
