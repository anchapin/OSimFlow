"""Lifecycle control for Campaign: cancellation, pause/resume, signals.

This module extracts the graceful-shutdown machinery from
``osimflow.campaign`` (issue #1462; issues #255 / #553 / #649 / #798
originally introduced the behaviours):

- the process-global :class:`CancelRegistry` singleton that routes
  SIGINT/SIGTERM to the currently-running Campaign,
- the in-memory cancel flag (sticky) and the non-latched ``.pause``
  flag-file check,
- ``.stop`` flag-file polling with cross-process ``fcntl.flock``
  protection (TOCTOU fix, issue #649),
- SIGINT/SIGTERM handler installation / restoration,
- shutdown and paused ``run.json`` trace writes.

Mirrors the ``_campaign_cost_tracker.py`` collaborator pattern:
:class:`CampaignLifecycle` owns the flags and locks; Campaign keeps
thin delegating methods so the historical instance-API surface
(``request_cancel``, ``pause``, ``resume``, ``_check_cancel_requested``,
...) is unchanged.  ``_CancelRegistry`` / ``_cancel_registry`` remain
importable from ``osimflow.campaign`` via re-export (tests rely on it).

Issue #1542: this module no longer imports or type-references
``Campaign``.  The cancel/pause/resume surface Campaign exposes is
captured by the explicit :class:`CancelSignal` protocol —
``Campaign`` satisfies it structurally (it has ``request_cancel`` /
``request_pause`` / ``request_resume`` methods), so lifecycle
behavior is testable and reusable without constructing (or faking)
a full Campaign.
"""

import fcntl
import logging
import os
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from .errors import OSimFlowError
from .executors import BaseExecutor
from .json_utils import safe_json_dumps
from .monitoring import RunTrace

log = logging.getLogger("osimflow.campaign")


@runtime_checkable
class CancelSignal(Protocol):
    """Explicit cancel/pause/resume surface a lifecycle owner depends on.

    Issue #1542: replaces the former ``Campaign`` back-reference.
    ``Campaign`` (and any test double) satisfies this protocol
    structurally by providing the three request callbacks; the
    lifecycle layer never needs the concrete Campaign type.
    """

    def request_cancel(self) -> None:
        """Request campaign cancellation (sticky, thread-safe)."""
        ...

    def request_pause(self) -> None:
        """Request a soft pause (running samples complete; new ones skip)."""
        ...

    def request_resume(self) -> None:
        """Resume a paused campaign."""
        ...


class CampaignPauseRequested(OSimFlowError):
    """Internal control-flow signal: a soft pause was requested (issue #1537).

    Raised by ``Campaign`` step fan-outs / pre-step checks when the
    ``.pause`` flag file is detected (issue #553).  It is deliberately
    NOT a :class:`KeyboardInterrupt`: ``Campaign.run()`` maps
    ``KeyboardInterrupt`` to *cancellation* (cancel active jobs, set
    ``finished_at``, status ``"cancelled"``), while this signal keeps
    the documented pause semantics — ``run.json`` status stays
    ``"paused"``, ``finished_at`` is not set, and running jobs are left
    to complete.  ``Campaign.run()`` then *returns*: the ``osimflow
    run`` process exits and there is no live orchestrator parked on the
    flag.  Recovery is cache replay — ``osimflow resume`` clears the
    flag and re-launches the recorded run (issue #1628), or the user
    re-runs ``osimflow run --outdir <same>`` manually.

    Only ``Campaign.run()`` catches this; it never escapes ``run()``.
    """


class CancelRegistry:
    """Global registry holding the currently-running Campaign for signal handling.

    When a SIGINT/SIGTERM is received, the signal handler calls
    ``request_cancel()`` on whatever :class:`CancelSignal` is registered
    here. Only one Campaign can run at a time per process — the registry
    is updated at ``run()`` entry and cleared on exit.

    Issue #1542: the slot is typed as the narrow ``CancelSignal``
    protocol rather than ``Campaign`` — the registry only ever calls
    ``request_cancel()`` on it, so the concrete type (and the circular
    ``_campaign_lifecycle`` → ``campaign`` reference) is unnecessary.
    """

    def __init__(self) -> None:
        self._signal: CancelSignal | None = None
        self._lock = threading.Lock()

    def register(self, signal_target: CancelSignal) -> None:
        with self._lock:
            self._signal = signal_target

    def request_cancel(self) -> None:
        with self._lock:
            if self._signal is not None:
                self._signal.request_cancel()

    def clear(self) -> None:
        with self._lock:
            self._signal = None


