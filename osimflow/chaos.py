"""Chaos testing utilities for OSimFlow resilience validation.

This module provides an opt-in chaos testing framework that allows
campaigns to inject controlled failures and stress conditions to
validate system resilience.

The framework is structured around a :class:`ChaosEngine` that manages
a collection of :class:`FaultInjector` instances. Each injector
implements a specific fault type (kill switch, network delay, CPU spike,
memory pressure). The engine can be used standalone or integrated
with the :class:`~osimflow.observability.ObservabilityBackend` interface.

Usage::

    from osimflow.chaos import ChaosEngine, KillSwitchSimulator, run_chaos_scenario

    # Standalone usage
    engine = ChaosEngine()
    engine.register(KillSwitchSimulator(fail_after=5))
    result = run_chaos_scenario(engine, target_fn, *args)

    # Integration with observability
    from osimflow.observability import ObservabilityBackend

    class ChaosObservabilityBackend(ObservabilityBackend):
        def __init__(self, chaos_engine: ChaosEngine):
            self._chaos = chaos_engine
            # ... existing backend setup ...

        def record_sample_metric(self, sample_id, metric_name, value, *, trace_id=None):
            # Inject chaos before recording
            self._chaos.inject(sample_id)
            # ... existing recording logic ...
"""

__all__ = [
    "ChaosEngine",
    "ChaosResult",
    "ChaosScenario",
    "CPUSpikeInjector",
    "FaultInjector",
    "FaultType",
    "KillSwitchInjector",
    "KillSwitchSimulator",
    "MemoryPressureInjector",
    "NetworkDelayInjector",
    "run_chaos_scenario",
]

import logging
import random
import threading
import time
import warnings
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

log = logging.getLogger("osimflow.chaos")


class FaultType(Enum):
    """Supported fault types for chaos injection."""

    KILL_SWITCH = "kill_switch"
    NETWORK_DELAY = "network_delay"
    CPU_SPIKE = "cpu_spike"
    MEMORY_PRESSURE = "memory_pressure"


@dataclass
class ChaosResult:
    """Result of a chaos injection.

    Attributes
    ----------
    fault_type
        The type of fault that was injected.
    target_id
        Identifier of the target (e.g., sample_id) the fault was injected on.
    injected
        Whether the fault was successfully injected.
    duration_s
        How long the fault lasted (for transient faults).
    error
        Error message if the injection failed.
    """

    fault_type: FaultType
    target_id: str
    injected: bool = False
    duration_s: float = 0.0
    error: str | None = None


@dataclass
class ChaosScenario:
    """A chaos testing scenario definition.

    Attributes
    ----------
    name
        Human-readable name for the scenario.
    injectors
        List of fault injectors to apply.
    probability
        Probability (0.0-1.0) that any given target will be affected.
    max_concurrent
        Maximum number of concurrent fault injections.
    """

    name: str
    injectors: list["FaultInjector"] = field(default_factory=list)
    probability: float = 1.0
    max_concurrent: int = 10


class FaultInjector(ABC):
    """Abstract base class for fault injectors.

    Subclass this to implement a specific fault type. Implement
    :meth:`inject` to perform the fault injection and :meth:`recover`
    to clean up after the fault (if needed).
    """

    @property
    @abstractmethod
    def fault_type(self) -> FaultType:
        """Return the type of fault this injector produces."""

    @abstractmethod
    def inject(self, target_id: str) -> ChaosResult:
        """Inject the fault into the target identified by *target_id*.

        Returns a :class:`ChaosResult` describing what happened.
        """

    def recover(self, target_id: str) -> None:
        """Recover from the fault injection (optional cleanup).

        The default implementation is a no-op. Override if your injector
        needs to clean up state after a fault.
        """
        return None


