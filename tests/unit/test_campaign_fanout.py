"""Standalone unit tests for ``osimflow._campaign_fanout`` (issue #1542).

These exercise the extracted fan-out wait loop through its explicit
:class:`FanoutDeps` dependency bundle — no ``Campaign`` instance is
constructed, which is the acceptance criterion for the extraction:
the collaborator must be testable (and reusable) without the Campaign
god object.

The Campaign-level behaviour (delegating methods, patch seams) stays
covered by the pre-existing oracles — ``tests/unit/test_campaign.py``
(TestFanOutRecoveryPath), ``test_await_one_cancellation.py``,
``test_await_one_deadline.py`` — which run unchanged against the
delegating ``Campaign._submit_and_await_all``.
"""

from __future__ import annotations

import concurrent.futures
from concurrent.futures import Future
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from osimflow import CampaignConfig
from osimflow._campaign_fanout import (
    FanoutDeps,
    compute_await_deadline,
    mark_sample_failed,
    submit_and_await_all,
)
from osimflow._campaign_lifecycle import CampaignPauseRequested
from osimflow.executors import Handle
from osimflow.monitoring import RunTrace, WorkerRecoveryManager


def _make_ok_handle(result_value: Any) -> Handle:
    fut: Future[Any] = Future()
    fut.set_result(result_value)
    return Handle(job_id="ok-job", _future=fut, worker_id="local", worker_ip="testhost")


def _make_failing_handle(exc: BaseException) -> Handle:
    fut: Future[Any] = Future()
    fut.set_exception(exc)
    return Handle(job_id="failing-job", _future=fut, worker_id="local", worker_ip="testhost")


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


def _deps(
    cfg: CampaignConfig,
    outdir: Path,
    *,
    max_workers: int = 1,
) -> tuple[FanoutDeps, dict[str, Any]]:
    """Build a FanoutDeps bundle from fakes; returns (deps, spies)."""
    job_queue = MagicMock()
    obs = MagicMock()
    maybe_alert = MagicMock()
    checkpoint = MagicMock()
    trace = RunTrace(campaign_id="fanout-test", config_summary={})
    state: dict[str, dict[str, object]] = {}
    flags = {"cancel": False, "pause": False}
    trace_ids: dict[str, str] = {}
    paused_trace_writes: list[int] = []

    deps = FanoutDeps(
        cfg=cfg,
        trace=trace,
        obs=obs,
        job_queue=job_queue,
        sample_state=state,
        max_workers=max_workers,
        effective_max_workers=lambda: max_workers,
        compute_await_deadline=lambda step: compute_await_deadline(cfg, step),
        trace_id_for=lambda sid: trace_ids.setdefault(sid, f"trace-{sid}"),
        checkpoint_sample=checkpoint,
        mark_sample_failed=lambda sid, step, err, tid: mark_sample_failed(
            # Nested bundle for the failure path only — shares the spies.
            FanoutDeps(
                cfg=cfg,
                trace=trace,
                obs=obs,
                job_queue=job_queue,
                sample_state=state,
                max_workers=max_workers,
                effective_max_workers=lambda: max_workers,
                compute_await_deadline=lambda step: None,
                trace_id_for=lambda s: trace_ids.setdefault(s, f"trace-{s}"),
                checkpoint_sample=checkpoint,
                mark_sample_failed=lambda *a: None,
                maybe_alert=maybe_alert,
                check_cancel_requested=lambda: flags["cancel"],
                cancel_requested=lambda: flags["cancel"],
                check_pause_requested=lambda: flags["pause"],
                write_paused_trace=lambda: paused_trace_writes.append(1),
            ),
            sid,
            step,
            err,
            tid,
        ),
        maybe_alert=maybe_alert,
        check_cancel_requested=lambda: flags["cancel"],
        cancel_requested=lambda: flags["cancel"],
        check_pause_requested=lambda: flags["pause"],
        write_paused_trace=lambda: paused_trace_writes.append(1),
    )
    spies = {
        "job_queue": job_queue,
        "obs": obs,
        "maybe_alert": maybe_alert,
        "checkpoint": checkpoint,
        "trace": trace,
        "state": state,
        "flags": flags,
        "paused_trace_writes": paused_trace_writes,
    }
    return deps, spies


