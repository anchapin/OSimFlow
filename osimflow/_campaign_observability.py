"""ObservabilityManager for Campaign — wraps ObservabilityBackend lifecycle.

This module extracts all observability operations from the Campaign class,
including:
- Backend construction via CampaignConfig
- Per-step duration recording
- Per-sample metric recording
- Campaign duration and flush

The ObservabilityManager is constructed with a CampaignConfig and exposes
a clean interface for the Campaign to call without directly coupling to
the ObservabilityBackend implementation.
"""

from __future__ import annotations

import logging

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
    constructing the appropriate backend from CampaignConfig at initialization
    and providing a clean interface for recording metrics throughout the
    campaign lifecycle.

    Parameters
    ----------
    cfg
        Campaign configuration used to determine which backend to instantiate.

    Attributes
    ----------
    backend : ObservabilityBackend
        The underlying observability backend ( NullBackend when observability
        is disabled for zero overhead).
    """

    def __init__(self, cfg: CampaignConfig) -> None:
        self._backend: ObservabilityBackend = self._build_backend(cfg)

    # ------------------------------------------------------------------
    # Backend construction
    # ------------------------------------------------------------------
    @staticmethod
    def _build_backend(cfg: CampaignConfig) -> ObservabilityBackend:
        """Instantiate the correct observability backend from config.

        Returns NullBackend when ``cfg.observability == "none"`` (zero
        overhead — all methods are empty ``pass`` bodies).
        """
        backend_type = cfg.observability
        if backend_type == "none":
            return NullBackend()
        if backend_type == "cloudwatch":
            return CloudWatchBackend(
                namespace=cfg.cloudwatch_namespace,
            )
        if backend_type == "prometheus":
            return PrometheusBackend(
                pushgateway_url=f"localhost:{cfg.prometheus_port}",
            )
        if backend_type == "opentelemetry":
            endpoint = cfg.otel_endpoint or "http://localhost:4317"
            return OpenTelemetryBackend(endpoint=endpoint)
        raise ValueError(f"unknown observability backend: {backend_type}")

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
        self._backend.record_step_duration(step_name, duration_s, generation=generation)

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
        self._backend.record_sample_metric(sample_id, metric_name, value, trace_id=trace_id)

    def record_sample_cost(
        self, sample_id: str, cost_usd: float, trace_id: str | None = None
    ) -> None:
        """Record per-sample cost metric.

        Convenience helper that forwards to record_sample_metric with
        "cost_usd" as the metric name.
        """
        if cost_usd is not None:
            self._backend.record_sample_metric(sample_id, "cost_usd", cost_usd, trace_id=trace_id)

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
        self._backend.record_sample_metric(sample_id, "status", value, trace_id=trace_id)

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
        self._backend.record_campaign_duration(duration_s)

    def flush(self) -> None:
        """Flush any buffered metrics to the backend.

        Call this at campaign end to ensure all metrics are delivered.
        """
        self._backend.flush()

    # ------------------------------------------------------------------
    # Trace ID helpers
    # ------------------------------------------------------------------
    @staticmethod
    def mint_trace_id() -> str:
        """Generate a new per-sample trace ID.

        Returns the first 8 hex characters of a UUID4 (e.g., ``"a1b2c3d4"``).
        """
        return new_trace_id()
