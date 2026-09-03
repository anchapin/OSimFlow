"""Shared poll / retry loop contracts in ``osimflow.executors.base`` (issue #1540).

``poll_until_terminal`` and ``retry_with_backoff`` are the single
owners of the terminal-poll skeleton and the bounded-attempt request
retry schedule that the per-executor handles / executors previously
hand-rolled. These tests pin the shared machine directly (mirroring
``test_polling_handle_state_machine.py`` for ``PollingHandle.result``)
so per-substrate suites only need to pin substrate wiring.
"""

from __future__ import annotations

import pytest

from osimflow.executors.base import poll_until_terminal, retry_with_backoff

_BASE = "osimflow.executors.base"


class _NeverTerminal(Exception):
    pass


class TestPollUntilTerminal:
    def test_terminal_first_probe_returns_without_sleeping(self) -> None:
        sleeps: list[float] = []
        seen: list[str] = []

        def _probe() -> str:
            seen.append("probe")
            return "done"

        out = poll_until_terminal(
            _probe,
            is_terminal=lambda s: s == "done",
            timeout=None,
            timeout_message=lambda elapsed: f"timed out {elapsed:.1f}",
            poll_interval_s=5.0,
            max_poll_interval_s=60.0,
        )
        assert out == "done"
        assert seen == ["probe"]

    def test_grow_before_sleep_sleep_sequence(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sleeps: list[float] = []
        monkeypatch.setattr(f"{_BASE}.time.sleep", sleeps.append)
        polls = iter(["run"] * 4 + ["done"])

        poll_until_terminal(
            lambda: next(polls),
            is_terminal=lambda s: s == "done",
            timeout=None,
            timeout_message=lambda e: f"t {e:.1f}",
            poll_interval_s=1.0,
            max_poll_interval_s=8.0,
            grow_before_sleep=True,
        )
        assert sleeps == [2.0, 4.0, 8.0, 8.0]

    def test_sleep_then_grow_sleep_sequence(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """AWS/Nomad ordering: first sleep is the interval itself."""
        sleeps: list[float] = []
        monkeypatch.setattr(f"{_BASE}.time.sleep", sleeps.append)
        polls = iter(["run"] * 4 + ["done"])

        poll_until_terminal(
            lambda: next(polls),
            is_terminal=lambda s: s == "done",
            timeout=None,
            timeout_message=lambda e: f"t {e:.1f}",
            poll_interval_s=1.0,
            max_poll_interval_s=4.0,
            grow_before_sleep=False,
        )
        assert sleeps == [1.0, 2.0, 4.0, 4.0]

    def test_timeout_raises_with_substrate_message(self) -> None:
        with pytest.raises(TimeoutError, match="waiting for job 'job-9'"):
            poll_until_terminal(
                lambda: "run",
                is_terminal=lambda s: s == "done",
                timeout=0.01,
                timeout_message=lambda elapsed: (
                    f"Timed out after {elapsed:.1f}s waiting for job 'job-9'"
                ),
                poll_interval_s=0.005,
                max_poll_interval_s=0.005,
            )

    def test_probe_error_propagates_by_default(self) -> None:
        def _probe() -> str:
            raise _NeverTerminal("boom")

        with pytest.raises(_NeverTerminal, match="boom"):
            poll_until_terminal(
                _probe,
                is_terminal=lambda s: False,
                timeout=None,
                timeout_message=lambda e: f"t {e:.1f}",
                poll_interval_s=0.01,
                max_poll_interval_s=0.01,
            )

    def test_probe_error_tolerated_keeps_polling(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(f"{_BASE}.time.sleep", lambda _s: None)
        warnings: list[str] = []
        polls = iter(["run", _NeverTerminal("blip"), "done"])

        def _probe() -> str:
            entry = next(polls)
            if isinstance(entry, Exception):
                raise entry
            return entry

        out = poll_until_terminal(
            _probe,
            is_terminal=lambda s: s == "done",
            timeout=None,
            timeout_message=lambda e: f"t {e:.1f}",
            poll_interval_s=0.01,
            max_poll_interval_s=0.01,
            tolerate_probe_errors=True,
            on_probe_error=lambda exc, _delay: warnings.append(str(exc)),
        )
        assert out == "done"
        assert warnings == ["blip"]

    def test_transient_retries_without_growing_backoff(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """PBS #1405 semantics: transient probe results hold the delay."""
        sleeps: list[float] = []
        monkeypatch.setattr(f"{_BASE}.time.sleep", sleeps.append)
        polls = iter(["TRANSIENT", "TRANSIENT", "run", "done"])

        poll_until_terminal(
            lambda: next(polls),
            is_terminal=lambda s: s == "done",
            timeout=None,
            timeout_message=lambda e: f"t {e:.1f}",
            poll_interval_s=1.0,
            max_poll_interval_s=8.0,
            is_transient=lambda s: s == "TRANSIENT",
            grow_before_sleep=True,
        )
        # Two transient retries sleep the un-grown 1.0s each; the
        # pending iteration then doubles (2.0) and caps at 8.
        assert sleeps == [1.0, 1.0, 2.0]

    def test_custom_sleep_for_and_next_delay(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Nomad shape: phased sleep + adaptive growth factor."""
        sleeps: list[float] = []
        monkeypatch.setattr(f"{_BASE}.time.sleep", sleeps.append)
        polls = iter(["run"] * 3 + ["done"])

        poll_until_terminal(
            lambda: next(polls),
            is_terminal=lambda s: s == "done",
            timeout=None,
            timeout_message=lambda e: f"t {e:.1f}",
            poll_interval_s=1.0,
            max_poll_interval_s=60.0,
            sleep_for=lambda delay, remaining: delay + 0.07,
            next_delay=lambda delay: min(delay * 1.6, 60.0),
            grow_before_sleep=False,
        )
        # sleep(1.0+0.07) -> grow 1.6 -> sleep(1.6+0.07) -> grow 2.56
        # -> sleep(2.56+0.07).
        assert sleeps == pytest.approx([1.07, 1.67, 2.63])


class TestRetryWithBackoff:
    def test_success_first_attempt_no_sleep(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sleeps: list[float] = []
        monkeypatch.setattr(f"{_BASE}.time.sleep", sleeps.append)
        calls: list[int] = []

        def _call() -> str:
            calls.append(1)
            return "ok"

        assert retry_with_backoff(_call, retry_on=lambda _e: True, max_attempts=3) == "ok"
        assert sleeps == []

    def test_retries_then_succeeds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(f"{_BASE}.time.sleep", lambda _s: None)
        attempts: list[int] = []

        def _call() -> str:
            attempts.append(1)
            if len(attempts) < 3:
                raise RuntimeError("transient")
            return "ok"

        out = retry_with_backoff(_call, retry_on=lambda e: True, max_attempts=5)
        assert out == "ok"
        assert len(attempts) == 3

    def test_non_retryable_propagates_immediately(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sleeps: list[float] = []
        monkeypatch.setattr(f"{_BASE}.time.sleep", sleeps.append)
        attempts: list[int] = []

        def _call() -> str:
            attempts.append(1)
            raise ValueError("permanent")

        with pytest.raises(ValueError, match="permanent"):
            retry_with_backoff(_call, retry_on=lambda _e: False, max_attempts=5)
        assert len(attempts) == 1
        assert sleeps == []

    def test_exhaustion_reraises_final_exception(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sleeps: list[float] = []
        monkeypatch.setattr(f"{_BASE}.time.sleep", sleeps.append)
        attempts: list[int] = []

        def _call() -> str:
            attempts.append(1)
            raise RuntimeError("always")

        with pytest.raises(RuntimeError, match="always"):
            retry_with_backoff(_call, retry_on=lambda _e: True, max_attempts=4)
        assert len(attempts) == 4
        # 3 sleeps between 4 attempts; the final attempt raises pre-sleep.
        assert len(sleeps) == 3

    def test_jitter_window_and_growth(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Full jitter: sleep = uniform(0, min(delay, cap)); delay doubles."""
        sleeps: list[float] = []
        monkeypatch.setattr(f"{_BASE}.time.sleep", sleeps.append)
        monkeypatch.setattr(f"{_BASE}.random.uniform", lambda lo, hi: hi)
        attempts: list[int] = []

        def _call() -> str:
            attempts.append(1)
            raise RuntimeError("throttled")

        with pytest.raises(RuntimeError):
            retry_with_backoff(
                _call,
                retry_on=lambda _e: True,
                max_attempts=4,
                initial_delay_s=0.5,
                max_delay_s=30.0,
                jitter=True,
            )
        assert sleeps == pytest.approx([0.5, 1.0, 2.0])

    def test_no_jitter_sleeps_window_deterministically(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sleeps: list[float] = []
        monkeypatch.setattr(f"{_BASE}.time.sleep", sleeps.append)
        attempts: list[int] = []

        def _call() -> str:
            attempts.append(1)
            raise RuntimeError("qstat down")

        with pytest.raises(RuntimeError):
            retry_with_backoff(
                _call,
                retry_on=lambda _e: True,
                max_attempts=4,
                initial_delay_s=1.0,
                max_delay_s=15.0,
                jitter=False,
            )
        # PBS shape: 1s, 2s, 4s deterministic windows.
        assert sleeps == pytest.approx([1.0, 2.0, 4.0])

    def test_on_retry_receives_attempt_and_window(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(f"{_BASE}.time.sleep", lambda _s: None)
        logged: list[tuple[int, float]] = []
        attempts: list[int] = []

        def _call() -> str:
            attempts.append(1)
            if len(attempts) < 3:
                raise RuntimeError("503")
            return "ok"

        retry_with_backoff(
            _call,
            retry_on=lambda _e: True,
            max_attempts=5,
            initial_delay_s=0.5,
            max_delay_s=30.0,
            on_retry=lambda _exc, attempt, window: logged.append((attempt, window)),
        )
        assert logged == [(1, 0.5), (2, 1.0)]
