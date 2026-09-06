"""Unit tests for the shared :class:`TokenBucketRateLimiter` (issue #1563).

The pre-#1563 token bucket lived as a private ``_TokenBucketRateLimiter``
inside ``osimflow.executors.aws_batch_executor``; the file
``tests/unit/test_aws_batch_rate_limiting.py`` exercised it from the
AWS side. The implementation now lives on
:mod:`osimflow.executors._rate_limiter` and every executor shares one
process-wide bucket per ``(name, rate_per_sec)`` tuple. These tests
cover the shared primitive directly so a regression in the limiter
itself surfaces uniformly across all 10 substrates.

Test surface
------------
* token consumption (``acquire``) and refill,
* burst capacity (bucket holds ``burst`` tokens),
* ``rate_per_sec <= 0`` disables the limiter (``acquire`` never sleeps),
* ``get_shared`` memoisation per ``(name, rate)`` tuple so concurrent
  executors cooperate on a single bucket,
* the ``BaseExecutor._init_rate_limiter`` shim resolves
  ``default_submit_rps`` plus the constructor ``submit_rps`` override.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from osimflow.executors import (
    AWSBatchExecutor,
    AzureBatchExecutor,
    GoogleBatchExecutor,
    KubernetesExecutor,
    LocalExecutor,
    NomadExecutor,
    PBSExecutor,
    SlurmExecutor,
)
from osimflow.executors._rate_limiter import TokenBucketRateLimiter


# ---------------------------------------------------------------------------
# TokenBucketRateLimiter primitives
# ---------------------------------------------------------------------------
class TestTokenBucketRateLimiter:
    def test_acquire_decrements_token(self) -> None:
        """A burst of acquires within capacity should not block."""
        limiter = TokenBucketRateLimiter(rate_per_sec=1000)
        for _ in range(10):
            limiter.acquire()

    def test_acquire_blocks_when_empty(self) -> None:
        """After exhausting the bucket, acquire must sleep until a token refills."""
        sleep_mock = MagicMock()
        now_values = [0.0, 0.0, 0.5, 1.0]
        with patch(
            "osimflow.executors._rate_limiter.time.monotonic",
            side_effect=lambda: now_values.pop(0),
        ):
            with patch("osimflow.executors._rate_limiter.time.sleep", sleep_mock):
                limiter = TokenBucketRateLimiter(rate_per_sec=1)  # 1 token/s, capacity=1
                limiter.acquire()  # drain the single token
                limiter.acquire()  # should block ~0.5s for refill

        # sleep should have been called once because we needed to wait ~0.5s
        sleep_mock.assert_called_once()

    def test_rps_zero_disables_limiting(self) -> None:
        """RPS=0 means no rate limiting — acquire should never sleep."""
        limiter = TokenBucketRateLimiter(rate_per_sec=0)
        sleep_mock = MagicMock()
        with patch("osimflow.executors._rate_limiter.time.sleep", sleep_mock):
            for _ in range(100):
                limiter.acquire()
        sleep_mock.assert_not_called()

    def test_disabled_property(self) -> None:
        """``disabled`` is True iff the rate is non-positive."""
        assert TokenBucketRateLimiter(rate_per_sec=0).disabled is True
        assert TokenBucketRateLimiter(rate_per_sec=-1.0).disabled is True
        assert TokenBucketRateLimiter(rate_per_sec=10).disabled is False

    def test_rate_per_sec_and_capacity_properties(self) -> None:
        limiter = TokenBucketRateLimiter(rate_per_sec=7.5)
        assert limiter.rate_per_sec == pytest.approx(7.5)
        # Burst defaults to max(1, int(rate_per_sec)) → 7 here.
        assert limiter.capacity == 7

    def test_custom_burst(self) -> None:
        limiter = TokenBucketRateLimiter(rate_per_sec=10, burst=50)
        assert limiter.capacity == 50


# ---------------------------------------------------------------------------
# get_shared singleton semantics (issue #1010)
# ---------------------------------------------------------------------------
class TestGetShared:
    def test_returns_same_instance_for_same_key(self) -> None:
        TokenBucketRateLimiter._reset_shared_for_testing()
        a = TokenBucketRateLimiter.get_shared(100.0)
        b = TokenBucketRateLimiter.get_shared(100.0)
        assert a is b

    def test_different_rps_returns_different_instance(self) -> None:
        TokenBucketRateLimiter._reset_shared_for_testing()
        a = TokenBucketRateLimiter.get_shared(100.0)
        b = TokenBucketRateLimiter.get_shared(200.0)
        assert a is not b

    def test_different_name_returns_different_instance(self) -> None:
        TokenBucketRateLimiter._reset_shared_for_testing()
        a = TokenBucketRateLimiter.get_shared(100.0, name="aws_batch")
        b = TokenBucketRateLimiter.get_shared(100.0, name="kubernetes")
        assert a is not b

    def test_disabled_bucket_shared_across_calls(self) -> None:
        """RPS=0 returns the same disabled limiter so disabled executors
        can compare identities (cheap branch in ``acquire``)."""
        TokenBucketRateLimiter._reset_shared_for_testing()
        a = TokenBucketRateLimiter.get_shared(0.0)
        b = TokenBucketRateLimiter.get_shared(0.0)
        assert a is b
        assert a.disabled is True


# ---------------------------------------------------------------------------
# BaseExecutor._init_rate_limiter: default + override (issue #1563)
# ---------------------------------------------------------------------------
class TestInitRateLimiter:
    @pytest.fixture(autouse=True)
    def _reset_singletons(self) -> None:
        TokenBucketRateLimiter._reset_shared_for_testing()
        yield
        TokenBucketRateLimiter._reset_shared_for_testing()

    def test_local_executor_no_throttle_by_default(self) -> None:
        ex = LocalExecutor(max_workers=2)
        # LocalExecutor sets ``default_submit_rps = float('inf')`` so
        # the shared limiter is constructed disabled.
        assert ex._rate_limiter.disabled is True

    def test_local_executor_override_enables_throttle(self) -> None:
        ex = LocalExecutor(max_workers=2, submit_rps=5.0)
        assert ex._rate_limiter.rate_per_sec == pytest.approx(5.0)

    def test_aws_batch_uses_default_800(self) -> None:
        """Pre-#1563 ``--aws-batch-submit-rps`` defaulted to 800; the
        shared limiter must preserve that semantic for AWS Batch."""
        ex = AWSBatchExecutor(
            job_queue="q",
            job_definition="d",
            region_name="us-east-1",
        )
        assert ex._rate_limiter.rate_per_sec == pytest.approx(800.0)

    def test_aws_batch_explicit_override(self) -> None:
        ex = AWSBatchExecutor(
            job_queue="q",
            job_definition="d",
            region_name="us-east-1",
            submit_rps=42.0,
        )
        assert ex._rate_limiter.rate_per_sec == pytest.approx(42.0)

    def test_nomad_default_is_5(self) -> None:
        ex = NomadExecutor(address="http://stub.local:4646")
        assert ex._rate_limiter.rate_per_sec == pytest.approx(5.0)

    def test_nomad_fanout_interval_derived_from_limiter(self) -> None:
        """``NomadExecutor.fanout_submit_interval_s()`` now reads the
        shared limiter (issue #1563)."""
        ex = NomadExecutor(address="http://stub.local:4646")
        assert ex.fanout_submit_interval_s() == pytest.approx(1.0 / 5.0)

    def test_kubernetes_default_is_5(self) -> None:
        ex = KubernetesExecutor()
        assert ex._rate_limiter.rate_per_sec == pytest.approx(5.0)

    def test_azure_batch_default_is_10(self) -> None:
        ex = AzureBatchExecutor.__new__(AzureBatchExecutor)
        # Don't go through the real ctor (Azure SDK); inject the limiter
        # the same way the conformance tests do.
        ex.account_name = "stub"
        ex.account_url = "https://stub.eastus.batch.azure.com"
        ex.pool_id = "stub"
        ex.location = "eastus"
        ex.poll_interval_s = 0.01
        ex.max_poll_interval_s = 0.02
        ex.use_spot = False
        ex.fallback_to_on_demand = False
        ex.max_retries = 0
        ex._client = None
        ex._azure_batch = MagicMock()
        ex._azure_identity = MagicMock()
        ex._init_rate_limiter(None)
        assert ex._rate_limiter.rate_per_sec == pytest.approx(10.0)

    def test_google_batch_default_is_10(self) -> None:
        assert GoogleBatchExecutor.default_submit_rps == pytest.approx(10.0)

    def test_pbs_default_is_100(self) -> None:
        assert PBSExecutor.default_submit_rps == pytest.approx(100.0)

    def test_slurm_default_is_100(self) -> None:
        assert SlurmExecutor.default_submit_rps == pytest.approx(100.0)


# ---------------------------------------------------------------------------
# submit() acquires from the shared limiter (issue #1563)
# ---------------------------------------------------------------------------
class TestSubmitAcquires:
    @pytest.fixture(autouse=True)
    def _reset_singletons(self) -> None:
        TokenBucketRateLimiter._reset_shared_for_testing()
        yield
        TokenBucketRateLimiter._reset_shared_for_testing()

    def test_submit_acquires_before_substrate_call(self) -> None:
        """``LocalExecutor.submit`` must call ``_rate_limiter.acquire``
        before the substrate-level work runs."""
        ex = LocalExecutor(max_workers=2)
        acquire_mock = MagicMock()
        ex._rate_limiter = MagicMock()  # noqa: SLF001
        ex._rate_limiter.acquire = acquire_mock  # noqa: SLF001

        ex.submit(lambda: None, name="acquire-then-call")
        acquire_mock.assert_called_once_with()

    def test_consecutive_submits_throttled_by_low_rps(self) -> None:
        """Two consecutive ``submit()`` calls with a low ``submit_rps``
        take at least ~0.5s in wall time (issue #1563 acceptance criterion).

        ``submit_rps=2`` with a 1-token burst: the first call drains
        the single token, then the second call has to wait ~0.5s for
        the bucket to refill. The slack (>= 0.4s) absorbs scheduling
        jitter.
        """
        import time as time_module

        TokenBucketRateLimiter._reset_shared_for_testing()
        # Force a 1-token burst so the second acquire has to wait — the
        # default ``_default_burst(rps=2)`` would give capacity=2, in
        # which case the bucket is full at construction time and the
        # second acquire has nothing to wait for.
        limiter = TokenBucketRateLimiter(rate_per_sec=2.0, burst=1)
        ex = LocalExecutor(max_workers=2)
        ex._rate_limiter = limiter  # noqa: SLF001 — install the test limiter

        limiter.acquire()  # drain the single token
        start = time_module.monotonic()
        ex.submit(lambda: None, name="second")
        elapsed = time_module.monotonic() - start
        assert elapsed >= 0.4, (
            f"expected second submit() to be throttled (~0.5s); got {elapsed:.3f}s"
        )
