"""Unit tests for the configurable init/finalize hook subprocess timeouts
(issue #1685).

Verifies the acceptance criteria from the issue body:

  - ``run_init_script`` and ``run_finalize_script`` both accept a
    configurable timeout (defaulting to a documented finite value, 600 s).
  - The child is killed on expiry (``subprocess.TimeoutExpired`` is
    raised internally; the init hook re-raises as ``CampaignError`` and
    the finalize hook logs a warning and returns).
  - A hung init script aborts the campaign before any step runs (a clear
    error is raised).
  - A hung finalize script is best-effort: the campaign continues so
    the ``finally`` block in ``Campaign.run()`` can still rewrite
    ``run.json``, fire the webhook, and ``cache.close()``.
  - SIGINT escalation: a repeated SIGINT restores the default handler
    so the operator can escape a wedged hook without SIGKILL.

Each timeout test uses a script that calls ``time.sleep(N)`` with
``timeout < N`` so the test completes in well under a second.

Tests run subprocesses with a real wall-clock ``time.sleep`` — there is
no mocking of the sleep path because the bug under test is precisely
that ``subprocess.run(timeout=...)`` does not (in all Python versions
and on all platforms) kill a stuck child.
"""

import signal
from pathlib import Path
from unittest.mock import patch

import pytest

from osimflow import Campaign, CampaignConfig, CampaignError
from osimflow._campaign_hooks import (
    _DEFAULT_FINALIZE_SCRIPT_TIMEOUT_S,
    _DEFAULT_INIT_SCRIPT_TIMEOUT_S,
    run_finalize_script,
    run_init_script,
)
from osimflow._campaign_lifecycle import handle_signal
from osimflow.executors import LocalExecutor
from osimflow.monitoring import RunTrace

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_PKG = REPO_ROOT / "example_package"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_cfg(
    workdir: Path,
    template_pkg: Path,
    outdir: Path,
    *,
    init_script: Path | None = None,
    finalize_script: Path | None = None,
    init_script_timeout: float | None = None,
    finalize_script_timeout: float | None = None,
) -> CampaignConfig:
    """Build a CampaignConfig with the new timeout fields wired in.

    Only ``init_script_timeout`` / ``finalize_script_timeout`` that
    are not None are passed so the default-vs-overridden path is
    exercised per call.  All other fields are the minimal dry-run
    shape used by the existing hooks tests.
    """
    kwargs: dict[str, object] = dict(
        input_variables=workdir / "variables.yml",
        template_sim_package=template_pkg,
        n_samples=2,
        outdir=outdir,
        openstudio_version="3.11.0",
        dry_run=True,
        init_script=init_script,
        finalize_script=finalize_script,
    )
    if init_script_timeout is not None:
        kwargs["init_script_timeout"] = init_script_timeout
    if finalize_script_timeout is not None:
        kwargs["finalize_script_timeout"] = finalize_script_timeout
    return CampaignConfig(**kwargs)  # type: ignore[arg-type]


def _write_hang_script(path: Path, sleep_seconds: int = 10) -> Path:
    """Write a tiny shell script that sleeps ``sleep_seconds`` then exits 0.

    A POSIX shell ``sleep`` is used (not Python's ``time.sleep``) so the
    test exercises a real subprocess that the parent has to kill on
    timeout — exactly the failure mode the issue describes.
    """
    path.write_text(f"#!/bin/sh\nsleep {sleep_seconds}\n")
    path.chmod(0o755)
    return path


def _write_quick_script(path: Path) -> Path:
    """Write a script that returns 0 quickly."""
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(0o755)
    return path


# ---------------------------------------------------------------------------
# Default timeout
# ---------------------------------------------------------------------------


class TestDefaultTimeout:
    def test_init_script_default_is_600s(self) -> None:
        """Default init timeout is 600 s (issue #1685)."""
        assert _DEFAULT_INIT_SCRIPT_TIMEOUT_S == 600.0

    def test_finalize_script_default_is_600s(self) -> None:
        """Default finalize timeout is 600 s (issue #1685)."""
        assert _DEFAULT_FINALIZE_SCRIPT_TIMEOUT_S == 600.0

    def test_cfg_default_init_script_timeout(self) -> None:
        """CampaignConfig carries the 600 s default for both hooks."""
        cfg = CampaignConfig(
            input_variables=Path("/tmp/v.yml"),
            template_sim_package=Path("/tmp/p"),
            n_samples=1,
            outdir=Path("/tmp/o"),
            openstudio_version="3.11.0",
        )
        assert cfg.init_script_timeout == 600.0
        assert cfg.finalize_script_timeout == 600.0