class KillSwitchSimulator(FaultInjector):
    """Simulate a kill switch that would terminate the target process.

    This injector logs a warning when the kill switch activates for a given
    target. It does **not** send an actual OS signal because the current
    wiring does not map sample IDs to worker PIDs or container IDs across
    all executors (issue #1179).

    Use this to validate that the campaign handles process termination
    gracefully — the warning fires and the sample is marked failed, but
    no actual process is terminated.

    Parameters
    ----------
    fail_after
        Number of calls after which the kill switch activates.
        If 0, the kill switch activates immediately on the first call.
    """

    name = "kill_switch_simulator"

    def __init__(
        self,
        fail_after: int = 1,
        signal_num: int | None = None,
        force: bool = False,
        **kwargs: Any,
    ) -> None:
        if signal_num is not None or force or kwargs:
            warnings.warn(
                "The signal_num and force parameters are deprecated and have no effect "
                "on KillSwitchSimulator (issue #1179).",
                DeprecationWarning,
                stacklevel=2,
            )
        self._fail_after = fail_after
        self._call_count: dict[str, int] = {}
        self._active: set[str] = set()

    @property
    def fault_type(self) -> FaultType:
        return FaultType.KILL_SWITCH

    def inject(self, target_id: str) -> ChaosResult:
        count = self._call_count.get(target_id, 0) + 1
        self._call_count[target_id] = count

        if count < self._fail_after:
            return ChaosResult(
                fault_type=self.fault_type,
                target_id=target_id,
                injected=False,
            )

        self._active.add(target_id)
        log.warning(
            "Kill switch simulator activated for target %s "
            "(no actual process was terminated — issue #1179)",
            target_id,
        )
        return ChaosResult(
            fault_type=self.fault_type,
            target_id=target_id,
            injected=True,
            duration_s=0.0,
        )

    def recover(self, target_id: str) -> None:
        self._active.discard(target_id)


def KillSwitchInjector(
    *args: Any,
    fail_after: int = 1,
    signal_num: int | None = None,
    force: bool = False,
    **kwargs: Any,
) -> KillSwitchSimulator:
    """Deprecated alias for :class:`KillSwitchSimulator`.

    ``KillSwitchInjector`` was renamed because the implementation only
    logs a warning and does not actually terminate a process (issue #1179).
    Use :class:`KillSwitchSimulator` for new code.

    The ``signal_num`` and ``force`` parameters are accepted for backward
    compatibility but have no effect (issue #1179).
    """
    warnings.warn(
        "KillSwitchInjector is deprecated, use KillSwitchSimulator instead "
        "(issue #1179)",
        DeprecationWarning,
        stacklevel=2,
    )
    return KillSwitchSimulator(fail_after=fail_after, signal_num=signal_num, force=force, **kwargs)


class NetworkDelayInjector(FaultInjector):
    """Inject network delay on outbound connections.

    This injector introduces an artificial delay before network operations,
    simulating degraded network conditions. The delay is applied via
    a threading Event that blocks for the specified duration.

    Parameters
    ----------
    delay_s
        Number of seconds to delay (default: 2.0).
    jitter_s
        Random jitter to add to the delay (default: 0.5).
    probability
        Probability (0.0-1.0) that any call actually experiences the delay.
    """

    def __init__(
        self,
        delay_s: float = 2.0,
        jitter_s: float = 0.5,
        probability: float = 1.0,
    ) -> None:
        self._delay_s = delay_s
        self._jitter_s = jitter_s
        self._probability = probability
        self._active: dict[str, threading.Event] = {}

    @property
    def fault_type(self) -> FaultType:
        return FaultType.NETWORK_DELAY

    def inject(self, target_id: str) -> ChaosResult:
        if random.random() > self._probability:
            return ChaosResult(
                fault_type=self.fault_type,
                target_id=target_id,
                injected=False,
            )

        delay = self._delay_s + random.uniform(-self._jitter_s, self._jitter_s)
        event = threading.Event()
        self._active[target_id] = event

        def _delayed_unblock() -> None:
            time.sleep(delay)
            event.set()

        thread = threading.Thread(target=_delayed_unblock, daemon=True)
        thread.start()

        log.debug("Network delay %.2fs activated for target %s", delay, target_id)
        return ChaosResult(
            fault_type=self.fault_type,
            target_id=target_id,
            injected=True,
            duration_s=delay,
        )

    def recover(self, target_id: str) -> None:
        if target_id in self._active:
            self._active[target_id].set()
            del self._active[target_id]


