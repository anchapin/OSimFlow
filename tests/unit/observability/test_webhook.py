# noqa: F841
"""Unit tests for osimflow/webhook.py (issues #283, #1671).

Tests the WebhookClient class using unittest.mock to patch the
client's opener (``urllib.request.OpenerDirector.open``) and
``socket.getaddrinfo`` so we can verify retry behaviour, backoff
timing, payload structure, and SSRF validation without making real
HTTP requests or DNS lookups.
"""

from __future__ import annotations

import http.client
import io
import json
import socket
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from unittest import mock

import pytest

from osimflow.webhook import WebhookClient, WebhookSSRFError

# A public, non-blocked IPv4 address (example.com) used as the default
# fake DNS answer so every test is hermetic.
_PUBLIC_V4 = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))


@pytest.fixture(autouse=True)
def _fake_dns() -> Iterator[None]:
    """Default: every hostname resolves to a public IP (hermetic tests)."""
    with mock.patch("osimflow.webhook.socket.getaddrinfo") as getaddrinfo:
        getaddrinfo.return_value = [_PUBLIC_V4]
        yield


class MockResponse:
    """Minimal mock HTTP response object."""

    def __init__(self, status: int) -> None:
        self.status = status


class TestWebhookClient:
    """Tests for WebhookClient."""

    def test_deliver_success_first_attempt(self) -> None:
        """Successful delivery on the first attempt returns True."""
        client = WebhookClient(url="https://example.com/webhook")

        with mock.patch("urllib.request.OpenerDirector.open") as mock_urlopen:
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

        with mock.patch("urllib.request.OpenerDirector.open") as mock_urlopen:
            mock_urlopen.return_value.__enter__ = mock.Mock(return_value=MockResponse(201))
            mock_urlopen.return_value.__exit__ = mock.Mock(return_value=False)

            result = client.deliver({"foo": "bar"})

        assert result is True

    def test_deliver_retries_on_500_then_succeeds(self) -> None:
        """Server error on first attempt, success on second attempt."""
        client = WebhookClient(url="https://example.com/webhook", initial_delay=0.01)

        with (
            mock.patch("urllib.request.OpenerDirector.open") as mock_urlopen,
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
            mock.patch("urllib.request.OpenerDirector.open") as mock_urlopen,
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
            mock.patch("urllib.request.OpenerDirector.open") as mock_urlopen,
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
            mock.patch("urllib.request.OpenerDirector.open") as mock_urlopen,
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
            mock.patch("urllib.request.OpenerDirector.open") as mock_urlopen,
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
            mock.patch("urllib.request.OpenerDirector.open") as mock_urlopen,
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
            mock.patch("urllib.request.OpenerDirector.open") as mock_urlopen,
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
            mock.patch("urllib.request.OpenerDirector.open") as mock_urlopen,
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

        with mock.patch("urllib.request.OpenerDirector.open") as mock_urlopen:
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
            mock.patch("urllib.request.OpenerDirector.open") as mock_urlopen,
            mock.patch.object(time, "sleep") as mock_sleep,  # noqa: F841
        ):  # noqa: F841
            mock_urlopen.side_effect = urllib.error.URLError("fail")

            result = client.deliver({"event": "campaign.completed"})

        assert result is False
        assert mock_urlopen.call_count == 1
        mock_sleep.assert_not_called()

    def test_custom_timeout(self) -> None:
        """Custom timeout is passed to the opener."""
        client = WebhookClient(url="https://example.com/webhook", timeout=45.0)

        with mock.patch("urllib.request.OpenerDirector.open") as mock_urlopen:
            mock_urlopen.return_value.__enter__ = mock.Mock(return_value=MockResponse(200))
            mock_urlopen.return_value.__exit__ = mock.Mock(return_value=False)

            client.deliver({"event": "campaign.completed"})

        call_args = mock_urlopen.call_args
        assert call_args[1]["timeout"] == 45.0


