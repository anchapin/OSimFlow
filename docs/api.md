# OSimFlow REST API

> Requires `pip install osimflow[api]`

## Starting the server

```bash
# Read-only (default) — browse completed campaigns
osimflow serve --outdir ./results

# Live mode — SSE events + campaign stop
osimflow serve --outdir ./results --read-write

# Custom host/port
osimflow serve --outdir ./results --host 127.0.0.1 --port 9000
```

## TLS (SEC-004)

**TLS is required for production deployments.** The API supports API key authentication (issue #268) but defaults to plain HTTP with no TLS enforcement. Without TLS, API keys are transmitted in clear text and are vulnerable to interception.

```bash
# Generate a self-signed certificate for testing
openssl req -x509 -newkey rsa:4096 -keyout /tmp/tls-key.pem -out /tmp/tls-cert.pem -days 365 -nodes -subj "/CN=localhost"

# Production: use a certificate from Let's Encrypt or your CA
osimflow serve --outdir ./results --tls-cert /path/to/cert.pem --tls-key /path/to/key.pem --host 0.0.0.0 --port 443
```

> **Important:** When `--enable-writes` or `--read-write` is set, the API accepts mutating requests (POST/PUT/DELETE). TLS is strongly recommended for these deployments to protect credentials in transit.

Both `--tls-cert` and `--tls-key` must be provided together. Omitting either one produces a clear error message at startup rather than a cryptic traceback.

## Health & Readiness

### GET /health

Liveness probe.

```bash
curl http://localhost:8000/health
```

```json
{"status": "alive"}
```

### GET /ready

Readiness probe — checks if `run.json` is accessible.

```bash
curl http://localhost:8000/ready
```

```json
{"status": "ready", "campaign_id": "my-campaign-001"}
```

## Campaign

### GET /api/v1/campaign

Campaign metadata from `run.json`.

```bash
curl http://localhost:8000/api/v1/campaign
```

```json
{
  "campaign_id": "my-campaign-001",
  "config_summary": {"executor": "local", "n_samples": 500},
  "started_at": 1718236800.0,
  "finished_at": 1718240400.0,
  "baseline_comparison": null
}
```

### POST /api/v1/campaign/stop

Write a stop flag to request campaign cancellation. **Requires `--read-write` mode.**

```bash
curl -X POST http://localhost:8000/api/v1/campaign/stop
```

```json
{"status": "stopping"}
```

Returns `403` in read-only mode (default).

## Steps

### GET /api/v1/steps

Step traces from `run.json`.

```bash
curl http://localhost:8000/api/v1/steps
```

```json
{
  "steps": [
    {"step": "GENERATE_LHS_SAMPLES", "cache": "MISS", "elapsed_s": 0.5, "exit_code": 0},
    {"step": "APPLY_PARAMETERS", "cache": "MISS×500", "elapsed_s": 12.3, "exit_code": 0},
    {"step": "RUN_OPENSTUDIO_SIM", "cache": "MISS×500", "elapsed_s": 3600.0, "exit_code": 0}
  ],
  "total_steps": 3
}
```

## Samples

### GET /api/v1/samples

Paginated per-sample traces.

```bash
# First page (default 50 items)
curl http://localhost:8000/api/v1/samples

# Custom pagination
curl "http://localhost:8000/api/v1/samples?page=2&per_page=100"
```

```json
{
  "samples": [
    {"sample_id": "sample_000", "status": "ok", "elapsed_s": 10.0},
    {"sample_id": "sample_001", "status": "failed", "elapsed_s": 5.0, "error_summary": "Severe Error"}
  ],
  "total": 500,
  "page": 1,
  "per_page": 50
}
```

### GET /api/v1/samples/{sid}

Single sample detail with KPIs and log file paths.

```bash
curl http://localhost:8000/api/v1/samples/sample_000
```

```json
{
  "sample_id": "sample_000",
  "status": "ok",
  "elapsed_s": 10.0,
  "kpis": {"eui_kwh_m2_yr": 120.5, "total_energy_kwh": 50000.0},
  "log_files": {
    "stdout.log": "/path/to/results/work/sim/sample_000/stdout.log",
    "stderr.log": "/path/to/results/work/sim/sample_000/stderr.log"
  }
}
```

## Results & Failures

### GET /api/v1/results

Aggregated results as JSON (from `aggregated_results.csv`).

```bash
curl http://localhost:8000/api/v1/results
```

```json
[
  {"sample_id": "sample_000", "eui": 120.5, "area": 500.0},
  {"sample_id": "sample_002", "eui": 98.3, "area": 480.0}
]
```

### GET /api/v1/failures

Failed simulations as JSON (from `failed_simulations.csv`).

```bash
curl http://localhost:8000/api/v1/failures
```

```json
[
  {"sample_id": "sample_001", "error_summary": "Severe Error in model"}
]
```

## Pareto Front

### GET /api/v1/pareto

Pareto front data from `outdir/pareto/gen_*.json` files.

```bash
curl http://localhost:8000/api/v1/pareto
```

```json
{
  "generations": [
    {
      "objective_names": ["eui", "cost"],
      "solutions": [
        {"sample_id": "s0", "objectives": {"eui": 100, "cost": 5000}}
      ],
      "_file": "gen_0.json"
    }
  ],
  "total_generations": 1
}
```

## Live Events (SSE)

### GET /api/v1/events

Server-Sent Events stream. **Requires `--read-write` mode.**

Polls `run.json` at ~1 Hz and emits structured events:
- `sample.started` — new sample detected
- `sample.completed` — sample finished (ok/failed/cached)
- `step.completed` — DAG step finished
- `campaign.completed` — entire campaign finished
- `ping` — heartbeat (~every 15 s)

```bash
# Connect to SSE stream
curl -N http://localhost:8000/api/v1/events
```

Example output:

```
event: step.completed
data: {"step": "GENERATE_LHS_SAMPLES", "cache": "MISS", "elapsed_s": 0.5, "exit_code": 0}

event: sample.completed
data: {"sample_id": "sample_000", "status": "ok", "elapsed_s": 10.0}

event: sample.completed
data: {"sample_id": "sample_001", "status": "failed", "elapsed_s": 5.0, "error_summary": "Severe Error"}

event: campaign.completed
data: {"campaign_id": "my-campaign-001", "finished_at": 1718240400.0, "elapsed_s": 3600.0}
```

Returns `403` in read-only mode (default).

## Error Responses

| Status | Meaning |
|--------|---------|
| 200 | Success |
| 403 | Forbidden (read-only mode, mutation not allowed) |
| 404 | Resource not found (run.json, CSV, pareto data) |
| 503 | Service unavailable (no output directory configured) |
