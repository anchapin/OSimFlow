"""Tests for the campaign artifact bundle download endpoint (issue #555)."""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="osimflow[api] extra required")
pytest.importorskip("slowapi", reason="osimflow[api] extra required")
from fastapi.testclient import TestClient

from osimflow.api import create_app


@pytest.fixture
def tmp_campaign_dir(tmp_path: Path) -> Path:
    """Create a temporary campaign directory with run.json and artifacts.

    The directory is named ``test-campaign-abc123`` so it matches the
    campaign_id used in the endpoint tests.
    """
    campaign_dir = tmp_path / "test-campaign-abc123"
    campaign_dir.mkdir()
    run_json = {
        "schema_version": 1,
        "campaign_id": "test-campaign-abc123",
        "started_at": 1000.0,
        "finished_at": 2000.0,
        "config_summary": {"executor": "local", "n_samples": 3},
        "steps": [
            {"step": "GENERATE_LHS_SAMPLES", "cache": "MISS", "elapsed_s": 0.5, "exit_code": 0},
            {"step": "RUN_OPENSTUDIO_SIM", "cache": "MISS", "elapsed_s": 100.0, "exit_code": 0},
        ],
        "per_sample": [
            {"sample_id": "sample-000", "status": "ok"},
            {"sample_id": "sample-001", "status": "ok"},
            {"sample_id": "sample-002", "status": "failed"},
        ],
    }
    (campaign_dir / "run.json").write_text(json.dumps(run_json))
    (campaign_dir / "samples.json").write_text(json.dumps({"variables": []}))
    (campaign_dir / "aggregated_results.csv").write_text("sample_id,kpi_eui\nsample-000,120.5\nsample-001,118.2\nsample-002,nan")
    (campaign_dir / "failed_simulations.csv").write_text("sample_id,error\nsample-002, Severe  Convergence failure")
    # Per-sample KPI files
    work_dir = campaign_dir / "work" / "sim"
    (work_dir / "sample-000").mkdir(parents=True)
    (work_dir / "sample-000" / "kpi.json").write_text(json.dumps({"eui": 120.5}))
    (work_dir / "sample-001").mkdir(parents=True)
    (work_dir / "sample-001" / "kpis.json").write_text(json.dumps({"eui": 118.2}))
    (work_dir / "sample-002").mkdir(parents=True)
    (work_dir / "sample-002" / "kpi.json").write_text(json.dumps({"eui": float("nan")}))
    # Plot files
    (campaign_dir / "plots").mkdir()
    (campaign_dir / "plots" / "pareto.png").write_bytes(b"\x89PNG\r\n\x1a\nfake png content")
    (campaign_dir / "summary.png").write_bytes(b"\x89PNG\r\n\x1a\nanother fake png")
    return campaign_dir


@pytest.fixture
def client(tmp_campaign_dir: Path) -> TestClient:
    # campaigns_base_dir must be the parent of tmp_campaign_dir so that
    # _campaign_dir_from_id(base, "test-campaign-abc123") resolves to
    # tmp_campaign_dir itself.
    app = create_app(campaigns_base_dir=tmp_campaign_dir.parent, outdir=tmp_campaign_dir.parent)
    return TestClient(app)


