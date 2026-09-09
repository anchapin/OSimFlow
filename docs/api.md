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
`readwrite`, `admin`; issue #395). The keys file stores SHA-256
digests (`key_sha256`), never plaintext keys (issue #1552) — see
[Secret Management — Hashed API keys at
rest](secret-management.md#hashed-api-keys-at-rest-issue-1552) for
the file format and the plaintext-migration one-liner. The Python
client (`osimflow.client.OSimFlowClient`) already sends the header.

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

### GET /api/v1/health

Campaign health dashboard (issue #437). Aggregates `run.json` into an
overall status (`healthy` / `degraded` / `unhealthy` / `unknown`) plus
per-step status, sample counts (`total` / `success` / `failed` /
`cached` / `running`), and key timestamps.

```bash
curl http://localhost:8000/api/v1/health
```

```json
{
  "campaign_id": "my-campaign-001",
  "overall_status": "degraded",
  "campaign_status": "running",
  "steps": [
    {"step": "GENERATE_LHS_SAMPLES", "cache": "MISS", "status": "ok", "elapsed_s": 0.5}
  ],
  "samples": {"total": 500, "success": 120, "failed": 3, "cached": 0, "running": 377},
  "started_at": 1718236800.0,
  "finished_at": null,
  "elapsed_s": 3600.0
}
```

### GET /api/v1/health/details

Full `run.json` payload for clients that need per-sample details, error
summaries, worker info, or any field not surfaced in `/api/v1/health`.

```bash
curl http://localhost:8000/api/v1/health/details
```

```json
{"campaign_id": "my-campaign-001", "per_sample": [...], "steps": [...], ...}
```

### GET /

Root redirect (issue #264). Returns `307 Temporary Redirect` to the
bundled web GUI (`osimflow/api/static/index.html`) so the dashboard
opens by default in a browser.

```bash
curl -i http://localhost:8000/
```

```http
HTTP/1.1 307 Temporary Redirect
Location: /static/index.html
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

### POST /api/v1/campaign/pause

Write a `.pause` flag file to request a soft-pause (issues #553/#1537).
In-flight samples complete normally; only new submissions are skipped,
so the campaign can be resumed from where it left off. **Requires
`--read-write` mode.**

```bash
curl -X POST http://localhost:8000/api/v1/campaign/pause
```

```json
{"status": "pausing"}
```

Returns `409` if the campaign has already finished, and `403` in read-only
mode. Use this on a single-outdir serve; for the registry-backed multi-
campaign serve use `POST /api/v1/campaigns/{campaign_id}/pause`.

### DELETE /api/v1/campaign/pause

Resume a paused single-outdir campaign by removing the `.pause` flag.
**Requires `--read-write` mode.**

```bash
curl -X DELETE http://localhost:8000/api/v1/campaign/pause
```

```json
{"status": "resuming"}
```

Returns `409` if the campaign is not currently paused, and `403` in
read-only mode. For the registry-backed variant see
`POST /api/v1/campaigns/{campaign_id}/resume`.

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

### GET /api/v1/campaigns/{campaign_id}/samples

Paginated per-sample results for a specific registry-backed campaign.
This is the multi-campaign equivalent of `GET /api/v1/samples`.

| Query param | Default | Notes |
|-------------|---------|-------|
| `page`      | `1`     | 1-indexed |
| `per_page`  | `50`    | max 500  |

```bash
curl "http://localhost:8000/api/v1/campaigns/campaign-aaa/samples?page=1&per_page=100"
```

```json
{
  "samples": [
    {
      "sample_id": "sample_000",
      "status": "ok",
      "elapsed_s": 10.0,
      "generation": 1,
      "worker_id": "local-0",
      "cost_usd": 0.0
    }
  ],
  "total": 500,
  "page": 1,
  "per_page": 100
}
```

### GET /api/v1/campaigns/{campaign_id}/samples/{sample_id}

Single sample detail — KPIs and log file paths, scoped to a specific
registry-backed campaign. `sample_id` is validated against path traversal.

```bash
curl http://localhost:8000/api/v1/campaigns/campaign-aaa/samples/sample_000
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

### POST /api/v1/campaigns/{campaign_id}/samples/batch_upload

Upload a batch of pre-generated datapoints to a campaign. The JSON body
takes a `samples` array; each entry must include `values` (variable name
→ value) and may include `kpi_values` and `generation`. Each new sample
gets a fresh `sample_id` (`sample_0001`, `sample_0002`, …) and is appended
to `run.json.per_sample`. **Requires `--read-write` mode.**

```bash
curl -X POST http://localhost:8000/api/v1/campaigns/campaign-aaa/samples/batch_upload \
  -H "Content-Type: application/json" \
  -d '{
    "samples": [
      {"values": {"wwr": 0.3, "insulation_r": 30}, "kpi_values": {"eui": 100.0}},
      {"values": {"wwr": 0.5, "insulation_r": 20}, "kpi_values": {"eui": 130.0}}
    ]
  }'
```

```json
{
  "campaign_id": "campaign-aaa",
  "new_sample_ids": ["sample_0501", "sample_0502"],
  "total_samples": 502
}
```

### POST /api/v1/campaigns/{campaign_id}/samples/{sample_id}/requeue

Mark a `COMPLETED` or `FAILED` sample for re-analysis. The endpoint
creates a new pending sample derived from the source's parameters and
appends it to `run.json.per_sample`. **Requires `--read-write` mode.**

```bash
curl -X POST http://localhost:8000/api/v1/campaigns/campaign-aaa/samples/sample_001/requeue
```

```json
{
  "campaign_id": "campaign-aaa",
  "original_sample_id": "sample_001",
  "new_sample_id": "sample_001_reanalyze_1",
  "status": "pending",
  "detail": "Created reanalysis sample 'sample_001_reanalyze_1' derived from 'sample_001'"
}
```

Returns `422` if the source sample is not in a requeueable state.

### GET /api/v1/campaigns/{campaign_id}/samples/{sample_id}/results/{filename}

Download a single per-sample result file
(`{campaign_outdir}/work/sim/{sample_id}/{filename}`). `filename` is
validated against path traversal; subpaths are allowed so `osw/...` and
similar nested artifacts resolve correctly. Returns `404` when the
campaign, sample, or file is not found.

```bash
curl -OJ http://localhost:8000/api/v1/campaigns/campaign-aaa/samples/sample_000/results/eplusout.sql
curl -OJ http://localhost:8000/api/v1/campaigns/campaign-aaa/samples/sample_000/results/osw/workflow.osw
```

### DELETE /api/v1/campaigns/{campaign_id}/samples/{sample_id}/results/{filename}

Delete a single per-sample result file. **Requires `--read-write` mode.**
Returns `204` on success.

```bash
curl -X DELETE http://localhost:8000/api/v1/campaigns/campaign-aaa/samples/sample_000/results/eplusout.sql
```

```http
HTTP/1.1 204 No Content
```

### GET /api/v1/campaigns/{campaign_id}/samples/{sample_id}/timeseries

Time-series data for a single variable from a sample's `eplusout.sql`.
Each per-sample SQL can be 5–200+ MB — retrieve only the variables you
need.

| Query param | Required | Notes |
|-------------|----------|-------|
| `variable`  | yes      | e.g. `Zone Air Temperature` |
| `freq`      | no       | `hourly` (default), `daily`, `monthly` |

```bash
curl "http://localhost:8000/api/v1/campaigns/campaign-aaa/samples/sample_000/timeseries?variable=Zone%20Air%20Temperature&freq=daily"
```

```json
{
  "variable": "Zone Air Temperature",
  "frequency": "daily",
  "units": "C",
  "n_points": 365,
  "data": [
    {"timestamp": "2024-01-01 00:00:00", "value": 21.2, "units": "C", "key": "Zone 1"}
  ]
}
```

### GET /api/v1/campaigns/{campaign_id}/samples/{sample_id}/timeseries/variables

List every available time-series variable in a sample's `eplusout.sql`
so you can pick a valid `variable` value before calling
`/timeseries`.

```bash
curl http://localhost:8000/api/v1/campaigns/campaign-aaa/samples/sample_000/timeseries/variables
```

```json
{
  "variables": [
    {"VariableName": "Zone Air Temperature", "KeyName": "Zone 1", "Units": "C"},
    {"VariableName": "Zone Electric Equipment Electricity Rate", "KeyName": "Zone 1", "Units": "W"}
  ],
  "total": 42
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

### GET /api/v1/campaigns/{campaign_id}/results/query

Query aggregated results for a specific campaign with server-side
filtering and pagination (issue #585). Reads `aggregated_results.csv`
from the campaign outdir.

| Query param | Default | Notes |
|-------------|---------|-------|
| `page`      | `1`     | 1-indexed |
| `per_page`  | `50`    | max 1000 |
| `status`    | —       | filter by sample status (`ok` / `failed` / `running`) |
| `filter`    | —       | MongoDB-style JSON filter, e.g. `{"kpi.eui": {"$gt": 100}}` |

```bash
# Page 1 of "ok" rows
curl "http://localhost:8000/api/v1/campaigns/campaign-aaa/results/query?status=ok&page=1&per_page=50"

# KPI filter via JSON
curl "http://localhost:8000/api/v1/campaigns/campaign-aaa/results/query?filter=%7B%22kpi.eui%22%3A%7B%22%24gt%22%3A100%7D%7D"
```

```json
{
  "rows": [
    {"sample_id": "sample_000", "status": "ok", "kpi": {"eui": 120.5}}
  ],
  "total": 498,
  "page": 1,
  "per_page": 50,
  "campaign_id": "campaign-aaa"
}
```

Supported operators: `$eq`, `$ne`, `$gt`, `$gte`, `$lt`, `$lte`, `$in`,
`$nin`, `$exists`. Returns `400` on malformed `filter` JSON.

### GET /api/v1/campaigns/{campaign_id}/results/export

Export aggregated results as CSV (default) or JSON for a specific
campaign. Returns the full result set without pagination — for the
paged view use `/results/query`.

| Query param | Default | Notes |
|-------------|---------|-------|
| `format`          | `csv`  | `csv` or `json` |
| `status`          | —      | filter by sample status |
| `include_failed`  | `true` | include failed simulations |

```bash
# CSV download (default)
curl -OJ "http://localhost:8000/api/v1/campaigns/campaign-aaa/results/export?format=csv"

# JSON, ok only
curl "http://localhost:8000/api/v1/campaigns/campaign-aaa/results/export?format=json&status=ok"
```

```http
Content-Type: text/csv
Content-Disposition: attachment; filename=campaign-aaa-results.csv
```

### GET /api/v1/errors/{sample_id}

Detailed error diagnosis for a failed sample — categorises the failure,
returns a root-cause line, counts severe errors, and emits an actionable
suggestion. Reads `eplusout.err` from the sample's sim directory.

```bash
curl http://localhost:8000/api/v1/errors/sample_001
```

```json
{
  "sample_id": "sample_001",
  "error_summary": "Severe Error in HVAC sizing — coil capacity too small",
  "failure_category": "hvac_sizing",
  "root_cause_line": "  ** Severe  ** Coil:Cooling:Water ... capacity is below minimum",
  "total_severe_errors": 12,
  "diagnosis_suggestion": "Increase the cooling coil capacity in your measure arguments",
  "severity": "high",
  "log_path": "/path/to/results/work/sim/sample_001/eplusout.err"
}
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

## Campaigns (registry)

When the server is started with `--registry <dir>`, every campaign in
that base directory becomes addressable via the
`/api/v1/campaigns/{campaign_id}/*` family. These endpoints resolve the
campaign from the registry, then dispatch to the on-disk artifacts
(`run.json`, `samples/`, `aggregated_results.csv`, etc.).

### GET /api/v1/campaigns

List every campaign under the configured base directory by scanning for
`run.json` files. Returns a summary per campaign — id, derived status,
sample counts, and timestamps.

```bash
curl http://localhost:8000/api/v1/campaigns
```

```json
{
  "campaigns": [
    {
      "campaign_id": "campaign-aaa",
      "status": "completed",
      "started_at": 1718236800.0,
      "finished_at": 1718240400.0,
      "n_samples": 500,
      "n_succeeded": 498,
      "n_failed": 2
    }
  ],
  "total": 1
}
```

### POST /api/v1/campaigns

Create a new campaign directory and optionally launch it in the
background. Requires **admin** permission. The body follows
`CampaignCreateRequest` — pass `outdir`, `variables_path`,
`template_sim_package`, `n_samples`, `openstudio_version`, and set
`auto_start=true` to kick off the run.

```bash
curl -X POST http://localhost:8000/api/v1/campaigns \
  -H "Content-Type: application/json" \
  -d '{
    "outdir": "campaign-zzz",
    "variables_path": "variables.yml",
    "template_sim_package": "example_package",
    "n_samples": 100,
    "openstudio_version": "3.11.0",
    "auto_start": true
  }'
```

```json
{
  "campaign_id": "campaign-1a2b3c4d",
  "outdir": "/srv/osimflow/campaigns/campaign-zzz",
  "status": "running"
}
```

Returns `403` when the caller lacks the admin role.

### GET /api/v1/campaigns/{campaign_id}

Detailed status for a single campaign — derived status, step timing,
sample summary, cost totals, baseline comparison, and quality summary
(when present in `run.json`).

```bash
curl http://localhost:8000/api/v1/campaigns/campaign-aaa
```

```json
{
  "campaign_id": "campaign-aaa",
  "status": "completed",
  "started_at": 1718236800.0,
  "finished_at": 1718240400.0,
  "elapsed_s": 3600.0,
  "config": {"executor": "aws_batch", "n_samples": 500},
  "summary": {"n_samples": 500, "n_succeeded": 498, "n_failed": 2},
  "total_cost_usd": 12.34,
  "spot_savings_usd": 5.67,
  "steps": [...]
}
```

### POST /api/v1/campaigns/{campaign_id}/cancel

Cancel a running campaign by writing a `.stop` flag file. **Requires
`--read-write` mode.** Returns `409` if the campaign has already
finished, and an audit log entry is written under the campaign outdir.

```bash
curl -X POST http://localhost:8000/api/v1/campaigns/campaign-aaa/cancel
```

```json
{"campaign_id": "campaign-aaa", "status": "stopping"}
```

### POST /api/v1/campaigns/{campaign_id}/pause

Soft-pause a running campaign (issues #553/#1537). Running samples
finish normally; only new submissions are skipped, and the campaign can
later be resumed from where it left off. **Requires `--read-write` mode.**

```bash
curl -X POST http://localhost:8000/api/v1/campaigns/campaign-aaa/pause
```

```json
{"campaign_id": "campaign-aaa", "status": "paused"}
```

### POST /api/v1/campaigns/{campaign_id}/resume

Resume a paused campaign by removing the `.pause` flag and flipping
`run.json.status` back to `running`. **Requires `--read-write` mode.**

```bash
curl -X POST http://localhost:8000/api/v1/campaigns/campaign-aaa/resume
```

```json
{"campaign_id": "campaign-aaa", "status": "running"}
```

Returns `409` if the campaign is not currently paused.

### GET /api/v1/campaigns/{campaign_id}/download

Download a bundled ZIP archive of every campaign artifact — `run.json`,
`samples.json`, `aggregated_results.csv`, `failed_simulations.csv`,
per-sample KPI JSONs, and PNG plots. Set `?include_sql=true` to also
include the per-sample `eplusout.sql` files (only present when the
campaign was launched with `--archive_intermediates`).

```bash
# Default bundle (~MBs)
curl -OJ http://localhost:8000/api/v1/campaigns/campaign-aaa/download

# Include eplusout.sql files (can be GB)
curl -OJ "http://localhost:8000/api/v1/campaigns/campaign-aaa/download?include_sql=true"
```

```http
HTTP/1.1 200 OK
Content-Type: application/zip
Content-Disposition: attachment; filename="campaign-campaign-aaa.zip"
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

## Variables

CRUD endpoints for the campaign's `variables.yml`. Used by the
`--editor` UI in the `[api]` server.

### GET /api/v1/variables

List every variable defined in the campaign's `variables.yml`.

```bash
curl http://localhost:8000/api/v1/variables
```

```json
{
  "variables": [
    {"name": "wwr", "distribution": "uniform", "description": "Window-to-wall ratio"},
    {"name": "insulation_r", "distribution": "normal", "description": null}
  ],
  "total": 2
}
```

### GET /api/v1/variables/{var_name}

Full detail for one variable — distribution plus distribution-specific
parameters (`min` / `max` / `mean` / `sigma` / `mode` / `values` /
`alpha` / `beta` / `rate` / `target` / `mapping`).

```bash
curl http://localhost:8000/api/v1/variables/wwr
```

```json
{
  "name": "wwr",
  "distribution": "uniform",
  "description": "Window-to-wall ratio",
  "min": 0.2,
  "max": 0.6,
  "mean": null,
  "sigma": null,
  "mode": null,
  "values": null,
  "alpha": null,
  "beta": null,
  "rate": null,
  "target": null,
  "mapping": null
}
```

Returns `404` if the variable is not found.

### POST /api/v1/variables

Create a new variable. **Requires `--read-write` mode.** The body is a
free-form variable object validated against the same schema as
`variables.yml`. Returns `409` on duplicate name.

```bash
curl -X POST http://localhost:8000/api/v1/variables \
  -H "Content-Type: application/json" \
  -d '{
    "name": "wwr",
    "distribution": "uniform",
    "min": 0.2,
    "max": 0.6
  }'
```

```json
{"name": "wwr", "distribution": "uniform", "min": 0.2, "max": 0.6, ...}
```

Returns `201` on success; `400` for unknown distribution; `409` on
duplicate name.

### PUT /api/v1/variables/{var_name}

Update an existing variable. **Requires `--read-write` mode.** Validates
the merged result before writing. Returns `404` if the variable is not
found, `409` if a rename collides with another variable.

```bash
curl -X PUT http://localhost:8000/api/v1/variables/wwr \
  -H "Content-Type: application/json" \
  -d '{"min": 0.25, "max": 0.55}'
```

### DELETE /api/v1/variables/{var_name}

Delete a variable from `variables.yml`. **Requires `--read-write` mode.**

```bash
curl -X DELETE http://localhost:8000/api/v1/variables/wwr
```

```json
{"name": "wwr", "deleted": true}
```

### POST /api/v1/variables/batch_update

Atomically update multiple variables in a single request. All variables
are validated up-front; if any update is fatal (unknown distribution,
missing required parameters), the entire batch is rejected with `400`
and an `errors` list. Renames that collide are reported per-item in
`errors` but other items in the batch still proceed.

```bash
curl -X POST http://localhost:8000/api/v1/variables/batch_update \
  -H "Content-Type: application/json" \
  -d '{
    "variables": [
      {"name": "wwr", "min": 0.25, "max": 0.55},
      {"name": "insulation_r", "mean": 30, "sigma": 5}
    ]
  }'
```

```json
{
  "updated": ["wwr", "insulation_r"],
  "errors": [],
  "total_updated": 2
}
```

Invalidates the LHS cache so a follow-up campaign run picks up the new
distributions.

## Files

Generic file upload/download/delete endpoints (issue #273). Files live
under `{uploads_dir}/{category}/{filename}` with metadata persisted to
`files_index.json`. Max 100 MB per file.

### GET /api/v1/files

List uploaded files, optionally filtered by category.

| Query param | Notes |
|-------------|-------|
| `category`  | e.g. `template`, `weather`, `measure` |

```bash
curl "http://localhost:8000/api/v1/files?category=weather"
```

```json
{
  "files": [
    {"file_id": "abc123", "filename": "USA_CA_SanFrancisco.epw", "category": "weather", "size_bytes": 1572864, "path": "weather/USA_CA_SanFrancisco.epw"}
  ],
  "total": 1
}
```

### POST /api/v1/files/upload

Multipart file upload. **Requires `--read-write` mode.** Pass the file
under the `file` form field and an optional `category` query param.

```bash
curl -X POST "http://localhost:8000/api/v1/files/upload?category=weather" \
  -F "file=@USA_CA_SanFrancisco.epw"
```

```json
{"file_id": "abc123", "filename": "USA_CA_SanFrancisco.epw", "category": "weather", "size_bytes": 1572864}
```

Returns `201` on success.

### GET /api/v1/files/{file_id}

Download an uploaded file by ID. Supports HTTP `Range` requests for
large files (returns `206 Partial Content` with `Content-Range`).

```bash
curl -OJ http://localhost:8000/api/v1/files/abc123

# Range request — first 1 MB
curl -H "Range: bytes=0-1048575" http://localhost:8000/api/v1/files/abc123 -OJ
```

### DELETE /api/v1/files/{file_id}

Delete an uploaded file by ID. **Requires `--read-write` mode.** The
underlying file is removed from disk and the index entry is dropped.

```bash
curl -X DELETE http://localhost:8000/api/v1/files/abc123
```

```json
{"file_id": "abc123", "deleted": true}
```

## Measures

Measure introspection + upload (issue #1626). Workflow-discovered
measures come from the campaign's `workflow.osw`; uploaded measures
live under the served `outdir/.measures/` directory and are addressed
by their content-based `version_uuid` (returned by
`POST /api/v1/measures/upload`).

### GET /api/v1/measures

List every measure referenced in `workflow.osw` plus any uploaded
measures. Filterable by `search`, `taxonomy`, and `tag`.

| Query param | Notes |
|-------------|-------|
| `search`   | substring match on name and description |
| `taxonomy` | prefix match on taxonomy (uploaded measures only) |
| `tag`      | exact tag match (uploaded measures only) |

```bash
curl "http://localhost:8000/api/v1/measures?search=insulation"
```

```json
{
  "measures": [
    {"measure_dir_name": "increase_insulation_r", "display_name": "Increase Insulation R", "description": "...", "measure_type": "Model", "arguments": [...]}
  ],
  "total": 1,
  "source": "workflow.osw+uploaded"
}
```

### GET /api/v1/measures/{measure_name}

Detail for a single workflow-discovered measure, including its
arguments (introspected from `measure.rb` / `measure.py`). The
`measure_name` is the `measure_dir_name` from `workflow.osw`.

```bash
curl http://localhost:8000/api/v1/measures/increase_insulation_r
```

```json
{
  "measure_dir_name": "increase_insulation_r",
  "display_name": "Increase Insulation R",
  "description": "Increase roof insulation R-value",
  "measure_type": "Model",
  "arguments": [
    {"name": "r_value", "type": "Double", "required": true, "default": 30}
  ]
}
```

Returns `404` when not in the workflow. For uploaded measures use
`/api/v1/measures/by-id/{measure_id}` instead.

### POST /api/v1/measures/upload

Upload a measure bundle (`.zip` or `.tar.gz`) containing
`measure.rb` or `measure.py`. **Requires `--read-write` mode** —
installs executable code, so a viewer-role key must not be able to do
it (parity with `/api/v1/files/upload`).

```bash
curl -X POST http://localhost:8000/api/v1/measures/upload \
  -F "file=@my_measure.zip"
```

```json
{"name": "my_measure", "measure_id": "01J0ABCDEFGH", "version_uuid": "...", "taxonomy": "...", "arguments": [...]}
```

### GET /api/v1/measures/by-id/{measure_id}

Full metadata for an uploaded measure by its content-based UUID.
Returns `404` for workflow-discovered measures — use
`/api/v1/measures/{name}` for those.

```bash
curl http://localhost:8000/api/v1/measures/by-id/01J0ABCDEFGH
```

### PATCH /api/v1/measures/by-id/{measure_id}

Update an uploaded measure's metadata (`taxonomy`, `description`,
`tags`, `measure_group`). **Requires `--read-write` mode.**

```bash
curl -X PATCH http://localhost:8000/api/v1/measures/by-id/01J0ABCDEFGH \
  -H "Content-Type: application/json" \
  -d '{"description": "Updated description", "tags": ["envelope", "roof"]}'
```

### DELETE /api/v1/measures/by-id/{measure_id}

Delete an uploaded measure (the on-disk directory and registry entry).
**Requires `--read-write` mode.**

```bash
curl -X DELETE http://localhost:8000/api/v1/measures/by-id/01J0ABCDEFGH
```

```json
{"measure_id": "01J0ABCDEFGH", "deleted": true}
```

## PAT Compatibility

OpenStudio PAT (Parametric Analysis Tool) compatible endpoints
(issue #1015). Each PAT analysis maps 1:1 to an OSimFlow campaign —
importing either a `.osa` file or an inline analysis JSON, and writing
the equivalent `variables.yml` plus a campaign config stub.

### POST /api/v1/pat/analyses

Create a PAT-style analysis. Pass either `osa_path` (a `.osa` archive
already on disk inside the campaigns base directory) or inline `analysis`
JSON. **Requires `--read-write` mode.**

```bash
# From an existing OSA file
curl -X POST http://localhost:8000/api/v1/pat/analyses \
  -H "Content-Type: application/json" \
  -d '{
    "osa_path": "imports/my_pat.osa",
    "template_sim_package": "example_package",
    "n_samples": 100,
    "openstudio_version": "3.11.0",
    "auto_start": true
  }'
```

```json
{"analysis_id": "pat-1a2b3c4d", "status": "running", "osimflow_campaign_id": "pat-1a2b3c4d", "outdir": "..."}
```

Returns `422` if neither `osa_path` nor `analysis` is supplied. Path
containment (issue #1669) ensures every client-supplied host path stays
inside the campaigns base directory.

### GET /api/v1/pat/analyses/{analysis_id}/status

PAT-style status — maps the underlying campaign's `run.json` status
onto the PAT vocabulary (`not started`, `running`, `complete`, `failed`,
`cancelled`) and reports data-point counts.

```bash
curl http://localhost:8000/api/v1/pat/analyses/pat-1a2b3c4d/status
```

```json
{
  "analysis_id": "pat-1a2b3c4d",
  "status": "running",
  "started_at": 1718236800.0,
  "finished_at": null,
  "elapsed_s": 42.5,
  "data_points": {"total": 100, "completed": 23, "failed": 0, "running": 77, "pending": 0}
}
```

### GET /api/v1/pat/analyses/{analysis_id}/data_points

List every data point (sample) for a PAT-style analysis, including KPI
results when present and `error_summary` for failures.

```bash
curl http://localhost:8000/api/v1/pat/analyses/pat-1a2b3c4d/data_points
```

```json
{
  "analysis_id": "pat-1a2b3c4d",
  "data_points": [
    {"data_point_id": "sample_000", "status": "ok", "elapsed_s": 10.0, "results": {"eui": 100.0}},
    {"data_point_id": "sample_001", "status": "failed", "elapsed_s": 5.0, "error_summary": "Severe Error in geometry"}
  ],
  "total": 100
}
```

## Plots

### GET /api/v1/plots

List every PNG plot under the campaign outdir (both the root and the
`plots/` subdirectory, deduplicated with the subdirectory winning on
collision).

```bash
curl http://localhost:8000/api/v1/plots
```

```json
{
  "plots": [
    {"name": "kde_eui.png", "size": 52341},
    {"name": "scatter_eui_vs_cost.png", "size": 48820}
  ],
  "total": 2
}
```

Returns `503` if no outdir is configured.

### GET /api/v1/plots/{filename}

Serve a single PNG plot file. `filename` is sanitised against path
traversal. Returns `404` if the plot is not on disk.

```bash
curl -OJ http://localhost:8000/api/v1/plots/kde_eui.png
```

## Validation

### POST /api/v1/validate

Pre-flight configuration validation (issue #398) — checks the supplied
config fields without running a campaign. Returns the same shape as
the CLI's pre-flight pass.

```bash
curl -X POST http://localhost:8000/api/v1/validate \
  -H "Content-Type: application/json" \
  -d '{
    "input_variables": "variables.yml",
    "template_sim_package": "example_package",
    "n_samples": 500,
    "openstudio_version": "3.11.0"
  }'
```

```json
{"valid": true, "errors": [], "warnings": ["n_samples=500 may exceed free-tier Batch quota"]}
```

Checks include: `variables.yml` schema, `template_sim_package`
structure, OpenStudio version format, sample/generation sanity, and
any custom script paths. Returns `422` for invalid request bodies.

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
`POST /api/v1/coordinator/campaigns/{campaign_id}/array-complete` flips the
campaign to `aggregating`. Lists every
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

### GET /api/v1/coordinator/campaigns

List every campaign currently tracked by the Coordinator.

```bash
curl http://localhost:8000/api/v1/coordinator/campaigns
```

```json
[
  {
    "campaign_id": "01J0ABCDEFGH",
    "name": "office-parametric-1",
    "status": "aggregating",
    "created_at": 1718236800.0,
    "updated_at": 1718240400.0,
    "n_samples": 100,
    "executor": "aws_batch",
    "openstudio_version": "3.11.0"
  }
]
```

### GET /api/v1/coordinator/campaigns/{campaign_id}/samples

List every sample parameter set stored on the Coordinator for a campaign.
Used by the Coordinator before submitting an array job, and by array
children enumerating the available sample indices.

```bash
curl http://localhost:8000/api/v1/coordinator/campaigns/01J0ABCDEFGH/samples
```

```json
{
  "campaign_id": "01J0ABCDEFGH",
  "samples": [
    {"index": 0, "parameters": {"wwr": 0.3, "insulation_r": 30}, "status": "pending"}
  ]
}
```

### GET /api/v1/coordinator/campaigns/{campaign_id}/samples/{index}

Get the parameter set for a specific sample by its zero-based index.
Array-job children read `AWS_BATCH_JOB_ARRAY_INDEX` from their environment
and call this to retrieve their assigned parameters. Returns `404` when
the index is out of range.

```bash
curl http://localhost:8000/api/v1/coordinator/campaigns/01J0ABCDEFGH/samples/42
```

```json
{"index": 42, "parameters": {"wwr": 0.45, "insulation_r": 25}, "status": "completed"}
```

### POST /api/v1/coordinator/campaigns/{campaign_id}/submit-array

Submit a campaign as a single AWS Batch array job. Each array child
sets `AWS_BATCH_JOB_ARRAY_INDEX` and fetches its parameters via
`GET /samples/{index}`. This replaces N individual `submit_job` calls
with one — satisfying the Phase 3 acceptance criterion
(one submission API call for a 50,000-run campaign). Requires **admin**
permission and AWS credentials with `batch.submit-job`.

```bash
curl -X POST http://localhost:8000/api/v1/coordinator/campaigns/01J0ABCDEFGH/submit-array \
  -H "Content-Type: application/json" \
  -d '{
    "job_queue": "osimflow-batch-queue",
    "job_definition": "osimflow-openstudio-job-def",
    "array_size": 100
  }'
