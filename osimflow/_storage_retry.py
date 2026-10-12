"""Transient-error retry split out of ``storage.py`` (issue #1890)."""

# ---------------------------------------------------------------------------
# Transient-error classification for remote storage I/O (issue #1398)
# ---------------------------------------------------------------------------
#
# Issue #1781: this module used to hand-roll an exponential-backoff
# loop with deterministic delays (``time.sleep(delay)``).  That loop
# is now a thin wrapper over the shared ``retry_with_backoff`` helper
# from :mod:`osimflow.executors.base` (issue #1540) so a fleet of
# concurrent workers hitting a transient 503 all wake up under full
# jitter rather than at the same deterministic instant.
#
# The ``_retry_transient_storage`` wrapper stays in ``storage.py`` so that
# ``patch("osimflow.storage._is_transient_storage_error")`` takes effect.
#
# Behaviour preserved:
#   - "3 attempts / 30s cap" (issue #1398 acceptance)
#   - same log.warning text — substrate-style ``on_retry`` callback
#     emits the historic line through the storage logger
#   - same permanent-error classification (5xx / throttling / 429)
#
# Behaviour changed:
#   - per-retry sleep is now ``random.uniform(0, min(delay, cap))``
#     instead of the deterministic ``delay`` schedule

#: Total attempts per operation (1 initial + 2 retries). Issue #1398
#: acceptance: "3 attempts / 30s cap".
_TRANSIENT_RETRY_ATTEMPTS = 3
#: Exponential backoff base — the window before jitter is
#: ``min(base * 2**(attempt-1), cap)`` (issue #1540 helper convention).
_TRANSIENT_RETRY_BASE_S = 2.0
#: Backoff ceiling so a pathological outage doesn't stall the orchestrator.
_TRANSIENT_RETRY_CAP_S = 30.0

#: Lowercased substrings that mark an exception as a *transient* storage
#: error — 5xx responses, throttling, and connection-level failures.
#: Non-matching exceptions propagate immediately (fail fast on permanent
#: errors such as 404 / auth denial).
_TRANSIENT_ERROR_MARKERS = (
    "503",
    "502",
    "500",
    "429",
    "slow down",
    "throttl",
    "service unavailable",
    "internal error",
    "bad gateway",
    "connection reset",
    "connection refused",
    "timed out",
    "timeout",
)


def _is_transient_storage_error(exc: BaseException) -> bool:
    """Return True when *exc* looks like a retryable transient storage error.

    ``ConnectionError`` covers ``urllib.error.URLError`` wrappers and
    socket-level resets; ``TimeoutError`` covers read/connect timeouts.
    Everything else is classified by scanning the exception text for
    retryable HTTP / provider markers (5xx, throttling, 429).
    """
    if isinstance(exc, ConnectionError | TimeoutError):
        return True
    text = f"{type(exc).__name__}: {exc}".lower()
    return any(marker in text for marker in _TRANSIENT_ERROR_MARKERS)
