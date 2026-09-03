"""Unit tests for the soft-pause lifecycle (issues #553 / #1537).

Issue #1537: pause was signaled by raising ``KeyboardInterrupt``, so
``run()``'s single cancellation handler misclassified every pause —
overwriting ``run.json`` status ``"paused"`` → ``"cancelled"``, setting
``finished_at``, cancelling active jobs, and reporting "cancelled" to
the registry / webhook.  The downstream contract broke completely:
``osimflow resume`` refuses to resume unless status == "paused".

The fix introduces a dedicated control-flow signal
(``CampaignPauseRequested``) so pause keeps the documented semantics
(``osimflow/_campaign_lifecycle.py``): running samples complete
normally, ``run.json`` stays ``"paused"`` with no ``finished_at``, and
active jobs are NOT cancelled — while genuine ``KeyboardInterrupt``
(SIGINT / Ctrl-C) still maps to cancellation.

Run via::

    .venv/bin/pytest tests/unit/test_pause_lifecycle.py -v
"""

from __future__ import annotations

import json
from concurrent.futures import Future
from pathlib import Path
from typing import Any

from osimflow import Campaign, CampaignConfig
from osimflow.__main__ import _build_parser, _cmd_resume
from osimflow.executors import BaseExecutor, Handle


class StubExecutor(BaseExecutor):
    """Minimal executor that runs functions synchronously for testing."""

    name = "stub"

    def __init__(self) -> None:
        self._cancel_called = False
        self.submit_count = 0

    def submit(
        self,
        fn: Any,
        *args: Any,
        name: str = "task",
        cpus: int = 1,
        memory_mb: int = 1024,
        time_min: int = 60,
        container: str | None = None,
        container_digest: str | None = None,
        openstudio_version: str | None = None,
        result_hint: Any = None,
        remote_command: str | None = None,
        result_transport_mode: str | None = None,
        result_storage_backend: str | None = None,
        result_storage_bucket: str | None = None,
        result_storage_prefix: str | None = None,
        result_storage_endpoint: str | None = None,
        variables_json: str | None = None,
        stdout_path: Any = None,
        stderr_path: Any = None,
        max_retries: int | None = None,
        worker_id: str | None = None,
        **kwargs: Any,
    ) -> Handle:
        self.submit_count += 1
        fut: Future[Any] = Future()
        try:
            result = fn(*args)
            fut.set_result(result)
        except Exception as e:
            fut.set_exception(e)
        return Handle(
            job_id=f"job-{name}",
            _future=fut,
            worker_id="local",
            worker_ip="localhost",
        )

    def cancel(self) -> None:
        self._cancel_called = True

    def shutdown(self) -> None:
        pass


def _cfg(
    variables_yml: Path,
    template_pkg: Path,
    outdir: Path,
    **overrides: Any,
) -> CampaignConfig:
    defaults: dict[str, Any] = {
        "input_variables": variables_yml,
        "template_sim_package": template_pkg,
        "n_samples": 4,
        "outdir": outdir,
        "openstudio_version": "3.11.0",
        "dry_run": False,
    }
    defaults.update(overrides)
    return CampaignConfig(**defaults)


def _run_json(outdir: Path) -> dict[str, Any]:
    return json.loads((outdir / "run.json").read_text())


def _pause_after_first_checkpoint(campaign: Campaign, outdir: Path) -> list[str]:
    """Patch ``_checkpoint_sample`` to write ``.pause`` after the first sample.

    Returns the list of sample ids that were checkpointed before the
    pause landed.  Mirrors the REST API / ``osimflow pause`` writing the
    flag mid-fan-out.
    """
    checkpointed: list[str] = []
    original = campaign._checkpoint_sample

    def checkpoint_then_pause(sid: str) -> None:
        original(sid)
        checkpointed.append(sid)
        if len(checkpointed) == 1:
            (outdir / ".pause").touch()

    campaign._checkpoint_sample = checkpoint_then_pause  # type: ignore[method-assign]
    return checkpointed


# ---------------------------------------------------------------------------
# Pause keeps paused semantics (issue #1537 core)
# ---------------------------------------------------------------------------


