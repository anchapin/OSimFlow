# noqa: F841
"""Unit tests for osimflow/webhook.py (issue #283).

Tests the WebhookClient class using unittest.mock to patch
urllib.request.urlopen so we can verify retry behaviour, backoff
timing, and payload structure without making real HTTP requests.
"""

from __future__ import annotations

import json
import time
import urllib.error
from unittest import mock

import pytest

from osimflow.webhook import WebhookClient


class MockResponse:
    """Minimal mock HTTP response object."""

    def __init__(self, status: int) -> None:
        self.status = status


class TestWebhookClient:
    """Tests for WebhookClient."""

    def test_deliver_success_first_attempt(self) -> None:
        """Successful delivery on the first attempt returns True."""
        client = WebhookClient(url="https://example.com/webhook")

        with mock.patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value.__enter__ = mock.Mock(return_value=MockResponse(200))
            mock_urlopen.return_value.__exit__ = mock.Mock(return_value=False)

            result = client.deliver({"event": "campaign.completed"})

        assert result is True
        mock_urlopen.assert_called_once()
        call_args = mock_urlopen.call_args
        assert call_args[0][0].full_url == "https://example.com/webhook"
        assert call_args[0][0].method == "POST"

    def test_deliver_success_201(self) -> None:
        """HTTP 201 Created also counts as success."""
        client = WebhookClient(url="https://example.com/hook")

        with mock.patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value.__enter__ = mock.Mock(return_value=MockResponse(201))
            mock_urlopen.return_value.__exit__ = mock.Mock(return_value=False)

            result = client.deliver({"foo": "bar"})

        assert result is True

    def test_deliver_retries_on_500_then_succeeds(self) -> None:
        """Server error on first attempt, success on second attempt."""
        client = WebhookClient(url="https://example.com/webhook", initial_delay=0.01)

        with (
            mock.patch("urllib.request.urlopen") as mock_urlopen,
            mock.patch.object(time, "sleep") as mock_sleep,  # noqa: F841
        ):
            mock_urlopen.side_effect = [
                urllib.error.HTTPError("url", 500, "Internal Server Error", {}, None),
                mock.Mock(
                    __enter__=mock.Mock(return_value=MockResponse(200)),
                    __exit__=mock.Mock(return_value=False),
                ),
            ]

            result = client.deliver({"event": "campaign.completed"})

        assert result is True
        assert mock_urlopen.call_count == 2
        mock_sleep.assert_called_once()
        delay = mock_sleep.call_args[0][0]
        assert delay == pytest.approx(0.01)

    def test_deliver_retries_on_network_error_then_succeeds(self) -> None:
        """URLError on first attempt, success on second attempt."""
        client = WebhookClient(url="https://example.com/webhook", initial_delay=0.01)

        with (
            mock.patch("urllib.request.urlopen") as mock_urlopen,
            mock.patch.object(time, "sleep") as mock_sleep,  # noqa: F841
        ):
            mock_urlopen.side_effect = [
                urllib.error.URLError("connection refused"),
                mock.Mock(
                    __enter__=mock.Mock(return_value=MockResponse(200)),
                    __exit__=mock.Mock(return_value=False),
                ),
            ]

            result = client.deliver({"event": "campaign.completed"})

        assert result is True
        assert mock_urlopen.call_count == 2

    def test_deliver_retries_on_timeout(self) -> None:
        """TimeoutError on first attempt, success on second."""
        client = WebhookClient(url="https://example.com/webhook", initial_delay=0.01)

        with (
            mock.patch("urllib.request.urlopen") as mock_urlopen,
            mock.patch.object(time, "sleep") as mock_sleep,  # noqa: F841
        ):  # noqa: F841
            mock_urlopen.side_effect = [
                TimeoutError("timed out"),
                mock.Mock(
                    __enter__=mock.Mock(return_value=MockResponse(200)),
                    __exit__=mock.Mock(return_value=False),
                ),
            ]

            result = client.deliver({"event": "campaign.completed"})

        assert result is True
        assert mock_urlopen.call_count == 2

    def test_deliver_all_retries_fail_returns_false(self) -> None:
        """All retry attempts fail — returns False after max_retries."""
        client = WebhookClient(
            url="https://example.com/webhook",
            max_retries=3,
            initial_delay=0.01,
        )

        with (
            mock.patch("urllib.request.urlopen") as mock_urlopen,
            mock.patch.object(time, "sleep") as mock_sleep,  # noqa: F841
        ):  # noqa: F841
            mock_urlopen.side_effect = urllib.error.URLError("connection refused")

            result = client.deliver({"event": "campaign.completed"})

        assert result is False
        assert mock_urlopen.call_count == 4  # initial + 3 retries

    def test_deliver_no_retry_on_404(self) -> None:
        """HTTP 404 does not retry — returns False immediately."""
        client = WebhookClient(url="https://example.com/webhook", initial_delay=0.01)

        with (
            mock.patch("urllib.request.urlopen") as mock_urlopen,
            mock.patch.object(time, "sleep") as mock_sleep,  # noqa: F841
        ):  # noqa: F841
            mock_urlopen.side_effect = urllib.error.HTTPError("url", 404, "Not Found", {}, None)

            result = client.deliver({"event": "campaign.completed"})

        assert result is False
        assert mock_urlopen.call_count == 1
        mock_sleep.assert_not_called()

    def test_deliver_no_retry_on_400(self) -> None:
        """HTTP 400 does not retry — returns False immediately."""
        client = WebhookClient(url="https://example.com/webhook", initial_delay=0.01)

        with (
            mock.patch("urllib.request.urlopen") as mock_urlopen,
            mock.patch.object(time, "sleep") as mock_sleep,  # noqa: F841
        ):  # noqa: F841
            mock_urlopen.side_effect = urllib.error.HTTPError("url", 400, "Bad Request", {}, None)

            result = client.deliver({"event": "campaign.completed"})

        assert result is False
        assert mock_urlopen.call_count == 1
        mock_sleep.assert_not_called()

    def test_deliver_exponential_backoff(self) -> None:
        """Verify exponential backoff: 1s, 2s, 4s for initial_delay=1."""
        client = WebhookClient(
            url="https://example.com/webhook",
            max_retries=3,
            initial_delay=1.0,
        )
        backoff_delays: list[float] = []

        with (
            mock.patch("urllib.request.urlopen") as mock_urlopen,
            mock.patch.object(
                time, "sleep", side_effect=lambda d: backoff_delays.append(d)
            ) as _mock_sleep,
        ):
            mock_urlopen.side_effect = urllib.error.URLError("fail")

            client.deliver({"event": "campaign.completed"})

        assert mock_urlopen.call_count == 4
        assert backoff_delays == pytest.approx([1.0, 2.0, 4.0])

    def test_deliver_backoff_capped_at_60s(self) -> None:
        """Backoff delay is capped at 60 seconds."""
        client = WebhookClient(
            url="https://example.com/webhook",
            max_retries=10,
            initial_delay=60.0,
        )
        backoff_delays: list[float] = []

        with (
            mock.patch("urllib.request.urlopen") as mock_urlopen,
            mock.patch.object(
                time, "sleep", side_effect=lambda d: backoff_delays.append(d)
            ) as _mock_sleep,
        ):
            mock_urlopen.side_effect = urllib.error.URLError("fail")

            client.deliver({"event": "campaign.completed"})

        for delay in backoff_delays:
            assert delay <= 60.0

    def test_deliver_payload_is_json(self) -> None:
        """The request body is valid JSON with correct Content-Type."""
        client = WebhookClient(url="https://example.com/webhook")

        with mock.patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value.__enter__ = mock.Mock(return_value=MockResponse(200))
            mock_urlopen.return_value.__exit__ = mock.Mock(return_value=False)

            client.deliver({"campaign_id": "abc123", "status": "success"})

        req = mock_urlopen.call_args[0][0]
        body = req.data
        assert body is not None
        parsed = json.loads(body.decode("utf-8"))
        assert parsed["campaign_id"] == "abc123"
        assert parsed["status"] == "success"
        assert req.get_header("Content-type") == "application/json"
        assert req.get_header("User-agent") == "OSimFlow/1.0"

    def test_build_payload_returns_expected_keys(self) -> None:
        """build_payload produces all required keys."""
        client = WebhookClient(url="https://example.com/webhook")
        payload = client.build_payload(
            campaign_id="run-001",
            status="success",
            elapsed_s=123.45,
            n_samples=50,
            n_succeeded=48,
            n_failed=2,
            total_cost_usd=12.34,
            outdir="/results/campaign-001",
        )

        assert payload["event"] == "campaign.completed"
        assert payload["campaign_id"] == "run-001"
        assert payload["status"] == "success"
        assert payload["elapsed_s"] == 123.45
        assert payload["n_samples"] == 50
        assert payload["n_succeeded"] == 48
        assert payload["n_failed"] == 2
        assert payload["total_cost_usd"] == 12.34
        assert payload["outdir"] == "/results/campaign-001"
        assert "osimflow_version" in payload

    def test_build_payload_with_none_cost(self) -> None:
        """total_cost_usd=None is passed through correctly."""
        client = WebhookClient(url="https://example.com/webhook")
        payload = client.build_payload(
            campaign_id="run-002",
            status="failure",
            elapsed_s=10.0,
            n_samples=5,
            n_succeeded=0,
            n_failed=5,
            total_cost_usd=None,
            outdir="/results/campaign-002",
        )

        assert payload["total_cost_usd"] is None
        assert payload["status"] == "failure"

    def test_max_retries_zero_no_retries(self) -> None:
        """max_retries=0 means exactly one attempt, no sleep."""
        client = WebhookClient(url="https://example.com/webhook", max_retries=0)

        with (
            mock.patch("urllib.request.urlopen") as mock_urlopen,
            mock.patch.object(time, "sleep") as mock_sleep,  # noqa: F841
        ):  # noqa: F841
            mock_urlopen.side_effect = urllib.error.URLError("fail")

            result = client.deliver({"event": "campaign.completed"})

        assert result is False
        assert mock_urlopen.call_count == 1
        mock_sleep.assert_not_called()

    def test_custom_timeout(self) -> None:
        """Custom timeout is passed to urlopen."""
        client = WebhookClient(url="https://example.com/webhook", timeout=45.0)

        with mock.patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value.__enter__ = mock.Mock(return_value=MockResponse(200))
            mock_urlopen.return_value.__exit__ = mock.Mock(return_value=False)

            client.deliver({"event": "campaign.completed"})

        call_args = mock_urlopen.call_args
        assert call_args[1]["timeout"] == 45.0
