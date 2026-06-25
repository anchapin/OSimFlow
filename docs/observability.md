# Observability

OSimFlow ships with pluggable observability backends that emit metrics at key campaign lifecycle points. This is separate from the built-in `run.json` trace and the optional MLflow integration.

## CLI Flags

| Flag | Choices / Type | Default | Description |
|------|---------------|---------|-------------|
| `--observability` | `none`, `cloudwatch`, `prometheus`, `opentelemetry` | `none` | Select the observability backend. `none` = zero overhead. |
| `--cloudwatch-namespace` | string | `OSimFlow` | CloudWatch metric namespace. |
| `--cloudwatch-log-group` | string | *(none)* | Optional CloudWatch log group name. |
| `--prometheus-port` | integer | `9090` | Prometheus pushgateway port. |
| `--otel-endpoint` | string | *(none)* | OTLP gRPC endpoint, e.g. `http://localhost:4317`. |

### Example Usage

```bash
# CloudWatch (requires pip install osimflow[aws])
osimflow run \
  --executor aws_batch \
  --observability cloudwatch \
  --cloudwatch-namespace MyOrg/SimCampaigns \
  --input_variables variables.yml \
  --template_sim_package ./pkg \
  --n_samples 100 \
  --outdir ./results

# Prometheus pushgateway
osimflow run \
  --executor local \
  --observability prometheus \
  --prometheus-port 9091 \
  --input_variables variables.yml \
  --template_sim_package ./pkg \
  --n_samples 50 \
  --outdir ./results

# OpenTelemetry (OTLP gRPC)
osimflow run \
  --executor slurm \
  --observability opentelemetry \
  --otel-endpoint http://otel-collector:4317 \
  --input_variables variables.yml \
  --template_sim_package ./pkg \
  --n_samples 500 \
  --outdir ./results
```

## Available Backends

### `none` (default)

No metrics are emitted. `NullBackend` is used — all methods are empty `pass` bodies. This adds zero measurable overhead (verified in tests: 100k calls in < 0.5s).

### `cloudwatch`

Emits metrics to AWS CloudWatch via `boto3`. Metrics are batched and flushed every 20 items (or at campaign completion).

**Requirements:** `pip install osimflow[aws]`

**Authentication:** Uses the IAM role attached to the compute environment (EC2 instance profile or ECS task role). No long-lived access keys.

### `prometheus`

Pushes metrics to a Prometheus pushgateway via `prometheus_client`. Metrics are buffered as gauge values and pushed on flush.

**Requirements:** `pip install prometheus_client`

### `opentelemetry`

Exports metrics via the OTLP gRPC exporter. Metrics are buffered and exported on flush.

**Requirements:** `pip install opentelemetry-api opentelemetry-sdk opentelemetry-exporter-otlp-proto-grpc`

## Metric Dictionary

The following metrics are emitted by all backends (except `none`).

### Campaign-Level Metrics

| Metric | Type | Description |
|--------|------|-------------|
| `CampaignDuration` / `osimflow_campaign_duration_seconds` / `osimflow.campaign.duration` | float | Total campaign wall-clock time in seconds. Recorded once at campaign completion. |

### Step-Level Metrics

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `StepDuration` / `osimflow_step_duration_seconds` / `osimflow.step.duration` | float | `step`, `generation` | Duration of each DAG step in seconds. Recorded at step completion. |

**Step names:**
- `GENERATE_LHS_SAMPLES` (or `GENERATE_<ALGO>_SAMPLES`)
- `PREFLIGHT_RUN_MODEL`
- `APPLY_PARAMETERS`
- `RUN_OPENSTUDIO_SIM`
- `EXTRACT_KPIS`
- `AGGREGATE_RESULTS`
- `GENERATE_BASIC_PLOTS`

### Sample-Level Metrics

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `status` / `osimflow_status` / `osimflow.sample.status` | float (1.0 or 0.0) | `sample_id` | Sample completion status: `1.0` = ok, `0.0` = failed. |
| `cost_usd` / `osimflow_cost_usd` / `osimflow.sample.cost_usd` | float | `sample_id` | Per-sample compute cost in USD (cloud executors only). |

### Backend-Specific Metric Names

Each backend uses its own naming convention:

| Backend | Step Duration | Sample Status | Campaign Duration |
|---------|--------------|---------------|-------------------|
| CloudWatch | `StepDuration` | `status` | `CampaignDuration` |
| Prometheus | `osimflow_step_duration_seconds` | `osimflow_status` | `osimflow_campaign_duration_seconds` |
| OpenTelemetry | `osimflow.step.duration` | `osimflow.sample.status` | `osimflow.campaign.duration` |

## Relationship to MLflow

The `--observability` flag is **independent** of the existing `--mlflow_tracking_uri` flag. They can be used together or separately:

- `--mlflow_tracking_uri` logs params, metrics, and artifacts to an MLflow tracking server (experiment-level tracking).
- `--observability` emits real-time metrics to a monitoring backend (infrastructure-level monitoring).

Both can be active simultaneously without conflict.

## Adding a Custom Backend

Subclass `ObservabilityBackend` and pass the instance to the Campaign constructor:

```python
from osimflow.observability import ObservabilityBackend

class MyDatadogBackend(ObservabilityBackend):
    def record_step_duration(self, step_name, duration_s, generation=0):
        # Push to Datadog
        ...
    def record_sample_metric(self, sample_id, metric_name, value):
        ...
    def record_campaign_duration(self, duration_s):
        ...
    def flush(self):
        ...
```

