"""Observability backends for OSimFlow campaigns.

Provides an abstract base class (:class:`ObservabilityBackend`) that
defines the metrics interface the Campaign calls at key lifecycle points.
Four built-in implementations:

* :class:`NullBackend` — no-op default when no observability is configured.
* :class:`CloudWatchBackend` — AWS CloudWatch metrics with batched uploads.
* :class:`PrometheusBackend` — Prometheus pushgateway with lazy ``prometheus_client``.
* :class:`OpenTelemetryBackend` — OTLP exporter with lazy ``opentelemetry-sdk``.

Third-party backends (Datadog, etc.) can be added by subclassing
:class:`ObservabilityBackend` and passing the instance to the Campaign
constructor.

Usage::

    from osimflow.observability import CloudWatchBackend

    backend = CloudWatchBackend(namespace="MyOrg/SimCampaigns", region="us-east-1")
    # ... pass to Campaign; it calls record_step_duration(), etc.
    backend.flush()  # ensure any buffered metrics are sent
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any

log = logging.getLogger("osimflow.observability")


class ObservabilityBackend(ABC):
    """Abstract base class for observability backends.

    Subclass this to add new backends (Prometheus, Datadog, etc.).
    The Campaign calls these methods at key lifecycle points.
    """

    @abstractmethod
    def record_step_duration(self, step_name: str, duration_s: float, generation: int = 0) -> None:
        """Record a DAG step duration."""

    @abstractmethod
    def record_sample_metric(self, sample_id: str, metric_name: str, value: float) -> None:
        """Record a per-sample metric (e.g., EUI, simulation time)."""

    @abstractmethod
    def record_campaign_duration(self, duration_s: float) -> None:
        """Record total campaign wall-clock time."""

    @abstractmethod
    def flush(self) -> None:
        """Flush any buffered metrics."""


class NullBackend(ObservabilityBackend):
    """No-op backend — default when no observability is configured."""

    def record_step_duration(self, step_name: str, duration_s: float, generation: int = 0) -> None:
        pass

    def record_sample_metric(self, sample_id: str, metric_name: str, value: float) -> None:
        pass

    def record_campaign_duration(self, duration_s: float) -> None:
        pass

    def flush(self) -> None:
        pass


class CloudWatchBackend(ObservabilityBackend):
    """CloudWatch metrics backend using boto3.

    Metrics are batched and flushed periodically to avoid API throttling.
    Requires ``pip install osimflow[aws]``.

    Parameters
    ----------
    namespace
        CloudWatch namespace for the metrics (default: ``"OSimFlow/Campaign"``).
    region
        AWS region for the CloudWatch client.  When ``None``, boto3
        resolves the region from the environment / instance profile.
    """

    # CloudWatch PutMetricData supports up to 20 MetricData items per call.
    _FLUSH_SIZE = 20

    def __init__(
        self,
        namespace: str = "OSimFlow/Campaign",
        region: str | None = None,
    ) -> None:
        self._namespace = namespace
        self._region = region
        self._buffer: list[dict[str, Any]] = []
        self._client: Any = None  # boto3 CloudWatch client, lazy

    def _get_client(self) -> Any:
        """Lazy-import boto3 to avoid hard dependency."""
        if self._client is None:
            try:
                import boto3  # noqa: PLC0415
            except ImportError:
                raise ImportError(
                    "CloudWatch backend requires boto3. Install with: pip install osimflow[aws]"
                ) from None
            kwargs: dict[str, Any] = {"service_name": "cloudwatch"}
            if self._region:
                kwargs["region_name"] = self._region
            self._client = boto3.client(**kwargs)
        return self._client

    def _add_metric(
        self,
        metric_name: str,
        value: float,
        dimensions: list[dict[str, str]] | None = None,
    ) -> None:
        """Buffer a metric for batched upload."""
        datum: dict[str, Any] = {
            "MetricName": metric_name,
            "Value": value,
            "Unit": "None",
        }
        if dimensions:
            datum["Dimensions"] = dimensions
        self._buffer.append(datum)
        if len(self._buffer) >= self._FLUSH_SIZE:
            self.flush()

    def record_step_duration(self, step_name: str, duration_s: float, generation: int = 0) -> None:
        self._add_metric(
            "StepDuration",
            duration_s,
            [
                {"Name": "StepName", "Value": step_name},
                {"Name": "Generation", "Value": str(generation)},
            ],
        )

    def record_sample_metric(self, sample_id: str, metric_name: str, value: float) -> None:
        self._add_metric(
            metric_name,
            value,
            [{"Name": "SampleId", "Value": sample_id}],
        )

    def record_campaign_duration(self, duration_s: float) -> None:
        self._add_metric("CampaignDuration", duration_s)

    def flush(self) -> None:
        if not self._buffer:
            return
        client = self._get_client()
        client.put_metric_data(
            Namespace=self._namespace,
            MetricData=self._buffer,
        )
        log.debug("Flushed %d metrics to CloudWatch", len(self._buffer))
        self._buffer = []


class PrometheusBackend(ObservabilityBackend):
    """Prometheus pushgateway backend.  Lazy-imports ``prometheus_client``.

    Metrics are buffered as gauge values and pushed to the pushgateway on
    :meth:`flush`.  Requires ``pip install prometheus_client``.

    Parameters
    ----------
    pushgateway_url
        URL of the Prometheus pushgateway (default: ``"localhost:9091"``).
    job_name
        Prometheus job label (default: ``"osimflow"``).
    """

    _FLUSH_SIZE = 20

    def __init__(
        self,
        pushgateway_url: str = "localhost:9091",
        job_name: str = "osimflow",
    ) -> None:
        self._url = pushgateway_url
        self._job = job_name
        self._buffer: list[tuple[str, dict[str, str], float]] = []
        # Lazy-initialised prometheus_client artefacts
        self._registry: Any = None
        self._gauges: dict[tuple[str, tuple[tuple[str, str], ...]], Any] = {}

    def _ensure_registry(self) -> Any:
        """Lazy-import ``prometheus_client`` and return a ``CollectorRegistry``."""
        if self._registry is None:
            try:
                from prometheus_client import CollectorRegistry  # noqa: PLC0415
            except ImportError:
                raise ImportError(
                    "Prometheus backend requires prometheus_client. "
                    "Install with: pip install prometheus_client"
                ) from None
            self._registry = CollectorRegistry()
            self._gauges = {}
        return self._registry

    def _get_or_create_gauge(self, name: str, labels: dict[str, str]) -> Any:
        """Return a cached ``Gauge`` for *name*, creating one if needed."""
        key = (name, tuple(sorted(labels.items())))
        if key not in self._gauges:
            from prometheus_client import Gauge  # noqa: PLC0415

            label_names = list(labels.keys())
            self._gauges[key] = Gauge(
                name,
                f"OSimFlow metric {name}",
                labelnames=label_names,
                registry=self._registry,
            )
        return self._gauges[key]

    def _add_metric(self, name: str, labels: dict[str, str], value: float) -> None:
        """Buffer a metric for batched push."""
        self._buffer.append((name, labels, value))
        if len(self._buffer) >= self._FLUSH_SIZE:
            self.flush()

    def record_step_duration(self, step_name: str, duration_s: float, generation: int = 0) -> None:
        self._add_metric(
            "osimflow_step_duration_seconds",
            {"step": step_name, "generation": str(generation)},
            duration_s,
        )

    def record_sample_metric(self, sample_id: str, metric_name: str, value: float) -> None:
        self._add_metric(
            f"osimflow_{metric_name}",
            {"sample_id": sample_id},
            value,
        )

    def record_campaign_duration(self, duration_s: float) -> None:
        self._add_metric("osimflow_campaign_duration_seconds", {}, duration_s)

    def flush(self) -> None:
        if not self._buffer:
            return
        registry = self._ensure_registry()
        # Materialise all buffered values into gauges on the registry.
        for name, labels, value in self._buffer:
            gauge = self._get_or_create_gauge(name, labels)
            gauge.labels(**labels).set(value)
        # Push to gateway.
        from prometheus_client import push_to_gateway  # noqa: PLC0415

        push_to_gateway(self._url, job=self._job, registry=registry)
        log.debug("Pushed %d metrics to Prometheus pushgateway", len(self._buffer))
        self._buffer = []


class OpenTelemetryBackend(ObservabilityBackend):
    """OpenTelemetry OTLP backend.  Lazy-imports ``opentelemetry-sdk``.

    Metrics are batched and exported via the OTLP gRPC exporter on
    :meth:`flush`.  Requires ``pip install opentelemetry-api opentelemetry-sdk
    opentelemetry-exporter-otlp-proto-grpc``.

    Parameters
    ----------
    endpoint
        OTLP gRPC endpoint (default: ``"http://localhost:4317"``).
    service_name
        OTel service name attribute (default: ``"osimflow"``).
    """

    _FLUSH_SIZE = 20

    def __init__(
        self,
        endpoint: str = "http://localhost:4317",
        service_name: str = "osimflow",
    ) -> None:
        self._endpoint = endpoint
        self._service = service_name
        self._buffer: list[tuple[str, dict[str, str], float]] = []
        # Lazy-initialised OTel artefacts
        self._meter: Any = None
        self._instruments: dict[str, Any] = {}

    def _ensure_meter(self) -> Any:
        """Lazy-import ``opentelemetry-sdk`` and return a ``Meter``."""
        if self._meter is None:
            try:
                from opentelemetry import metrics as otel_metrics  # noqa: PLC0415
                from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import (  # noqa: PLC0415
                    OTLPMetricExporter,
                )
                from opentelemetry.sdk.metrics import MeterProvider  # noqa: PLC0415
                from opentelemetry.sdk.metrics.export import (  # noqa: PLC0415
                    PeriodicExportingMetricReader,
                )
                from opentelemetry.sdk.resources import Resource  # noqa: PLC0415
            except ImportError:
                raise ImportError(
                    "OpenTelemetry backend requires opentelemetry packages. "
                    "Install with: pip install opentelemetry-api "
                    "opentelemetry-sdk opentelemetry-exporter-otlp-proto-grpc"
                ) from None

            exporter = OTLPMetricExporter(endpoint=self._endpoint, insecure=True)
            reader = PeriodicExportingMetricReader(exporter, export_interval_millis=60_000)
            resource = Resource.create({"service.name": self._service})
            provider = MeterProvider(metric_readers=[reader], resource=resource)
            otel_metrics.set_meter_provider(provider)
            self._meter = provider.get_meter("osimflow")
            self._instruments = {}
        return self._meter

    def _get_or_create_gauge(self, name: str) -> Any:
        """Return a cached OTel ``UpDownCounter`` for *name*."""
        if name not in self._instruments:
            self._instruments[name] = self._meter.create_gauge(
                name,
                description=f"OSimFlow metric {name}",
                unit="1",
            )
        return self._instruments[name]

    def _add_metric(self, name: str, labels: dict[str, str], value: float) -> None:
        """Buffer a metric for batched export."""
        self._buffer.append((name, labels, value))
        if len(self._buffer) >= self._FLUSH_SIZE:
            self.flush()

    def record_step_duration(self, step_name: str, duration_s: float, generation: int = 0) -> None:
        self._add_metric(
            "osimflow.step.duration",
            {"step": step_name, "generation": str(generation)},
            duration_s,
        )

    def record_sample_metric(self, sample_id: str, metric_name: str, value: float) -> None:
        self._add_metric(
            f"osimflow.sample.{metric_name}",
            {"sample_id": sample_id},
            value,
        )

    def record_campaign_duration(self, duration_s: float) -> None:
        self._add_metric("osimflow.campaign.duration", {}, duration_s)

    def flush(self) -> None:
        if not self._buffer:
            return
        self._ensure_meter()
        for name, labels, value in self._buffer:
            gauge = self._get_or_create_gauge(name)
            gauge.set(value, attributes=labels)
        log.debug("Recorded %d OTel metrics (provider will export)", len(self._buffer))
        self._buffer = []
