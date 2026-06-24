"""Tests for osimflow/api/measures.py (issue #348, #547)."""

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

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_outdir(tmp_path: Path) -> Path:
    """Create a temporary output directory with a minimal run.json."""
    run_json = {
        "schema_version": 1,
        "campaign_id": "test-campaign-001",
        "started_at": 1000.0,
        "finished_at": 2000.0,
        "config_summary": {"executor": "local", "n_samples": 5},
        "steps": [],
        "per_sample": [],
    }
    (tmp_path / "run.json").write_text(json.dumps(run_json))
    return tmp_path


@pytest.fixture
def client(tmp_outdir: Path) -> TestClient:
    app = create_app(outdir=tmp_outdir)
    return TestClient(app)


@pytest.fixture
def workflow_osw(tmp_path: Path) -> Path:
    """Create a minimal workflow.osw in the outdir."""
    osw = {
        "seed_file": "model.osm",
        "weather_file": "",
        "measure_paths": ["measures"],
        "steps": [
            {
                "measure_dir_name": "SetThermostatSchedule",
                "arguments": {
                    "heating_setpoint": 20.0,
                    "cooling_setpoint": 25.0,
                },
            },
            {
                "measure_dir_name": "SetEnvelopePerformance",
                "arguments": {
                    "wwr": 0.4,
                    "wall_r_value": 3.5,
                },
            },
        ],
    }
    path = tmp_path / "workflow.osw"
    path.write_text(json.dumps(osw))
    return path


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestListMeasures:
    """Tests for GET /api/v1/measures."""

    def test_no_workflow_returns_empty(self, client: TestClient) -> None:
        resp = client.get("/api/v1/measures")
        assert resp.status_code == 200
        data = resp.json()
        assert data["measures"] == []
        assert data["total"] == 0
        assert data["source"] == "none"

    def test_returns_measures_from_workflow(self, tmp_outdir: Path, workflow_osw: Path) -> None:
        app = create_app(outdir=tmp_outdir)
        client = TestClient(app)
        resp = client.get("/api/v1/measures")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 2
        assert data["source"] == "workflow.osw"
        names = {m["measure_dir_name"] for m in data["measures"]}
        assert "SetThermostatSchedule" in names
        assert "SetEnvelopePerformance" in names

    def test_measure_includes_workflow_arguments(
        self, tmp_outdir: Path, workflow_osw: Path
    ) -> None:
        app = create_app(outdir=tmp_outdir)
        client = TestClient(app)
        resp = client.get("/api/v1/measures")
        assert resp.status_code == 200
        measures = {m["measure_dir_name"]: m for m in resp.json()["measures"]}
        thermo = measures["SetThermostatSchedule"]
        # Arguments should be synthesized from workflow defaults
        arg_names = {a["name"] for a in thermo["arguments"]}
        assert "heating_setpoint" in arg_names
        assert "cooling_setpoint" in arg_names

    def test_modified_sim_package_workflow(self, tmp_path: Path) -> None:
        """workflow.osw inside modified_sim_package/ is also discovered."""
        (tmp_path / "run.json").write_text(
            json.dumps({"campaign_id": "x", "started_at": 1.0, "steps": [], "per_sample": []})
        )
        modified_dir = tmp_path / "modified_sim_package"
        modified_dir.mkdir()
        osw = {
            "seed_file": "model.osm",
            "measure_paths": [],
            "steps": [
                {"measure_dir_name": "SingleMeasure", "arguments": {"temp": 21.0}},
            ],
        }
        (modified_dir / "workflow.osw").write_text(json.dumps(osw))

        app = create_app(outdir=tmp_path)
        client = TestClient(app)
        resp = client.get("/api/v1/measures")
        assert resp.status_code == 200
        assert resp.json()["total"] == 1

    def test_template_sim_package_workflow(self, tmp_path: Path) -> None:
        """workflow.osw inside template_sim_package/ is also discovered."""
        (tmp_path / "run.json").write_text(
            json.dumps({"campaign_id": "x", "started_at": 1.0, "steps": [], "per_sample": []})
        )
        template_dir = tmp_path / "template_sim_package"
        template_dir.mkdir()
        osw = {
            "seed_file": "model.osm",
            "measure_paths": [],
            "steps": [
                {"measure_dir_name": "TemplateMeasure", "arguments": {}},
            ],
        }
        (template_dir / "workflow.osw").write_text(json.dumps(osw))

        app = create_app(outdir=tmp_path)
        client = TestClient(app)
        resp = client.get("/api/v1/measures")
        assert resp.status_code == 200
        assert resp.json()["total"] == 1


