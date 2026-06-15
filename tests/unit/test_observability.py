"""Unit tests for per-sample trace IDs in osimflow.observability (issue #436).

Acceptance criteria (issue #436):

  * ``new_trace_id()`` mints short, unique, UUID-derived IDs.
  * Every backend accepts an optional ``trace_id`` keyword and, when
    supplied, stamps it as a dimension / label / attribute.
  * Omitting ``trace_id`` is backward compatible — no dimension/label is
    emitted and behaviour matches the pre-issue contract.
  * ``ObservabilityBackend.record_sample_event`` is a concrete helper
    that delegates to ``record_sample_metric`` and forwards the trace id.
  * The ABC contract is unchanged — subclasses implementing only the
    abstract methods (without ``trace_id``) still instantiate.
  * ``SampleTrace`` carries the ``trace_id`` and serializes it when set.
"""

from __future__ import annotations

import uuid
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest

from osimflow.monitoring import SampleTrace
from osimflow.observability import (
    CloudWatchBackend,
    NullBackend,
    ObservabilityBackend,
    OpenTelemetryBackend,
    PrometheusBackend,
    new_trace_id,
)


# ---------------------------------------------------------------------------
# new_trace_id()
# ---------------------------------------------------------------------------
class TestNewTraceId:
    """``new_trace_id()`` produces short, unique, UUID-derived IDs."""

    def test_returns_short_hex_string(self) -> None:
        tid = new_trace_id()
        assert isinstance(tid, str)
        assert len(tid) == 8
        # All hex characters.
        int(tid, 16)  # raises ValueError if not hex

    def test_uniqueness_across_many_calls(self) -> None:
        ids = {new_trace_id() for _ in range(10_000)}
        # 8 hex chars → ~4 billion space; 10k draws should be unique.
        assert len(ids) == 10_000

    def test_derived_from_uuid4(self) -> None:
        """The returned ID is the 8-char prefix of a uuid4 hex."""
        fixed = uuid.UUID("12345678-9abc-def0-1234-56789abcdef0")
        with patch("osimflow.observability.uuid.uuid4", return_value=fixed):
            assert new_trace_id() == "12345678"


# ---------------------------------------------------------------------------
# NullBackend — trace_id is accepted and silently dropped
# ---------------------------------------------------------------------------
class TestNullBackendTraceId:
    """NullBackend must accept trace_id without error (no-op)."""

    def test_record_sample_metric_with_trace_id(self) -> None:
        NullBackend().record_sample_metric("s001", "eui", 1.0, trace_id="deadbeef")

    def test_record_step_duration_with_trace_id(self) -> None:
        NullBackend().record_step_duration("RUN_OPENSTUDIO_SIM", 5.0, trace_id="cafef00d")

    def test_record_campaign_duration_with_trace_id(self) -> None:
        NullBackend().record_campaign_duration(99.0, trace_id="trace-x")

    def test_record_sample_event_with_trace_id(self) -> None:
        NullBackend().record_sample_event("s001", "sim_done", trace_id="trace-y")

    def test_calls_without_trace_id_still_work(self) -> None:
        b = NullBackend()
        b.record_sample_metric("s001", "eui", 1.0)
        b.record_step_duration("RUN_OPENSTUDIO_SIM", 5.0)
        b.record_campaign_duration(99.0)
        b.record_sample_event("s001", "sim_done")


