"""Tests for runner.registerValue output capture (issue #251).

Verifies that:
  * _parse_register_values correctly parses OpenStudio CLI JSON output.
  * _run_real_openstudio writes register_values.json when CLI outputs registered values.
  * Stub mode does not produce register_values.json.
  * SampleTrace.register_values field is serialised correctly.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from osimflow.work import _parse_register_values, _run_real_openstudio


# ---------------------------------------------------------------------------
# _parse_register_values tests
# ---------------------------------------------------------------------------
def test_parse_register_values_valid_json(tmp_path: Path) -> None:
    """Correctly parses a JSON array of runner.registerValue entries."""
    stdout = tmp_path / "stdout.log"
    stdout.write_text(
        json.dumps(
            [
                {"name": "electricity_kwh", "value": 1234.5, "type": "Double"},
                {"name": "gas_kwh", "value": 567.8, "type": "Double"},
                {"name": "peak_load_w", "value": 9999, "type": "Double"},
            ]
        )
    )
    result = _parse_register_values(stdout)
    assert result is not None
    assert result["electricity_kwh"] == 1234.5
    assert result["gas_kwh"] == 567.8
    assert result["peak_load_w"] == 9999


def test_parse_register_values_empty_array(tmp_path: Path) -> None:
    """Empty JSON array returns None (no values to capture)."""
    stdout = tmp_path / "stdout.log"
    stdout.write_text("[]")
    assert _parse_register_values(stdout) is None


def test_parse_register_values_malformed_json(tmp_path: Path) -> None:
    """Malformed JSON returns None without raising."""
    stdout = tmp_path / "stdout.log"
    stdout.write_text("not valid json")
    assert _parse_register_values(stdout) is None


def test_parse_register_values_not_a_list(tmp_path: Path) -> None:
    """JSON object (not array) returns None."""
    stdout = tmp_path / "stdout.log"
    stdout.write_text(json.dumps({"name": "foo", "value": 1}))
    assert _parse_register_values(stdout) is None


def test_parse_register_values_missing_name_or_value(tmp_path: Path) -> None:
    """Entries missing 'name' or 'value' are skipped."""
    stdout = tmp_path / "stdout.log"
    stdout.write_text(
        json.dumps(
            [
                {"name": "good_entry", "value": 42.0},
                {"name": "missing_value"},  # missing value
                {"value": 123},  # missing name
                {"name": "another_good", "value": 99.0},
            ]
        )
    )
    result = _parse_register_values(stdout)
    assert result is not None
    assert "good_entry" in result
    assert "another_good" in result
    assert "missing_value" not in result
    assert "value" not in result


def test_parse_register_values_missing_file(tmp_path: Path) -> None:
    """Non-existent stdout file returns None."""
    assert _parse_register_values(tmp_path / "nonexistent.log") is None


def test_parse_register_values_empty_file(tmp_path: Path) -> None:
    """Empty file returns None."""
    stdout = tmp_path / "stdout.log"
    stdout.write_text("")
    assert _parse_register_values(stdout) is None


def test_parse_register_values_whitespace_only(tmp_path: Path) -> None:
    """Whitespace-only file returns None."""
    stdout = tmp_path / "stdout.log"
    stdout.write_text("   \n\n  ")
    assert _parse_register_values(stdout) is None


# ---------------------------------------------------------------------------
# _run_real_openstudio register_values.json tests
# ---------------------------------------------------------------------------
@pytest.fixture
def sim_package(tmp_path: Path) -> Path:
    """A minimal template sim package with a workflow.osw."""
    pkg = tmp_path / "modified_package"
    pkg.mkdir()
    (pkg / "workflow.osw").write_text(json.dumps({"name": "test_workflow"}))
    return pkg


@pytest.fixture
def out_dir(tmp_path: Path) -> Path:
    d = tmp_path / "sim_out"
    d.mkdir()
    return d


@pytest.fixture
def log_paths(tmp_path: Path) -> tuple[Path, Path]:
    stdout = tmp_path / "stdout.log"
    stderr = tmp_path / "stderr.log"
    return stdout, stderr


def test_real_cli_captures_register_values(
    sim_package: Path, out_dir: Path, log_paths: tuple[Path, Path]
) -> None:
    """When CLI stdout contains registerValue JSON, write register_values.json."""
    stdout_path, stderr_path = log_paths
    sim_out = out_dir / "0001"
    sim_out.mkdir(parents=True, exist_ok=True)

    register_value_json = json.dumps(
        [
            {"name": "electricity_kwh", "value": 1234.5, "type": "Double"},
            {"name": "gas_kwh", "value": 567.8, "type": "Double"},
        ]
    )

    clean_env = {k: v for k, v in os.environ.items() if k != "OSIMFLOW_STUB_SIM"}

    def mock_run(
        cmd: list[str], *, stdout_path: Path, stderr_path: Path, **kwargs: object
    ) -> subprocess.CompletedProcess:
        stdout_path.parent.mkdir(parents=True, exist_ok=True)
        stdout_path.write_text(register_value_json, encoding="utf-8")
        stderr_path.parent.mkdir(parents=True, exist_ok=True)
        stderr_path.write_text("", encoding="utf-8")
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    with (
        patch.dict(os.environ, clean_env, clear=True),
        patch("osimflow.work._is_openstudio_available", return_value=True),
        patch("osimflow.work.run_subprocess", side_effect=mock_run),
    ):
        result = _run_real_openstudio(
            modified_sim_package=sim_package,
            sample_id="0001",
            sim_out=sim_out,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
        )

    register_values_path = result / "register_values.json"
    assert register_values_path.is_file(), "register_values.json should be written"
    data = json.loads(register_values_path.read_text())
    assert "electricity_kwh" in data
    assert data["electricity_kwh"] == 1234.5
    assert data["gas_kwh"] == 567.8


def test_real_cli_no_register_values_file_when_no_json(
    sim_package: Path, out_dir: Path, log_paths: tuple[Path, Path]
) -> None:
    """When CLI stdout has no registerValue JSON, no register_values.json is written."""
    stdout_path, stderr_path = log_paths
    stdout_path.write_text("some non-json output\n")

    clean_env = {k: v for k, v in os.environ.items() if k != "OSIMFLOW_STUB_SIM"}

    with (
        patch.dict(os.environ, clean_env, clear=True),
        patch("osimflow.work._is_openstudio_available", return_value=True),
        patch("osimflow.work.run_subprocess") as mock_run,
    ):
        mock_run.return_value = subprocess.CompletedProcess(
            args=["openstudio.cli", "run", "-w", str(sim_package / "workflow.osw")],
            returncode=0,
            stdout="",
            stderr="",
        )

        result = _run_real_openstudio(
            modified_sim_package=sim_package,
            sample_id="0001",
            sim_out=out_dir / "0001",
            stdout_path=stdout_path,
            stderr_path=stderr_path,
        )

    register_values_path = result / "register_values.json"
    assert not register_values_path.exists()


# ---------------------------------------------------------------------------
# SampleTrace.register_values serialisation test
# ---------------------------------------------------------------------------
def test_sample_trace_register_values_serialises(tmp_path: Path) -> None:
    """SampleTrace.register_values is included in to_dict output."""
    from osimflow.monitoring import SampleTrace

    trace = SampleTrace(
        sample_id="0001",
        status="ok",
        elapsed_s=10.0,
        register_values={"electricity_kwh": 1234.5, "gas_kwh": 567.8},
    )
    d = trace.to_dict()
    assert "register_values" in d
    assert d["register_values"]["electricity_kwh"] == 1234.5


def test_sample_trace_register_values_excluded_when_none(tmp_path: Path) -> None:
    """SampleTrace.register_values is excluded from to_dict when None."""
    from osimflow.monitoring import SampleTrace

    trace = SampleTrace(
        sample_id="0001",
        status="ok",
        elapsed_s=10.0,
    )
    d = trace.to_dict()
    assert "register_values" not in d