class TestPauseKeepsPausedStatus:
    def test_pause_before_apply_keeps_paused_status(
        self, variables_yml: Path, template_pkg: Path, outdir: Path
    ) -> None:
        """A ``.pause`` file present at run() start pauses (not cancels).

        The pause fires at the APPLY_PARAMETERS pre-step check — the
        first pause checkpoint in the DAG.
        """
        executor = StubExecutor()
        cfg = _cfg(variables_yml, template_pkg, outdir)
        campaign = Campaign(cfg=cfg, executor=executor)
        (outdir / ".pause").touch()

        result = campaign.run()

        assert result["status"] == "paused"
        assert campaign.trace.status == "paused"
        # finished_at must NOT be set — pause is non-terminal.
        assert campaign.trace.finished_at is None
        data = _run_json(outdir)
        assert data["status"] == "paused"
        assert data["finished_at"] is None
        assert data["paused_at"] is not None
        # Active jobs must NOT be cancelled on pause.
        assert executor._cancel_called is False
        # run() must not consume the .pause file — `osimflow resume` owns it.
        assert (outdir / ".pause").exists()

    def test_pause_mid_fanout_sequential_keeps_paused_status(
        self, variables_yml: Path, template_pkg: Path, outdir: Path
    ) -> None:
        """Pause mid-fan-out (sequential path) raises CampaignPauseRequested.

        The sequential ``_submit_and_await_all`` loop raises the pause
        signal between sample awaits; run() must keep status "paused",
        not flip it to "cancelled".
        """
        executor = StubExecutor()
        cfg = _cfg(variables_yml, template_pkg, outdir)
        campaign = Campaign(cfg=cfg, executor=executor, max_workers=1)
        checkpointed = _pause_after_first_checkpoint(campaign, outdir)

        result = campaign.run()

        assert checkpointed, "at least one sample should checkpoint before the pause"
        assert result["status"] == "paused"
        assert campaign.trace.status == "paused"
        assert campaign.trace.finished_at is None
        data = _run_json(outdir)
        assert data["status"] == "paused"
        assert data["finished_at"] is None
        assert executor._cancel_called is False

    def test_pause_mid_fanout_concurrent_keeps_paused_status(
        self, variables_yml: Path, template_pkg: Path, outdir: Path
    ) -> None:
        """Pause mid-fan-out (concurrent path) pauses at the next pre-step check.

        The concurrent ``_submit_and_await_all`` loop breaks without
        raising; the following step's pre-check raises the pause signal.
        """
        executor = StubExecutor()
        cfg = _cfg(variables_yml, template_pkg, outdir)
        campaign = Campaign(cfg=cfg, executor=executor, max_workers=2)
        _pause_after_first_checkpoint(campaign, outdir)

        result = campaign.run()

        assert result["status"] == "paused"
        assert campaign.trace.status == "paused"
        data = _run_json(outdir)
        assert data["status"] == "paused"
        assert data["finished_at"] is None
        assert executor._cancel_called is False

    def test_pause_before_aggregation_finalizes_paused(
        self, variables_yml: Path, template_pkg: Path, outdir: Path
    ) -> None:
        """Pause landing after the last fan-out must not complete as success.

        ``_finalize_full_campaign`` checks pause at entry (mirroring its
        cancellation check) so a campaign paused during/after
        EXTRACT_KPIS finalizes as "paused" instead of "success".
        """
        executor = StubExecutor()
        cfg = _cfg(variables_yml, template_pkg, outdir)
        campaign = Campaign(cfg=cfg, executor=executor)

        original_extract = campaign.step_extract_kpis

        def extract_then_pause(*args: Any, **kwargs: Any) -> Any:
            result = original_extract(*args, **kwargs)
            (outdir / ".pause").touch()
            return result

        campaign.step_extract_kpis = extract_then_pause  # type: ignore[method-assign]

        result = campaign.run()

        assert result["status"] == "paused"
        assert campaign.trace.status == "paused"
        data = _run_json(outdir)
        assert data["status"] == "paused"
        assert data["finished_at"] is None
        assert executor._cancel_called is False


# ---------------------------------------------------------------------------
# Registry / webhook / finalize-hook reporting
# ---------------------------------------------------------------------------


class TestPauseReporting:
    def test_registry_and_webhook_report_paused(
        self, variables_yml: Path, template_pkg: Path, outdir: Path
    ) -> None:
        """The finally block reports "paused" (not "cancelled") downstream."""
        executor = StubExecutor()
        cfg = _cfg(variables_yml, template_pkg, outdir)
        campaign = Campaign(cfg=cfg, executor=executor)

        registry_statuses: list[str] = []
        webhook_statuses: list[tuple[str, float]] = []
        campaign._update_registry_status = registry_statuses.append  # type: ignore[method-assign]

        def fake_webhook(status: str, elapsed_s: float) -> None:
            webhook_statuses.append((status, elapsed_s))

        campaign._maybe_fire_webhook = fake_webhook  # type: ignore[method-assign]

        (outdir / ".pause").touch()
        campaign.run()

        assert registry_statuses == ["paused"]
        assert [s for s, _ in webhook_statuses] == ["paused"]


# ---------------------------------------------------------------------------
# Resume flow (issue #553 acceptance criterion for #1537)
# ---------------------------------------------------------------------------