class TestWebhookSSRFValidation:
    """SSRF hardening tests (issue #1671).

    Covers the three gaps: DNS resolution of hostnames, RFC1918 /
    CGNAT ranges in the blocklist, and per-hop redirect
    re-validation.
    """

    # -- DNS resolution ------------------------------------------------

    def test_dns_name_resolving_to_blocked_ip_rejected(self) -> None:
        """A hostname whose A record is 169.254.169.254 is rejected."""
        client = WebhookClient(url="https://metadata.attacker.example/hook")

        with mock.patch("osimflow.webhook.socket.getaddrinfo") as getaddrinfo:
            getaddrinfo.return_value = [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", 0))
            ]
            with pytest.raises(WebhookSSRFError, match="169.254.169.254"):
                client._check_ip_blocklist("metadata.attacker.example")

    def test_dns_name_resolving_to_loopback_rejected(self) -> None:
        """A hostname resolving to 127.0.0.1 is rejected (even scheme-allowlisted)."""
        client = WebhookClient(
            url="http://localhost:9000/hook",
            allowed_insecure_hosts={"localhost"},
        )

        with mock.patch("osimflow.webhook.socket.getaddrinfo") as getaddrinfo:
            getaddrinfo.return_value = [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0))
            ]
            with pytest.raises(WebhookSSRFError, match="127.0.0.1"):
                client._check_ip_blocklist("localhost")

    def test_dns_multiple_records_any_blocked_rejected(self) -> None:
        """ANY blocked A/AAAA record rejects the host (not just the first)."""
        client = WebhookClient(url="https://mixed.example/hook")

        with mock.patch("osimflow.webhook.socket.getaddrinfo") as getaddrinfo:
            getaddrinfo.return_value = [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", 0)),
            ]
            with pytest.raises(WebhookSSRFError, match="10.0.0.5"):
                client._check_ip_blocklist("mixed.example")

    def test_dns_resolution_failure_fail_closed(self) -> None:
        """An unresolvable host is treated as blocked (fail-closed)."""
        client = WebhookClient(url="https://unresolvable.example/hook")

        with mock.patch("osimflow.webhook.socket.getaddrinfo") as getaddrinfo:
            getaddrinfo.side_effect = socket.gaierror(-2, "Name or service not known")
            with pytest.raises(WebhookSSRFError, match="fail-closed"):
                client._check_ip_blocklist("unresolvable.example")

    def test_deliver_dns_name_to_blocked_ip_returns_false_no_request(self) -> None:
        """deliver() to a DNS name resolving to a blocked IP: False, no HTTP call."""
        client = WebhookClient(url="https://metadata.attacker.example/hook")

        with (
            mock.patch("osimflow.webhook.socket.getaddrinfo") as getaddrinfo,
            mock.patch("urllib.request.OpenerDirector.open") as mock_open,
            mock.patch.object(time, "sleep") as mock_sleep,
        ):
            getaddrinfo.return_value = [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", 0))
            ]
            result = client.deliver({"event": "campaign.completed"})

        assert result is False
        mock_open.assert_not_called()
        mock_sleep.assert_not_called()

    # -- Literal IP blocklist (RFC1918 / CGNAT / metadata) -------------

    @pytest.mark.parametrize(
        "blocked_host",
        [
            "127.0.0.1",
            "127.8.9.10",
            "10.0.0.5",
            "10.255.255.1",
            "172.16.0.1",
            "172.31.255.254",
            "192.168.1.100",
            "169.254.169.254",
            "100.64.0.1",
            "100.127.255.254",
            "0.0.0.0",
            "::1",
            "fe80::1",
            "fd00::1",
            "::ffff:169.254.169.254",  # IPv4-mapped IPv6 must not bypass
        ],
    )
    def test_literal_blocked_ip_rejected(self, blocked_host: str) -> None:
        """Literal internal/loopback/metadata IPs are rejected outright."""
        client = WebhookClient(url="https://example.com/hook")
        with pytest.raises(WebhookSSRFError):
            client._check_ip_blocklist(blocked_host)

    @pytest.mark.parametrize(
        "allowed_host",
        ["8.8.8.8", "1.1.1.1", "93.184.216.34", "172.32.0.1", "100.63.0.1", "2606:4700::1111"],
    )
    def test_literal_public_ip_allowed(self, allowed_host: str) -> None:
        """Literal public IPs (incl. just-outside-range boundaries) pass."""
        client = WebhookClient(url="https://example.com/hook")
        client._check_ip_blocklist(allowed_host)  # must not raise

    def test_deliver_literal_rfc1918_url_rejected(self) -> None:
        """deliver() to a literal 192.168.x URL: False, no HTTP call."""
        client = WebhookClient(
            url="http://192.168.1.10:8080/hook",
            allowed_insecure_hosts={"192.168.1.10"},
        )

        with (
            mock.patch("urllib.request.OpenerDirector.open") as mock_open,
            mock.patch.object(time, "sleep") as mock_sleep,
        ):
            result = client.deliver({"event": "campaign.completed"})

        assert result is False
        mock_open.assert_not_called()
        mock_sleep.assert_not_called()

    # -- Redirect re-validation ----------------------------------------

    def _redirect(self, client: WebhookClient, newurl: str) -> object:
        """Invoke the client's redirect handler for one hop."""
        req = urllib.request.Request(client.url, method="POST")
        headers = http.client.HTTPMessage()
        return client._redirect_handler.redirect_request(
            req, io.BytesIO(b""), 302, "Found", headers, newurl
        )

    @pytest.mark.parametrize(
        "newurl",
        [
            "http://169.254.169.254/latest/meta-data/",
            "https://169.254.169.254/latest/meta-data/",
            "https://10.0.0.5/internal",
            "https://172.20.1.2/internal",
            "https://192.168.0.9/internal",
        ],
    )
    def test_redirect_to_blocked_target_rejected(self, newurl: str) -> None:
        """Every redirect hop to a blocked IP is refused (issue #1671)."""
        client = WebhookClient(url="https://example.com/hook")
        with pytest.raises(WebhookSSRFError):
            self._redirect(client, newurl)

    def test_redirect_to_dns_name_with_blocked_a_record_rejected(self) -> None:
        """A redirect to a hostname resolving to the metadata IP is refused."""
        client = WebhookClient(url="https://example.com/hook")

        with mock.patch("osimflow.webhook.socket.getaddrinfo") as getaddrinfo:
            getaddrinfo.return_value = [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", 0))
            ]
            with pytest.raises(WebhookSSRFError):
                self._redirect(client, "https://metadata.attacker.example/latest")

    def test_redirect_to_plain_http_not_allowlisted_rejected(self) -> None:
        """A redirect downgrading https->http is refused unless allowlisted."""
        client = WebhookClient(url="https://example.com/hook")
        with pytest.raises(WebhookSSRFError, match="allowlisting"):
            self._redirect(client, "http://plain-http-target.example/next")

    def test_redirect_to_allowed_https_target_followed(self) -> None:
        """A redirect to a public https URL is followed (Request returned)."""
        client = WebhookClient(url="https://example.com/hook")

        new_req = self._redirect(client, "https://good.example/next")

        assert isinstance(new_req, urllib.request.Request)
        assert new_req.full_url == "https://good.example/next"

    def test_redirect_to_allowlisted_http_target_followed(self) -> None:
        """An allowlisted host keeps its http:// scheme exemption on hops."""
        client = WebhookClient(
            url="https://example.com/hook",
            allowed_insecure_hosts={"insecure.example"},
        )

        new_req = self._redirect(client, "http://insecure.example/next")

        assert isinstance(new_req, urllib.request.Request)
        assert new_req.full_url == "http://insecure.example/next"

    # -- Happy path -----------------------------------------------------

    def test_normal_https_public_url_still_delivers(self) -> None:
        """A normal https URL with a public DNS answer still delivers."""
        client = WebhookClient(url="https://example.com/webhook")

        with mock.patch("urllib.request.OpenerDirector.open") as mock_open:
            mock_open.return_value.__enter__ = mock.Mock(return_value=MockResponse(200))
            mock_open.return_value.__exit__ = mock.Mock(return_value=False)

            result = client.deliver({"event": "campaign.completed"})

        assert result is True
        mock_open.assert_called_once()

    def test_allowlisted_http_public_host_still_delivers(self) -> None:
        """allowed_insecure_hosts still works for public hosts (scheme only)."""
        client = WebhookClient(
            url="http://example.com/hook",
            allowed_insecure_hosts={"example.com"},
        )

        with mock.patch("urllib.request.OpenerDirector.open") as mock_open:
            mock_open.return_value.__enter__ = mock.Mock(return_value=MockResponse(200))
            mock_open.return_value.__exit__ = mock.Mock(return_value=False)

            result = client.deliver({"event": "campaign.completed"})

        assert result is True
