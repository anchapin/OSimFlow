"""Circuit breaker for Redis-backed coordination layers (issue #1111).

When Redis suffers a *persistent* outage, every cache hit/miss and every
document-store operation previously attempted a fresh Redis call that
burned its full socket timeout (5 s) before degrading or failing. For a
1 000-sample campaign that is 24 000+ wasted attempts — tens of hours of
cumulative stall time and thousands of duplicate warning lines.

The :class:`CircuitBreaker` implements the classic closed → open →
half-open pattern:

* **closed** — normal operation; consecutive failures are counted.
* **open** — after ``failure_threshold`` consecutive failures, callers
  are rejected immediately (fail-fast) for ``cooldown_s`` seconds.
* **half-open** — once the cooldown elapses, one probe request is
  allowed through: success closes the circuit, failure re-opens it.

The breaker is thread-safe so it can be shared between the sync data
plane and the pub/sub machinery inside one process.
"""

from __future__ import annotations

__all__ = ["CircuitBreaker", "CircuitOpenError"]

import threading
import time
from collections.abc import Callable

from .errors import OSimFlowRuntimeError


class CircuitOpenError(OSimFlowRuntimeError):
    """Raised when an operation is attempted while the circuit is open."""


class CircuitBreaker:
    """Thread-safe closed/open/half-open circuit breaker.

    Args:
        name: Human-readable identifier used in log/error messages.
        failure_threshold: Consecutive failures before the circuit opens.
        cooldown_s: Seconds the circuit stays open before allowing one
            half-open probe.
        on_transition: Optional callback ``(name, from_state, to_state) -> None``
            invoked on every state change. Intended to forward events to an
            :class:`~osimflow.observability.ObservabilityBackend`.
        clock: Monotonic clock used for cooldown math. Inject a controllable
            callable in tests to age the breaker past ``cooldown_s``
            deterministically without real ``time.sleep`` (issue #1481).
            When ``None`` (the default) the breaker calls
            :func:`time.monotonic` on every invocation so callers that
            ``monkeypatch.setattr("osimflow.circuit_breaker.time.monotonic",
            ...)`` keep working.
    """

    def __init__(
        self,
        name: str = "redis",
        failure_threshold: int = 5,
        cooldown_s: float = 30.0,
        *,
        on_transition: Callable[[str, str, str], None] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.name = name
        self.failure_threshold = max(int(failure_threshold), 1)
        self.cooldown_s = max(float(cooldown_s), 0.0)
        self._lock = threading.Lock()
        self._consecutive_failures = 0
        self._state = "closed"
        self._opened_at = 0.0
        self._on_transition = on_transition
        self._clock = clock

    def _now(self) -> float:
        """Return the current monotonic time, honoring an injected clock.

        The injected ``clock`` callable is invoked as-is; when no clock was
        supplied the breaker falls back to :func:`time.monotonic` on every
        call so tests that monkeypatch the module attribute still see the
        patched value (the previous direct ``time.monotonic()`` call in
        this method broke that contract once we started storing the clock
        on the instance).
        """
        if self._clock is None:
            return time.monotonic()
        return self._clock()

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------
    @property
    def state(self) -> str:
        """Current circuit state: ``closed``, ``open``, or ``half_open``."""
        with self._lock:
            return self._effective_state()

    @property
    def consecutive_failures(self) -> int:
        """Number of consecutive failures recorded (reset on success)."""
        with self._lock:
            return self._consecutive_failures

    def _effective_state(self) -> str:
        """Return the state after applying cooldown transitions (lock held)."""
        if self._state == "open" and (self._now() - self._opened_at) >= self.cooldown_s:
            self._state = "half_open"
        return self._state

    # ------------------------------------------------------------------
    # Gate + outcome recording
    # ------------------------------------------------------------------
    def allow(self) -> bool:
        """Return True when an operation may proceed.

        Transitions ``open`` → ``half_open`` once the cooldown has elapsed;
        in ``half_open`` exactly one probe per ``record_success`` /
        ``record_failure`` cycle is admitted.
        """
        with self._lock:
            prev_state = self._state
            state = self._effective_state()
            if state != prev_state and self._on_transition is not None:
                self._on_transition(self.name, prev_state, state)
            # closed: normal operation. half_open: admit the single probe;
            # its outcome resolves back to closed or re-opens.
            return state in ("closed", "half_open")

    def check(self) -> None:
        """Like :meth:`allow` but raises :class:`CircuitOpenError`."""
        if not self.allow():
            raise CircuitOpenError(
                f"circuit {self.name!r} is open after "
                f"{self.failure_threshold} consecutive failures — "
                f"fail-fast for {self.cooldown_s:g}s cooldown"
            )

    def record_success(self) -> None:
        """Record a successful operation (closes the circuit)."""
        with self._lock:
            prev_state = self._state
            self._consecutive_failures = 0
            self._state = "closed"
            if prev_state != "closed" and self._on_transition is not None:
                self._on_transition(self.name, prev_state, "closed")

    def set_on_transition_callback(self, callback: Callable[[str, str, str], None] | None) -> None:
        """Set or clear the state-transition callback (issue #1310).

        Can be called after construction to wire the breaker to an
        ObservabilityBackend without requiring the callback at init time.
        """
        with self._lock:
            self._on_transition = callback

    def record_failure(self) -> None:
        """Record a failed operation (opens the circuit past threshold)."""
        with self._lock:
            self._consecutive_failures += 1
            was_half_open = self._state == "half_open"
            if self._consecutive_failures >= self.failure_threshold or was_half_open:
                prev_state = self._state
                self._state = "open"
                self._opened_at = self._now()
                if was_half_open:
                    self._consecutive_failures = 0
                if self._on_transition is not None:
                    self._on_transition(self.name, prev_state, "open")
