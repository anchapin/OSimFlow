"""Tests for --observability CLI flag, CampaignConfig fields, and backend wiring.

Verifies:
- CLI flag parsing for --observability and related flags
- CampaignConfig observability fields
- NullBackend used by default (zero overhead)
- Backend instantiation for each type (with mocks)
- Observability metrics recorded at key campaign lifecycle points
- No MLflow regression when observability is enabled
"""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from osimflow import (
    Campaign,
    CampaignConfig,
    CloudWatchBackend,
    NullBackend,
    ObservabilityManager,
    OpenTelemetryBackend,
    PrometheusBackend,
)
from osimflow.__main__ import _build_parser

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_cfg(**overrides: object) -> CampaignConfig:
    """Create a CampaignConfig with sensible defaults for testing."""
    defaults = {
        "input_variables": Path("/tmp/variables.yml"),
        "template_sim_package": Path("/tmp/template"),
        "n_samples": 2,
        "outdir": Path("/tmp/results"),
        "openstudio_version": "3.11.0",
    }
    defaults.update(overrides)
    return CampaignConfig(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# CLI flag parsing
# ---------------------------------------------------------------------------


class TestCLIFlagParsing:
    """Test that --observability and related flags parse correctly."""

    def test_default_observability_is_none(self) -> None:
        parser = _build_parser()
        args = parser.parse_args(
            [
                "run",
                "--input_variables",
                "variables.yml",
                "--template_sim_package",
                "./pkg",
                "--n_samples",
                "5",
                "--outdir",
                "./out",
            ]
        )
        assert args.observability == "none"

    def test_cloudwatch_flag(self) -> None:
        parser = _build_parser()
        args = parser.parse_args(
            [
                "run",
                "--input_variables",
                "variables.yml",
                "--template_sim_package",
                "./pkg",
                "--n_samples",
                "5",
                "--outdir",
                "./out",
                "--observability",
                "cloudwatch",
                "--cloudwatch-namespace",
                "MyOrg/SimCampaigns",
                "--cloudwatch-log-group",
                "/osimflow/logs",
            ]
        )
        assert args.observability == "cloudwatch"
        assert args.cloudwatch_namespace == "MyOrg/SimCampaigns"
        assert args.cloudwatch_log_group == "/osimflow/logs"

    def test_prometheus_flag(self) -> None:
        parser = _build_parser()
        args = parser.parse_args(
            [
                "run",
                "--input_variables",
                "variables.yml",
                "--template_sim_package",
                "./pkg",
                "--n_samples",
                "5",
                "--outdir",
                "./out",
                "--observability",
                "prometheus",
                "--prometheus-port",
                "9091",
            ]
        )
        assert args.observability == "prometheus"
        assert args.prometheus_port == 9091

    def test_opentelemetry_flag(self) -> None:
        parser = _build_parser()
        args = parser.parse_args(
            [
                "run",
                "--input_variables",
                "variables.yml",
                "--template_sim_package",
                "./pkg",
                "--n_samples",
                "5",
                "--outdir",
                "./out",
                "--observability",
                "opentelemetry",
                "--otel-endpoint",
                "http://otel-collector:4317",
            ]
        )
        assert args.observability == "opentelemetry"
        assert args.otel_endpoint == "http://otel-collector:4317"

    def test_invalid_observability_rejected(self) -> None:
        parser = _build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(
                [
                    "run",
                    "--input_variables",
                    "variables.yml",
                    "--template_sim_package",
                    "./pkg",
                    "--n_samples",
                    "5",
                    "--outdir",
                    "./out",
                    "--observability",
                    "datadog",
                ]
            )

    def test_default_cloudwatch_namespace(self) -> None:
        parser = _build_parser()
        args = parser.parse_args(
            [
                "run",
                "--input_variables",
                "variables.yml",
                "--template_sim_package",
                "./pkg",
                "--n_samples",
                "5",
                "--outdir",
                "./out",
            ]
        )
        assert args.cloudwatch_namespace == "OSimFlow"

    def test_default_prometheus_port(self) -> None:
        parser = _build_parser()
        args = parser.parse_args(
            [
                "run",
                "--input_variables",
                "variables.yml",
                "--template_sim_package",
                "./pkg",
                "--n_samples",
                "5",
                "--outdir",
                "./out",
            ]
        )
        assert args.prometheus_port == 9090

    def test_default_otel_endpoint_is_none(self) -> None:
        parser = _build_parser()
        args = parser.parse_args(
            [
                "run",
                "--input_variables",
                "variables.yml",
                "--template_sim_package",
                "./pkg",
                "--n_samples",
                "5",
                "--outdir",
                "./out",
            ]
        )
        assert args.otel_endpoint is None


# ---------------------------------------------------------------------------
# CampaignConfig fields
# ---------------------------------------------------------------------------


class TestCampaignConfigFields:
    """Test that CampaignConfig includes observability fields."""

    def test_default_observability_is_none(self) -> None:
        cfg = _make_cfg()
        assert cfg.observability == "none"

    def test_default_cloudwatch_namespace(self) -> None:
        cfg = _make_cfg()
        assert cfg.cloudwatch_namespace == "OSimFlow"

    def test_default_cloudwatch_log_group_is_none(self) -> None:
        cfg = _make_cfg()
        assert cfg.cloudwatch_log_group is None

    def test_default_prometheus_port(self) -> None:
        cfg = _make_cfg()
        assert cfg.prometheus_port == 9090

    def test_default_otel_endpoint_is_none(self) -> None:
        cfg = _make_cfg()
        assert cfg.otel_endpoint is None

    def test_custom_observability_values(self) -> None:
        cfg = _make_cfg(
            observability="cloudwatch",
            cloudwatch_namespace="MyOrg",
            cloudwatch_log_group="/logs",
            prometheus_port=9091,
            otel_endpoint="http://localhost:4317",
        )
        assert cfg.observability == "cloudwatch"
        assert cfg.cloudwatch_namespace == "MyOrg"
        assert cfg.cloudwatch_log_group == "/logs"
        assert cfg.prometheus_port == 9091
        assert cfg.otel_endpoint == "http://localhost:4317"


# ---------------------------------------------------------------------------
# Backend instantiation
# ---------------------------------------------------------------------------


class TestBackendInstantiation:
    """Test that ObservabilityManager._build_backend produces the correct backend."""

    def test_none_produces_null_backend(self) -> None:
        cfg = _make_cfg(observability="none")
        backend = ObservabilityManager._build_backend(cfg)
        assert isinstance(backend, NullBackend)

    def test_cloudwatch_produces_cloudwatch_backend(self) -> None:
        cfg = _make_cfg(
            observability="cloudwatch",
            cloudwatch_namespace="TestNS",
        )
        backend = ObservabilityManager._build_backend(cfg)
        assert isinstance(backend, CloudWatchBackend)
        assert backend._namespace == "TestNS"

    def test_prometheus_produces_prometheus_backend(self) -> None:
        cfg = _make_cfg(
            observability="prometheus",
            prometheus_port=9091,
        )
        backend = ObservabilityManager._build_backend(cfg)
        assert isinstance(backend, PrometheusBackend)
        assert backend._url == "localhost:9091"

    def test_opentelemetry_produces_otel_backend(self) -> None:
        cfg = _make_cfg(
            observability="opentelemetry",
            otel_endpoint="http://collector:4317",
        )
        backend = ObservabilityManager._build_backend(cfg)
        assert isinstance(backend, OpenTelemetryBackend)
        assert backend._endpoint == "http://collector:4317"

    def test_opentelemetry_default_endpoint(self) -> None:
        cfg = _make_cfg(
            observability="opentelemetry",
        )
        backend = ObservabilityManager._build_backend(cfg)
        assert isinstance(backend, OpenTelemetryBackend)
        assert backend._endpoint == "http://localhost:4317"

    def test_unknown_backend_raises_value_error(self) -> None:
        cfg = _make_cfg(observability="datadog")  # type: ignore[call-arg]
        with pytest.raises(ValueError, match="unknown observability backend"):
            ObservabilityManager._build_backend(cfg)


# ---------------------------------------------------------------------------
# NullBackend zero overhead
# ---------------------------------------------------------------------------


class TestNullBackendZeroOverhead:
    """Verify NullBackend adds no measurable overhead."""

    def test_null_backend_all_methods_are_noop(self) -> None:
        backend = NullBackend()
        # None of these should raise or produce side effects.
        backend.record_step_duration("step", 1.0)
        backend.record_sample_metric("sample_0", "eui", 100.0)
        backend.record_campaign_duration(42.0)
        backend.flush()

    def test_null_backend_no_latency(self) -> None:
        """Call each NullBackend method 100k times; must complete in < 0.5s."""
        backend = NullBackend()
        t0 = time.perf_counter()
        for _ in range(100_000):
            backend.record_step_duration("step", 1.0, generation=0)
            backend.record_sample_metric("s0", "eui", 100.0)
            backend.record_campaign_duration(42.0)
        backend.flush()
        elapsed = time.perf_counter() - t0
        assert elapsed < 0.5, f"NullBackend added {elapsed:.3f}s overhead for 100k calls"


# ---------------------------------------------------------------------------
# Backend wiring into Campaign
# ---------------------------------------------------------------------------


class TestCampaignBackendWiring:
    """Test that Campaign uses the correct backend based on config."""

    def _make_mock_executor(self) -> MagicMock:
        mock_executor = MagicMock()
        mock_executor.name = "local"
        return mock_executor

    @patch("osimflow.campaign.build_cache")
    def test_campaign_uses_null_backend_by_default(
        self,
        mock_cache_cls: MagicMock,
    ) -> None:
        mock_cache = MagicMock()
        mock_cache.stats.return_value = "hits=0 misses=0"
        mock_cache_cls.return_value = mock_cache

        cfg = _make_cfg(observability="none")
        campaign = Campaign(cfg, self._make_mock_executor())
        assert isinstance(campaign._obs.backend, NullBackend)

    @patch("osimflow.campaign.build_cache")
    def test_campaign_uses_cloudwatch_backend(
        self,
        mock_cache_cls: MagicMock,
    ) -> None:
        mock_cache = MagicMock()
        mock_cache.stats.return_value = "hits=0 misses=0"
        mock_cache_cls.return_value = mock_cache

        cfg = _make_cfg(
            observability="cloudwatch",
            cloudwatch_namespace="TestNS",
        )
        campaign = Campaign(cfg, self._make_mock_executor())
        assert isinstance(campaign._obs.backend, CloudWatchBackend)
        assert campaign._obs.backend._namespace == "TestNS"

    @patch("osimflow.campaign.build_cache")
    def test_campaign_uses_prometheus_backend(
        self,
        mock_cache_cls: MagicMock,
    ) -> None:
        mock_cache = MagicMock()
        mock_cache.stats.return_value = "hits=0 misses=0"
        mock_cache_cls.return_value = mock_cache

        cfg = _make_cfg(
            observability="prometheus",
            prometheus_port=9091,
        )
        campaign = Campaign(cfg, self._make_mock_executor())
        assert isinstance(campaign._obs.backend, PrometheusBackend)

    @patch("osimflow.campaign.build_cache")
    def test_campaign_uses_otel_backend(
        self,
        mock_cache_cls: MagicMock,
    ) -> None:
        mock_cache = MagicMock()
        mock_cache.stats.return_value = "hits=0 misses=0"
        mock_cache_cls.return_value = mock_cache

        cfg = _make_cfg(
            observability="opentelemetry",
            otel_endpoint="http://collector:4317",
        )
        campaign = Campaign(cfg, self._make_mock_executor())
        assert isinstance(campaign._obs.backend, OpenTelemetryBackend)


# ---------------------------------------------------------------------------
# Observability metric recording
# ---------------------------------------------------------------------------


class TestMetricRecording:
    """Test that observability methods are called at key lifecycle points."""

    def test_null_backend_record_step_duration(self) -> None:
        """Verify the interface contract — NullBackend accepts all calls."""
        backend = NullBackend()
        # All of these should succeed without error
        backend.record_step_duration("GENERATE_LHS_SAMPLES", 0.5, generation=0)
        backend.record_step_duration("RUN_OPENSTUDIO_SIM", 120.0, generation=0)
        backend.record_step_duration("EXTRACT_KPIS", 5.0, generation=0)
        backend.record_step_duration("AGGREGATE_RESULTS", 2.0, generation=0)
        backend.record_step_duration("GENERATE_BASIC_PLOTS", 1.0, generation=0)

    def test_null_backend_record_sample_metrics(self) -> None:
        backend = NullBackend()
        backend.record_sample_metric("sample_0", "status", 1.0)
        backend.record_sample_metric("sample_0", "eui", 120.5)
        backend.record_sample_metric("sample_0", "cost_usd", 0.05)
        backend.record_sample_metric("sample_0", "duration_s", 300.0)

    def test_null_backend_record_campaign_duration(self) -> None:
        backend = NullBackend()
        backend.record_campaign_duration(3600.0)


# ---------------------------------------------------------------------------
# No MLflow regression
# ---------------------------------------------------------------------------


class TestNoMLflowRegression:
    """Verify that observability does not interfere with MLflow."""

    def test_mlflow_tracking_uri_still_works_with_observability(self) -> None:
        """Both --mlflow_tracking_uri and --observability should coexist."""
        cfg = _make_cfg(
            observability="none",
            mlflow_tracking_uri="http://localhost:5000",
        )
        assert cfg.mlflow_tracking_uri == "http://localhost:5000"
        assert cfg.observability == "none"

    def test_cloudwatch_and_mlflow_can_coexist(self) -> None:
        cfg = _make_cfg(
            observability="cloudwatch",
            mlflow_tracking_uri="http://localhost:5000",
        )
        assert cfg.observability == "cloudwatch"
        assert cfg.mlflow_tracking_uri == "http://localhost:5000"

    def test_prometheus_and_mlflow_can_coexist(self) -> None:
        cfg = _make_cfg(
            observability="prometheus",
            mlflow_tracking_uri="http://mlflow:5000",
        )
        assert cfg.observability == "prometheus"
        assert cfg.mlflow_tracking_uri == "http://mlflow:5000"


# ---------------------------------------------------------------------------
# load_config integration
# ---------------------------------------------------------------------------


class TestLoadConfigIntegration:
    """Test that load_config correctly maps CLI args to CampaignConfig."""

    def test_load_config_with_observability_none(self, tmp_path: Path) -> None:
        """load_config maps observability CLI args to CampaignConfig."""
        from osimflow.config import load_config

        variables_yml = tmp_path / "variables.yml"
        variables_yml.write_text(
            "variables:\n  - name: test\n    distribution: uniform\n    min: 0\n    max: 1\n"
        )
        template = tmp_path / "template"
        template.mkdir()
        (template / "workflow.osw").write_text("{}")
        outdir = tmp_path / "out"

        args = {
            "input_variables": str(variables_yml),
            "template_sim_package": str(template),
            "n_samples": 5,
            "outdir": str(outdir),
            "openstudio_version": "3.11.0",
            "archive_intermediates": False,
            "mlflow_tracking_uri": None,
            "slurm_qos": None,
            "slurm_constraint": None,
            "slurm_gres": None,
            "weather_dir": "weather",
            "dry_run": False,
            "sample": None,
            "algorithm": "lhs",
            "init_script": None,
            "finalize_script": None,
            "skip_preflight": False,
            "max_generations": 1,
            "aws_batch_max_spot_price_usd": None,
            "aws_batch_fallback_to_on_demand": False,
            "aws_batch_max_retries": 3,
            "ecr_repository": None,
            "observability": "none",
            "cloudwatch_namespace": "OSimFlow",
            "cloudwatch_log_group": None,
            "prometheus_port": 9090,
            "otel_endpoint": None,
        }
        cfg = load_config(args)
        assert cfg.observability == "none"
        assert cfg.cloudwatch_namespace == "OSimFlow"
        assert cfg.prometheus_port == 9090

    def test_load_config_with_cloudwatch(self, tmp_path: Path) -> None:
        from osimflow.config import load_config

        variables_yml = tmp_path / "variables.yml"
        variables_yml.write_text(
            "variables:\n  - name: test\n    distribution: uniform\n    min: 0\n    max: 1\n"
        )
        template = tmp_path / "template"
        template.mkdir()
        (template / "workflow.osw").write_text("{}")
        outdir = tmp_path / "out"

        args = {
            "input_variables": str(variables_yml),
            "template_sim_package": str(template),
            "n_samples": 5,
            "outdir": str(outdir),
            "openstudio_version": "3.11.0",
            "archive_intermediates": False,
            "mlflow_tracking_uri": None,
            "slurm_qos": None,
            "slurm_constraint": None,
            "slurm_gres": None,
            "weather_dir": "weather",
            "dry_run": False,
            "sample": None,
            "algorithm": "lhs",
            "init_script": None,
            "finalize_script": None,
            "skip_preflight": False,
            "max_generations": 1,
            "aws_batch_max_spot_price_usd": None,
            "aws_batch_fallback_to_on_demand": False,
            "aws_batch_max_retries": 3,
            "ecr_repository": None,
            "observability": "cloudwatch",
            "cloudwatch_namespace": "MyOrg/Sims",
            "cloudwatch_log_group": "/osimflow/logs",
            "prometheus_port": 9090,
            "otel_endpoint": None,
        }
        cfg = load_config(args)
        assert cfg.observability == "cloudwatch"
        assert cfg.cloudwatch_namespace == "MyOrg/Sims"
        assert cfg.cloudwatch_log_group == "/osimflow/logs"
