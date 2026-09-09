"""Webhook client for campaign completion callbacks (issue #283).

Delivers a POST to a user-configured URL when a campaign completes
successfully or with failure. The payload is a JSON summary of the
campaign run. Delivery is best-effort: webhook failures are logged but
do not crash or abort the campaign.

The client retries up to 3 times with exponential backoff (1s, 2s, 4s)
to handle transient network errors.

SSRF hardening (issue #1671): webhook target hosts are resolved via
``socket.getaddrinfo`` and every resolved A/AAAA record is checked
against ``_BLOCKED_NETWORKS`` (loopback, RFC1918, CGNAT, link-local,
and metadata ranges). Unresolvable hosts are rejected fail-closed.
Redirect targets are re-validated (scheme + host + IP blocklist) on
every hop.
"""

from __future__ import annotations

import http.client
import ipaddress
import json
import logging
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import IO, Any

from .errors import OSimFlowError

log = logging.getLogger("osimflow.webhook")

_BLOCKED_NETWORKS: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = [
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
]


class WebhookSSRFError(OSimFlowError):
    """Raised when a webhook URL fails SSRF validation."""


class WebhookDeliveryError(OSimFlowError):
    """Raised when all retry attempts for a webhook delivery fail."""


def _resolved_addresses(
    host: str,
) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """Return every IP address *host* is or resolves to (fail-closed).

    Literal IPs are parsed directly. Hostnames are resolved via
    ``socket.getaddrinfo`` (both A and AAAA records); a resolution
    failure is treated as blocked (issue #1671 fail-closed posture)
    rather than allowed through unchecked.
    """
    try:
        return [ipaddress.ip_address(host)]
    except ValueError:
        pass

    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except (socket.gaierror, OSError) as exc:
        raise WebhookSSRFError(
            f"Could not resolve webhook host {host!r} — treating it as "
            f"blocked (fail-closed): {exc}"
        ) from exc

    addresses: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    seen: set[str] = set()
    for info in infos:
        sockaddr = info[4]
        raw = str(sockaddr[0]).split("%", 1)[0]  # strip IPv6 zone id (fe80::1%eth0)
        try:
            addr = ipaddress.ip_address(raw)
        except ValueError:
            continue
        if str(addr) not in seen:
            seen.add(str(addr))
            addresses.append(addr)

    if not addresses:
        raise WebhookSSRFError(
            f"Webhook host {host!r} resolved to no usable IP addresses — "
            f"treating it as blocked (fail-closed)."
        )
    return addresses


def _address_variants(
    addr: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """Unwrap IPv4-mapped IPv6 addresses so ``::ffff:a.b.c.d`` cannot bypass the v4 blocklist."""
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        return [addr, addr.ipv4_mapped]
    return [addr]


class _SSRFValidatingRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Redirect handler that re-validates every hop (issue #1671).

    urllib's default ``HTTPRedirectHandler`` follows ``Location``
    headers blindly. This subclass re-applies the owning client's
    scheme policy and IP blocklist (including DNS resolution) to each
    redirect target before following it, so a public HTTPS URL cannot
    bounce the POST to an internal (e.g. metadata) endpoint.
    """

    def __init__(self, client: WebhookClient) -> None:
        self._client = client

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: http.client.HTTPMessage,
        newurl: str,
    ) -> urllib.request.Request | None:
        self._client._validate_url(newurl)
        self._client._check_ip_blocklist(urllib.parse.urlparse(newurl).hostname or "")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


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
        Set of hosts allowed to use http:// (insecure) instead of
        https://. This exempts the host from the https:// scheme
        requirement only — the IP blocklist is always enforced, on the
        initial URL and on the host of every redirect hop (issue
        #1671). By default, no insecure HTTP is allowed.
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
        self._redirect_handler = _SSRFValidatingRedirectHandler(self)
        self._opener = urllib.request.build_opener(self._redirect_handler)
        self._validate_url(self.url)

    def _validate_url(self, url: str) -> None:
        """Validate a URL's scheme/host to prevent SSRF (issues #1175, #1671).

        - Requires https:// by default
        - Allows http:// only for hosts in allowed_insecure_hosts
          (scheme exemption only; the IP blocklist always applies)
        - Blocks localhost, link-local, RFC1918, CGNAT, and metadata IPs
          (see ``_check_ip_blocklist``)

        Called on the configured URL and re-called on every redirect
        hop via ``_SSRFValidatingRedirectHandler``.
        """
        parsed = urllib.parse.urlparse(url)
        scheme = parsed.scheme.lower()
        host = parsed.hostname

        if not host:
            raise WebhookSSRFError(f"Invalid URL: no host in {url!r}")

        if scheme == "https":
            return

        if scheme == "http":
            if host in self.allowed_insecure_hosts:
                return
            raise WebhookSSRFError(
                f"Insecure http:// URLs require explicit allowlisting. "
                f"URL {url!r} has host {host!r} which is not in "
                f"allowed_insecure_hosts. To allow this host, pass "
                f"allowed_insecure_hosts={{{host!r}}} when constructing "
                f"WebhookClient."
            )

        raise WebhookSSRFError(
            f"URL scheme must be https:// (or http:// for allowed hosts). "
            f"Got {scheme!r} in {url!r}"
        )

    def _check_ip_blocklist(self, host: str) -> None:
        """Reject when *host* is — or resolves to — a blocked IP address.

        Literal IPs are checked directly. Hostnames are resolved via
        DNS (``socket.getaddrinfo``) and rejected when ANY resolved
        A/AAAA record falls in a blocked network (issue #1671 — a DNS
        name pointing at 169.254.169.254 or 127.0.0.1 must not slip
        through). Resolution failure is rejected fail-closed. IPv4-
        mapped IPv6 addresses are unwrapped before checking.
        """
        if not host:
            raise WebhookSSRFError(f"Webhook URL {self.url!r} has no host to validate.")
        for resolved in _resolved_addresses(host):
            for addr in _address_variants(resolved):
                for network in _BLOCKED_NETWORKS:
                    if addr in network:
                        raise WebhookSSRFError(
                            f"Webhook URL {self.url!r} (host {host!r}) resolves to "
                            f"blocked IP address {resolved}. URLs targeting "
                            f"localhost, link-local, RFC1918, CGNAT, or metadata "
                            f"addresses are not allowed."
                        )

    def deliver(self, payload: dict[str, Any]) -> bool:
        """Deliver *payload* as a JSON POST to the configured URL.

        Uses exponential backoff: initial_delay * 2^attempt seconds between
        retries, capped at 60 seconds. Retries on HTTP 5xx errors and
        network-level ``URLError`` / ``TimeoutError`` exceptions.

        SSRF validation (issue #1671) is applied to the configured URL
        and to every redirect hop; a blocked target (or a host that
        cannot be resolved — fail-closed) aborts delivery immediately
        with ``False`` and no retry.

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
        try:
            self._check_ip_blocklist(urllib.parse.urlparse(self.url).hostname or "")
        except WebhookSSRFError:
            log.error(
                "webhook to %s blocked by SSRF validation (host resolves to a "
                "blocked range or could not be resolved)",
                self.url,
            )
            return False

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
                with self._opener.open(req, timeout=self.timeout) as resp:  # noqa: S310
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
            except WebhookSSRFError as exc:
                # A redirect hop pointed somewhere forbidden (issue #1671).
                # Do not retry: the target is blocked, not transiently failing.
                log.error(
                    "webhook to %s blocked by SSRF validation on a redirect "
                    "hop: %s",
                    self.url,
                    exc,
                )
                return False
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
