"""Unit tests for osimflow/work.py — per-step work functions.

Covers:
  * default_apply_parameters: subprocess invocation, param JSON, error propagation
  * run_openstudio_sim: stub mode, real CLI path, missing workflow.osw, log capture
  * extract_kpis: subprocess invocation and error propagation
  * aggregate_results: subprocess invocation, baseline, ts_resolution, error propagation
  * generate_plots: subprocess invocation, baseline, pareto_dir, error propagation
  * preflight_run_model: stub pass, real CLI pass, real CLI severe error, missing osw
  * _extract_severe_error: pattern matching
  * generate_lhs: subprocess invocation and error propagation
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

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
    def test_creates_out_dir_and_param_file(self, template_pkg: Path, out_dir: Path) -> None:
        params = {"heating_setpoint": 20.0, "cooling_setpoint": 25.0}
        with (
            patch.dict(os.environ, _env_without_stub(), clear=True),
            patch("osimflow.work._is_openstudio_available", return_value=False),
            patch("osimflow.work.subprocess.run") as mock_run,
        ):
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            result = default_apply_parameters(template_pkg, params, "0001", out_dir)

        assert result == out_dir / "0001"
        assert result.is_dir()
        param_file = out_dir / "0001.params.json"
        assert param_file.is_file()
        written = json.loads(param_file.read_text())
        assert written == {"cooling_setpoint": 25.0, "heating_setpoint": 20.0}

    def test_cli_invoked_when_available(self, template_pkg: Path, out_dir: Path) -> None:
        """When openstudio.cli is available, it is called instead of static patching (issue #248)."""
        pkg = template_pkg
        (pkg / "workflow.osw").write_text(json.dumps({"name": "test_workflow"}))
        params = {"wwr": 0.4}
        with (
            patch.dict(os.environ, _env_without_stub(), clear=True),
            patch("osimflow.work._is_openstudio_available", return_value=True),
            patch("osimflow.work._get_openstudio_cmd", return_value="openstudio.cli"),
            patch("osimflow.work.run_subprocess") as mock_run,
        ):
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            default_apply_parameters(pkg, params, "0042", out_dir)

        mock_run.assert_called_once()
        call_args = mock_run.call_args
        cmd = call_args[0][0]
        assert cmd[0] == "openstudio.cli"
        assert "run" in cmd
        assert "-w" in cmd

    def test_fallback_to_apply_params_script_when_cli_missing(
        self, template_pkg: Path, out_dir: Path
    ) -> None:
        """When CLI is unavailable, falls back to apply_params_to_model.py (backward compat)."""
        params = {"wwr": 0.4}
        with (
            patch.dict(os.environ, _env_without_stub(), clear=True),
            patch("osimflow.work._is_openstudio_available", return_value=False),
            patch("osimflow.work.subprocess.run") as mock_run,
        ):
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            default_apply_parameters(template_pkg, params, "0042", out_dir)

        mock_run.assert_called_once()
        call_args = mock_run.call_args
        cmd = call_args[0][0]
        assert cmd[0] == sys.executable
        assert "--template" in cmd
        assert "--sample_id" in cmd
        assert "0042" in cmd
        assert "--out" in cmd

    def test_raises_on_subprocess_failure(self, template_pkg: Path, out_dir: Path) -> None:
        with (
            patch.dict(os.environ, _env_without_stub(), clear=True),
            patch("osimflow.work._is_openstudio_available", return_value=False),
            patch("osimflow.work.subprocess.run") as mock_run,
        ):
            mock_run.side_effect = subprocess.CalledProcessError(
                returncode=1, cmd=[], stderr="apply_params error"
            )
            with pytest.raises(RuntimeError, match="apply_params failed"):
                default_apply_parameters(template_pkg, {"x": 1.0}, "0001", out_dir)

    def test_makes_parent_dirs(self, out_dir: Path, tmp_path: Path) -> None:
        template = tmp_path / "tmpl"
        template.mkdir()
        deep_out = out_dir / "nested" / "deep"
        with (
            patch.dict(os.environ, _env_without_stub(), clear=True),
            patch("osimflow.work._is_openstudio_available", return_value=False),
            patch("osimflow.work.subprocess.run") as mock_run,
        ):
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            result = default_apply_parameters(template, {"a": 1}, "0001", deep_out)
        assert result.is_dir()

    def test_cli_error_raises(self, template_pkg: Path, out_dir: Path) -> None:
        """When CLI is available but fails, RuntimeError is raised."""
        pkg = template_pkg
        (pkg / "workflow.osw").write_text(json.dumps({"name": "test_workflow"}))
        params = {"wwr": 0.4}
        with (
            patch.dict(os.environ, _env_without_stub(), clear=True),
            patch("osimflow.work._is_openstudio_available", return_value=True),
            patch("osimflow.work.run_subprocess") as mock_run,
        ):
            mock_run.side_effect = subprocess.SubprocessError("CLI failed")
            with pytest.raises(RuntimeError, match="apply_parameters failed"):
                default_apply_parameters(pkg, params, "0001", out_dir)


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
    def test_stub_writes_placeholder_sql(
        self, sim_package: Path, out_dir: Path, log_paths: tuple[Path, Path]
    ) -> None:
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
        assert (result / "eplusout.sql").read_text() == "-- placeholder sql"
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
        with (
            patch.dict(os.environ, _env_without_stub(), clear=True),
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
        assert kwargs[1]["cwd"] == sim_out
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
class TestExtractKpis:
    def test_returns_kpi_json_path(self, tmp_path: Path) -> None:
        sim_dir = tmp_path / "sim"
        sim_dir.mkdir()
        out = tmp_path / "kpi_out"
        with patch("osimflow.work.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            result = extract_kpis(sim_dir, "0001", out)
        assert result == out / "kpi_0001.json"
        assert out.is_dir()

    def test_subprocess_called_with_correct_args(self, tmp_path: Path) -> None:
        sim_dir = tmp_path / "sim"
        sim_dir.mkdir()
        out = tmp_path / "kpi_out"
        with patch("osimflow.work.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            extract_kpis(sim_dir, "0042", out)
        cmd = mock_run.call_args[0][0]
        assert "--simulation_dir" in cmd
        assert str(sim_dir) in cmd
        assert "--sample_id" in cmd
        assert "0042" in cmd
        assert "--out" in cmd

    def test_raises_on_subprocess_failure(self, tmp_path: Path) -> None:
        sim_dir = tmp_path / "sim"
        sim_dir.mkdir()
        out = tmp_path / "kpi_out"
        with patch("osimflow.work.subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.CalledProcessError(
                returncode=1, cmd=[], stderr="kpi error"
            )
            with pytest.raises(RuntimeError, match="extract_kpis failed"):
                extract_kpis(sim_dir, "0001", out)

    def test_creates_out_dir(self, tmp_path: Path) -> None:
        sim_dir = tmp_path / "sim"
        sim_dir.mkdir()
        out = tmp_path / "deep" / "nested"
        with patch("osimflow.work.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            extract_kpis(sim_dir, "0001", out)
        assert out.is_dir()


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
