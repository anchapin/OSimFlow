"""Shared ``PollingHandle`` poll-retry-fallback state machine (issue #1464).

``_AzureBatchHandle`` and ``_GoogleBatchHandle`` used to duplicate the
same ``result()`` algorithm (terminal-poll loop, jittered exponential
backoff, retry accounting, fallback-to-on-demand transition, and —
since #1465 — the caller-supplied deadline).  The algorithm now lives
once in ``osimflow.executors.base.PollingHandle``; the per-substrate
handles only supply hooks.

These tests exercise the shared state machine directly through a
minimal scripted subclass, so backoff/retry/fallback/deadline
accounting is pinned once instead of per substrate.  Substrate-level
regression coverage (identical externally visible behaviour) remains
in ``test_azure_batch_executor.py`` / ``test_google_batch_executor.py``
/ ``test_handle_timeout.py`` / ``test_result_transport_contract.py``,
all of which pass unchanged.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import Future
from typing import Any

import pytest

from osimflow.executors.base import PollingHandle, PollOutcome

_BASE = "osimflow.executors.base"


class _ScriptedExecutor:
    """Executor stand-in exposing only the knobs the state machine reads."""

    def __init__(self, *, max_retries: int = 3, fallback_to_on_demand: bool = False) -> None:
        self.max_retries = max_retries
        self.fallback_to_on_demand = fallback_to_on_demand


class _ScriptedPollingHandle(PollingHandle):
    """Minimal ``PollingHandle`` whose polls replay a scripted outcome list.

    Script entries:
    * ``"ok"``            -> PollOutcome.SUCCEEDED
    * ``"indeterminate"`` -> PollOutcome.INDETERMINATE
    * ``"spot:<text>"``   -> FAILED, classified as a spot interruption
    * anything else       -> FAILED, non-spot (hard failure)
    * an ``Exception``    -> raised from ``_wait_for_terminal``
    * ``("sleep", s, entry)`` -> really wait ``s`` seconds (via
      ``threading.Event``, so ``time.sleep`` patches don't erase it),
      then replay ``entry``
    """

    def __init__(
        self,
        executor: _ScriptedExecutor,
        script: list[Any],
        *,
        poll_delay_s: float = 0.0,
    ) -> None:
        self.job_id = "job-0"
        self._executor = executor
        self._future: Future[Any] = Future()
        self._script = list(script)
        self._poll_delay_s = poll_delay_s
        # Result-transport attributes (unused by these scripts but part
        # of the shared-handle contract).
        self._result_hint = None
        self._result_transport_mode = "auto"
        self._result_storage_backend = None
        self._result_storage_bucket = None
        self._result_storage_prefix = None
        self._result_storage_endpoint = None
        self.worker_id: str | None = "job-0"
        # Accounting.
        self.polls = 0
        self.poll_timeouts: list[float | None] = []
        self.resubmits = 0
        self.on_demand_submits = 0

    # --- hooks ---------------------------------------------------------

    def _wait_for_terminal(self, timeout: float | None) -> Any:
        entry = self._script.pop(0)
        if isinstance(entry, tuple) and len(entry) == 3 and entry[0] == "sleep":
            _, delay, actual = entry
            threading.Event().wait(delay)
            entry = actual
        self.polls += 1
        self.poll_timeouts.append(timeout)
        if isinstance(entry, Exception):
            raise entry
        return entry

    def _classify(self, job: Any) -> tuple[PollOutcome, str | None]:
        if job == "ok":
            return PollOutcome.SUCCEEDED, None
        if job == "indeterminate":
            return PollOutcome.INDETERMINATE, None
        return PollOutcome.FAILED, str(job)

    def _resolve_success_result(self, timeout: float | None = None) -> Any:
        return "resolved"

    def _is_spot_interruption(self, reason: str | None) -> bool:
        return bool(reason) and reason.startswith("spot:")

    def _resubmit(self) -> None:
        self.resubmits += 1
        self.job_id = f"job-retry-{self.resubmits}"
        self.worker_id = self.job_id

    def _submit_on_demand(self) -> None:
        self.on_demand_submits += 1
        self.job_id = "job-ondemand"
        self.worker_id = self.job_id

    def _failure_error(self, job: Any) -> RuntimeError:
        return RuntimeError(f"hard failure: {job}")

    def _fallback_failure_error(self, job: Any) -> RuntimeError:
        return RuntimeError(f"fallback failure: {job}")


def _midpoint_jitter(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch ``random.uniform`` (module singleton) to its midpoint."""
    monkeypatch.setattr(f"{_BASE}.random.uniform", lambda lo, hi: lo + (hi - lo) * 0.5)


class TestPollingHandleStateMachine:
    """Issue #1464: the shared machine owns poll/backoff/retry/fallback/deadline."""

    def test_success_on_first_poll(self) -> None:
        """A succeeded job resolves, sets the future, and does no retry work."""
        handle = _ScriptedPollingHandle(_ScriptedExecutor(max_retries=3), ["ok"])
        assert handle.result() == "resolved"
        assert handle.polls == 1
        assert handle.resubmits == 0
        assert handle.on_demand_submits == 0
        assert handle._future.done() and handle._future.result() == "resolved"

    def test_spot_failure_retries_then_succeeds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A retryable spot failure sleeps a jittered backoff, resubmits, succeeds."""
        sleeps: list[float] = []
        monkeypatch.setattr(f"{_BASE}.time.sleep", sleeps.append)
        _midpoint_jitter(monkeypatch)

        handle = _ScriptedPollingHandle(_ScriptedExecutor(max_retries=3), ["spot:preempted", "ok"])
        assert handle.result() == "resolved"

        assert handle.polls == 2
        assert handle.resubmits == 1
        assert handle.on_demand_submits == 0
        # Attempt 0 backoff = min(5 * 2**0, 60) = 5.0; midpoint jitter -> 2.5.
        assert sleeps == [pytest.approx(2.5)]

    def test_backoff_curve_and_retry_accounting(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Retries stop at max_retries; the curve is min(5*2**n, 60) with jitter."""
        sleeps: list[float] = []
        monkeypatch.setattr(f"{_BASE}.time.sleep", sleeps.append)
        _midpoint_jitter(monkeypatch)

        handle = _ScriptedPollingHandle(_ScriptedExecutor(max_retries=6), ["spot:x"] * 7)
        with pytest.raises(RuntimeError, match="Spot retries exhausted \\(6\\): spot:x"):
            handle.result()

        # max_retries=6 -> exactly 7 polls (initial + 6 retries), 6 resubmits.
        assert handle.polls == 7
        assert handle.resubmits == 6
        assert handle.on_demand_submits == 0
        # Full-jitter midpoint of min(5 * 2**attempt, 60) per attempt 0..5:
        # 5/2, 10/2, 20/2, 40/2, 60/2 (capped from 80), 60/2.
        assert sleeps == [
            pytest.approx(2.5),
            pytest.approx(5.0),
            pytest.approx(10.0),
            pytest.approx(20.0),
            pytest.approx(30.0),
            pytest.approx(30.0),
        ]

    def test_exhaustion_without_fallback_leaves_future_pending(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No fallback configured: spot exhaustion raises without set_exception.

        This pins the (historic) quirk shared by the Azure and Google
        implementations: the ``Spot retries exhausted`` path raises
        directly and never settles the underlying future.
        """
        monkeypatch.setattr(f"{_BASE}.time.sleep", lambda _s: None)
        handle = _ScriptedPollingHandle(
            _ScriptedExecutor(max_retries=1, fallback_to_on_demand=False),
            ["spot:x", "spot:x"],
        )
        with pytest.raises(RuntimeError, match="Spot retries exhausted \\(1\\)"):
            handle.result()
        assert not handle._future.done()

    def test_exhaustion_falls_back_to_on_demand_and_succeeds(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Retries exhausted + fallback enabled: on-demand job is waited once."""
        monkeypatch.setattr(f"{_BASE}.time.sleep", lambda _s: None)
        handle = _ScriptedPollingHandle(
            _ScriptedExecutor(max_retries=1, fallback_to_on_demand=True),
            ["spot:x", "spot:x", "ok"],
        )
        assert handle.result() == "resolved"

        assert handle.polls == 3  # initial + 1 retry + 1 fallback wait
        assert handle.resubmits == 1
        assert handle.on_demand_submits == 1
        assert handle.job_id == "job-ondemand"
        assert handle.worker_id == "job-ondemand"

    def test_fallback_failure_raises_and_settles_future(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A non-spot failure *after* the fallback uses the fallback error."""
        monkeypatch.setattr(f"{_BASE}.time.sleep", lambda _s: None)
        handle = _ScriptedPollingHandle(
            _ScriptedExecutor(max_retries=1, fallback_to_on_demand=True),
            ["spot:x", "spot:x", "disk-full"],
        )
        with pytest.raises(RuntimeError, match="fallback failure: disk-full"):
            handle.result()
        assert handle._future.done()
        assert isinstance(handle._future.exception(), RuntimeError)

    def test_non_spot_failure_raises_and_settles_future(self) -> None:
        """A hard (non-spot) failure raises immediately — no retry, no fallback."""
        handle = _ScriptedPollingHandle(
            _ScriptedExecutor(max_retries=3, fallback_to_on_demand=True), ["disk-full"]
        )
        with pytest.raises(RuntimeError, match="hard failure: disk-full"):
            handle.result()
        assert handle.polls == 1
        assert handle.resubmits == 0
        assert handle.on_demand_submits == 0
        assert handle._future.done()

    def test_indeterminate_outcome_returns_none(self) -> None:
        """A terminal-but-unclassifiable job settles the future with None."""
        handle = _ScriptedPollingHandle(_ScriptedExecutor(max_retries=3), ["indeterminate"])
        assert handle.result() is None
        assert handle._future.done() and handle._future.result() is None

    def test_deadline_expires_before_first_poll(self) -> None:
        """An already-expired deadline raises TimeoutError without polling."""
        handle = _ScriptedPollingHandle(_ScriptedExecutor(max_retries=3), ["ok"])
        with pytest.raises(TimeoutError, match="Timed out after .* waiting for job 'job-0'"):
            handle.result(timeout=0.0)
        assert handle.polls == 0

    def test_deadline_is_shared_across_retries(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Time spent polling + backoff counts against the deadline (#1465).

        After a retryable spot failure consumes the budget, the next
        attempt raises TimeoutError naming the *resubmitted* job id.
        """
        _midpoint_jitter(monkeypatch)  # backoff = 2.5s -> no-oped below, not the limiter
        monkeypatch.setattr(f"{_BASE}.time.sleep", lambda _s: None)
        handle = _ScriptedPollingHandle(
            _ScriptedExecutor(max_retries=3),
            [("sleep", 0.15, "spot:x"), "ok"],
        )
        start = time.monotonic()
        with pytest.raises(TimeoutError, match="Timed out after .* waiting for job 'job-retry-1'"):
            handle.result(timeout=0.1)
        assert time.monotonic() - start < 2.0
        assert handle.resubmits == 1
        assert handle.poll_timeouts[0] == pytest.approx(0.1, abs=1e-3)

    def test_poll_exception_propagates_and_settles_future(self) -> None:
        """An exception from the poll loop is set on the future and re-raised."""
        handle = _ScriptedPollingHandle(
            _ScriptedExecutor(max_retries=3), [ConnectionError("substrate unreachable")]
        )
        with pytest.raises(ConnectionError, match="substrate unreachable"):
            handle.result()
        assert handle._future.done()
        assert isinstance(handle._future.exception(), ConnectionError)

    def test_timeout_passed_through_to_wait_for_terminal(self) -> None:
        """Each poll receives the remaining budget (None when no timeout)."""
        handle = _ScriptedPollingHandle(_ScriptedExecutor(max_retries=0), ["ok"])
        assert handle.result() == "resolved"
        assert handle.poll_timeouts == [None]