```

```json
{"campaign_id": "01J0ABCDEFGH", "array_job_id": "a1b2c3d4-...", "array_size": 100}
```

### POST /api/v1/coordinator/campaigns/{campaign_id}/array-complete

Target of an EventBridge rule firing on a Batch array-job state change.
Authenticates the webhook via the `X-OSimFLOW-Webhook-Secret` header,
re-queries `describe_jobs` for the array parent stored on the campaign,
and only flips `running → aggregating` when every child is in a terminal
state (SUCCEEDED or FAILED). Idempotent with `/poll-array`: whichever
fires first transitions the campaign; the other is a `200` no-op.

```bash
curl -X POST http://localhost:8000/api/v1/coordinator/campaigns/01J0ABCDEFGH/array-complete \
  -H "X-OSimFLOW-Webhook-Secret: <shared-secret>" \
  -H "Content-Type: application/json" \
  -d '{
    "source": "aws.batch",
    "detail-type": "Batch Job State Change",
    "detail": {"jobId": "a1b2c3d4-...", "status": "SUCCEEDED"}
  }'
```

```json
{"campaign_id": "01J0ABCDEFGH", "transitioned": true, "status": "aggregating", "succeeded": 98, "failed": 2, "total": 100}
```

Fails **closed** (401) when the server's webhook secret is unset — never
accept unauthenticated state transitions in production. Returns `409`
when the event's `jobId` does not match the array parent stored on the
campaign.

### GET /api/v1/coordinator/campaigns/{campaign_id}/poll-array

Poll AWS Batch for the status of a previously-submitted array job.
Returns per-child counts (succeeded / failed / pending). Shares the exact
transition logic with `/array-complete`, so the two paths are
idempotent. Requires **admin** permission.

```bash
curl http://localhost:8000/api/v1/coordinator/campaigns/01J0ABCDEFGH/poll-array
```

```json
{
  "campaign_id": "01J0ABCDEFGH",
  "array_job_id": "a1b2c3d4-...",
  "status": "complete",
  "succeeded": 98,
  "failed": 2,
  "pending": 0,
  "total": 100,
  "result_bucket": "osimflow-results",
  "message": "Array job a1b2c3d4-...: 98 succeeded, 2 failed, 0 pending of 100 total."
}
```

A job is *complete* (not *succeeded*) once `succeeded + failed ==
array_size` — partial failures still advance the campaign to
`aggregating`; the split is recorded for the aggregator.

### POST /api/v1/coordinator/campaigns/{campaign_id}/notify

Trigger a completion notification via the configured backend (issue #628):
`sns`, `email`, or `webhook`. Builds a `campaign.succeeded` payload with a
short-lived presigned `download_url` for the aggregated CSV (lifetime
mirrors `--s3-artifact-presigned-url-expiration`). Requires **admin**
permission; backend errors are logged with `exc_info=True` and never
propagate (best-effort).

```bash
curl -X POST http://localhost:8000/api/v1/coordinator/campaigns/01J0ABCDEFGH/notify \
  -H "Content-Type: application/json" \
  -d '{
    "notification_type": "sns",
    "subject": "Run complete",
    "expires_in_seconds": 3600
  }'
```

```json
{"campaign_id": "01J0ABCDEFGH", "notification_type": "sns", "delivered": true}
```

### PATCH /api/v1/coordinator/campaigns/{campaign_id}/status

Internal status transition (Phase 3/4 worker self-reporting). Requires
**admin** permission; not exposed to the public API without
authentication. Pass the new status as a `?status=` query param.

```bash
curl -X PATCH "http://localhost:8000/api/v1/coordinator/campaigns/01J0ABCDEFGH/status?status=running"
```

```json
{"campaign_id": "01J0ABCDEFGH", "status": "running", "updated_at": 1718240400.0}
```

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

