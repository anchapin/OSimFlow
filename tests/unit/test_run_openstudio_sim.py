"""Tests for run_openstudio_sim real CLI invocation (issue #31).

TDD RED phase: these tests define the expected behavior for the real
OpenStudio CLI invocation path. The existing stub behavior is preserved
when openstudio.cli is not available on PATH, so all existing integration
tests continue to pass without modification.

Coverage:
  * _find_workflow_osw discovers the .osw in the modified package.
  * _is_openstudio_available returns True when shutil.which finds the CLI.
  * _is_stub_mode returns True when OSIMFLOW_STUB_SIM=1 is set.
  * run_openstudio_sim uses real CLI when available and not in stub mode.
  * run_openstudio_sim falls back to stub when CLI is not available.
  * Real CLI path does NOT write placeholder eplusout.sql/err.
  * OSIMFLOW_RUN_REAL_OPENSTUDIO=1 gated E2E test skeleton.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from osimflow.work import run_openstudio_sim


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def sim_package(tmp_path: Path) -> Path:
    """A minimal template sim package with a workflow.osw."""
    pkg = tmp_path / "modified_package"
    pkg.mkdir()
    (pkg / "workflow.osw").write_text(json.dumps({"name": "test_workflow"}))
    (pkg / "model.osm").write_text(json.dumps({"attributes": {"u1": 0.0}}))
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


# ---------------------------------------------------------------------------
# _find_workflow_osw tests
# ---------------------------------------------------------------------------
def test_find_workflow_osw_discovers_osw(sim_package: Path) -> None:
    """The helper should find workflow.osw inside the modified package."""
    from osimflow.work import _find_workflow_osw

    result = _find_workflow_osw(sim_package)
    assert result is not None
    assert result.name == "workflow.osw"


def test_find_workflow_osw_returns_none_when_no_osw(tmp_path: Path) -> None:
    """When no .osw exists in the package, return None."""
    from osimflow.work import _find_workflow_osw

    empty_dir = tmp_path / "empty_pkg"
    empty_dir.mkdir()
    assert _find_workflow_osw(empty_dir) is None


def test_find_workflow_osw_prefers_root_osw_over_nested(tmp_path: Path) -> None:
    """When multiple .osw files exist, prefer the one at the root."""
    from osimflow.work import _find_workflow_osw

    pkg = tmp_path / "multi_osw"
    pkg.mkdir()
    (pkg / "workflow.osw").write_text('{"root": true}')
    nested = pkg / "subdir"
    nested.mkdir()
    (nested / "workflow.osw").write_text('{"nested": true}')
    result = _find_workflow_osw(pkg)
    assert result is not None
    assert result.read_text() == '{"root": true}'


# ---------------------------------------------------------------------------
# _is_openstudio_available tests
# ---------------------------------------------------------------------------
def test_is_openstudio_available_true_when_found() -> None:
    """Returns True when shutil.which finds openstudio.cli."""
    from osimflow.work import _is_openstudio_available

    with patch("shutil.which", return_value="/usr/local/bin/openstudio.cli"):
        assert _is_openstudio_available() is True


def test_is_openstudio_available_false_when_not_found() -> None:
    """Returns False when shutil.which returns None."""
    from osimflow.work import _is_openstudio_available

    with patch("shutil.which", return_value=None):
        assert _is_openstudio_available() is False


# ---------------------------------------------------------------------------
# _is_stub_mode tests
# ---------------------------------------------------------------------------
def test_is_stub_mode_true_when_env_set() -> None:
    """Returns True when OSIMFLOW_STUB_SIM=1."""
    from osimflow.work import _is_stub_mode

    with patch.dict(os.environ, {"OSIMFLOW_STUB_SIM": "1"}):
        assert _is_stub_mode() is True


def test_is_stub_mode_false_when_env_unset() -> None:
    """Returns False when OSIMFLOW_STUB_SIM is not set."""
    from osimflow.work import _is_stub_mode

    env = {k: v for k, v in os.environ.items() if k != "OSIMFLOW_STUB_SIM"}
    with patch.dict(os.environ, env, clear=True):
        assert _is_stub_mode() is False


def test_is_stub_mode_false_when_env_not_one() -> None:
    """Returns False when OSIMFLOW_STUB_SIM is set to something other than 1."""
    from osimflow.work import _is_stub_mode

    with patch.dict(os.environ, {"OSIMFLOW_STUB_SIM": "0"}):
        assert _is_stub_mode() is False


# ---------------------------------------------------------------------------
# run_openstudio_sim: stub fallback path (existing behavior)
# ---------------------------------------------------------------------------
def test_stub_mode_writes_placeholder_sql(
    sim_package: Path, out_dir: Path, log_paths: tuple[Path, Path]
) -> None:
    """When OSIMFLOW_STUB_SIM=1, the stub writes placeholder eplusout.sql."""
    stdout_path, stderr_path = log_paths
    with patch.dict(os.environ, {"OSIMFLOW_STUB_SIM": "1"}):
        result = run_openstudio_sim(
            modified_sim_package=sim_package,
            sample_id="0001",
            openstudio_version="3.4.0",
            out=out_dir,
            simulate_work_s=0.0,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
        )
    assert result == out_dir / "0001"
    assert (result / "eplusout.sql").is_file()
    assert (result / "eplusout.sql").read_text() == "-- placeholder sql"


def test_no_cli_falls_back_to_stub(
    sim_package: Path, out_dir: Path, log_paths: tuple[Path, Path]
) -> None:
    """When openstudio.cli is not on PATH and not in stub mode,
    the function falls back to stub behavior."""
    stdout_path, stderr_path = log_paths
    env = {k: v for k, v in os.environ.items() if k != "OSIMFLOW_STUB_SIM"}
    with (
        patch.dict(os.environ, env, clear=True),
        patch("osimflow.work._is_openstudio_available", return_value=False),
    ):
        result = run_openstudio_sim(
            modified_sim_package=sim_package,
            sample_id="0001",
            openstudio_version="3.4.0",
            out=out_dir,
            simulate_work_s=0.0,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
        )
    # Falls back to stub: placeholder sql is written
    assert (result / "eplusout.sql").is_file()


# ---------------------------------------------------------------------------
# run_openstudio_sim: real CLI path
# ---------------------------------------------------------------------------
def test_real_cli_invoked_when_available(
    sim_package: Path, out_dir: Path, log_paths: tuple[Path, Path]
) -> None:
    """When openstudio.cli is available and not in stub mode,
    the function invokes `openstudio.cli run -w workflow.osw`."""
    stdout_path, stderr_path = log_paths

    env = {k: v for k, v in os.environ.items() if k != "OSIMFLOW_STUB_SIM"}

    with (
        patch.dict(os.environ, env, clear=True),
        patch("osimflow.work._is_openstudio_available", return_value=True),
        patch("osimflow.work.run_subprocess") as mock_run,
    ):
        # Simulate a successful CLI run — the real CLI writes eplusout.sql
        mock_run.return_value = subprocess.CompletedProcess(
            args=["openstudio.cli", "run", "-w", str(sim_package / "workflow.osw")],
            returncode=0,
            stdout="",
            stderr="",
        )

        run_openstudio_sim(
            modified_sim_package=sim_package,
            sample_id="0001",
            openstudio_version="3.4.0",
            out=out_dir,
            simulate_work_s=0.0,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
        )

    # Verify openstudio.cli was invoked with correct args
    mock_run.assert_called_once()
    call_args = mock_run.call_args
    cmd = call_args[0][0] if call_args[0] else call_args.kwargs.get("cmd", [])
    assert cmd[0] == "openstudio.cli"
    assert "run" in cmd
    assert "-w" in cmd


def test_real_cli_does_not_write_placeholder_sql(
    sim_package: Path, out_dir: Path, log_paths: tuple[Path, Path]
) -> None:
    """The real CLI path must NOT write placeholder eplusout.sql/err."""
    stdout_path, stderr_path = log_paths

    env = {k: v for k, v in os.environ.items() if k != "OSIMFLOW_STUB_SIM"}

    sim_result: Path = Path()  # will be assigned inside the with block

    with (
        patch.dict(os.environ, env, clear=True),
        patch("osimflow.work._is_openstudio_available", return_value=True),
        patch("osimflow.work.run_subprocess") as mock_run,
    ):
        mock_run.return_value = subprocess.CompletedProcess(
            args=["openstudio.cli", "run", "-w", str(sim_package / "workflow.osw")],
            returncode=0,
            stdout="",
            stderr="",
        )

        sim_result = run_openstudio_sim(
            modified_sim_package=sim_package,
            sample_id="0001",
            openstudio_version="3.4.0",
            out=out_dir,
            simulate_work_s=0.0,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
        )

    # No placeholder files should be written by the framework
    assert (
        not (sim_result / "eplusout.sql").exists()
        or (sim_result / "eplusout.sql").read_text() != "-- placeholder sql"
    )
    assert (
        not (sim_result / "eplusout.err").exists()
        or (sim_result / "eplusout.err").read_text() != ""
    )


def test_real_cli_raises_on_missing_workflow_osw(
    tmp_path: Path, out_dir: Path, log_paths: tuple[Path, Path]
) -> None:
    """When no workflow.osw is found in the modified package, raise RuntimeError."""
    pkg = tmp_path / "no_osw_package"
    pkg.mkdir()
    (pkg / "model.osm").write_text('{"attributes": {}}')

    stdout_path, stderr_path = log_paths
    env = {k: v for k, v in os.environ.items() if k != "OSIMFLOW_STUB_SIM"}

    with (
        patch.dict(os.environ, env, clear=True),
        patch("osimflow.work._is_openstudio_available", return_value=True),
    ):
        with pytest.raises(RuntimeError, match="workflow.osw"):
            run_openstudio_sim(
                modified_sim_package=pkg,
                sample_id="0001",
                openstudio_version="3.4.0",
                out=out_dir,
                simulate_work_s=0.0,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
            )


def test_real_cli_propagates_subprocess_error(
    sim_package: Path, out_dir: Path, log_paths: tuple[Path, Path]
) -> None:
    """When openstudio.cli exits non-zero, the subprocess error propagates."""
    stdout_path, stderr_path = log_paths
    env = {k: v for k, v in os.environ.items() if k != "OSIMFLOW_STUB_SIM"}

    with (
        patch.dict(os.environ, env, clear=True),
        patch("osimflow.work._is_openstudio_available", return_value=True),
        patch("osimflow.work.run_subprocess") as mock_run,
    ):
        mock_run.side_effect = subprocess.CalledProcessError(
            returncode=1,
            cmd=["openstudio.cli", "run", "-w", "workflow.osw"],
            stderr="EnergyPlus Severe Error",
        )

        with pytest.raises(subprocess.CalledProcessError):
            run_openstudio_sim(
                modified_sim_package=sim_package,
                sample_id="0001",
                openstudio_version="3.4.0",
                out=out_dir,
                simulate_work_s=0.0,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
            )


# ---------------------------------------------------------------------------
# OSIMFLOW_RUN_REAL_OPENSTUDIO=1 gated E2E test (skeleton)
# ---------------------------------------------------------------------------
@pytest.mark.skipif(
    os.environ.get("OSIMFLOW_RUN_REAL_OPENSTUDIO") != "1",
    reason="Set OSIMFLOW_RUN_REAL_OPENSTUDIO=1 to run real OpenStudio E2E tests",
)
class TestRealOpenStudioE2E:
    """End-to-end tests that require a real openstudio.cli on PATH.

    These tests are gated by the OSIMFLOW_RUN_REAL_OPENSTUDIO=1 environment
    variable. They are intended for CI environments or developer machines
    where the NREL OpenStudio CLI is installed (either natively or inside
    the nrel/openstudio container).

    To run:
        OSIMFLOW_RUN_REAL_OPENSTUDIO=1 .venv/bin/pytest tests/unit/test_run_openstudio_sim.py::TestRealOpenStudioE2E -v
    """

    def test_real_cli_invocation_with_minimal_workflow(self, tmp_path: Path) -> None:
        """Invoke openstudio.cli against a minimal workflow.

        This test requires a real OpenStudio installation. It creates a
        minimal .osw and runs it through the CLI, asserting the output
        directory contains the expected artifacts.
        """
        # Create a minimal template package
        pkg = tmp_path / "template"
        pkg.mkdir()
        # Write a minimal .osw that uses no measures (just a seed model)
        (pkg / "workflow.osw").write_text(
            json.dumps(
                {
                    "seed_file": "model.osm",
                    "steps": [],
                }
            )
        )
        # Write a minimal .osm (empty building)
        (pkg / "model.osm").write_text(json.dumps({"attributes": {}}))

        out_dir = tmp_path / "sim_out"
        out_dir.mkdir()
        stdout_path = tmp_path / "stdout.log"
        stderr_path = tmp_path / "stderr.log"

        result = run_openstudio_sim(
            modified_sim_package=pkg,
            sample_id="0001",
            openstudio_version="3.4.0",
            out=out_dir,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
        )

        # The real CLI should produce output files
        assert result.is_dir()
        # Per-sample stdout/stderr logs are populated
        assert stdout_path.is_file()
        assert stderr_path.is_file()
        # The stdout should contain real OpenStudio CLI output (not the stub banner)
        stdout_text = stdout_path.read_text()
        assert "openstudio CLI stub" not in stdout_text