# ---------------------------------------------------------------------------
# record_sample_event() — concrete helper on the ABC
# ---------------------------------------------------------------------------
class TestRecordSampleEvent:
    """``record_sample_event`` delegates to ``record_sample_metric``."""

    def test_delegates_to_record_sample_metric(self) -> None:
        calls: list[tuple] = []

        class _Spy(ObservabilityBackend):
            def record_step_duration(
                self, step_name, duration_s, generation=0, *, trace_id=None
            ) -> None:
                pass

            def record_sample_metric(self, sample_id, metric_name, value, *, trace_id=None) -> None:
                calls.append((sample_id, metric_name, value, trace_id))

            def record_campaign_duration(self, duration_s, *, trace_id=None) -> None:
                pass

            def flush(self) -> None:
                pass

        spy = _Spy()
        spy.record_sample_event("s042", "apply_started", trace_id="abc12345")
        assert calls == [("s042", "apply_started", 1.0, "abc12345")]

    def test_default_value_is_one(self) -> None:
        seen: dict[str, float] = {}

        class _Spy(ObservabilityBackend):
            def record_step_duration(
                self, step_name, duration_s, generation=0, *, trace_id=None
            ) -> None:
                pass

            def record_sample_metric(self, sample_id, metric_name, value, *, trace_id=None) -> None:
                seen[metric_name] = value

            def record_campaign_duration(self, duration_s, *, trace_id=None) -> None:
                pass

            def flush(self) -> None:
                pass

        _Spy().record_sample_event("s001", "sim_done")
        assert seen["sim_done"] == 1.0

    def test_not_abstract_subclass_without_override(self) -> None:
        """A subclass that implements only the abstract methods can be
        instantiated without overriding record_sample_event()."""
        # NullBackend is exactly such a subclass.
        assert isinstance(NullBackend(), ObservabilityBackend)
        # And record_sample_event works via inheritance.
        NullBackend().record_sample_event("s001", "ok", trace_id="t1")


# ---------------------------------------------------------------------------
# ABC backward compatibility
# ---------------------------------------------------------------------------
class TestABCBackwardCompat:
    """The ABC still works for subclasses that pre-date the trace_id kwarg."""

    def test_subclass_omitting_trace_id_instantiates(self) -> None:
        """A subclass that implements the abstract methods with the
        pre-issue signatures still instantiates and is callable."""

        class _Legacy(ObservabilityBackend):
            def record_step_duration(self, step_name, duration_s, generation=0) -> None:
                pass

            def record_sample_metric(self, sample_id, metric_name, value) -> None:
                pass

            def record_campaign_duration(self, duration_s) -> None:
                pass

            def flush(self) -> None:
                pass

        instance = _Legacy()
        # Calling with a trace_id must not raise even though the legacy
        # implementation does not declare it (Python accepts unknown
        # keyword arguments only when the method does NOT use **kwargs;
        # here we expect TypeError because the legacy signature rejects
        # the keyword). This documents the contract: legacy subclasses
        # are NOT required to accept trace_id, and callers that pass it
        # to a legacy subclass get a TypeError — which is the existing
        # Python behaviour for unexpected kwargs.
        with pytest.raises(TypeError):
            instance.record_sample_metric("s001", "eui", 1.0, trace_id="t")
        # But calls without trace_id work fine (backward compatible).
        instance.record_sample_metric("s001", "eui", 1.0)
        instance.record_step_duration("APPLY_PARAMETERS", 1.0)
        instance.record_campaign_duration(99.0)
        instance.flush()


