"""Tests for the ``osimflow health`` CLI subcommand and health module (issue #411)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from osimflow.__main__ import _build_parser, _cmd_health, main
from osimflow.health import (
    CheckCategory,
    CheckResult,
    CheckStatus,
    HealthReport,
    format_results,
    get_exit_code,
    run_health_checks,
    to_json,
)

# ---------------------------------------------------------------------------
# CheckResult unit tests
# ---------------------------------------------------------------------------


class TestCheckResult:
    """Tests for the CheckResult dataclass."""

    def test_failed_property(self) -> None:
        """failed is True only for FAIL status."""
        assert CheckResult("x", CheckStatus.FAIL, CheckCategory.CRITICAL, "err").failed
        assert not CheckResult("x", CheckStatus.PASS, CheckCategory.CRITICAL, "ok").failed
        assert not CheckResult("x", CheckStatus.WARN, CheckCategory.INFORMATIONAL, "warn").failed

    def test_critical_property(self) -> None:
        """critical is True only for CRITICAL category."""
        assert CheckResult("x", CheckStatus.PASS, CheckCategory.CRITICAL, "ok").critical
        assert not CheckResult("x", CheckStatus.PASS, CheckCategory.INFORMATIONAL, "ok").critical

    def test_to_dict_keys(self) -> None:
        """to_dict returns expected keys."""
        r = CheckResult(
            "Test",
            CheckStatus.PASS,
            CheckCategory.CRITICAL,
            "msg",
            "detail",
        )
        d = r.to_dict()
        assert set(d.keys()) == {"name", "status", "category", "message", "detail"}
        assert d["status"] == "pass"
        assert d["category"] == "critical"


# ---------------------------------------------------------------------------
# HealthReport unit tests
# ---------------------------------------------------------------------------


class TestHealthReport:
    """Tests for the HealthReport aggregation."""

    def test_all_critical_pass_with_no_failures(self) -> None:
        """all_critical_pass is True when no critical check fails."""
        report = HealthReport(
            results=[
                CheckResult("a", CheckStatus.PASS, CheckCategory.CRITICAL, "ok"),
                CheckResult("b", CheckStatus.WARN, CheckCategory.INFORMATIONAL, "warn"),
            ]
        )
        assert report.all_critical_pass

    def test_all_critical_pass_false_on_critical_fail(self) -> None:
        """all_critical_pass is False when a critical check fails."""
        report = HealthReport(
            results=[
                CheckResult("a", CheckStatus.FAIL, CheckCategory.CRITICAL, "err"),
                CheckResult("b", CheckStatus.PASS, CheckCategory.INFORMATIONAL, "ok"),
            ]
        )
        assert not report.all_critical_pass

    def test_all_critical_pass_true_with_informational_fail(self) -> None:
        """Informational failures do not affect all_critical_pass."""
        report = HealthReport(
            results=[
                CheckResult("a", CheckStatus.PASS, CheckCategory.CRITICAL, "ok"),
                CheckResult("b", CheckStatus.FAIL, CheckCategory.INFORMATIONAL, "err"),
            ]
        )
        assert report.all_critical_pass

    def test_to_dict_summary_counts(self) -> None:
        """to_dict summary counts are correct."""
        report = HealthReport(
            results=[
                CheckResult("a", CheckStatus.PASS, CheckCategory.CRITICAL, "ok"),
                CheckResult("b", CheckStatus.FAIL, CheckCategory.CRITICAL, "err"),
                CheckResult("c", CheckStatus.WARN, CheckCategory.INFORMATIONAL, "warn"),
                CheckResult("d", CheckStatus.SKIP, CheckCategory.INFORMATIONAL, "skip"),
            ]
        )
        d = report.to_dict()
        assert d["summary"]["total"] == 4
        assert d["summary"]["passed"] == 1
        assert d["summary"]["failed"] == 1
        assert d["summary"]["warnings"] == 1
        assert d["summary"]["skipped"] == 1
        assert d["summary"]["critical_failures"] == 1
        assert d["summary"]["healthy"] is False

    def test_to_dict_summary_healthy(self) -> None:
        """to_dict healthy is True when no critical failures."""
        report = HealthReport(
            results=[
                CheckResult("a", CheckStatus.PASS, CheckCategory.CRITICAL, "ok"),
            ]
        )
        d = report.to_dict()
        assert d["summary"]["healthy"] is True


# ---------------------------------------------------------------------------
# run_health_checks integration tests
# ---------------------------------------------------------------------------


class TestRunHealthChecks:
    """Tests for run_health_checks with real system state."""

    def test_returns_report_with_results(self, tmp_path: Path) -> None:
        """run_health_checks returns a non-empty HealthReport."""
        report = run_health_checks(outdir=tmp_path, skip_network=True)
        assert isinstance(report, HealthReport)
        assert len(report.results) > 0

    def test_includes_critical_checks(self, tmp_path: Path) -> None:
        """Report includes Python Version and Core Packages checks."""
        report = run_health_checks(outdir=tmp_path, skip_network=True)
        names = [r.name for r in report.results]
        assert "Python Version" in names
        assert "Core Packages" in names
        assert "SQLite" in names
        assert "Write Permissions" in names

    def test_includes_informational_checks(self, tmp_path: Path) -> None:
        """Report includes optional packages and external tools."""
        report = run_health_checks(outdir=tmp_path, skip_network=True)
        names = [r.name for r in report.results]
        assert "Optional Packages" in names
        assert "Disk Space" in names
        # External tools
        assert "OpenStudio CLI" in names
        assert "Docker" in names
        assert "Podman" in names

    def test_network_skipped_when_requested(self, tmp_path: Path) -> None:
        """Network check is SKIPPED when skip_network=True."""
        report = run_health_checks(outdir=tmp_path, skip_network=True)
        net_results = [r for r in report.results if r.name == "Network Connectivity"]
        assert len(net_results) == 1
        assert net_results[0].status == CheckStatus.SKIP

    def test_python_version_passes(self, tmp_path: Path) -> None:
        """Python version check passes since tests run on 3.12+."""
        report = run_health_checks(outdir=tmp_path, skip_network=True)
        py_results = [r for r in report.results if r.name == "Python Version"]
        assert len(py_results) == 1
        assert py_results[0].status == CheckStatus.PASS

    def test_sqlite_passes(self, tmp_path: Path) -> None:
        """SQLite check passes on any working system."""
        report = run_health_checks(outdir=tmp_path, skip_network=True)
        sqlite_results = [r for r in report.results if r.name == "SQLite"]
        assert len(sqlite_results) == 1
        assert sqlite_results[0].status == CheckStatus.PASS

    def test_write_permissions_passes_on_tmpdir(self, tmp_path: Path) -> None:
        """Write permissions check passes on a writable tmpdir."""
        report = run_health_checks(outdir=tmp_path, skip_network=True)
        wp_results = [r for r in report.results if r.name == "Write Permissions"]
        assert len(wp_results) == 1
        assert wp_results[0].status == CheckStatus.PASS
        # Test file should be cleaned up
        assert not (tmp_path / ".osimflow_health_test").exists()

    def test_write_permissions_fails_on_readonly(self, tmp_path: Path) -> None:
        """Write permissions check fails on a read-only directory."""
        import os  # noqa: PLC0415

        ro_dir = tmp_path / "readonly"
        ro_dir.mkdir()
        # Make read-only if not root (CI may run as root).
        if os.geteuid() != 0:
            ro_dir.chmod(0o444)
            report = run_health_checks(outdir=ro_dir, skip_network=True)
            wp_results = [r for r in report.results if r.name == "Write Permissions"]
            assert wp_results[0].status == CheckStatus.FAIL
            # Cleanup
            ro_dir.chmod(0o755)

    def test_disk_space_includes_gb(self, tmp_path: Path) -> None:
        """Disk space message includes 'GB' units."""
        report = run_health_checks(outdir=tmp_path, skip_network=True)
        ds_results = [r for r in report.results if r.name == "Disk Space"]
        assert len(ds_results) == 1
        assert "GB" in ds_results[0].message


# ---------------------------------------------------------------------------
# Formatting tests
# ---------------------------------------------------------------------------


class TestFormatResults:
    """Tests for format_results human-readable output."""

    def test_output_contains_check_names(self) -> None:
        """Output includes check names."""
        report = HealthReport(
            results=[
                CheckResult("Test Check", CheckStatus.PASS, CheckCategory.CRITICAL, "ok"),
            ]
        )
        output = format_results(report, use_color=False)
        assert "Test Check" in output
        assert "ok" in output

    def test_output_contains_summary(self) -> None:
        """Output includes a summary line."""
        report = HealthReport(
            results=[
                CheckResult("a", CheckStatus.PASS, CheckCategory.CRITICAL, "ok"),
            ]
        )
        output = format_results(report, use_color=False)
        assert "Result:" in output
        assert "HEALTHY" in output

    def test_output_shows_unhealthy(self) -> None:
        """Output shows UNHEALTHY when a critical check fails."""
        report = HealthReport(
            results=[
                CheckResult("a", CheckStatus.FAIL, CheckCategory.CRITICAL, "err"),
            ]
        )
        output = format_results(report, use_color=False)
        assert "UNHEALTHY" in output

    def test_output_without_color(self) -> None:
        """Output without color uses [PASS]/[FAIL] brackets."""
        report = HealthReport(
            results=[
                CheckResult("a", CheckStatus.PASS, CheckCategory.CRITICAL, "ok"),
                CheckResult("b", CheckStatus.FAIL, CheckCategory.CRITICAL, "err"),
            ]
        )
        output = format_results(report, use_color=False)
        assert "[PASS]" in output
        assert "[FAIL]" in output

    def test_output_with_color(self) -> None:
        """Output with color uses emoji."""
        report = HealthReport(
            results=[
                CheckResult("a", CheckStatus.PASS, CheckCategory.CRITICAL, "ok"),
            ]
        )
        output = format_results(report, use_color=True)
        assert "\u2705" in output  # ✅


class TestToJson:
    """Tests for the JSON serialization."""

    def test_valid_json_output(self) -> None:
        """to_json produces valid parseable JSON."""
        report = HealthReport(
            results=[
                CheckResult("a", CheckStatus.PASS, CheckCategory.CRITICAL, "ok", "d"),
            ]
        )
        raw = to_json(report)
        data = json.loads(raw)
        assert "summary" in data
        assert "checks" in data
        assert data["checks"][0]["name"] == "a"

    def test_json_summary_is_correct(self) -> None:
        """JSON summary fields are correct."""
        report = HealthReport(
            results=[
                CheckResult("a", CheckStatus.PASS, CheckCategory.CRITICAL, "ok"),
                CheckResult("b", CheckStatus.FAIL, CheckCategory.CRITICAL, "err"),
            ]
        )
        raw = to_json(report)
        data = json.loads(raw)
        assert data["summary"]["passed"] == 1
        assert data["summary"]["failed"] == 1
        assert data["summary"]["healthy"] is False


class TestGetExitCode:
    """Tests for the exit code logic."""

    def test_exit_zero_when_healthy(self) -> None:
        """Exit code is 0 when all critical checks pass."""
        report = HealthReport(
            results=[
                CheckResult("a", CheckStatus.PASS, CheckCategory.CRITICAL, "ok"),
            ]
        )
        assert get_exit_code(report) == 0

    def test_exit_one_when_unhealthy(self) -> None:
        """Exit code is 1 when any critical check fails."""
        report = HealthReport(
            results=[
                CheckResult("a", CheckStatus.FAIL, CheckCategory.CRITICAL, "err"),
            ]
        )
        assert get_exit_code(report) == 1

    def test_exit_zero_with_informational_failure(self) -> None:
        """Exit code is 0 when only informational checks fail."""
        report = HealthReport(
            results=[
                CheckResult("a", CheckStatus.PASS, CheckCategory.CRITICAL, "ok"),
                CheckResult("b", CheckStatus.FAIL, CheckCategory.INFORMATIONAL, "err"),
            ]
        )
        assert get_exit_code(report) == 0


# ---------------------------------------------------------------------------
# CLI integration tests
# ---------------------------------------------------------------------------


class TestHealthCLI:
    """Tests for the health subcommand wiring."""

    def test_health_in_parser(self) -> None:
        """Health subcommand is registered in the parser."""
        parser = _build_parser()
        args = parser.parse_args(["health"])
        assert args.command == "health"

    def test_health_accepts_json_flag(self) -> None:
        """Health subcommand accepts --json."""
        parser = _build_parser()
        args = parser.parse_args(["health", "--json"])
        assert args.json is True

    def test_health_json_defaults_false(self) -> None:
        """Health subcommand --json defaults to False."""
        parser = _build_parser()
        args = parser.parse_args(["health"])
        assert args.json is False

    def test_health_accepts_offline_flag(self) -> None:
        """Health subcommand accepts --offline."""
        parser = _build_parser()
        args = parser.parse_args(["health", "--offline"])
        assert args.offline is True

    def test_health_offline_defaults_false(self) -> None:
        """Health subcommand --offline defaults to False."""
        parser = _build_parser()
        args = parser.parse_args(["health"])
        assert args.offline is False

    def test_health_accepts_outdir(self, tmp_path: Path) -> None:
        """Health subcommand accepts --outdir."""
        parser = _build_parser()
        args = parser.parse_args(["health", "--outdir", str(tmp_path)])
        assert args.outdir == tmp_path

    def test_health_outdir_defaults_none(self) -> None:
        """Health subcommand --outdir defaults to None."""
        parser = _build_parser()
        args = parser.parse_args(["health"])
        assert args.outdir is None

    def test_health_table_output(self, tmp_path: Path) -> None:
        """_cmd_health prints human-readable output and returns 0."""
        parser = _build_parser()
        args = parser.parse_args(["health", "--outdir", str(tmp_path), "--offline"])
        result = _cmd_health(args)
        assert result == 0

    def test_health_json_output(self, tmp_path: Path) -> None:
        """_cmd_health with --json outputs valid JSON and returns 0."""
        parser = _build_parser()
        args = parser.parse_args(["health", "--outdir", str(tmp_path), "--offline", "--json"])
        result = _cmd_health(args)
        assert result == 0

    def test_health_log_level_default(self) -> None:
        """Health subcommand defaults log_level to ERROR."""
        parser = _build_parser()
        args = parser.parse_args(["health"])
        assert args.log_level == "ERROR"

    def test_health_main_dispatches(self, tmp_path: Path) -> None:
        """main() dispatches to health subcommand and exits 0."""
        rc = main(["health", "--outdir", str(tmp_path), "--offline"])
        assert rc == 0

    def test_health_main_json_dispatches(self, tmp_path: Path) -> None:
        """main() with --json dispatches to health subcommand and exits 0."""
        rc = main(["health", "--outdir", str(tmp_path), "--offline", "--json"])
        assert rc == 0


# ---------------------------------------------------------------------------
# Individual check function tests (isolated)
# ---------------------------------------------------------------------------


class TestIndividualChecks:
    """Tests for individual check functions in isolation."""

    def test_check_python_version_pass(self) -> None:
        """Python version check passes on the test runner."""
        from osimflow.health import _check_python_version  # noqa: PLC0415

        result = _check_python_version()
        assert result.name == "Python Version"
        assert result.critical
        # Tests run on 3.12+ so this should pass.
        assert result.status == CheckStatus.PASS

    def test_check_python_version_fail(self) -> None:
        """Python version check fails when version is below minimum."""
        from osimflow.health import _check_python_version  # noqa: PLC0415

        with patch.object(sys, "version_info", (3, 10, 0, "final", 0)):
            result = _check_python_version()
        assert result.status == CheckStatus.FAIL
        assert "3.10" in result.message

    def test_check_core_packages_pass(self) -> None:
        """Core packages check passes when all installed."""
        from osimflow.health import _check_core_packages  # noqa: PLC0415

        result = _check_core_packages()
        assert result.name == "Core Packages"
        assert result.critical
        # In the dev environment all core packages should be installed.
        assert result.status == CheckStatus.PASS

    def test_check_sqlite_pass(self) -> None:
        """SQLite check passes on a working system."""
        from osimflow.health import _check_sqlite  # noqa: PLC0415

        result = _check_sqlite()
        assert result.status == CheckStatus.PASS

    def test_check_write_permissions_pass(self, tmp_path: Path) -> None:
        """Write permissions check passes on a writable directory."""
        from osimflow.health import _check_write_permissions  # noqa: PLC0415

        result = _check_write_permissions(tmp_path)
        assert result.status == CheckStatus.PASS

    def test_check_write_permissions_cleanup(self, tmp_path: Path) -> None:
        """Write permissions check cleans up its test file."""
        from osimflow.health import _check_write_permissions  # noqa: PLC0415

        _check_write_permissions(tmp_path)
        assert not (tmp_path / ".osimflow_health_test").exists()

    def test_check_optional_packages_returns_result(self) -> None:
        """Optional packages check returns a valid CheckResult."""
        from osimflow.health import _check_optional_packages  # noqa: PLC0415

        result = _check_optional_packages()
        assert result.name == "Optional Packages"
        assert not result.critical
        # In dev environment some optional packages are installed (rich, etc).
        assert result.status in (CheckStatus.PASS, CheckStatus.WARN)

    def test_check_external_tools_returns_list(self) -> None:
        """External tools check returns multiple results."""
        from osimflow.health import _check_external_tools  # noqa: PLC0415

        results = _check_external_tools()
        assert len(results) == 3  # OpenStudio, Docker, Podman
        names = [r.name for r in results]
        assert "OpenStudio CLI" in names
        assert "Docker" in names
        assert "Podman" in names
        for r in results:
            assert not r.critical

    def test_check_disk_space_pass(self, tmp_path: Path) -> None:
        """Disk space check returns a result with GB info."""
        from osimflow.health import _check_disk_space  # noqa: PLC0415

        result = _check_disk_space(tmp_path)
        assert result.name == "Disk Space"
        assert "GB" in result.message
        assert not result.critical

    def test_check_network_skip_returns_skip(self) -> None:
        """Network skip is handled by run_health_checks, not _check_network."""
        # Verify that skip_network=True produces a SKIP status
        import tempfile  # noqa: PLC0415

        with tempfile.TemporaryDirectory() as d:
            report = run_health_checks(outdir=Path(d), skip_network=True)
        net_results = [r for r in report.results if r.name == "Network Connectivity"]
        assert net_results[0].status == CheckStatus.SKIP


# ---------------------------------------------------------------------------
# Per-executor health checks (issue #1024)
# ---------------------------------------------------------------------------


# All ten built-in executors MUST have a registered health check. Add a new
# executor to this set when you add a new entry in ExecutorRegistry.register()
# at the bottom of osimflow/executors/__init__.py. The regression test
# below fails fast if anyone adds a new executor and forgets the check.
EXPECTED_EXECUTOR_HEALTH_CHECKS: frozenset[str] = frozenset(
    {
        "local",
        "slurm",
        "pbs",
        "aws_batch",
        "azure_batch",
        "google_batch",
        "nomad",
        "kubernetes",
        "docker_swarm",
        "dask_jobqueue",
    }
)


class TestExecutorHealthChecks:
    """Tests for per-executor health checks (issue #1024).

    Covers:
      * every ExecutorRegistry entry has a registered health check
      * the orchestrator dispatches via the registry, not a hardcoded list
      * ``configured_executor`` promotes the matching check to CRITICAL
      * a check that raises is contained and reported, not propagated
    """

    def test_registry_lists_all_ten_executors(self) -> None:
        """ExecutorRegistry still lists the canonical 10 executors."""
        from osimflow.executors import ExecutorRegistry  # noqa: PLC0415

        available = set(ExecutorRegistry.list_available())
        assert EXPECTED_EXECUTOR_HEALTH_CHECKS.issubset(available), (
            f"Missing executors: {EXPECTED_EXECUTOR_HEALTH_CHECKS - available}"
        )

    def test_iter_health_checks_covers_all_ten(self) -> None:
        """Every expected executor has a registered health check."""
        from osimflow.executors import ExecutorRegistry  # noqa: PLC0415

        names = {name for name, _fn in ExecutorRegistry.iter_health_checks()}
        assert names >= EXPECTED_EXECUTOR_HEALTH_CHECKS, (
            f"Missing health checks for: {EXPECTED_EXECUTOR_HEALTH_CHECKS - names}"
        )

    def test_iter_health_checks_is_sorted(self) -> None:
        """iter_health_checks returns a sorted list for deterministic output."""
        from osimflow.executors import ExecutorRegistry  # noqa: PLC0415

        names = [name for name, _fn in ExecutorRegistry.iter_health_checks()]
        assert names == sorted(names)

    def test_register_health_check_rejects_unknown_executor(self) -> None:
        """register_health_check raises ValueError for an unknown executor."""
        from osimflow.executors import ExecutorRegistry  # noqa: PLC0415

        with pytest.raises(ValueError, match="not registered"):
            ExecutorRegistry.register_health_check("does_not_exist", lambda: None)

    def test_get_health_check_returns_callable(self) -> None:
        """get_health_check returns a callable for a known executor."""
        from osimflow.executors import ExecutorRegistry  # noqa: PLC0415

        for name in EXPECTED_EXECUTOR_HEALTH_CHECKS:
            check = ExecutorRegistry.get_health_check(name)
            assert callable(check), f"{name} did not return a callable"

    def test_run_health_checks_includes_all_executor_results(self, tmp_path: Path) -> None:
        """run_health_checks emits one result per registered executor check."""
        report = run_health_checks(outdir=tmp_path, skip_network=True)
        executor_names = [r.name for r in report.results if r.name.startswith("Executor: ")]
        assert len(executor_names) >= len(EXPECTED_EXECUTOR_HEALTH_CHECKS)
        for expected in EXPECTED_EXECUTOR_HEALTH_CHECKS:
            assert f"Executor: {expected}" in executor_names, (
                f"Missing health check result for executor '{expected}'"
            )

    def test_default_run_returns_only_informational_executor_checks(self, tmp_path: Path) -> None:
        """Without configured_executor, every per-executor check is INFORMATIONAL."""
        report = run_health_checks(outdir=tmp_path, skip_network=True)
        for r in report.results:
            if r.name.startswith("Executor: "):
                assert r.category == CheckCategory.INFORMATIONAL, (
                    f"{r.name} unexpectedly promoted to {r.category}"
                )

    def test_configured_executor_promotes_check_to_critical(self, tmp_path: Path) -> None:
        """configured_executor=<name> promotes that check to CRITICAL."""
        report = run_health_checks(
            outdir=tmp_path,
            skip_network=True,
            configured_executor="local",
        )
        local_results = [r for r in report.results if r.name == "Executor: local"]
        assert len(local_results) == 1
        assert local_results[0].critical
        # Other executor checks stay INFORMATIONAL.
        for r in report.results:
            if r.name.startswith("Executor: ") and r.name != "Executor: local":
                assert r.category == CheckCategory.INFORMATIONAL

    def test_configured_executor_for_unregistered_name_is_noop(self, tmp_path: Path) -> None:
        """An unknown configured_executor name does not raise or promote anything."""
        # Should not raise; no per-executor check has that name, so all
        # stay INFORMATIONAL.
        report = run_health_checks(
            outdir=tmp_path,
            skip_network=True,
            configured_executor="this_executor_does_not_exist",
        )
        for r in report.results:
            if r.name.startswith("Executor: "):
                assert r.category == CheckCategory.INFORMATIONAL

    def test_check_raising_is_contained(self, tmp_path: Path) -> None:
        """A check that raises is captured as WARN, not propagated."""
        from osimflow.executors import ExecutorRegistry  # noqa: PLC0415

        original = ExecutorRegistry.get_health_check("local")
        ExecutorRegistry.register_health_check(
            "local", lambda: (_ for _ in ()).throw(RuntimeError("boom"))
        )
        try:
            report = run_health_checks(outdir=tmp_path, skip_network=True)
            local_results = [r for r in report.results if r.name == "Executor: local"]
            assert len(local_results) == 1
            assert local_results[0].status == CheckStatus.WARN
            assert "boom" in local_results[0].detail
        finally:
            # Restore so we don't leak state across tests.
            ExecutorRegistry.register_health_check("local", original)


# ---------------------------------------------------------------------------
# Individual per-executor check functions (issue #1024)
# ---------------------------------------------------------------------------


class TestIndividualExecutorChecks:
    """Smoke tests for each of the 10 _check_<executor>() functions."""

    def _invoke(self, name: str):  # noqa: ANN202
        from osimflow.health import _BUILTIN_EXECUTOR_HEALTH_CHECKS  # noqa: PLC0415

        fn = _BUILTIN_EXECUTOR_HEALTH_CHECKS[name]
        return fn()

    def test_local_check_returns_check_result(self) -> None:
        """_check_local returns a CheckResult with the canonical name."""
        r = self._invoke("local")
        assert isinstance(r, CheckResult)
        assert r.name == "Executor: local"
        # The CI runner is Linux with many CPUs — should pass.
        assert r.status in {CheckStatus.PASS, CheckStatus.WARN}

    def test_slurm_check_handles_missing_sinfo(self) -> None:
        """_check_slurm returns SKIP when sinfo is absent."""
        with patch("shutil.which", return_value=None):
            r = self._invoke("slurm")
        assert r.status == CheckStatus.SKIP
        assert r.category == CheckCategory.INFORMATIONAL

    def test_pbs_check_handles_missing_qstat(self) -> None:
        """_check_pbs returns SKIP when qstat is absent."""
        with patch("shutil.which", return_value=None):
            r = self._invoke("pbs")
        assert r.status == CheckStatus.SKIP

    def test_aws_batch_check_skips_without_boto3(self) -> None:
        """_check_aws_batch returns SKIP when boto3 is not importable."""
        import builtins  # noqa: PLC0415

        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):  # noqa: ANN001, ANN002
            if name == "boto3":
                raise ImportError("simulated missing boto3")
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=fake_import):
            r = self._invoke("aws_batch")
        assert r.status == CheckStatus.SKIP
        assert "boto3" in r.message

    def test_nomad_check_skips_without_cli(self) -> None:
        """_check_nomad returns SKIP when nomad CLI is absent."""
        with patch("shutil.which", return_value=None):
            r = self._invoke("nomad")
        assert r.status == CheckStatus.SKIP

    def test_kubernetes_check_skips_without_kubectl(self) -> None:
        """_check_kubernetes returns SKIP when kubectl is absent."""
        with patch("shutil.which", return_value=None):
            r = self._invoke("kubernetes")
        assert r.status == CheckStatus.SKIP

    def test_docker_swarm_check_skips_without_docker(self) -> None:
        """_check_docker_swarm returns SKIP when docker CLI is absent."""
        with patch("shutil.which", return_value=None):
            r = self._invoke("docker_swarm")
        assert r.status == CheckStatus.SKIP

    def test_dask_jobqueue_check_skips_without_sdk(self) -> None:
        """_check_dask_jobqueue returns SKIP when dask_jobqueue is not installed."""
        import builtins  # noqa: PLC0415

        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):  # noqa: ANN001, ANN002
            if name == "dask_jobqueue":
                raise ImportError("simulated missing dask_jobqueue")
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=fake_import):
            r = self._invoke("dask_jobqueue")
        assert r.status == CheckStatus.SKIP

    def test_azure_batch_check_skips_without_sdk(self) -> None:
        """_check_azure_batch returns SKIP when azure-batch is not installed."""
        import builtins  # noqa: PLC0415

        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):  # noqa: ANN001, ANN002
            if name.startswith("azure.batch"):
                raise ImportError("simulated missing azure-batch")
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=fake_import):
            r = self._invoke("azure_batch")
        assert r.status == CheckStatus.SKIP

    def test_google_batch_check_skips_without_sdk(self) -> None:
        """_check_google_batch returns SKIP when google-cloud-batch is missing."""
        import builtins  # noqa: PLC0415

        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):  # noqa: ANN001, ANN002
            if name.startswith("google.cloud.batch"):
                raise ImportError("simulated missing google-cloud-batch")
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=fake_import):
            r = self._invoke("google_batch")
        assert r.status == CheckStatus.SKIP


# ---------------------------------------------------------------------------
# CLI flag for --executor (issue #1024)
# ---------------------------------------------------------------------------


class TestHealthCLIExecutorFlag:
    """The ``osimflow health --executor <name>`` flag promotes that check."""

    def test_help_lists_executor_flag(self) -> None:
        """--help shows the --executor flag."""
        parser = _build_parser()
        # Invoke the parser directly rather than spawning a subprocess
        # (which would require `python` on PATH and a re-installed
        # package). ``format_help`` walks every subparser so the health
        # subcommand's help text is included.
        help_text = parser.format_help()
        # We can't see the health subcommand's own --help from the
        # top-level help; the next-best check is to verify the parser
        # accepts --executor without raising.
        try:
            args = parser.parse_args(["health", "--offline", "--executor", "slurm"])
        except SystemExit as exc:  # argparse exits on parse error
            pytest.fail(f"parser rejected --executor flag: exit={exc.code}")
        assert args.executor == "slurm"
        # Sanity: the help text mentions the health subcommand somewhere.
        assert "health" in help_text

    def test_cmd_health_passes_configured_executor(self, tmp_path: Path) -> None:
        """_cmd_health forwards --executor to run_health_checks."""
        parser = _build_parser()
        args = parser.parse_args(
            ["health", "--offline", "--executor", "local", "--outdir", str(tmp_path)]
        )
        assert args.executor == "local"
        # Stub run_health_checks via the _cmd_health invocation path.
        with patch("osimflow.health.run_health_checks") as mock_run:
            mock_run.return_value = HealthReport(results=[])
            from osimflow.__main__ import _cmd_health  # noqa: PLC0415

            exit_code = _cmd_health(args)
        mock_run.assert_called_once()
        kwargs = mock_run.call_args.kwargs
        assert kwargs.get("configured_executor") == "local"


class TestTaskPayloadSigningCheck:
    """Health-check warning for HMAC task-payload contract (issue #1404)."""

    @staticmethod
    def _check(executor: str | None):
        from osimflow.health import _check_task_payload_signing

        return _check_task_payload_signing(executor)

    def test_skipped_without_configured_executor(self) -> None:
        result = self._check(None)
        assert result.status.value in {"skip", "SKIP", "informational"}
        assert "Skipped" in result.message or "no --executor" in result.message

    def test_skipped_for_non_payload_executor(self) -> None:
        for name in ("local", "slurm", "pbs"):
            result = self._check(name)
            assert result.status.value in {"skip", "SKIP", "informational"}, name

    def test_warn_when_secret_missing_on_payload_executor(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("OSIMFLOW_TASK_PAYLOAD_SECRET", raising=False)
        result = self._check("nomad")
        assert result.status.value.lower() == "warn"
        assert "fails closed" in result.message

    @pytest.mark.parametrize("executor", ["nomad", "kubernetes", "aws_batch", "azure_batch"])
    def test_pass_when_secret_set_and_executor_signs(
        self, executor: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OSIMFLOW_TASK_PAYLOAD_SECRET", "s" * 32)
        result = self._check(executor)
        assert result.status.value.lower() == "pass", result.message

    def test_warn_when_executor_does_not_sign_but_secret_set(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The acceptance scenario: orchestrator-with-secret + executor-without-signing.

        Simulated by flipping a real payload executor's ``signs_task_payload``
        to ``False`` — the shape of a third-party executor (or a regression)
        that consumes the payload contract without propagating the secret.
        """
        from osimflow.executors import NomadExecutor

        monkeypatch.setattr(NomadExecutor, "signs_task_payload", False)
        monkeypatch.setenv("OSIMFLOW_TASK_PAYLOAD_SECRET", "s" * 32)
        result = self._check("nomad")
        assert result.status.value.lower() == "warn", result.message
        assert "signature verification" in result.message or "swallow" in result.detail


# ---------------------------------------------------------------------------
# Redis deployment-mode check (issue #1562 / ADR-0004)
# ---------------------------------------------------------------------------
# Uses fakeredis to keep the check hermetic and free of any network call.
# Mirrors the wiring pattern in
# tests/integration/test_distributed_cache_invalidation.py — the lazy
# ``import redis`` is intercepted so ``from_url`` returns a
# ``fakeredis.FakeRedis`` shared by every test that needs one. The
# probe in ``_check_redis_deployment_mode`` only calls ``ping()`` and
# ``info()`` on the client, both of which fakeredis implements.


@pytest.fixture
def fake_redis_module():
    """Patch the lazy ``import redis`` inside ``osimflow.health`` to fakeredis.

    Returns the shared :class:`fakeredis.FakeServer` so tests can flush
    state or assert on writes if needed. The probe only reads, so this
    fixture's main role is to make ``from_url`` succeed hermetically.

    Fakeredis 2.x does not implement ``INFO`` (it raises
    ``ResponseError``), so this fixture also patches the client's
    ``info()`` to return a minimal but realistic payload so the role /
    version / latency assertions in the tests are deterministic.
    """
    try:
        import fakeredis as _fakeredis  # noqa: PLC0415
    except ImportError:  # pragma: no cover — [dev] only
        pytest.skip("fakeredis not installed")
    server = _fakeredis.FakeServer()
    fake = _fakeredis.FakeRedis(server=server, decode_responses=True)

    def _info_stub(*_args: object, **_kwargs: object) -> dict[str, object]:
        return {
            "redis_version": "7.4.0-fakeredis",
            "replication": {"role": "master"},
        }

    fake.info = _info_stub  # type: ignore[method-assign]

    class _FakeModule:
        def from_url(self, *_args: object, **_kwargs: object) -> object:
            return fake

    class _FakeAsyncioModule:
        def from_url(self, *_args: object, **_kwargs: object) -> object:  # pragma: no cover
            return _fakeredis.FakeAsyncRedis(server=server)

    with patch.dict(sys.modules, {"redis": _FakeModule()}):
        yield server


class TestRedisDeploymentModeCheck:
    """Issue #1562 / ADR-0004 acceptance: health check reports the
    deployment mode of the Redis instance behind ``--redis-url`` and
    surfaces the cache-replay resume path on outage."""

    @staticmethod
    def _check(redis_url: str | None):
        from osimflow.health import _check_redis_deployment_mode  # noqa: PLC0415

        return _check_redis_deployment_mode(redis_url)

    def test_skip_when_no_url(self) -> None:
        """No URL -> SKIP with the four-plane pointer in detail."""
        result = self._check(None)
        assert result.name == "Redis Deployment Mode"
        assert result.status == CheckStatus.SKIP
        assert result.category == CheckCategory.INFORMATIONAL
        assert "DistributedCache" in result.detail
        assert "RedisDocumentStore" in result.detail
        assert "DistributedJobQueue" in result.detail
        assert "rate limiter" in result.detail
        assert "1562" in result.detail

    def test_pass_for_single_instance(self, fake_redis_module: object) -> None:
        """A reachable ``redis://`` URL reports single + version + role."""
        result = self._check("redis://localhost:6379/0")
        assert result.status == CheckStatus.PASS
        assert result.category == CheckCategory.INFORMATIONAL
        assert "single-instance Redis" in result.message
        # Info role: fakeredis returns role='master' by default.
        assert "role=master" in result.detail
        assert "ADR-0004" in result.detail
        # URL redaction should be a no-op for a URL without credentials.
        assert "localhost" in result.detail

    def test_pass_redacts_embedded_password(self, fake_redis_module: object) -> None:
        """A URL with ``user:pass@`` must not leak the password in detail."""
        result = self._check("redis://user:secret@localhost:6379/0")
        assert result.status == CheckStatus.PASS
        assert "secret" not in result.detail
        assert ":***@" in result.detail

    def test_warn_for_sentinel_url(self, fake_redis_module: object) -> None:
        """A ``redis+sentinel://`` URL is currently unsupported -> WARN."""
        result = self._check("redis+sentinel://sentinel-1:26379,mymaster")
        assert result.status == CheckStatus.WARN
        assert result.category == CheckCategory.INFORMATIONAL
        assert "sentinel" in result.message.lower()
        assert "Sentinel" in result.detail
        assert "not in this release" in result.detail or "not currently supported" in result.detail
        assert "ADR-0004" in result.detail

    def test_warn_for_cluster_url(self, fake_redis_module: object) -> None:
        """A ``redis+cluster://`` URL is currently unsupported -> WARN."""
        result = self._check("redis+cluster://redis-cluster-1:6379")
        assert result.status == CheckStatus.WARN
        assert "cluster" in result.message.lower()
        assert "ADR-0004" in result.detail

    def test_warn_for_unknown_scheme(self, fake_redis_module: object) -> None:
        """An unrecognized scheme that still connects -> WARN unknown."""
        result = self._check("http://localhost:6379")
        assert result.status == CheckStatus.WARN
        assert "mode=unknown" in result.detail or "unknown" in result.message.lower()

    def test_fail_when_unreachable(self) -> None:
        """An unreachable host must FAIL with the recovery story in detail."""
        # 127.0.0.1:1 is reserved (tcpmux) and refused on every host —
        # the connection refuses immediately rather than hanging on the
        # 5 s timeout. We patch ``from_url`` to a stub that raises the
        # same ConnectionError real redis-py would raise on refusal.
        from redis.exceptions import ConnectionError as RedisConnectionError  # noqa: PLC0415

        class _RaisingModule:
            def from_url(self, *_args: object, **_kwargs: object) -> object:
                raise RedisConnectionError("Connection refused.")

        with patch.dict(sys.modules, {"redis": _RaisingModule()}):
            result = self._check("redis://127.0.0.1:1/0")
        assert result.status == CheckStatus.FAIL
        assert "Redis outage" in result.detail
        # Recovery story must be cited.
        assert "cache" in result.detail.lower() and "replay" in result.detail.lower()
        assert "ADR-0004" in result.detail
        # Passwords must not leak even on FAIL.
        assert "secret" not in result.detail

    def test_fail_with_password_redacted(self) -> None:
        """Failure path must not leak the URL password into detail."""
        from redis.exceptions import ConnectionError as RedisConnectionError  # noqa: PLC0415

        class _RaisingModule:
            def from_url(self, *_args: object, **_kwargs: object) -> object:
                raise RedisConnectionError("Connection refused.")

        with patch.dict(sys.modules, {"redis": _RaisingModule()}):
            result = self._check("redis://user:secret@127.0.0.1:1/0")
        assert result.status == CheckStatus.FAIL
        assert "secret" not in result.detail
        assert ":***@" in result.detail

    def test_skip_when_redis_not_installed(self) -> None:
        """If the ``redis`` package is absent the check SKIPs cleanly."""
        import builtins  # noqa: PLC0415

        real_import = builtins.__import__

        def _fake_import(name: str, *args: object, **kwargs: object):  # noqa: ANN001, ANN002
            if name == "redis" or name.startswith("redis."):
                raise ImportError("simulated missing redis")
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=_fake_import):
            result = self._check("redis://localhost:6379/0")
        assert result.status == CheckStatus.SKIP
        assert "redis" in result.message.lower()


class TestHealthCLIRedisURLFlag:
    """The ``osimflow health --redis-url`` CLI flag forwards to the check."""

    def test_help_lists_redis_url_flag(self) -> None:
        parser = _build_parser()
        try:
            args = parser.parse_args(
                ["health", "--offline", "--redis-url", "redis://localhost:6379/0"]
            )
        except SystemExit as exc:  # argparse exits on parse error
            pytest.fail(f"parser rejected --redis-url flag: exit={exc.code}")
        assert args.redis_url == "redis://localhost:6379/0"

    def test_cmd_health_forwards_redis_url(self, tmp_path: Path) -> None:
        """_cmd_health forwards --redis-url to run_health_checks."""
        with patch("osimflow.health.run_health_checks") as mock_run:
            mock_run.return_value = HealthReport(results=[])
            from osimflow.__main__ import _cmd_health  # noqa: PLC0415

            parser = _build_parser()
            args = parser.parse_args(
                [
                    "health",
                    "--offline",
                    "--redis-url",
                    "redis://user:hush@redis.local:6379/0",
                    "--outdir",
                    str(tmp_path),
                ]
            )
            exit_code = _cmd_health(args)
        mock_run.assert_called_once()
        kwargs = mock_run.call_args.kwargs
        assert kwargs.get("redis_url") == "redis://user:hush@redis.local:6379/0"
        assert exit_code == 0

    def test_run_health_checks_omits_redis_check_by_default(self, tmp_path: Path) -> None:
        """Without ``redis_url=`` the Redis check SKIPs (single-node campaigns)."""
        report = run_health_checks(outdir=tmp_path, skip_network=True)
        redis_results = [r for r in report.results if r.name == "Redis Deployment Mode"]
        assert len(redis_results) == 1
        assert redis_results[0].status == CheckStatus.SKIP
