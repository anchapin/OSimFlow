"""Real metric-sink validation for the observability backends (issue #947).

Only runs when ``OSIMFLOW_OBSERVABILITY_REAL=1`` is set **and** the
per-backend sink endpoints are configured.  The whole module is skipped
by default so it never runs in PR CI and does not affect the coverage
gate (the tests report as ``s``/skipped, not errors).

The three pluggable backends in ``osimflow/observability.py``
(:class:`~osimflow.observability.CloudWatchBackend`,
:class:`~osimflow.observability.PrometheusBackend`,
:class:`~osimflow.observability.OpenTelemetryBackend`) are otherwise
exercised exclusively via ``unittest.mock`` (see
``tests/unit/test_observability_*.py``).  These tests validate that the
wire format — metric names, label/dimension shapes, and API call
signatures — actually lands in a real metric sink.

Requirements per backend (configure only the ones you want to exercise):

  CloudWatch (test_cloudwatch_real_sink)
    - ``OSIMFLOW_CW_NAMESPACE``      CloudWatch metric namespace to publish under.
    - ``OSIMFLOW_CW_LOG_GROUP``      CloudWatch log group (presence gate; the
                                     metrics backend does not write logs itself,
                                     but this signals a fully-configured CW setup).
    - ``OSIMFLOW_CW_REGION``         AWS region (falls back to ``AWS_REGION`` /
                                     ``AWS_DEFAULT_REGION``).
    - AWS credentials via the IAM role / env (the boto3 default chain).

  Prometheus (test_prometheus_real_sink)
    - ``OSIMFLOW_PROMETHEUS_URL``    Pushgateway ``host:port`` (e.g. ``localhost:9091``).
    - ``OSIMFLOW_PROMETHEUS_JOB``    Optional job label (default: a unique
                                     ``osimflow-realtest-<rand>`` per run).

  OpenTelemetry (test_opentelemetry_real_sink)
    - ``OSIMFLOW_OTEL_ENDPOINT``      OTLP gRPC endpoint (e.g. ``localhost:4317``).
    - ``OSIMFLOW_OTEL_OUTPUT_FILE``   File the otel-collector ``file`` exporter
                                      writes to (used to verify the metric landed).

To run a single backend locally (Prometheus is the simplest)::

    docker run --rm -d -p 9091:9091 prom/pushgateway
    OSIMFLOW_OBSERVABILITY_REAL=1 OSIMFLOW_PROMETHEUS_URL=localhost:9091 \
        .venv/bin/pytest tests/integration/test_observability_real_sinks.py \
        -k prometheus -v --no-cov

See ``docs/observability.md`` → "Real-sink validation" for full local-sink
setup guides (pushgateway, otel-collector config, AWS CloudWatch guidance).
"""

import os
import time
import urllib.error
import urllib.request
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from osimflow.observability import (
    CloudWatchBackend,
    OpenTelemetryBackend,
    PrometheusBackend,
)

# Inert unless explicitly enabled. Modeled on the
# ``OSIMFLOW_AWS_BATCH_E2E`` gate in ``tests/integration/test_aws_batch_real.py``.
pytestmark = pytest.mark.skipif(
    os.environ.get("OSIMFLOW_OBSERVABILITY_REAL") != "1",
    reason="Set OSIMFLOW_OBSERVABILITY_REAL=1 and configure sink endpoints to validate real backends",
)

REPO_ROOT = Path(__file__).resolve().parents[2]

# Per-backend readiness, evaluated once at collection time so partial
# configurations still work (e.g. only Prometheus configured).
_CW_READY = bool(os.environ.get("OSIMFLOW_CW_NAMESPACE")) and bool(
    os.environ.get("OSIMFLOW_CW_LOG_GROUP")
)
_PROM_READY = bool(os.environ.get("OSIMFLOW_PROMETHEUS_URL"))
_OTEL_READY = bool(os.environ.get("OSIMFLOW_OTEL_ENDPOINT")) and bool(
    os.environ.get("OSIMFLOW_OTEL_OUTPUT_FILE")
)


