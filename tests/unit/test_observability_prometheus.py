"""Unit tests for osimflow.observability — PrometheusBackend.

Acceptance criteria (issue #127):

  * PrometheusBackend buffers metrics and auto-flushes at threshold.
  * PrometheusBackend lazy-imports prometheus_client (no hard import at module load).
  * PrometheusBackend raises ImportError when prometheus_client is not installed.
  * All methods produce correctly-named gauges with appropriate labels.
  * flush() pushes to the pushgateway via push_to_gateway().
  * All tests use unittest.mock — no real prometheus_client needed.
"""

from __future__ import annotations

from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest

from osimflow.observability import PrometheusBackend

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fake_prometheus_client() -> ModuleType:
    """Build a fake ``prometheus_client`` module with the three symbols we use."""
    fake = ModuleType("prometheus_client")
    fake.CollectorRegistry = MagicMock(name="CollectorRegistry")
    fake.Gauge = MagicMock(name="Gauge")
    fake.push_to_gateway = MagicMock(name="push_to_gateway")
    return fake


def _make_backend() -> tuple[PrometheusBackend, dict[str, MagicMock]]:
    """Create a PrometheusBackend with a mocked prometheus_client.

    Returns (backend, fakes) where fakes has keys 'registry', 'gauge', 'push'.
    """
    backend = PrometheusBackend(pushgateway_url="localhost:9091", job_name="test_job")
    # Pre-seed the lazy internals so we don't trigger a real import.
    fake_registry = MagicMock(name="CollectorRegistry")
    fake_gauge_cls = MagicMock(name="Gauge")
    fake_push = MagicMock(name="push_to_gateway")

    # _get_or_create_gauge will be called with Gauge() returning a mock.
    backend._registry = fake_registry
    backend._gauges = {}

    fakes = {
        "registry": fake_registry,
        "Gauge": fake_gauge_cls,
        "push_to_gateway": fake_push,
    }
    return backend, fakes


# ---------------------------------------------------------------------------
# Buffering
# ---------------------------------------------------------------------------
class TestPrometheusBuffering:
    """Metrics are buffered and only pushed on flush or at threshold."""

    def test_buffer_accumulates_without_flushing(self) -> None:
        backend, _ = _make_backend()
        for i in range(5):
            backend.record_sample_metric(f"s{i:03d}", "eui", float(i))
        assert len(backend._buffer) == 5

    def test_auto_flush_at_threshold(self) -> None:
        backend, _ = _make_backend()
        # Patch push_to_gateway inside the flush path.
        with patch.dict("sys.modules", {"prometheus_client": _fake_prometheus_client()}):
            backend._registry = None  # force lazy init
            backend._gauges = {}
            for i in range(PrometheusBackend._FLUSH_SIZE):
                backend._add_metric("test_metric", {"idx": str(i)}, float(i))
            # After 20 adds, flush was triggered — buffer should be empty.
            assert len(backend._buffer) == 0

    def test_explicit_flush_sends_buffered(self) -> None:
        backend = PrometheusBackend()
        fake_pc = _fake_prometheus_client()

        with patch.dict("sys.modules", {"prometheus_client": fake_pc}):
            backend.record_step_duration("APPLY_PARAMETERS", 3.14)
            backend.record_step_duration("RUN_OPENSTUDIO_SIM", 42.0)
            assert len(backend._buffer) == 2
            backend.flush()
            assert len(backend._buffer) == 0

    def test_flush_on_empty_buffer_is_noop(self) -> None:
        backend = PrometheusBackend()
        fake_pc = _fake_prometheus_client()

        with patch.dict("sys.modules", {"prometheus_client": fake_pc}):
            backend.flush()
            fake_pc.push_to_gateway.assert_not_called()


# ---------------------------------------------------------------------------
# Metric content
# ---------------------------------------------------------------------------
class TestPrometheusMetricContent:
    """Verify that the correct gauge names and labels are produced."""

    def test_step_duration_labels(self) -> None:
        backend = PrometheusBackend()
        backend.record_step_duration("APPLY_PARAMETERS", 1.5, generation=2)
        name, labels, value = backend._buffer[0]
        assert name == "osimflow_step_duration_seconds"
        assert labels == {"step": "APPLY_PARAMETERS", "generation": "2"}
        assert value == 1.5

    def test_sample_metric_labels(self) -> None:
        backend = PrometheusBackend()
        backend.record_sample_metric("s042", "eui", 120.0)
        name, labels, value = backend._buffer[0]
        assert name == "osimflow_eui"
        assert labels == {"sample_id": "s042"}
        assert value == 120.0

    def test_campaign_duration_no_labels(self) -> None:
        backend = PrometheusBackend()
        backend.record_campaign_duration(1234.0)
        name, labels, value = backend._buffer[0]
        assert name == "osimflow_campaign_duration_seconds"
        assert labels == {}
        assert value == 1234.0

    def test_flush_materialises_gauges(self) -> None:
        backend = PrometheusBackend()
        fake_pc = _fake_prometheus_client()
        # Gauge() should return a mock whose .labels().set() we can inspect.
        mock_gauge_instance = MagicMock(name="gauge_instance")
        mock_labeled = MagicMock(name="labeled_gauge")
        mock_gauge_instance.labels.return_value = mock_labeled
        fake_pc.Gauge.return_value = mock_gauge_instance

        with patch.dict("sys.modules", {"prometheus_client": fake_pc}):
            backend.record_sample_metric("s001", "eui", 100.0)
            backend.flush()

        # push_to_gateway should have been called once.
        fake_pc.push_to_gateway.assert_called_once_with(
            "localhost:9091", job="osimflow", registry=backend._registry
        )
        # The gauge value was set.
        mock_labeled.set.assert_called_once_with(100.0)


# ---------------------------------------------------------------------------
# Lazy import
# ---------------------------------------------------------------------------
class TestPrometheusLazyImport:
    """prometheus_client must not be imported at module load."""

    def test_no_import_at_construction(self) -> None:
        """Constructing a PrometheusBackend does not import prometheus_client."""
        with patch.dict("sys.modules", {"prometheus_client": None}):
            backend = PrometheusBackend()
            assert backend._registry is None

    def test_flush_triggers_import(self) -> None:
        """Flushing triggers the lazy import and creates a registry."""
        fake_pc = _fake_prometheus_client()

        backend = PrometheusBackend()
        with patch.dict("sys.modules", {"prometheus_client": fake_pc}):
            backend.record_campaign_duration(1.0)
            backend.flush()

        fake_pc.CollectorRegistry.assert_called_once()
        fake_pc.push_to_gateway.assert_called_once()

    def test_import_error_without_prometheus_client(self) -> None:
        """If prometheus_client cannot be imported, flush raises ImportError."""
        backend = PrometheusBackend()
        backend.record_campaign_duration(1.0)
        # Ensure the import fails.
        with patch.dict("sys.modules", {"prometheus_client": None}):
            with pytest.raises(ImportError, match="pip install prometheus_client"):
                backend.flush()

    def test_registry_reused_across_flushes(self) -> None:
        """The CollectorRegistry is created once and reused."""
        fake_pc = _fake_prometheus_client()
        backend = PrometheusBackend()

        with patch.dict("sys.modules", {"prometheus_client": fake_pc}):
            backend.record_campaign_duration(1.0)
            backend.flush()
            first_registry = backend._registry
            backend.record_campaign_duration(2.0)
            backend.flush()

        assert backend._registry is first_registry
        assert fake_pc.CollectorRegistry.call_count == 1
        assert fake_pc.push_to_gateway.call_count == 2