class CPUSpikeInjector(FaultInjector):
    """Inject a CPU spike that consumes CPU cycles.

    This injector spawns a CPU-intensive background thread that burns
    cycles for the specified duration, simulating high CPU load.

    Parameters
    ----------
    duration_s
        Duration of the CPU spike in seconds (default: 5.0).
    intensity
        CPU intensity as a fraction of available cores to consume (0.0-1.0).
    probability
        Probability (0.0-1.0) that any call triggers the spike.
    """

    def __init__(
        self,
        duration_s: float = 5.0,
        intensity: float = 0.8,
        probability: float = 1.0,
    ) -> None:
        self._duration_s = duration_s
        self._intensity = intensity
        self._probability = probability
        self._active: dict[str, threading.Event] = {}
        self._threads: dict[str, list[threading.Thread]] = {}

    @property
    def fault_type(self) -> FaultType:
        return FaultType.CPU_SPIKE

    def inject(self, target_id: str) -> ChaosResult:
        if random.random() > self._probability:
            return ChaosResult(
                fault_type=self.fault_type,
                target_id=target_id,
                injected=False,
            )

        stop_event = threading.Event()
        self._active[target_id] = stop_event
        threads: list[threading.Thread] = []
        num_threads = max(1, int(self._intensity * 4))  # 4 cores baseline

        def _cpu_burn() -> None:
            # Busy-wait to consume CPU cycles
            end = time.time() + self._duration_s
            while time.time() < end and not stop_event.is_set():
                _ = sum(range(1000))

        for _ in range(num_threads):
            t = threading.Thread(target=_cpu_burn, daemon=True)
            t.start()
            threads.append(t)

        self._threads[target_id] = threads
        log.debug("CPU spike activated for target %s (%d threads)", target_id, num_threads)
        return ChaosResult(
            fault_type=self.fault_type,
            target_id=target_id,
            injected=True,
            duration_s=self._duration_s,
        )

    def recover(self, target_id: str) -> None:
        if target_id in self._active:
            self._active[target_id].set()
            del self._active[target_id]
        if target_id in self._threads:
            for t in self._threads[target_id]:
                t.join(timeout=1.0)
            del self._threads[target_id]


class MemoryPressureInjector(FaultInjector):
    """Inject memory pressure by allocating a large buffer.

    This injector allocates a significant amount of memory that is held
    for the specified duration, simulating high memory usage conditions.

    Parameters
    ----------
    size_mb
        Size of the memory allocation in megabytes (default: 512).
    duration_s
        Duration to hold the memory in seconds (default: 10.0).
    probability
        Probability (0.0-1.0) that any call triggers the pressure.
    """

    def __init__(
        self,
        size_mb: int = 512,
        duration_s: float = 10.0,
        probability: float = 1.0,
    ) -> None:
        self._size_mb = size_mb
        self._duration_s = duration_s
        self._probability = probability
        self._buffers: dict[str, bytearray] = {}
        self._timers: dict[str, threading.Timer] = {}

    @property
    def fault_type(self) -> FaultType:
        return FaultType.MEMORY_PRESSURE

    def inject(self, target_id: str) -> ChaosResult:
        if random.random() > self._probability:
            return ChaosResult(
                fault_type=self.fault_type,
                target_id=target_id,
                injected=False,
            )

        try:
            # Allocate memory (1MB = 1024 * 1024 bytes)
            buffer = bytearray(self._size_mb * 1024 * 1024)
            self._buffers[target_id] = buffer

            def _release() -> None:
                if target_id in self._buffers:
                    del self._buffers[target_id]
                log.debug("Memory pressure released for target %s", target_id)

            timer = threading.Timer(self._duration_s, _release)
            timer.start()
            self._timers[target_id] = timer

            log.debug(
                "Memory pressure %.1f MB activated for target %s",
                self._size_mb,
                target_id,
            )
            return ChaosResult(
                fault_type=self.fault_type,
                target_id=target_id,
                injected=True,
                duration_s=self._duration_s,
            )
        except Exception as exc:  # noqa: BLE001
            log.error(
                "Memory pressure injection failed for %s: %s",
                target_id,
                exc,
                exc_info=True,
            )
            return ChaosResult(
                fault_type=self.fault_type,
                target_id=target_id,
                injected=False,
                error=str(exc),
            )

    def recover(self, target_id: str) -> None:
        if target_id in self._timers:
            self._timers[target_id].cancel()
            del self._timers[target_id]
        if target_id in self._buffers:
            del self._buffers[target_id]


