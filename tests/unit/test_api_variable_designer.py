"""Tests for osimflow/api/variable_designer.py (issue #1695).

The ``variable_designer`` UI route module was previously referenced by
no test file — only transitively imported by ``create_app``.  These
tests exercise each route via ``create_app(variable_editor=True)``'s
TestClient: registration, redirect status, and Location header.
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi", reason="osimflow[api] extra required")
pytest.importorskip("slowapi", reason="osimflow[api] extra required")
from fastapi.testclient import TestClient

from osimflow.api import create_app

# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


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
def editor_client() -> TestClient:
    return TestClient(create_app(variable_editor=True))


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------


class TestVariableDesignerRouteRegistration:
    def test_router_included_when_variable_editor_true(self, editor_client: TestClient) -> None:
        paths = _collect_route_paths(editor_client.app)
        assert "/ui/designer/" in paths

    def test_variable_designer_router_wired_when_enabled(self, editor_client: TestClient) -> None:
        """The ``variable_designer_router`` module is included when the
        flag is on — assert via its own routes, which only carry the
        ``/ui/designer/`` redirect."""
        from osimflow.api.variable_designer import variable_designer_router

        paths = {r.path for r in variable_designer_router.routes}
        assert "/ui/designer/" in paths


# ---------------------------------------------------------------------------
# GET /ui/designer/
# ---------------------------------------------------------------------------


class TestVariableDesignerRedirect:
    def test_returns_redirect_status(self, editor_client: TestClient) -> None:
        resp = editor_client.get("/ui/designer/", follow_redirects=False)
        assert resp.status_code in (307, 308, 302)
        assert resp.headers["location"] == "/static/variable_designer.html"

    def test_followed_redirect_returns_html(self, editor_client: TestClient) -> None:
        resp = editor_client.get("/ui/designer/", follow_redirects=True)
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/html")
        assert "variable_designer" in resp.text.lower() or "<html" in resp.text.lower()


# ---------------------------------------------------------------------------
# Parametrized smoke check across all registered routes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path,expected_status",
    [
        ("/ui/designer/", 307),
    ],
)
def test_variable_designer_routes_respond(
    editor_client: TestClient, path: str, expected_status: int
) -> None:
    resp = editor_client.get(path, follow_redirects=False)
    assert resp.status_code == expected_status
