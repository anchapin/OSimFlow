"""Regression tests for issue #1025 — jitter on retry loops.

Both ``osimflow.work.run_with_retry`` and the AWS Batch spot-retry loop in
``osimflow.executors._AWSBatchHandle.result`` previously used deterministic
exponential backoff (``delay = min(base_delay * (2**attempt), 60.0)``,
``time.sleep(delay)``). When a fan-out hit a transient failure on many
samples simultaneously, every worker retried in lock-step, producing a
thundering-herd against the failing backend.

The fix adds ``random.uniform(0, delay)`` jitter so concurrent calls
desynchronize. These tests assert that the spread of ``time.sleep``
durations across 100 parallel invocations is non-degenerate.
"""

from __future__ import annotations

import statistics
import threading
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from osimflow.testing.patch_targets import _AWSBatchHandle
from osimflow.work import TransientError, run_with_retry


# ---------------------------------------------------------------------------
# osimflow.work.run_with_retry jitter (issue #1025)
# ---------------------------------------------------------------------------
class TestRunWithRetryJitter:
    """``run_with_retry`` must apply jitter to its exponential backoff."""

    def _always_transient(self) -> Path:
        """A function that always raises TransientError so the retry loop runs."""
        raise TransientError("simulated network timeout")

    def test_jitter_spread_across_parallel_calls(self) -> None:
        """100 parallel ``run_with_retry`` invocations produce non-degenerate sleeps.

        Without jitter, every call's ``time.sleep(delay)`` would be the same
        value, so the recorded durations would have min == max and
        zero variance. With ``random.uniform(0, delay)`` the durations must
        differ across calls.
        """
        sleeps: list[float] = []
        sleep_lock = threading.Lock()

        def _capture_sleep(duration: float) -> None:
            with sleep_lock:
                sleeps.append(duration)

        def _worker(index: int) -> None:
            with pytest.raises(TransientError):
                run_with_retry(
                    self._always_transient,
                    sample_id=f"sample-{index}",
                    max_retries=2,
                    base_delay=1.0,
                )

        patcher = patch("osimflow.work.time.sleep", side_effect=_capture_sleep)
        patcher.start()
        try:
            threads = [threading.Thread(target=_worker, args=(i,)) for i in range(100)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        finally:
            patcher.stop()

        # Each call does max_retries=2 attempts; the final attempt doesn't
        # sleep, so each call yields exactly max_retries=2 sleeps.
        assert len(sleeps) == 200, f"expected 200 sleeps (100 calls * 2 retries), got {len(sleeps)}"

        # Non-degenerate spread: variance > 0 and min != max.
        assert min(sleeps) != max(sleeps), (
            f"all sleeps identical ({min(sleeps)}): jitter is missing"
        )
        assert statistics.variance(sleeps) > 0.0, f"variance is zero: sleeps = {sleeps[:5]}..."

        # Sanity bounds: each sleep must be in [0, base_delay * 2**attempt],
        # which for attempt 0 is [0, 1.0] and attempt 1 is [0, 2.0].
        assert all(0.0 <= s <= 2.0 for s in sleeps), (
            f"sleep out of expected jitter range: {min(sleeps)}..{max(sleeps)}"
        )


# ---------------------------------------------------------------------------
# osimflow.executors._AWSBatchHandle spot-retry jitter (issue #1025)
# ---------------------------------------------------------------------------
class TestAwsBatchSpotRetryJitter:
    """The AWS Batch spot-retry loop must apply jitter to its backoff."""

    def _build_executor(self) -> Any:
        """Build a stand-in executor that always reports a Spot interruption.

        Mirrors the pattern in ``tests/unit/test_executor_resource_contract.py``
        so we exercise the real ``_is_spot_interruption`` matcher against
        a real ``AWSBatchExecutor`` instance.
        """
        from osimflow.executors import AWSBatchExecutor

        ex = MagicMock()
        ex.max_retries = 3  # > 1 so the retry loop hits the time.sleep path
        ex.fallback_to_on_demand = False
        ex._submit_job = MagicMock(return_value="resubmitted-job")
        ex._calculate_job_cost = MagicMock(return_value=(0.0, 0.0))
        real = AWSBatchExecutor.__new__(AWSBatchExecutor)
        real._SPOT_INTERRUPTION_MARKERS = AWSBatchExecutor._SPOT_INTERRUPTION_MARKERS
        ex._is_spot_interruption = real._is_spot_interruption
        spot_job = {"status": "FAILED", "statusReason": "Spot Instance termination: capacity-over"}
        ex._wait_for_terminal = MagicMock(return_value=spot_job)
        return ex

    def _make_handle(self, executor: Any) -> _AWSBatchHandle:
        handle: Any = object.__new__(_AWSBatchHandle)  # noqa: SLF001
        handle.job_id = "job-1"
        handle._executor = executor  # noqa: SLF001
        handle._submit_params = {"name": "jitter-test"}  # noqa: SLF001
        handle._result_hint = None  # noqa: SLF001
        handle._future = MagicMock()  # noqa: SLF001
        handle.worker_id = "job-1"  # noqa: SLF001
        handle.worker_ip = None  # noqa: SLF001
        handle.worker_region = None  # noqa: SLF001
        handle.cost_usd = None  # noqa: SLF001
        handle.billed_duration_seconds = None  # noqa: SLF001
        handle.error = None  # noqa: SLF001
        return handle  # type: ignore[no-any-return]

    def test_jitter_spread_across_parallel_calls(self) -> None:
        """100 parallel ``_AWSBatchHandle.result()`` invocations produce non-degenerate sleeps.

        Without jitter every retry sleep would be the deterministic
        ``min(5.0 * 2**attempt, 60.0)`` value, so min == max and variance
        is zero. With ``random.uniform(0, backoff)`` the spread must be
        non-degenerate.
        """
        sleeps: list[float] = []
        sleep_lock = threading.Lock()

        def _capture_sleep(duration: float) -> None:
            with sleep_lock:
                sleeps.append(duration)

        def _worker(index: int) -> None:  # noqa: ARG001
            ex = self._build_executor()
            handle = self._make_handle(ex)
            with pytest.raises(RuntimeError, match="exhausted"):
                handle.result()

        patcher = patch("osimflow.testing.patch_targets.time.sleep", side_effect=_capture_sleep)
        patcher.start()
        try:
            threads = [threading.Thread(target=_worker, args=(i,)) for i in range(100)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        finally:
            patcher.stop()

        # Each call does max_retries=3 retries before exhausting, so each
        # call yields exactly max_retries=3 sleeps.
        assert len(sleeps) == 300, f"expected 300 sleeps (100 calls * 3 retries), got {len(sleeps)}"

        # Non-degenerate spread: variance > 0 and min != max.
        assert min(sleeps) != max(sleeps), (
            f"all sleeps identical ({min(sleeps)}): jitter is missing"
        )
        assert statistics.variance(sleeps) > 0.0, f"variance is zero: sleeps = {sleeps[:5]}..."

        # Sanity bounds: each sleep must be in [0, 60.0]. Attempt 0 backoff
        # is min(5.0, 60.0) = 5.0; attempt 1 is min(10.0, 60.0) = 10.0;
        # attempt 2 is min(20.0, 60.0) = 20.0. So overall upper bound is 20.0.
        assert all(0.0 <= s <= 20.0 for s in sleeps), (
            f"sleep out of expected jitter range: {min(sleeps)}..{max(sleeps)}"
        )
