"""Tests for the web GUI and plots endpoints (issue #264)."""

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


@pytest.fixture
def tmp_outdir(tmp_path: Path) -> Path:
    """Create a temporary output directory with a sample run.json."""
    run_json = {
        "schema_version": 1,
        "campaign_id": "gui-test-001",
        "started_at": 1000.0,
        "finished_at": 2000.0,
        "config_summary": {"executor": "local", "n_samples": 5},
        "steps": [
            {"step": "GENERATE_LHS_SAMPLES", "cache": "MISS", "elapsed_s": 0.5, "exit_code": 0},
        ],
        "per_sample": [
            {"sample_id": "s001", "status": "ok", "elapsed_s": 10.0},
            {"sample_id": "s002", "status": "failed", "elapsed_s": 5.0, "error_summary": "Severe Error"},
        ],
    }
    (tmp_path / "run.json").write_text(json.dumps(run_json))
    return tmp_path


@pytest.fixture
def client(tmp_outdir: Path) -> TestClient:
    app = create_app(outdir=tmp_outdir)
    return TestClient(app)


# ---------------------------------------------------------------------------
# Root redirect
# ---------------------------------------------------------------------------


class TestRootRedirect:
    """Tests for GET / → redirect to /static/index.html."""

    def test_root_redirects_to_static(self, client: TestClient) -> None:
        resp = client.get("/", follow_redirects=False)
        assert resp.status_code == 307
        assert resp.headers["location"] == "/static/index.html"

    def test_root_redirect_follows(self, client: TestClient) -> None:
        resp = client.get("/", follow_redirects=True)
        # Should end up at the static HTML page (200).
        assert resp.status_code == 200
        assert b"OSimFlow" in resp.content

    def test_root_redirect_with_api_key(self, tmp_outdir: Path) -> None:
        """Root redirect is a public path, accessible without API key."""
        app = create_app(outdir=tmp_outdir, api_key="secret123")
        c = TestClient(app)
        resp = c.get("/", follow_redirects=False)
        assert resp.status_code == 307

    def test_static_index_accessible(self, client: TestClient) -> None:
        resp = client.get("/static/index.html")
        assert resp.status_code == 200
        assert b"OSimFlow Dashboard" in resp.content

    def test_static_index_with_api_key(self, tmp_outdir: Path) -> None:
        """Static index.html is a public path."""
        app = create_app(outdir=tmp_outdir, api_key="secret123")
        c = TestClient(app)
        resp = c.get("/static/index.html")
        assert resp.status_code == 200

    def test_static_index_is_html(self, client: TestClient) -> None:
        resp = client.get("/static/index.html")
        assert "text/html" in resp.headers.get("content-type", "")


# ---------------------------------------------------------------------------
# Plots endpoints
# ---------------------------------------------------------------------------