class TestMarkSampleFailed:
    """The four recorded side-effects of the failure-marking path (issue #1570)."""

    def test_job_queue_mark_failed_called(
        self, variables_yml: Path, template_pkg: Path, outdir: Path
    ) -> None:
        cfg = _cfg(variables_yml, template_pkg, outdir)
        deps, spies = _deps(cfg, outdir)
        mark_sample_failed(deps, "sample_0", "RUN_OPENSTUDIO_SIM", RuntimeError("boom"), "t0")
        spies["job_queue"].mark_failed.assert_called_once_with(
            "sample_0_RUN_OPENSTUDIO_SIM", "boom"
        )

    def test_sample_state_keys(self, variables_yml: Path, template_pkg: Path, outdir: Path) -> None:
        cfg = _cfg(variables_yml, template_pkg, outdir)
        deps, spies = _deps(cfg, outdir)
        mark_sample_failed(deps, "sample_0", "EXTRACT_KPIS", RuntimeError("boom"), "t0")
        state = spies["state"]["sample_0"]
        assert state["extract_kpis_exit_code"] == 1
        assert state["extract_kpis_status"] == "failed"
        assert "boom" in state["error_summary"]

    def test_checkpoint_obs_and_alert_called(
        self, variables_yml: Path, template_pkg: Path, outdir: Path
    ) -> None:
        cfg = _cfg(variables_yml, template_pkg, outdir)
        deps, spies = _deps(cfg, outdir)
        mark_sample_failed(deps, "sample_0", "RUN_OPENSTUDIO_SIM", RuntimeError("boom"), "t0")
        spies["checkpoint"].assert_called_once_with("sample_0")
        spies["obs"].record_sample_status.assert_called_once_with(
            "sample_0", "failed", trace_id="t0"
        )
        spies["maybe_alert"].assert_called_once_with(
            "sample.failed",
            {
                "campaign_id": spies["trace"].campaign_id,
                "sample_id": "sample_0",
                "step": "RUN_OPENSTUDIO_SIM",
                "status": "failed",
                "error": "boom",
            },
        )


class TestComputeAwaitDeadline:
    """Direct derivations (issue #1566) — mirrors the Campaign-level tests."""

    def test_default_floor_uses_per_step_time_min(
        self, variables_yml: Path, template_pkg: Path, outdir: Path
    ) -> None:
        cfg = _cfg(variables_yml, template_pkg, outdir)
        assert compute_await_deadline(cfg, "EXTRACT_KPIS") == 600.0
        assert compute_await_deadline(cfg, "RUN_OPENSTUDIO_SIM") == 14400.0
        assert compute_await_deadline(cfg, "AGGREGATE_RESULTS") == 900.0

    def test_await_timeout_s_is_the_deadline(
        self, variables_yml: Path, template_pkg: Path, outdir: Path
    ) -> None:
        cfg = _cfg(variables_yml, template_pkg, outdir, await_timeout_s=1800.0)
        assert compute_await_deadline(cfg, "EXTRACT_KPIS") == 1800.0
        assert compute_await_deadline(cfg, "RUN_OPENSTUDIO_SIM") == 1800.0

    def test_byos_below_floor_keeps_per_step_floor(
        self, variables_yml: Path, template_pkg: Path, outdir: Path
    ) -> None:
        cfg = _cfg(variables_yml, template_pkg, outdir, byos_timeout_s=120.0)
        # max(600, 120) → 600: a small BYOS watchdog does not shrink the
        # per-step substrate-aligned floor.
        assert compute_await_deadline(cfg, "EXTRACT_KPIS") == 600.0

    def test_max_of_time_min_and_byos_when_await_unset(
        self, variables_yml: Path, template_pkg: Path, outdir: Path
    ) -> None:
        cfg = _cfg(variables_yml, template_pkg, outdir, byos_timeout_s=7200.0)
        assert compute_await_deadline(cfg, "EXTRACT_KPIS") == 7200.0
        assert compute_await_deadline(cfg, "RUN_OPENSTUDIO_SIM") == 14400.0


