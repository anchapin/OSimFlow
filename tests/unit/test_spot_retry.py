"""Spot instance retry + price ceiling tests for AWSBatchExecutor (issue #131).

Tests cover:
- Spot interruption triggers retry with exponential backoff
- Price ceiling rejects expensive spots
- Fallback to on-demand after spot exhaustion
- Max retries exhaustion raises RuntimeError
- Non-spot errors don't retry
- No retry when max_retries=0
"""

from unittest.mock import MagicMock, patch

import pytest

from osimflow.executors import AWSBatchExecutor

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_executor(
    *,
    max_spot_price_usd: float | None = None,
    fallback_to_on_demand: bool = False,
    max_retries: int = 3,
    poll_interval_s: float = 0.01,
) -> AWSBatchExecutor:
    """Create an AWSBatchExecutor with mocked boto3.

    The executor is constructed with the given spot retry parameters.
    ``_boto3`` is replaced with a MagicMock so no real AWS calls happen.
    """
    with patch("osimflow.executors.boto3", create=True) as mock_boto3:
        mock_boto3.client.return_value = MagicMock()
        ex = AWSBatchExecutor.__new__(AWSBatchExecutor)
        # Replicate __init__ without the real boto3 import.
        ex._boto3 = mock_boto3  # noqa: SLF001
        ex._region_name = None  # noqa: SLF001
        ex._client = MagicMock()  # noqa: SLF001
        ex._ec2_client = MagicMock()  # noqa: SLF001
        ex.job_queue = "test-queue"
        ex.job_definition = "test-job-def"
        ex.poll_interval_s = poll_interval_s
        ex.max_poll_interval_s = 0.02
        ex.max_spot_price_usd = max_spot_price_usd
        ex.fallback_to_on_demand = fallback_to_on_demand
        ex.max_retries = max_retries
    return ex


def _mock_submit_response(job_id: str = "job-123") -> dict[str, str]:
    """Return a boto3 submit_job response."""
    return {"jobId": job_id}


def _mock_describe_response(
    status: str,
    status_reason: str = "",
) -> dict[str, list[dict[str, str]]]:
    """Return a boto3 describe_jobs response."""
    return {
        "jobs": [
            {
                "jobId": "job-123",
                "status": status,
                "statusReason": status_reason,
            }
        ]
    }