See `osimflow/observability.py` for the full ABC definition.

## Real-sink validation

The observability backends are unit-tested with mocks only (see
`tests/unit/test_observability_*.py`). To validate the real wire format —
metric names, label/dimension shapes, and API call signatures against live
metric sinks — a set of skip-gated integration tests live in
`tests/integration/test_observability_real_sinks.py`.

These tests are **inert by default**: the whole module is skipped unless
`OSIMFLOW_OBSERVABILITY_REAL=1` is set, so they never run in PR CI and do not
affect the coverage gate. Each backend is additionally gated on its own sink
configuration, so a partial setup (e.g. only a pushgateway running) still
works — the other two tests simply skip.

### Environment variables

In addition to `OSIMFLOW_OBSERVABILITY_REAL=1`, each backend needs its own
sink config:

| Backend | Variable | Purpose |
|---------|----------|---------|
| CloudWatch | `OSIMFLOW_CW_NAMESPACE` | CloudWatch metric namespace to publish under. |
| CloudWatch | `OSIMFLOW_CW_LOG_GROUP` | CloudWatch log group (presence gate). |
| CloudWatch | `OSIMFLOW_CW_REGION` | AWS region (falls back to `AWS_REGION` / `AWS_DEFAULT_REGION`). |
| Prometheus | `OSIMFLOW_PROMETHEUS_URL` | Pushgateway `host:port` (e.g. `localhost:9091`). |
| Prometheus | `OSIMFLOW_PROMETHEUS_JOB` | Optional job label (default: unique per run). |
| OpenTelemetry | `OSIMFLOW_OTEL_ENDPOINT` | OTLP gRPC endpoint (e.g. `localhost:4317`). |
| OpenTelemetry | `OSIMFLOW_OTEL_OUTPUT_FILE` | File the collector's file exporter writes to. |

### Running a single backend

Each test records a `status` metric carrying a unique marker (a UUID-derived
`sample_id` / `SampleId`), pushes it through the backend, and then reads it
back from the sink (CloudWatch `get_metric_data`, the pushgateway `/metrics`
endpoint, or the OTel collector's file output) and asserts the marker landed.

```bash
# Prometheus (simplest local sink — one container, no config)
docker run --rm -d -p 9091:9091 prom/pushgateway
OSIMFLOW_OBSERVABILITY_REAL=1 OSIMFLOW_PROMETHEUS_URL=localhost:9091 \
    .venv/bin/pytest tests/integration/test_observability_real_sinks.py \
    -k prometheus -v --no-cov
```

The CloudWatch metric names, Prometheus label names, and OpenTelemetry
attribute keys asserted by these tests match the [Metric Dictionary](#metric-dictionary)
exactly.

### Recommended local sink setup

#### Prometheus pushgateway

One container, no configuration file. Requires `pip install prometheus_client`.

```bash
docker run --rm -d -p 9091:9091 prom/pushgateway
export OSIMFLOW_OBSERVABILITY_REAL=1
export OSIMFLOW_PROMETHEUS_URL=localhost:9091
.venv/bin/pytest tests/integration/test_observability_real_sinks.py -k prometheus -v --no-cov
```

The test cleans up its pushed job group after asserting (best-effort).

#### OpenTelemetry collector

Run an `otel-collector` with an OTLP **gRPC** receiver and a `file` exporter,
then point the test at the output file. Save this collector config locally:

```yaml
receivers:
  otlp:
    protocols:
      grpc:
        endpoint: 0.0.0.0:4317
exporters:
  file:
    path: /tmp/otel-metrics.json
service:
  pipelines:
    metrics:
      receivers: [otlp]
      exporters: [file]
```

Then run the collector and the test:

```bash
docker run --rm -d -p 4317:4317 \
    -v $(pwd)/otel-collector-config.yaml:/etc/otelcol/config.yaml \
    -v /tmp:/tmp \
    otel/opentelemetry-collector
export OSIMFLOW_OBSERVABILITY_REAL=1
export OSIMFLOW_OTEL_ENDPOINT=localhost:4317
export OSIMFLOW_OTEL_OUTPUT_FILE=/tmp/otel-metrics.json
.venv/bin/pytest tests/integration/test_observability_real_sinks.py -k opentelemetry -v --no-cov
```

Requires `pip install opentelemetry-api opentelemetry-sdk
opentelemetry-exporter-otlp-proto-grpc`. The test calls
`MeterProvider.force_flush()` to trigger an immediate export, then polls the
output file for the unique attribute (the SDK's periodic reader otherwise
exports every ~60 s).

#### CloudWatch

Point the test at a real AWS account. Authentication uses the standard boto3
credential chain (instance profile, `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY`,
or OIDC) — never long-lived keys committed to config.

```bash
export OSIMFLOW_OBSERVABILITY_REAL=1
export OSIMFLOW_CW_NAMESPACE=OSimFlow/RealSinkTest
export OSIMFLOW_CW_LOG_GROUP=/osimflow/real-sink-test
export OSIMFLOW_CW_REGION=us-east-1
.venv/bin/pytest tests/integration/test_observability_real_sinks.py -k cloudwatch -v --no-cov
```

CloudWatch custom metrics take a few seconds to become queryable; the test
polls `get_metric_data` for the unique `SampleId` dimension for up to ~60 s.