class TestSubmitAndAwaitAll:
    """The extracted wait loop via FanoutDeps (no Campaign)."""

    def test_ok_handle_marked_completed_and_on_success_called(
        self, variables_yml: Path, template_pkg: Path, outdir: Path
    ) -> None:
        cfg = _cfg(variables_yml, template_pkg, outdir)
        deps, spies = _deps(cfg, outdir)
        on_success = MagicMock()
        submit_and_await_all(
            deps,
            {"sample_0": (_make_ok_handle(Path("/tmp/r")), on_success)},
            "RUN_OPENSTUDIO_SIM",
        )
        on_success.assert_called_once()
        spies["job_queue"].mark_completed.assert_called_with("sample_0_RUN_OPENSTUDIO_SIM")
        spies["job_queue"].mark_failed.assert_not_called()
        spies["checkpoint"].assert_called_with("sample_0")

    def test_failed_handle_routes_through_mark_failed(
        self, variables_yml: Path, template_pkg: Path, outdir: Path
    ) -> None:
        cfg = _cfg(variables_yml, template_pkg, outdir)
        deps, spies = _deps(cfg, outdir)
        submit_and_await_all(
            deps,
            {"sample_0": (_make_failing_handle(RuntimeError("simulator crashed")), MagicMock())},
            "RUN_OPENSTUDIO_SIM",
        )
        spies["job_queue"].mark_failed.assert_called_once_with(
            "sample_0_RUN_OPENSTUDIO_SIM", "simulator crashed"
        )
        state = spies["state"]["sample_0"]
        assert state["run_openstudio_sim_status"] == "failed"

    def test_cancelled_error_routed_through_failure_path(
        self, variables_yml: Path, template_pkg: Path, outdir: Path
    ) -> None:
        cfg = _cfg(variables_yml, template_pkg, outdir)
        deps, spies = _deps(cfg, outdir)
        submit_and_await_all(
            deps,
            {
                "sample_0": (
                    _make_failing_handle(concurrent.futures.CancelledError("cancelled")),
                    MagicMock(),
                )
            },
            "RUN_OPENSTUDIO_SIM",
        )
        spies["job_queue"].mark_failed.assert_called_once_with(
            "sample_0_RUN_OPENSTUDIO_SIM", "cancelled"
        )
        spies["maybe_alert"].assert_called_once()

    def test_keyboard_interrupt_propagates(
        self, variables_yml: Path, template_pkg: Path, outdir: Path
    ) -> None:
        cfg = _cfg(variables_yml, template_pkg, outdir)
        deps, spies = _deps(cfg, outdir)
        with pytest.raises(KeyboardInterrupt):
            submit_and_await_all(
                deps,
                {
                    "sample_0": (
                        _make_failing_handle(KeyboardInterrupt("user Ctrl-C")),
                        MagicMock(),
                    )
                },
                "RUN_OPENSTUDIO_SIM",
            )
        # No failure accounting for genuine interrupts.
        spies["job_queue"].mark_failed.assert_not_called()

    def test_sequential_pause_raises_campaign_pause_requested(
        self, variables_yml: Path, template_pkg: Path, outdir: Path
    ) -> None:
        cfg = _cfg(variables_yml, template_pkg, outdir)
        deps, spies = _deps(cfg, outdir, max_workers=1)
        spies["flags"]["pause"] = True
        with pytest.raises(CampaignPauseRequested):
            submit_and_await_all(
                deps,
                {"sample_0": (_make_ok_handle(Path("/tmp/r")), MagicMock())},
                "RUN_OPENSTUDIO_SIM",
            )
        # Paused trace written at the raise site (issue #1537).
        assert spies["paused_trace_writes"]

    def test_sequential_cancel_after_loop_raises_keyboard_interrupt(
        self, variables_yml: Path, template_pkg: Path, outdir: Path
    ) -> None:
        cfg = _cfg(variables_yml, template_pkg, outdir)
        deps, spies = _deps(cfg, outdir, max_workers=1)

        # Cancel flips after the first sample completes so the loop
        # body ran once, then the post-loop sticky check fires.
        original_checkpoint = spies["checkpoint"]

        def _flip_cancel(sid: str) -> None:
            original_checkpoint(sid)
            spies["flags"]["cancel"] = True

        deps.checkpoint_sample = _flip_cancel  # type: ignore[method-assign]
        with pytest.raises(KeyboardInterrupt):
            submit_and_await_all(
                deps,
                {
                    "sample_0": (_make_ok_handle(Path("/tmp/r")), MagicMock()),
                    "sample_1": (_make_ok_handle(Path("/tmp/r")), MagicMock()),
                },
                "RUN_OPENSTUDIO_SIM",
            )

    def test_concurrent_mode_collects_all_results(
        self, variables_yml: Path, template_pkg: Path, outdir: Path
    ) -> None:
        cfg = _cfg(variables_yml, template_pkg, outdir)
        deps, spies = _deps(cfg, outdir, max_workers=4)
        submissions = {
            f"sample_{i}": (_make_ok_handle(Path(f"/tmp/r{i}")), MagicMock()) for i in range(8)
        }
        submit_and_await_all(deps, submissions, "EXTRACT_KPIS")
        assert spies["job_queue"].mark_completed.call_count == 8
        assert spies["job_queue"].mark_failed.call_count == 0

    def test_recovery_loop_resubmits_until_success(
        self, variables_yml: Path, template_pkg: Path, outdir: Path
    ) -> None:
        cfg = _cfg(variables_yml, template_pkg, outdir)
        deps, spies = _deps(cfg, outdir)

        recovery_manager = WorkerRecoveryManager(outdir)
        # Force every check_and_recover to admit recovery on attempt 1.
        recovery_manager.check_and_recover = MagicMock(return_value=(True, 1))  # type: ignore[method-assign]

        resubmits: list[str] = []

        def resubmit_callback(sid: str) -> Handle | None:
            resubmits.append(sid)
            return _make_ok_handle(Path(f"/tmp/recovered-{sid}"))

        submit_and_await_all(
            deps,
            {"sample_0": (_make_failing_handle(RuntimeError("worker died")), MagicMock())},
            "RUN_OPENSTUDIO_SIM",
            recovery_manager=recovery_manager,
            resubmit_callback=resubmit_callback,
        )
        assert resubmits == ["sample_0"]
        spies["job_queue"].mark_completed.assert_called_with("sample_0_RUN_OPENSTUDIO_SIM")

    def test_empty_submissions_noop(
        self, variables_yml: Path, template_pkg: Path, outdir: Path
    ) -> None:
        cfg = _cfg(variables_yml, template_pkg, outdir)
        deps, spies = _deps(cfg, outdir)
        submit_and_await_all(deps, {}, "RUN_OPENSTUDIO_SIM")
        spies["job_queue"].enqueue.assert_not_called()