class TestGetMeasure:
    """Tests for GET /api/v1/measures/{measure_name}."""

    def test_unknown_measure_returns_404(self, tmp_outdir: Path, workflow_osw: Path) -> None:
        app = create_app(outdir=tmp_outdir)
        client = TestClient(app)
        resp = client.get("/api/v1/measures/NonExistentMeasure")
        assert resp.status_code == 404
        assert "not found" in resp.json()["detail"].lower()

    def test_returns_correct_measure(self, tmp_outdir: Path, workflow_osw: Path) -> None:
        app = create_app(outdir=tmp_outdir)
        client = TestClient(app)
        resp = client.get("/api/v1/measures/SetThermostatSchedule")
        assert resp.status_code == 200
        data = resp.json()
        assert data["measure_dir_name"] == "SetThermostatSchedule"

    def test_measure_includes_arguments(self, tmp_outdir: Path, workflow_osw: Path) -> None:
        app = create_app(outdir=tmp_outdir)
        client = TestClient(app)
        resp = client.get("/api/v1/measures/SetEnvelopePerformance")
        assert resp.status_code == 200
        data = resp.json()
        assert data["measure_dir_name"] == "SetEnvelopePerformance"
        assert len(data["arguments"]) >= 2

    def test_no_workflow_returns_404(self, client: TestClient) -> None:
        resp = client.get("/api/v1/measures/AnyMeasure")
        assert resp.status_code == 404
        assert "workflow.osw not found" in resp.json()["detail"]


class TestMeasureArgumentIntrospection:
    """Tests for Ruby/Python measure argument introspection."""

    def test_ruby_measure_introspection(self, tmp_path: Path) -> None:
        """Arguments are extracted from measure.rb source when available."""
        (tmp_path / "run.json").write_text(
            json.dumps({"campaign_id": "x", "started_at": 1.0, "steps": [], "per_sample": []})
        )
        # Create a fake measure directory
        measure_dir = tmp_path / "measures" / "TestRubyMeasure"
        measure_dir.mkdir(parents=True)
        (measure_dir / "measure.rb").write_text(
            """
# Class: SetThermostatSchedule
# This measure sets the thermostat setpoints for heating and cooling.

require 'openstudio'
include OpenStudio::Measure

class TestRubyMeasure < Rule
  def arguments
    args = []
    heating_setpoint = OpenStudio::Measure::MeasureArgument.new("heating_setpoint")
    heating_setpoint.setDisplayName("Heating Setpoint")
    heating_setpoint.setDescription("Dry-bulb temperature for heating")
    heating_setpoint.setAttribute("type", 2)
    heating_setpoint.setDefaultValue(20.0)
    heating_setpoint.setRequired(false)
    args << heating_setpoint
    return args
  end

  def run(model, runner)
    # no-op stub
    true
  end
end
"""
        )
        osw = {
            "seed_file": "model.osm",
            "measure_paths": ["measures"],
            "steps": [
                {"measure_dir_name": "TestRubyMeasure", "arguments": {"heating_setpoint": 22.0}},
            ],
        }
        (tmp_path / "workflow.osw").write_text(json.dumps(osw))

        app = create_app(outdir=tmp_path)
        client = TestClient(app)

        # List
        resp = client.get("/api/v1/measures")
        assert resp.status_code == 200
        measures = {m["measure_dir_name"]: m for m in resp.json()["measures"]}
        m = measures["TestRubyMeasure"]
        assert m["description"] is not None
        assert len(m["arguments"]) >= 1

        # Detail
        resp = client.get("/api/v1/measures/TestRubyMeasure")
        assert resp.status_code == 200
        args = {a["name"]: a for a in resp.json()["arguments"]}
        assert "heating_setpoint" in args
        assert args["heating_setpoint"]["argument_type"] == "Double"
        assert args["heating_setpoint"]["default_value"] == 20.0

    def test_python_measure_introspection(self, tmp_path: Path) -> None:
        """Arguments are extracted from measure.py source when available."""
        (tmp_path / "run.json").write_text(
            json.dumps({"campaign_id": "x", "started_at": 1.0, "steps": [], "per_sample": []})
        )
        measure_dir = tmp_path / "measures" / "TestPythonMeasure"
        measure_dir.mkdir(parents=True)
        (measure_dir / "measure.py").write_text(
            """
# Measure: SetLightingPowerDensity
# Sets the lighting power density in the model.

import openstudio

class TestPythonMeasure(openstudio.OpenStudio_Measure):
    def arguments(self):
        args = []
        lpd = openstudio.OpenStudio.Measure.MeasureArgument.new("lighting_power_density")
        lpd.setDisplayName("Lighting Power Density")
        lpd.setDescription("Watts per square foot of lighting")
        lpd.setAttribute("type", 2)
        lpd.setDefaultValue(10.0)
        lpd.setRequired(true)
        args.append(lpd)
        return args

    def run(self, model, runner):
        pass
"""
        )
        osw = {
            "seed_file": "model.osm",
            "measure_paths": ["measures"],
            "steps": [
                {
                    "measure_dir_name": "TestPythonMeasure",
                    "arguments": {"lighting_power_density": 12.0},
                },
            ],
        }
        (tmp_path / "workflow.osw").write_text(json.dumps(osw))

        app = create_app(outdir=tmp_path)
        client = TestClient(app)

        resp = client.get("/api/v1/measures/TestPythonMeasure")
        assert resp.status_code == 200
        args = {a["name"]: a for a in resp.json()["arguments"]}
        assert "lighting_power_density" in args
        assert args["lighting_power_density"]["argument_type"] == "Double"


