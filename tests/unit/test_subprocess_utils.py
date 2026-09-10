"""Unit tests for ``osimflow._subprocess_utils`` (issue #1686).

Regression for issue #1686: ``terminate_active_subprocesses`` previously
sent SIGTERM to each registered child and returned immediately. A wedged
child that ignored SIGTERM (e.g. EnergyPlus unable to service the signal,
a process stuck in uninterruptible D-state on a dying NFS mount) stayed
alive forever, parking its ``run_subprocess.communicate()`` caller
indefinitely. The fix introduces a bounded grace period: SIGTERM is
issued first, the caller polls for graceful exit, and any child still
alive after the grace is escalated to SIGKILL. After SIGKILL, a bounded
wait releases the file descriptor so the orchestrator is never blocked
on a misbehaving child.

The tests verify:

* The default ``grace_period`` is 5.0 seconds (issue #1686 acceptance
  criterion: bounded kill escalation).
* A SIGTERM-ignoring child is escalated to SIGKILL after the grace
  period and reaped within ``kill_timeout``.
* A SIGTERM-responsive child (``sleep``) exits before the grace period
  without an unnecessary SIGKILL — the happy path is preserved.
* Already-exited children are skipped (no spurious signals).
* The call is a no-op when the registry is empty.
* Negative ``grace_period`` / ``kill_timeout`` / ``poll_interval`` are
  rejected.
* The owning thread that called ``run_subprocess`` is unblocked within
  ``grace_period + kill_timeout`` of the kill sweep — the regression
  symptom of issue #1686 was that the thread stayed parked in
  ``communicate()`` because the child was wedged.

The tests intentionally avoid ``time.sleep`` for synchronization — the
condition under test (child reaped, owning thread returned) is observable
via ``proc.poll()`` / ``thread.join()`` / ``proc.returncode``, so
wall-clock busy-waits are not needed and would only inflate the merge
gate's per-test budget.
"""

from __future__ import annotations

import signal
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from osimflow._subprocess_utils import (
    _ACTIVE_SUBPROCESSES,
    _ACTIVE_SUBPROCESSES_LOCK,
    DEFAULT_TERMINATE_GRACE_PERIOD_S,
    DEFAULT_TERMINATE_KILL_TIMEOUT_S,
    _register_active_subprocess,
    _unregister_active_subprocess,
    run_subprocess,
    terminate_active_subprocesses,
)

# A Python child that ignores SIGTERM and sleeps long enough that the
# test's grace period reliably elapses first.  We use Python (not bash
# ``trap``) for portability and to keep the test independent of any
# shell implementation detail.  ``signal.SIG_IGN`` is the canonical way
# to opt out of SIGTERM service on POSIX.
#
# The script writes ``"ready\n"`` to stdout AFTER installing the SIGTERM
# handler so the parent test can synchronise on the install point —
# ``subprocess.Popen`` returns as soon as the child is forked+exec'd,
# not when the child reaches its ``time.sleep``.  Without this
# synchronisation the parent can race the handler install and deliver
# SIGTERM before it is installed, which would kill the child with the
# default SIGTERM action (and the test would observe a SIGTERM
# returncode instead of the expected SIGKILL).  ``readline()`` on the
# parent's stdout blocks until the child has flushed "ready" — at that
# point the handler is installed and ``time.sleep`` is the next
# syscall.
_IGNORES_SIGTERM_PY: str = (
    "import signal, sys, time\n"
    "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    "sys.stdout.write('ready\\n')\n"
    "sys.stdout.flush()\n"
    "time.sleep(60)\n"
)


def _spawn_sigterm_ignoring_child() -> subprocess.Popen[str]:
    """Spawn a Python child that ignores SIGTERM and sleeps for 60 s.

    The returned proc has ``stdout=PIPE`` so the test can read the
    ``"ready\n"`` sentinel that the child writes after installing its
    SIGTERM handler.  The sentinel closes the parent-child race where
    SIGTERM would otherwise be delivered before the handler is
    installed — see ``_IGNORES_SIGTERM_PY`` for the rationale.
    """
    return subprocess.Popen(  # noqa: S603 — argv fully controlled
        [sys.executable, "-c", _IGNORES_SIGTERM_PY],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        bufsize=1,  # line-buffered; the "ready\n" line must reach us promptly
    )


