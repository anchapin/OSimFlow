"""Shared token-bucket rate limiter (issue #1563).

The previous fan-out throttling surface was inconsistent: ``AWSBatchExecutor``
shipped a private ``_TokenBucketRateLimiter`` shared process-wide via
``--aws-batch-submit-rps`` (issue #1010), ``NomadExecutor`` shipped an ad-hoc
``fanout_submit_rate_per_sec`` pacing hook used only by the Campaign-side
``_fanout_submit_interval_s`` (no token bucket, just ``time.sleep`` between
submits), and the other eight substrates had nothing. With the 1000+-sample
fan-out ambition (issues #1013, #1559) every substrate needs the same
throttle primitive so Kubernetes 429s, Azure Batch pool limits, and Nomad
plan-applies don't blow up the run.

This module is the single owner of the token-bucket implementation that
lived as a private class inside ``aws_batch_executor.py``. Every executor's
``BaseExecutor.submit()`` funnels through one of these limiters (via
``BaseExecutor._rate_limiter``, set in each subclass's ``__init__``) before
calling the substrate-specific ``_do_submit()`` template method.

The implementation is intentionally identical to the pre-#1563 AWS limiter:

* thread-safe — the token bucket is guarded by a single ``threading.Lock``;
* cooperative — ``acquire()`` sleeps outside the lock so other threads can
  refill while a slow thread waits;
* disable-aware — ``rate_per_sec <= 0`` short-circuits to a no-op so test
  suites can construct an executor with throttling off without paying the
  lock cost.
"""

from __future__ import annotations

__all__ = ["TokenBucketRateLimiter"]

import threading
import time
from typing import ClassVar


class TokenBucketRateLimiter:
    """Thread-safe token-bucket rate limiter for executor submit throttling.

    The bucket has capacity ``burst`` tokens and refills at ``rate_per_sec``
    tokens per second. ``acquire()`` blocks until a token is available,
    then consumes one. ``rate_per_sec <= 0`` disables the limiter entirely
    (``acquire()`` becomes a no-op) so test paths and the in-process
    :class:`~osimflow.executors.LocalExecutor` (no I/O quota to bump
    against) don't pay the lock cost.

    A class-level ``shared_instances`` cache lets every executor that
    resolves to the same effective RPS share a single bucket — without
    this, each new executor object would mint its own limiter and the
    aggregate fan-out rate across executor instances would exceed the
    configured RPS (the bug the pre-#1563 AWS limiter's ``get_shared``
    classmethod was built to avoid; see issue #1010). The cache is keyed
    on the *effective* RPS (after default substitution) plus an explicit
    ``name`` so two distinct executors requesting different names get
    distinct buckets — important when an in-process
    :class:`~osimflow.executors.LocalExecutor` and a remote substrate are
    instantiated simultaneously.
    """

    @staticmethod
    def _default_burst(rps: float) -> int:
        """Default burst = max(1, ceil(rate_per_sec)) — a fresh bucket
        can deliver ~1s worth of work as a burst before throttling kicks in.
        Set to 1 when ``rate_per_sec`` is fractional/zero so the bucket
        capacity never accidentally drops below one token. ``inf`` is
        treated as a large finite rate; the limiter still uses a
        minimal capacity since the bucket is effectively a no-op.
        """
        import math

        if rps <= 0 or math.isnan(rps):
            return 1
        # ``int(rps)`` overflows on ``float('inf')``; cap at 1 since
        # the bucket is disabled in that case.
        if rps == float("inf"):
            return 1
        return max(1, int(rps))

    #: Shared per-(name, rps) buckets so concurrent executors sharing the
    #: same effective RPS stay within the quota. ``_SHARED_LOCK`` guards
    #: the dict itself; ``_INSTANCES_LOCK`` is dead and intentionally not
    #: used (single ``_SHARED_LOCK`` is sufficient because insertions are
    #: rare and lock-protected; the per-instance lock lives on each
    #: ``TokenBucketRateLimiter``).
    _SHARED_INSTANCES: ClassVar[dict[tuple[str, float], TokenBucketRateLimiter]] = {}
    _SHARED_LOCK: ClassVar[threading.Lock] = threading.Lock()

    def __init__(
        self,
        rate_per_sec: float,
        *,
        burst: int | None = None,
        name: str = "default",
    ) -> None:
        """Construct a standalone limiter.

        Most callers should prefer :meth:`get_shared` so concurrent
        executors cooperate on a single bucket. Direct construction is
        appropriate for tests that need an isolated bucket.
        """
        self.name = name
        self._rate = max(0.0, float(rate_per_sec))
        self._disabled = self._rate <= 0.0
        self._capacity: int = max(
            1, int(burst) if burst is not None else self._default_burst(self._rate)
        )
        if self._disabled:
            # Keep _capacity >= 1 so a disabled limiter never underflows
            # on a hypothetical acquire(); the disabled short-circuit makes
            # the value semantically meaningless anyway.
            self._capacity = 1
        self._tokens: float = float(self._capacity) if not self._disabled else 1.0
        self._last_refill: float = time.monotonic()
        self._lock: threading.Lock = threading.Lock()

    @classmethod
    def get_shared(
        cls,
        rate_per_sec: float,
        *,
        name: str = "default",
    ) -> TokenBucketRateLimiter:
        """Return a process-shared limiter for the requested ``(name, rate_per_sec)``.

        Concurrent executor instances that resolve to the same effective
        RPS (via ``BaseExecutor.default_submit_rps`` or an override)
        share the same bucket, so the aggregate submit rate across
        executor instances stays bounded (the original AWS Batch
        use case, issue #1010). ``rate_per_sec <= 0`` returns a
        process-shared disabled limiter — disabled limiters are still
        memoised so test suites can compare identities.
        """
        effective: float = max(0.0, float(rate_per_sec))
        key = (name, effective)
        with cls._SHARED_LOCK:
            existing = cls._SHARED_INSTANCES.get(key)
            if existing is not None:
                return existing
            limiter = cls(effective, name=name)
            cls._SHARED_INSTANCES[key] = limiter
            return limiter

    @classmethod
    def _reset_shared_for_testing(cls) -> None:
        """Drop every shared bucket. Tests use this to avoid bucket bleed."""
        with cls._SHARED_LOCK:
            cls._SHARED_INSTANCES.clear()

    def acquire(self) -> None:
        """Block until a token is available, then consume one."""
        if self._disabled:
            return
        while True:
            with self._lock:
                now = time.monotonic()
                elapsed = now - self._last_refill
                self._tokens = min(
                    float(self._capacity),
                    self._tokens + elapsed * self._rate,
                )
                self._last_refill = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                deficit = 1.0 - self._tokens
                wait_s = deficit / self._rate
            # Sleep outside the lock so other threads aren't blocked
            # while we're waiting.
            time.sleep(wait_s)

    @property
    def rate_per_sec(self) -> float:
        """Configured steady-state rate (0 = disabled)."""
        return self._rate

    @property
    def capacity(self) -> int:
        """Maximum burst size."""
        return self._capacity

    @property
    def disabled(self) -> bool:
        """Whether the limiter is a no-op (``rate_per_sec <= 0``)."""
        return self._disabled
