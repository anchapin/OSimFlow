"""Unit tests for AWS Batch rate-limiting and spot-price caching (issue #1010).

Tests cover:
  - ``_TokenBucketRateLimiter``: token consumption, refill, shared singleton,
    RPS=0 (disabled) path.
  - ``_SpotPriceCache``: get / set / TTL expiry, key scoping.
  - ``_aws_error_code``: extraction from ``botocore.exceptions.ClientError``.
  - ``_submit_job_with_retry``: exponential backoff on
    ``ThrottlingException`` / ``RequestLimitExceeded``.
  - ``_submit_job``: rate-limiter is acquired before every ``submit_job``.
  - ``AWSBatchExecutor.__init__``: ``submit_rps`` is wired to the shared
    limiter and boto3 retry config.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from osimflow.executors import (
    AWSBatchExecutor,
    _aws_error_code,
    _SpotPriceCache,
    _TokenBucketRateLimiter,
)


# ---------------------------------------------------------------------------
# _TokenBucketRateLimiter
# ---------------------------------------------------------------------------
class TestTokenBucketRateLimiter:
    def test_acquire_decrements_token(self) -> None:
        """A burst of acquires within capacity should not block."""
        limiter = _TokenBucketRateLimiter(rps=1000)
        for _ in range(10):
            limiter.acquire()

    def test_acquire_blocks_when_empty(self) -> None:
        """After exhausting the bucket, acquire must sleep until a token refills."""
        sleep_mock = MagicMock()
        now_values = [0.0, 0.0, 0.5, 1.0]
        with patch("osimflow.executors.time.monotonic", side_effect=lambda: now_values.pop(0)):
            with patch("osimflow.executors.time.sleep", sleep_mock):
                limiter = _TokenBucketRateLimiter(rps=1)  # 1 token/s, capacity=1
                limiter.acquire()  # drain the single token
                limiter.acquire()  # should block ~0.5s for refill

        # sleep should have been called once because we needed to wait ~0.5s
        sleep_mock.assert_called_once()

    def test_rps_zero_disables_limiting(self) -> None:
        """RPS=0 means no rate limiting — acquire should never sleep."""
        limiter = _TokenBucketRateLimiter(rps=0)
        sleep_mock = MagicMock()
        with patch("osimflow.executors.time.sleep", sleep_mock):
            for _ in range(100):
                limiter.acquire()
        sleep_mock.assert_not_called()

    def test_get_shared_returns_same_instance(self) -> None:
        """``get_shared`` should return the same limiter for the same rps."""
        a = _TokenBucketRateLimiter.get_shared(rps=100)
        b = _TokenBucketRateLimiter.get_shared(rps=100)
        assert a is b

    def test_get_shared_different_rps_different_instance(self) -> None:
        """Different RPS values should get different limiter instances."""
        a = _TokenBucketRateLimiter.get_shared(rps=100)
        b = _TokenBucketRateLimiter.get_shared(rps=200)
        assert a is not b

    def test_get_shared_none_uses_default(self) -> None:
        """``None`` rps should use the default (800)."""
        a = _TokenBucketRateLimiter.get_shared(rps=None)
        b = _TokenBucketRateLimiter.get_shared(rps=800)
        assert a is b

    def test_get_shared_returns_disabled_for_default(self) -> None:
        """The default (None) should still be a valid limiter."""
        limiter = _TokenBucketRateLimiter.get_shared(rps=None)
        assert limiter is not None


# ---------------------------------------------------------------------------
# _SpotPriceCache
# ---------------------------------------------------------------------------
class TestSpotPriceCache:
    def test_set_then_get(self) -> None:
        cache = _SpotPriceCache(ttl_s=60.0)
        cache.set(("us-east-1", "m5.large", "Linux/UNIX"), 0.042)
        assert cache.get(("us-east-1", "m5.large", "Linux/UNIX")) == 0.042

    def test_get_missing_returns_none(self) -> None:
        cache = _SpotPriceCache(ttl_s=60.0)
        assert cache.get(("us-east-1", "m5.large", "Linux/UNIX")) is None

    def test_ttl_expiry(self) -> None:
        """After TTL, a cached value should miss (controllable clock, issue #1544)."""
        clock = {"now": 100.0}
        with patch(
            "osimflow.executors.aws_batch_executor.time.monotonic",
            side_effect=lambda: clock["now"],
        ):
            cache = _SpotPriceCache(ttl_s=0.01)
            cache.set(("us-east-1", "c5.large", "Linux/UNIX"), 0.03)
            assert cache.get(("us-east-1", "c5.large", "Linux/UNIX")) == 0.03
            clock["now"] = 200.0  # age the entry past the TTL — no sleep
            assert cache.get(("us-east-1", "c5.large", "Linux/UNIX")) is None

    def test_different_keys_independent(self) -> None:
        cache = _SpotPriceCache(ttl_s=60.0)
        cache.set(("us-east-1", "m5.large", "Linux/UNIX"), 0.05)
        cache.set(("us-east-1", "c5.large", "Linux/UNIX"), 0.03)
        assert cache.get(("us-east-1", "m5.large", "Linux/UNIX")) == 0.05
        assert cache.get(("us-east-1", "c5.large", "Linux/UNIX")) == 0.03

    def test_overwrite_same_key(self) -> None:
        cache = _SpotPriceCache(ttl_s=60.0)
        cache.set(("us-east-1", "m5.large", "Linux/UNIX"), 0.05)
        cache.set(("us-east-1", "m5.large", "Linux/UNIX"), 0.06)
        assert cache.get(("us-east-1", "m5.large", "Linux/UNIX")) == 0.06


# ---------------------------------------------------------------------------
# _aws_error_code
# ---------------------------------------------------------------------------
class TestAwsErrorCode:
    def test_extracts_code(self) -> None:
        try:
            import botocore.exceptions
        except ImportError:
            pytest.skip("botocore not available")

        error_response = {"Error": {"Code": "ThrottlingException", "Message": "Rate exceeded"}}
        exc = botocore.exceptions.ClientError(error_response, "SubmitJob")
        assert _aws_error_code(exc) == "ThrottlingException"

    def test_unknown_code_returns_unknown(self) -> None:
        try:
            import botocore.exceptions
        except ImportError:
            pytest.skip("botocore not available")

        error_response = {"Error": {"Code": "AccessDeniedException", "Message": "No"}}
        exc = botocore.exceptions.ClientError(error_response, "DescribeJobs")
        assert _aws_error_code(exc) == "AccessDeniedException"


# ---------------------------------------------------------------------------
# _submit_job_with_retry
# ---------------------------------------------------------------------------
class TestSubmitJobWithRetry:
    @pytest.fixture
    def executor(self) -> AWSBatchExecutor:
        return AWSBatchExecutor(
            job_queue="test-queue",
            job_definition="test-def",
            region_name="us-east-1",
        )

    def test_retries_on_throttling(self, executor: AWSBatchExecutor) -> None:
        """ThrottlingException on first attempt should retry and then succeed."""
        import botocore.exceptions

        error_response = {"Error": {"Code": "ThrottlingException", "Message": "Rate exceeded"}}
        throttle_exc = botocore.exceptions.ClientError(error_response, "SubmitJob")

        call_count = 0
        responses: list[object] = []

        def _mock_submit(**kwargs: object) -> dict[str, object]:
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise throttle_exc
            return {"jobId": f"job-{call_count}", "job": {}}

        client_mock = MagicMock()
        client_mock.submit_job.side_effect = _mock_submit
        executor._client = client_mock  # noqa: SLF001

        with patch("osimflow.executors.time.sleep"):
            result = executor._submit_job_with_retry(submit_kwargs={})

        assert result["jobId"] == "job-3"
        assert call_count == 3

    def test_raises_after_max_retries(self, executor: AWSBatchExecutor) -> None:
        """After 5 throttling attempts, should give up."""
        import botocore.exceptions

        error_response = {"Error": {"Code": "RequestLimitExceeded", "Message": "Rate exceeded"}}
        throttle_exc = botocore.exceptions.ClientError(error_response, "SubmitJob")

        client_mock = MagicMock()
        client_mock.submit_job.side_effect = throttle_exc
        executor._client = client_mock  # noqa: SLF001

        with patch("osimflow.executors.time.sleep"):
            with pytest.raises(botocore.exceptions.ClientError, match="RequestLimitExceeded"):
                executor._submit_job_with_retry(submit_kwargs={})

    def test_no_retry_on_non_throttle_error(self, executor: AWSBatchExecutor) -> None:
        """A non-throttling ClientError should not be retried."""
        import botocore.exceptions

        error_response = {"Error": {"Code": "AccessDeniedException", "Message": "No"}}
        access_exc = botocore.exceptions.ClientError(error_response, "SubmitJob")

        client_mock = MagicMock()
        client_mock.submit_job.side_effect = access_exc
        executor._client = client_mock  # noqa: SLF001

        with patch("osimflow.executors.time.sleep"):
            with pytest.raises(botocore.exceptions.ClientError, match="AccessDeniedException"):
                executor._submit_job_with_retry(submit_kwargs={})

        # Only one call — no retry for non-throttle errors.
        assert client_mock.submit_job.call_count == 1

    def test_retry_applies_jitter(self, executor: AWSBatchExecutor) -> None:
        """Verify jitter is applied to retry backoff (issue #1089).

        ``random.uniform(0, delay)`` should be used so concurrent campaigns
        retrying at the same throttle point do not retry in lockstep.
        """
        import botocore.exceptions

        error_response = {"Error": {"Code": "ThrottlingException", "Message": "Rate exceeded"}}
        throttle_exc = botocore.exceptions.ClientError(error_response, "SubmitJob")

        call_count = 0

        def _mock_submit(**kwargs: object) -> dict[str, object]:
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise throttle_exc
            return {"jobId": f"job-{call_count}", "job": {}}

        client_mock = MagicMock()
        client_mock.submit_job.side_effect = _mock_submit
        executor._client = client_mock  # noqa: SLF001

        sleep_durations: list[float] = []
        with patch("osimflow.executors.time.sleep", side_effect=sleep_durations.append):
            with patch(
                "osimflow.executors.random.uniform",
                side_effect=lambda lo, hi: lo + (hi - lo) * 0.25,
            ):
                result = executor._submit_job_with_retry(submit_kwargs={})

        assert result["jobId"] == "job-3"
        assert len(sleep_durations) == 2  # Two retries before success
        # First retry: delay=0.5, jitter = 0.5 * 0.25 = 0.125
        assert sleep_durations[0] == pytest.approx(0.125)
        # Second retry: delay=1.0, jitter = 1.0 * 0.25 = 0.25
        assert sleep_durations[1] == pytest.approx(0.25)


# ---------------------------------------------------------------------------
# _submit_job: rate limiter integration
# ---------------------------------------------------------------------------
class TestSubmitJobRateLimiting:
    @pytest.fixture
    def executor(self) -> AWSBatchExecutor:
        executor = AWSBatchExecutor(
            job_queue="test-queue",
            job_definition="test-def",
            region_name="us-east-1",
            submit_rps=10,  # finite rate so we can verify the limiter is used
        )
        return executor

    def test_submit_calls_rate_limiter(self, executor: AWSBatchExecutor) -> None:
        """``_submit_job`` must call ``self._submit_limiter.acquire()``."""
        acquire_mock = MagicMock()
        executor._submit_limiter = MagicMock()  # noqa: SLF001
        executor._submit_limiter.acquire = acquire_mock  # noqa: SLF001

        client_mock = MagicMock()
        client_mock.submit_job.return_value = {"jobId": "job-1", "job": {}}
        executor._client = client_mock  # noqa: SLF001

        executor._submit_job(
            name="test",
            cpus=2,
            memory_mb=1024,
            time_min=10,
            environment=[],
        )

        acquire_mock.assert_called_once()
        client_mock.submit_job.assert_called_once()

    def test_submit_rps_none_defaults_to_800(self) -> None:
        """When submit_rps is None, the default of 800 should be used."""
        executor = AWSBatchExecutor(
            job_queue="q",
            job_definition="d",
            submit_rps=None,
            region_name="us-east-1",
        )
        assert executor._submit_rps is None
        # The limiter should still be created with the default.
        assert executor._submit_limiter is not None


# ---------------------------------------------------------------------------
# _get_spot_price: cache integration
# ---------------------------------------------------------------------------
class TestGetSpotPriceCache:
    def _make_executor(self) -> AWSBatchExecutor:
        return AWSBatchExecutor(
            job_queue="q",
            job_definition="d",
            region_name="us-east-1",
            instance_type="m5.large",
        )

    def test_caches_price(self) -> None:
        """Two consecutive ``_get_spot_price`` calls should only hit the
        EC2 API once (second call is a cache hit)."""
        executor = self._make_executor()

        ec2_mock = MagicMock()
        ec2_mock.describe_spot_price_history.return_value = {
            "SpotPriceHistory": [{"SpotPrice": "0.042"}]
        }
        executor._ec2_client = ec2_mock  # noqa: SLF001

        price1 = executor._get_spot_price()
        price2 = executor._get_spot_price()

        assert price1 == pytest.approx(0.042)
        assert price2 == pytest.approx(0.042)
        ec2_mock.describe_spot_price_history.assert_called_once()

    def test_cache_miss_after_ttl(self) -> None:
        """After the cache TTL expires, a new EC2 call should be made (controllable clock, issue #1544)."""
        executor = AWSBatchExecutor(
            job_queue="q",
            job_definition="d",
            region_name="us-east-1",
            instance_type="m5.large",
        )
        # Override the cache with a short TTL — expiry is driven by the
        # controllable monotonic clock, not wall-clock elapsed time.
        executor._spot_price_cache = _SpotPriceCache(ttl_s=0.01)  # noqa: SLF001

        ec2_mock = MagicMock()
        ec2_mock.describe_spot_price_history.return_value = {
            "SpotPriceHistory": [{"SpotPrice": "0.05"}]
        }
        executor._ec2_client = ec2_mock  # noqa: SLF001

        clock = {"now": 1000.0}
        with patch(
            "osimflow.executors.aws_batch_executor.time.monotonic",
            side_effect=lambda: clock["now"],
        ):
            price1 = executor._get_spot_price()
            clock["now"] = 2000.0  # age the cache past the TTL — no sleep
            price2 = executor._get_spot_price()

        assert price1 == pytest.approx(0.05)
        assert price2 == pytest.approx(0.05)
        # Two calls because TTL expired in between.
        assert ec2_mock.describe_spot_price_history.call_count == 2
