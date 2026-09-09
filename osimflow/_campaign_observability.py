"""ObservabilityManager for Campaign — wraps ObservabilityBackend lifecycle.

This module extracts all observability operations from the Campaign class,
including:
- Backend construction via CampaignConfig
- Per-step duration recording
- Per-sample metric recording
- Campaign duration and flush
- Periodic flush to prevent metric loss on early crash (issue #1186)
- Multi-backend composition with coordinated flush (issue #1332)
- Record-path exception containment (issue #1668)

The ObservabilityManager is constructed with a CampaignConfig and exposes
a clean interface for the Campaign to call without directly coupling to
the ObservabilityBackend implementation.

When multiple backends are configured (issue #1332), the ObservabilityManager
coordinates flush across all backends, catching per-backend exceptions
individually and re-raising only after all backends have attempted flush.

Reliability contract (issue #1668): every ``record_*`` method contains
backend exceptions at this layer.  The Campaign calls ``record_sample_status``
from inside the fan-out success callback (``_on_success``), so a backend
exception escaping the record path — e.g. CloudWatch throttling on the
inline 20-metric buffer flush inside ``CloudWatchBackend._add_metric`` —
would be caught by ``_await_one``'s ``except Exception`` and route a
successful sample through ``mark_sample_failed``.  A backend failure is
logged and swallowed instead; it must never fail the campaign or distort
sample accounting.
"""

from __future__ import annotations

__all__ = ["ObservabilityManager"]

import logging
import threading
from collections.abc import Callable

from .config import CampaignConfig
from .observability import (
    CloudWatchBackend,
    NullBackend,
    ObservabilityBackend,
    OpenTelemetryBackend,
    PrometheusBackend,
    new_trace_id,
)

log = logging.getLogger("osimflow.campaign")