class TestNoOutdir:
    """Edge case: no outdir configured."""

    def test_list_measures_no_outdir_returns_503(self) -> None:
        app = create_app(outdir=None)
        client = TestClient(app)
        resp = client.get("/api/v1/measures")
        assert resp.status_code == 503

    def test_get_measure_no_outdir_returns_503(self) -> None:
        app = create_app(outdir=None)
        client = TestClient(app)
        resp = client.get("/api/v1/measures/AnyMeasure")
        assert resp.status_code == 503


# ---------------------------------------------------------------------------
# Uploaded-measure fixtures
# ---------------------------------------------------------------------------


def _make_ruby_measure_zip(measure_name: str = "TestRubyMeasure") -> bytes:
    """Build a valid Ruby measure zip for upload testing."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        measure_rb = f"""
# Class: {measure_name}
# A test measure for unit testing.

require 'openstudio'
include OpenStudio::Measure

class {measure_name} < Rule
  def arguments
    args = []
    lpd = OpenStudio::Measure::MeasureArgument.new("lighting_power_density")
    lpd.setDisplayName("Lighting Power Density")
    lpd.setDescription("Watts per square foot of lighting")
    lpd.setAttribute("type", 2)
    lpd.setDefaultValue(10.0)
    lpd.setRequired(false)
    args << lpd
    return args
  end

  def run(model, runner)
    true
  end
