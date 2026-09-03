"""Unit tests for ``osimflow.work.run_with_retry``.

Direct coverage of the exponential-backoff retry helper used by every
per-step work function. See issue #1008.
"""

import subprocess
from pathlib import Path

import pytest

from osimflow.work import run_with_retry


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
class _TransientError(RuntimeError):
    """Marker exception whose message trips ``_is_transient_error``."""


class _NonTransientError(ValueError):
    """Marker exception that ``_is_transient_error`` should treat as fatal."""


def _transient(msg: str = "network timeout") -> _TransientError:
    return _TransientError(msg)


def _transient_subprocess(returncode: int) -> subprocess.CalledProcessError:
    return subprocess.CalledProcessError(returncode=returncode, cmd=["x"])


def _timeout_expired(timeout: float = 600.0) -> subprocess.TimeoutExpired:
    """A subprocess wall-clock timeout kill (issue #1534).

    ``str()`` of this exception contains "timed out", which historically
    tripped the message-marker list in ``_is_transient_error`` and caused
    legitimate slow simulations to be re-executed ``max_retries`` times.
    """
    return subprocess.TimeoutExpired(cmd=["openstudio.cli", "run"], timeout=timeout)


def _flaky(n_failures: int, return_value: Path):
    """Return a callable that fails ``n_failures`` times then succeeds."""
    state = {"calls": 0}

    def _callable(*_args: object, **_kwargs: object) -> Path:
        state["calls"] += 1
        if state["calls"] <= n_failures:
            raise _transient(f"call {state['calls']} timed out")
        return return_value

    return _callable, state


