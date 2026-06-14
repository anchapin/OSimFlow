"""Tests for the variable management API (issue #347)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="osimflow[api] extra required")
pytest.importorskip("slowapi", reason="osimflow[api] extra required")
from fastapi.testclient import TestClient

from osimflow.api import create_app


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
        ],
        "per_sample": [],
    }
    (tmp_path / "run.json").write_text(json.dumps(run_json))
    return tmp_path


@pytest.fixture
def client(tmp_outdir: Path) -> TestClient:
    """Read-write test client."""
    app = create_app(outdir=tmp_outdir, read_only=False)
    return TestClient(app)


@pytest.fixture
def read_only_client(tmp_outdir: Path) -> TestClient:
    """Read-only test client."""
    app = create_app(outdir=tmp_outdir, read_only=True)
    return TestClient(app)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sample_variables() -> list[dict[str, object]]:
    return [
        {
            "name": "heating_setpoint",
            "distribution": "uniform",
            "min": 18.0,
            "max": 22.0,
            "description": "Indoor heating setpoint in Celsius",
        },
        {
            "name": "cooling_setpoint",
            "distribution": "uniform",
            "min": 24.0,
            "max": 28.0,
            "description": "Indoor cooling setpoint in Celsius",
        },
    ]


# ---------------------------------------------------------------------------
# GET /api/v1/variables — list
# ---------------------------------------------------------------------------


def test_list_variables_empty(client: TestClient, tmp_outdir: Path) -> None:
    """No variables.yml exists yet — returns empty list."""
    resp = client.get("/api/v1/variables")
    assert resp.status_code == 200
    data = resp.json()
    assert data["variables"] == []
    assert data["total"] == 0


def test_list_variables_with_data(client: TestClient, tmp_outdir: Path) -> None:
    """variables.yml exists — returns the variables."""
    var_path = tmp_outdir / "variables.yml"
    var_path.write_text(
        "variables:\n"
        "  - name: heating_setpoint\n"
        "    distribution: uniform\n"
        "    min: 18.0\n"
        "    max: 22.0\n"
    )
    resp = client.get("/api/v1/variables")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert data["variables"][0]["name"] == "heating_setpoint"
    assert data["variables"][0]["distribution"] == "uniform"


# ---------------------------------------------------------------------------
# GET /api/v1/variables/{name} — detail
# ---------------------------------------------------------------------------


def test_get_variable_not_found(client: TestClient, tmp_outdir: Path) -> None:
    """Variable does not exist — returns 404."""
    resp = client.get("/api/v1/variables/heating_setpoint")
    assert resp.status_code == 404


def test_get_variable_found(client: TestClient, tmp_outdir: Path) -> None:
    """Variable exists — returns full detail."""
    var_path = tmp_outdir / "variables.yml"
    var_path.write_text(
        "variables:\n"
        "  - name: heating_setpoint\n"
        "    distribution: uniform\n"
        "    min: 18.0\n"
        "    max: 22.0\n"
        "    description: Indoor heating setpoint\n"
    )
    resp = client.get("/api/v1/variables/heating_setpoint")
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "heating_setpoint"
    assert data["distribution"] == "uniform"
    assert data["min"] == 18.0
    assert data["max"] == 22.0
    assert data["description"] == "Indoor heating setpoint"


# ---------------------------------------------------------------------------
# POST /api/v1/variables — create
# ---------------------------------------------------------------------------


def test_create_variable_read_only(read_only_client: TestClient, tmp_outdir: Path) -> None:
    """Read-only mode — returns 403."""
    resp = read_only_client.post(
        "/api/v1/variables",
        json={"name": "heating_setpoint", "distribution": "uniform", "min": 18.0, "max": 22.0},
    )
    assert resp.status_code == 403


def test_create_variable_success(client: TestClient, tmp_outdir: Path) -> None:
    """Successfully create a new variable."""
    resp = client.post(
        "/api/v1/variables",
        json={
            "name": "heating_setpoint",
            "distribution": "uniform",
            "min": 18.0,
            "max": 22.0,
            "description": "Indoor heating setpoint",
        },
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "heating_setpoint"
    assert data["distribution"] == "uniform"
    assert data["min"] == 18.0
    assert data["max"] == 22.0

    # Verify file was written
    var_path = tmp_outdir / "variables.yml"
    assert var_path.exists()
    import yaml

    loaded = yaml.safe_load(var_path.read_text())
    assert len(loaded["variables"]) == 1
    assert loaded["variables"][0]["name"] == "heating_setpoint"


def test_create_variable_duplicate(client: TestClient, tmp_outdir: Path) -> None:
    """Creating a variable with a duplicate name returns 409."""
    var_path = tmp_outdir / "variables.yml"
    var_path.write_text(
        "variables:\n"
        "  - name: heating_setpoint\n"
        "    distribution: uniform\n"
        "    min: 18.0\n"
        "    max: 22.0\n"
    )
    resp = client.post(
        "/api/v1/variables",
        json={"name": "heating_setpoint", "distribution": "uniform", "min": 18.0, "max": 22.0},
    )
    assert resp.status_code == 409


def test_create_variable_invalid_distribution(client: TestClient, tmp_outdir: Path) -> None:
    """Unknown distribution — returns 400."""
    resp = client.post(
        "/api/v1/variables",
        json={"name": "heating_setpoint", "distribution": "unknown_dist", "min": 18.0, "max": 22.0},
    )
    assert resp.status_code == 400
    assert "unknown distribution" in resp.json()["detail"].lower()


def test_create_variable_missing_required_param(client: TestClient, tmp_outdir: Path) -> None:
    """Uniform distribution without min/max — returns 400."""
    resp = client.post(
        "/api/v1/variables",
        json={"name": "heating_setpoint", "distribution": "uniform"},
    )
    assert resp.status_code == 400
    assert "requires parameter" in resp.json()["detail"].lower()


# ---------------------------------------------------------------------------
# PUT /api/v1/variables/{name} — update
# ---------------------------------------------------------------------------


def test_update_variable_read_only(read_only_client: TestClient, tmp_outdir: Path) -> None:
    """Read-only mode — returns 403."""
    resp = read_only_client.put(
        "/api/v1/variables/heating_setpoint",
        json={"min": 19.0},
    )
    assert resp.status_code == 403


def test_update_variable_not_found(client: TestClient, tmp_outdir: Path) -> None:
    """Variable does not exist — returns 404."""
    resp = client.put(
        "/api/v1/variables/heating_setpoint",
        json={"min": 19.0},
    )
    assert resp.status_code == 404


def test_update_variable_success(client: TestClient, tmp_outdir: Path) -> None:
    """Successfully update a variable's min value."""
    var_path = tmp_outdir / "variables.yml"
    var_path.write_text(
        "variables:\n"
        "  - name: heating_setpoint\n"
        "    distribution: uniform\n"
        "    min: 18.0\n"
        "    max: 22.0\n"
    )
    resp = client.put(
        "/api/v1/variables/heating_setpoint",
        json={"min": 19.0, "max": 23.0},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["min"] == 19.0
    assert data["max"] == 23.0

    # Verify file was updated
    import yaml

    loaded = yaml.safe_load(var_path.read_text())
    assert loaded["variables"][0]["min"] == 19.0


def test_update_variable_rename(client: TestClient, tmp_outdir: Path) -> None:
    """Successfully rename a variable."""
    var_path = tmp_outdir / "variables.yml"
    var_path.write_text(
        "variables:\n"
        "  - name: heating_setpoint\n"
        "    distribution: uniform\n"
        "    min: 18.0\n"
        "    max: 22.0\n"
    )
    resp = client.put(
        "/api/v1/variables/heating_setpoint",
        json={"name": "new_heating_setpoint"},
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == "new_heating_setpoint"


def test_update_variable_rename_conflict(client: TestClient, tmp_outdir: Path) -> None:
    """Renaming to an existing variable's name returns 409."""
    var_path = tmp_outdir / "variables.yml"
    var_path.write_text(
        "variables:\n"
        "  - name: heating_setpoint\n"
        "    distribution: uniform\n"
        "    min: 18.0\n"
        "    max: 22.0\n"
        "  - name: cooling_setpoint\n"
        "    distribution: uniform\n"
        "    min: 24.0\n"
        "    max: 28.0\n"
    )
    resp = client.put(
        "/api/v1/variables/heating_setpoint",
        json={"name": "cooling_setpoint"},
    )
    assert resp.status_code == 409


# ---------------------------------------------------------------------------
# DELETE /api/v1/variables/{name} — delete
# ---------------------------------------------------------------------------


def test_delete_variable_read_only(read_only_client: TestClient, tmp_outdir: Path) -> None:
    """Read-only mode — returns 403."""
    resp = read_only_client.delete("/api/v1/variables/heating_setpoint")
    assert resp.status_code == 403


def test_delete_variable_not_found(client: TestClient, tmp_outdir: Path) -> None:
    """Variable does not exist — returns 404."""
    resp = client.delete("/api/v1/variables/heating_setpoint")
    assert resp.status_code == 404


def test_delete_variable_success(client: TestClient, tmp_outdir: Path) -> None:
    """Successfully delete a variable."""
    var_path = tmp_outdir / "variables.yml"
    var_path.write_text(
        "variables:\n"
        "  - name: heating_setpoint\n"
        "    distribution: uniform\n"
        "    min: 18.0\n"
        "    max: 22.0\n"
        "  - name: cooling_setpoint\n"
        "    distribution: uniform\n"
        "    min: 24.0\n"
        "    max: 28.0\n"
    )
    resp = client.delete("/api/v1/variables/heating_setpoint")
    assert resp.status_code == 200
    assert resp.json()["name"] == "heating_setpoint"
    assert resp.json()["status"] == "deleted"

    # Verify file was updated
    import yaml

    loaded = yaml.safe_load(var_path.read_text())
    assert len(loaded["variables"]) == 1
    assert loaded["variables"][0]["name"] == "cooling_setpoint"


# ---------------------------------------------------------------------------
# Cache invalidation
# ---------------------------------------------------------------------------


def test_cache_invalidation_on_create(client: TestClient, tmp_outdir: Path) -> None:
    """Creating a variable invalidates the LHS cache."""
    # Set up cache with a GENERATE_LHS_SAMPLES entry
    work_dir = tmp_outdir / "work"
    work_dir.mkdir(parents=True, exist_ok=True)
    cache_db = work_dir / "cache.sqlite"
    import sqlite3

    conn = sqlite3.connect(cache_db)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS cache_entries ("
        "  step TEXT, sample_id TEXT, openstudio_version TEXT, "
        "  inputs_sha256 TEXT, code_sha256 TEXT, container_digest TEXT, "
        "  generation INTEGER DEFAULT 0, output_path TEXT, "
        "  started_at REAL, finished_at REAL, exit_code INTEGER, "
        "  PRIMARY KEY (step, sample_id, openstudio_version, inputs_sha256, "
        "              code_sha256, container_digest, generation)"
        ")"
    )
    conn.execute(
        "INSERT OR REPLACE INTO cache_entries "
        "(step, sample_id, openstudio_version, inputs_sha256, code_sha256, "
        " container_digest, generation, output_path, started_at, finished_at, exit_code) "
        "VALUES ('GENERATE_LHS_SAMPLES', 'ALL', '3.11.0', 'abc', 'def', 'ghi', 0, "
        "'/tmp/out', 1.0, 2.0, 0)"
    )
    conn.commit()
    conn.close()

    # Create a variable
    client.post(
        "/api/v1/variables",
        json={"name": "heating_setpoint", "distribution": "uniform", "min": 18.0, "max": 22.0},
    )

    # Verify cache was invalidated
    conn2 = sqlite3.connect(cache_db)
    row = conn2.execute(
        "SELECT COUNT(*) FROM cache_entries WHERE step='GENERATE_LHS_SAMPLES'"
    ).fetchone()
    conn2.close()
    assert row is not None
    assert row[0] == 0


def test_cache_invalidation_on_update(client: TestClient, tmp_outdir: Path) -> None:
    """Updating a variable invalidates the LHS cache."""
    var_path = tmp_outdir / "variables.yml"
    var_path.write_text(
        "variables:\n"
        "  - name: heating_setpoint\n"
        "    distribution: uniform\n"
        "    min: 18.0\n"
        "    max: 22.0\n"
    )
    # Set up cache
    work_dir = tmp_outdir / "work"
    work_dir.mkdir(parents=True, exist_ok=True)
    cache_db = work_dir / "cache.sqlite"
    import sqlite3

    conn = sqlite3.connect(cache_db)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS cache_entries ("
        "  step TEXT, sample_id TEXT, openstudio_version TEXT, "
        "  inputs_sha256 TEXT, code_sha256 TEXT, container_digest TEXT, "
        "  generation INTEGER DEFAULT 0, output_path TEXT, "
        "  started_at REAL, finished_at REAL, exit_code INTEGER, "
        "  PRIMARY KEY (step, sample_id, openstudio_version, inputs_sha256, "
        "              code_sha256, container_digest, generation)"
        ")"
    )
    conn.execute(
        "INSERT OR REPLACE INTO cache_entries "
        "(step, sample_id, openstudio_version, inputs_sha256, code_sha256, "
        " container_digest, generation, output_path, started_at, finished_at, exit_code) "
        "VALUES ('GENERATE_LHS_SAMPLES', 'ALL', '3.11.0', 'abc', 'def', 'ghi', 0, "
        "'/tmp/out', 1.0, 2.0, 0)"
    )
    conn.commit()
    conn.close()

    # Update the variable
    client.put(
        "/api/v1/variables/heating_setpoint",
        json={"min": 19.0},
    )

    # Verify cache was invalidated
    conn2 = sqlite3.connect(cache_db)
    row = conn2.execute(
        "SELECT COUNT(*) FROM cache_entries WHERE step='GENERATE_LHS_SAMPLES'"
    ).fetchone()
    conn2.close()
    assert row is not None
    assert row[0] == 0


def test_cache_invalidation_on_delete(client: TestClient, tmp_outdir: Path) -> None:
    """Deleting a variable invalidates the LHS cache."""
    var_path = tmp_outdir / "variables.yml"
    var_path.write_text(
        "variables:\n"
        "  - name: heating_setpoint\n"
        "    distribution: uniform\n"
        "    min: 18.0\n"
        "    max: 22.0\n"
    )
    # Set up cache
    work_dir = tmp_outdir / "work"
    work_dir.mkdir(parents=True, exist_ok=True)
    cache_db = work_dir / "cache.sqlite"
    import sqlite3

    conn = sqlite3.connect(cache_db)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS cache_entries ("
        "  step TEXT, sample_id TEXT, openstudio_version TEXT, "
        "  inputs_sha256 TEXT, code_sha256 TEXT, container_digest TEXT, "
        "  generation INTEGER DEFAULT 0, output_path TEXT, "
        "  started_at REAL, finished_at REAL, exit_code INTEGER, "
        "  PRIMARY KEY (step, sample_id, openstudio_version, inputs_sha256, "
        "              code_sha256, container_digest, generation)"
        ")"
    )
    conn.execute(
        "INSERT OR REPLACE INTO cache_entries "
        "(step, sample_id, openstudio_version, inputs_sha256, code_sha256, "
        " container_digest, generation, output_path, started_at, finished_at, exit_code) "
        "VALUES ('GENERATE_LHS_SAMPLES', 'ALL', '3.11.0', 'abc', 'def', 'ghi', 0, "
        "'/tmp/out', 1.0, 2.0, 0)"
    )
    conn.commit()
    conn.close()

    # Delete the variable
    client.delete("/api/v1/variables/heating_setpoint")

    # Verify cache was invalidated
    conn2 = sqlite3.connect(cache_db)
    row = conn2.execute(
        "SELECT COUNT(*) FROM cache_entries WHERE step='GENERATE_LHS_SAMPLES'"
    ).fetchone()
    conn2.close()
    assert row is not None
    assert row[0] == 0
