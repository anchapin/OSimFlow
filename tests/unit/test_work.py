"""Unit tests for osimflow/work.py — per-step work functions.

Covers:
  * default_apply_parameters: subprocess invocation, param JSON, error propagation
  * run_openstudio_sim: stub mode, real CLI path, missing workflow.osw, log capture
  * extract_kpis: in-process call into osimflow._work_scripts.extract_kpis
    (issue #1015 — was subprocess.run per sample, now an in-process call;
    verifies no subprocess is spawned and runs a 100-sample perf smoke).
  * aggregate_results: subprocess invocation, baseline, ts_resolution, error propagation
  * generate_plots: subprocess invocation, baseline, pareto_dir, error propagation
  * preflight_run_model: stub pass, real CLI pass, real CLI severe error, missing osw
  * _extract_severe_error: pattern matching
  * generate_lhs: subprocess invocation and error propagation
"""

from __future__ import annotations

import asyncio
import builtins
import json
import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from osimflow.manifest import MANIFEST_FIELDS
from osimflow.storage import ResultStorage
from osimflow.work import (
    SevereEnergyPlusError,
    _extract_severe_error,
    _find_workflow_osw,
    _is_openstudio_available,
    _is_stub_mode,
    _run_real_openstudio,
    aggregate_results,
    default_apply_parameters,
    extract_kpis,
    generate_lhs,
    generate_plots,
    preflight_run_model,
    publish_kpi_results,
    run_openstudio_sim,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def template_pkg(tmp_path: Path) -> Path:
    pkg = tmp_path / "template"
    pkg.mkdir()
    (pkg / "model.osm").write_text('{"type": "OSM"}')
    return pkg


@pytest.fixture
def out_dir(tmp_path: Path) -> Path:
    d = tmp_path / "out"
    d.mkdir()
    return d


@pytest.fixture
def sim_package(tmp_path: Path) -> Path:
    pkg = tmp_path / "modified_package"
    pkg.mkdir()
    (pkg / "workflow.osw").write_text(json.dumps({"name": "test_workflow"}))
    (pkg / "model.osm").write_text('{"attributes": {"u1": 0.0}}')
    return pkg


@pytest.fixture
def log_paths(tmp_path: Path) -> tuple[Path, Path]:
    return tmp_path / "stdout.log", tmp_path / "stderr.log"


def _env_without_stub() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k != "OSIMFLOW_STUB_SIM"}


# ===========================================================================
# default_apply_parameters
# ===========================================================================
class TestDefaultApplyParameters:
    def test_raises_when_openstudio_not_installed(self, template_pkg: Path) -> None:
        """When OpenStudio Python bindings are not installed, RuntimeError is raised."""
        real_import = builtins.__import__

        def fake_import(name: str, *args: object, **kwargs: object) -> object:
            if name == "openstudio":
                raise ImportError("No module named 'openstudio'")
            return real_import(name, *args, **kwargs)

        with (
            patch("osimflow.work._is_stub_mode", return_value=False),
            patch.object(builtins, "__import__", side_effect=fake_import),
        ):
            with pytest.raises(RuntimeError, match="OpenStudio Python bindings are not installed"):
                default_apply_parameters(
                    template_pkg, {"heating_setpoint": 20.0}, "0001", template_pkg
                )

    def test_raises_when_model_not_found(self, template_pkg: Path) -> None:
        """Raises FileNotFoundError when model.osm is not in sim_dir."""
        (template_pkg / "model.osm").unlink()
        real_import = builtins.__import__
        mock_openstudio = MagicMock()

        def fake_import(name: str, *args: object, **kwargs: object) -> object:
            if name == "openstudio":
                return mock_openstudio
            return real_import(name, *args, **kwargs)

        with (
            patch("osimflow.work._is_stub_mode", return_value=False),
            patch.object(builtins, "__import__", side_effect=fake_import),
        ):
            with pytest.raises(FileNotFoundError, match="model.osm not found"):
                default_apply_parameters(
                    template_pkg, {"heating_setpoint": 20.0}, "0001", template_pkg
                )

    def test_raises_when_model_fails_to_load(self, template_pkg: Path) -> None:
        """Raises RuntimeError when OpenStudio fails to load the model."""
        real_import = builtins.__import__
        mock_openstudio = MagicMock()
        mock_model_opt = MagicMock()
        mock_model_opt.is_initialized.return_value = False
        mock_openstudio.openstudiomodelcore.Model.load.return_value = mock_model_opt

        def fake_import(name: str, *args: object, **kwargs: object) -> object:
            if name == "openstudio":
                return mock_openstudio
            return real_import(name, *args, **kwargs)

        with (
            patch("osimflow.work._is_stub_mode", return_value=False),
            patch.object(builtins, "__import__", side_effect=fake_import),
        ):
            with pytest.raises(RuntimeError, match="OpenStudio failed to load model"):
                default_apply_parameters(
                    template_pkg, {"heating_setpoint": 20.0}, "0001", template_pkg
                )

    def test_applies_mutations_and_saves_model(self, template_pkg: Path) -> None:
        """Mutates model.osm in sim_dir and saves it back."""
        osm_path = template_pkg / "model.osm"
        osm_path.write_text('{"type": "OSM"}')
        real_import = builtins.__import__
        mock_openstudio = MagicMock()
        mock_model = MagicMock()
        mock_model_opt = MagicMock()
        mock_model_opt.is_initialized.return_value = True
        mock_model_opt.get.return_value = mock_model
        mock_openstudio.openstudiomodelcore.Model.load.return_value = mock_model_opt

        def fake_import(name: str, *args: object, **kwargs: object) -> object:
            if name == "openstudio":
                return mock_openstudio
            return real_import(name, *args, **kwargs)

        with (
            patch("osimflow.work._is_stub_mode", return_value=False),
            patch.object(builtins, "__import__", side_effect=fake_import),
            patch("osimflow.work._apply_osm_mutations") as mock_mutate,
        ):
            result = default_apply_parameters(
                template_pkg, {"lighting_power_density": 10.0}, "0001", template_pkg
            )

        assert result == template_pkg
        mock_openstudio.openstudiomodelcore.Model.load.assert_called_once_with(str(osm_path))
        mock_mutate.assert_called_once_with(
            mock_model, mock_openstudio, {"lighting_power_density": 10.0}
        )
        mock_model.save.assert_called_once_with(str(osm_path), overwrite=True)

    def test_accepts_max_retries_kwarg(self, template_pkg: Path) -> None:
        """Regression (#1487): the Campaign forwards max_retries on submit —
        default_apply_parameters must accept the keyword-only kwarg like
        run_openstudio_sim / extract_kpis do (issue #1394 fan-out)."""
        osm_path = template_pkg / "model.osm"
        osm_path.write_text('{"type": "OSM"}')
        real_import = builtins.__import__
        mock_openstudio = MagicMock()
        mock_model = MagicMock()
        mock_model_opt = MagicMock()
        mock_model_opt.is_initialized.return_value = True
        mock_model_opt.get.return_value = mock_model
        mock_openstudio.openstudiomodelcore.Model.load.return_value = mock_model_opt

        def fake_import(name: str, *args: object, **kwargs: object) -> object:
            if name == "openstudio":
                return mock_openstudio
            return real_import(name, *args, **kwargs)

        with (
            patch("osimflow.work._is_stub_mode", return_value=False),
            patch.object(builtins, "__import__", side_effect=fake_import),
            patch("osimflow.work._apply_osm_mutations"),
        ):
            result = default_apply_parameters(
                template_pkg,
                {"lighting_power_density": 10.0},
                "0001",
                template_pkg,
                max_retries=0,
            )

        assert result == template_pkg
        mock_model.save.assert_called_once()

    def test_forwards_max_retries_to_run_with_retry(self, template_pkg: Path) -> None:
        """The retry budget propagates to run_with_retry with step context."""
        osm_path = template_pkg / "model.osm"
        osm_path.write_text('{"type": "OSM"}')
        real_import = builtins.__import__
        mock_openstudio = MagicMock()

        def fake_import(name: str, *args: object, **kwargs: object) -> object:
            if name == "openstudio":
                return mock_openstudio
            return real_import(name, *args, **kwargs)

        with (
            patch("osimflow.work._is_stub_mode", return_value=False),
            patch.object(builtins, "__import__", side_effect=fake_import),
            patch("osimflow.work.run_with_retry") as mock_retry,
        ):
            default_apply_parameters(
                template_pkg,
                {"lighting_power_density": 10.0},
                "0001",
                template_pkg,
                max_retries=7,
            )

        assert mock_retry.call_count == 1
        call = mock_retry.call_args
        assert call.kwargs["max_retries"] == 7
        assert call.kwargs["sample_id"] == "0001"
        assert call.kwargs["step_name"] == "APPLY_PARAMETERS"

    def test_raises_on_mutation_error(self, template_pkg: Path) -> None:
        """Re-raises OSMAttributeError from mutation failures."""
        from osimflow.apply_params import OSMAttributeError

        osm_path = template_pkg / "model.osm"
        osm_path.write_text('{"type": "OSM"}')
        real_import = builtins.__import__
        mock_openstudio = MagicMock()
        mock_model = MagicMock()
        mock_model_opt = MagicMock()
        mock_model_opt.is_initialized.return_value = True
        mock_model_opt.get.return_value = mock_model
        mock_openstudio.openstudiomodelcore.Model.load.return_value = mock_model_opt
        mutation_error = OSMAttributeError(
            "Cannot resolve SpaceType 'Office' for variable 'lighting_power_density'"
        )

        def fake_import(name: str, *args: object, **kwargs: object) -> object:
            if name == "openstudio":
                return mock_openstudio
            return real_import(name, *args, **kwargs)

        with (
            patch("osimflow.work._is_stub_mode", return_value=False),
            patch.object(builtins, "__import__", side_effect=fake_import),
            patch("osimflow.work._apply_osm_mutations", side_effect=mutation_error),
        ):
            with pytest.raises(OSMAttributeError, match="Cannot resolve SpaceType"):
                default_apply_parameters(
                    template_pkg, {"lighting_power_density": 10.0}, "0001", template_pkg
                )


