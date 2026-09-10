"""Tests for osimflow/api/ core endpoints (issue #138) and security (issue #268)."""

from __future__ import annotations

import hashlib
import json
import warnings
from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="osimflow[api] extra required")
pytest.importorskip("slowapi", reason="osimflow[api] extra required")
from fastapi import Request
from fastapi.testclient import TestClient

from osimflow.api import create_app, generate_api_key, validate_api_key
from osimflow.api.app import _get_api_key_from_request, _make_per_user_key_func
from osimflow.api.auth import (
    API_KEY_QUERY_PARAM_MIGRATION_HINT,
    APIKeyQueryParameterError,
    extract_api_key,
)


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


class TestCampaignHealthEndpoint:
    """Tests for /api/v1/health endpoint (issue #437)."""

    def test_health_returns_overall_status(self, tmp_outdir: Path) -> None:
        app = create_app(outdir=tmp_outdir)
        client = TestClient(app)
        resp = client.get("/api/v1/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["overall_status"] in ("healthy", "degraded", "unknown")
        assert data["campaign_id"] == "test-campaign-001"

    def test_health_returns_sample_counts(self, tmp_outdir: Path) -> None:
        app = create_app(outdir=tmp_outdir)
        client = TestClient(app)
        resp = client.get("/api/v1/health")
        assert resp.status_code == 200
        data = resp.json()
        assert "samples" in data
        assert "total" in data["samples"]
        assert "success" in data["samples"]
        assert "failed" in data["samples"]
        assert "running" in data["samples"]
        assert "cached" in data["samples"]

    def test_health_returns_step_statuses(self, tmp_outdir: Path) -> None:
        app = create_app(outdir=tmp_outdir)
        client = TestClient(app)
        resp = client.get("/api/v1/health")
        assert resp.status_code == 200
        data = resp.json()
        assert "steps" in data
        assert len(data["steps"]) == 2
        assert data["steps"][0]["step"] == "GENERATE_LHS_SAMPLES"

    def test_health_returns_timestamps(self, tmp_outdir: Path) -> None:
        app = create_app(outdir=tmp_outdir)
        client = TestClient(app)
        resp = client.get("/api/v1/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["started_at"] == 1000.0
        assert data["finished_at"] == 2000.0

    def test_health_no_outdir(self) -> None:
        app = create_app(outdir=None)
        client = TestClient(app)
        resp = client.get("/api/v1/health")
        assert resp.status_code == 503

    def test_health_no_run_json(self, tmp_path: Path) -> None:
        app = create_app(outdir=tmp_path)
        client = TestClient(app)
        resp = client.get("/api/v1/health")
        assert resp.status_code == 404

    def test_health_running_campaign(self, tmp_path: Path) -> None:
        run_json = {
            "schema_version": 1,
            "campaign_id": "running-campaign",
            "status": "running",
            "started_at": 1000.0,
            "steps": [],
            "per_sample": [
                {"sample_id": "s0", "status": "ok", "elapsed_s": 10.0},
                {"sample_id": "s1", "status": "failed", "elapsed_s": 5.0},
            ],
        }
        (tmp_path / "run.json").write_text(json.dumps(run_json))
        app = create_app(outdir=tmp_path)
        client = TestClient(app)
        resp = client.get("/api/v1/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["overall_status"] == "degraded"
        assert data["campaign_status"] == "running"
        assert data["samples"]["total"] == 2
        assert data["samples"]["success"] == 1
        assert data["samples"]["failed"] == 1

    def test_health_success_campaign_no_failures(self, tmp_path: Path) -> None:
        run_json = {
            "schema_version": 1,
            "campaign_id": "success-campaign",
            "status": "success",
            "started_at": 1000.0,
            "finished_at": 2000.0,
            "steps": [],
            "per_sample": [
                {"sample_id": "s0", "status": "ok", "elapsed_s": 10.0},
                {"sample_id": "s1", "status": "ok", "elapsed_s": 11.0},
            ],
        }
        (tmp_path / "run.json").write_text(json.dumps(run_json))
        app = create_app(outdir=tmp_path)
        client = TestClient(app)
        resp = client.get("/api/v1/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["overall_status"] == "healthy"
        assert data["campaign_status"] == "success"

    def test_health_cancelled_campaign(self, tmp_path: Path) -> None:
        run_json = {
            "schema_version": 1,
            "campaign_id": "cancelled-campaign",
            "status": "cancelled",
            "started_at": 1000.0,
            "finished_at": 1500.0,
            "steps": [],
            "per_sample": [],
        }
        (tmp_path / "run.json").write_text(json.dumps(run_json))
        app = create_app(outdir=tmp_path)
        client = TestClient(app)
        resp = client.get("/api/v1/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["overall_status"] == "unhealthy"
        assert data["campaign_status"] == "cancelled"


class TestCampaignHealthDetailsEndpoint:
    """Tests for /api/v1/health/details endpoint (issue #437)."""

    def test_health_details_returns_full_run_json(self, tmp_outdir: Path) -> None:
        app = create_app(outdir=tmp_outdir)
        client = TestClient(app)
        resp = client.get("/api/v1/health/details")
        assert resp.status_code == 200
        data = resp.json()
        assert data["campaign_id"] == "test-campaign-001"
        assert data["schema_version"] == 1
        assert "steps" in data
        assert "per_sample" in data

    def test_health_details_no_outdir(self) -> None:
        app = create_app(outdir=None)
        client = TestClient(app)
        resp = client.get("/api/v1/health/details")
        assert resp.status_code == 503

    def test_health_details_no_run_json(self, tmp_path: Path) -> None:
        app = create_app(outdir=tmp_path)
        client = TestClient(app)
        resp = client.get("/api/v1/health/details")
        assert resp.status_code == 404


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

    def test_query_param_key_rejected_with_migration_hint(self, tmp_outdir: Path) -> None:
        """A valid key supplied ONLY via ?api_key= no longer authenticates (issue #1466)."""
        app = create_app(outdir=tmp_outdir, api_key=TEST_API_KEY)
        client = TestClient(app)
        resp = client.get(f"/api/v1/campaign?api_key={TEST_API_KEY}")
        assert resp.status_code == 401
        assert API_KEY_QUERY_PARAM_MIGRATION_HINT in resp.json()["detail"]

    def test_query_param_key_rejected_in_multi_user_mode(self, tmp_outdir: Path) -> None:
        """Query-param transport is dropped in multi-user mode too (issue #1466)."""
        import hashlib

        keys_file = tmp_outdir / "api_keys.json"
        keys_file.write_text(
            json.dumps(
                {
                    "users": [
                        {
                            "key_sha256": hashlib.sha256(TEST_API_KEY.encode()).hexdigest(),
                            "user_id": "alice",
                            "role": "admin",
                        },
                    ]
                }
            )
        )
        keys_file.chmod(0o600)
        app = create_app(outdir=tmp_outdir, api_keys_file=keys_file)
        client = TestClient(app)
        resp = client.get(f"/api/v1/campaign?api_key={TEST_API_KEY}")
        assert resp.status_code == 401
        assert API_KEY_QUERY_PARAM_MIGRATION_HINT in resp.json()["detail"]

    def test_header_takes_precedence_over_query_param(self, tmp_outdir: Path) -> None:
        """When both are present the header wins (issue #1466).

        Header-precedence keeps the rejection narrowly scoped to the
        removed channel: a client that already sends the header is not
        broken by a stale link that also carries ?api_key=.
        """
        app = create_app(outdir=tmp_outdir, api_key=TEST_API_KEY)
        client = TestClient(app)
        resp = client.get(
            f"/api/v1/campaign?api_key={TEST_API_KEY}",
            headers={"X-API-Key": TEST_API_KEY},
        )
        assert resp.status_code == 200

    @staticmethod
    def _make_request(headers: dict[str, str], query_string: str = "") -> Request:
        """Build a minimal starlette Request for helper-level tests."""
        return Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/api/v1/campaign",
                "headers": [
                    (name.lower().encode(), value.encode()) for name, value in headers.items()
                ],
                "query_string": query_string.encode(),
            }
        )

    def test_extract_api_key_header_only(self) -> None:
        """extract_api_key reads the header and ignores nothing else (issue #1466)."""
        req = self._make_request({"X-API-Key": TEST_API_KEY})
        assert extract_api_key(req) == TEST_API_KEY
        req_no_key = self._make_request({})
        assert extract_api_key(req_no_key) is None

    def test_extract_api_key_raises_on_query_param(self) -> None:
        """extract_api_key raises the migration-hint error for ?api_key= (issue #1466)."""
        req = self._make_request({}, query_string=f"api_key={TEST_API_KEY}")
        with pytest.raises(APIKeyQueryParameterError, match="X-API-Key header"):
            extract_api_key(req)

    def test_rate_limiter_key_func_ignores_query_param(self) -> None:
        """The limiter key func no longer extracts keys from the query string."""
        req = self._make_request({}, query_string=f"api_key={TEST_API_KEY}")
        assert _get_api_key_from_request(req) is None

    def test_rate_limiter_identity_is_hashed(self) -> None:
        """Per-user limiter identity is a SHA-256 digest, not the raw key (issue #1466)."""
        req = self._make_request({"X-API-Key": TEST_API_KEY})
        identity = _make_per_user_key_func()(req)
        expected = f"user:{hashlib.sha256(TEST_API_KEY.encode()).hexdigest()}"
        assert identity == expected
        # The raw bearer-equivalent credential must not appear in the
        # identity string that lands in limiter state (dict keys / Redis).
        assert TEST_API_KEY not in identity

    def test_rate_limiter_identity_stable_across_identical_keys(self) -> None:
        """Same key → same identity (stable across processes/instances)."""
        key_func = _make_per_user_key_func()
        first = key_func(self._make_request({"X-API-Key": TEST_API_KEY}))
        second = key_func(self._make_request({"X-API-Key": TEST_API_KEY}))
        assert first == second

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


class TestServeAuthWarning:
    """Tests for the SEC-001 non-local no-auth warning (issue #1095).

    The CLI helper lives in ``osimflow.__main__``; the behaviour it
    encodes is that a network-accessible bind with no key store must
    emit a loud warning.
    """

    def _import_helper(self):
        from osimflow import __main__ as cli

        return cli

    def test_warns_when_auth_disabled_on_nonlocal_bind(self) -> None:
        cli = self._import_helper()
        with pytest.warns(UserWarning, match="Authentication is DISABLED"):
            cli._warn_if_auth_disabled_nonlocal(None, None, "0.0.0.0", 8000)

    def test_warns_on_star_bind(self) -> None:
        cli = self._import_helper()
        with pytest.warns(UserWarning, match="Authentication is DISABLED"):
            cli._warn_if_auth_disabled_nonlocal(None, None, "*", 8000)

    def test_no_warning_when_api_key_set(self) -> None:
        cli = self._import_helper()
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            cli._warn_if_auth_disabled_nonlocal("secret", None, "0.0.0.0", 8000)

    def test_no_warning_when_keys_file_set(self, tmp_path: Path) -> None:
        cli = self._import_helper()
        keys_file = tmp_path / "keys.json"
        keys_file.write_text('{"users": [{"key": "k", "user_id": "u", "role": "admin"}]}')
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            cli._warn_if_auth_disabled_nonlocal(None, keys_file, "0.0.0.0", 8000)

    def test_no_warning_on_localhost_bind(self) -> None:
        cli = self._import_helper()
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            cli._warn_if_auth_disabled_nonlocal(None, None, "127.0.0.1", 8000)

    def test_no_warning_on_localhost_default(self) -> None:
        cli = self._import_helper()
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            cli._warn_if_auth_disabled_nonlocal(None, None, "localhost", 8000)

    def test_no_warning_when_auth_enabled_on_localhost(self) -> None:
        cli = self._import_helper()
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            cli._warn_if_auth_disabled_nonlocal("secret", None, "localhost", 8000)


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
    """Tests for rate limiting via the custom middleware (issue #454)."""

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
        assert client.get("/health").status_code == 200
        assert client.get("/health").status_code == 200
        resp = client.get("/health")
        assert resp.status_code == 429, f"Expected 429 but got {resp.status_code}"

    def test_rate_limit_uses_x_forwarded_for(self, tmp_path: Path) -> None:
        """X-Forwarded-For header should be used for per-client rate limiting.

        When behind a load balancer, the real client IP is passed via
        X-Forwarded-For.  The rate limiter must use this header to enforce
        per-client limits correctly in horizontal scaling deployments —
        but only when the operator explicitly opts in via
        ``trust_x_forwarded_for`` + a ``trusted_proxies`` allowlist
        (issue #1683).  TestClient connects from the ``testclient``
        peer, so the allowlist must include it for the XFF chain to
        be honored.
        """
        (tmp_path / "run.json").write_text(json.dumps({"campaign_id": "x"}))
        app = create_app(
            outdir=tmp_path,
            rate_limit="2/minute",
            trust_x_forwarded_for=True,
            trusted_proxies=["testclient"],
        )
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
        app = create_app(
            outdir=tmp_path,
            rate_limit="1/minute",
            trust_x_forwarded_for=True,
            trusted_proxies=["testclient"],
        )
        client = TestClient(app)

        # X-Forwarded-For with port should still identify the client correctly.
        resp1 = client.get("/health", headers={"X-Forwarded-For": "192.168.1.100:8080"})
        assert resp1.status_code == 200
        # Second request from same IP:port combo is rate limited.
        resp2 = client.get("/health", headers={"X-Forwarded-For": "192.168.1.100:8080"})
        assert resp2.status_code == 429

    # --- issue #1683: default-deny X-Forwarded-For, opt-in trust gate ---

    def test_rate_limit_ignores_spoofed_xff_by_default(self, tmp_path: Path) -> None:
        """Default: spoofed X-Forwarded-For must NOT bypass the rate limit.

        Without ``trust_x_forwarded_for=True``, every request from
        TestClient collapses to the same ``testclient`` peer bucket, so
        rotating the XFF value cannot grant a fresh bucket.  N distinct
        spoofed XFF values under the same real client IP must share
        ONE bucket (issue #1683 acceptance criterion).
        """
        (tmp_path / "run.json").write_text(json.dumps({"campaign_id": "x"}))
        app = create_app(outdir=tmp_path, rate_limit="2/minute")
        client = TestClient(app)

        # First two requests (any XFF) succeed.
        resp1 = client.get("/health", headers={"X-Forwarded-For": "203.0.113.1"})
        assert resp1.status_code == 200
        resp2 = client.get("/health", headers={"X-Forwarded-For": "203.0.113.2"})
        assert resp2.status_code == 200

        # Third request, regardless of XFF, is rate-limited because the
        # socket peer bucket is shared.
        resp3 = client.get("/health", headers={"X-Forwarded-For": "203.0.113.99"})
        assert resp3.status_code == 429, (
            "Spoofed XFF must not reset the rate-limit bucket when "
            "trust_x_forwarded_for is disabled (issue #1683)."
        )

        # Same real peer, different spoofed XFF: still rate-limited.
        resp4 = client.get("/health", headers={"X-Forwarded-For": "198.51.100.7"})
        assert resp4.status_code == 429

    def test_rate_limit_trusted_peer_xff_only_when_in_allowlist(self, tmp_path: Path) -> None:
        """Opt-in XFF trust: only honored when peer is in the allowlist.

        With ``trust_x_forwarded_for=True`` + a ``trusted_proxies``
        allowlist, XFF is honored only if the immediate upstream (the
        socket peer) matches the allowlist (issue #1683).

        TestClient connects from ``testclient``; when the allowlist
        permits ``testclient``, the XFF chain is honored and different
        spoofed XFF values yield different buckets.  When the allowlist
        excludes the peer, XFF is ignored and all requests share the
        peer bucket.
        """
        # Sub-case A: allowlist matches peer → XFF honored, distinct
        # spoofed XFFs yield distinct buckets.
        (tmp_path / "run.json").write_text(json.dumps({"campaign_id": "x"}))
        app_trusted = create_app(
            outdir=tmp_path,
            rate_limit="1/minute",
            trust_x_forwarded_for=True,
            trusted_proxies=["testclient"],
        )
        client_trusted = TestClient(app_trusted)

        # Spoofed XFF #1: first hit, allowed.
        r = client_trusted.get("/health", headers={"X-Forwarded-For": "203.0.113.1"})
        assert r.status_code == 200
        # Same spoofed XFF: rate-limited.
        r = client_trusted.get("/health", headers={"X-Forwarded-For": "203.0.113.1"})
        assert r.status_code == 429
        # Different spoofed XFF: new bucket, allowed (this is the
        # opt-in semantics — operator accepted the trust gate, XFF is
        # the client identity).
        r = client_trusted.get("/health", headers={"X-Forwarded-For": "203.0.113.2"})
        assert r.status_code == 200

        # Sub-case B: allowlist does NOT match peer → XFF ignored, all
        # requests collapse to the peer bucket.
        (tmp_path / "run.json").write_text(json.dumps({"campaign_id": "x"}))
        app_untrusted = create_app(
            outdir=tmp_path,
            rate_limit="1/minute",
            trust_x_forwarded_for=True,
            # ``10.0.0.0/8`` does NOT cover the ``testclient`` peer.
            trusted_proxies=["10.0.0.0/8"],
        )
        client_untrusted = TestClient(app_untrusted)

        r1 = client_untrusted.get("/health", headers={"X-Forwarded-For": "203.0.113.1"})
        assert r1.status_code == 200
        r2 = client_untrusted.get("/health", headers={"X-Forwarded-For": "203.0.113.2"})
        assert r2.status_code == 429, (
            "When the socket peer is not in the trusted-proxy allowlist, "
            "XFF must be ignored even with trust_x_forwarded_for=True "
            "(issue #1683)."
        )

    def test_rate_limit_first_untrusted_hop_in_xff_chain(self, tmp_path: Path) -> None:
        """Opt-in XFF trust: the "first untrusted hop" rule is honored.

        When the immediate upstream is trusted and the XFF chain is
        ``client, proxy1, proxy2``, the client IP is the first (leftmost)
        entry that is NOT in the allowlist — i.e. the ``client`` IP.
        This is the canonical RFC 7239 / nginx ``real_ip`` behavior
        (issue #1683).
        """
        (tmp_path / "run.json").write_text(json.dumps({"campaign_id": "x"}))
        # Trust ``testclient`` (TestClient peer) AND ``198.51.100.0/24``
        # (one of the trusted proxies in the chain).  The client
        # ``203.0.113.50`` is NOT trusted, so it must be the resolved
        # client IP.
        app = create_app(
            outdir=tmp_path,
            rate_limit="1/minute",
            trust_x_forwarded_for=True,
            trusted_proxies=["testclient", "198.51.100.0/24"],
        )
        client = TestClient(app)

        # XFF chain: true client ``203.0.113.50``, trusted proxy
        # ``198.51.100.10``, peer ``testclient``.  Resolved client IP
        # is ``203.0.113.50``.
        r = client.get(
            "/health",
            headers={"X-Forwarded-For": "203.0.113.50, 198.51.100.10"},
        )
        assert r.status_code == 200
        # Same true client (different left-most hop order preserved),
        # second request → rate-limited.
        r = client.get(
            "/health",
            headers={"X-Forwarded-For": "203.0.113.50, 198.51.100.99"},
        )
        assert r.status_code == 429

        # A DIFFERENT true client (leftmost untrusted IP) gets a fresh
        # bucket.
        r = client.get(
            "/health",
            headers={"X-Forwarded-For": "203.0.113.99, 198.51.100.10"},
        )
        assert r.status_code == 200

    def test_trust_xff_without_allowlist_fails_closed(self, tmp_path: Path) -> None:
        """Opting into XFF trust without a proxy allowlist must fail closed.

        Issue #1683 requires that turning the gate on without an
        allowlist raises ``ValueError`` at app creation — accepting
        XFF from any upstream would silently re-introduce the bypass.
        """
        with pytest.raises(ValueError, match="trusted_proxies"):
            create_app(
                outdir=tmp_path,
                trust_x_forwarded_for=True,
                trusted_proxies=[],
            )

        # Empty env-var + env-enabled trust also fails closed.
        import os

        from osimflow import api as _api_pkg  # noqa: PLC0415

        prior = os.environ.pop("OSIMFLOW_TRUSTED_PROXIES", None)
        os.environ["OSIMFLOW_TRUST_X_FORWARDED_FOR"] = "1"
        try:
            with pytest.raises(ValueError, match="trusted_proxies"):
                _api_pkg.create_app(trust_x_forwarded_for=False)
        finally:
            os.environ.pop("OSIMFLOW_TRUST_X_FORWARDED_FOR", None)
            if prior is not None:
                os.environ["OSIMFLOW_TRUSTED_PROXIES"] = prior

    def test_invalid_trusted_proxy_raises(self, tmp_path: Path) -> None:
        """Bad CIDR / IP entries in ``trusted_proxies`` fail at app creation."""
        with pytest.raises(ValueError, match="Invalid trusted-proxy"):
            create_app(
                outdir=tmp_path,
                trust_x_forwarded_for=True,
                trusted_proxies=["bad@entry"],
            )

    def test_rate_limit_redis_backed_allows_under_limit(self, tmp_path: Path) -> None:
        """Redis-backed rate limiter allows requests under the limit (issue #663).

        When ``redis_url`` is set, the rate limiter uses Redis sorted sets
        so that the counter is shared across multiple API instances behind
        a load balancer.  This test verifies the Redis path is taken and
        requests under the limit succeed.
        """
        from unittest.mock import AsyncMock, MagicMock, patch

        (tmp_path / "run.json").write_text(json.dumps({"campaign_id": "x"}))

        # Build a mock async Redis client with pipeline support.
        mock_pipeline = AsyncMock()
        # pipeline.execute() returns [zremrangebyscore result, zcard result, zadd result, expire result]
        mock_pipeline.execute.return_value = [0, 0, 1, True]
        mock_client = AsyncMock()
        mock_client.pipeline.return_value = mock_pipeline
        # zrange for retry_after calculation (only called when over limit)
        mock_client.zrange = AsyncMock(return_value=[])

        mock_ra = MagicMock()
        mock_ra.from_url.return_value = mock_client

        with patch("osimflow.api.app._get_redis_asyncio", return_value=mock_ra):
            app = create_app(
                outdir=tmp_path, rate_limit="10/minute", redis_url="redis://localhost:6379/0"
            )
            client = TestClient(app)

        # Requests under limit should succeed
        for _ in range(5):
            resp = client.get("/health")
            assert resp.status_code == 200, f"Expected 200 but got {resp.status_code}"

    def test_rate_limit_redis_backed_blocks_over_limit(self, tmp_path: Path) -> None:
        """Redis-backed rate limiter blocks requests over the limit (issue #663).

        When the rate limit is exceeded using the Redis backend, the
        middleware should return 429 with a Retry-After header, the same
        as the in-process limiter.  This confirms the fix works across
        multiple API instances sharing the same Redis backend.
        """
        from unittest.mock import AsyncMock, MagicMock, patch

        (tmp_path / "run.json").write_text(json.dumps({"campaign_id": "x"}))

        # Build a mock async Redis client that reports being over the limit.
        # First 2 requests succeed, 3rd is over limit.
        call_count = {"count": 0}

        async def mock_execute() -> list:
            call_count["count"] += 1
            if call_count["count"] <= 2:
                # Under limit: 0 entries before adding
                return [0, 0, 1, True]
            else:
                # Over limit: already have 2 entries (at max_requests)
                return [0, 2, 1, True]

        mock_pipeline = AsyncMock()
        mock_pipeline.execute.side_effect = mock_execute

        # When over limit, zrange is called to compute retry_after
        mock_oldest = AsyncMock(return_value=[("entry1", 1000.0)])
        mock_client = AsyncMock()
        mock_client.pipeline.return_value = mock_pipeline
        mock_client.zrange = mock_oldest

        mock_ra = MagicMock()
        mock_ra.from_url.return_value = mock_client

        with patch("osimflow.api.app._get_redis_asyncio", return_value=mock_ra):
            app = create_app(
                outdir=tmp_path, rate_limit="2/minute", redis_url="redis://localhost:6379/0"
            )
            client = TestClient(app)

        # First two requests should succeed
        assert client.get("/health").status_code == 200
        assert client.get("/health").status_code == 200
        # Third request should be rate limited
        resp = client.get("/health")
        assert resp.status_code == 429, f"Expected 429 but got {resp.status_code}"
        assert "Retry-After" in resp.headers

    def test_rate_limit_redis_backed_fallback_on_redis_error(self, tmp_path: Path) -> None:
        """Redis-backed rate limiter falls back to in-process on Redis failure (issue #663).

        When Redis is unavailable or returns an error, the middleware should
        fall back to the in-process counter so the API remains available even if
        Redis goes down.
        """
        from unittest.mock import MagicMock, patch

        (tmp_path / "run.json").write_text(json.dumps({"campaign_id": "x"}))

        # Mock Redis that raises an exception
        mock_ra = MagicMock()
        mock_ra.from_url.side_effect = Exception("Redis connection refused")

        with patch("osimflow.api.app._get_redis_asyncio", return_value=mock_ra):
            app = create_app(
                outdir=tmp_path, rate_limit="2/minute", redis_url="redis://localhost:6379/0"
            )
            client = TestClient(app)

        # Should fall back to in-process and work normally
        assert client.get("/health").status_code == 200
        assert client.get("/health").status_code == 200
        resp = client.get("/health")
        assert resp.status_code == 429, (
            f"Expected 429 (fallback to in-process) but got {resp.status_code}"
        )


class TestApiRedisUrlValidation:
    """create_app enforces the rediss:// TLS baseline on redis_url (issue #1467).

    The distributed rate-limiter store is shared security state (per-key
    abuse counters); a MITM on a plaintext connection could read and reset
    it.  ``create_app`` must therefore reject insecure URLs at app
    creation (fail closed), with the same semantics as
    ``osimflow.distributed_cache.validate_redis_url`` used by
    ``build_cache`` (``require_auth=False`` parity).
    """

    def test_nonlocalhost_redis_rejected_at_app_creation(self, tmp_path: Path) -> None:
        """A plaintext redis:// URL to a remote host must raise at create_app."""
        (tmp_path / "run.json").write_text(json.dumps({"campaign_id": "x"}))
        with pytest.raises(ValueError, match="issue #1321"):
            create_app(
                outdir=tmp_path,
                rate_limit="60/minute",
                redis_url="redis://redis.example.com:6379/0",
            )

    def test_nonlocalhost_redis_with_creds_still_rejected(self, tmp_path: Path) -> None:
        """TLS is mandatory for non-localhost even when credentials are embedded."""
        (tmp_path / "run.json").write_text(json.dumps({"campaign_id": "x"}))
        with pytest.raises(ValueError, match="issue #1321"):
            create_app(
                outdir=tmp_path,
                rate_limit="60/minute",
                redis_url="redis://user:pass@redis.example.com:6379/0",
            )

    def test_nonlocalhost_rediss_without_creds_rejected(self, tmp_path: Path) -> None:
        """rediss:// without credentials is rejected (require_auth parity with build_cache)."""
        (tmp_path / "run.json").write_text(json.dumps({"campaign_id": "x"}))
        with pytest.raises(ValueError, match="issue #1277"):
            create_app(
                outdir=tmp_path,
                rate_limit="60/minute",
                redis_url="rediss://redis.example.com:6379/0",
            )

    def test_nonlocalhost_rediss_with_creds_accepted(self, tmp_path: Path) -> None:
        """rediss:// with embedded credentials passes validation and app creation.

        The async Redis client is constructed lazily on first request, so
        creating the app must not touch the network — asserted by patching
        ``_get_redis_asyncio`` to fail if called.
        """
        from unittest.mock import patch

        (tmp_path / "run.json").write_text(json.dumps({"campaign_id": "x"}))

        def _fail_if_called() -> None:
            raise AssertionError("Redis client must not be constructed at create_app time")

        with patch("osimflow.api.app._get_redis_asyncio", side_effect=_fail_if_called):
            app = create_app(
                outdir=tmp_path,
                rate_limit="60/minute",
                redis_url="rediss://user:pass@redis.example.com:6379/0",
            )
        assert app.title == "OSimFlow API"

    def test_localhost_redis_accepted(self, tmp_path: Path) -> None:
        """Loopback redis:// URLs are exempt from the TLS baseline (cache parity)."""
        from unittest.mock import patch

        (tmp_path / "run.json").write_text(json.dumps({"campaign_id": "x"}))

        def _fail_if_called() -> None:
            raise AssertionError("Redis client must not be constructed at create_app time")

        for loopback_url in (
            "redis://localhost:6379/0",
            "redis://127.0.0.1:6379/0",
            "redis://[::1]:6379/0",
        ):
            with patch("osimflow.api.app._get_redis_asyncio", side_effect=_fail_if_called):
                app = create_app(outdir=tmp_path, rate_limit="60/minute", redis_url=loopback_url)
                assert app.title == "OSimFlow API"


class TestRateLimitKeyValidation:
    """Tests for rate_limit_key validation (issue #1329)."""

    def test_rate_limit_key_valid_values(self, tmp_outdir: Path) -> None:
        """Valid rate_limit_key values should be accepted."""
        for key in ("ip", "user", "campaign"):
            app = create_app(outdir=tmp_outdir, rate_limit_key=key)
            assert app.state.rate_limit_key == key

    def test_rate_limit_key_empty_raises(self, tmp_outdir: Path) -> None:
        """Empty rate_limit_key should raise ValueError."""
        with pytest.raises(ValueError, match="non-empty"):
            create_app(outdir=tmp_outdir, rate_limit_key="")

    def test_rate_limit_key_too_long_raises(self, tmp_outdir: Path) -> None:
        """rate_limit_key longer than 64 characters should raise ValueError."""
        long_key = "a" * 65
        with pytest.raises(ValueError, match="at most 64"):
            create_app(outdir=tmp_outdir, rate_limit_key=long_key)

    def test_rate_limit_key_64_chars_ok(self, tmp_outdir: Path) -> None:
        """Valid rate_limit_key at max length should be accepted."""
        app = create_app(outdir=tmp_outdir, rate_limit_key="ip")
        assert app.state.rate_limit_key == "ip"

    def test_rate_limit_key_non_ascii_raises(self, tmp_outdir: Path) -> None:
        """rate_limit_key with non-ASCII characters should raise ValueError."""
        with pytest.raises(ValueError, match="printable ASCII"):
            create_app(outdir=tmp_outdir, rate_limit_key="us\u00e9r")

    def test_rate_limit_key_control_chars_raises(self, tmp_outdir: Path) -> None:
        """rate_limit_key with control characters should raise ValueError."""
        with pytest.raises(ValueError, match="printable ASCII"):
            create_app(outdir=tmp_outdir, rate_limit_key="user\n")

    def test_rate_limit_key_unknown_value_raises(self, tmp_outdir: Path) -> None:
        """Unknown rate_limit_key value should raise ValueError."""
        with pytest.raises(ValueError, match="must be one of"):
            create_app(outdir=tmp_outdir, rate_limit_key="unknown")


class TestRealRemoteAddressHelper:
    """Direct unit tests for ``_make_real_remote_address_func`` (issue #1683).

    These bypass the TestClient stack to keep the helper's contract
    visible at a glance: default-deny, opt-in trust, first-untrusted-hop.
    """

    def _request(self, *, peer: str = "testclient", xff: str | None = None) -> Request:
        """Build a Starlette ``Request`` with a controllable peer + XFF."""
        from starlette.requests import Request as StarletteRequest  # noqa: PLC0415

        headers: list[tuple[bytes, bytes]] = []
        if xff is not None:
            headers.append((b"x-forwarded-for", xff.encode()))
        scope: dict[str, object] = {
            "type": "http",
            "method": "GET",
            "path": "/health",
            "raw_path": b"/health",
            "query_string": b"",
            "headers": headers,
            "client": (peer, 12345),
            "server": ("testserver", 80),
            "scheme": "http",
        }
        return StarletteRequest(scope)

    def test_default_deny_ignores_xff(self) -> None:
        from osimflow.api.app import _make_real_remote_address_func  # noqa: PLC0415

        key_func = _make_real_remote_address_func(trust_x_forwarded_for=False, trusted_proxies=[])
        req = self._request(peer="203.0.113.5", xff="198.51.100.7")
        assert key_func(req) == "203.0.113.5"

    def test_opt_in_untrusted_peer_falls_back_to_peer(self) -> None:
        from osimflow.api.app import _make_real_remote_address_func  # noqa: PLC0415

        key_func = _make_real_remote_address_func(
            trust_x_forwarded_for=True,
            trusted_proxies=[_parse_trusted_proxies(["10.0.0.0/8"])[0]],
        )
        req = self._request(peer="203.0.113.5", xff="198.51.100.7")
        # Peer is NOT in the trusted set → XFF ignored, return peer.
        assert key_func(req) == "203.0.113.5"

    def test_opt_in_trusted_peer_first_untrusted_hop(self) -> None:
        from osimflow.api.app import _make_real_remote_address_func  # noqa: PLC0415

        key_func = _make_real_remote_address_func(
            trust_x_forwarded_for=True,
            trusted_proxies=_parse_trusted_proxies(["testclient", "198.51.100.0/24"]),
        )
        # Right-to-left: testclient (trusted), 198.51.100.10 (trusted),
        # 203.0.113.50 (untrusted) → resolved client IP is 203.0.113.50.
        req = self._request(peer="testclient", xff="203.0.113.50, 198.51.100.10")
        assert key_func(req) == "203.0.113.50"

    def test_opt_in_all_trusted_chain_falls_back_to_peer(self) -> None:
        """When every XFF entry is itself a trusted proxy (or the chain
        is empty), fall back to the socket peer rather than returning
        an attacker-controlled header value (issue #1683)."""
        from osimflow.api.app import _make_real_remote_address_func  # noqa: PLC0415

        key_func = _make_real_remote_address_func(
            trust_x_forwarded_for=True,
            trusted_proxies=_parse_trusted_proxies(["testclient", "198.51.100.0/24"]),
        )
        req = self._request(peer="testclient", xff="198.51.100.10, 198.51.100.20")
        assert key_func(req) == "testclient"

    def test_opt_in_strips_ipv4_port(self) -> None:
        from osimflow.api.app import _make_real_remote_address_func  # noqa: PLC0415

        key_func = _make_real_remote_address_func(
            trust_x_forwarded_for=True,
            trusted_proxies=_parse_trusted_proxies(["testclient"]),
        )
        req = self._request(peer="testclient", xff="203.0.113.50:8080")
        assert key_func(req) == "203.0.113.50"

    def test_opt_in_strips_ipv6_bracketed_port(self) -> None:
        from osimflow.api.app import _make_real_remote_address_func  # noqa: PLC0415

        key_func = _make_real_remote_address_func(
            trust_x_forwarded_for=True,
            trusted_proxies=_parse_trusted_proxies(["testclient"]),
        )
        req = self._request(peer="testclient", xff="[2001:db8::1]:8080")
        assert key_func(req) == "2001:db8::1"

    def test_opt_in_garbage_xff_falls_back_to_peer(self) -> None:
        from osimflow.api.app import _make_real_remote_address_func  # noqa: PLC0415

        key_func = _make_real_remote_address_func(
            trust_x_forwarded_for=True,
            trusted_proxies=_parse_trusted_proxies(["testclient"]),
        )
        # Garbage entry that fails ipaddress parsing → fail-closed,
        # fall back to the peer rather than returning the attacker text.
        req = self._request(peer="testclient", xff="not-a-valid-ip-at-all")
        assert key_func(req) == "testclient"

    def test_opt_in_xff_garbage_skipped_walks_to_real_client(self) -> None:
        """When a garbage XFF entry is followed by a real client IP,
        the trust gate skips the garbage and returns the real client.

        This mirrors what nginx / haproxy / envoy do: a malformed XFF
        entry that the proxy itself did not produce is silently
        dropped (issue #1683).
        """
        from osimflow.api.app import _make_real_remote_address_func  # noqa: PLC0415

        key_func = _make_real_remote_address_func(
            trust_x_forwarded_for=True,
            trusted_proxies=_parse_trusted_proxies(["testclient"]),
        )
        # Garbage first (right-most), real client second (left-most).
        # Walk right-to-left: "garbage123" skipped (not an IP),
        # "203.0.113.5" is untrusted → returned.
        req = self._request(peer="testclient", xff="203.0.113.5, garbage123")
        assert key_func(req) == "203.0.113.5"

    def test_parse_trusted_proxies_accepts_cidr_and_bare_ip(self) -> None:
        nets = _parse_trusted_proxies(["10.0.0.1", "192.168.0.0/16", "2001:db8::/32"])
        assert len(nets) == 3
        # CIDR membership:
        from ipaddress import ip_address  # noqa: PLC0415

        assert ip_address("10.0.0.1") in nets[0]
        assert ip_address("192.168.5.7") in nets[1]
        assert ip_address("2001:db8::1") in nets[2]

    def test_parse_trusted_proxies_rejects_garbage(self) -> None:
        with pytest.raises(ValueError, match="Invalid trusted-proxy"):
            _parse_trusted_proxies(["bad@entry"])
        with pytest.raises(ValueError, match="Invalid trusted-proxy"):
            _parse_trusted_proxies(["10.0.0.0/99"])

    def test_parse_trusted_proxies_handles_empty_input(self) -> None:
        assert _parse_trusted_proxies(None) == []
        assert _parse_trusted_proxies([]) == []
        # Whitespace-only entries are skipped, not validated.
        assert _parse_trusted_proxies(["   "]) == []


def _parse_trusted_proxies(values):
    """Local re-export to keep the helper's API obvious in tests."""
    from osimflow.api.app import _parse_trusted_proxies as _impl  # noqa: PLC0415

    return _impl(values)


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


class TestSampleResultFiles:
    """Tests for per-sample result file download and delete (issue #559)."""

    @pytest.fixture
    def campaigns_base(self, tmp_path: Path) -> Path:
        """Create a campaigns base dir with one campaign and sample result files."""
        base = tmp_path / "campaigns"
        base.mkdir()
        campaign_dir = base / "test-campaign-001"
        campaign_dir.mkdir(parents=True)

        run_json = {
            "schema_version": 1,
            "campaign_id": "test-campaign-001",
            "started_at": 1000.0,
            "finished_at": 2000.0,
            "config_summary": {"executor": "local", "n_samples": 2},
            "steps": [
                {"step": "RUN_OPENSTUDIO_SIM", "cache": "MISS", "elapsed_s": 10.0, "exit_code": 0}
            ],
            "per_sample": [
                {"sample_id": "s0001", "status": "ok", "elapsed_s": 10.0},
                {"sample_id": "s0002", "status": "failed", "elapsed_s": 5.0},
            ],
        }
        (campaign_dir / "run.json").write_text(json.dumps(run_json))

        # Create sample result files.
        sample_dir = campaign_dir / "work" / "sim" / "s0001"
        sample_dir.mkdir(parents=True)
        (sample_dir / "eplusout.sql").write_bytes(b"SQLITE DATABASE CONTENT")
        (sample_dir / "stdout.log").write_text("simulation output log")
        (sample_dir / "eplusout.err").write_text("warning: something happened")
        (sample_dir / "workflow.osw").write_text("workflow content")

        return base

    @pytest.fixture
    def client_rw(self, campaigns_base: Path) -> TestClient:
        """Read-write TestClient."""
        return TestClient(create_app(campaigns_base_dir=campaigns_base, read_only=False))

    @pytest.fixture
    def client_ro(self, campaigns_base: Path) -> TestClient:
        """Read-only TestClient."""
        return TestClient(create_app(campaigns_base_dir=campaigns_base))

    def test_download_result_file_success_sql(self, client_rw: TestClient) -> None:
        """Download an existing .sql result file."""
        resp = client_rw.get(
            "/api/v1/campaigns/test-campaign-001/samples/s0001/results/eplusout.sql",
        )
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/x-sqlite3"
        assert resp.content == b"SQLITE DATABASE CONTENT"

    def test_download_result_file_success_log(self, client_rw: TestClient) -> None:
        """Download an existing .log result file."""
        resp = client_rw.get(
            "/api/v1/campaigns/test-campaign-001/samples/s0001/results/stdout.log",
        )
        assert resp.status_code == 200
        assert "text/plain" in resp.headers["content-type"]
        assert resp.text == "simulation output log"

    def test_download_result_file_success_err(self, client_rw: TestClient) -> None:
        """Download an existing .err result file."""
        resp = client_rw.get(
            "/api/v1/campaigns/test-campaign-001/samples/s0001/results/eplusout.err",
        )
        assert resp.status_code == 200
        assert "text/plain" in resp.headers["content-type"]

    def test_download_result_file_success_osw(self, client_rw: TestClient) -> None:
        """Download an existing .osw result file."""
        resp = client_rw.get(
            "/api/v1/campaigns/test-campaign-001/samples/s0001/results/workflow.osw",
        )
        assert resp.status_code == 200
        assert "text/plain" in resp.headers["content-type"]

    def test_download_result_file_success_other_ext(self, client_rw: TestClient) -> None:
        """Download a file with an unknown extension returns octet-stream."""
        (
            client_rw.app.state.campaigns_base_dir
            / "test-campaign-001"
            / "work"
            / "sim"
            / "s0001"
            / "data.csv"
        ).write_text("a,b")
        resp = client_rw.get(
            "/api/v1/campaigns/test-campaign-001/samples/s0001/results/data.csv",
        )
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/octet-stream"

    def test_download_result_file_not_found(self, client_rw: TestClient) -> None:
        """404 for a missing result file."""
        resp = client_rw.get(
            "/api/v1/campaigns/test-campaign-001/samples/s0001/results/missing.sql",
        )
        assert resp.status_code == 404
        assert "not found" in resp.json()["detail"]

    def test_download_result_file_path_traversal(self, client_rw: TestClient) -> None:
        """400 for a filename containing '..' (path traversal attempt).

        Note: Starlette resolves '..' in URL paths before routing, so we
        URL-encode the path separators to prevent that resolution.
        """
        # URL-encode the path separators in the traversal attempt:
        # '../../../etc/passwd' -> '%2F%2F%2E.%2F%2E.%2Fetc%2Fpasswd'
        resp = client_rw.get(
            "/api/v1/campaigns/test-campaign-001/samples/s0001/results/%2F%2F%2E.%2F%2E.%2Fetc%2Fpasswd",
        )
        assert resp.status_code == 400
        assert "path" in resp.json()["detail"].lower()

    def test_download_result_file_campaign_not_found(self, client_rw: TestClient) -> None:
        """404 for a non-existent campaign."""
        resp = client_rw.get(
            "/api/v1/campaigns/nonexistent-campaign/samples/s0001/results/eplusout.sql",
        )
        assert resp.status_code == 404

    def test_download_result_file_sample_not_found(self, client_rw: TestClient) -> None:
        """404 for a non-existent sample."""
        resp = client_rw.get(
            "/api/v1/campaigns/test-campaign-001/samples/s9999/results/eplusout.sql",
        )
        assert resp.status_code == 404

    def test_delete_result_file_success(self, client_rw: TestClient) -> None:
        """DELETE removes an existing result file and returns 204."""
        sample_dir = (
            client_rw.app.state.campaigns_base_dir / "test-campaign-001" / "work" / "sim" / "s0001"
        )
        assert (sample_dir / "eplusout.err").exists()

        resp = client_rw.delete(
            "/api/v1/campaigns/test-campaign-001/samples/s0001/results/eplusout.err",
        )
        assert resp.status_code == 204
        assert not (sample_dir / "eplusout.err").exists()

    def test_delete_result_file_not_found(self, client_rw: TestClient) -> None:
        """DELETE returns 404 for a missing file."""
        resp = client_rw.delete(
            "/api/v1/campaigns/test-campaign-001/samples/s0001/results/missing.log",
        )
        assert resp.status_code == 404

    def test_delete_result_file_path_traversal(self, client_rw: TestClient) -> None:
        """DELETE returns 400 for a filename containing '..'.

        Note: Starlette resolves '..' in URL paths before routing, so we
        URL-encode the path separators to prevent that resolution.
        """
        resp = client_rw.delete(
            "/api/v1/campaigns/test-campaign-001/samples/s0001/results/%2F%2F%2E.%2F%2E.%2Fetc%2Fpasswd",
        )
        assert resp.status_code == 400

    def test_delete_result_file_read_only_forbidden(self, client_ro: TestClient) -> None:
        """DELETE returns 403 in read-only mode."""
        resp = client_ro.delete(
            "/api/v1/campaigns/test-campaign-001/samples/s0001/results/eplusout.err",
        )
        assert resp.status_code == 403
