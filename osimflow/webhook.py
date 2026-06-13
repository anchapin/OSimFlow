"""Webhook client for campaign completion callbacks (issue #283).

Delivers a POST to a user-configured URL when a campaign completes
successfully or with failure. The payload is a JSON summary of the
campaign run. Delivery is best-effort: webhook failures are logged but
do not crash or abort the campaign.

The client retries up to 3 times with exponential backoff (1s, 2s, 4s)
to handle transient network errors.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from typing import Any

log = logging.getLogger("osimflow.webhook")


class WebhookDeliveryError(Exception):
    """Raised when all retry attempts for a webhook delivery fail."""


class WebhookClient:
    """Delivers campaign completion webhooks with retry and exponential backoff.

    Parameters
    ----------
    url
        The target URL to POST to. Must be an http:// or https:// URL.
    timeout
        Request timeout in seconds (default: 30).
    max_retries
        Maximum number of retry attempts on failure (default: 3).
    initial_delay
        Initial backoff delay in seconds (default: 1.0).
    """

    def __init__(
        self,
        url: str,
        timeout: float = 30.0,
        max_retries: int = 3,
        initial_delay: float = 1.0,
    ) -> None:
        self.url = url
        self.timeout = timeout
        self.max_retries = max_retries
        self.initial_delay = initial_delay

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