# ===========================================================================
# generate_lhs
# ===========================================================================
class TestGenerateLhs:
    def test_creates_out_dir_and_returns_path(self, tmp_path: Path) -> None:
        var_yml = tmp_path / "variables.yml"
        var_yml.write_text(
            "variables:\n  - name: p1\n    distribution: uniform\n    min: 0\n    max: 1\n"
        )
        out = tmp_path / "lhs_out"
        with patch("osimflow.work.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            result = generate_lhs(var_yml, 5, out)
        assert result == out / "samples.json"
        assert out.is_dir()

    def test_raises_on_subprocess_failure(self, tmp_path: Path) -> None:
        var_yml = tmp_path / "variables.yml"
        var_yml.write_text("variables: []\n")
        out = tmp_path / "lhs_out"
        with patch("osimflow.work.subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.CalledProcessError(
                returncode=1, cmd=[], stderr="lhs error"
            )
            with pytest.raises(RuntimeError, match="generate_lhs failed"):
                generate_lhs(var_yml, 3, out)


# ===========================================================================
# run_openstudio_sim — stub mode
# ===========================================================================
class TestRunOpenstudioSimStub:
    def test_stub_writes_valid_sqlite_sql(
        self, sim_package: Path, out_dir: Path, log_paths: tuple[Path, Path]
    ) -> None:
        """Issue #1419 — stub ``eplusout.sql`` is now a parseable SQLite DB.

        The previous contract wrote the literal string ``"-- placeholder sql"``
        which is not a valid SQLite database, so ``extract_kpis`` raised
        ``sqlite3.DatabaseError`` on every sample.  The new contract
        delegates to :func:`osimflow.work._write_stub_eplusout_sql` which
        writes a SQLite database that opens cleanly and contains the
        ``TabularDataWithStrings`` + ``Zones`` fields
        ``extract_kpis`` reads.
        """
        import sqlite3

        stdout_path, stderr_path = log_paths
        with patch.dict(os.environ, {"OSIMFLOW_STUB_SIM": "1"}):
            result = run_openstudio_sim(
                modified_sim_package=sim_package,
                sample_id="0001",
                openstudio_version="3.11.0",
                out=out_dir,
                simulate_work_s=0.0,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
            )
        assert result == out_dir / "0001"
        assert (result / "eplusout.sql").is_file()
        # Round-trip through sqlite3 — the stub file must NOT be the old
        # ``-- placeholder sql`` literal (issue #1419 root cause).  The
        # new file is binary SQLite, so we read raw bytes rather than
        # going through UTF-8 text decoding.
        sql_path = result / "eplusout.sql"
        assert sql_path.read_bytes() != b"-- placeholder sql"
        assert sql_path.read_bytes().startswith(b"SQLite format 3")
        conn = sqlite3.connect(str(sql_path))
        try:
            cur = conn.cursor()
            cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = {row[0] for row in cur.fetchall()}
            assert "TabularDataWithStrings" in tables
            assert "Zones" in tables
        finally:
            conn.close()
        assert (result / "eplusout.err").read_text() == ""

    def test_stub_writes_stdout_stderr_logs(
        self, sim_package: Path, out_dir: Path, log_paths: tuple[Path, Path]
    ) -> None:
        stdout_path, stderr_path = log_paths
        with patch.dict(os.environ, {"OSIMFLOW_STUB_SIM": "1"}):
            run_openstudio_sim(
                modified_sim_package=sim_package,
                sample_id="0002",
                openstudio_version="3.11.0",
                out=out_dir,
                simulate_work_s=0.0,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
            )
        assert stdout_path.is_file()

    def test_no_cli_falls_back_to_stub(
        self, sim_package: Path, out_dir: Path, log_paths: tuple[Path, Path]
    ) -> None:
        stdout_path, stderr_path = log_paths
        # When CLI is not available and stub mode is enabled, the stub subprocess runs.
        with (
            patch.dict(os.environ, {"OSIMFLOW_STUB_SIM": "1"}, clear=True),
            patch("osimflow.work._is_openstudio_available", return_value=False),
        ):
            result = run_openstudio_sim(
                modified_sim_package=sim_package,
                sample_id="0001",
                openstudio_version="3.11.0",
                out=out_dir,
                simulate_work_s=0.0,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
            )
        assert (result / "eplusout.sql").is_file()

    def test_default_log_paths_when_none(self, sim_package: Path, out_dir: Path) -> None:
        with patch.dict(os.environ, {"OSIMFLOW_STUB_SIM": "1"}):
            result = run_openstudio_sim(
                modified_sim_package=sim_package,
                sample_id="0003",
                openstudio_version="3.11.0",
                out=out_dir,
                simulate_work_s=0.0,
            )
        assert (result / "stdout.log").is_file()
        assert (result / "stderr.log").is_file()

    def test_stub_subprocess_error_propagates(
        self, sim_package: Path, out_dir: Path, log_paths: tuple[Path, Path]
    ) -> None:
        stdout_path, stderr_path = log_paths
        with (
            patch.dict(os.environ, {"OSIMFLOW_STUB_SIM": "1"}),
            patch("osimflow.work.run_subprocess") as mock_run,
        ):
            mock_run.side_effect = subprocess.SubprocessError("stub failed")
            with pytest.raises(subprocess.SubprocessError, match="stub failed"):
                run_openstudio_sim(
                    modified_sim_package=sim_package,
                    sample_id="0004",
                    openstudio_version="3.11.0",
                    out=out_dir,
                    simulate_work_s=0.0,
                    stdout_path=stdout_path,
                    stderr_path=stderr_path,
                )


# ===========================================================================
# run_openstudio_sim — real CLI path
# ===========================================================================
class TestRunOpenstudioSimRealCli:
    def test_real_cli_invoked(
        self, sim_package: Path, out_dir: Path, log_paths: tuple[Path, Path]
    ) -> None:
        stdout_path, stderr_path = log_paths
        with (
            patch.dict(os.environ, _env_without_stub(), clear=True),
            patch("osimflow.work._is_openstudio_available", return_value=True),
            patch("osimflow.work._get_openstudio_cmd", return_value="openstudio.cli"),
            patch("osimflow.work.run_subprocess") as mock_run,
        ):
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            run_openstudio_sim(
                modified_sim_package=sim_package,
                sample_id="0001",
                openstudio_version="3.11.0",
                out=out_dir,
                simulate_work_s=0.0,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
            )
        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "openstudio.cli"
        assert "run" in cmd
        assert "-w" in cmd

    def test_real_cli_timeout_passed_to_subprocess(
        self, sim_package: Path, out_dir: Path, log_paths: tuple[Path, Path]
    ) -> None:
        """timeout_s is forwarded to run_subprocess so a wedged CLI cannot hang (#1109)."""
        stdout_path, stderr_path = log_paths
        with (
            patch.dict(os.environ, _env_without_stub(), clear=True),
            patch("osimflow.work._is_openstudio_available", return_value=True),
            patch("osimflow.work._get_openstudio_cmd", return_value="openstudio.cli"),
            patch("osimflow.work.run_subprocess") as mock_run,
        ):
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            run_openstudio_sim(
                modified_sim_package=sim_package,
                sample_id="0001",
                openstudio_version="3.11.0",
                out=out_dir,
                simulate_work_s=0.0,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                timeout_s=123.5,
            )
        assert mock_run.call_args[1]["timeout"] == 123.5

    def test_skips_when_eplusout_sql_exists(
        self, sim_package: Path, out_dir: Path, log_paths: tuple[Path, Path]
    ) -> None:
        """When eplusout.sql already exists (simulation ran in apply_parameters), skip re-run."""
        stdout_path, stderr_path = log_paths
        sim_out = out_dir / "0001"
        sim_out.mkdir(parents=True, exist_ok=True)
        (sim_out / "eplusout.sql").write_text("-- already simulated")
        with (
            patch.dict(os.environ, _env_without_stub(), clear=True),
            patch("osimflow.work._is_openstudio_available", return_value=True),
            patch("osimflow.work.run_subprocess") as mock_run,
        ):
            result = run_openstudio_sim(
                modified_sim_package=sim_package,
                sample_id="0001",
                openstudio_version="3.11.0",
                out=out_dir,
                simulate_work_s=0.0,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
            )
        mock_run.assert_not_called()
        assert result == sim_out
        assert (sim_out / "eplusout.sql").read_text() == "-- already simulated"

    def test_no_placeholder_sql_on_real_cli(
        self, sim_package: Path, out_dir: Path, log_paths: tuple[Path, Path]
    ) -> None:
        stdout_path, stderr_path = log_paths
        with (
            patch.dict(os.environ, _env_without_stub(), clear=True),
            patch("osimflow.work._is_openstudio_available", return_value=True),
            patch("osimflow.work.run_subprocess") as mock_run,
        ):
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            result = run_openstudio_sim(
                modified_sim_package=sim_package,
                sample_id="0001",
                openstudio_version="3.11.0",
                out=out_dir,
                simulate_work_s=0.0,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
            )
        assert not (result / "eplusout.sql").exists() or (
            (result / "eplusout.sql").read_text() != "-- placeholder sql"
        )

    def test_copies_nested_package_run_sql_without_rerun(
        self, sim_package: Path, out_dir: Path, log_paths: tuple[Path, Path]
    ) -> None:
        """Copies SQL from nested run/**/eplusout.sql produced by apply step."""
        stdout_path, stderr_path = log_paths
        nested_run = sim_package / "run" / "008_measure" / "output" / "SR1" / "run"
        nested_run.mkdir(parents=True, exist_ok=True)
        (nested_run / "eplusout.sql").write_text("-- nested sql")
        (nested_run / "eplusout.err").write_text("nested err")
        with (
            patch.dict(os.environ, _env_without_stub(), clear=True),
            patch("osimflow.work._is_openstudio_available", return_value=True),
            patch("osimflow.work.run_subprocess") as mock_run,
        ):
            result = run_openstudio_sim(
                modified_sim_package=sim_package,
                sample_id="0005",
                openstudio_version="3.11.0",
                out=out_dir,
                simulate_work_s=0.0,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
            )
        mock_run.assert_not_called()
        assert result == out_dir / "0005"
        assert (result / "eplusout.sql").read_text() == "-- nested sql"
        assert (result / "eplusout.err").read_text() == "nested err"

    def test_missing_workflow_osw_raises(
        self, tmp_path: Path, out_dir: Path, log_paths: tuple[Path, Path]
    ) -> None:
        pkg = tmp_path / "no_osw"
        pkg.mkdir()
        (pkg / "model.osm").write_text("{}")
        stdout_path, stderr_path = log_paths
        with (
            patch.dict(os.environ, _env_without_stub(), clear=True),
            patch("osimflow.work._is_openstudio_available", return_value=True),
        ):
            with pytest.raises(RuntimeError, match="workflow.osw"):
                run_openstudio_sim(
                    modified_sim_package=pkg,
                    sample_id="0001",
                    openstudio_version="3.11.0",
                    out=out_dir,
                    simulate_work_s=0.0,
                    stdout_path=stdout_path,
                    stderr_path=stderr_path,
                )

    def test_real_cli_propagates_subprocess_error(
        self, sim_package: Path, out_dir: Path, log_paths: tuple[Path, Path]
    ) -> None:
        stdout_path, stderr_path = log_paths
        with (
            patch.dict(os.environ, _env_without_stub(), clear=True),
            patch("osimflow.work._is_openstudio_available", return_value=True),
            patch("osimflow.work.run_subprocess") as mock_run,
        ):
            mock_run.side_effect = subprocess.CalledProcessError(
                returncode=1, cmd=["openstudio.cli"], stderr="fail"
            )
            with pytest.raises(subprocess.CalledProcessError):
                run_openstudio_sim(
                    modified_sim_package=sim_package,
                    sample_id="0001",
                    openstudio_version="3.11.0",
                    out=out_dir,
                    simulate_work_s=0.0,
                    stdout_path=stdout_path,
                    stderr_path=stderr_path,
                )


# ===========================================================================
# _run_real_openstudio (direct call)
# ===========================================================================
class TestRunRealOpenstudioDirect:
    def test_passes_correct_cwd_and_paths(self, sim_package: Path, tmp_path: Path) -> None:
        sim_out = tmp_path / "sim_out" / "0001"
        sim_out.mkdir(parents=True)
        stdout_path = tmp_path / "stdout.log"
        stderr_path = tmp_path / "stderr.log"
        with patch("osimflow.work.run_subprocess") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            result = _run_real_openstudio(
                modified_sim_package=sim_package,
                sample_id="0001",
                sim_out=sim_out,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
            )
        assert result == sim_out
        mock_run.assert_called_once()
        kwargs = mock_run.call_args
        assert kwargs[1]["cwd"] == sim_package
        assert kwargs[1]["stdout_path"] == stdout_path
        assert kwargs[1]["stderr_path"] == stderr_path

    def test_missing_osw_raises_runtime_error(self, tmp_path: Path) -> None:
        pkg = tmp_path / "empty_pkg"
        pkg.mkdir()
        sim_out = tmp_path / "sim_out"
        sim_out.mkdir()
        with pytest.raises(RuntimeError, match="workflow.osw"):
            _run_real_openstudio(
                modified_sim_package=pkg,
                sample_id="0001",
                sim_out=sim_out,
                stdout_path=tmp_path / "stdout.log",
                stderr_path=tmp_path / "stderr.log",
            )

    def test_subprocess_error_propagates(self, sim_package: Path, tmp_path: Path) -> None:
        sim_out = tmp_path / "sim_out"
        sim_out.mkdir()
        with patch("osimflow.work.run_subprocess") as mock_run:
            mock_run.side_effect = subprocess.CalledProcessError(
                returncode=2, cmd=["openstudio.cli"], stderr="crash"
            )
            with pytest.raises(subprocess.CalledProcessError):
                _run_real_openstudio(
                    modified_sim_package=sim_package,
                    sample_id="0001",
                    sim_out=sim_out,
                    stdout_path=tmp_path / "stdout.log",
                    stderr_path=tmp_path / "stderr.log",
                )


# ===========================================================================
# extract_kpis
# ===========================================================================
def _make_minimal_eplus_sql(path: Path) -> Path:
    """Create a minimal eplusout.sql with a single EUI tabular row.

    Used by the in-process extract_kpis tests (issue #1015).  Matches
    the schema used in tests/unit/test_extract_kpis.py.
    """
    import sqlite3

    conn = sqlite3.connect(str(path))
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE TabularDataWithStrings (
            ReportName TEXT,
            ReportForString TEXT,
            TableName TEXT,
            RowName TEXT,
            ColumnName TEXT,
            Units TEXT,
            Value TEXT
        )
    """
    )
    cur.execute(
        "INSERT INTO TabularDataWithStrings VALUES (?,?,?,?,?,?,?)",
        (
            "AnnualBuildingUtilityPerformanceSummary",
            "Entire Facility",
            "Site and Source Energy",
            "Total Site Energy",
            "Energy Per Total Building Area",
            "MJ/m2",
            "360.0",
        ),
    )
    cur.execute(
        "INSERT INTO TabularDataWithStrings VALUES (?,?,?,?,?,?,?)",
        (
            "AnnualBuildingUtilityPerformanceSummary",
            "Entire Facility",
            "Site and Source Energy",
            "Total Site Energy",
            "Total Energy",
            "MJ",
            "36000.0",
        ),
    )
    cur.execute("CREATE TABLE Zones (ZoneIndex INTEGER, Floor_Area REAL)")
    cur.execute("INSERT INTO Zones VALUES (0, 100.0)")
    conn.commit()
    conn.close()
    return path


class TestExtractKpis:
    def test_returns_kpi_json_path(self, tmp_path: Path) -> None:
        sim_dir = tmp_path / "sim"
        sim_dir.mkdir()
        out = tmp_path / "kpi_out"
        with patch("osimflow._work_scripts.extract_kpis.run_extract_kpis") as mock_run_extract:
            mock_run_extract.return_value = out / "kpi_0001.json"
            result = extract_kpis(sim_dir, "0001", out)
        assert result == out / "kpi_0001.json"
        assert out.is_dir()

    def test_in_process_call_uses_run_extract_kpis(self, tmp_path: Path) -> None:
        """Issue #1015: _extract_kpis_impl must delegate to run_extract_kpis
        (in-process), not subprocess.run.
        """
        sim_dir = tmp_path / "sim"
        sim_dir.mkdir()
        out = tmp_path / "kpi_out"
        with patch("osimflow._work_scripts.extract_kpis.run_extract_kpis") as mock_run_extract:
            mock_run_extract.return_value = out / "kpi_0042.json"
            extract_kpis(sim_dir, "0042", out)
        mock_run_extract.assert_called_once()
        kwargs = mock_run_extract.call_args.kwargs
        assert kwargs["simulation_dir"] == sim_dir
        assert kwargs["sample_id"] == "0042"
        assert kwargs["out_path"] == out / "kpi_0042.json"
        assert kwargs["openstudio_version"] is None

    def test_does_not_spawn_subprocess(self, tmp_path: Path) -> None:
        """Issue #1015 acceptance: no subprocess.run is invoked when the
        default extract_kpis is called from the in-process Campaign path.
        """
        sim_dir = tmp_path / "sim"
        sim_dir.mkdir()
        out = tmp_path / "kpi_out"
        with patch("osimflow.work.subprocess.run") as mock_subprocess:
            with patch("osimflow._work_scripts.extract_kpis.run_extract_kpis") as mock_run_extract:
                mock_run_extract.return_value = out / "kpi_0001.json"
                extract_kpis(sim_dir, "0001", out)
        mock_subprocess.assert_not_called()

    def test_creates_out_dir(self, tmp_path: Path) -> None:
        sim_dir = tmp_path / "sim"
        sim_dir.mkdir()
        out = tmp_path / "deep" / "nested"
        with patch("osimflow._work_scripts.extract_kpis.run_extract_kpis") as mock_run_extract:
            mock_run_extract.return_value = out / "kpi_0001.json"
            extract_kpis(sim_dir, "0001", out)
        assert out.is_dir()

    def test_in_process_with_real_sql(self, tmp_path: Path) -> None:
        """End-to-end: in-process extraction of a real eplusout.sql
        produces a valid KPI JSON without spawning a subprocess.
        """
        sim_dir = tmp_path / "sim"
        sim_dir.mkdir()
        _make_minimal_eplus_sql(sim_dir / "eplusout.sql")
        out = tmp_path / "kpi_out"

        with patch("osimflow.work.subprocess.run") as mock_subprocess:
            result = extract_kpis(sim_dir, "0001", out)

        assert result == out / "kpi_0001.json"
        assert result.is_file()
        mock_subprocess.assert_not_called()
        payload = json.loads(result.read_text())
        assert payload["sample_id"] == "0001"
        assert "kpis" in payload
        assert "quality" in payload
        assert payload["kpis"]["eui_kwh_m2_yr"] == pytest.approx(100.0, abs=0.01)

    def test_perf_in_process_under_one_second_per_hundred(self, tmp_path: Path) -> None:
        """Issue #1015 acceptance smoke: 100 in-process KPI extractions
        must complete in well under what 100 subprocess startups would
        take (~15-30 s on a typical host).  We assert < 5 s here to
        leave a wide margin on slow CI but still catch a regression to
        the subprocess path.
        """
        import time

        from osimflow._work_scripts.extract_kpis import run_extract_kpis

        sim_dir = tmp_path / "sim"
        sim_dir.mkdir()
        _make_minimal_eplus_sql(sim_dir / "eplusout.sql")
        out = tmp_path / "kpi_out"
        out.mkdir()

        start = time.perf_counter()
        for i in range(100):
            sample_id = f"{i:04d}"
            run_extract_kpis(
                simulation_dir=sim_dir,
                sample_id=sample_id,
                out_path=out / f"kpi_{sample_id}.json",
            )
        elapsed = time.perf_counter() - start

        assert elapsed < 5.0, f"100 in-process extractions took {elapsed:.2f}s"
        assert (out / "kpi_0000.json").is_file()
        assert (out / "kpi_0099.json").is_file()


# ===========================================================================
# aggregate_results
# ===========================================================================
class TestAggregateResults:
    def test_returns_expected_paths(self, tmp_path: Path) -> None:
        kpi_files = [tmp_path / "kpi_0001.json"]
        kpi_files[0].write_text('{"sample_id": "0001"}')
        sim_dirs = [tmp_path / "sim" / "0001"]
        sim_dirs[0].mkdir(parents=True)
        out = tmp_path / "agg_out"
        with patch("osimflow.work.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            result = aggregate_results(kpi_files, sim_dirs, out)
        assert result["csv"] == out / "aggregated_results.csv"
        assert result["parquet"] == out / "aggregated_results.parquet"
        assert result["failed"] == out / "failed_simulations.csv"
        assert result["timeseries"] == out / "timeseries_aggregated.csv"

    def test_subprocess_receives_all_args(self, tmp_path: Path) -> None:
        kpi_files = [tmp_path / "k1.json", tmp_path / "k2.json"]
        for f in kpi_files:
            f.write_text("{}")
        sim_dirs = [tmp_path / "s1", tmp_path / "s2"]
        for d in sim_dirs:
            d.mkdir()
        out = tmp_path / "agg_out"
        with patch("osimflow.work.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            aggregate_results(kpi_files, sim_dirs, out)
        cmd = mock_run.call_args[0][0]
        assert "--kpis" in cmd
        assert str(kpi_files[0]) in cmd
        assert str(kpi_files[1]) in cmd
        assert "--simulation_dirs" in cmd
        assert str(sim_dirs[0]) in cmd
        assert "--ts_resolution" in cmd
        assert "monthly" in cmd

    def test_baseline_sample_id_forwarded(self, tmp_path: Path) -> None:
        out = tmp_path / "agg_out"
        with patch("osimflow.work.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            aggregate_results([], [], out, baseline_sample_id="0001")
        cmd = mock_run.call_args[0][0]
        assert "--baseline_sample_id" in cmd
        assert "0001" in cmd

    def test_ts_resolution_forwarded(self, tmp_path: Path) -> None:
        out = tmp_path / "agg_out"
        with patch("osimflow.work.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            aggregate_results([], [], out, ts_resolution="hourly")
        cmd = mock_run.call_args[0][0]
        assert "hourly" in cmd

    def test_raises_on_subprocess_failure(self, tmp_path: Path) -> None:
        out = tmp_path / "agg_out"
        with patch("osimflow.work.subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.CalledProcessError(
                returncode=1, cmd=[], stderr="agg error"
            )
            with pytest.raises(RuntimeError, match="aggregate_results failed"):
                aggregate_results([], [], out)


# ===========================================================================
# generate_plots
# ===========================================================================
class TestGeneratePlots:
    def _setup_plot_files(self, tmp_path: Path) -> tuple[Path, Path, Path]:
        csv_path = tmp_path / "agg.csv"
        csv_path.write_text("sample_id,eui\n0001,100\n")
        failed_path = tmp_path / "fail.csv"
        failed_path.write_text("sample_id,error\n")
        out = tmp_path / "plots"
        return csv_path, failed_path, out

    def test_returns_plot_files(self, tmp_path: Path) -> None:
        csv_path, failed_path, out = self._setup_plot_files(tmp_path)
        with patch("osimflow.work.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            out.mkdir(parents=True, exist_ok=True)
            (out / "eui_histogram.png").write_text("png")
            (out / "scatter.pdf").write_text("pdf")
            result = generate_plots(csv_path, failed_path, out)
        assert out.is_dir()
        assert all(isinstance(p, Path) for p in result)

    def test_baseline_forwarded(self, tmp_path: Path) -> None:
        csv_path, failed_path, out = self._setup_plot_files(tmp_path)
        with patch("osimflow.work.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            generate_plots(csv_path, failed_path, out, baseline_sample_id="0001")
        cmd = mock_run.call_args[0][0]
        assert "--baseline_sample_id" in cmd
        assert "0001" in cmd

    def test_pareto_dir_forwarded(self, tmp_path: Path) -> None:
        csv_path, failed_path, out = self._setup_plot_files(tmp_path)
        pareto = tmp_path / "pareto"
        pareto.mkdir()
        with patch("osimflow.work.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            generate_plots(csv_path, failed_path, out, pareto_dir=pareto)
        cmd = mock_run.call_args[0][0]
        assert "--pareto_dir" in cmd
        assert str(pareto) in cmd

    def test_raises_on_subprocess_failure(self, tmp_path: Path) -> None:
        csv_path, failed_path, out = self._setup_plot_files(tmp_path)
        with patch("osimflow.work.subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.CalledProcessError(
                returncode=1, cmd=[], stderr="plot error"
            )
            with pytest.raises(RuntimeError, match="generate_plots failed"):
                generate_plots(csv_path, failed_path, out)

    def test_empty_result_when_no_files(self, tmp_path: Path) -> None:
        csv_path, failed_path, out = self._setup_plot_files(tmp_path)
        with patch("osimflow.work.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            result = generate_plots(csv_path, failed_path, out)
        assert result == []


# ===========================================================================
# preflight_run_model
# ===========================================================================
class TestPreflightRunModel:
    def test_stub_mode_passes(self, tmp_path: Path) -> None:
        template = tmp_path / "template"
        template.mkdir()
        (template / "model.osm").write_text("{}")
        with (
            patch.dict(os.environ, {"OSIMFLOW_STUB_SIM": "1"}),
            patch("osimflow.work._is_openstudio_available", return_value=False),
        ):
            preflight_run_model(template, "3.11.0")

    def test_real_cli_passes(self, tmp_path: Path) -> None:
        template = tmp_path / "template"
        template.mkdir()
        (template / "workflow.osw").write_text("{}")
        (template / "model.osm").write_text("{}")
        with (
            patch.dict(os.environ, _env_without_stub(), clear=True),
            patch("osimflow.work._is_openstudio_available", return_value=True),
            patch("osimflow.work.subprocess.run") as mock_run,
        ):
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            preflight_run_model(template, "3.11.0")

    def test_real_cli_severe_error_raises(self, tmp_path: Path) -> None:
        template = tmp_path / "template"
        template.mkdir()
        (template / "workflow.osw").write_text("{}")
        (template / "model.osm").write_text("{}")
        with (
            patch.dict(os.environ, _env_without_stub(), clear=True),
            patch("osimflow.work._is_openstudio_available", return_value=True),
            patch("osimflow.work.subprocess.run") as mock_run,
        ):
            mock_run.return_value = subprocess.CompletedProcess(
                args=[],
                returncode=1,
                stdout="",
                stderr="   ** Severe  ** Zone 'X' not found\n",
            )
            with pytest.raises(SevereEnergyPlusError, match="Preflight simulation FAILED"):
                preflight_run_model(template, "3.11.0")

    def test_real_cli_missing_osw_raises(self, tmp_path: Path) -> None:
        template = tmp_path / "template"
        template.mkdir()
        (template / "model.osm").write_text("{}")
        with (
            patch.dict(os.environ, _env_without_stub(), clear=True),
            patch("osimflow.work._is_openstudio_available", return_value=True),
        ):
            with pytest.raises(RuntimeError, match="workflow.osw"):
                preflight_run_model(template, "3.11.0")

    def test_real_cli_nonzero_exit_no_severe(self, tmp_path: Path) -> None:
        template = tmp_path / "template"
        template.mkdir()
        (template / "workflow.osw").write_text("{}")
        (template / "model.osm").write_text("{}")
        with (
            patch.dict(os.environ, _env_without_stub(), clear=True),
            patch("osimflow.work._is_openstudio_available", return_value=True),
            patch("osimflow.work.subprocess.run") as mock_run,
        ):
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=1, stdout="some error", stderr=""
            )
            with pytest.raises(SevereEnergyPlusError):
                preflight_run_model(template, "3.11.0")


# ===========================================================================
# _extract_severe_error
# ===========================================================================
class TestExtractSevereError:
    def test_extracts_first_severe_line(self) -> None:
        output = "line1\n   1 ** Severe  ** Zone not found\nline3"
        result = _extract_severe_error(output)
        assert "Zone not found" in result

    def test_returns_empty_when_no_severe(self) -> None:
        output = "just some normal output\nno errors here"
        assert _extract_severe_error(output) == ""

    def test_empty_input(self) -> None:
        assert _extract_severe_error("") == ""

    def test_multiple_severe_returns_first(self) -> None:
        output = "   1 ** Severe  ** First error\n   2 ** Severe  ** Second error\n"
        result = _extract_severe_error(output)
        assert "First error" in result

    def test_case_insensitive(self) -> None:
        output = "   1 ** SEVERE  ** Something bad\n"
        result = _extract_severe_error(output)
        assert "Something bad" in result

    def test_extracts_single_asterisk_severe_line(self) -> None:
        """Regression test for issue #1091.

        EnergyPlus sometimes uses a single ``*`` (not ``**``) for severe
        errors. The regex ``r"^\\s*(?:\\d+\\s+)?\\*+\\s*Severe"`` uses
        ``\\*+`` (one-or-more) so it should catch single-asterisk lines.
        """
        output = "line1\n   * Severe  ** Zone not found\nline3"
        result = _extract_severe_error(output)
        assert "Zone not found" in result

    def test_extracts_single_asterisk_no_leading_space(self) -> None:
        """Issue #1091: single-asterisk severe line without leading spaces."""
        output = "line1\n* Severe  ** Zone not found\nline3"
        result = _extract_severe_error(output)
        assert "Zone not found" in result

    def test_extracts_numbered_single_asterisk_severe(self) -> None:
        """Issue #1091: single-asterisk with number prefix."""
        output = "line1\n   1 * Severe  ** Zone not found\nline3"
        result = _extract_severe_error(output)
        assert "Zone not found" in result


# ===========================================================================
# _find_workflow_osw
# ===========================================================================
class TestFindWorkflowOsw:
    def test_finds_root_osw(self, tmp_path: Path) -> None:
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        (pkg / "workflow.osw").write_text("{}")
        result = _find_workflow_osw(pkg)
        assert result is not None
        assert result.name == "workflow.osw"

    def test_returns_none_when_no_osw(self, tmp_path: Path) -> None:
        pkg = tmp_path / "empty"
        pkg.mkdir()
        assert _find_workflow_osw(pkg) is None

    def test_finds_nested_osw(self, tmp_path: Path) -> None:
        pkg = tmp_path / "pkg"
        nested = pkg / "sub"
        nested.mkdir(parents=True)
        (nested / "workflow.osw").write_text("{}")
        result = _find_workflow_osw(pkg)
        assert result is not None

    def test_prefers_root_over_nested(self, tmp_path: Path) -> None:
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        (pkg / "workflow.osw").write_text('{"root": true}')
        nested = pkg / "sub"
        nested.mkdir()
        (nested / "workflow.osw").write_text('{"nested": true}')
        result = _find_workflow_osw(pkg)
        assert result is not None
        assert result.read_text() == '{"root": true}'


# ===========================================================================
# _is_openstudio_available / _is_stub_mode
# ===========================================================================
class TestIsOpenstudioAvailable:
    def test_true(self) -> None:
        with patch("shutil.which", return_value="/usr/bin/openstudio.cli"):
            assert _is_openstudio_available() is True

    def test_false(self) -> None:
        with patch("shutil.which", return_value=None):
            assert _is_openstudio_available() is False


class TestIsStubMode:
    def test_true(self) -> None:
        with patch.dict(os.environ, {"OSIMFLOW_STUB_SIM": "1"}):
            assert _is_stub_mode() is True

    def test_false_unset(self) -> None:
        with patch.dict(os.environ, _env_without_stub(), clear=True):
            assert _is_stub_mode() is False

    def test_false_not_one(self) -> None:
        with patch.dict(os.environ, {"OSIMFLOW_STUB_SIM": "0"}):
            assert _is_stub_mode() is False


# ---------------------------------------------------------------------------
# Worker direct-to-storage push (issue #625)
# ---------------------------------------------------------------------------
class _RecordingStorage(ResultStorage):
    """Fake ResultStorage that records every upload in call order."""

    name = "fake"

    def __init__(self) -> None:
        self.uploads: list[str] = []
        self.contents: dict[str, dict[str, object]] = {}

    def upload_file(self, local_path: Path, remote_path: str) -> None:
        self.uploads.append(remote_path)
        try:
            self.contents[remote_path] = json.loads(Path(local_path).read_text())
        except (OSError, ValueError):
            # Non-JSON payloads (e.g. eplusout.sql) are recorded as keys only.
            pass

    def download_file(self, remote_path: str, local_path: Path) -> None:
        raise NotImplementedError

    def list_results(self, prefix: str = "") -> list[str]:
        return [k for k in self.uploads if k.startswith(prefix)]

    async def upload_file_async(self, local_path: Path, remote_path: str) -> None:
        return asyncio.to_thread(self.upload_file, local_path, remote_path)

    async def download_file_async(self, remote_path: str, local_path: Path) -> None:
        return asyncio.to_thread(self.download_file, remote_path, local_path)

    async def list_results_async(self, prefix: str = "") -> list[str]:
        return asyncio.to_thread(self.list_results, prefix)


class TestPublishKpiResults:
    """Worker direct-to-storage push: kpis.json + atomic _manifest.json (#625)."""

    def test_publishes_kpis_then_manifest_with_contract_fields(self, tmp_path: Path) -> None:
        storage = _RecordingStorage()
        sim_dir = tmp_path / "sim" / "0001"
        sim_dir.mkdir(parents=True)
        kpi_path = tmp_path / "kpis" / "kpi_0001.json"
        kpi_path.parent.mkdir(parents=True)
        kpi_path.write_text(json.dumps({"sample_id": "0001", "kpis": {"eui": 100.0}}))

        manifest_key = publish_kpi_results(
            storage=storage,
            campaign_id="camp-1",
            sample_id="0001",
            index=2,
            simulation_dir=sim_dir,
            kpi_path=kpi_path,
            exit_code=0,
            status="completed",
            archive_intermediates=False,
        )

        kpis_key = "camp-1/samples/0001/kpis.json"
        assert manifest_key == "camp-1/samples/0001/_manifest.json"
        # Both keys present.
        assert kpis_key in storage.uploads
        assert manifest_key in storage.uploads
        # Manifest written AFTER kpis.json (strict ordering).
        assert storage.uploads.index(kpis_key) < storage.uploads.index(manifest_key)
        # Manifest carries all §3.1 fields with correct values.
        manifest = storage.contents[manifest_key]
        for field in MANIFEST_FIELDS:
            assert field in manifest, f"manifest missing field: {field}"
        assert manifest["sample_id"] == "0001"
        assert manifest["index"] == 2
        assert manifest["status"] == "completed"
        assert manifest["kpis_key"] == kpis_key
        assert manifest["exit_code"] == 0
        assert manifest["first_severe_error"] is None
        assert isinstance(manifest["finished_at"], float)

    def test_local_storage_is_a_noop(self, tmp_path: Path) -> None:
        from osimflow.storage import LocalStorage

        storage = LocalStorage()
        kpi_path = tmp_path / "kpi_0001.json"
        kpi_path.write_text("{}")

        result = publish_kpi_results(
            storage=storage,
            campaign_id="camp-1",
            sample_id="0001",
            index=0,
            simulation_dir=tmp_path,
            kpi_path=kpi_path,
            exit_code=0,
            status="completed",
            archive_intermediates=False,
        )
        # Local path unchanged: no key returned, no uploads, no manifest file.
        assert result is None
        assert not (tmp_path / "_manifest.json").exists()

    def test_failure_records_severe_error_from_eplusout_err(self, tmp_path: Path) -> None:
        storage = _RecordingStorage()
        sim_dir = tmp_path / "sim" / "0002"
        sim_dir.mkdir(parents=True)
        (sim_dir / "eplusout.err").write_text(
            "   Program Version,EnergyPlus\n"
            "  * Severe ~  HVAC sizing failed in zone Z\n"
            "  * Severe ~  Another problem\n"
        )
        # Failed extraction produced no kpi file.
        manifest_key = publish_kpi_results(
            storage=storage,
            campaign_id="camp-1",
            sample_id="0002",
            index=1,
            simulation_dir=sim_dir,
            kpi_path=None,
            exit_code=1,
            status="failed",
            archive_intermediates=False,
        )
        assert manifest_key == "camp-1/samples/0002/_manifest.json"
        manifest = storage.contents[manifest_key]
        assert manifest["status"] == "failed"
        assert manifest["exit_code"] == 1
        assert manifest["kpis_key"] is None
        # first_severe_error = FIRST '  * Severe' line (AGENTS.md §8 gotcha #4).
        assert manifest["first_severe_error"] == "* Severe ~  HVAC sizing failed in zone Z"
        # No kpis.json uploaded on failure (no file existed).
        assert "camp-1/samples/0002/kpis.json" not in storage.uploads

    def test_archive_intermediates_uploads_sql_never_err_or_log(self, tmp_path: Path) -> None:
        storage = _RecordingStorage()
        sim_dir = tmp_path / "sim" / "0003"
        sim_dir.mkdir(parents=True)
        (sim_dir / "eplusout.sql").write_text("SQLITE-HEADER")
        (sim_dir / "eplusout.err").write_text("  * Severe ~  boom")
        (sim_dir / "eplusout.log").write_text("verbose log " * 1000)
        kpi_path = tmp_path / "kpi_0003.json"
        kpi_path.write_text(json.dumps({"sample_id": "0003", "kpis": {}}))

        publish_kpi_results(
            storage=storage,
            campaign_id="camp-1",
            sample_id="0003",
            index=0,
            simulation_dir=sim_dir,
            kpi_path=kpi_path,
            exit_code=0,
            status="completed",
            archive_intermediates=True,
        )
        keys = set(storage.uploads)
        assert "camp-1/samples/0003/eplusout.sql" in keys
        # Size guard (AGENTS.md §8 gotcha #1/#8): never upload .err / .log.
        assert not any(k.endswith("eplusout.err") for k in keys)
        assert not any(k.endswith("eplusout.log") for k in keys)

    def test_coordinator_report_is_best_effort(self, tmp_path: Path) -> None:
        storage = _RecordingStorage()
        sim_dir = tmp_path / "sim" / "0004"
        sim_dir.mkdir(parents=True)
        kpi_path = tmp_path / "kpi_0004.json"
        kpi_path.write_text("{}")

        seen: dict[str, object] = {}

        def _fake_patch(
            url: str,
            body: bytes,
            headers: dict[str, str],
            params: dict[str, str],
            timeout_s: float,
        ) -> None:
            seen["url"] = url
            seen["body"] = json.loads(body)
            seen["params"] = params

        with patch("osimflow.manifest._do_patch", side_effect=_fake_patch):
            publish_kpi_results(
                storage=storage,
                campaign_id="camp-9",
                sample_id="0004",
                index=0,
                simulation_dir=sim_dir,
                kpi_path=kpi_path,
                exit_code=0,
                status="completed",
                archive_intermediates=False,
                coordinator_url="https://coordinator.example.com/",
            )
        assert seen["url"] == (
            "https://coordinator.example.com/api/v1/coordinator/campaigns/camp-9/status"
        )
        assert seen["params"] == {"status": "completed"}
        assert seen["body"]["sample_id"] == "0004"  # type: index

    def test_coordinator_report_swallows_network_errors(self, tmp_path: Path) -> None:
        """Telemetry must not break the worker when the Coordinator is down."""
        storage = _RecordingStorage()
        sim_dir = tmp_path / "sim" / "0005"
        sim_dir.mkdir(parents=True)
        kpi_path = tmp_path / "kpi_0005.json"
        kpi_path.write_text("{}")

        with patch("osimflow.manifest._do_patch", side_effect=ConnectionError("network down")):
            # Must NOT raise even though the Coordinator is unreachable.
            key = publish_kpi_results(
                storage=storage,
                campaign_id="camp-1",
                sample_id="0005",
                index=0,
                simulation_dir=sim_dir,
                kpi_path=kpi_path,
                exit_code=0,
                status="completed",
                archive_intermediates=False,
                coordinator_url="https://coordinator.example.com",
            )
        # Manifest still published locally to storage despite the report failure.
        assert key == "camp-1/samples/0005/_manifest.json"
