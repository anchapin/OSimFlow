"""Tests for osimflow/api/dashboard.py (issue #1695).

The ``dashboard`` UI route module was previously referenced by no test
file — at best the suite executed module import and route registration,
never the hand-rolled handlers.  These tests exercise each route via
``create_app(dashboard=True)``'s TestClient: registration, status codes,
and meaningful payload assertions.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="osimflow[api] extra required")
pytest.importorskip("slowapi", reason="osimflow[api] extra required")
from fastapi.testclient import TestClient

from osimflow.api import create_app

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_run_json(
    *,
    campaign_id: str = "dash-campaign-001",
    status: str = "success",
    samples: list[dict] | None = None,
    steps: list[dict] | None = None,
) -> dict:
    default_steps = [
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
    ]
    default_samples = [
        {"sample_id": "s0", "status": "ok", "elapsed_s": 10.0},
        {"sample_id": "s1", "status": "ok", "elapsed_s": 12.0},
        {"sample_id": "s2", "status": "failed", "elapsed_s": 11.0},
    ]
    return {
        "schema_version": 1,
        "campaign_id": campaign_id,
        "status": status,
        "started_at": 1000.0,
        "finished_at": 2000.0,
        "elapsed_s": 1000.0,
        "config_summary": {"executor": "local", "n_samples": 3},
        "steps": default_steps if steps is None else steps,
        "per_sample": default_samples if samples is None else samples,
    }


def _collect_route_paths(app) -> set[str]:
    """Recursively collect every APIRoute path registered on a FastAPI app.

    ``app.router.routes`` contains ``APIRoute`` objects directly plus
    ``_IncludedRouter`` wrappers (one per ``include_router`` call).  The
    wrapper exposes the wrapped router as ``original_router``; that
    router's ``routes`` attribute is a list of ``APIRoute`` (and any
    other ``Route`` subclass) we want to enumerate.
    """
    from fastapi.routing import APIRoute

    paths: set[str] = set()
    routes = getattr(app, "router", app).routes
    for r in routes:
        if isinstance(r, APIRoute):
            paths.add(r.path)
        elif hasattr(r, "original_router"):
            paths.update(r.original_router.routes and _routes_paths(r.original_router))
    return paths


def _routes_paths(router) -> set[str]:
    """Collect ``APIRoute.path`` from an APIRouter's ``routes`` attribute."""
    from fastapi.routing import APIRoute

    return {r.path for r in router.routes if isinstance(r, APIRoute)}


@pytest.fixture
def tmp_outdir(tmp_path: Path) -> Path:
    (tmp_path / "run.json").write_text(json.dumps(_make_run_json()))
    return tmp_path


@pytest.fixture
def dashboard_client(tmp_outdir: Path) -> TestClient:
    return TestClient(create_app(outdir=tmp_outdir, dashboard=True))


@pytest.fixture
def no_outdir_client() -> TestClient:
    return TestClient(create_app(outdir=None, dashboard=True))


@pytest.fixture
def empty_outdir_client(tmp_path: Path) -> TestClient:
    return TestClient(create_app(outdir=tmp_path, dashboard=True))


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------


class TestDashboardRouteRegistration:
    def test_dashboard_router_included_when_dashboard_true(
        self, dashboard_client: TestClient
    ) -> None:
        paths = _collect_route_paths(dashboard_client.app)
        assert "/dashboard" in paths
        assert "/api/v1/dashboard/status" in paths

    def test_dashboard_router_omitted_when_dashboard_false(self, tmp_path: Path) -> None:
        (tmp_path / "run.json").write_text(json.dumps(_make_run_json()))
        client = TestClient(create_app(outdir=tmp_path, dashboard=False))
        paths = _collect_route_paths(client.app)
        assert "/dashboard" not in paths
        assert "/api/v1/dashboard/status" not in paths


# ---------------------------------------------------------------------------
# GET /dashboard
# ---------------------------------------------------------------------------


class TestDashboardHtmlEndpoint:
    def test_returns_200_with_html(self, dashboard_client: TestClient) -> None:
        resp = dashboard_client.get("/dashboard")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/html")

    def test_serves_static_dashboard_html(self, dashboard_client: TestClient) -> None:
        resp = dashboard_client.get("/dashboard")
        assert resp.status_code == 200
        expected = (
            Path(__file__).resolve().parents[2] / "osimflow" / "api" / "static" / "dashboard.html"
        ).read_text()
        assert resp.text == expected

    def test_does_not_require_outdir(self, no_outdir_client: TestClient) -> None:
        resp = no_outdir_client.get("/dashboard")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# GET /api/v1/dashboard/status
# ---------------------------------------------------------------------------