class TestCampaignDownloadEndpoint:
    """Tests for GET /api/v1/campaigns/{campaign_id}/download."""

    def test_download_returns_zip(self, client: TestClient, tmp_campaign_dir: Path) -> None:
        resp = client.get("/api/v1/campaigns/test-campaign-abc123/download")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/zip"
        assert "attachment" in resp.headers["content-disposition"]
        assert "campaign-test-campaign-abc123.zip" in resp.headers["content-disposition"]

    def test_download_zip_contains_expected_files(
        self, client: TestClient, tmp_campaign_dir: Path
    ) -> None:
        resp = client.get("/api/v1/campaigns/test-campaign-abc123/download")
        assert resp.status_code == 200
        zip_bytes = resp.content
        with zipfile.ZipFile(io.BytesIO(zip_bytes), "r") as zf:
            names = zf.namelist()
        # Root-level files
        assert "run.json" in names
        assert "samples.json" in names
        assert "aggregated_results.csv" in names
        assert "failed_simulations.csv" in names
        # Per-sample KPI files
        assert "samples/sample-000/kpi.json" in names
        assert "samples/sample-001/kpis.json" in names
        assert "samples/sample-002/kpi.json" in names
        # Plot files
        assert "plots/pareto.png" in names
        assert "summary.png" in names

    def test_download_zip_content_is_valid(
        self, client: TestClient, tmp_campaign_dir: Path
    ) -> None:
        resp = client.get("/api/v1/campaigns/test-campaign-abc123/download")
        assert resp.status_code == 200
        zip_bytes = resp.content
        with zipfile.ZipFile(io.BytesIO(zip_bytes), "r") as zf:
            run_json_bytes = zf.read("run.json")
        run_json = json.loads(run_json_bytes.decode())
        assert run_json["campaign_id"] == "test-campaign-abc123"

    def test_download_campaign_not_found(self, client: TestClient) -> None:
        resp = client.get("/api/v1/campaigns/nonexistent-campaign/download")
        assert resp.status_code == 404

    def test_download_include_sql_false(
        self, client: TestClient, tmp_campaign_dir: Path
    ) -> None:
        # Create a fake eplusout.sql to verify it's NOT included by default
        work_dir = tmp_campaign_dir / "work" / "sim" / "sample-000"
        (work_dir / "eplusout.sql").write_text("fake sql content")
        resp = client.get("/api/v1/campaigns/test-campaign-abc123/download?include_sql=0")
        assert resp.status_code == 200
        zip_bytes = resp.content
        with zipfile.ZipFile(io.BytesIO(zip_bytes), "r") as zf:
            names = zf.namelist()
        assert "samples/sample-000/eplusout.sql" not in names

    def test_download_include_sql_true(
        self, client: TestClient, tmp_campaign_dir: Path
    ) -> None:
        # Create a fake eplusout.sql to verify it IS included when requested
        work_dir = tmp_campaign_dir / "work" / "sim" / "sample-000"
        (work_dir / "eplusout.sql").write_text("fake sql content")
        resp = client.get("/api/v1/campaigns/test-campaign-abc123/download?include_sql=1")
        assert resp.status_code == 200
        zip_bytes = resp.content
        with zipfile.ZipFile(io.BytesIO(zip_bytes), "r") as zf:
            names = zf.namelist()
        assert "samples/sample-000/eplusout.sql" in names

    def test_download_content_length_header(
        self, client: TestClient, tmp_campaign_dir: Path
    ) -> None:
        resp = client.get("/api/v1/campaigns/test-campaign-abc123/download")
        assert resp.status_code == 200
        assert "content-length" in resp.headers
        # Content-Length should match actual zip size
        zip_bytes = resp.content
        assert int(resp.headers["content-length"]) == len(zip_bytes)

    def test_download_campaign_id_sanitization(
        self, tmp_campaign_dir: Path
    ) -> None:
        """Campaign IDs with traversal characters are rejected with 400."""
        # Create a campaign directory with a "suspicious" name containing ..
        special_dir = tmp_campaign_dir.parent / "campaign-abc..123"
        special_dir.mkdir()
        (special_dir / "run.json").write_text(json.dumps({"campaign_id": "campaign-abc..123"}))
        app = create_app(campaigns_base_dir=tmp_campaign_dir.parent)
        test_client = TestClient(app)
        resp = test_client.get("/api/v1/campaigns/campaign-abc..123/download")
        # _campaign_dir_from_id raises HTTPException(400) for paths with ".."
        assert resp.status_code == 400

    def test_download_no_campaigns_base_dir(self) -> None:
        """When neither outdir nor campaigns_base_dir is set, return 503."""
        app = create_app(outdir=None, campaigns_base_dir=None)
        client = TestClient(app)
        resp = client.get("/api/v1/campaigns/any/download")
        assert resp.status_code == 503


class TestCampaignDownloadClientMethod:
    """Tests for OSimFlowClient.download_campaign."""

    @pytest.mark.asyncio
    async def test_download_campaign_saves_zip(self, tmp_path: Path) -> None:
        pytest.importorskip("httpx")
        from unittest.mock import AsyncMock, patch

        from osimflow.client import OSimFlowClient

        output_file = tmp_path / "campaign.zip"
        # Simulate a minimal zip
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("run.json", '{"campaign_id": "test"}')
        zip_bytes = zip_buffer.getvalue()

        mock_response = type("MockResponse", (), {"status_code": 200, "content": zip_bytes})()

        client = OSimFlowClient("http://localhost:8000")
        client._client = AsyncMock()
        client._client.get = AsyncMock(return_value=mock_response)  # type: ignore[assignment]

        await client.download_campaign("test-campaign", output_file)

        assert output_file.exists()
        with zipfile.ZipFile(output_file, "r") as zf:
            assert "run.json" in zf.namelist()