"""Unit tests for osimflow.observability — OpenTelemetryBackend.

Acceptance criteria (issue #127):

  * OpenTelemetryBackend buffers metrics and auto-flushes at threshold.
  * OpenTelemetryBackend lazy-imports opentelemetry-sdk (no hard import at module load).
  * OpenTelemetryBackend raises ImportError when opentelemetry packages are not installed.
  * All methods produce correctly-named instruments with appropriate labels.
  * flush() materialises gauges on the OTel meter.
  * All tests use unittest.mock — no real opentelemetry-sdk needed.
"""

from __future__ import annotations

import sys
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest

from osimflow.observability import OpenTelemetryBackend

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fake_otel_api() -> ModuleType:
    """Build a fake ``opentelemetry.metrics`` module."""
    mod = ModuleType("opentelemetry.metrics")
    mod.set_meter_provider = MagicMock(name="set_meter_provider")
    return mod


def _fake_otel_sdk() -> dict[str, ModuleType]:
    """Build a dict of fake OTel SDK modules covering the import surface."""
    exporter_mod = ModuleType("opentelemetry.exporter.otlp.proto.grpc.metric_exporter")
    exporter_mod.OTLPMetricExporter = MagicMock(name="OTLPMetricExporter")

    reader_mod = ModuleType("opentelemetry.sdk.metrics.export")
    reader_mod.PeriodicExportingMetricReader = MagicMock(name="PeriodicExportingMetricReader")

    provider_mod = ModuleType("opentelemetry.sdk.metrics")
    provider_mod.MeterProvider = MagicMock(name="MeterProvider")

    resource_mod = ModuleType("opentelemetry.sdk.resources")
    resource_mod.Resource = MagicMock(name="Resource")
    resource_mod.Resource.create = MagicMock(name="Resource.create")

    metrics_api = ModuleType("opentelemetry.metrics")
    metrics_api.set_meter_provider = MagicMock(name="set_meter_provider")

    return {
        "opentelemetry": ModuleType("opentelemetry"),
        "opentelemetry.metrics": metrics_api,
        "opentelemetry.exporter": ModuleType("opentelemetry.exporter"),
        "opentelemetry.exporter.otlp": ModuleType("opentelemetry.exporter.otlp"),
        "opentelemetry.exporter.otlp.proto": ModuleType("opentelemetry.exporter.otlp.proto"),
        "opentelemetry.exporter.otlp.proto.grpc": ModuleType(
            "opentelemetry.exporter.otlp.proto.grpc"
        ),
        "opentelemetry.exporter.otlp.proto.grpc.metric_exporter": exporter_mod,
        "opentelemetry.sdk": ModuleType("opentelemetry.sdk"),
        "opentelemetry.sdk.metrics": provider_mod,
        "opentelemetry.sdk.metrics.export": reader_mod,
        "opentelemetry.sdk.resources": resource_mod,
    }


def _inject_otel_fakes() -> dict[str, MagicMock]:
    """Patch sys.modules with fake OTel modules and return key mocks."""
    fakes = _fake_otel_sdk()
    # Wire up MeterProvider.get_meter to return a fake meter.
    fake_meter = MagicMock(name="meter")
    fake_gauge = MagicMock(name="gauge_instrument")
    fake_meter.create_gauge.return_value = fake_gauge
    fakes["opentelemetry.sdk.metrics"].MeterProvider.return_value.get_meter.return_value = (
        fake_meter
    )
    return fakes, fake_meter, fake_gauge


# ---------------------------------------------------------------------------
# Buffering
# ---------------------------------------------------------------------------
class TestOTelBuffering:
    """Metrics are buffered and only sent on flush or at threshold."""

    def test_buffer_accumulates_without_flushing(self) -> None:
        backend = OpenTelemetryBackend()
        for i in range(5):
            backend.record_sample_metric(f"s{i:03d}", "eui", float(i))
        assert len(backend._buffer) == 5

    def test_auto_flush_at_threshold(self) -> None:
        backend = OpenTelemetryBackend()
        fakes, _, _ = _inject_otel_fakes()
        with patch.dict("sys.modules", fakes):
            backend._meter = None
            backend._instruments = {}
            for i in range(OpenTelemetryBackend._FLUSH_SIZE):
                backend._add_metric("test_metric", {"idx": str(i)}, float(i))
            assert len(backend._buffer) == 0

    def test_explicit_flush_sends_buffered(self) -> None:
        backend = OpenTelemetryBackend()
        fakes, fake_meter, _ = _inject_otel_fakes()

        with patch.dict("sys.modules", fakes):
            backend.record_step_duration("APPLY_PARAMETERS", 3.14)
            backend.record_step_duration("RUN_OPENSTUDIO_SIM", 42.0)
            assert len(backend._buffer) == 2
            backend.flush()
            assert len(backend._buffer) == 0

    def test_flush_on_empty_buffer_is_noop(self) -> None:
        backend = OpenTelemetryBackend()
        fakes, fake_meter, _ = _inject_otel_fakes()

        with patch.dict("sys.modules", fakes):
            backend.flush()
            # Meter should not be created if nothing to flush.
            assert backend._meter is None