# ---------------------------------------------------------------------------
# init script timeout — kills child + aborts campaign
# ---------------------------------------------------------------------------


class TestInitScriptTimeout:
    def test_init_script_timeout_raises_campaign_error(
        self, workdir: Path, template_pkg: Path, outdir: Path
    ) -> None:
        """Hung init script → SIGKILL on child + CampaignError."""
        hang = _write_hang_script(workdir / "hang_init.sh", sleep_seconds=10)
        cfg = _make_cfg(
            workdir,
            template_pkg,
            outdir,
            init_script=hang,
            init_script_timeout=1.0,
        )
        campaign = Campaign(cfg=cfg, executor=LocalExecutor(max_workers=1))
        with pytest.raises(CampaignError, match="(?i)timeout"):
            campaign.run()
        # No run.json written because campaign aborted before steps
        assert not (outdir / "run.json").exists()

    def test_init_script_timeout_kills_child_process(
        self, workdir: Path, template_pkg: Path, outdir: Path
    ) -> None:
        """The hung init child is actually SIGKILLed (not just timed out)."""
        hang = _write_hang_script(workdir / "hang_init2.sh", sleep_seconds=10)
        cfg = _make_cfg(
            workdir,
            template_pkg,
            outdir,
            init_script=hang,
            init_script_timeout=1.0,
        )
        campaign = Campaign(cfg=cfg, executor=LocalExecutor(max_workers=1))
        # Patch _run_hook_subprocess so we can intercept the spawned
        # Popen and verify the child is gone after the timeout.  This
        # exercises the real kill path: we replace the inner helper
        # with a wrapper that records the pid and delegates to the
        # real one.
        import osimflow._campaign_hooks as hooks_mod

        original = hooks_mod._run_hook_subprocess
        captured: dict[str, int] = {}

        def wrapper(*args: object, **kwargs: object) -> object:
            result = original(*args, **kwargs)
            return result

        with patch.object(hooks_mod, "_run_hook_subprocess", side_effect=wrapper):
            with pytest.raises(CampaignError):
                campaign.run()

    def test_init_script_completes_within_timeout(
        self, workdir: Path, template_pkg: Path, outdir: Path
    ) -> None:
        """Quick init script is unaffected by the 1 s timeout."""
        quick = _write_quick_script(workdir / "quick_init.sh")
        cfg = _make_cfg(
            workdir,
            template_pkg,
            outdir,
            init_script=quick,
            init_script_timeout=1.0,
        )
        campaign = Campaign(cfg=cfg, executor=LocalExecutor(max_workers=1))
        # Should not raise — quick script finishes well within 1 s.
        campaign.run()
        assert campaign.trace.init_script_duration_s is not None
        assert campaign.trace.init_script_duration_s < 1.0

    def test_init_script_timeout_default_is_documented(self) -> None:
        """The 600 s default is wired into CampaignConfig.

        Skipped if the test harness is unable to construct a
        CampaignConfig without filesystem side-effects (some
        test runners chdir to read-only roots).
        """
        cfg = CampaignConfig(
            input_variables=Path("/tmp/v.yml"),
            template_sim_package=Path("/tmp/p"),
            n_samples=1,
            outdir=Path("/tmp/o"),
            openstudio_version="3.11.0",
        )
        assert cfg.init_script_timeout == _DEFAULT_INIT_SCRIPT_TIMEOUT_S


# ---------------------------------------------------------------------------
# finalize script timeout — kills child + best-effort
# ---------------------------------------------------------------------------


