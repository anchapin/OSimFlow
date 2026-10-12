"""CancelledError must reach the failure-marking path in _await_one (issue #1570).

``_await_one`` (in ``osimflow/campaign.py``) used to wrap
``handle.result()`` in ``except Exception`` only.  Modern Python makes
``concurrent.futures.CancelledError`` (and submitit's cancellation
error) a ``BaseException`` subclass, so they bypassed the handler and
the sample was silently absent from ``run.json`` /
``failed_simulations.csv``.

Acceptance criterion: the failure-marking path now catches
``BaseException`` (re-raising genuine ``KeyboardInterrupt`` /
``SystemExit``) and routes cancellation errors through the same
recording side-effects as ordinary ``Exception`` failures:

  * ``_job_queue.mark_failed(...)`` is called,
  * ``_sample_state[sid]`` is populated with ``<step>_exit_code=1``,
    ``<step>_status="failed"``, and an ``error_summary``,
  * ``_checkpoint_sample(sid)`` writes an incremental run.json row,
  * the observability backend receives a ``"failed"`` status metric,
  * the ``sample.failed`` alert is fired via ``_maybe_alert``.
"""

from __future__ import annotations

import concurrent.futures
from concurrent.futures import Future
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from osimflow import Campaign, CampaignConfig
from osimflow.executors import Handle, LocalExecutor


def _make_ok_handle(result_value: Any) -> Handle:
    """Handle whose ``result()`` resolves to *result_value*."""
    fut: Future[Any] = Future()
    fut.set_result(result_value)
    return Handle(job_id="ok-job", _future=fut, worker_id="local", worker_ip="testhost")


def _make_cancelled_handle() -> Handle:
    """Handle whose ``result()`` raises ``concurrent.futures.CancelledError``."""
    fut: Future[Any] = Future()
    fut.set_exception(concurrent.futures.CancelledError("simulator cancelled"))
    return Handle(job_id="cancelled-job", _future=fut, worker_id="local", worker_ip="testhost")


def _cfg(
    variables_yml: Path,
    template_pkg: Path,
    outdir: Path,
    **overrides: Any,
) -> CampaignConfig:
    defaults: dict[str, Any] = {
        "input_variables": variables_yml,
        "template_sim_package": template_pkg,
        "n_samples": 3,
        "outdir": outdir,
        "openstudio_version": "3.11.0",
        "dry_run": True,
    }
    defaults.update(overrides)
    return CampaignConfig(**defaults)