# ---------------------------------------------------------------------------
# CloudWatchBackend — TraceId dimension
# ---------------------------------------------------------------------------
class TestCloudWatchTraceId:
    """CloudWatchBackend attaches a ``TraceId`` dimension when trace_id is set."""

    def _make(self) -> tuple[CloudWatchBackend, MagicMock]:
        backend = CloudWatchBackend(namespace="Test/Trace")
        fake_cw = MagicMock()
        backend._client = fake_cw
        return backend, fake_cw

    def test_sample_metric_includes_trace_id_dimension(self) -> None:
        backend, fake_cw = self._make()
        backend.record_sample_metric("s042", "eui", 120.0, trace_id="abcd1234")
        backend.flush()
        metric = fake_cw.put_metric_data.call_args.kwargs["MetricData"][0]
        dims = {d["Name"]: d["Value"] for d in metric["Dimensions"]}
        assert dims["SampleId"] == "s042"
        assert dims["TraceId"] == "abcd1234"

    def test_step_duration_includes_trace_id_dimension(self) -> None:
        backend, fake_cw = self._make()
        backend.record_step_duration("RUN_OPENSTUDIO_SIM", 42.0, generation=1, trace_id="cafef00d")
        backend.flush()
        metric = fake_cw.put_metric_data.call_args.kwargs["MetricData"][0]
        dims = {d["Name"]: d["Value"] for d in metric["Dimensions"]}
        assert dims["TraceId"] == "cafef00d"
        assert dims["StepName"] == "RUN_OPENSTUDIO_SIM"
        assert dims["Generation"] == "1"

    def test_campaign_duration_includes_trace_id_dimension(self) -> None:
        backend, fake_cw = self._make()
        backend.record_campaign_duration(1234.0, trace_id="trace-c")
        backend.flush()
        metric = fake_cw.put_metric_data.call_args.kwargs["MetricData"][0]
        dims = {d["Name"]: d["Value"] for d in metric["Dimensions"]}
        assert dims["TraceId"] == "trace-c"

    def test_omitting_trace_id_omits_dimension(self) -> None:
        """Backward compat: no trace_id → no TraceId dimension."""
        backend, fake_cw = self._make()
        backend.record_sample_metric("s001", "eui", 1.0)
        backend.record_step_duration("APPLY_PARAMETERS", 1.0)
        backend.record_campaign_duration(1.0)
        backend.flush()
        for metric in fake_cw.put_metric_data.call_args.kwargs["MetricData"]:
            dim_names = {d["Name"] for d in metric.get("Dimensions", [])}
            assert "TraceId" not in dim_names


# ---------------------------------------------------------------------------
# PrometheusBackend — trace_id label
# ---------------------------------------------------------------------------
def _fake_prometheus_client() -> ModuleType:
    fake = ModuleType("prometheus_client")
    fake.CollectorRegistry = MagicMock(name="CollectorRegistry")
    fake.Gauge = MagicMock(name="Gauge")
    fake.push_to_gateway = MagicMock(name="push_to_gateway")
    return fake


class TestPrometheusTraceId:
    """PrometheusBackend attaches a ``trace_id`` label when set."""

    def test_sample_metric_includes_trace_id_label(self) -> None:
        backend = PrometheusBackend()
        backend.record_sample_metric("s042", "eui", 120.0, trace_id="abcd1234")
        name, labels, value = backend._buffer[0]
        assert labels["sample_id"] == "s042"
        assert labels["trace_id"] == "abcd1234"
        assert value == 120.0

    def test_step_duration_includes_trace_id_label(self) -> None:
        backend = PrometheusBackend()
        backend.record_step_duration("RUN_OPENSTUDIO_SIM", 42.0, generation=1, trace_id="cafef00d")
        _, labels, _ = backend._buffer[0]
        assert labels["trace_id"] == "cafef00d"
        assert labels["step"] == "RUN_OPENSTUDIO_SIM"
        assert labels["generation"] == "1"

    def test_campaign_duration_includes_trace_id_label(self) -> None:
        backend = PrometheusBackend()
        backend.record_campaign_duration(1234.0, trace_id="trace-c")
        _, labels, _ = backend._buffer[0]
        assert labels == {"trace_id": "trace-c"}

    def test_omitting_trace_id_omits_label(self) -> None:
        backend = PrometheusBackend()
        backend.record_sample_metric("s001", "eui", 1.0)
        backend.record_step_duration("APPLY_PARAMETERS", 1.0)
        backend.record_campaign_duration(1.0)
        for _, labels, _ in backend._buffer:
            assert "trace_id" not in labels

    def test_flush_forwards_trace_id_to_gauge_attributes(self) -> None:
        backend = PrometheusBackend()
        fake_pc = _fake_prometheus_client()
        mock_gauge_instance = MagicMock(name="gauge_instance")
        mock_labeled = MagicMock(name="labeled_gauge")
        mock_gauge_instance.labels.return_value = mock_labeled
        fake_pc.Gauge.return_value = mock_gauge_instance

        with patch.dict("sys.modules", {"prometheus_client": fake_pc}):
            backend.record_sample_metric("s001", "eui", 100.0, trace_id="deadbeef")
            backend.flush()

        # Gauge.labels was called with the trace_id among the labels.
        mock_gauge_instance.labels.assert_called_with(sample_id="s001", trace_id="deadbeef")
        mock_labeled.set.assert_called_once_with(100.0)