class TestResumeFlowAfterPause:
    def test_pause_then_resume_then_rerun_continues_from_cache(
        self, variables_yml: Path, template_pkg: Path, outdir: Path
    ) -> None:
        """End-to-end: pause mid-fan-out → resume → re-run from cache replay.

        1. Pause mid-APPLY-fan-out: run.json status stays "paused".
        2. ``osimflow resume`` accepts it (status == "paused") and
           removes the ``.pause`` flag.
        3. Re-running the same outdir completes with status "success"
           and replay hits the cache for the samples finished pre-pause.
        """
        executor1 = StubExecutor()
        cfg1 = _cfg(variables_yml, template_pkg, outdir)
        campaign1 = Campaign(cfg=cfg1, executor=executor1, max_workers=1)
        checkpointed = _pause_after_first_checkpoint(campaign1, outdir)

        result1 = campaign1.run()
        assert result1["status"] == "paused"
        assert _run_json(outdir)["status"] == "paused"

        # The pre-#1537 bug: status flipped to "cancelled" here, so
        # _cmd_resume refused with "campaign is not paused".
        parser = _build_parser()
        args = parser.parse_args(["resume", str(outdir)])
        assert _cmd_resume(args) == 0
        assert not (outdir / ".pause").exists()

        executor2 = StubExecutor()
        cfg2 = _cfg(variables_yml, template_pkg, outdir)
        campaign2 = Campaign(cfg=cfg2, executor=executor2, max_workers=1)
        result2 = campaign2.run()

        assert result2.get("status") != "cancelled"
        assert campaign2.trace.status == "success"
        data = _run_json(outdir)
        assert data["status"] == "success"
        assert data["finished_at"] is not None
        # Cache replay: the samples completed before the pause are hits
        # in the re-run (their work is not re-executed).
        assert campaign2.cache.get_stats().hits >= len(checkpointed)

    def test_resume_rejects_cancelled_campaign(
        self, variables_yml: Path, template_pkg: Path, outdir: Path
    ) -> None:
        """``osimflow resume`` still refuses a cancelled campaign (regression pin).

        Cancel and pause must stay distinct: only status "paused" is
        resumable.
        """
        cfg = _cfg(variables_yml, template_pkg, outdir)
        campaign = Campaign(cfg=cfg, executor=StubExecutor())
        campaign.request_cancel()
        campaign.run()

        assert _run_json(outdir)["status"] == "cancelled"
        parser = _build_parser()
        args = parser.parse_args(["resume", str(outdir)])
        assert _cmd_resume(args) == 1


# ---------------------------------------------------------------------------
# Cancel semantics preserved
# ---------------------------------------------------------------------------


class TestCancelStillCancels:
    def test_real_keyboard_interrupt_still_cancels(
        self, variables_yml: Path, template_pkg: Path, outdir: Path
    ) -> None:
        """A genuine KeyboardInterrupt (SIGINT / Ctrl-C) still cancels.

        The dedicated pause signal must not weaken real-interrupt
        handling: status "cancelled", finished_at set, active jobs
        cancelled.
        """
        executor = StubExecutor()
        cfg = _cfg(variables_yml, template_pkg, outdir)
        campaign = Campaign(cfg=cfg, executor=executor)

        original_generate = campaign.step_generate_samples

        def generate_then_interrupt(*args: Any, **kwargs: Any) -> Any:
            original_generate(*args, **kwargs)
            raise KeyboardInterrupt("real SIGINT")

        campaign.step_generate_samples = generate_then_interrupt  # type: ignore[method-assign]

        result = campaign.run()

        assert result["status"] == "cancelled"
        assert campaign.trace.status == "cancelled"
        assert campaign.trace.finished_at is not None
        assert executor._cancel_called is True
        assert _run_json(outdir)["status"] == "cancelled"

    def test_cancel_during_fanout_still_cancels(
        self, variables_yml: Path, template_pkg: Path, outdir: Path
    ) -> None:
        """Cancel mid-fan-out keeps its pre-#1537 behaviour (issue #255)."""
        executor = StubExecutor()
        cfg = _cfg(variables_yml, template_pkg, outdir)
        campaign = Campaign(cfg=cfg, executor=executor, max_workers=1)

        call_count = 0
        original_check = campaign._check_cancel_requested

        def patched_check() -> bool:
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                return True
            return original_check()

        campaign._check_cancel_requested = patched_check  # type: ignore[method-assign]

        campaign.run()

        # This cancel fires in the generation loop / finalize cancel
        # branch, whose result dict has no "status" key — the observable
        # contract is trace.status + run.json (mirrors
        # test_cancel_during_fanout_stops in test_graceful_shutdown.py).
        # (Executor job-cancel is only invoked on the KeyboardInterrupt
        # unwind — see test_real_keyboard_interrupt_still_cancels.)
        assert campaign.trace.status == "cancelled"
        assert _run_json(outdir)["status"] == "cancelled"