class TestAwaitOneCancellationAccounting:
    """``_await_one`` must route ``CancelledError`` through the failure path."""

    def test_cancelled_error_marks_job_queue_failed(
        self,
        variables_yml: Path,
        template_pkg: Path,
        outdir: Path,
    ) -> None:
        """Job-queue entry transitions to ``failed`` (not stuck ``in_progress``)."""
        cfg = _cfg(variables_yml, template_pkg, outdir, n_samples=2)
        campaign = Campaign(cfg=cfg, executor=LocalExecutor())

        submissions: dict[str, tuple[Handle, Any]] = {
            "sample_0": (_make_cancelled_handle(), MagicMock()),
            "sample_1": (_make_ok_handle("ok"), MagicMock()),
        }

        with patch.object(campaign, "_job_queue") as mock_jq:
            campaign._submit_and_await_all(
                submissions=submissions,
                step_name="RUN_OPENSTUDIO_SIM",
            )

            failed_keys = [call.args[0] for call in mock_jq.mark_failed.call_args_list]
            assert "sample_0_RUN_OPENSTUDIO_SIM" in failed_keys

            completed_keys = [call.args[0] for call in mock_jq.mark_completed.call_args_list]
            assert "sample_1_RUN_OPENSTUDIO_SIM" in completed_keys
            # The cancelled sample must NOT be marked completed.
            assert "sample_0_RUN_OPENSTUDIO_SIM" not in completed_keys

    def test_cancelled_error_records_failure_in_sample_state(
        self,
        variables_yml: Path,
        template_pkg: Path,
        outdir: Path,
    ) -> None:
        """``_sample_state[sid]`` carries the failure keys for cancelled samples."""
        cfg = _cfg(variables_yml, template_pkg, outdir, n_samples=1)
        campaign = Campaign(cfg=cfg, executor=LocalExecutor())

        submissions: dict[str, tuple[Handle, Any]] = {
            "sample_0": (_make_cancelled_handle(), MagicMock()),
        }

        with patch.object(campaign, "_checkpoint_sample", lambda sid: None):
            campaign._submit_and_await_all(
                submissions=submissions,
                step_name="RUN_OPENSTUDIO_SIM",
            )

        state = campaign._sample_state["sample_0"]
        # ``_await_one`` writes ``<step_name.lower()>_exit_code`` /
        # ``<step_name.lower()>_status`` — for RUN_OPENSTUDIO_SIM that
        # is ``run_openstudio_sim_*`` (pre-existing key shape).
        assert state["run_openstudio_sim_exit_code"] == 1
        assert state["run_openstudio_sim_status"] == "failed"
        # ``str(CancelledError("simulator cancelled"))`` is the bare
        # message — what the helper stores in ``error_summary``.
        assert "simulator cancelled" in state["error_summary"]

    def test_cancelled_error_fires_sample_failed_alert(
        self,
        variables_yml: Path,
        template_pkg: Path,
        outdir: Path,
    ) -> None:
        """``sample.failed`` alert is invoked for cancelled samples too."""
        cfg = _cfg(variables_yml, template_pkg, outdir, n_samples=1)
        campaign = Campaign(cfg=cfg, executor=LocalExecutor())

        submissions: dict[str, tuple[Handle, Any]] = {
            "sample_0": (_make_cancelled_handle(), MagicMock()),
        }

        with (
            patch.object(campaign, "_checkpoint_sample", lambda sid: None),
            patch.object(campaign, "_maybe_alert") as mock_alert,
        ):
            campaign._submit_and_await_all(
                submissions=submissions,
                step_name="RUN_OPENSTUDIO_SIM",
            )

        alert_event_types = [call.args[0] for call in mock_alert.call_args_list]
        assert "sample.failed" in alert_event_types

        # The alert context for the cancelled sample includes the failure
        # status, the step, and the truncated error string.
        failure_alerts = [
            call for call in mock_alert.call_args_list if call.args[0] == "sample.failed"
        ]
        assert failure_alerts, "expected at least one sample.failed alert"
        ctx = failure_alerts[0].args[1]
        assert ctx["sample_id"] == "sample_0"
        assert ctx["step"] == "RUN_OPENSTUDIO_SIM"
        assert ctx["status"] == "failed"
        assert "simulator cancelled" in ctx["error"]

    def test_cancelled_error_records_observability_status(
        self,
        variables_yml: Path,
        template_pkg: Path,
        outdir: Path,
    ) -> None:
        """The observability backend sees ``"failed"`` for cancelled samples."""
        cfg = _cfg(variables_yml, template_pkg, outdir, n_samples=1)
        campaign = Campaign(cfg=cfg, executor=LocalExecutor())

        submissions: dict[str, tuple[Handle, Any]] = {
            "sample_0": (_make_cancelled_handle(), MagicMock()),
        }

        with (
            patch.object(campaign, "_checkpoint_sample", lambda sid: None),
            patch.object(campaign._obs, "record_sample_status", autospec=True) as mock_record,
        ):
            campaign._submit_and_await_all(
                submissions=submissions,
                step_name="RUN_OPENSTUDIO_SIM",
            )

        # ``_obs.record_sample_status`` was called with status="failed"
        # for the cancelled sample.
        failed_calls = [call for call in mock_record.call_args_list if call.args[1] == "failed"]
        assert any(call.args[0] == "sample_0" for call in failed_calls)


class TestAwaitOneInterruptPropagation:
    """Genuine ``KeyboardInterrupt`` / ``SystemExit`` MUST propagate."""

    def test_keyboard_interrupt_propagates_through_await_one(
        self,
        variables_yml: Path,
        template_pkg: Path,
        outdir: Path,
    ) -> None:
        cfg = _cfg(variables_yml, template_pkg, outdir, n_samples=1)
        campaign = Campaign(cfg=cfg, executor=LocalExecutor())

        fut: Future[Any] = Future()
        fut.set_exception(KeyboardInterrupt("user Ctrl-C"))
        handle = Handle(job_id="int-job", _future=fut, worker_id="local", worker_ip="testhost")

        submissions: dict[str, tuple[Handle, Any]] = {"sample_0": (handle, MagicMock())}

        with patch.object(campaign, "_checkpoint_sample", lambda sid: None):
            with pytest.raises(KeyboardInterrupt):
                campaign._submit_and_await_all(
                    submissions=submissions,
                    step_name="RUN_OPENSTUDIO_SIM",
                )

    def test_system_exit_propagates_through_await_one(
        self,
        variables_yml: Path,
        template_pkg: Path,
        outdir: Path,
    ) -> None:
        cfg = _cfg(variables_yml, template_pkg, outdir, n_samples=1)
        campaign = Campaign(cfg=cfg, executor=LocalExecutor())

        fut: Future[Any] = Future()
        fut.set_exception(SystemExit(1))
        handle = Handle(job_id="exit-job", _future=fut, worker_id="local", worker_ip="testhost")

        submissions: dict[str, tuple[Handle, Any]] = {"sample_0": (handle, MagicMock())}

        with patch.object(campaign, "_checkpoint_sample", lambda sid: None):
            with pytest.raises(SystemExit):
                campaign._submit_and_await_all(
                    submissions=submissions,
                    step_name="RUN_OPENSTUDIO_SIM",
                )