end
"""
        zf.writestr(f"{measure_name}/measure.rb", measure_rb)
        zf.writestr(f"{measure_name}/tests/test.rb", "# test stub")
        zf.writestr(f"{measure_name}/resources/helper.rb", "# helper stub")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Tests for measure upload (issue #547)
# ---------------------------------------------------------------------------


class TestUploadMeasure:
    """Tests for POST /api/v1/measures/upload."""

    def test_upload_measure_zip(self, tmp_path: Path) -> None:
        """A valid Ruby measure zip is accepted and stored."""
        app = create_app(outdir=tmp_path)
        client = TestClient(app)
        zip_bytes = _make_ruby_measure_zip()

        resp = client.post(
            "/api/v1/measures/upload",
            files={"file": ("test_measure.zip", io.BytesIO(zip_bytes), "application/zip")},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "measure_id" in data
        assert data["name"] == "TestRubyMeasure"
        assert "version_uuid" in data
        assert data["argument_count"] == 1
        assert "uploaded successfully" in data["detail"]

    def test_upload_duplicate_hash_returns_existing(self, tmp_path: Path) -> None:
        """Re-uploading the same measure content returns the existing measure_id."""
        app = create_app(outdir=tmp_path)
        client = TestClient(app)
        zip_bytes = _make_ruby_measure_zip()

        # First upload
        resp1 = client.post(
            "/api/v1/measures/upload",
            files={"file": ("test_measure.zip", io.BytesIO(zip_bytes), "application/zip")},
        )
        assert resp1.status_code == 200
        id1 = resp1.json()["measure_id"]

        # Second upload — same content
        resp2 = client.post(
            "/api/v1/measures/upload",
            files={"file": ("test_measure2.zip", io.BytesIO(zip_bytes), "application/zip")},
        )
        assert resp2.status_code == 200
        data2 = resp2.json()
        assert data2["measure_id"] == id1
        assert "already exists" in data2["detail"]

    def test_upload_invalid_file_400(self, tmp_path: Path) -> None:
        """A non-zip/tar.gz file is rejected with 400."""
        app = create_app(outdir=tmp_path)
        client = TestClient(app)

        resp = client.post(
            "/api/v1/measures/upload",
            files={"file": ("not_a_zip.txt", io.BytesIO(b"not a zip at all"), "text/plain")},
        )
        assert resp.status_code == 400
        assert "neither a valid zip nor tar.gz" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# Tests for uuid-based measure endpoints (issue #547)
# ---------------------------------------------------------------------------


class TestUploadedMeasureCrud:
    """Tests for GET/PATCH/DELETE /api/v1/measures/by-id/{measure_id}."""

    def _upload_measure(
        self, tmp_path: Path, name: str = "TestRubyMeasure"
    ) -> tuple[TestClient, str]:
        """Helper: upload a measure and return (client, measure_id)."""
        app = create_app(outdir=tmp_path)
        client = TestClient(app)
        zip_bytes = _make_ruby_measure_zip(name)
        resp = client.post(
            "/api/v1/measures/upload",
            files={"file": ("m.zip", io.BytesIO(zip_bytes), "application/zip")},
        )
        assert resp.status_code == 200
        return client, resp.json()["measure_id"]

    def test_get_measure_by_id(self, tmp_path: Path) -> None:
        """GET /api/v1/measures/by-id/{id} returns full metadata for an uploaded measure."""
        client, mid = self._upload_measure(tmp_path)
        resp = client.get(f"/api/v1/measures/by-id/{mid}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["measure_id"] == mid
        assert data["name"] == "TestRubyMeasure"
        assert data["is_uploaded"] is True
        assert data["version_uuid"] != ""
        assert len(data["arguments"]) == 1

    def test_patch_measure_taxonomy(self, tmp_path: Path) -> None:
        """PATCH updates taxonomy, description, tags, and measure_group."""
        client, mid = self._upload_measure(tmp_path)

        resp = client.patch(
            f"/api/v1/measures/by-id/{mid}",
            json={
                "taxonomy": "Economics.Construction.General",
                "description": "Updated description",
                "tags": ["lighting", "energy"],
                "measure_group": "Lighting",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["taxonomy"] == "Economics.Construction.General"
        assert data["description"] == "Updated description"
        assert data["tags"] == ["lighting", "energy"]
        assert data["measure_group"] == "Lighting"

    def test_delete_uploaded_measure(self, tmp_path: Path) -> None:
        """DELETE removes the uploaded measure from registry and disk."""
        client, mid = self._upload_measure(tmp_path)

        resp = client.delete(f"/api/v1/measures/by-id/{mid}")
        assert resp.status_code == 200
        assert resp.json()["status"] == "deleted"

        # Verify it's gone
        resp2 = client.get(f"/api/v1/measures/by-id/{mid}")
        assert resp2.status_code == 404

    def test_delete_builtin_measure_403(self, tmp_outdir: Path, workflow_osw: Path) -> None:
        """DELETE on a workflow-discovered measure returns 403."""
        app = create_app(outdir=tmp_outdir)
        client = TestClient(app)

        # First upload a measure so there's something in the registry
        zip_bytes = _make_ruby_measure_zip()
        resp = client.post(
            "/api/v1/measures/upload",
            files={"file": ("m.zip", io.BytesIO(zip_bytes), "application/zip")},
        )
        assert resp.status_code == 200
        uploaded_id = resp.json()["measure_id"]

        # Attempting to delete a non-existent workflow measure via by-id should 404
        # (there are no workflow measures in the by-id registry — they use name-based paths)
        resp2 = client.delete(f"/api/v1/measures/by-id/{uploaded_id}")
        # Since is_uploaded=True, it should succeed for the uploaded measure
        assert resp2.status_code == 200

        # Verify a truly non-existent UUID returns 404
        resp3 = client.delete("/api/v1/measures/by-id/00000000-0000-0000-0000-000000000000")
        assert resp3.status_code == 404

    def test_get_uploaded_measure_404_for_workflow_name(
        self, tmp_outdir: Path, workflow_osw: Path
    ) -> None:
        """GET /by-id on a workflow measure name returns 404."""
        app = create_app(outdir=tmp_outdir)
        client = TestClient(app)
        # Try to look up a workflow measure name as a UUID — should 404
        resp = client.get("/api/v1/measures/by-id/SetThermostatSchedule")
        assert resp.status_code == 404


class TestListMeasuresWithSearch:
    """Tests for search/taxonomy/tag filtering on GET /api/v1/measures."""

    def test_list_measures_with_search(self, tmp_path: Path) -> None:
        """The search parameter filters by name and description."""
        app = create_app(outdir=tmp_path)
        client = TestClient(app)

        # Upload a measure
        zip_bytes = _make_ruby_measure_zip("LightingPowerMeasure")
        client.post(
            "/api/v1/measures/upload",
            files={"file": ("m.zip", io.BytesIO(zip_bytes), "application/zip")},
        )

        # Search for it
        resp = client.get("/api/v1/measures?search=lighting")
        assert resp.status_code == 200
        names = {m["measure_dir_name"] for m in resp.json()["measures"]}
        assert "LightingPowerMeasure" in names

        # Non-matching search returns empty
        resp2 = client.get("/api/v1/measures?search=nonexistent")
        assert resp2.status_code == 200
        assert resp2.json()["total"] == 0

    def test_list_measures_with_taxonomy_filter(self, tmp_path: Path) -> None:
        """The taxonomy parameter filters uploaded measures by taxonomy prefix."""
        app = create_app(outdir=tmp_path)
        client = TestClient(app)

        # Upload and tag
        zip_bytes = _make_ruby_measure_zip("EconomicsMeasure")
        resp = client.post(
            "/api/v1/measures/upload",
            files={"file": ("m.zip", io.BytesIO(zip_bytes), "application/zip")},
        )
        mid = resp.json()["measure_id"]

        # Set taxonomy
        client.patch(
            f"/api/v1/measures/by-id/{mid}",
            json={"taxonomy": "Economics.Lighting"},
        )

        # Filter by taxonomy prefix
        resp2 = client.get("/api/v1/measures?taxonomy=Economics")
        assert resp2.status_code == 200
        names = {m["measure_dir_name"] for m in resp2.json()["measures"]}
        assert "EconomicsMeasure" in names

        # Non-matching prefix returns empty
        resp3 = client.get("/api/v1/measures?taxonomy=Energy")
        assert resp3.status_code == 200
        assert resp3.json()["total"] == 0

    def test_list_measures_with_tag_filter(self, tmp_path: Path) -> None:
        """The tag parameter filters uploaded measures by exact tag."""
        app = create_app(outdir=tmp_path)
        client = TestClient(app)

        # Upload
        zip_bytes = _make_ruby_measure_zip("TaggedMeasure")
        resp = client.post(
            "/api/v1/measures/upload",
            files={"file": ("m.zip", io.BytesIO(zip_bytes), "application/zip")},
        )
        mid = resp.json()["measure_id"]

        # Add tag
        client.patch(
            f"/api/v1/measures/by-id/{mid}",
            json={"tags": ["envelope", "energy"]},
        )

        # Filter by tag
        resp2 = client.get("/api/v1/measures?tag=envelope")
        assert resp2.status_code == 200
        names = {m["measure_dir_name"] for m in resp2.json()["measures"]}
        assert "TaggedMeasure" in names

        # Non-matching tag returns empty
        resp3 = client.get("/api/v1/measures?tag=hvac")
        assert resp3.status_code == 200
        assert resp3.json()["total"] == 0