class TestPlotsList:
    """Tests for GET /api/v1/plots."""

    def test_plots_no_outdir(self) -> None:
        app = create_app(outdir=None)
        c = TestClient(app)
        resp = c.get("/api/v1/plots")
        assert resp.status_code == 503

    def test_plots_empty_dir(self, tmp_outdir: Path) -> None:
        app = create_app(outdir=tmp_outdir)
        c = TestClient(app)
        resp = c.get("/api/v1/plots")
        assert resp.status_code == 200
        data = resp.json()
        assert data["plots"] == []
        assert data["total"] == 0

    def test_plots_finds_pngs_in_outdir(self, tmp_outdir: Path) -> None:
        # Create fake plot PNG files
        (tmp_outdir / "eui_scatter.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)
        (tmp_outdir / "param_tornado.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 200)

        app = create_app(outdir=tmp_outdir)
        c = TestClient(app)
        resp = c.get("/api/v1/plots")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 2
        names = {p["name"] for p in data["plots"]}
        assert names == {"eui_scatter.png", "param_tornado.png"}

    def test_plots_finds_pngs_in_plots_subdir(self, tmp_outdir: Path) -> None:
        plots_dir = tmp_outdir / "plots"
        plots_dir.mkdir()
        (plots_dir / "scatter.png").write_bytes(b"\x89PNG" + b"\x00" * 50)

        app = create_app(outdir=tmp_outdir)
        c = TestClient(app)
        resp = c.get("/api/v1/plots")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["plots"][0]["name"] == "scatter.png"

    def test_plots_deduplicates(self, tmp_outdir: Path) -> None:
        """When same filename exists in outdir and plots/, prefer plots/."""
        (tmp_outdir / "scatter.png").write_bytes(b"\x89PNG" + b"\x00" * 50)
        plots_dir = tmp_outdir / "plots"
        plots_dir.mkdir()
        (plots_dir / "scatter.png").write_bytes(b"\x89PNG" + b"\x00" * 100)

        app = create_app(outdir=tmp_outdir)
        c = TestClient(app)
        resp = c.get("/api/v1/plots")
        data = resp.json()
        assert data["total"] == 1
        # The plots/ version should be preferred (larger size)
        assert data["plots"][0]["size"] == 104

    def test_plots_ignores_non_png(self, tmp_outdir: Path) -> None:
        (tmp_outdir / "data.csv").write_text("a,b\n1,2")
        (tmp_outdir / "plot.png").write_bytes(b"\x89PNG" + b"\x00" * 50)

        app = create_app(outdir=tmp_outdir)
        c = TestClient(app)
        resp = c.get("/api/v1/plots")
        data = resp.json()
        assert data["total"] == 1


class TestPlotsFile:
    """Tests for GET /api/v1/plots/{filename}."""

    def test_serves_png_from_outdir(self, tmp_outdir: Path) -> None:
        png_data = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
        (tmp_outdir / "eui.png").write_bytes(png_data)

        app = create_app(outdir=tmp_outdir)
        c = TestClient(app)
        resp = c.get("/api/v1/plots/eui.png")
        assert resp.status_code == 200
        assert resp.content == png_data
        assert resp.headers["content-type"] == "image/png"

    def test_serves_png_from_plots_subdir(self, tmp_outdir: Path) -> None:
        plots_dir = tmp_outdir / "plots"
        plots_dir.mkdir()
        png_data = b"\x89PNG" + b"\x00" * 50
        (plots_dir / "scatter.png").write_bytes(png_data)

        app = create_app(outdir=tmp_outdir)
        c = TestClient(app)
        resp = c.get("/api/v1/plots/scatter.png")
        assert resp.status_code == 200
        assert resp.content == png_data

    def test_prefers_plots_subdir(self, tmp_outdir: Path) -> None:
        (tmp_outdir / "scatter.png").write_bytes(b"root_version")
        plots_dir = tmp_outdir / "plots"
        plots_dir.mkdir()
        (plots_dir / "scatter.png").write_bytes(b"plots_version")

        app = create_app(outdir=tmp_outdir)
        c = TestClient(app)
        resp = c.get("/api/v1/plots/scatter.png")
        assert resp.status_code == 200
        assert resp.content == b"plots_version"

    def test_not_found(self, tmp_outdir: Path) -> None:
        app = create_app(outdir=tmp_outdir)
        c = TestClient(app)
        resp = c.get("/api/v1/plots/nonexistent.png")
        assert resp.status_code == 404

    def test_rejects_non_png(self, tmp_outdir: Path) -> None:
        app = create_app(outdir=tmp_outdir)
        c = TestClient(app)
        resp = c.get("/api/v1/plots/evil.txt")
        assert resp.status_code == 400

    def test_rejects_path_traversal(self, tmp_outdir: Path) -> None:
        app = create_app(outdir=tmp_outdir)
        c = TestClient(app)
        resp = c.get("/api/v1/plots/../run.json")
        assert resp.status_code in (400, 404)

    def test_no_outdir(self) -> None:
        app = create_app(outdir=None)
        c = TestClient(app)
        resp = c.get("/api/v1/plots/test.png")
        assert resp.status_code == 503


# ---------------------------------------------------------------------------
# Static file serving
# ---------------------------------------------------------------------------


class TestStaticFiles:
    """Tests for the static file mount."""

    def test_static_dir_exists(self) -> None:
        static_dir = Path(__file__).parent.parent.parent / "osimflow" / "api" / "static"
        assert static_dir.is_dir()

    def test_index_html_exists(self) -> None:
        index = Path(__file__).parent.parent.parent / "osimflow" / "api" / "static" / "index.html"
        assert index.is_file()

    def test_index_html_contains_spa(self) -> None:
        index = Path(__file__).parent.parent.parent / "osimflow" / "api" / "static" / "index.html"
        content = index.read_text()
        assert "OSimFlow Dashboard" in content
        assert "EventSource" in content
        assert "/api/v1/campaign" in content
        assert "/api/v1/samples" in content
        assert "/api/v1/plots" in content
        assert "/api/v1/events" in content
