"""Tests for osimflow/api/measures.py (issue #348)."""

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