class ObservabilityManager:
    """Manages observability backend lifecycle and metric recording.

    This class encapsulates all observability operations for a Campaign,
    constructing the appropriate backend(s) from CampaignConfig at initialization
    and providing a clean interface for recording metrics throughout the
    campaign lifecycle.

    When multiple backends are configured (e.g., "cloudwatch,prometheus"),
    all record methods fan out to every backend, and flush() coordinates
    across all of them, catching per-backend exceptions individually and
    re-raising only after every backend has attempted its flush (issue
    #1332).

    Record-path containment (issue #1668): every ``record_*`` method
    catches and logs per-backend exceptions and never propagates them
    to the caller — the Campaign invokes these from fan-out callbacks
    where an escaping exception would mark a successful sample failed.

    Parameters
    ----------
    cfg
        Campaign configuration used to determine which backend(s) to instantiate.

    Attributes
    ----------
    backend : ObservabilityBackend
        The primary observability backend (read-only).  When multiple backends
        are active this is the first one in the list; use ``backends`` to
        access all of them.
    backends : list[ObservabilityBackend]
        All active backends (never empty — ``"none"`` produces a list
        containing only the NullBackend).
    """

    def __init__(self, cfg: CampaignConfig) -> None:
        self._cfg = cfg
        self._backends: list[ObservabilityBackend] = self._build_backends(cfg)
        self._periodic_flush_thread: threading.Thread | None = None
        self._periodic_flush_stop_event = threading.Event()

    @property
    def backend(self) -> ObservabilityBackend:
        """The primary backend (backwards-compatible alias for single-backend code)."""
        return self._backends[0]

    @property
    def backends(self) -> list[ObservabilityBackend]:
        """All active backends (never empty)."""
        return self._backends

    # ------------------------------------------------------------------
    # Backend construction
    # ------------------------------------------------------------------
    @staticmethod
    def _build_backends(cfg: CampaignConfig) -> list[ObservabilityBackend]:
        """Instantiate one or more observability backends from config.

        Returns a list containing only NullBackend when
        ``cfg.observability == "none"`` (zero overhead — all methods are
        empty ``pass`` bodies).

        Multiple backends can be specified by passing a comma-separated string
        (e.g., ``"cloudwatch,prometheus"``) or a list of backend names.
        Each backend is constructed with the shared CampaignConfig settings.
        """
        backend_types = ObservabilityManager._parse_observability(cfg.observability)
        if not backend_types or backend_types == ["none"]:
            return [NullBackend()]
        backends: list[ObservabilityBackend] = []
        for backend_type in backend_types:
            backend = ObservabilityManager._build_single_backend(cfg, backend_type)
            if backend is not None:
                backends.append(backend)
        if not backends:
            return [NullBackend()]
        return backends

    @staticmethod
    def _parse_observability(value: str | list[str]) -> list[str]:
        """Parse ``observability`` config value into a list of backend names."""
        if isinstance(value, list):
            return value
        return [v.strip() for v in value.split(",") if v.strip()]

    @staticmethod
    def _build_single_backend(
        cfg: CampaignConfig, backend_type: str
    ) -> ObservabilityBackend | None:
        """Instantiate a single named backend, or None if the type is unknown."""
        if backend_type == "cloudwatch":
            return CloudWatchBackend(namespace=cfg.cloudwatch_namespace)
        if backend_type == "prometheus":
            return PrometheusBackend(pushgateway_url=f"localhost:{cfg.prometheus_port}")
        if backend_type == "opentelemetry":
            endpoint = cfg.otel_endpoint or "http://localhost:4317"
            return OpenTelemetryBackend(endpoint=endpoint)
        if backend_type in ("none", ""):
            return None
        log.warning("unknown observability backend type: %s — ignoring", backend_type)
        return None

    # ------------------------------------------------------------------
    # Record-path containment (issue #1668)
    # ------------------------------------------------------------------
    def _fanout_record(
        self,
        op: str,
        record: Callable[[ObservabilityBackend], None],
    ) -> None:
        """Fan a record operation out to every backend, containing failures.

        Each backend is attempted in turn; an exception raised by one
        backend (e.g. ``CloudWatchBackend`` hitting a throttling error on
        the inline buffer flush triggered from ``record_sample_metric``)
        is logged and swallowed so that (a) it never propagates into the
        Campaign hot path — the record methods are invoked from the
        fan-out success/failure callbacks, where an escaping exception
        would mark a successful sample failed — and (b) the remaining
        backends still receive the metric (mirroring the per-backend
        coordination of :meth:`flush`, issue #1332).

        This containment is deliberate: an observability-backend failure
        must never fail the campaign.  Buffering backends retain their
        buffered metrics on a failed flush, so the periodic flush
        (issue #1186) retries delivery later.
        """
        for backend in self._backends:
            try:
                record(backend)
            except Exception as exc:  # noqa: BLE001 — containment is the point (issue #1668)
                log.error(
                    "observability backend %s failed to record %s "
                    "(metric dropped; campaign unaffected): %s",
                    type(backend).__name__,
                    op,
                    exc,
                    exc_info=True,
                )

    # ------------------------------------------------------------------
    # Per-step metrics
    # ------------------------------------------------------------------
    def record_step_duration(
        self,
        step_name: str,
        duration_s: float,
        generation: int = 0,
    ) -> None:
        """Record a DAG step duration.

        Parameters
        ----------
        step_name
            The step name (e.g., "APPLY_PARAMETERS", "RUN_OPENSTUDIO_SIM").
        duration_s
            Elapsed time in seconds.
        generation
            Generation number for iterative algorithms.
        """
        self._fanout_record(
            f"step_duration:{step_name}",
            lambda backend: backend.record_step_duration(
                step_name, duration_s, generation=generation
            ),
        )

    # ------------------------------------------------------------------
    # Per-sample metrics
    # ------------------------------------------------------------------
    def record_sample_metric(
        self,
        sample_id: str,
        metric_name: str,
        value: float,
        trace_id: str | None = None,
    ) -> None:
        """Record a per-sample metric.

        Parameters
        ----------
        sample_id
            The sample identifier.
        metric_name
            Metric name (e.g., "cost_usd", "status").
        value
            Metric value.
        trace_id
            Optional trace ID for distributed correlation.
        """
        self._fanout_record(
            f"sample_metric:{metric_name}",
            lambda backend: backend.record_sample_metric(
                sample_id, metric_name, value, trace_id=trace_id
            ),
        )

    def record_sample_cost(
        self, sample_id: str, cost_usd: float | None, trace_id: str | None = None
    ) -> None:
        """Record per-sample cost metric.

        Convenience helper that forwards to record_sample_metric with
        "cost_usd" as the metric name.
        """
        if cost_usd is not None:
            self._fanout_record(
                "sample_metric:cost_usd",
                lambda backend: backend.record_sample_metric(
                    sample_id, "cost_usd", cost_usd, trace_id=trace_id
                ),
            )

    def record_sample_status(
        self,
        sample_id: str,
        status: str,
        trace_id: str | None = None,
    ) -> None:
        """Record per-sample status metric.

        Convenience helper that converts status string to 1.0/0.0
        and forwards to record_sample_metric with "status" as the metric name.

        Parameters
        ----------
        sample_id
            The sample identifier.
        status
            Status string ("ok" or "failed").
        trace_id
            Optional trace ID for distributed correlation.
        """
        value = 1.0 if status == "ok" else 0.0
        self._fanout_record(
            "sample_metric:status",
            lambda backend: backend.record_sample_metric(
                sample_id, "status", value, trace_id=trace_id
            ),
        )

    # ------------------------------------------------------------------
    # Campaign-level metrics
    # ------------------------------------------------------------------
    def record_campaign_duration(self, duration_s: float) -> None:
        """Record the total campaign duration.

        Parameters
        ----------
        duration_s
            Total elapsed time in seconds.
        """
        self._fanout_record(
            "campaign_duration",
            lambda backend: backend.record_campaign_duration(duration_s),
        )

    def flush(self) -> None:
        """Flush all buffered metrics to every backend (issue #1332).

        Calls ``flush()`` on every configured backend, catching any exception
        raised by a backend so that flush attempts on the remaining backends
        are not suppressed.  After all backends have attempted to flush, the
        first exception encountered is re-raised; if no backend raised an
        exception, the method returns normally.

        This ensures that a failing flush on one backend (e.g., CloudWatch
        throttling) does not silently suppress flushes on other backends
        (e.g., Prometheus pushgateway).
        """
        errors: list[Exception] = []
        for backend in self._backends:
            try:
                backend.flush()
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "observability backend %s flush failed: %s",
                    type(backend).__name__,
                    exc,
                    exc_info=True,
                )
                errors.append(exc)
        if errors:
            raise errors[0]

    def start_periodic_flush(self) -> None:
        """Start a background thread that periodically flushes metrics (issue #1186).

        The periodic flush ensures metrics are not lost if the process crashes
        before the campaign finishes normally. The thread runs until
        ``stop_periodic_flush()`` is called.

        The flush interval is controlled by ``flush_interval_seconds`` in
        ``CampaignConfig`` (default: 30 seconds).
        """
        if self._periodic_flush_thread is not None:
            log.warning("periodic flush thread already running")
            return
        if all(isinstance(b, NullBackend) for b in self._backends):
            log.debug("periodic flush skipped for NullBackend only")
            return
        flush_interval = getattr(self._cfg, "flush_interval_seconds", 30.0)
        self._periodic_flush_stop_event.clear()
        self._periodic_flush_thread = threading.Thread(
            target=self._periodic_flush_loop,
            args=(flush_interval,),
            name="observability-periodic-flush",
            daemon=True,
        )
        self._periodic_flush_thread.start()
        log.info("periodic observability flush started (interval=%.1fs)", flush_interval)

    def stop_periodic_flush(self) -> None:
        """Stop the periodic flush background thread.

        Also performs a final coordinated flush to ensure all metrics are delivered.
        """
        if self._periodic_flush_thread is None:
            return
        log.info("stopping periodic observability flush")
        self._periodic_flush_stop_event.set()
        thread = self._periodic_flush_thread
        self._periodic_flush_thread = None
        thread.join(timeout=5.0)
        self.flush()
        log.info("periodic observability flush stopped")

    def _periodic_flush_loop(self, interval_seconds: float) -> None:
        """Background loop that periodically flushes metrics to all backends."""
        while not self._periodic_flush_stop_event.wait(timeout=interval_seconds):
            try:
                self.flush()
                log.debug("periodic observability flush completed")
            except Exception:  # noqa: BLE001
                log.exception("periodic observability flush failed")

    # ------------------------------------------------------------------
    # Trace ID helpers
    # ------------------------------------------------------------------
    @staticmethod
    def mint_trace_id() -> str:
        """Generate a new per-sample trace ID.

        Returns the first 8 hex characters of a UUID4 (e.g., ``"a1b2c3d4"``).
        """
        return new_trace_id()
