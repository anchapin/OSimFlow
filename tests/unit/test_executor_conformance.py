"""Smoke test for :mod:`osimflow.testing` (issue #1478).

Runs :class:`osimflow.testing.ExecutorConformanceSuite` against the
in-repo :class:`~osimflow.executors.LocalExecutor` to prove the harness
itself is correct. Third-party plug-in authors will write their own
subclass with their own factory; this test ensures the suite catches
the same regressions the in-repo ``test_local_executor.py`` does, but
parameterised over the executor under test.

The full 3-sample stub campaign check is opt-in here (``run_stub_campaign=True``)
because ``LocalExecutor`` exercises every Campaign code path, including
the per-sample resource propagation warnings (``cpus`` / ``memory_mb``
are advisory locally). Remote executors that already have their own
integration test should set ``run_stub_campaign=False`` to avoid
duplicating the campaign surface.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from osimflow.executors import LocalExecutor
from osimflow.testing import (
    ConformanceCheck,
    ConformanceReport,
    ExecutorConformanceSuite,
    run_executor_conformance,
)


def _local_factory() -> LocalExecutor:
    """Build a fresh ``LocalExecutor`` per test."""
    return LocalExecutor(max_workers=3)


class TestLocalExecutorConformance(ExecutorConformanceSuite):
    """Run the conformance suite against ``LocalExecutor``."""

    executor_factory = staticmethod(_local_factory)
    # The stub campaign runs end-to-end through LocalExecutor, so we
    # opt in here. Remote-only plug-ins should leave this False.
    run_stub_campaign = True


# ---------------------------------------------------------------------------
# Direct programmatic-runner coverage (issue #1478, third-party scripts)
# ---------------------------------------------------------------------------


class TestRunExecutorConformance:
    """Smoke-test :func:`run_executor_conformance` independently of pytest."""

    def test_report_dataclass_round_trip(self) -> None:
        """``ConformanceReport`` exposes pass/fail counts via ``to_dict``."""
        report = ConformanceReport(executor_name="local")
        report.checks.append(ConformanceCheck("a", True, "ok"))
        report.checks.append(ConformanceCheck("b", False, "boom"))
        assert not report.passed
        assert [c.name for c in report.failed_checks] == ["b"]
        d = report.to_dict()
        assert d["executor"] == "local"
        assert d["passed"] is False
        assert d["n_checks"] == 2
        assert d["n_passed"] == 1
        assert d["n_failed"] == 1
        assert d["checks"] == [
            {"name": "a", "passed": True, "detail": "ok"},
            {"name": "b", "passed": False, "detail": "boom"},
        ]

    def test_report_passed_true_when_all_checks_pass(self) -> None:
        report = ConformanceReport(executor_name="local")
        report.checks.append(ConformanceCheck("only", True, "ok"))
        assert report.passed
        assert report.failed_checks == []

    def test_run_executor_conformance_against_local_executor(self) -> None:
        """The programmatic runner exercises every check against LocalExecutor."""
        # run_stub_campaign=False keeps the test fast; the campaign check
        # has its own pytest.mark.slow test below.
        report = run_executor_conformance(LocalExecutor(max_workers=2))
        assert report.executor_name == "local"
        # Every check must be present and passing.
        names = [c.name for c in report.checks]
        expected = {
            "submit_returns_handle",
            "handle_job_id_non_empty",
            "handle_done_returns_bool",
            "handle_result_returns_value",
            "handle_result_respects_timeout",
            "handle_error_propagates",
            "resource_directives_accepted",
            "transport_path_round_trip",
            "transport_result_hint_default",
            "transport_result_hint_path_payload",
            "fanout_chunk_size_positive",
        }
        assert expected.issubset(set(names)), f"missing checks: {expected - set(names)}"
        failed = report.failed_checks
        assert not failed, f"failed checks: {[(c.name, c.detail) for c in failed]}"

    def test_run_executor_conformance_campaign_opt_in(self, tmp_path: Path) -> None:
        """Opt-in stub campaign produces all four artifacts."""
        example = Path(__file__).resolve().parents[2] / "example_package"
        if not example.is_dir():
            pytest.skip(f"example_package not found at {example}")
        report = run_executor_conformance(
            LocalExecutor(max_workers=3),
            run_stub_campaign=True,
            example_package=example,
        )
        campaign_checks = [c for c in report.checks if c.name == "three_sample_stub_campaign"]
        assert len(campaign_checks) == 1
        assert campaign_checks[0].passed, campaign_checks[0].detail

    def test_run_executor_conformance_returns_serialisable_dict(self) -> None:
        """``to_dict()`` output is JSON-serialisable for CI consumption."""
        import json as _json

        report = run_executor_conformance(LocalExecutor(max_workers=1))
        payload = report.to_dict()
        # Round-trip through JSON to confirm no non-serialisable values.
        _json.dumps(payload)


# ---------------------------------------------------------------------------
# In-suite health-check registration smoke (extra coverage beyond the mixin)
# ---------------------------------------------------------------------------


def test_health_check_registration_round_trip() -> None:
    """The mixin's ``test_register_health_check_returns_callable`` passes for ``local``."""
    from osimflow.executors import ExecutorRegistry  # noqa: PLC0415
    from osimflow.health import (  # noqa: PLC0415
        CheckCategory,
        CheckResult,
        CheckStatus,
    )

    original = ExecutorRegistry.get_health_check("local")
    try:
        ExecutorRegistry.register_health_check(
            "local",
            lambda: CheckResult(
                name="Executor: local (conformance-direct)",
                status=CheckStatus.PASS,
                category=CheckCategory.INFORMATIONAL,
                message="ok",
            ),
        )
        check = ExecutorRegistry.get_health_check("local")
        assert check is not None
        result = check()
        assert isinstance(result, CheckResult)
        assert result.status == CheckStatus.PASS
    finally:
        ExecutorRegistry.clear_health_checks()
        if original is not None:
            ExecutorRegistry.register_health_check("local", original)


def test_run_json_schema_round_trip(tmp_path: Path) -> None:
    """Sanity check: ``ConformanceReport.to_dict()`` survives ``json.dumps``."""
    report = ConformanceReport(executor_name="local")
    report.checks.append(ConformanceCheck("a", True, "ok"))
    text = json.dumps(report.to_dict())
    assert "passed" in text
    assert "executor" in text
