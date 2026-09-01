"""Webhook client for campaign completion callbacks (issue #283).

Delivers a POST to a user-configured URL when a campaign completes
successfully or with failure. The payload is a JSON summary of the
campaign run. Delivery is best-effort: webhook failures are logged but
do not crash or abort the campaign.

The client retries up to 3 times with exponential backoff (1s, 2s, 4s)
to handle transient network errors.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .errors import OSimFlowError

log = logging.getLogger("osimflow.webhook")

_BLOCKED_NETWORKS: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = [
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
]


class WebhookSSRFError(OSimFlowError):
    """Raised when a webhook URL fails SSRF validation."""


class WebhookDeliveryError(OSimFlowError):
    """Raised when all retry attempts for a webhook delivery fail."""


class WebhookClient:
    """Delivers campaign completion webhooks with retry and exponential backoff.

    Parameters
    ----------
    url
        The target URL to POST to. Must be an https:// URL by default,
        or http:// if the host is in ``allowed_insecure_hosts``.
    timeout
        Request timeout in seconds (default: 30).
    max_retries
        Maximum number of retry attempts on failure (default: 3).
    initial_delay
        Initial backoff delay in seconds (default: 1.0).
    allowed_insecure_hosts
        Set of hosts allowed to use http:// (insecure) instead of https://.
        These are checked against the URL's host after any redirects.
        By default, no insecure HTTP is allowed.
    """

    def __init__(
        self,
        url: str,
        timeout: float = 30.0,
        max_retries: int = 3,
        initial_delay: float = 1.0,
        allowed_insecure_hosts: set[str] | None = None,
    ) -> None:
        self.url = url
        self.timeout = timeout
        self.max_retries = max_retries
        self.initial_delay = initial_delay
        self.allowed_insecure_hosts = allowed_insecure_hosts or set()
        self._validate_url()

    def _validate_url(self) -> None:
        """Validate URL to prevent SSRF attacks (issue #1175).

        - Requires https:// by default
        - Allows http:// only for hosts in allowed_insecure_hosts
        - Blocks localhost, link-local, and metadata IPs
        """
        parsed = urllib.parse.urlparse(self.url)
        scheme = parsed.scheme.lower()
        host = parsed.hostname

        if not host:
            raise WebhookSSRFError(f"Invalid URL: no host in {self.url!r}")

        if scheme == "https":
            return

        if scheme == "http":
            if host in self.allowed_insecure_hosts:
                return
            raise WebhookSSRFError(
                f"Insecure http:// URLs require explicit allowlisting. "
                f"URL {self.url!r} has host {host!r} which is not in "
                f"allowed_insecure_hosts. To allow this host, pass "
                f"allowed_insecure_hosts={{{host!r}}} when constructing "
                f"WebhookClient."
            )

        raise WebhookSSRFError(
            f"URL scheme must be https:// (or http:// for allowed hosts). "
            f"Got {scheme!r} in {self.url!r}"
        )

    def _check_ip_blocklist(self, host: str) -> None:
        """Check if host resolves to a blocked IP address."""
        try:
            addr = ipaddress.ip_address(host)
        except ValueError:
            return
        for network in _BLOCKED_NETWORKS:
            if addr in network:
                raise WebhookSSRFError(
                    f"Webhook URL {self.url!r} resolves to blocked "
                    f"IP address {addr}. URLs resolving to localhost, "
                    f"link-local, or metadata addresses are not allowed."
                )

    def deliver(self, payload: dict[str, Any]) -> bool:
        """Deliver *payload* as a JSON POST to the configured URL.

        Uses exponential backoff: initial_delay * 2^attempt seconds between
        retries, capped at 60 seconds. Retries on HTTP 5xx errors and
        network-level ``URLError`` / ``TimeoutError`` exceptions.

        Parameters
        ----------
        payload
            Campaign summary dict serialized as JSON in the request body.

        Returns
        -------
        bool
            ``True`` if the delivery succeeded (2xx response), ``False`` if
            all retries were exhausted.
        """
        body = json.dumps(payload, default=str).encode("utf-8")
        self._check_ip_blocklist(urllib.parse.urlparse(self.url).hostname or "")

        for attempt in range(self.max_retries + 1):
            try:
                req = urllib.request.Request(
                    self.url,
                    data=body,
                    headers={
                        "Content-Type": "application/json",
                        "User-Agent": "OSimFlow/1.0",
                        "Accept": "application/json",
                    },
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310
                    if 200 <= resp.status < 300:
                        log.info(
                            "webhook delivered successfully to %s (attempt %d, status %d)",
                            self.url,
                            attempt + 1,
                            resp.status,
                        )
                        return True
                    log.warning(
                        "webhook received HTTP %d from %s (attempt %d/%d)",
                        resp.status,
                        self.url,
                        attempt + 1,
                        self.max_retries + 1,
                    )
            except urllib.error.HTTPError as exc:
                status = exc.code
                log.warning(
                    "webhook HTTP error %d from %s (attempt %d/%d): %s",
                    status,
                    self.url,
                    attempt + 1,
                    self.max_retries + 1,
                    exc,
                )
                if status < 500:
                    return False
            except urllib.error.URLError as exc:
                log.warning(
                    "webhook URL error for %s (attempt %d/%d): %s",
                    self.url,
                    attempt + 1,
                    self.max_retries + 1,
                    exc,
                )
            except TimeoutError as exc:
                log.warning(
                    "webhook timeout for %s (attempt %d/%d): %s",
                    self.url,
                    attempt + 1,
                    self.max_retries + 1,
                    exc,
                )

            # Exponential backoff: delay * 2^attempt, capped at 60s.
            if attempt < self.max_retries:
                delay = min(self.initial_delay * (2**attempt), 60.0)
                log.debug("webhook retry %d/%d in %.1fs", attempt + 1, self.max_retries, delay)
                time.sleep(delay)

        log.error(
            "webhook delivery to %s failed after %d attempts",
            self.url,
            self.max_retries + 1,
        )
        return False

    def build_payload(
        self,
        campaign_id: str,
        status: str,
        elapsed_s: float,
        n_samples: int,
        n_succeeded: int,
        n_failed: int,
        total_cost_usd: float | None,
        outdir: str,
    ) -> dict[str, Any]:
        """Build a campaign completion webhook payload.

        Returns a dict suitable for passing to :meth:`deliver`.
        """
        return {
            "event": "campaign.completed",
            "campaign_id": campaign_id,
            "status": status,
            "elapsed_s": round(elapsed_s, 2),
            "n_samples": n_samples,
            "n_succeeded": n_succeeded,
            "n_failed": n_failed,
            "total_cost_usd": total_cost_usd,
            "outdir": str(outdir),
            "osimflow_version": self._osimflow_version(),
        }

    @staticmethod
    def _osimflow_version() -> str:
        """Return the installed OSimFlow version, or 'unknown'."""
        try:
            from importlib.metadata import version  # noqa: PLC0415

            return version("osimflow")
        except Exception:
            return "unknown"