def _await_sigterm_handler_installed(proc: subprocess.Popen[str]) -> None:
    """Block until the SIGTERM-ignoring child has installed its handler.

    Reads the ``"ready\\n"`` sentinel the child writes after
    ``signal.signal(SIGTERM, SIG_IGN)``.  Without this synchronisation
    the test races the handler install and may deliver SIGTERM before
    it is installed (the default action would kill the child with
    SIGTERM, masking the SIGKILL escalation we are trying to test).
    """
    line = proc.stdout.readline()
    assert line.strip() == "ready", (
        f"unexpected child output: {line!r} — handler-install sentinel missing"
    )


def _spawn_sigterm_responsive_child() -> subprocess.Popen[str]:
    """Spawn ``sleep 60`` — POSIX sleep exits cleanly on SIGTERM."""
    return subprocess.Popen(  # noqa: S603 — argv fully controlled
        ["sleep", "60"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
    )


@pytest.fixture
def _isolated_registry() -> None:
    """Snapshot the subprocess registry before/after each test.

    The tests manually register/unregister procs against the current
    thread ident, so we guarantee no leakage between tests.
    """
    ident = threading.get_ident()
    with _ACTIVE_SUBPROCESSES_LOCK:
        before = dict(_ACTIVE_SUBPROCESSES)
    try:
        yield
    finally:
        with _ACTIVE_SUBPROCESSES_LOCK:
            leaked = {k: v for k, v in _ACTIVE_SUBPROCESSES.items() if k not in before}
            for key, proc in leaked.items():
                if proc.poll() is None:
                    proc.kill()
                    try:
                        proc.wait(timeout=2.0)
                    except subprocess.TimeoutExpired:
                        pass
                _ACTIVE_SUBPROCESSES.pop(key, None)
            # The test's own thread-ident entry must have been cleared
            # by the test's finally clause.
            assert ident not in _ACTIVE_SUBPROCESSES, (
                "test leaked an entry in _ACTIVE_SUBPROCESSES; "
                "_unregister_active_subprocess() must be called in finally"
            )


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


class TestDefaults:
    """The public defaults match the issue #1686 acceptance criterion."""

    def test_default_grace_period_is_five_seconds(self) -> None:
        """Issue #1686 spec: default grace period is 5 s."""
        assert DEFAULT_TERMINATE_GRACE_PERIOD_S == 5.0

    def test_default_kill_timeout_is_two_seconds(self) -> None:
        """Defense-in-depth: 2 s is generous for the post-SIGKILL reap."""
        assert DEFAULT_TERMINATE_KILL_TIMEOUT_S == 2.0

    def test_default_signature(self) -> None:
        """The function exposes grace_period as the first positional arg.

        ``kill_timeout`` and ``poll_interval`` are keyword-only because
        they are implementation details callers should rarely need to
        override — the grace period is the user-tunable knob.
        """
        import inspect

        sig = inspect.signature(terminate_active_subprocesses)
        params = sig.parameters
        assert "grace_period" in params
        assert params["grace_period"].default == DEFAULT_TERMINATE_GRACE_PERIOD_S
        assert "kill_timeout" in params
        assert params["kill_timeout"].kind is inspect.Parameter.KEYWORD_ONLY
        assert "poll_interval" in params
        assert params["poll_interval"].kind is inspect.Parameter.KEYWORD_ONLY


# ---------------------------------------------------------------------------
# SIGTERM → grace → SIGKILL escalation (issue #1686 acceptance criterion)
# ---------------------------------------------------------------------------


class TestSigkillEscalation:
    """A SIGTERM-resistant child must be SIGKILL'd within the grace + kill bound."""

    def test_sigterm_ignoring_child_is_sigkilled_after_grace(
        self,
        _isolated_registry: None,
    ) -> None:
        """SIGTERM-ignoring child is escalated to SIGKILL after grace period.

        Regression for issue #1686: pre-fix, the child never died and
        ``run_subprocess.communicate()`` stayed parked forever. The fix
        sends SIGTERM, polls for ``grace_period`` seconds, and escalates
        to SIGKILL. After SIGKILL the kernel reaps the child in
        milliseconds; ``proc.returncode`` is ``-SIGKILL`` (negative
        signal number).
        """
        proc = _spawn_sigterm_ignoring_child()
        _await_sigterm_handler_installed(proc)
        _register_active_subprocess(proc)
        try:
            assert proc.poll() is None, "child exited before terminate"

            grace = 0.5
            killed = terminate_active_subprocesses(grace_period=grace)

            assert killed == 1, f"expected exactly 1 SIGTERM to be issued, got {killed}"
            # After SIGKILL the child should be reaped promptly. We give
            # a generous bound (grace + 2.0s for the post-SIGKILL wait
            # + scheduling slack) so the assertion fails only on real
            # regressions, not on loaded CI runners.
            try:
                proc.wait(timeout=grace + 2.0)
            except subprocess.TimeoutExpired:
                pytest.fail(
                    f"child was not reaped within {grace + 2.0}s after "
                    f"terminate_active_subprocesses(grace_period={grace}) — "
                    "SIGKILL escalation did not take effect"
                )

            # Negative returncode on Unix indicates killed by signal.
            # SIGTERM would have given -15, but the child ignores SIGTERM,
            # so the only way it dies is SIGKILL = signal 9.
            assert proc.returncode is not None and proc.returncode < 0, (
                f"expected killed-by-signal returncode < 0, got {proc.returncode}"
            )
            assert abs(proc.returncode) == signal.SIGKILL, (
                f"expected killed by SIGKILL (signal {signal.SIGKILL}), "
                f"got signal {abs(proc.returncode)} — SIGTERM was not "
                "escalated to SIGKILL"
            )
        finally:
            if proc.poll() is None:
                proc.kill()
                try:
                    proc.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    pass
            _unregister_active_subprocess()

    def test_sigterm_responsive_child_exits_before_grace(
        self,
        _isolated_registry: None,
    ) -> None:
        """A child that responds to SIGTERM (``sleep``) exits before the grace
        period elapses; no SIGKILL is sent.

        Verifies the SIGTERM happy path is preserved after the
        escalation fix: a well-behaved child does not get an
        unnecessary SIGKILL (which would lose any unflushed output
        buffers and bypass graceful shutdown handlers).
        """
        proc = _spawn_sigterm_responsive_child()
        _register_active_subprocess(proc)
        try:
            assert proc.poll() is None, "child exited before terminate"

            grace = 2.0
            killed = terminate_active_subprocesses(grace_period=grace)

            assert killed == 1
            # SIGTERM should kill sleep in milliseconds — wait for the
            # reap using a generous bound (well within grace).
            try:
                proc.wait(timeout=grace)
            except subprocess.TimeoutExpired:
                pytest.fail(
                    "SIGTERM did not terminate sleep within the grace period — happy path is broken"
                )

            assert proc.returncode is not None and proc.returncode < 0
            # SIGTERM = signal 15. If we got SIGKILL (signal 9) here the
            # grace period was too short or the polling was wrong.
            assert abs(proc.returncode) == signal.SIGTERM, (
                f"expected killed by SIGTERM (signal {signal.SIGTERM}), "
                f"got signal {abs(proc.returncode)} — graceful path was "
                "broken"
            )
        finally:
            if proc.poll() is None:
                proc.kill()
                try:
                    proc.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    pass
            _unregister_active_subprocess()

    def test_run_subprocess_caller_unblocks_within_bound(
        self,
        tmp_path: Path,
        _isolated_registry: None,
    ) -> None:
        """End-to-end: the thread that called ``run_subprocess`` returns
        within ``grace_period + kill_timeout`` of ``terminate_active_subprocesses``.

        This is the user-visible symptom of issue #1686: the
        ``run_subprocess`` caller was parked in ``communicate()``
        indefinitely because the child ignored SIGTERM. The fix unblocks
        the caller by escalating to SIGKILL, which causes
        ``communicate()`` to return.
        """
        # Tell the SIGTERM-ignoring child to write its "ready\n"
        # sentinel to a side-channel file (the production ``run_subprocess``
        # redirects stdout/stderr to log files).  We pass it via an env
        # var so the test stays independent of file-handle orderings.
        sentinel_path = tmp_path / "sentinel.txt"
        script = _IGNORES_SIGTERM_PY.replace(
            "sys.stdout.write('ready\\n')\nsys.stdout.flush()\n",
            ("open(__import__('os').environ['OSIMFLOW_TEST_READY'], 'w')\\\n    .write('ready')\n"),
        )

        result: dict[str, object] = {}
        started = threading.Event()

        def _run_child() -> None:
            started.set()
            try:
                proc = run_subprocess(
                    [sys.executable, "-c", script],
                    stdout_path=tmp_path / "stdout.log",
                    stderr_path=tmp_path / "stderr.log",
                    env={"OSIMFLOW_TEST_READY": str(sentinel_path)},
                )
                result["returncode"] = proc.returncode
            except BaseException as exc:  # noqa: BLE001 — recorded, not raised
                result["error"] = exc

        t = threading.Thread(target=_run_child, daemon=True)
        t.start()

        try:
            assert started.wait(timeout=10), "child thread did not start"
            # Wait for the child to install its SIGTERM handler. The
            # child writes "ready" to ``sentinel_path`` immediately
            # after ``signal.signal(SIGTERM, SIG_IGN)`` returns. We poll
            # the file's existence with a generous bound — this is test
            # setup synchronisation, not the property under test.
            import time as _time

            sentinel_deadline = _time.monotonic() + 5.0
            while not sentinel_path.exists() and _time.monotonic() < sentinel_deadline:
                _time.sleep(0.02)
            assert sentinel_path.exists(), (
                "child did not reach the SIGTERM-handler install sentinel "
                "within 5 s — race between handler install and SIGTERM "
                "delivery would mask the SIGKILL escalation we want to test"
            )

            grace = 0.5
            kill_timeout = 1.0
            terminate_active_subprocesses(
                grace_period=grace,
                kill_timeout=kill_timeout,
            )

            # The caller thread must be unblocked within the bound. The
            # bound here is the grace + kill_timeout + a generous slack
            # for thread scheduling on loaded CI runners.
            t.join(timeout=grace + kill_timeout + 2.0)
            assert not t.is_alive(), (
                "run_subprocess caller stayed parked after SIGKILL "
                "escalation — SIGKILL did not unblock communicate()"
            )

            assert "error" not in result, (
                f"run_subprocess raised unexpectedly: {result.get('error')!r}"
            )
            returncode = result.get("returncode")
            assert isinstance(returncode, int) and returncode < 0, (
                f"expected killed-by-signal returncode < 0, got {returncode!r}"
            )
            assert abs(returncode) == signal.SIGKILL, (
                f"expected killed by SIGKILL (signal {signal.SIGKILL}), "
                f"got signal {abs(returncode)}"
            )
        finally:
            if t.is_alive():
                # Best-effort cleanup; should not happen on a healthy
                # runner.
                t.join(timeout=2.0)


# ---------------------------------------------------------------------------
# Edge cases / non-regression
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Idempotence and edge cases."""

    def test_terminate_does_nothing_when_no_procs(self) -> None:
        """No registered procs → returns 0, no exceptions."""
        with _ACTIVE_SUBPROCESSES_LOCK:
            # Snapshot any pre-existing live procs (should be none under
            # test isolation); only assert if a proc is somehow present
            # and already dead — the contract is "no live procs".
            for proc in _ACTIVE_SUBPROCESSES.values():
                assert proc.poll() is not None, "test precondition: no live procs in registry"
        assert terminate_active_subprocesses(grace_period=0.1) == 0

    def test_already_exited_proc_is_skipped(
        self,
        _isolated_registry: None,
    ) -> None:
        """A proc that exited before the call is skipped — no signal is sent.

        Otherwise ``proc.terminate()`` on an already-reaped child is a
        no-op but the function would still count it as a SIGTERM
        issued, overstating the kill count.
        """
        proc = subprocess.Popen(  # noqa: S603 — argv fully controlled
            [sys.executable, "-c", "pass"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        proc.wait()  # reap immediately
        _register_active_subprocess(proc)
        try:
            assert proc.poll() is not None, "child should already be exited"
            killed = terminate_active_subprocesses(grace_period=0.1)
            assert killed == 0, "already-exited proc should not be signalled"
        finally:
            _unregister_active_subprocess()

    def test_already_exited_proc_does_not_block_sweep(
        self,
        _isolated_registry: None,
    ) -> None:
        """Mix of live + dead procs: only live procs are signalled.

        The dead proc must not consume a poll cycle or block the sweep.
        """
        live = _spawn_sigterm_ignoring_child()
        _await_sigterm_handler_installed(live)
        dead = subprocess.Popen(  # noqa: S603
            [sys.executable, "-c", "pass"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        dead.wait()
        # The registry is keyed by thread ident; registering two procs
        # from the same thread would overwrite.  Inject both entries
        # directly via the lock — the ``_register_active_subprocess``
        # one-proc-per-thread invariant is a property of ``run_subprocess``,
        # not of the registry itself, and ``terminate_active_subprocesses``
        # iterates over all entries regardless of ident.
        ident_live = threading.get_ident()
        ident_dead = ident_live + 1  # synthetic second thread ident
        with _ACTIVE_SUBPROCESSES_LOCK:
            _ACTIVE_SUBPROCESSES[ident_live] = live
            _ACTIVE_SUBPROCESSES[ident_dead] = dead
        try:
            grace = 0.3
            killed = terminate_active_subprocesses(grace_period=grace)
            assert killed == 1, f"expected exactly 1 SIGTERM (live only), got {killed}"
            # Live child must be SIGKILL'd.
            try:
                live.wait(timeout=grace + 1.5)
            except subprocess.TimeoutExpired:
                pytest.fail("live child not reaped within bound")
            assert live.returncode is not None and live.returncode < 0
            assert abs(live.returncode) == signal.SIGKILL
        finally:
            if live.poll() is None:
                live.kill()
                try:
                    live.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    pass
            with _ACTIVE_SUBPROCESSES_LOCK:
                _ACTIVE_SUBPROCESSES.pop(ident_live, None)
                _ACTIVE_SUBPROCESSES.pop(ident_dead, None)


# ---------------------------------------------------------------------------
# Argument validation
# ---------------------------------------------------------------------------


class TestArgumentValidation:
    """Invalid grace / kill / poll arguments are rejected up front."""

    def test_negative_grace_period_rejected(self) -> None:
        with pytest.raises(ValueError, match="grace_period"):
            terminate_active_subprocesses(grace_period=-1.0)

    def test_negative_kill_timeout_rejected(self) -> None:
        with pytest.raises(ValueError, match="kill_timeout"):
            terminate_active_subprocesses(grace_period=0.1, kill_timeout=-1.0)

    def test_zero_poll_interval_rejected(self) -> None:
        with pytest.raises(ValueError, match="poll_interval"):
            terminate_active_subprocesses(grace_period=0.1, poll_interval=0)

    def test_negative_poll_interval_rejected(self) -> None:
        with pytest.raises(ValueError, match="poll_interval"):
            terminate_active_subprocesses(grace_period=0.1, poll_interval=-0.1)
