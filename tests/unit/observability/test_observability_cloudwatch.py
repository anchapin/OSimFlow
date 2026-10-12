"""Unit tests for osimflow.observability — ABC, NullBackend, CloudWatchBackend.

Acceptance criteria (issue #145):

  * NullBackend does nothing (no errors on any method).
  * CloudWatchBackend buffers metrics and auto-flushes at 20.
  * CloudWatchBackend lazy-imports boto3 (no hard import at module load).
  * CloudWatchBackend raises ImportError without boto3 installed.
  * ABC enforces the interface — subclass missing a method raises TypeError.
  * All tests use ``unittest.mock`` for the boto3 client.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from osimflow.observability import (
    CloudWatchBackend,
    NullBackend,
    ObservabilityBackend,
)


# ---------------------------------------------------------------------------
# NullBackend
# ---------------------------------------------------------------------------
class TestNullBackend:
    """NullBackend is the safe default — every method is a silent no-op."""

    def test_record_step_duration_no_error(self) -> None:
        NullBackend().record_step_duration("APPLY_PARAMETERS", 1.5)

    def test_record_sample_metric_no_error(self) -> None:
        NullBackend().record_sample_metric("s001", "eui", 120.0)

    def test_record_campaign_duration_no_error(self) -> None:
        NullBackend().record_campaign_duration(42.0)

    def test_flush_no_error(self) -> None:
        NullBackend().flush()


# ---------------------------------------------------------------------------
# ABC enforcement
# ---------------------------------------------------------------------------
class TestABCEnforcement:
    """ObservabilityBackend cannot be instantiated with missing methods."""

    def test_subclass_missing_method_raises_type_error(self) -> None:
        """A subclass that omits any abstract method raises TypeError on
        instantiation."""

        class _Incomplete(ObservabilityBackend):
            def record_step_duration(
                self, step_name: str, duration_s: float, generation: int = 0
            ) -> None:
                pass

            # Intentionally missing: record_sample_metric, record_campaign_duration, flush

        with pytest.raises(TypeError, match="abstract"):
            _Incomplete()  # type: ignore[abstract]

    def test_complete_subclass_instantiates(self) -> None:
        """A fully-implemented subclass can be instantiated."""

        class _Complete(ObservabilityBackend):
            def record_step_duration(
                self, step_name: str, duration_s: float, generation: int = 0
            ) -> None:
                pass

            def record_sample_metric(self, sample_id: str, metric_name: str, value: float) -> None:
                pass

            def record_campaign_duration(self, duration_s: float) -> None:
                pass

            def flush(self) -> None:
                pass

        instance = _Complete()
        assert isinstance(instance, ObservabilityBackend)


# ---------------------------------------------------------------------------
# CloudWatchBackend — buffering
# ---------------------------------------------------------------------------
class TestCloudWatchBuffering:
    """Metrics are buffered and only sent when the buffer reaches 20 or
    ``flush()`` is called explicitly."""

    def _make_backend(self) -> tuple[CloudWatchBackend, MagicMock]:
        """Create a CloudWatchBackend with a mocked boto3 client."""
        backend = CloudWatchBackend(namespace="Test/Namespace")
        fake_cw = MagicMock()
        backend._client = fake_cw
        return backend, fake_cw

    def test_buffer_accumulates_without_flushing(self) -> None:
        backend, fake_cw = self._make_backend()
        for i in range(5):
            backend.record_sample_metric(f"s{i:03d}", "eui", float(i))
        # 5 metrics buffered — flush should not have been called yet.
        assert len(backend._buffer) == 5
        fake_cw.put_metric_data.assert_not_called()

    def test_auto_flush_at_threshold(self) -> None:
        backend, fake_cw = self._make_backend()
        for i in range(CloudWatchBackend._FLUSH_SIZE):
            backend.record_sample_metric(f"s{i:03d}", "eui", float(i))
        # The 20th metric triggers auto-flush, so buffer is now empty.
        assert len(backend._buffer) == 0
        fake_cw.put_metric_data.assert_called_once()
        call_kwargs = fake_cw.put_metric_data.call_args
        assert call_kwargs.kwargs["Namespace"] == "Test/Namespace"
        metric_data = call_kwargs.kwargs["MetricData"]
        assert len(metric_data) == CloudWatchBackend._FLUSH_SIZE

    def test_explicit_flush_sends_buffered(self) -> None:
        backend, fake_cw = self._make_backend()
        backend.record_step_duration("APPLY_PARAMETERS", 3.14)
        backend.record_step_duration("RUN_OPENSTUDIO_SIM", 42.0)
        assert len(backend._buffer) == 2
        backend.flush()
        assert len(backend._buffer) == 0
        fake_cw.put_metric_data.assert_called_once()

    def test_flush_on_empty_buffer_is_noop(self) -> None:
        backend, fake_cw = self._make_backend()
        backend.flush()  # nothing buffered
        fake_cw.put_metric_data.assert_not_called()

    def test_step_duration_includes_dimensions(self) -> None:
        backend, fake_cw = self._make_backend()
        backend.record_step_duration("APPLY_PARAMETERS", 1.5, generation=2)
        backend.flush()
        call_kwargs = fake_cw.put_metric_data.call_args
        metric = call_kwargs.kwargs["MetricData"][0]
        assert metric["MetricName"] == "StepDuration"
        assert metric["Value"] == 1.5
        dims = {d["Name"]: d["Value"] for d in metric["Dimensions"]}
        assert dims["StepName"] == "APPLY_PARAMETERS"
        assert dims["Generation"] == "2"

    def test_sample_metric_includes_sample_id_dimension(self) -> None:
        backend, fake_cw = self._make_backend()
        backend.record_sample_metric("s042", "simulation_time", 300.0)
        backend.flush()
        metric = fake_cw.put_metric_data.call_args.kwargs["MetricData"][0]
        assert metric["MetricName"] == "simulation_time"
        assert metric["Value"] == 300.0
        dims = {d["Name"]: d["Value"] for d in metric["Dimensions"]}
        assert dims["SampleId"] == "s042"

    def test_campaign_duration_no_dimensions(self) -> None:
        backend, fake_cw = self._make_backend()
        backend.record_campaign_duration(1234.0)
        backend.flush()
        metric = fake_cw.put_metric_data.call_args.kwargs["MetricData"][0]
        assert metric["MetricName"] == "CampaignDuration"
        assert metric["Value"] == 1234.0
        assert "Dimensions" not in metric


# ---------------------------------------------------------------------------
# CloudWatchBackend — lazy boto3 import
# ---------------------------------------------------------------------------
class TestCloudWatchLazyImport:
    """boto3 must not be imported at module load — only on first flush."""

    def test_no_boto3_import_at_construction(self) -> None:
        """Constructing a CloudWatchBackend does not import boto3."""
        with patch.dict("sys.modules", {"boto3": None}):
            # This should not raise — boto3 is only needed on flush.
            backend = CloudWatchBackend()
            assert backend._client is None

    def test_flush_triggers_boto3_import(self) -> None:
        """Flushing with no pre-set client triggers the lazy import."""
        fake_boto3 = MagicMock()
        fake_cw_client = MagicMock()
        fake_boto3.client.return_value = fake_cw_client

        backend = CloudWatchBackend(namespace="Test/Lazy")
        with patch.dict("sys.modules", {"boto3": fake_boto3}):
            backend.record_campaign_duration(1.0)
            backend.flush()

        fake_boto3.client.assert_called_once_with(service_name="cloudwatch")
        fake_cw_client.put_metric_data.assert_called_once()

    def test_region_forwarded_to_boto3(self) -> None:
        """When region is set, it is passed to boto3.client()."""
        fake_boto3 = MagicMock()
        fake_cw_client = MagicMock()
        fake_boto3.client.return_value = fake_cw_client

        backend = CloudWatchBackend(namespace="Test/Region", region="eu-west-1")
        with patch.dict("sys.modules", {"boto3": fake_boto3}):
            backend.record_campaign_duration(1.0)
            backend.flush()

        fake_boto3.client.assert_called_once_with(
            service_name="cloudwatch", region_name="eu-west-1"
        )

    def test_import_error_without_boto3(self) -> None:
        """If boto3 cannot be imported, flush raises ImportError with a
        helpful message."""
        backend = CloudWatchBackend()
        backend.record_campaign_duration(1.0)
        with patch.dict("sys.modules", {"boto3": None}):
            with pytest.raises(ImportError, match="pip install osimflow\\[aws\\]"):
                backend.flush()

    def test_client_reused_across_flushes(self) -> None:
        """The boto3 client is created once and reused for subsequent flushes."""
        fake_boto3 = MagicMock()
        fake_cw_client = MagicMock()
        fake_boto3.client.return_value = fake_cw_client

        backend = CloudWatchBackend()
        with patch.dict("sys.modules", {"boto3": fake_boto3}):
            backend.record_campaign_duration(1.0)
            backend.flush()
            backend.record_campaign_duration(2.0)
            backend.flush()

        # boto3.client called only once — client reused.
        assert fake_boto3.client.call_count == 1
        assert fake_cw_client.put_metric_data.call_count == 2
