"""Observability backends for OSimFlow campaigns.

Provides an abstract base class (:class:`ObservabilityBackend`) that
defines the metrics interface the Campaign calls at key lifecycle points.
Two built-in implementations:

* :class:`NullBackend` — no-op default when no observability is configured.
* :class:`CloudWatchBackend` — AWS CloudWatch metrics with batched uploads.

Third-party backends (Prometheus, Datadog, etc.) can be added by
subclassing :class:`ObservabilityBackend` and passing the instance to
the Campaign constructor.

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
