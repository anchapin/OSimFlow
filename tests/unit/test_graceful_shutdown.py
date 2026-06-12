"""Unit tests for graceful shutdown and campaign cancellation (issue #255).

Covers:
- _cancel_requested flag is initially False
- request_cancel() sets the flag to True (thread-safe)
- _check_cancel_requested() checks both the flag and .stop file
- .stop file detection triggers cancellation
- Signal handler registration (SIGINT/SIGTERM)
- Signal handler calls request_cancel()
- Campaign.run() registers signal handlers and _cancel_registry
- Campaign.run() restores signal handlers on exit
- Cancellation check raises KeyboardInterrupt before steps
- Cancellation is checked in _submit_and_await_all
- Cancellation is checked in _run_full_campaign generation loop
- Cancellation in _finalize_full_campaign writes partial trace
- State preservation: run.json written before exit
"""

import json
import signal
import threading
from concurrent.futures import Future
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from osimflow import Campaign, CampaignConfig
from osimflow.campaign import _cancel_registry, _CancelRegistry
from osimflow.executors import BaseExecutor, Handle


class StubExecutor(BaseExecutor):
    """Minimal executor that runs functions synchronously for testing."""

    name = "stub"

    def __init__(self) -> None:
        self._cancel_called = False

    def submit(
        self,
        fn: Any,
        *args: Any,
        name: str = "task",
        cpus: int = 1,
        memory_mb: int = 1024,
        time_min: int = 60,
        container: str | None = None,
        **kwargs: Any,
    ) -> Handle:
        fut: Future[Any] = Future()
        try:
            result = fn(*args, **kwargs)
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
        "n_samples": 2,
        "outdir": outdir,
        "openstudio_version": "3.11.0",
        "dry_run": True,
    }
    defaults.update(overrides)
    return CampaignConfig(**defaults)


# ---------------------------------------------------------------------------
# _CancelRegistry tests
# ---------------------------------------------------------------------------