# ---------------------------------------------------------------------------
# OpenTelemetryBackend — trace_id attribute
# ---------------------------------------------------------------------------
def _inject_otel_fakes() -> tuple[dict, MagicMock, MagicMock]:
    """Patch sys.modules with fake OTel modules and return key mocks."""
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

    fake_meter = MagicMock(name="meter")
    fake_gauge = MagicMock(name="gauge_instrument")
    fake_meter.create_gauge.return_value = fake_gauge
    provider_mod.MeterProvider.return_value.get_meter.return_value = fake_meter

    fakes = {
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
    return fakes, fake_meter, fake_gauge


class TestOpenTelemetryTraceId:
    """OpenTelemetryBackend attaches trace_id as a metric attribute."""

    def test_sample_metric_includes_trace_id_attribute(self) -> None:
        backend = OpenTelemetryBackend()
        backend.record_sample_metric("s042", "eui", 120.0, trace_id="abcd1234")
        name, labels, value = backend._buffer[0]
        assert labels["sample_id"] == "s042"
        assert labels["trace_id"] == "abcd1234"
        assert value == 120.0

    def test_step_duration_includes_trace_id_attribute(self) -> None:
        backend = OpenTelemetryBackend()
        backend.record_step_duration("RUN_OPENSTUDIO_SIM", 42.0, generation=1, trace_id="cafef00d")
        _, labels, _ = backend._buffer[0]
        assert labels["trace_id"] == "cafef00d"
        assert labels["step"] == "RUN_OPENSTUDIO_SIM"
        assert labels["generation"] == "1"

    def test_campaign_duration_includes_trace_id_attribute(self) -> None:
        backend = OpenTelemetryBackend()
        backend.record_campaign_duration(1234.0, trace_id="trace-c")
        _, labels, _ = backend._buffer[0]
        assert labels == {"trace_id": "trace-c"}

    def test_omitting_trace_id_omits_attribute(self) -> None:
        backend = OpenTelemetryBackend()
        backend.record_sample_metric("s001", "eui", 1.0)
        backend.record_step_duration("APPLY_PARAMETERS", 1.0)
        backend.record_campaign_duration(1.0)
        for _, labels, _ in backend._buffer:
            assert "trace_id" not in labels

    def test_flush_sets_gauge_attributes_with_trace_id(self) -> None:
        """OTel maps trace_id to span/metric attributes (issue #436)."""
        backend = OpenTelemetryBackend()
        fakes, _, fake_gauge = _inject_otel_fakes()

        with patch.dict("sys.modules", fakes):
            backend.record_sample_metric("s001", "eui", 100.0, trace_id="deadbeef")
            backend.flush()

        fake_gauge.set.assert_called_once_with(
            100.0, attributes={"sample_id": "s001", "trace_id": "deadbeef"}
        )


# ---------------------------------------------------------------------------
# SampleTrace — trace_id field
# ---------------------------------------------------------------------------
class TestSampleTraceTraceId:
    """``SampleTrace`` carries and serializes the trace_id."""

    def test_default_is_none(self) -> None:
        t = SampleTrace(sample_id="s001", status="ok", elapsed_s=1.0)
        assert t.trace_id is None

    def test_set_and_serialize(self) -> None:
        t = SampleTrace(sample_id="s001", status="ok", elapsed_s=1.0, trace_id="abcd1234")
        d = t.to_dict()
        assert d["trace_id"] == "abcd1234"

    def test_none_is_omitted_from_dict(self) -> None:
        """to_dict() drops None fields, so absent trace_id stays absent."""
        t = SampleTrace(sample_id="s001", status="ok", elapsed_s=1.0)
        assert "trace_id" not in t.to_dict()