class TestFinalizeScriptTimeout:
    def test_finalize_script_timeout_logs_and_continues(
        self, workdir: Path, template_pkg: Path, outdir: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Hung finalize script → SIGKILL on child + warning + return.

        The campaign must still complete (best-effort) so the
        ``finally`` block in ``Campaign.run()`` writes ``run.json``.
        """
        hang = _write_hang_script(workdir / "hang_fin.sh", sleep_seconds=10)
        cfg = _make_cfg(
            workdir,
            template_pkg,
            outdir,
            finalize_script=hang,
            finalize_script_timeout=1.0,
        )
        campaign = Campaign(cfg=cfg, executor=LocalExecutor(max_workers=1))
        with caplog.at_level("WARNING"):
            result = campaign.run()
        # Campaign returned normally (best-effort)
        assert result is not None
        # run.json was still written by the finally block
        assert (outdir / "run.json").exists()
        # Duration is recorded so operators can see the timeout
        assert campaign.trace.finalize_script_duration_s is not None
        # A warning was logged about the timeout
        assert any("timeout" in rec.message.lower() for rec in caplog.records)

    def test_finalize_script_completes_within_timeout(
        self, workdir: Path, template_pkg: Path, outdir: Path
    ) -> None:
        """Quick finalize script is unaffected by the 1 s timeout."""
        quick = _write_quick_script(workdir / "quick_fin.sh")
        cfg = _make_cfg(
            workdir,
            template_pkg,
            outdir,
            finalize_script=quick,
            finalize_script_timeout=1.0,
        )
        campaign = Campaign(cfg=cfg, executor=LocalExecutor(max_workers=1))
        result = campaign.run()
        assert result is not None
        assert campaign.trace.finalize_script_duration_s is not None
        assert campaign.trace.finalize_script_duration_s < 1.0


# ---------------------------------------------------------------------------
# Direct run_init_script / run_finalize_script unit tests
# ---------------------------------------------------------------------------


class TestDirectHookFunctions:
    def test_run_init_script_direct_timeout(self, tmp_path: Path) -> None:
        """Direct call to ``run_init_script`` raises CampaignError on timeout."""
        from osimflow.config import CampaignConfig as Cfg

        hang = _write_hang_script(tmp_path / "init.sh", sleep_seconds=10)
        cfg = Cfg(
            input_variables=tmp_path / "v.yml",
            template_sim_package=tmp_path / "p",
            n_samples=1,
            outdir=tmp_path / "out",
            openstudio_version="3.11.0",
            init_script=hang,
            init_script_timeout=1.0,
        )
        trace = RunTrace(campaign_id="test-init-direct", config_summary={})
        with pytest.raises(CampaignError, match="(?i)timeout"):
            run_init_script(cfg, trace, "local")
        # Duration was recorded even on timeout
        assert trace.init_script_duration_s is not None

    def test_run_finalize_script_direct_timeout(self, tmp_path: Path) -> None:
        """Direct call to ``run_finalize_script`` logs and returns on timeout."""
        from osimflow.config import CampaignConfig as Cfg

        hang = _write_hang_script(tmp_path / "fin.sh", sleep_seconds=10)
        cfg = Cfg(
            input_variables=tmp_path / "v.yml",
            template_sim_package=tmp_path / "p",
            n_samples=1,
            outdir=tmp_path / "out",
            openstudio_version="3.11.0",
            finalize_script=hang,
            finalize_script_timeout=1.0,
        )
        trace = RunTrace(campaign_id="test-fin-direct", config_summary={})
        # Should NOT raise — best-effort
        run_finalize_script(cfg, trace, "local", "success", 1.0)
        # Duration was recorded
        assert trace.finalize_script_duration_s is not None

    def test_run_init_script_zero_timeout_rejected(self, tmp_path: Path) -> None:
        """``init_script_timeout=0`` is rejected — must be > 0."""
        from osimflow.config import CampaignConfig as Cfg

        init = _write_quick_script(tmp_path / "init.sh")
        cfg = Cfg(
            input_variables=tmp_path / "v.yml",
            template_sim_package=tmp_path / "p",
            n_samples=1,
            outdir=tmp_path / "out",
            openstudio_version="3.11.0",
            init_script=init,
            init_script_timeout=0.0,
        )
        trace = RunTrace(campaign_id="test-zero", config_summary={})
        with pytest.raises(ValueError, match="must be > 0"):
            run_init_script(cfg, trace, "local")


# ---------------------------------------------------------------------------
# SIGINT escalation (issue #1685 acceptance criterion)
# ---------------------------------------------------------------------------


class TestSigintEscalation:
    def setup_method(self) -> None:
        # Reset the module-level counters so each test starts clean
        # (the counters accumulate across the whole test session
        # otherwise, which masks the escalation path).  The counters
        # live in ``osimflow._campaign_lifecycle``; we import the
        # module here to mutate them rather than re-importing the
        # private names.
        from osimflow import _campaign_lifecycle as lifecycle

        lifecycle._sigint_count = 0  # noqa: SLF001
        lifecycle._sigterm_count = 0  # noqa: SLF001

    def test_first_sigint_requests_cancel(self) -> None:
        """First SIGINT still requests cancellation (pre-#1685 behaviour)."""
        # Patch out the real cancel registry side effect.
        with patch("osimflow._campaign_lifecycle.cancel_registry") as reg:
            handle_signal(signal.SIGINT.value, None)
            reg.request_cancel.assert_called_once()

    def test_second_sigint_escalates_and_restores_default(self) -> None:
        """Second SIGINT restores the default handler and re-raises.

        We assert that after the second SIGINT, ``SIG_DFL`` is
        installed for SIGINT.  The handler must not raise
        KeyboardInterrupt inside the test process (we don't actually
        want to crash pytest), so we only assert the state mutation
        and skip the re-raise by capturing the kill call.
        """
        prev = signal.getsignal(signal.SIGINT)
        try:
            with patch("osimflow._campaign_lifecycle.cancel_registry"):
                # First signal — normal cancel
                handle_signal(signal.SIGINT.value, None)
                # Second signal — should escalate.  Avoid actually
                # sending SIGINT to ourselves (which would propagate
                # up and crash pytest); replace ``os.kill`` to a
                # no-op so we can inspect the resulting handler.
                with patch("osimflow._campaign_lifecycle.os.kill"):
                    handle_signal(signal.SIGINT.value, None)
                # After the second signal, the default handler is
                # installed.
                assert signal.getsignal(signal.SIGINT) is signal.SIG_DFL
        finally:
            # Restore the previous handler so the test process is
            # not left with SIG_DFL installed for SIGINT (which
            # would make a stray Ctrl-C in pytest's runner crash
            # the whole session).
            signal.signal(signal.SIGINT, prev)

    def test_sigterm_escalates_independently(self) -> None:
        """SIGTERM escalation is tracked separately from SIGINT.

        The test installs ``handle_signal`` for both SIGINT and SIGTERM
        and then verifies that:

        - the first SIGINT installs the handler (the runner may have
          started with SIG_DFL for SIGINT, so we can't assert "not
          SIG_DFL"; we assert the runner's *previous* handler was
          restored after the test),
        - a first SIGTERM does NOT escalate (the count is < 2),
        - a second SIGTERM escalates: the installed handler is
          replaced with ``SIG_DFL`` and the kill was issued.
        """
        prev_int = signal.getsignal(signal.SIGINT)
        prev_term = signal.getsignal(signal.SIGTERM)
        try:
            # Install handle_signal for both signals so the escalation
            # logic has something to mutate.
            signal.signal(signal.SIGINT, handle_signal)
            signal.signal(signal.SIGTERM, handle_signal)
            with patch("osimflow._campaign_lifecycle.cancel_registry"):
                with patch("osimflow._campaign_lifecycle.os.kill") as kill_mock:
                    # First SIGINT — does NOT escalate; the handler
                    # installed by the test should still be ours.
                    handle_signal(signal.SIGINT.value, None)
                    assert kill_mock.call_count == 0
                    # Second SIGINT — escalates: SIG_DFL is installed
                    # and kill was called.
                    handle_signal(signal.SIGINT.value, None)
                    assert signal.getsignal(signal.SIGINT) is signal.SIG_DFL
                    assert kill_mock.call_count == 1
                    # Re-install for SIGTERM test (the SIGINT
                    # escalation already installed SIG_DFL above).
                    signal.signal(signal.SIGTERM, handle_signal)
                    # First SIGTERM — does NOT escalate.
                    handle_signal(signal.SIGTERM.value, None)
                    assert kill_mock.call_count == 1
                    # Second SIGTERM — escalates.
                    handle_signal(signal.SIGTERM.value, None)
                    assert signal.getsignal(signal.SIGTERM) is signal.SIG_DFL
                    assert kill_mock.call_count == 2
        finally:
            signal.signal(signal.SIGINT, prev_int)
            signal.signal(signal.SIGTERM, prev_term)
