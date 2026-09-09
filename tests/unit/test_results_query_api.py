"""Tests for osimflow/api/results_query.py (issue #585)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="osimflow[api] extra required")
pytest.importorskip("slowapi", reason="osimflow[api] extra required")
from fastapi.testclient import TestClient


@pytest.fixture
def tmp_campaign_dir(tmp_path: Path) -> Path:
    """Create a temporary campaign directory with run.json and aggregated_results.csv."""
    campaign_dir = tmp_path / "test-campaign-001"
    campaign_dir.mkdir()

    run_json = {
        "schema_version": 1,
        "campaign_id": "test-campaign-001",
        "started_at": 1000.0,
        "finished_at": 2000.0,
        "config_summary": {"executor": "local", "n_samples": 5},
        "steps": [],
        "per_sample": [],
    }
    (campaign_dir / "run.json").write_text(json.dumps(run_json))

    aggregated_csv = "sample_id,status,kpi.eui,kpi.cost\n"
    aggregated_csv += "s0001,ok,150.5,1200.0\n"
    aggregated_csv += "s0002,ok,145.2,1100.0\n"
    aggregated_csv += "s0003,failed,200.0,0.0\n"
    aggregated_csv += "s0004,ok,155.8,1300.0\n"
    aggregated_csv += "s0005,cached,148.0,1150.0\n"
    (campaign_dir / "aggregated_results.csv").write_text(aggregated_csv)

    return campaign_dir


@pytest.fixture
def campaigns_base_dir(tmp_campaign_dir: Path) -> Path:
    """Create a campaigns base directory containing the test campaign."""
    return tmp_campaign_dir.parent


@pytest.fixture
def client(campaigns_base_dir: Path) -> TestClient:
    """Create a test client with the results_query router."""
    from osimflow.api.app import create_app
    from osimflow.api.results_query import results_query_router

    app = create_app(campaigns_base_dir=campaigns_base_dir)
    app.include_router(results_query_router)
    return TestClient(app)


@pytest.fixture
def multi_campaign_client(tmp_path: Path) -> TestClient:
    """Create a test client with multiple campaigns for cross-campaign queries."""
    from osimflow.api.app import create_app
    from osimflow.api.results_query import results_query_router

    base = tmp_path / "campaigns"
    base.mkdir()

    for i in range(1, 3):
        campaign_dir = base / f"campaign-{i:03d}"
        campaign_dir.mkdir()
        run_json = {
            "schema_version": 1,
            "campaign_id": f"campaign-{i:03d}",
            "started_at": 1000.0 + i * 100,
            "finished_at": 2000.0 + i * 100,
            "config_summary": {"executor": "local", "n_samples": 3},
            "steps": [],
            "per_sample": [],
        }
        (campaign_dir / "run.json").write_text(json.dumps(run_json))
        csv_content = (
            f"sample_id,status,kpi.eui,kpi.cost\ns{i:04d},ok,{150 + i:.1f},{1000 + i * 100:.0f}\n"
        )
        (campaign_dir / "aggregated_results.csv").write_text(csv_content)

    app = create_app(campaigns_base_dir=base)
    app.include_router(results_query_router)
    return TestClient(app)


class TestQueryCampaignResults:
    """Tests for GET /api/v1/campaigns/{campaign_id}/results/query."""

    def test_query_no_results_csv(self, client: TestClient) -> None:
        """Returns empty rows when aggregated_results.csv does not exist."""
        resp = client.get("/api/v1/campaigns/nonexistent/results/query")
        assert resp.status_code == 404

    def test_query_pagination(self, client: TestClient) -> None:
        """Paginated results are returned correctly."""
        resp = client.get("/api/v1/campaigns/test-campaign-001/results/query?page=1&per_page=2")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 5
        assert len(data["rows"]) == 2
        assert data["page"] == 1
        assert data["per_page"] == 2
        assert data["campaign_id"] == "test-campaign-001"

    def test_query_second_page(self, client: TestClient) -> None:
        """Second page returns remaining rows."""
        resp = client.get("/api/v1/campaigns/test-campaign-001/results/query?page=2&per_page=2")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["rows"]) == 2
        assert data["page"] == 2

    def test_query_filter_by_status(self, client: TestClient) -> None:
        """Filter by status query param works."""
        resp = client.get("/api/v1/campaigns/test-campaign-001/results/query?status=ok")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 3
        for row in data["rows"]:
            assert row["status"] == "ok"

    def test_query_filter_failed(self, client: TestClient) -> None:
        """Filter by failed status returns only failed rows."""
        resp = client.get("/api/v1/campaigns/test-campaign-001/results/query?status=failed")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["rows"][0]["status"] == "failed"

    def test_query_filter_by_json(self, client: TestClient) -> None:
        """MongoDB-style JSON filter works."""
        filter_json = json.dumps({"status": "ok"})
        resp = client.get(f"/api/v1/campaigns/test-campaign-001/results/query?filter={filter_json}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 3
        for row in data["rows"]:
            assert row["status"] == "ok"

    def test_query_filter_numeric_gt(self, client: TestClient) -> None:
        """Numeric $gt filter works."""
        filter_json = json.dumps({"kpi.eui": {"$gt": 150}})
        resp = client.get(f"/api/v1/campaigns/test-campaign-001/results/query?filter={filter_json}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 3  # 150.5, 155.8, 200.0
        for row in data["rows"]:
            assert float(row["kpi.eui"]) > 150

    def test_query_invalid_filter_json(self, client: TestClient) -> None:
        """Invalid filter JSON returns 400."""
        resp = client.get("/api/v1/campaigns/test-campaign-001/results/query?filter=not-valid-json")
        assert resp.status_code == 400


class TestExportCampaignResults:
    """Tests for GET /api/v1/campaigns/{campaign_id}/results/export."""

    def test_export_csv_default(self, client: TestClient) -> None:
        """CSV export returns correct content type and data."""
        resp = client.get("/api/v1/campaigns/test-campaign-001/results/export")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/csv")
        assert "attachment" in resp.headers.get("content-disposition", "")
        lines = resp.text.strip().split("\n")
        assert len(lines) == 6  # header + 5 rows

    def test_export_json_format(self, client: TestClient) -> None:
        """JSON export returns correct content."""
        resp = client.get("/api/v1/campaigns/test-campaign-001/results/export?format=json")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("application/json")
        data = resp.json()
        assert "rows" in data
        assert len(data["rows"]) == 5
        assert data["campaign_id"] == "test-campaign-001"

    def test_export_filter_by_status(self, client: TestClient) -> None:
        """Export respects status filter."""
        resp = client.get("/api/v1/campaigns/test-campaign-001/results/export?status=ok")
        assert resp.status_code == 200
        if resp.headers["content-type"].startswith("text/csv"):
            lines = resp.text.strip().split("\n")
            assert len(lines) == 4  # header + 3 ok rows
        else:
            data = resp.json()
            for row in data["rows"]:
                assert row["status"] == "ok"

    def test_export_exclude_failed(self, client: TestClient) -> None:
        """include_failed=false excludes failed rows."""
        resp = client.get("/api/v1/campaigns/test-campaign-001/results/export?include_failed=false")
        assert resp.status_code == 200
        if resp.headers["content-type"].startswith("text/csv"):
            lines = resp.text.strip().split("\n")
            for line in lines[1:]:
                assert "failed" not in line
        else:
            data = resp.json()
            for row in data["rows"]:
                assert row["status"] != "failed"

    def test_export_no_results(self, client: TestClient) -> None:
        """Returns 404 when no results file exists."""
        resp = client.get("/api/v1/campaigns/nonexistent/results/export")
        assert resp.status_code == 404


class TestQueryResultsCLI:
    """Tests for the CLI helper functions in osimflow.results_query (issue #1699).

    The CLI helpers used to live in :mod:`osimflow.api.results_query`, but
    that module pulls FastAPI at import time. The canonical home is now
    :mod:`osimflow.results_query`, so the CLI subcommands work on installs
    without the optional ``[api]`` extra.
    """

    def test_query_results_cli_no_data(self) -> None:
        """Returns empty when no outdirs provided."""
        from osimflow.results_query import query_results_cli

        result = query_results_cli(campaign_ids=[], outdirs=[])
        assert result["rows"] == []
        assert result["total"] == 0

    def test_query_results_cli_single_outdir(self, tmp_path: Path) -> None:
        """Queries a single outdir correctly."""
        from osimflow.results_query import query_results_cli

        campaign_dir = tmp_path / "campaign-001"
        campaign_dir.mkdir()
        csv_content = "sample_id,status,kpi.eui\ns001,ok,150.5\ns002,failed,200.0\n"
        (campaign_dir / "aggregated_results.csv").write_text(csv_content)

        result = query_results_cli(outdirs=[str(campaign_dir)])
        assert result["total"] == 2
        assert result["campaigns_queried"] == 1

    def test_export_results_cli(self, tmp_path: Path) -> None:
        """Export produces CSV content."""
        from osimflow.results_query import export_results_cli

        campaign_dir = tmp_path / "campaign-001"
        campaign_dir.mkdir()
        csv_content = "sample_id,status,kpi.eui\ns001,ok,150.5\n"
        (campaign_dir / "aggregated_results.csv").write_text(csv_content)

        output_file = tmp_path / "export.csv"
        exit_code = export_results_cli(
            outdirs=[str(campaign_dir)],
            output_path=str(output_file),
        )
        assert exit_code == 0
        assert output_file.read_text().startswith("sample_id")

    def test_export_results_cli_json_format(self, tmp_path: Path) -> None:
        """Export produces JSON content when format=json."""
        from osimflow.results_query import export_results_cli

        campaign_dir = tmp_path / "campaign-001"
        campaign_dir.mkdir()
        csv_content = "sample_id,status,kpi.eui\ns001,ok,150.5\n"
        (campaign_dir / "aggregated_results.csv").write_text(csv_content)

        output_file = tmp_path / "export.json"
        exit_code = export_results_cli(
            outdirs=[str(campaign_dir)],
            format="json",
            output_path=str(output_file),
        )
        assert exit_code == 0
        content = json.loads(output_file.read_text())
        assert "rows" in content
        assert len(content["rows"]) == 1


class TestQueryResultsCLIWithoutFastAPI:
    """Verify issue #1699: the CLI helpers work without the ``[api]`` extra.

    Before the fix, ``query_results_cli`` and ``export_results_cli``
    lived in ``osimflow.api.results_query``, which imported FastAPI at
    module scope. Any install without the optional ``[api]`` extra
    would crash with ``ModuleNotFoundError`` the moment a user ran
    ``osimflow query-results`` or ``osimflow export-results``. The
    helpers now live in :mod:`osimflow.results_query`, so they must
    be importable in a clean import environment with no FastAPI on
    the path.
    """

    def test_canonical_module_imports_without_fastapi(self) -> None:
        """``osimflow.results_query`` must import successfully with FastAPI blocked.

        We use a meta-path finder that pretends ``fastapi`` (and any
        submodule thereof) does not exist; this simulates an install
        without the ``[api]`` extra. The canonical module's import
        chain must not reach for FastAPI at any point.
        """
        import importlib
        import sys

        class FastapiBlocker:
            """Block all fastapi imports to simulate an install without [api]."""

            def find_spec(self, fullname, path, target=None):
                if fullname == "fastapi" or fullname.startswith("fastapi."):
                    raise ModuleNotFoundError(f"No module named {fullname!r} (blocked by test)")
                return None

        blocker = FastapiBlocker()
        sys.meta_path.insert(0, blocker)
        try:
            # Drop the canonical module from sys.modules in case an
            # earlier test already imported it via a different path.
            for name in list(sys.modules):
                if name == "osimflow.results_query" or name.startswith("osimflow.results_query."):
                    del sys.modules[name]
            module = importlib.import_module("osimflow.results_query")
            assert module.query_results_cli is not None
            assert module.export_results_cli is not None
        finally:
            sys.meta_path.remove(blocker)
            # Re-import so subsequent tests see the populated module.
            for name in list(sys.modules):
                if name == "osimflow.results_query" or name.startswith("osimflow.results_query."):
                    del sys.modules[name]
            importlib.import_module("osimflow.results_query")

    def test_main_uses_canonical_home(self) -> None:
        """``osimflow.__main__`` must import the CLI helpers from ``osimflow.results_query``.

        This is the runtime equivalent of the CLI subcommand path: the
        lazy imports inside ``_cmd_query_results`` / ``_cmd_export_results``
        are what crash today when FastAPI is missing. They should now
        point at the no-FastAPI module.
        """
        import inspect

        from osimflow.__main__ import _cmd_export_results, _cmd_query_results

        src_query = inspect.getsource(_cmd_query_results)
        src_export = inspect.getsource(_cmd_export_results)
        assert "from osimflow.results_query import" in src_query
        assert "from osimflow.api.results_query import" not in src_query
        assert "from osimflow.results_query import" in src_export
        assert "from osimflow.api.results_query import" not in src_export