class TestCancelRegistry:
    def test_register_and_request_cancel(self) -> None:
        registry = _CancelRegistry()
        mock_campaign = MagicMock()
        mock_campaign.request_cancel = MagicMock()

        registry.register(mock_campaign)
        registry.request_cancel()

        mock_campaign.request_cancel.assert_called_once()

    def test_clear(self) -> None:
        registry = _CancelRegistry()
        mock_campaign = MagicMock()
        mock_campaign.request_cancel = MagicMock()

        registry.register(mock_campaign)
        registry.clear()
        registry.request_cancel()

        mock_campaign.request_cancel.assert_not_called()

    def test_request_cancel_no_campaign(self) -> None:
        registry = _CancelRegistry()
        registry.request_cancel()  # Should not raise

    def test_thread_safety(self) -> None:
        registry = _CancelRegistry()
        mock_campaign = MagicMock()
        mock_campaign.request_cancel = MagicMock()
        registry.register(mock_campaign)

        errors: list[BaseException] = []

        def call_request_cancel() -> None:
            try:
                for _ in range(100):
                    registry.request_cancel()
            except BaseException as e:
                errors.append(e)

        threads = [threading.Thread(target=call_request_cancel) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        # 10 threads x 100 iterations = 1000 calls
        assert mock_campaign.request_cancel.call_count == 1000


# ---------------------------------------------------------------------------
# Campaign cancellation flag tests
# ---------------------------------------------------------------------------


class TestCampaignCancelFlag:
    def test_cancel_flag_initially_false(
        self, variables_yml: Path, template_pkg: Path, outdir: Path
    ) -> None:
        cfg = _cfg(variables_yml, template_pkg, outdir)
        campaign = Campaign(cfg=cfg, executor=StubExecutor())
        assert campaign._cancel_requested is False

    def test_request_cancel_sets_flag(
        self, variables_yml: Path, template_pkg: Path, outdir: Path
    ) -> None:
        cfg = _cfg(variables_yml, template_pkg, outdir)
        campaign = Campaign(cfg=cfg, executor=StubExecutor())
        campaign.request_cancel()
        assert campaign._cancel_requested is True

    def test_request_cancel_idempotent(
        self, variables_yml: Path, template_pkg: Path, outdir: Path
    ) -> None:
        cfg = _cfg(variables_yml, template_pkg, outdir)
        campaign = Campaign(cfg=cfg, executor=StubExecutor())
        campaign.request_cancel()
        campaign.request_cancel()
        assert campaign._cancel_requested is True

    def test_check_cancel_requested_flag(
        self, variables_yml: Path, template_pkg: Path, outdir: Path
    ) -> None:
        cfg = _cfg(variables_yml, template_pkg, outdir)
        campaign = Campaign(cfg=cfg, executor=StubExecutor())
        assert campaign._check_cancel_requested() is False
        campaign.request_cancel()
        assert campaign._check_cancel_requested() is True

    def test_check_cancel_requested_stop_file(
        self, variables_yml: Path, template_pkg: Path, outdir: Path
    ) -> None:
        cfg = _cfg(variables_yml, template_pkg, outdir)
        campaign = Campaign(cfg=cfg, executor=StubExecutor())
        assert campaign._check_cancel_requested() is False
        (outdir / ".stop").touch()
        assert campaign._check_cancel_requested() is True

    def test_check_cancel_requested_stop_file_deleted(
        self, variables_yml: Path, template_pkg: Path, outdir: Path
    ) -> None:
        cfg = _cfg(variables_yml, template_pkg, outdir)
        campaign = Campaign(cfg=cfg, executor=StubExecutor())
        (outdir / ".stop").touch()
        assert campaign._check_cancel_requested() is True
        (outdir / ".stop").unlink()
        # Once cancelled, flag stays True (cancellation is sticky)
        assert campaign._check_cancel_requested() is True


# ---------------------------------------------------------------------------
# Signal handler tests
# ---------------------------------------------------------------------------


class TestSignalHandlers:
    def test_setup_signal_handlers(
        self, variables_yml: Path, template_pkg: Path, outdir: Path
    ) -> None:
        cfg = _cfg(variables_yml, template_pkg, outdir)
        campaign = Campaign(cfg=cfg, executor=StubExecutor())

        prev_int = signal.getsignal(signal.SIGINT)
        prev_term = signal.getsignal(signal.SIGTERM)

        campaign._setup_signal_handlers()

        try:
            new_int = signal.getsignal(signal.SIGINT)
            new_term = signal.getsignal(signal.SIGTERM)
            assert new_int is not prev_int
            assert new_term is not prev_term
        finally:
            campaign._restore_signal_handlers()

    def test_restore_signal_handlers(
        self, variables_yml: Path, template_pkg: Path, outdir: Path
    ) -> None:
        cfg = _cfg(variables_yml, template_pkg, outdir)
        campaign = Campaign(cfg=cfg, executor=StubExecutor())

        prev_int = signal.getsignal(signal.SIGINT)
        prev_term = signal.getsignal(signal.SIGTERM)

        campaign._setup_signal_handlers()
        campaign._restore_signal_handlers()

        assert signal.getsignal(signal.SIGINT) is prev_int
        assert signal.getsignal(signal.SIGTERM) is prev_term

    def test_handle_signal_requests_cancel(
        self, variables_yml: Path, template_pkg: Path, outdir: Path
    ) -> None:
        cfg = _cfg(variables_yml, template_pkg, outdir)
        campaign = Campaign(cfg=cfg, executor=StubExecutor())
        _cancel_registry.register(campaign)

        try:
            campaign._handle_signal(signal.SIGINT, None)
            assert campaign._cancel_requested is True
        finally:
            _cancel_registry.clear()


# ---------------------------------------------------------------------------
# Integration: cancellation during campaign run
# ---------------------------------------------------------------------------


class TestCampaignCancellation:
    def test_cancel_before_steps_raises_keyboard_interrupt(
        self, variables_yml: Path, template_pkg: Path, outdir: Path
    ) -> None:
        cfg = _cfg(variables_yml, template_pkg, outdir, dry_run=True)
        campaign = Campaign(cfg=cfg, executor=StubExecutor())
        campaign.request_cancel()

        with pytest.raises(KeyboardInterrupt, match="cancellation requested"):
            campaign.run()

        assert campaign.trace.status == "cancelled"

    def test_stop_file_before_steps_raises_keyboard_interrupt(
        self, variables_yml: Path, template_pkg: Path, outdir: Path
    ) -> None:
        cfg = _cfg(variables_yml, template_pkg, outdir, dry_run=True)
        (outdir / ".stop").touch()
        campaign = Campaign(cfg=cfg, executor=StubExecutor())

        with pytest.raises(KeyboardInterrupt, match="cancellation requested"):
            campaign.run()

        assert campaign.trace.status == "cancelled"

    def test_cancel_during_generation_loop_stops(
        self, variables_yml: Path, template_pkg: Path, outdir: Path
    ) -> None:
        cfg = _cfg(variables_yml, template_pkg, outdir, dry_run=True, max_generations=5)
        campaign = Campaign(cfg=cfg, executor=StubExecutor())

        call_count = 0
        original_check = campaign._check_cancel_requested

        def patched_check() -> bool:
            nonlocal call_count
            call_count += 1
            if call_count >= 3:
                return True
            return original_check()

        campaign._check_cancel_requested = patched_check

        campaign.run()

        assert campaign.trace.status == "cancelled"

    def test_cancel_in_finalize_writes_partial_trace(
        self, variables_yml: Path, template_pkg: Path, outdir: Path
    ) -> None:
        cfg = _cfg(variables_yml, template_pkg, outdir, dry_run=True)
        campaign = Campaign(cfg=cfg, executor=StubExecutor())

        call_count = 0
        original_check = campaign._check_cancel_requested

        def patched_check() -> bool:
            nonlocal call_count
            call_count += 1
            if call_count >= 10:
                return True
            return original_check()

        campaign._check_cancel_requested = patched_check

        campaign.run()

        assert campaign.trace.status == "cancelled"
        run_json = outdir / "run.json"
        assert run_json.exists()
        data = json.loads(run_json.read_text())
        assert data["status"] == "cancelled"

    def test_cancel_during_fanout_stops_early(
        self, variables_yml: Path, template_pkg: Path, outdir: Path
    ) -> None:
        cfg = _cfg(variables_yml, template_pkg, outdir, dry_run=True)
        campaign = Campaign(cfg=cfg, executor=StubExecutor())

        check_count = 0
        original_check = campaign._check_cancel_requested

        def patched_check() -> bool:
            nonlocal check_count
            check_count += 1
            if check_count >= 2:
                return True
            return original_check()

        campaign._check_cancel_requested = patched_check

        campaign.run()

        assert campaign.trace.status == "cancelled"

    def test_run_json_written_on_cancel(
        self, variables_yml: Path, template_pkg: Path, outdir: Path
    ) -> None:
        cfg = _cfg(variables_yml, template_pkg, outdir, dry_run=True)
        campaign = Campaign(cfg=cfg, executor=StubExecutor())
        campaign.request_cancel()

        campaign.run()

        run_json = outdir / "run.json"
        assert run_json.exists()
        data = json.loads(run_json.read_text())
        assert data["status"] == "cancelled"


# ---------------------------------------------------------------------------
# Executor cancel integration
# ---------------------------------------------------------------------------


class TestExecutorCancel:
    def test_executor_cancel_called_on_cancel(
        self, variables_yml: Path, template_pkg: Path, outdir: Path
    ) -> None:
        cfg = _cfg(variables_yml, template_pkg, outdir, dry_run=True)
        executor = StubExecutor()
        campaign = Campaign(cfg=cfg, executor=executor)
        campaign.request_cancel()

        campaign.run()

        assert executor._cancel_called is True