cancel_registry = CancelRegistry()

# Issue #1685: SIGINT escalation counter.  The first SIGINT requests
# cancellation (preserved pre-#1685 behaviour — the Campaign unwinds
# through the existing finally block so ``run.json`` is rewritten and
# the webhook fires).  A second SIGINT restores the default handler
# and re-raises the signal so the operator can escape a wedged
# ``run_init_script`` / ``run_finalize_script`` (or any other step
# that ignores the cancel flag) without resorting to SIGKILL — which
# would forfeit the graceful run.json write that restart-by-replay
# depends on.  Tracked per-signal so SIGTERM escalation is
# independent of SIGINT escalation.
_sigint_count: int = 0
_sigterm_count: int = 0


def handle_signal(signum: int, _frame: object) -> None:
    """Signal handler that requests cancellation on the registered Campaign.

    Uses the global registry so the signal can reach the running Campaign
    even though the signal callback only receives (signum, frame).

    Issue #1685: the first invocation of a given signal (SIGINT or
    SIGTERM) requests cancellation and returns, preserving the
    pre-#1685 behaviour.  A second invocation of the same signal
    escalates by restoring the default signal handler and re-raising
    the signal — this gives the operator an escape hatch against a
    wedged hook subprocess (init/finalize) or any step that ignores
    the cancel flag, without forcing them to reach for SIGKILL (which
    would skip the graceful run.json rewrite that restart-by-replay
    depends on).
    """
    global _sigint_count, _sigterm_count  # noqa: PLW0603
    sig_name = signal.Signals(signum).name
    if signum == signal.SIGINT.value:
        _sigint_count += 1
        if _sigint_count >= 2:
            log.warning(
                "received second SIGINT — escalating: restoring default "
                "handler and re-raising so the operator can escape a "
                "wedged hook (issue #1685)",
            )
            # Restore default handler so the third SIGINT is honoured
            # by Python's default KeyboardInterrupt path.  Re-raise
            # the current signal to actually trigger the escalation
            # *now* (the second signal would otherwise be swallowed by
            # the default handler re-installing our handler again on
            # some platforms).
            signal.signal(signal.SIGINT, signal.SIG_DFL)
            os.kill(os.getpid(), signal.SIGINT)
            return
    elif signum == signal.SIGTERM.value:
        _sigterm_count += 1
        if _sigterm_count >= 2:
            log.warning(
                "received second SIGTERM — escalating: restoring default "
                "handler and re-raising so the operator can escape a "
                "wedged hook (issue #1685)",
            )
            signal.signal(signal.SIGTERM, signal.SIG_DFL)
            os.kill(os.getpid(), signal.SIGTERM)
            return
    log.warning("received %s — requesting cancellation", sig_name)
    cancel_registry.request_cancel()