@pytest.mark.skipif(
    not _CW_READY,
    reason="Set OSIMFLOW_CW_NAMESPACE, OSIMFLOW_CW_LOG_GROUP, and AWS creds to validate CloudWatch",
)
def test_cloudwatch_real_sink() -> None:
    """Push a metric to real CloudWatch and read it back via ``get_metric_data``.

    Records a ``status`` metric carrying a unique ``SampleId`` dimension,
    flushes, then polls ``get_metric_data`` for that exact dimension until
    the metric surfaces (CloudWatch custom metrics take a few seconds to
    become queryable).
    """
    import boto3

    namespace = os.environ["OSIMFLOW_CW_NAMESPACE"]
    region = (
        os.environ.get("OSIMFLOW_CW_REGION")
        or os.environ.get("AWS_REGION")
        or os.environ.get("AWS_DEFAULT_REGION")
    )

    marker = f"cw-realtest-{uuid.uuid4().hex[:8]}"

    backend = CloudWatchBackend(namespace=namespace, region=region)
    backend.record_sample_metric(marker, "status", 1.0)
    backend.record_step_duration("RUN_OPENSTUDIO_SIM", 42.0)
    backend.flush()

    cw_kwargs: dict[str, str] = {}
    if region:
        cw_kwargs["region_name"] = region
    cw = boto3.client("cloudwatch", **cw_kwargs)

    metric_query = [
        {
            "Id": "m_status",
            "MetricStat": {
                "Metric": {
                    "Namespace": namespace,
                    "MetricName": "status",
                    "Dimensions": [{"Name": "SampleId", "Value": marker}],
                },
                "Period": 60,
                "Stat": "Maximum",
            },
        }
    ]

    found = False
    for _ in range(12):  # up to ~60s; CW custom metrics need a few seconds to surface
        now = datetime.now(UTC)
        resp = cw.get_metric_data(
            MetricDataQueries=metric_query,
            StartTime=now - timedelta(minutes=5),
            EndTime=now,
        )
        results = resp.get("MetricDataResults", [])
        if results and results[0].get("Values"):
            found = True
            break
        time.sleep(5)

    assert found, (
        f"CloudWatch metric 'status' with SampleId={marker!r} was not visible in "
        f"namespace {namespace!r} within the ~60s poll window"
    )


@pytest.mark.skipif(
    not _PROM_READY,
    reason="Set OSIMFLOW_PROMETHEUS_URL (pushgateway host:port) to validate Prometheus",
)
def test_prometheus_real_sink() -> None:
    """Push a metric to a real Prometheus pushgateway and scrape it back.

    Records a ``status`` gauge carrying a unique ``sample_id`` label, pushes
    to the pushgateway, then HTTP-GETs ``/metrics`` and asserts the unique
    label is present in the exposition. Cleans up the pushed job afterwards
    (best-effort).
    """
    url = os.environ["OSIMFLOW_PROMETHEUS_URL"]
    job = os.environ.get("OSIMFLOW_PROMETHEUS_JOB", f"osimflow-realtest-{uuid.uuid4().hex[:6]}")
    marker = f"prom-realtest-{uuid.uuid4().hex[:8]}"

    backend = PrometheusBackend(pushgateway_url=url, job_name=job)
    backend.record_sample_metric(marker, "status", 1.0)
    backend.record_step_duration("RUN_OPENSTUDIO_SIM", 42.0)
    backend.flush()

    scrape_url = f"http://{url}/metrics"
    found = False
    for _ in range(5):  # pushgateway serves synchronously; a short poll suffices
        try:
            with urllib.request.urlopen(scrape_url, timeout=5) as resp:
                body = resp.read().decode("utf-8", errors="replace")
        except urllib.error.URLError:
            body = ""
        if marker in body:
            found = True
            break
        time.sleep(2)

    # Best-effort cleanup of the pushed job group.
    try:
        from prometheus_client import delete_from_gateway

        delete_from_gateway(url, job=job)
    except Exception:  # noqa: BLE001, S110 - cleanup is best-effort
        pass

    assert found, (
        f"Prometheus pushgateway at {scrape_url!r} did not expose the unique "
        f"label sample_id={marker!r} after push"
    )


@pytest.mark.skipif(
    not _OTEL_READY,
    reason="Set OSIMFLOW_OTEL_ENDPOINT (OTLP gRPC) and OSIMFLOW_OTEL_OUTPUT_FILE to validate OpenTelemetry",
)
def test_opentelemetry_real_sink() -> None:
    """Export a metric to a real OTLP collector and verify it in the file output.

    Records a gauge carrying a unique ``sample_id`` attribute, flushes, then
    forces the OTel SDK to export (``MeterProvider.force_flush``) and polls
    the collector's file-export output until the unique attribute appears.
    """
    from opentelemetry import metrics as otel_metrics

    endpoint = os.environ["OSIMFLOW_OTEL_ENDPOINT"]
    output_file = Path(os.environ["OSIMFLOW_OTEL_OUTPUT_FILE"])
    marker = f"otel-realtest-{uuid.uuid4().hex[:8]}"

    backend = OpenTelemetryBackend(endpoint=endpoint, service_name="osimflow-realtest")
    backend.record_sample_metric(marker, "status", 1.0)
    backend.record_step_duration("RUN_OPENSTUDIO_SIM", 42.0)
    backend.flush()

    # The OTel SDK defers export to a periodic reader (~60s interval).
    # Force it now so the assertion can succeed quickly. This relies on
    # the global MeterProvider being the one the backend just installed
    # (the case when this skip-gated test runs in an isolated process).
    provider = otel_metrics.get_meter_provider()
    force_flush = getattr(provider, "force_flush", None)
    if force_flush is not None:
        force_flush()

    found = False
    for _ in range(20):  # up to ~80s; covers the 60s periodic-export fallback
        try:
            text = output_file.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            text = ""
        if marker in text:
            found = True
            break
        time.sleep(4)

    assert found, (
        f"OpenTelemetry collector output {output_file!r} did not contain the "
        f"unique attribute sample_id={marker!r} after force_flush"
    )
