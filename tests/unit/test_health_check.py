"""Tests for the ``osimflow health`` CLI subcommand and health module (issue #411)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

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
