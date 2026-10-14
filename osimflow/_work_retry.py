"""Moved from ``osimflow.work`` (issue #1890); re-exported there."""

import logging
import random
import signal
import subprocess
import time
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path
from typing import Any

from osimflow.errors import OSimFlowRuntimeError

log = logging.getLogger("osimflow.work")


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------
class SevereEnergyPlusError(OSimFlowRuntimeError):
    """Raised when a preflight simulation encounters severe errors."""


class TransientError(OSimFlowRuntimeError):
    """Raised when a simulation failure is potentially transient and retryable.

    Examples: network timeout, resource contention, temporary file lock,
    exit code indicating a recoverable condition.
    """


# ---------------------------------------------------------------------------
# Failure classifier (issue #1568)
#
# Single source of truth for whether a work-function failure should be
# retried. ``classify_failure`` returns a :class:`RetryDecision`; future
# contributors add cases here rather than scattering substring / exit-code
# checks across the per-step functions.
#
# Two bad interactions were closed in #1568:
#   1. ``_TRANSIENT_EXIT_CODES`` previously included ``-1`` and ``15``
#      (SIGTERM positive form), so a subprocess killed by SIGTERM during
#      graceful shutdown was retried up to ``max_retries`` times — fighting
#      every ``osimflow resume`` / ``osimflow cancel`` and stretching real-
#      CLI campaign cancellations by tens of minutes.
#   2. The message-substring scan matched "timeout" / "timed out" on
#      ``subprocess.TimeoutExpired`` itself. ``TimeoutExpired`` is already
#      classified non-transient by exception type (issue #1534), but the
#      message fallback was a second, redundant trigger that could mask
#      legitimate kills.
#
# Rule of thumb:
#   * Type-precise matching FIRST (TimeoutError, ConnectionError,
#     CalledProcessError with signal-kill returncode).
#   * Narrow message-fallback LAST — only for libraries that raise
#     stringy exceptions without proper exception types.
#   * Default = NON_TRANSIENT. Silent retries on unrecognised errors have
#     masked real bugs in the past; fail loud and let ``--max-sample-retries``
#     raise the retry budget when a new transient class is added.
# ---------------------------------------------------------------------------
class RetryDecision(StrEnum):
    """Outcome of :func:`classify_failure` (issue #1568).

    * ``TRANSIENT`` — retry with exponential backoff (network blip,
      resource contention, retryable I/O).
    * ``NON_TRANSIENT`` — deterministic; re-running does not change the
      outcome. Propagate immediately.
    * ``BOUNDED_ZERO_RETRY`` — reserved for a future bounded-retry class
      (e.g. a 3-strike rule on a specific flake); zero retries today.
    """

    TRANSIENT = "transient"
    NON_TRANSIENT = "non_transient"
    BOUNDED_ZERO_RETRY = "bounded_zero_retry"


# Subprocess exit codes that historically correlated with retryable
# resource contention / shell-side failures in upstream openstudio-server
# (positive form only — negative returncodes are signal kills and are
# classified by :data:`_NON_TRANSIENT_SUBPROCESS_SIGNALS` instead).
# Issue #1568 removed ``-1`` and ``15`` (SIGTERM positive form) — both
# were misclassified transient and stretched every cancellation by
# up to ``max_retries`` full simulation attempts.
_TRANSIENT_EXIT_CODES: frozenset[int] = frozenset([2, 4, 5, 6, 11, 12, 24, 25, 26, 27, 28])


# Signal numbers that are deliberate terminations and MUST NOT be retried
# (issue #1568). Python's subprocess surfaces a signal kill as
# ``returncode == -signal_number``; the classifier checks both forms.
# ``SIGKILL`` is POSIX-only (9) and absent from ``signal`` on Windows.
_NON_TRANSIENT_SUBPROCESS_SIGNALS: frozenset[int] = frozenset(
    {
        int(signal.SIGTERM),  # graceful cancellation / osimflow cancel
        int(getattr(signal, "SIGKILL", 9)),  # OOM-killer / hard kill
    }
)


# Narrow fallback message-scan — only consulted when type-precise matching
# missed. Intentionally excludes the bare substring ``"timeout"`` /
# ``"timed out"`` (issue #1568): those are covered by ``TimeoutError``
# type-precise matching and were a false-positive trap on
# ``subprocess.TimeoutExpired``.
_TRANSIENT_MESSAGE_MARKERS: tuple[str, ...] = (
    "connection refused",
    "connection reset",
    "network is unreachable",
    "resource busy",
    "too many open files",
    "disk full",
    "i/o error",
    "io error",
    "temporary failure",
)


def _is_signal_killed_subprocess(exc: BaseException) -> bool:
    """Return True if *exc* is a ``CalledProcessError`` killed by a deliberate signal.

    Python's subprocess surfaces signal kills as ``returncode == -signal_number``
    (e.g. ``-15`` for SIGTERM, ``-9`` for SIGKILL). The positive form is
    rare but covered defensively for completeness.

    Signal kills are NEVER transient (issue #1568): SIGTERM is graceful
    cancellation (``osimflow cancel`` / ``osimflow resume``); SIGKILL is
    usually the OOM-killer or a hard kill — re-executing the same sample
    would burn the same budget and hit the same wall again.
    """
    if not isinstance(exc, subprocess.CalledProcessError):
        return False
    rc = exc.returncode
    if rc is None:
        return False
    if rc < 0:
        return -rc in _NON_TRANSIENT_SUBPROCESS_SIGNALS
    return rc in _NON_TRANSIENT_SUBPROCESS_SIGNALS