def _mock_spot_price(price: float) -> dict[str, list[dict[str, str]]]:
    """Return a describe_spot_price_history response."""
    return {
        "SpotPriceHistory": [
            {"SpotPrice": str(price)},
        ]
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSpotInterruptionRetry:
    """Spot interruption triggers retry with exponential backoff."""

    def test_single_spot_interruption_then_success(self) -> None:
        """A single Spot interruption followed by success on retry."""
        ex = _make_executor(max_retries=3)

        submit_responses = [
            _mock_submit_response("job-1"),
            _mock_submit_response("job-2"),
        ]
        describe_responses = [
            _mock_describe_response("FAILED", "Spot interruption: instance terminated"),
            _mock_describe_response("SUCCEEDED"),
        ]

        ex._get_client().submit_job.side_effect = submit_responses  # noqa: SLF001
        ex._get_client().describe_jobs.side_effect = describe_responses  # noqa: SLF001

        # Patch time.sleep to track backoff without real delay.
        with patch("osimflow.executors.time.sleep") as mock_sleep:
            handle = ex.submit(lambda: None, name="test")

        assert handle.job_id == "job-2"
        # Verify backoff was called for the retry.
        mock_sleep.assert_called_once()
        backoff = mock_sleep.call_args[0][0]
        assert backoff == pytest.approx(5.0)  # First retry: 5s

    def test_multiple_spot_interruptions_then_success(self) -> None:
        """Two Spot interruptions followed by success on the third attempt."""
        ex = _make_executor(max_retries=3)

        submit_responses = [
            _mock_submit_response("job-1"),
            _mock_submit_response("job-2"),
            _mock_submit_response("job-3"),
        ]
        describe_responses = [
            _mock_describe_response("FAILED", "Spot Instance termination notice received"),
            _mock_describe_response("FAILED", "Host EC2 instance terminated due to spot"),
            _mock_describe_response("SUCCEEDED"),
        ]

        ex._get_client().submit_job.side_effect = submit_responses  # noqa: SLF001
        ex._get_client().describe_jobs.side_effect = describe_responses  # noqa: SLF001

        with patch("osimflow.executors.time.sleep"):
            handle = ex.submit(lambda: None, name="test")

        assert handle.job_id == "job-3"
        assert ex._get_client().submit_job.call_count == 3  # noqa: SLF001


class TestPriceCeiling:
    """Price ceiling rejects expensive spots."""

    def test_price_above_ceiling_raises(self) -> None:
        """When Spot price exceeds ceiling and no fallback, raise RuntimeError."""
        ex = _make_executor(max_spot_price_usd=0.05)

        ex._get_ec2_client().describe_spot_price_history.return_value = (  # noqa: SLF001
            _mock_spot_price(0.10)
        )

        with pytest.raises(RuntimeError, match="exceeds ceiling"):
            ex.submit(lambda: None, name="test")

    def test_price_below_ceiling_succeeds(self) -> None:
        """When Spot price is below ceiling, submit normally."""
        ex = _make_executor(max_spot_price_usd=0.10)

        ex._get_ec2_client().describe_spot_price_history.return_value = (  # noqa: SLF001
            _mock_spot_price(0.05)
        )
        ex._get_client().submit_job.return_value = _mock_submit_response("job-ok")  # noqa: SLF001
        ex._get_client().describe_jobs.return_value = _mock_describe_response("SUCCEEDED")  # noqa: SLF001

        handle = ex.submit(lambda: None, name="test")
        assert handle.job_id == "job-ok"

    def test_no_ceiling_skips_price_check(self) -> None:
        """When no ceiling is set, the Spot price API is never called."""
        ex = _make_executor(max_spot_price_usd=None)

        ex._get_client().submit_job.return_value = _mock_submit_response("job-ok")  # noqa: SLF001
        ex._get_client().describe_jobs.return_value = _mock_describe_response("SUCCEEDED")  # noqa: SLF001

        handle = ex.submit(lambda: None, name="test")
        assert handle.job_id == "job-ok"
        ex._get_ec2_client().describe_spot_price_history.assert_not_called()  # noqa: SLF001


class TestFallbackToOnDemand:
    """Fallback to on-demand after spot exhaustion."""

    def test_price_ceiling_breach_with_fallback(self) -> None:
        """When price ceiling breached and fallback enabled, go to on-demand."""
        ex = _make_executor(max_spot_price_usd=0.05, fallback_to_on_demand=True)

        ex._get_ec2_client().describe_spot_price_history.return_value = (  # noqa: SLF001
            _mock_spot_price(0.10)
        )
        ex._get_client().submit_job.return_value = _mock_submit_response("job-ondemand")  # noqa: SLF001
        # No describe_jobs call needed — submit returns immediately
        # on price ceiling breach + fallback path.

        handle = ex.submit(lambda: None, name="test")
        assert handle.job_id == "job-ondemand"

    def test_retry_exhaustion_with_fallback(self) -> None:
        """After max retries exhausted, fall back to on-demand."""
        ex = _make_executor(max_retries=2, fallback_to_on_demand=True)

        # All spot attempts fail with interruption.
        submit_responses = [
            _mock_submit_response("spot-1"),
            _mock_submit_response("spot-2"),
            _mock_submit_response("spot-3"),
            _mock_submit_response("ondemand-1"),
        ]
        describe_responses = [
            _mock_describe_response("FAILED", "Spot interruption"),
            _mock_describe_response("FAILED", "Spot interruption"),
            _mock_describe_response("FAILED", "Spot interruption"),
        ]

        ex._get_client().submit_job.side_effect = submit_responses  # noqa: SLF001
        ex._get_client().describe_jobs.side_effect = describe_responses  # noqa: SLF001

        with patch("osimflow.executors.time.sleep"):
            handle = ex.submit(lambda: None, name="test")

        assert handle.job_id == "ondemand-1"


class TestMaxRetriesExhaustion:
    """Max retries exhaustion raises RuntimeError."""

    def test_max_retries_exhausted_no_fallback(self) -> None:
        """After max retries, raise RuntimeError when no fallback."""
        ex = _make_executor(max_retries=2, fallback_to_on_demand=False)

        submit_responses = [
            _mock_submit_response("job-1"),
            _mock_submit_response("job-2"),
            _mock_submit_response("job-3"),
        ]
        describe_responses = [
            _mock_describe_response("FAILED", "Spot interruption"),
            _mock_describe_response("FAILED", "Spot interruption"),
            _mock_describe_response("FAILED", "Spot interruption"),
        ]

        ex._get_client().submit_job.side_effect = submit_responses  # noqa: SLF001
        ex._get_client().describe_jobs.side_effect = describe_responses  # noqa: SLF001

        with (
            patch("osimflow.executors.time.sleep"),
            pytest.raises(RuntimeError, match="Spot retries exhausted"),
        ):
            ex.submit(lambda: None, name="test")


class TestNonSpotErrors:
    """Non-spot errors don't retry."""

    def test_non_spot_failure_no_retry(self) -> None:
        """A non-spot failure raises immediately without retry."""
        ex = _make_executor(max_retries=3)

        ex._get_client().submit_job.return_value = _mock_submit_response("job-fail")  # noqa: SLF001
        ex._get_client().describe_jobs.return_value = (  # noqa: SLF001
            _mock_describe_response("FAILED", "Container command failed with exit code 1")
        )

        with pytest.raises(RuntimeError, match="exit code 1"):
            ex.submit(lambda: None, name="test")

        # submit_job called exactly once — no retry.
        assert ex._get_client().submit_job.call_count == 1  # noqa: SLF001

    def test_non_spot_failure_no_retry_on_oom(self) -> None:
        """OOM kill does not trigger retry."""
        ex = _make_executor(max_retries=3)

        ex._get_client().submit_job.return_value = _mock_submit_response("job-oom")  # noqa: SLF001
        ex._get_client().describe_jobs.return_value = (  # noqa: SLF001
            _mock_describe_response("FAILED", "OutOfMemoryError: container killed")
        )

        with pytest.raises(RuntimeError, match="OutOfMemoryError"):
            ex.submit(lambda: None, name="test")

        assert ex._get_client().submit_job.call_count == 1  # noqa: SLF001


class TestNoRetryWhenMaxRetriesZero:
    """No retry when max_retries=0."""

    def test_zero_retries_spot_interruption_fails(self) -> None:
        """With max_retries=0, a Spot interruption fails immediately."""
        ex = _make_executor(max_retries=0)

        ex._get_client().submit_job.return_value = _mock_submit_response("job-spot")  # noqa: SLF001
        ex._get_client().describe_jobs.return_value = (  # noqa: SLF001
            _mock_describe_response("FAILED", "Spot interruption: terminated")
        )

        with pytest.raises(RuntimeError, match="Spot retries exhausted"):
            ex.submit(lambda: None, name="test")

        assert ex._get_client().submit_job.call_count == 1  # noqa: SLF001

    def test_zero_retries_spot_interruption_with_fallback(self) -> None:
        """With max_retries=0 and fallback, immediately go to on-demand."""
        ex = _make_executor(max_retries=0, fallback_to_on_demand=True)

        submit_responses = [
            _mock_submit_response("spot-fail"),
            _mock_submit_response("ondemand-ok"),
        ]
        describe_responses = [
            _mock_describe_response("FAILED", "Spot interruption"),
        ]

        ex._get_client().submit_job.side_effect = submit_responses  # noqa: SLF001
        ex._get_client().describe_jobs.side_effect = describe_responses  # noqa: SLF001

        with patch("osimflow.executors.time.sleep"):
            handle = ex.submit(lambda: None, name="test")

        assert handle.job_id == "ondemand-ok"