class TestDashboardStatusEndpoint:
    def test_503_when_outdir_not_configured(self, no_outdir_client: TestClient) -> None:
        resp = no_outdir_client.get("/api/v1/dashboard/status")
        assert resp.status_code == 503
        assert "directory" in resp.json()["detail"].lower()

    def test_404_when_run_json_missing(self, empty_outdir_client: TestClient) -> None:
        resp = empty_outdir_client.get("/api/v1/dashboard/status")
        assert resp.status_code == 404
        assert "run.json" in resp.json()["detail"]

    def test_returns_full_shape_with_run_json(self, dashboard_client: TestClient) -> None:
        resp = dashboard_client.get("/api/v1/dashboard/status")
        assert resp.status_code == 200
        data = resp.json()
        for key in (
            "campaign_id",
            "overall_status",
            "campaign_status",
            "steps",
            "samples",
            "started_at",
            "finished_at",
            "elapsed_s",
        ):
            assert key in data, f"missing key: {key}"

    def test_samples_block_counts_match_run_json(self, dashboard_client: TestClient) -> None:
        resp = dashboard_client.get("/api/v1/dashboard/status")
        data = resp.json()
        assert data["samples"]["total"] == 3
        assert data["samples"]["success"] == 2
        assert data["samples"]["failed"] == 1
        assert data["samples"]["cached"] == 0
        assert data["samples"]["running"] == 0

    def test_step_statuses_classified_by_exit_code(self, dashboard_client: TestClient) -> None:
        resp = dashboard_client.get("/api/v1/dashboard/status")
        data = resp.json()
        assert len(data["steps"]) == 2
        for s in data["steps"]:
            assert s["status"] in ("ok", "failed")
            assert "step" in s
            assert "elapsed_s" in s
            assert "cache" in s

    def test_overall_status_degraded_when_some_failed(self, dashboard_client: TestClient) -> None:
        resp = dashboard_client.get("/api/v1/dashboard/status")
        data = resp.json()
        assert data["overall_status"] == "degraded"
        assert data["campaign_status"] == "success"

    def test_overall_status_healthy_when_all_ok(self, tmp_path: Path) -> None:
        run_json = _make_run_json(
            samples=[
                {"sample_id": "s0", "status": "ok", "elapsed_s": 1.0},
                {"sample_id": "s1", "status": "ok", "elapsed_s": 1.0},
            ]
        )
        (tmp_path / "run.json").write_text(json.dumps(run_json))
        client = TestClient(create_app(outdir=tmp_path, dashboard=True))
        resp = client.get("/api/v1/dashboard/status")
        assert resp.status_code == 200
        assert resp.json()["overall_status"] == "healthy"

    def test_overall_status_unhealthy_when_terminal_failed(self, tmp_path: Path) -> None:
        run_json = _make_run_json(
            status="failed",
            samples=[
                {"sample_id": "s0", "status": "failed", "elapsed_s": 1.0},
                {"sample_id": "s1", "status": "failed", "elapsed_s": 1.0},
            ],
        )
        (tmp_path / "run.json").write_text(json.dumps(run_json))
        client = TestClient(create_app(outdir=tmp_path, dashboard=True))
        resp = client.get("/api/v1/dashboard/status")
        assert resp.json()["overall_status"] == "unhealthy"

    def test_overall_status_healthy_when_terminal_success_no_samples(self, tmp_path: Path) -> None:
        run_json = _make_run_json(status="success", samples=[])
        (tmp_path / "run.json").write_text(json.dumps(run_json))
        client = TestClient(create_app(outdir=tmp_path, dashboard=True))
        resp = client.get("/api/v1/dashboard/status")
        assert resp.json()["overall_status"] == "healthy"

    def test_overall_status_unknown_for_unknown_terminal_status(self, tmp_path: Path) -> None:
        run_json = _make_run_json(status="weird")
        (tmp_path / "run.json").write_text(json.dumps(run_json))
        client = TestClient(create_app(outdir=tmp_path, dashboard=True))
        resp = client.get("/api/v1/dashboard/status")
        assert resp.json()["overall_status"] == "unknown"

    def test_overall_status_unhealthy_when_cancelled(self, tmp_path: Path) -> None:
        run_json = _make_run_json(status="cancelled", samples=[])
        (tmp_path / "run.json").write_text(json.dumps(run_json))
        client = TestClient(create_app(outdir=tmp_path, dashboard=True))
        resp = client.get("/api/v1/dashboard/status")
        assert resp.json()["overall_status"] == "unhealthy"


# ---------------------------------------------------------------------------
# Parametrized smoke check across both routes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path,expected_status",
    [
        ("/dashboard", 200),
        ("/api/v1/dashboard/status", 200),
    ],
)
def test_dashboard_routes_respond(
    dashboard_client: TestClient, path: str, expected_status: int
) -> None:
    resp = dashboard_client.get(path)
    assert resp.status_code == expected_status