def classify_failure(exc: BaseException) -> RetryDecision:
    """Classify a work-function failure into a retry decision (issue #1568).

    Order of checks — most specific first:

    1. :class:`subprocess.TimeoutExpired` → ``NON_TRANSIENT``. The
       configured wall-clock bound was hit; re-running burns the same
       budget and still fails (issue #1534). This MUST be checked before
       the broader :class:`TimeoutError` branch below because
       ``TimeoutExpired`` is a ``TimeoutError`` subclass on 3.11+.
    2. :class:`subprocess.CalledProcessError` killed by SIGTERM/SIGKILL
       → ``NON_TRANSIENT``. Deliberate termination; retry fights graceful
       shutdown (issue #1568).
    3. :class:`osimflow.work.TransientError` → ``TRANSIENT``. Explicit
       signal from the work layer that this is a retryable condition
       (e.g. stale container heartbeat — see ``run_openstudio_sim``).
    4. :class:`ConnectionError` or :class:`TimeoutError` (non-subprocess)
       → ``TRANSIENT``. Genuine network / read-timeout conditions raised
       by urllib3, requests, boto3, stdlib socket, etc.
    5. :class:`subprocess.CalledProcessError` whose ``returncode`` is in
       the curated :data:`_TRANSIENT_EXIT_CODES` set → ``TRANSIENT``.
    6. Fallback message-scan against :data:`_TRANSIENT_MESSAGE_MARKERS`
       → ``TRANSIENT`` if any marker substring matches.
    7. Default → ``NON_TRANSIENT``. Silent retries on unrecognised errors
       have masked real bugs in the past; fail loud and add a new branch
       here when a new transient class is identified.
    """
    # Hard non-transient: deliberate termination / wall-clock timeout.
    # Both MUST be checked before the broader ``TimeoutError`` branch
    # because ``subprocess.TimeoutExpired`` is a ``TimeoutError`` subclass
    # on 3.11+.
    if isinstance(exc, subprocess.TimeoutExpired) or _is_signal_killed_subprocess(exc):
        return RetryDecision.NON_TRANSIENT

    # Hard transient: explicit work-layer signal or genuine network failure.
    if isinstance(exc, (TransientError, ConnectionError, TimeoutError)):
        return RetryDecision.TRANSIENT

    # Subprocess exit-code classification — only applies when we have a
    # ``CalledProcessError``; default for that branch is ``NON_TRANSIENT``
    # (an unrecognised exit code should fail loud, not retry silently).
    if isinstance(exc, subprocess.CalledProcessError):
        rc = exc.returncode
        return (
            RetryDecision.TRANSIENT
            if rc is not None and rc in _TRANSIENT_EXIT_CODES
            else RetryDecision.NON_TRANSIENT
        )

    # Fallback message-scan — only consulted when type-precise matching
    # missed. Intentionally excludes bare "timeout" / "timed out" markers
    # (covered by the ``TimeoutError`` branch above).
    msg = str(exc).lower()
    if any(marker in msg for marker in _TRANSIENT_MESSAGE_MARKERS):
        return RetryDecision.TRANSIENT

    return RetryDecision.NON_TRANSIENT


def _is_transient_error(exc: BaseException) -> bool:
    """Back-compat boolean wrapper around :func:`classify_failure`.

    Existing call sites still expect a ``bool``; this shim keeps them
    green while the classifier carries the full decision surface.
    """
    return classify_failure(exc) == RetryDecision.TRANSIENT


def run_with_retry(
    func: Callable[..., Path],
    *args: Any,
    max_retries: int = 3,
    base_delay: float = 1.0,
    sample_id: str = "unknown",
    step_name: str = "step",
    **kwargs: Any,
) -> Path:
    """Run *func* with exponential-backoff retry for transient failures.

    Parameters
    ----------
    func
        Callable to invoke. Must accept *args and **kwargs.
    *args
        Positional arguments forwarded to *func*.
    max_retries
        Maximum retry attempts (default 3). A value <= 0 disables retries.
    base_delay
        Initial backoff delay in seconds (default 1.0). Each retry
        doubles the delay: delay = base_delay * 2**attempt.
    sample_id
        Identifier for log messages.
    step_name
        Step name for log messages.
    **kwargs
        Keyword arguments forwarded to *func*.

    Returns
    -------
    Path
        The return value of *func* on first success or after retries.

    Raises
    ------
    The last exception encountered when all retries are exhausted.
    """
    if max_retries <= 0:
        return func(*args, **kwargs)

    last_exc: BaseException | None = None
    for attempt in range(max_retries + 1):
        try:
            return func(*args, **kwargs)
        except BaseException as exc:  # noqa: BLE001
            if not _is_transient_error(exc):
                raise
            last_exc = exc
            if attempt == max_retries:
                break
            delay = min(base_delay * (2**attempt), 60.0)
            jittered_delay = random.uniform(0, delay)
            log.warning(
                "%s %s transient failure (attempt %d/%d), retrying in %.1fs: %s",
                step_name,
                sample_id,
                attempt + 1,
                max_retries,
                jittered_delay,
                exc,
            )
            time.sleep(jittered_delay)

    if last_exc is not None:
        raise last_exc
    raise RuntimeError("run_with_retry: unexpected code path")  # pragma: no cover