class ChaosEngine:
    """Manages chaos injection for a campaign.

    The engine holds a collection of registered fault injectors and
    provides methods to inject faults into targets (e.g., sample IDs).
    The engine is thread-safe and can be used concurrently.

    Parameters
    ----------
    max_concurrent
        Maximum number of concurrent fault injections (default: 10).
    enabled
        Whether chaos injection is enabled (default: True). When False,
        all inject calls are no-ops.

    Example
    -------
    >>> engine = ChaosEngine()
    >>> engine.register(KillSwitchSimulator(fail_after=3))
    >>> engine.register(NetworkDelayInjector(delay_s=1.5))
    >>> result = engine.inject("sample-001")
    """

    def __init__(self, max_concurrent: int = 10, enabled: bool = True) -> None:
        self._injectors: list[FaultInjector] = []
        self._max_concurrent = max_concurrent
        self._enabled = enabled
        self._active_count = 0
        self._lock = threading.Lock()
        self._results: list[ChaosResult] = []

    @property
    def enabled(self) -> bool:
        """Whether chaos injection is enabled."""
        return self._enabled

    @enabled.setter
    def enabled(self, value: bool) -> None:
        self._enabled = value

    def register(self, injector: FaultInjector) -> None:
        """Register a fault injector with the engine.

        Parameters
        ----------
        injector
            The fault injector to register.
        """
        with self._lock:
            self._injectors.append(injector)

    def unregister(self, injector: FaultInjector) -> None:
        """Unregister a fault injector from the engine.

        Parameters
        ----------
        injector
            The fault injector to remove.
        """
        with self._lock:
            self._injectors.remove(injector)

    def inject(self, target_id: str) -> list[ChaosResult]:
        """Inject faults from all registered injectors into the target.

        This method applies all registered injectors to the target. Each
        injector decides whether to actually inject based on its own
        probability settings.

        Parameters
        ----------
        target_id
            Identifier of the target to inject faults into.

        Returns
        -------
        list[ChaosResult]
            List of results from each injector's attempt.
        """
        if not self._enabled:
            return []

        with self._lock:
            if self._active_count >= self._max_concurrent:
                log.warning(
                    "Max concurrent injections (%d) reached, skipping %s",
                    self._max_concurrent,
                    target_id,
                )
                return []
            self._active_count += 1

        try:
            results: list[ChaosResult] = []
            for injector in self._injectors:
                try:
                    result = injector.inject(target_id)
                    results.append(result)
                    if result.injected:
                        log.info(
                            "Injected %s for target %s",
                            result.fault_type.value,
                            target_id,
                        )
                except Exception as exc:  # noqa: BLE001
                    log.error(
                        "Injector %s failed for %s: %s",
                        injector.fault_type.value,
                        target_id,
                        exc,
                        exc_info=True,
                    )
                    results.append(
                        ChaosResult(
                            fault_type=injector.fault_type,
                            target_id=target_id,
                            injected=False,
                            error=str(exc),
                        )
                    )
            self._results.extend(results)
            return results
        finally:
            with self._lock:
                self._active_count -= 1

    def recover(self, target_id: str) -> None:
        """Recover all active faults for the target.

        Calls :meth:`FaultInjector.recover` for each registered injector.

        Parameters
        ----------
        target_id
            Identifier of the target to recover.
        """
        for injector in self._injectors:
            try:
                injector.recover(target_id)
            except Exception as exc:  # noqa: BLE001
                log.error(
                    "Recovery failed for %s (%s): %s",
                    target_id,
                    injector.fault_type.value,
                    exc,
                    exc_info=True,
                )

    def get_results(self) -> list[ChaosResult]:
        """Return all chaos injection results recorded so far."""
        with self._lock:
            return list(self._results)

    def clear_results(self) -> None:
        """Clear all recorded chaos injection results."""
        with self._lock:
            self._results.clear()


def run_chaos_scenario(
    scenario: ChaosScenario,
    target_fn: Callable[..., Any],
    *args: Any,
    **kwargs: Any,
) -> tuple[Any, list[ChaosResult]]:
    """Run a target function within a chaos scenario.

    This is a convenience function that creates a temporary ChaosEngine
    from the scenario, injects faults before calling *target_fn*, and
    returns the result along with the injection results.

    Parameters
    ----------
    scenario
        The chaos scenario to apply.
    target_fn
        The function to call under chaos conditions.
    *args
        Positional arguments to pass to *target_fn*.
    **kwargs
        Keyword arguments to pass to *target_fn*.

    Returns
    -------
    tuple[Any, list[ChaosResult]]
        A tuple of (target_fn result, list of ChaosResult from injections).

    Example
    -------
    >>> scenario = ChaosScenario(
    ...     name="network test",
    ...     injectors=[NetworkDelayInjector(delay_s=1.0)],
    ...     probability=0.5,
    ... )
    >>> result, chaos_results = run_chaos_scenario(scenario, my_function, arg1, arg2)
    """
    engine = ChaosEngine(
        max_concurrent=scenario.max_concurrent,
        enabled=True,
    )
    for injector in scenario.injectors:
        engine.register(injector)

    # Generate a target ID for the scenario run
    target_id = f"scenario-{scenario.name}-{int(time.time() * 1000)}"

    # Inject faults before calling the target
    inject_results = engine.inject(target_id)

    try:
        result = target_fn(*args, **kwargs)
        return result, inject_results
    finally:
        engine.recover(target_id)