# ===========================================================================
# TestRunWithRetry
# ===========================================================================
class TestRunWithRetry:
    """Direct tests for ``osimflow.work.run_with_retry`` (issue #1008)."""

    # ------------------------------------------------------------------
    # Zero retries — single attempt, no sleep
    # ------------------------------------------------------------------
    def test_zero_retries_runs_once_then_propagates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``max_retries=0`` skips the retry loop entirely: one attempt."""
        sleep_calls: list[float] = []
        monkeypatch.setattr("osimflow.work.time.sleep", sleep_calls.append)

        def _always_fails() -> Path:
            raise _transient("connection refused")

        with pytest.raises(_TransientError, match="connection refused"):
            run_with_retry(_always_fails, max_retries=0, base_delay=2.0)

        assert sleep_calls == []

    def test_zero_retries_returns_on_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``max_retries=0`` still returns the callable's value on success."""
        sleep_calls: list[float] = []
        monkeypatch.setattr("osimflow.work.time.sleep", sleep_calls.append)
        sentinel = Path("/tmp/run_with_retry_zero_retry_ok")

        result = run_with_retry(lambda: sentinel, max_retries=0)

        assert result == sentinel
        assert sleep_calls == []

    # ------------------------------------------------------------------
    # Exhausted retries — raises last exception after max_retries+1 attempts
    # ------------------------------------------------------------------
    def test_exhausted_retries_raises_last_exception(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """All retries consumed → re-raise the final transient error."""
        sleep_calls: list[float] = []
        monkeypatch.setattr("osimflow.work.time.sleep", sleep_calls.append)
        flaky, state = _flaky(n_failures=10, return_value=Path("/never"))

        with pytest.raises(_TransientError, match="call 4 timed out"):
            run_with_retry(flaky, max_retries=3, base_delay=0.5)

        assert state["calls"] == 4
        # Jitter (PR #1029): each sleep is `random.uniform(0, expected)`,
        # so assert per-element ranges rather than exact equality.
        assert len(sleep_calls) == 3
        for actual, exp in zip(sleep_calls, [0.5, 1.0, 2.0], strict=True):
            assert 0 <= actual <= exp, f"expected jittered sleep in [0, {exp}], got {actual}"

    def test_exhausted_retries_propagates_non_transient_subprocess_exit_code(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Non-transient subprocess exit codes propagate even when retries are
        configured. Verifies the implementation does not silently swallow
        deterministic failures."""
        sleep_calls: list[float] = []
        monkeypatch.setattr("osimflow.work.time.sleep", sleep_calls.append)
        attempts = {"n": 0}

        def _non_transient_subprocess() -> Path:
            attempts["n"] += 1
            raise _transient_subprocess(returncode=1)

        with pytest.raises(subprocess.CalledProcessError):
            run_with_retry(_non_transient_subprocess, max_retries=3)

        assert attempts["n"] == 1
        assert sleep_calls == []

    # ------------------------------------------------------------------
    # Non-transient error — propagates immediately, no retries, no sleep
    # ------------------------------------------------------------------
    def test_non_transient_error_propagates_immediately(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A non-transient exception must not trigger any retries."""
        sleep_calls: list[float] = []
        monkeypatch.setattr("osimflow.work.time.sleep", sleep_calls.append)
        attempts = {"n": 0}

        def _bad_value() -> Path:
            attempts["n"] += 1
            raise _NonTransientError("user supplied an invalid template")

        with pytest.raises(_NonTransientError, match="invalid template"):
            run_with_retry(_bad_value, max_retries=5, base_delay=1.0)

        assert attempts["n"] == 1
        assert sleep_calls == []

    # ------------------------------------------------------------------
    # Subprocess timeout kills — NON-transient (issue #1534)
    # ------------------------------------------------------------------
    def test_timeout_expired_is_not_transient(self) -> None:
        """``subprocess.TimeoutExpired`` must classify as non-transient.

        Its message contains "timed out" which historically matched the
        transient marker list — burning ~4x the timeout budget per slow
        sample before failing permanently (issue #1534).
        """
        from osimflow.work import _is_transient_error  # noqa: PLC0415

        assert _is_transient_error(_timeout_expired(timeout=600.0)) is False

    def test_timeout_expired_fails_once_without_retry(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A wall-clock timeout kill fails exactly once — no re-execution.

        Even with ``max_retries=3`` configured, a sample killed by the
        subprocess timeout must not be re-run: the identical sample would
        burn the same wall-clock budget again and still fail (issue #1534).
        """
        sleep_calls: list[float] = []
        monkeypatch.setattr("osimflow.work.time.sleep", sleep_calls.append)
        attempts = {"n": 0}

        def _times_out() -> Path:
            attempts["n"] += 1
            raise _timeout_expired(timeout=600.0)

        with pytest.raises(subprocess.TimeoutExpired):
            run_with_retry(_times_out, max_retries=3, base_delay=0.0)

        assert attempts["n"] == 1
        assert sleep_calls == []

    def test_message_level_network_timeout_still_transient(self) -> None:
        """Non-subprocess timeouts (network, I/O) remain transient.

        The issue #1534 fix must be scoped to ``subprocess.TimeoutExpired``
        only — a connection timeout raised by a client library is still a
        legitimately retryable condition.
        """
        from osimflow.work import _is_transient_error  # noqa: PLC0415

        assert _is_transient_error(_transient("connection timed out")) is True

    # ------------------------------------------------------------------
    # Exponential backoff — durations follow base_delay * 2**attempt
    # ------------------------------------------------------------------
    def test_exponential_backoff_durations(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Sleep durations follow ``base_delay * 2**attempt`` between retries."""
        sleep_calls: list[float] = []
        monkeypatch.setattr("osimflow.work.time.sleep", sleep_calls.append)
        flaky, state = _flaky(n_failures=10, return_value=Path("/never"))

        with pytest.raises(_TransientError):
            run_with_retry(flaky, max_retries=3, base_delay=1.0)

        expected = [1.0, 2.0, 4.0]
        assert len(sleep_calls) == len(expected)
        # Jitter (PR #1029): each sleep is `random.uniform(0, expected)`,
        # so assert per-element ranges rather than exact equality.
        for actual, exp in zip(sleep_calls, expected, strict=True):
            assert 0 <= actual <= exp, f"expected jittered sleep in [0, {exp}], got {actual}"
        assert state["calls"] == 4

    def test_exponential_backoff_respects_base_delay(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``base_delay`` scales the backoff curve linearly."""
        sleep_calls: list[float] = []
        monkeypatch.setattr("osimflow.work.time.sleep", sleep_calls.append)
        flaky, state = _flaky(n_failures=10, return_value=Path("/never"))

        with pytest.raises(_TransientError):
            run_with_retry(flaky, max_retries=3, base_delay=0.25)

        expected = [0.25, 0.5, 1.0]
        assert len(sleep_calls) == len(expected)
        # Jitter (PR #1029): each sleep is `random.uniform(0, expected)`,
        # so assert per-element ranges rather than exact equality.
        for actual, exp in zip(sleep_calls, expected, strict=True):
            assert 0 <= actual <= exp, f"expected jittered sleep in [0, {exp}], got {actual}"
        assert state["calls"] == 4

    def test_exponential_backoff_capped_at_60_seconds(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``run_with_retry`` clamps the sleep delay at 60 seconds."""
        sleep_calls: list[float] = []
        monkeypatch.setattr("osimflow.work.time.sleep", sleep_calls.append)
        flaky, state = _flaky(n_failures=10, return_value=Path("/never"))

        with pytest.raises(_TransientError):
            run_with_retry(flaky, max_retries=5, base_delay=10.0)

        expected = [10.0, 20.0, 40.0, 60.0, 60.0]
        assert len(sleep_calls) == len(expected)
        # Jitter (PR #1029): each sleep is `random.uniform(0, expected)`,
        # so assert per-element ranges rather than exact equality.
        for actual, exp in zip(sleep_calls, expected, strict=True):
            assert 0 <= actual <= exp, f"expected jittered sleep in [0, {exp}], got {actual}"
        assert state["calls"] == 6

    # ------------------------------------------------------------------
    # Success on the Nth attempt — return value, stop sleeping
    # ------------------------------------------------------------------
    def test_success_on_nth_attempt_returns_value(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A successful attempt returns immediately and skips remaining sleeps."""
        sleep_calls: list[float] = []
        monkeypatch.setattr("osimflow.work.time.sleep", sleep_calls.append)
        sentinel = Path("/tmp/run_with_retry_success")
        flaky, state = _flaky(n_failures=2, return_value=sentinel)

        result = run_with_retry(flaky, max_retries=3, base_delay=0.1)

        assert result == sentinel
        assert state["calls"] == 3
        # Jitter (PR #1029): each sleep is `random.uniform(0, expected)`,
        # so assert per-element ranges rather than exact equality.
        expected = [0.1, 0.2]
        assert len(sleep_calls) == len(expected)
        for actual, exp in zip(sleep_calls, expected, strict=True):
            assert 0 <= actual <= exp, f"expected jittered sleep in [0, {exp}], got {actual}"

    def test_success_on_first_attempt_does_not_sleep(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """No failures → no sleeps at all."""
        sleep_calls: list[float] = []
        monkeypatch.setattr("osimflow.work.time.sleep", sleep_calls.append)
        sentinel = Path("/tmp/run_with_retry_first_shot")

        result = run_with_retry(lambda: sentinel, max_retries=3, base_delay=1.0)

        assert result == sentinel
        assert sleep_calls == []

    # ------------------------------------------------------------------
    # Sanity — confirm ``time.sleep`` patched at the ``osimflow.work`` binding
    # is actually called by ``run_with_retry``. Acts as a regression guard for
    # future refactors that might import ``time`` under a different alias or
    # bypass the module-level binding.
    # ------------------------------------------------------------------
    def test_monkeypatch_targets_osimflow_work_time_module(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``run_with_retry`` must call ``time.sleep`` via the ``osimflow.work``
        binding — patching at that location is what the other tests rely on.
        """
        sleep_calls: list[float] = []
        monkeypatch.setattr("osimflow.work.time.sleep", sleep_calls.append)
        flaky, state = _flaky(n_failures=2, return_value=Path("/tmp/ok"))

        result = run_with_retry(flaky, max_retries=3, base_delay=0.5)

        assert result == Path("/tmp/ok")
        assert state["calls"] == 3
        # Jitter (PR #1029): each sleep is `random.uniform(0, expected)`,
        # so assert per-element ranges rather than exact equality.
        expected = [0.5, 1.0]
        assert len(sleep_calls) == len(expected)
        for actual, exp in zip(sleep_calls, expected, strict=True):
            assert 0 <= actual <= exp, f"expected jittered sleep in [0, {exp}], got {actual}"
