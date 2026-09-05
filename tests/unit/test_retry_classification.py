"""Unit tests for the work-function failure classifier (issue #1568).

Covers :func:`osimflow.work.classify_failure` and its companion
:func:`osimflow.work.run_with_retry` integration:

* Deliberate terminations (SIGTERM / SIGKILL) and ``TimeoutExpired`` are
  classified ``NON_TRANSIENT`` — the sample fails exactly once, no retry,
  no backoff sleep.
* Legitimate network failures (``ConnectionError``, ``TimeoutError``,
  ``ConnectionResetError``, ``urllib3``-style timeout) remain
  ``TRANSIENT`` and are retried with exponential backoff.
* The fallback message-scan catches resource-busy / I/O patterns raised
  by libraries that do not surface proper exception types.
* The curated subprocess exit-code set excludes SIGTERM-positive and
  ``-1`` (issue #1568) so a ``-1`` / ``15`` returncode is never
  re-executed.

These tests replace the legacy ``_is_transient_error`` substring-scan
behaviour with type-precise matching.
"""

from __future__ import annotations

import signal
import subprocess
from pathlib import Path

import pytest

from osimflow.work import (
    RetryDecision,
    TransientError,
    _is_signal_killed_subprocess,
    _is_transient_error,
    classify_failure,
    run_with_retry,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
class _FlakyState:
    """Mutable call counter shared between the test and the flaky function."""

    def __init__(self) -> None:
        self.calls = 0


def _make_always(exc: BaseException) -> tuple[object, _FlakyState]:
    """Return ``(callable, state)`` where ``callable`` always raises *exc*."""
    state = _FlakyState()

    def _callable(*_args: object, **_kwargs: object) -> Path:
        state.calls += 1
        raise exc

    return _callable, state


def _make_called_process_error(returncode: int) -> subprocess.CalledProcessError:
    """Build a ``CalledProcessError`` with the given *returncode*."""
    return subprocess.CalledProcessError(returncode=returncode, cmd=["openstudio.cli"])


# ===========================================================================
# ClassifyFailure — direct unit tests
# ===========================================================================
class TestClassifyFailureSubprocessTimeoutExpired:
    """``subprocess.TimeoutExpired`` → ``NON_TRANSIENT`` (issue #1534, #1568)."""

    def test_classify_returns_non_transient(self) -> None:
        exc = subprocess.TimeoutExpired(cmd=["openstudio.cli"], timeout=600.0)
        assert classify_failure(exc) == RetryDecision.NON_TRANSIENT

    def test_classify_returns_non_transient_even_with_timeout_in_message(self) -> None:
        """The exception's message contains ``"timed out"`` but the decision
        is driven by type, not by the message substring (issue #1568).
        """
        exc = subprocess.TimeoutExpired(
            cmd=["openstudio.cli"], timeout=600.0, output=b"...timed out after 600s..."
        )
        assert classify_failure(exc) == RetryDecision.NON_TRANSIENT

    def test_back_compat_boolean_is_false(self) -> None:
        exc = subprocess.TimeoutExpired(cmd=["openstudio.cli"], timeout=600.0)
        assert _is_transient_error(exc) is False


class TestClassifyFailureSignalKill:
    """Subprocess killed by SIGTERM/SIGKILL → ``NON_TRANSIENT`` (issue #1568)."""

    @pytest.mark.parametrize(
        "signal_number",
        [signal.SIGTERM, signal.SIGKILL],
        ids=["SIGTERM", "SIGKILL"],
    )
    def test_negative_returncode_for_signal_is_non_transient(self, signal_number: int) -> None:
        """Python's subprocess surfaces a signal kill as
        ``returncode == -signal_number`` (issue #1568).
        """
        exc = _make_called_process_error(returncode=-signal_number)
        assert classify_failure(exc) == RetryDecision.NON_TRANSIENT

    @pytest.mark.parametrize(
        "signal_number",
        [signal.SIGTERM, signal.SIGKILL],
        ids=["SIGTERM-positive", "SIGKILL-positive"],
    )
    def test_positive_returncode_for_signal_is_non_transient(self, signal_number: int) -> None:
        """Defensive coverage for the rare positive form."""
        exc = _make_called_process_error(returncode=signal_number)
        assert classify_failure(exc) == RetryDecision.NON_TRANSIENT

    def test_sigkill_classified_by_helper(self) -> None:
        exc = _make_called_process_error(returncode=-signal.SIGKILL)
        assert _is_signal_killed_subprocess(exc) is True

    def test_normal_exit_code_not_classified_as_signal_kill(self) -> None:
        exc = _make_called_process_error(returncode=2)
        assert _is_signal_killed_subprocess(exc) is False

    def test_helper_returns_false_for_non_called_process_error(self) -> None:
        """``_is_signal_killed_subprocess`` must only match ``CalledProcessError``."""
        assert _is_signal_killed_subprocess(RuntimeError("killed")) is False
        assert (
            _is_signal_killed_subprocess(subprocess.TimeoutExpired(cmd=["x"], timeout=1.0)) is False
        )


class TestClassifyFailureRemovedExitCodes:
    """``_TRANSIENT_EXIT_CODES`` no longer contains ``-1`` or ``15`` (issue #1568)."""

    def test_negative_one_is_non_transient(self) -> None:
        """``-1`` previously implied transient; now deterministic → propagate."""
        exc = _make_called_process_error(returncode=-1)
        assert classify_failure(exc) == RetryDecision.NON_TRANSIENT

    def test_sigterm_positive_form_is_non_transient(self) -> None:
        """``15`` previously implied transient; SIGTERM-positive must not retry."""
        exc = _make_called_process_error(returncode=15)
        assert classify_failure(exc) == RetryDecision.NON_TRANSIENT

    def test_curated_transient_exit_code_still_transient(self) -> None:
        """Regression guard — the curated ``2, 4, 5, 6, 11, 12, 24, 25, 26, 27, 28``
        set must remain classified transient (issue #1568 narrowed, did not
        empty, the set).
        """
        for returncode in (2, 4, 5, 6, 11, 12, 24, 25, 26, 27, 28):
            exc = _make_called_process_error(returncode=returncode)
            assert classify_failure(exc) == RetryDecision.TRANSIENT, (
                f"returncode={returncode} unexpectedly non-transient"
            )


class TestClassifyFailureTransientError:
    """An explicit ``TransientError`` is always transient."""

    def test_classify_returns_transient(self) -> None:
        """Work layer signals retryable explicitly — must be type-precise transient
        regardless of message content.
        """
        exc = TransientError("container health check stale")
        assert classify_failure(exc) == RetryDecision.TRANSIENT

    def test_classify_returns_transient_for_empty_message(self) -> None:
        exc = TransientError("")
        assert classify_failure(exc) == RetryDecision.TRANSIENT


class TestClassifyFailureNetworkExceptions:
    """Legitimate network failures remain transient (type-precise)."""

    def test_connection_error_is_transient(self) -> None:
        assert classify_failure(ConnectionError("peer closed")) == RetryDecision.TRANSIENT

    def test_connection_reset_error_is_transient(self) -> None:
        assert (
            classify_failure(ConnectionResetError("connection reset by peer"))
            == RetryDecision.TRANSIENT
        )

    def test_connection_refused_error_is_transient(self) -> None:
        assert (
            classify_failure(ConnectionRefusedError("connection refused"))
            == RetryDecision.TRANSIENT
        )

    def test_timeout_error_is_transient(self) -> None:
        """Generic ``TimeoutError`` (urllib, socket, stdlib) → transient."""
        assert classify_failure(TimeoutError("read timed out")) == RetryDecision.TRANSIENT

    def test_socket_timeout_subclass_is_transient(self) -> None:
        """``socket.timeout`` is a ``TimeoutError`` alias (issue #1568)."""
        import socket

        # ``socket.timeout`` is an alias of ``TimeoutError`` since Python 3.10
        # (ruff UP032). The classifier must treat it as a network-transient
        # exception — covered by the type-precise ``TimeoutError`` branch.
        assert classify_failure(TimeoutError("timed out")) == RetryDecision.TRANSIENT
        # ``socket.timeout`` itself is the same class as ``TimeoutError``.
        assert socket.timeout is TimeoutError


class TestClassifyFailureFallbackMarkers:
    """Fallback message-scan catches libraries without proper exception types."""

    @pytest.mark.parametrize(
        "msg",
        [
            "Connection refused by upstream",
            "connection reset by peer",
            "network is unreachable",
            "Resource busy",
            "too many open files",
            "No space left on device / disk full",
            "I/O error on closed file",
            "Temporary failure in name resolution",
        ],
    )
    def test_message_marker_is_transient(self, msg: str) -> None:
        """Resource-busy / I-O / connection-refused patterns → transient."""
        assert classify_failure(RuntimeError(msg)) == RetryDecision.TRANSIENT

    def test_bare_timeout_message_no_longer_drives_decision(self) -> None:
        """Bare ``"timeout"`` / ``"timed out"`` substring must NOT trigger
        transient (issue #1568) — ``TimeoutError`` type-precise matching
        covers the legitimate cases.
        """
        assert (
            classify_failure(RuntimeError("operation timeout exceeded"))
            == RetryDecision.NON_TRANSIENT
        )
        assert (
            classify_failure(RuntimeError("connection timed out after 30s"))
            == RetryDecision.NON_TRANSIENT
        )

    def test_unrecognised_message_is_non_transient(self) -> None:
        """Default → NON_TRANSIENT. Silent retries have masked real bugs in the past."""
        assert (
            classify_failure(RuntimeError("user supplied an invalid template"))
            == RetryDecision.NON_TRANSIENT
        )
        assert classify_failure(ValueError("bad input")) == RetryDecision.NON_TRANSIENT


# ===========================================================================
# run_with_retry integration — SIGTERM / TimeoutExpired fail exactly once
# ===========================================================================
class TestRunWithRetryNonTransientKills:
    """``run_with_retry`` must invoke the work function exactly once for
    non-transient kills (issue #1568 acceptance criterion).
    """

    def test_sigtermed_subprocess_fails_once_no_retry(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A SIGTERMed subprocess fails exactly once — no backoff, no retry."""
        sleep_calls: list[float] = []
        monkeypatch.setattr("osimflow.work.time.sleep", sleep_calls.append)
        sigterm_exc = _make_called_process_error(returncode=-signal.SIGTERM)
        callable_, state = _make_always(sigterm_exc)

        with pytest.raises(subprocess.CalledProcessError):
            run_with_retry(callable_, max_retries=3, base_delay=1.0)  # type: ignore[arg-type]

        assert state.calls == 1, (
            f"SIGTERMed subprocess was retried {state.calls - 1} extra times — issue #1568"
        )
        assert sleep_calls == []

    def test_sigkilled_subprocess_fails_once_no_retry(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A SIGKILLed subprocess fails exactly once — no retry."""
        sleep_calls: list[float] = []
        monkeypatch.setattr("osimflow.work.time.sleep", sleep_calls.append)
        sigkill_exc = _make_called_process_error(returncode=-signal.SIGKILL)
        callable_, state = _make_always(sigkill_exc)

        with pytest.raises(subprocess.CalledProcessError):
            run_with_retry(callable_, max_retries=3, base_delay=1.0)  # type: ignore[arg-type]

        assert state.calls == 1
        assert sleep_calls == []

    def test_subprocess_timeout_expired_fails_once_no_retry(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``subprocess.TimeoutExpired`` fails exactly once (issue #1534 + #1568)."""
        sleep_calls: list[float] = []
        monkeypatch.setattr("osimflow.work.time.sleep", sleep_calls.append)
        timeout_exc = subprocess.TimeoutExpired(cmd=["openstudio.cli"], timeout=600.0)
        callable_, state = _make_always(timeout_exc)

        with pytest.raises(subprocess.TimeoutExpired):
            run_with_retry(callable_, max_retries=3, base_delay=0.0)  # type: ignore[arg-type]

        assert state.calls == 1
        assert sleep_calls == []

    def test_negative_one_returncode_fails_once_no_retry(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Returncode ``-1`` (Python's "process never started" / fork failure)
        was previously classified transient and retried — issue #1568 closes
        that path.
        """
        sleep_calls: list[float] = []
        monkeypatch.setattr("osimflow.work.time.sleep", sleep_calls.append)
        exc = _make_called_process_error(returncode=-1)
        callable_, state = _make_always(exc)

        with pytest.raises(subprocess.CalledProcessError):
            run_with_retry(callable_, max_retries=3, base_delay=1.0)  # type: ignore[arg-type]

        assert state.calls == 1
        assert sleep_calls == []


class TestRunWithRetryTransientRetries:
    """``run_with_retry`` MUST still retry genuine transient failures."""

    def test_connection_error_is_retried_with_backoff(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A ``ConnectionError`` is retried up to ``max_retries`` times
        with exponential backoff (regression guard — issue #1568 must not
        accidentally close the legitimate-transient path).
        """
        sleep_calls: list[float] = []
        monkeypatch.setattr("osimflow.work.time.sleep", sleep_calls.append)
        exc = ConnectionError("peer closed")
        callable_, state = _make_always(exc)

        with pytest.raises(ConnectionError):
            run_with_retry(callable_, max_retries=3, base_delay=0.5)  # type: ignore[arg-type]

        # max_retries + 1 total attempts (3 retries → 4 attempts).
        assert state.calls == 4
        # 3 sleeps between 4 attempts; jitter means each is in [0, expected].
        assert len(sleep_calls) == 3
        for actual, exp in zip(sleep_calls, [0.5, 1.0, 2.0], strict=True):
            assert 0 <= actual <= exp, f"expected jittered sleep in [0, {exp}], got {actual}"

    def test_timeout_error_is_retried_with_backoff(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A real ``TimeoutError`` (non-subprocess) is retried."""
        sleep_calls: list[float] = []
        monkeypatch.setattr("osimflow.work.time.sleep", sleep_calls.append)
        exc = TimeoutError("read timed out")
        callable_, state = _make_always(exc)

        with pytest.raises(TimeoutError):
            run_with_retry(callable_, max_retries=2, base_delay=0.0)  # type: ignore[arg-type]

        assert state.calls == 3  # 2 retries + 1
        assert len(sleep_calls) == 2

    def test_connection_refused_string_marker_is_retried(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression guard — a ``RuntimeError`` whose message trips the
        fallback ``"connection refused"`` marker is still retried.
        """
        sleep_calls: list[float] = []
        monkeypatch.setattr("osimflow.work.time.sleep", sleep_calls.append)
        exc = RuntimeError("Connection refused by upstream proxy")
        callable_, state = _make_always(exc)

        with pytest.raises(RuntimeError):
            run_with_retry(callable_, max_retries=2, base_delay=0.0)  # type: ignore[arg-type]

        assert state.calls == 3
        assert len(sleep_calls) == 2

    def test_custom_transient_class_via_transient_error_is_retried(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``TransientError`` is type-precise transient — connectivity blip
        simulated via the framework's own retry signal class.
        """
        sleep_calls: list[float] = []
        monkeypatch.setattr("osimflow.work.time.sleep", sleep_calls.append)
        exc = TransientError("simulated connectivity blip")
        callable_, state = _make_always(exc)

        with pytest.raises(TransientError):
            run_with_retry(callable_, max_retries=2, base_delay=0.0)  # type: ignore[arg-type]

        assert state.calls == 3
        assert len(sleep_calls) == 2

    def test_resource_busy_string_marker_is_retried(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Regression guard — ``"resource busy"`` string marker still retries."""
        sleep_calls: list[float] = []
        monkeypatch.setattr("osimflow.work.time.sleep", sleep_calls.append)
        exc = RuntimeError("Resource busy, try again later")
        callable_, state = _make_always(exc)

        with pytest.raises(RuntimeError):
            run_with_retry(callable_, max_retries=2, base_delay=0.0)  # type: ignore[arg-type]

        assert state.calls == 3
        assert len(sleep_calls) == 2
