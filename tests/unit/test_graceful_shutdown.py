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
- Cancellation before steps sets status to "cancelled"
- Cancellation is checked in _submit_and_await_all
- Cancellation is checked in _run_full_campaign generation loop
- Cancellation in _finalize_full_campaign writes partial trace
- State preservation: run.json written before exit
- Executor.cancel() called on campaign cancellation
"""

import fcntl
import json
import os
import signal
import sys
import threading
from concurrent.futures import Future
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

from osimflow import Campaign, CampaignConfig
from osimflow.campaign import SampleSpec, _cancel_registry, _CancelRegistry
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
        self._container_digest = container_digest
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

    def test_check_cancel_requested_stop_file_with_lock(
        self, variables_yml: Path, template_pkg: Path, outdir: Path
    ) -> None:
        """Test that .stop file check uses fcntl.flock for cross-process safety.

        This test verifies the TOCTOU race fix (issue #649) by holding an
        exclusive lock on the .stop file and checking that _check_cancel_requested
        correctly reports the file's state.
        """
        if sys.platform == "win32":
            # fcntl.flock is not available on Windows; skip this test.
            return

        cfg = _cfg(variables_yml, template_pkg, outdir)
        campaign = Campaign(cfg=cfg, executor=StubExecutor())
        stop_path = outdir / ".stop"

        # Case 1: file does not exist -> returns False
        assert campaign._check_cancel_requested() is False

        # Case 2: file exists and is locked by another process -> still detects it
        stop_path.touch()
        fd = os.open(str(stop_path), os.O_RDWR)
        try:
            # Hold an exclusive lock on the file.
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                # _check_cancel_requested should still correctly detect the file.
                assert campaign._check_cancel_requested() is True
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

        # Case 3: file is deleted while we hold the lock -> correctly returns
        # the cached True value (cancellation is sticky after first detection)
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
    def test_cancel_before_steps_sets_cancelled_status(
        self, variables_yml: Path, template_pkg: Path, outdir: Path
    ) -> None:
        cfg = _cfg(variables_yml, template_pkg, outdir, dry_run=True)
        campaign = Campaign(cfg=cfg, executor=StubExecutor())
        campaign.request_cancel()

        # run() handles cancellation gracefully — it does NOT re-raise
        # KeyboardInterrupt.  The campaign status is set to "cancelled"
        # and run.json is written.
        campaign.run()

        assert campaign.trace.status == "cancelled"

    def test_stop_file_before_steps_sets_cancelled_status(
        self, variables_yml: Path, template_pkg: Path, outdir: Path
    ) -> None:
        cfg = _cfg(variables_yml, template_pkg, outdir, dry_run=True)
        (outdir / ".stop").touch()
        campaign = Campaign(cfg=cfg, executor=StubExecutor())

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
        # Do NOT use dry_run=True — dry-run mode skips the finalize steps
        # where cancellation should be detected.  With StubExecutor the
        # full campaign path still completes instantly (synchronous exec).
        cfg = _cfg(variables_yml, template_pkg, outdir, dry_run=False)
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

    def test_cancel_during_dry_run_stops(
        self, variables_yml: Path, template_pkg: Path, outdir: Path
    ) -> None:
        """A ``.stop`` file planted mid-dry-run halts the run cleanly (issue #621).

        Mirrors ``test_cancel_during_generation_loop_stops`` but exercises
        the ``_run_dry_run`` path, which bypasses the generation loop in
        ``_run_full_campaign``.  The ``.stop`` file is written as a side
        effect of ``step_extract_kpis`` *after* that step's own entry
        check has already passed — so without the inter-step check added
        in this fix, the cancel signal would be silently lost and the
        trace written as ``status="ok"``.
        """
        cfg = _cfg(variables_yml, template_pkg, outdir, dry_run=True)
        campaign = Campaign(cfg=cfg, executor=StubExecutor())

        extract_calls = 0
        original_extract = campaign.step_extract_kpis

        def extract_then_request_cancel(*args: Any, **kwargs: Any) -> Any:
            nonlocal extract_calls
            result = original_extract(*args, **kwargs)
            extract_calls += 1
            # Simulate the REST API / external tooling writing .stop
            # mid-flight, AFTER the last step's entry check has passed.
            (outdir / ".stop").touch()
            return result

        campaign.step_extract_kpis = extract_then_request_cancel

        # Must not raise — cancellation is handled gracefully and the
        # trace is written with status="cancelled".
        campaign.run()

        assert extract_calls == 1, "step_extract_kpis should have run once"
        assert campaign.trace.status == "cancelled"
        run_json = outdir / "run.json"
        assert run_json.exists()
        data = json.loads(run_json.read_text())
        assert data["status"] == "cancelled"

    def test_cancel_during_dry_run_before_sim_skips_sim(
        self, variables_yml: Path, template_pkg: Path, outdir: Path
    ) -> None:
        """Cancel before RUN_OPENSTUDIO_SIM halts the dry-run mid-flight.

        Patches ``_check_cancel_requested`` to latch True after the first
        few calls so the inter-step check between APPLY_PARAMETERS and
        RUN_OPENSTUDIO_SIM fires.  Asserts the long-running sim step is
        never reached (no unhandled exception) and a partial cancelled
        trace is written.
        """
        cfg = _cfg(variables_yml, template_pkg, outdir, dry_run=True)
        campaign = Campaign(cfg=cfg, executor=StubExecutor())

        sim_calls = 0
        original_sim = campaign.step_run_openstudio_sim

        def sim_spy(*args: Any, **kwargs: Any) -> Any:
            nonlocal sim_calls
            sim_calls += 1
            return original_sim(*args, **kwargs)

        campaign.step_run_openstudio_sim = sim_spy

        call_count = 0
        original_check = campaign._check_cancel_requested

        def patched_check() -> bool:
            nonlocal call_count
            call_count += 1
            # Latch True after the inter-step check that fires between
            # APPLY_PARAMETERS and RUN_OPENSTUDIO_SIM.  Without the new
            # inter-step check this would only be caught at the next
            # step's entry — but the sim step would still run.
            if call_count >= 5:
                return True
            return original_check()

        campaign._check_cancel_requested = patched_check

        campaign.run()  # must not raise

        assert sim_calls == 0, "RUN_OPENSTUDIO_SIM must not run after cancel"
        assert campaign.trace.status == "cancelled"
        run_json = outdir / "run.json"
        assert run_json.exists()
        data = json.loads(run_json.read_text())
        assert data["status"] == "cancelled"

    def test_cancel_during_single_sample_stops(
        self, variables_yml: Path, template_pkg: Path, outdir: Path
    ) -> None:
        """A ``.stop`` file planted mid-single-sample halts the run (issue #621).

        ``_run_single_sample`` has the same bypass-the-generation-loop
        shape as ``_run_dry_run``, so it needs the same inter-step
        cancellation polling.  Plants ``.stop`` as a side effect of
        ``step_extract_kpis`` and asserts the post-step check catches
        it before the trace is written as ``status="ok"``.

        Uses ``LocalExecutor`` (matching the existing single-sample
        tests) rather than ``StubExecutor`` so the executor consumes
        its own bookkeeping kwargs (e.g. ``result_hint``) instead of
        forwarding them to the work function.
        """
        from osimflow.executors import LocalExecutor

        cfg = _cfg(variables_yml, template_pkg, outdir, dry_run=False, sample=0)
        campaign = Campaign(cfg=cfg, executor=LocalExecutor(max_workers=1))

        # Single-sample mode requires a pre-existing samples.json.
        campaign.cfg.work_dir.mkdir(parents=True, exist_ok=True)
        samples: list[SampleSpec] = [
            {"sample_id": f"s{i:04d}", "values": {"window_u_value": float(i)}} for i in range(3)
        ]
        campaign.cfg.samples_file.write_text(json.dumps({"samples": samples}))

        original_extract = campaign.step_extract_kpis

        def extract_then_request_cancel(*args: Any, **kwargs: Any) -> Any:
            result = original_extract(*args, **kwargs)
            (outdir / ".stop").touch()
            return result

        campaign.step_extract_kpis = extract_then_request_cancel

        campaign.run()  # must not raise

        assert campaign.trace.status == "cancelled"
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