class CampaignLifecycle:
    """Owns cancellation, pause, and signal-handling state for a Campaign.

    Issue #1542: the constructor receives an explicit
    :class:`CancelSignal` protocol (the Campaign wires itself — or any
    object exposing ``request_cancel`` / ``request_pause`` /
    ``request_resume`` — in at construction) instead of holding the
    ``Campaign`` type, so the lifecycle collaborator carries no
    circular back-reference.
    """

    def __init__(self, signal: CancelSignal | None = None) -> None:
        # The cancel/pause/resume surface of the owning campaign (or any
        # other lifecycle-capable object). Structural: no Campaign
        # import is needed (issue #1542).
        self.signal = signal
        # Graceful shutdown (issue #255): cancellation flag and lock.
        self._cancel_requested = False
        self._cancel_lock = threading.Lock()
        # Soft pause (issue #553): pause flag and lock.
        self._pause_requested = False
        self._pause_lock = threading.Lock()
        # Original signal handlers — restored on exit.
        self._prev_sigint: Any = None
        self._prev_sigterm: Any = None

    # ------------------------------------------------------------------
    # Cancellation
    # ------------------------------------------------------------------
    @property
    def cancel_requested(self) -> bool:
        """Whether cancellation has been requested (sticky flag)."""
        with self._cancel_lock:
            return self._cancel_requested

    def request_cancel(self) -> None:
        """Request campaign cancellation.

        Called by the signal handler or by external code that wants to
        stop a running campaign. Thread-safe. Idempotent.
        """
        with self._cancel_lock:
            self._cancel_requested = True
        log.warning("campaign cancellation requested")

    def check_cancel_requested(self, outdir: Path) -> bool:
        """Check if cancellation has been requested.

        Checks both the in-memory flag and the ``.stop`` file in the
        outdir. The ``.stop`` file is written by the REST API's
        ``POST /api/v1/campaign/stop`` endpoint (issue #143) and by
        external tooling that wants to interrupt a running campaign.

        Returns:
            ``True`` if cancellation is requested, ``False`` otherwise.
        """
        # Fast path: check the in-memory flag first (no file I/O).
        with self._cancel_lock:
            if self._cancel_requested:
                return True

        # Check the .stop file with cross-process file locking to close the
        # TOCTOU race window (issue #649). Using fcntl.flock() ensures that
        # between checking "does .stop file exist" and acting on that check,
        # no other process can interfere (on POSIX systems).
        stop_file = outdir / ".stop"
        try:
            # Open existing file (fail if it doesn't exist; we don't create it).
            # O_NOFOLLOW prevents symlink attacks.
            fd = os.open(str(stop_file), os.O_RDWR | os.O_NOFOLLOW)
        except OSError:
            # File does not exist or is not accessible — no cancel requested.
            return False

        try:
            if sys.platform == "win32":
                # On Windows, msvcrt.locking does not support exclusive locks.
                # Fall back to a simple existence check inside the open fd.
                # The advisory locking on Windows is less robust than POSIX
                # flock, so we rely on the atomic rename from the API server
                # for safety.
                file_exists = True
            else:
                try:
                    # Acquire exclusive lock (non-blocking). If we get it, we're
                    # the sole accessor and can safely check the file state.
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    file_exists = stop_file.is_file()
                except BlockingIOError:
                    # Another process holds a conflicting lock — we cannot
                    # safely read the file state. Treat as cancel requested
                    # (conservative: better to cancel when we shouldn't than
                    # to miss a cancel request).
                    file_exists = True
            try:
                if file_exists:
                    log.warning(".stop file detected — requesting cancellation")
                    with self._cancel_lock:
                        self._cancel_requested = True
                    return True
            finally:
                if sys.platform != "win32":
                    fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
        return False

    def cancel_active_jobs(self, executor: BaseExecutor) -> None:
        """Cancel all active jobs on the executor's substrate (issues #255, #1538).

        Called during graceful shutdown to stop in-flight work as quickly
        as possible: ``executor.cancel()`` sweeps every live handle and
        issues its substrate kill (TerminateJob / scancel / batch delete /
        allocation stop / local subprocess terminate). The call is
        defensive — a substrate-wide failure is logged but never
        propagates, so the shutdown path always continues to the final
        ``run.json`` write. Safe to call repeatedly; the executor's
        handle registry is cleared by the first sweep.
        """
        log.info("canceling active executor jobs")
        try:
            executor.cancel()
        except Exception:  # noqa: BLE001 — never block the shutdown trace
            log.warning("executor cancel raised", exc_info=True)
        log.info("executor cancel requested")

    def write_shutdown_trace(
        self, trace: RunTrace, outdir: Path, status: str = "cancelled"
    ) -> None:
        """Write run.json with cancellation status before exit.

        Marks the campaign as cancelled so a re-run can resume correctly.
        """
        try:
            trace.status = status
            trace.write(outdir / "run.json")
            log.info("wrote cancellation trace to run.json")
        except Exception as exc:
            log.warning("could not write cancellation trace: %s", exc, exc_info=True)

    # ------------------------------------------------------------------
    # Soft pause / resume (issue #553)
    # ------------------------------------------------------------------
    def reset_pause(self) -> None:
        """Clear the pause flag so a re-run is not born paused (issue #798)."""
        with self._pause_lock:
            self._pause_requested = False

    def check_pause_requested(self, outdir: Path) -> bool:
        """Check if pause has been requested via the ``.pause`` file.

        The ``.pause`` file is written by the REST API's
        ``POST /api/v1/campaign/pause`` endpoint (issue #553) or by the
        CLI ``osimflow pause`` command.

        Unlike the cancelled flag, the pause flag is NOT latched — we
        check the file existence on every call so that deleting the
        ``.pause`` file immediately unblocks new submissions (issue #798).

        Returns:
            ``True`` if pause is requested, ``False`` otherwise.
        """
        pause_file = outdir / ".pause"
        if pause_file.is_file():
            log.warning(".pause file detected — pausing new submissions")
            return True
        return False

    def pause(self, outdir: Path, trace: RunTrace) -> None:
        """Request campaign pause (soft-stop).

        Writes a ``.pause`` flag file to the campaign directory and
        records the ``paused_at`` timestamp in the run trace.  Running
        samples complete normally; the executor checks for the ``.pause``
        file between sample dispatches and skips queuing new ones.

        Thread-safe and idempotent.
        """
        pause_file = outdir / ".pause"
        safe_json_dumps({"requested_at": time.time()}, pause_file)
        trace.status = "paused"
        trace.paused_at = time.time()
        trace.write(outdir / "run.json")
        log.warning("campaign pause requested (paused_at=%.0f)", trace.paused_at)

    def resume(self, outdir: Path, trace: RunTrace) -> None:
        """Clear the pause flag and reset the trace's paused state.

        Removes the ``.pause`` flag file, clears the in-memory pause
        flag, and resets ``run.json`` from ``"paused"`` to
        ``"running"``.  This does NOT by itself continue a paused
        campaign: a paused campaign has already exited
        ``Campaign.run()`` — there is no live fan-out loop waiting on
        the flag.  The end-user recovery is ``osimflow resume``, which
        clears this flag and re-launches the recorded ``osimflow run``
        invocation so completed steps replay from cache (issue #1628).

        Thread-safe and idempotent.
        """
        pause_file = outdir / ".pause"
        if pause_file.is_file():
            pause_file.unlink()
        with self._pause_lock:
            self._pause_requested = False
        trace.status = "running"
        trace.paused_at = None
        trace.write(outdir / "run.json")
        log.warning("campaign resume requested")

    def write_paused_trace(self, trace: RunTrace, outdir: Path) -> None:
        """Write run.json with paused status when a soft-pause is triggered.

        Sets ``trace.status = "paused"`` and records the ``paused_at``
        timestamp so a subsequent resume can continue from where the
        campaign left off.
        """
        try:
            trace.status = "paused"
            trace.paused_at = time.time()
            outdir.mkdir(parents=True, exist_ok=True)
            trace.write(outdir / "run.json")
            log.info("wrote paused trace to run.json (paused_at=%.0f)", trace.paused_at)
        except Exception as exc:
            log.warning("could not write paused trace: %s", exc, exc_info=True)

    # ------------------------------------------------------------------
    # Signal handlers
    # ------------------------------------------------------------------
    def setup_signal_handlers(self) -> None:
        """Register SIGINT/SIGTERM handlers to request graceful shutdown.

        Saves the previous handlers so they can be restored on exit.
        When a signal is received, ``request_cancel()`` is called on the
        Campaign registered in the global :data:`cancel_registry`.
        """
        self._prev_sigint = signal.signal(signal.SIGINT, handle_signal)
        self._prev_sigterm = signal.signal(signal.SIGTERM, handle_signal)
        log.debug("signal handlers registered (SIGINT/SIGTERM)")

    def restore_signal_handlers(self) -> None:
        """Restore the previous signal handlers."""
        if self._prev_sigint is not None:
            signal.signal(signal.SIGINT, self._prev_sigint)
        if self._prev_sigterm is not None:
            signal.signal(signal.SIGTERM, self._prev_sigterm)
        log.debug("signal handlers restored")
