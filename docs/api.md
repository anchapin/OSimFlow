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

## Authentication (SEC-001)

Authentication is mandatory for non-local binds. API keys are
transported via the **`X-API-Key` request header only** (issue #268):

```bash
curl -H "X-API-Key: <your-key>" http://localhost:8000/api/v1/campaign
```

> **Important (issue #1466):** the `?api_key=` query parameter is **no
> longer accepted** as a key transport. Query strings are recorded by
> reverse proxies, access logs, browser history, and `Referer` headers,
> which turned bearer-equivalent credentials into durable log artifacts.
> Requests carrying an `api_key` query parameter (with no header) are
> rejected with `401` and a migration hint — pass the `X-API-Key`
> header instead.

Pass `--api-key <key>` for single-key mode, or `--api-keys-file
<file.json>` for multi-user keys with per-user roles (`readonly`,
`readwrite`, `admin`; issue #395). The Python client
(`osimflow.client.OSimFlowClient`) already sends the header.

### Auto-generated ephemeral key (issue #1553, SEC-001 localhost gap)

When `serve` is started without `--api-key` and without
`--api-keys-file`, an ephemeral API key is auto-generated at startup
and printed **once to stderr**:

```text
Generated ephemeral API key for localhost serve: <key> — pass --api-key <key> to pin it on subsequent serves.
```

The auto-gen path fires for **both** read-only and read-write binds
so that a loopback `serve` is never unauthenticated — including on
shared HPC login nodes where every local account can otherwise read
`run.json`, KPI results, and registry listings over
`http://127.0.0.1:8000`. A WARNING log describing the multi-user-host
exposure is emitted at startup. Pass `--api-key <key>` explicitly to
pin a stable key across serves (the auto-generated key is ephemeral
and shown only once).

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

### GET /api/v1/samples/{sid}/logs/{log_name}

Retrieve the raw content of a sample's log file (`stdout.log` or `stderr.log`).

```bash
curl http://localhost:8000/api/v1/samples/sample_000/logs/stdout.log
```

Returns the raw text content of the log file.

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

## Campaign Comparison (issue #404)

### POST /api/v1/campaigns/compare

Compare two or more campaigns side by side by registry ID, campaign
directory name, or explicit outdir path.  Returns per-campaign metadata,
step timing, sample counts and success rates, per-KPI aggregated
statistics, and an aligned KPI comparison table.

Each entry in the request body may specify `campaign_id` (resolved via
the registry or campaigns base directory) **or** `outdir` (a direct
filesystem path).

Campaigns that cannot be found are included with `found: false` and an
`error` message — the endpoint never raises 404 so callers can compare
even when some campaigns are missing.

```bash
# Compare by campaign IDs
curl -X POST http://localhost:8000/api/v1/campaigns/compare \
  -H "Content-Type: application/json" \
  -d '{"campaigns": [{"campaign_id": "campaign-aaa"}, {"campaign_id": "campaign-bbb"}]}'

# Compare by outdir paths
curl -X POST http://localhost:8000/api/v1/campaigns/compare \
  -H "Content-Type: application/json" \
  -d '{"campaigns": [{"outdir": "/path/to/run1"}, {"outdir": "/path/to/run2"}]}'

# Compare 3+ campaigns
curl -X POST http://localhost:8000/api/v1/campaigns/compare \
  -H "Content-Type: application/json" \
  -d '{"campaigns": [{"campaign_id": "a"}, {"campaign_id": "b"}, {"campaign_id": "c"}]}'
```

Example response:

```json
{
  "campaigns": [
    {
      "identifier": "campaign-aaa",
      "found": true,
      "campaign_id": "campaign-aaa",
      "status": "completed",
      "started_at": 1000.0,
      "finished_at": 2000.0,
      "elapsed_s": 1000.0,
      "config": {"executor": "local", "n_samples": 2},
      "step_timing": [{"step": "GENERATE_LHS_SAMPLES", "cache": "MISS", "elapsed_s": 0.5}],
      "sample_summary": {"n_samples": 2, "n_succeeded": 2, "n_failed": 0, "success_rate": 1.0},
      "kpi_stats": {
        "eui": {"mean": 119.35, "min": 118.2, "max": 120.5, "std": 1.15, "count": 2}
      },
      "error": null
    }
  ],
  "kpi_comparison": [
    {"metric": "eui", "values": [119.35, 133.65]}
  ],
  "total": 2
}
```

> **Note:** When the server is started with `--registry`, campaign IDs
> are resolved via the campaign registry database first, then fall back
> to the campaigns base directory.

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

## Coordinator (fire-and-forget `--detach`)

When a campaign is handed off to a remote Coordinator (`osimflow run --detach
--coordinator-url ...`), the CLI exits immediately and the campaign runs on the
Coordinator. The CLI persists a local handoff record (`.coordinator_handoff.json`)
under the outdir so `osimflow status` / `osimflow download` can reconnect to the
remote campaign from a fresh shell or a rebooted machine.

### POST /api/v1/coordinator/handoff  →  `202 Accepted`

Accepts a campaign configuration and returns immediately with a `campaign_id`.
The response carries an absolute `status_url` the CLI persists to the local
handoff record.

**Idempotent** on the `Idempotency-Key` header (issue #630): a duplicate
handoff carrying the same key as a prior, accepted request returns the
*original* `campaign_id` and `status_url` instead of creating a second
campaign. The CLI derives the key deterministically from the campaign config +
outdir, so re-running the same `osimflow run --detach ...` command after a lost
HTTP response safely reuses the campaign the Coordinator already created.
Omitting the header preserves the legacy behaviour (a new campaign each call).

```jsonc
// 202 response
{
  "campaign_id": "3fe851f6-...",
  "status": "pending",
  "message": "Campaign 'my-run' accepted. Use GET https://.../campaigns/3fe851f6-... to poll status.",
  "status_url": "https://coordinator.example.com/api/v1/coordinator/campaigns/3fe851f6-..."
}
```

Returns `403` in read-only mode.

### GET /api/v1/coordinator/campaigns/{campaign_id}

Live status for a handed-off campaign — the URL the CLI stores in
`status_url`. `osimflow status <outdir>` resolves the `campaign_id` from the
local handoff record and calls this.

### GET /api/v1/coordinator/campaigns/{campaign_id}/results

Enumerates result files and, once aggregation is complete, returns a short-lived
`aggregated_results_url` (presigned GET, signed by the Coordinator's IAM role).
`osimflow download <outdir>` fetches **only** the aggregated CSV via that URL —
per-sample bytes are intentionally not downloaded (issue #630).

### POST /api/v1/coordinator/campaigns/{campaign_id}/aggregate

Terminal aggregation step (issue #627, Epic #624). Triggered after
`POST /array-complete` flips the campaign to `aggregating`. Lists every
`{campaign_id}/samples/*/_manifest.json`, reads each referenced `kpis.json`,
and compiles `aggregated_results.csv` (same column contract as
`bin/aggregate_results.py`) plus `failed_simulations.csv` (first
`  * Severe` line per failed manifest — AGENTS.md §8 gotcha #4). When the campaign
algorithm is multi-objective (`nsga2`/`pso`) a Pareto-front JSON is also
written. Artifacts land under `{campaign_id}/_aggregated/` and the campaign
status flips `aggregating → complete`.

```jsonc
// 202 response
{
  "campaign_id": "01J0ABCDEFGH",
  "aggregator_job_id": "01J0ABCDEFGH-aggregator",
  "status": "complete",
  "ok_count": 98,
  "failed_count": 2,
  "total_count": 100,
  "aggregated_results_key": "01J0ABCDEFGH/_aggregated/aggregated_results.csv",
  "failed_simulations_key": "01J0ABCDEFGH/_aggregated/failed_simulations.csv",
  "pareto_front_key": null,
  "message": "Aggregated 100 samples: 98 ok, 2 failed. Artifacts written to 01J0ABCDEFGH/_aggregated/."
}
```

Returns `409` when the campaign is not in the `aggregating` state (already
aggregated, or the array job has not yet been declared complete). An ok
manifest whose `kpis.json` is missing is logged and counted as failed — it
never crashes the aggregation (issue #627 criterion #5).

### Local handoff record (`.coordinator_handoff.json`)

```jsonc
{
  "version": 1,
  "campaign_id": "3fe851f6-...",
  "coordinator_url": "https://coordinator.example.com",
  "submitted_at": 1718240400.0,
  "status_url": "https://coordinator.example.com/api/v1/coordinator/campaigns/3fe851f6-...",
  "idempotency_key": "osimflow-<sha256[:32]>"
}
```

If `osimflow status` / `osimflow download` is run on an outdir with no record,
the error is: *no Coordinator campaign associated with this outdir; did you run
with `--detach`?*.

### Manual-verify checklist (issue #630)

End-to-end checks against a running Coordinator (the unit tests cover the
logic with a stubbed transport; these verify the live UX):

1. **Idempotent handoff** — run `osimflow run --detach --coordinator-url <url>
   ...` twice with identical args; the second invocation prints the *same*
   `campaign_id` (local-record fast path, no second campaign created).
2. **202 + clean exit** — after handoff the CLI prints `campaign_id` +
   `status_url` and exits; `ps` shows no lingering `osimflow` process and
   the `.coordinator_handoff.json` record exists under the outdir.
3. **Reconnect from a fresh shell** — open a new terminal and run
   `osimflow status <outdir>`; it resolves the `campaign_id` from the record and
   prints the Coordinator's live status (no `run.json` needed).
4. **Download aggregated-only** — `osimflow download <outdir>` fetches only
   `aggregated_results.csv` via the presigned URL; no per-sample bytes land in
   the output directory.
5. **Failure: Coordinator unreachable** — with the Coordinator stopped,
   `osimflow run --detach ...` exits 1 with an actionable "could not reach"
   message and writes **no** handoff record.
6. **Failure: 4xx config** — a malformed config returns exit 1 with "No
   campaign was created".
7. **Recovery: 5xx** — a server error returns exit 1 with a message noting the
   `Idempotency-Key` recovery path, and re-running the same command recovers.

## Regenerating `docs/openapi.json`

The committed spec at `docs/openapi.json` is **generated** from the
running FastAPI app — it is not hand-edited. After any change to a
route, request/response schema, or new endpoint under `osimflow/api/`,
regenerate the spec and commit the result in the same PR:

```bash
# 1. Make sure the [api] extra is installed
pip install -e ".[api]"

# 2. Regenerate
python scripts/generate_openapi.py --output docs/openapi.json

# 3. Verify locally (exit 0 = in sync)
python tools/check_openapi_sync.py --summary
```

### CI gate

`.github/workflows/agents-contract.yml` runs `tools/check_openapi_sync.py`
on every PR. If `docs/openapi.json` is stale relative to the live app,
the `agents & docs contract` job fails with the diff and a one-line
hint pointing at the regenerate command above. Volatile keys
(`info.version`, `x-timestamp`, etc.) are stripped before diffing so
the check focuses on schema content. Pass `--strict` to also fail on
volatile-field drift.