# ---------------------------------------------------------------------------
# Metric content
# ---------------------------------------------------------------------------
class TestOTelMetricContent:
    """Verify the correct instrument names and labels are produced."""

    def test_step_duration_labels(self) -> None:
        backend = OpenTelemetryBackend()
        backend.record_step_duration("APPLY_PARAMETERS", 1.5, generation=2)
        name, labels, value = backend._buffer[0]
        assert name == "osimflow.step.duration"
        assert labels == {"step": "APPLY_PARAMETERS", "generation": "2"}
        assert value == 1.5

    def test_sample_metric_labels(self) -> None:
        backend = OpenTelemetryBackend()
        backend.record_sample_metric("s042", "simulation_time", 300.0)
        name, labels, value = backend._buffer[0]
        assert name == "osimflow.sample.simulation_time"
        assert labels == {"sample_id": "s042"}
        assert value == 300.0

    def test_campaign_duration_no_labels(self) -> None:
        backend = OpenTelemetryBackend()
        backend.record_campaign_duration(1234.0)
        name, labels, value = backend._buffer[0]
        assert name == "osimflow.campaign.duration"
        assert labels == {}
        assert value == 1234.0

    def test_flush_records_gauge_values(self) -> None:
        backend = OpenTelemetryBackend()
        fakes, _, fake_gauge = _inject_otel_fakes()

        with patch.dict("sys.modules", fakes):
            backend.record_sample_metric("s001", "eui", 100.0)
            backend.flush()

        # The gauge's .set() was called with the value and attributes.
        fake_gauge.set.assert_called_once_with(100.0, attributes={"sample_id": "s001"})


# ---------------------------------------------------------------------------
# Lazy import
# ---------------------------------------------------------------------------
class TestOTelLazyImport:
    """opentelemetry-sdk must not be imported at module load."""

    def test_no_import_at_construction(self) -> None:
        """Constructing an OpenTelemetryBackend does not import OTel."""
        backend = OpenTelemetryBackend()
        assert backend._meter is None

    def test_flush_triggers_import(self) -> None:
        """Flushing triggers the lazy import and creates a meter."""
        backend = OpenTelemetryBackend()
        fakes, fake_meter, _ = _inject_otel_fakes()

        with patch.dict("sys.modules", fakes):
            backend.record_campaign_duration(1.0)
            backend.flush()

        assert backend._meter is fake_meter

    def test_import_error_without_otel(self) -> None:
        """If opentelemetry-sdk cannot be imported, flush raises ImportError."""
        backend = OpenTelemetryBackend()
        backend.record_campaign_duration(1.0)

        # Block all opentelemetry imports.
        blocked: dict[str, None] = {}
        for key in list(sys.modules):
            if key.startswith("opentelemetry"):
                blocked[key] = None
        with patch.dict("sys.modules", blocked, clear=False):
            with pytest.raises(ImportError, match="pip install"):
                backend.flush()

    def test_meter_reused_across_flushes(self) -> None:
        """The meter is created once and reused for subsequent flushes."""
        backend = OpenTelemetryBackend()
        fakes, fake_meter, _ = _inject_otel_fakes()

        with patch.dict("sys.modules", fakes):
            backend.record_campaign_duration(1.0)
            backend.flush()
            first_meter = backend._meter
            backend.record_campaign_duration(2.0)
            backend.flush()

        assert backend._meter is first_meter
        # MeterProvider constructor called only once.
        fakes["opentelemetry.sdk.metrics"].MeterProvider.assert_called_once()

    def test_endpoint_forwarded_to_exporter(self) -> None:
        """The endpoint is passed to the OTLPMetricExporter."""
        backend = OpenTelemetryBackend(
            endpoint="http://collector:4317", service_name="test_svc"
        )
        fakes, _, _ = _inject_otel_fakes()

        with patch.dict("sys.modules", fakes):
            backend.record_campaign_duration(1.0)
            backend.flush()

        fakes["opentelemetry.exporter.otlp.proto.grpc.metric_exporter"].OTLPMetricExporter.assert_called_once_with(
            endpoint="http://collector:4317", insecure=True,
        )
