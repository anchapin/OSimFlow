"""Storage endpoint validation split out of ``storage.py`` (issue #1890)."""

from __future__ import annotations

import logging
from urllib.parse import urlparse

log = logging.getLogger("osimflow.storage")


_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0"})


def _validate_storage_endpoint(endpoint_url: str | None, *, allow_insecure: bool = False) -> None:
    """Validate that an S3 endpoint URL uses TLS (issue #1386).

    Mirrors :func:`osimflow.distributed_cache.validate_redis_url`: rejects
    ``http://`` endpoints unless the operator has explicitly opted in with
    ``allow_insecure=True`` (typically only for local MinIO / dev).  The
    loopback hosts (localhost, ``127.0.0.1``, ``::1``, ``0.0.0.0``) are
    exempt because they never traverse a real network — same exception
    the Redis validator uses.

    Parameters
    ----------
    endpoint_url
        The candidate endpoint URL.  ``None`` and ``""`` pass silently —
        there is no custom endpoint to validate.
    allow_insecure
        When ``True``, ``http://`` endpoints are accepted with a loud
        ``WARNING``.  Defaults to ``False`` (fail-closed).

    Raises
    ------
    ValueError
        When ``endpoint_url`` is non-empty, non-loopback, and uses a
        scheme other than ``https`` while ``allow_insecure`` is ``False``.
    """
    if not endpoint_url:
        return

    parsed = urlparse(endpoint_url)
    scheme = (parsed.scheme or "").lower()
    host = (parsed.hostname or "").lower()

    if scheme == "https":
        return

    if scheme != "http":
        raise ValueError(
            f"invalid S3 endpoint URL (issue #1386): {endpoint_url!r} "
            f"uses scheme {scheme!r}; expected 'https://'. "
            f"Non-HTTPS endpoints are rejected unless "
            f"--allow-insecure-storage-endpoint is set."
        )

    if host in _LOOPBACK_HOSTS:
        return

    if allow_insecure:
        log.warning(
            "INSECURE S3 endpoint (issue #1386): %s uses plaintext HTTP; "
            "SigV4 signing material and campaign artifacts will traverse "
            "the network in cleartext. This is allowed because "
            "--allow-insecure-storage-endpoint was set — do not use in "
            "production.",
            endpoint_url,
        )
        return

    raise ValueError(
        f"insecure S3 endpoint URL (issue #1386): {endpoint_url!r} uses "
        f"plaintext HTTP and is not a loopback host. Non-HTTPS storage "
        f"endpoints leak AWS SigV4 signing material and campaign "
        f"artifacts in cleartext. Use https:// or pass "
        f"--allow-insecure-storage-endpoint to override (dev/test only)."
    )
