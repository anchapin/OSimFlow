# API Contract — Coordinator & Direct-to-Cloud Result Streaming

> **Scope:** Defines the contracts for issues **#617** (Cloud-Hosted Campaign Manager) and **#618** (Direct-to-Cloud Result Streaming + Async Aggregation).
>
> **Status:** Partially implemented. The Coordinator service skeleton already lives in `osimflow/api/coordinator.py` (functions: `coordinator_handoff`, `submit_campaign_array_job`, `poll_array_job`, `notify_campaign`, `get_coordinator_campaign`, `list_coordinator_campaigns`, `update_coordinator_campaign_status`). Object-storage backends exist in `osimflow/storage.py` (`LocalStorage`, `S3Storage`, `GCSStorage`, `AzureBlobStorage`, `build_result_storage`). This contract documents the **existing** surface and specifies the **gaps** the two issues must close. It is the source of truth for the Epic task decomposition.
>
> **Design principle:** once a campaign is handed off, the submitting client may fully disconnect. Workers write results directly to object storage; the Coordinator detects completion via webhook/poll and triggers a terminal aggregation task. No result traffic flows back through the user's local machine.

---

## 1. Conventions

| Item | Value |
|---|---|
| Base URL | `OSIMFLOW_COORDINATOR_URL` (e.g. `https://coordinator.example.com`) |
| API prefix | `/api/v1` |
| Auth | API key header `X-API-Key` (multi-user via `--api-keys-file`, issue #395); RBAC via `get_user_permission(request, role)` |
| Content type | `application/json` unless body is a multipart upload |
| Idempotency | All mutating endpoints accept `Idempotency-Key` header; retries are safe |
| Error envelope | `{ "detail": "<msg>", "request_id": "<uuid>" }` with standard HTTP codes |
| Campaign ID | Server-generated ULID string on handoff; immutable for the campaign lifetime |

---

## 2. Coordinator Lifecycle API (#617)

### 2.1 Handoff — `POST /api/v1/coordinator/handoff`

**Implemented:** `coordinator_handoff()` in `osimflow/api/coordinator.py`.

Submits a packaged campaign to the Coordinator and returns immediately. The client may exit after a 2xx. Used by the CLI when `--detach --coordinator-url ...` is set.

**Request**
```jsonc
{
  "campaign_name": "ashrae901_wwr_sweep",
  "config": { /* serialized CampaignConfig dataclass */ },
  "template_package_url": "s3://osimflow-uploads/.../template.zip",
  "variables_url": "s3://osimflow-uploads/.../variables.yml",
  "algorithm": "lhs",
  "n_samples": 50000,
  "openstudio_version": "3.11.0"
}
```

**Response** `202 Accepted`
```jsonc
{
  "campaign_id": "01J0ABCDEFGH",
  "status": "accepted",
  "coordinator_url": "https://coordinator.example.com",
  "status_url": "/api/v1/coordinator/campaigns/01J0ABCDEFGH"
}
```

### 2.2 Submit Array Job — `POST /api/v1/coordinator/campaigns/{campaign_id}/submit-array`

**Implemented:** `submit_campaign_array_job(campaign_id, submit_req, request)`.

Replaces N individual `submit_job` calls with one AWS Batch array job (`arrayProperties.size = n_samples`). Each array child reads `AWS_BATCH_JOB_ARRAY_INDEX` and resolves its parameter set via `GET /campaigns/{campaign_id}/samples/{index}`.

**Request**
```jsonc
{
  "job_queue": "osimflow-batch-queue",
  "job_definition": "osimflow-openstudio-job-def",
  "array_size": 50000
}
```

**Response** `202 Accepted`
```jsonc
{
  "campaign_id": "01J0ABCDEFGH",
  "array_job_id": "b1a2c3...",
  "status": "pending",
  "message": "Array job b1a2c3 submitted with 50000 children."
}
```

> **Existing contract guarantees (must be preserved):** the submission passes `containerOverrides.environment` (`OSIMFLOW_CAMPAIGN_ID`, `OSIMFLOW_COORDINATOR_URL`) and a per-campaign `timeout.attemptDurationSeconds`. The `openstudio_version` is resolved into the container image at the **job-definition** level — it is NOT a `submit_job` parameter. This is directly relevant to bug **#622**: no SDK argument may be passed as both positional and keyword.

### 2.3 Sample retrieval — `GET /api/v1/coordinator/campaigns/{campaign_id}/samples/{index}`

Called by each array child to fetch its parameter set. Returns one row of the generated sample manifest.

**Response** `200 OK`
```jsonc
{ "index": 42, "sample_id": "s0042", "parameters": { "wwr": 0.42, "rvalue": 3.1 } }
```

### 2.4 Status & polling

| Endpoint | Implemented | Purpose |
|---|---|---|
| `GET /api/v1/coordinator/campaigns/{id}` | `get_coordinator_campaign` | Single campaign detail incl. `array_job_id`, counts |
| `GET /api/v1/coordinator/campaigns` | `list_coordinator_campaigns` | Filterable list (status, limit) |
| `PATCH /api/v1/coordinator/campaigns/{id}/status` | `update_coordinator_campaign_status` | Internal: workers/array report state transitions |
| `POST /api/v1/coordinator/campaigns/{id}/poll` | `poll_array_job` | Force a Batch `describe_jobs` refresh |

**Campaign status values:** `accepted → submitted → running → aggregating → succeeded | failed | cancelled`

### 2.5 Disconnect resilience (acceptance for #617)

After `POST /handoff` returns `202`, the Coordinator owns the lifecycle. Required invariants:
- Reconnect any time via `GET /campaigns/{id}` → current status + progress counts.
- The user's machine going to sleep/offline MUST NOT abort the remote execution loop.
- `POST /campaigns/{id}/cancel` writes a durable cancel marker the Coordinator honors at the next poll boundary (links to bug **#621** for the local in-process loop).

---

## 3. Direct-to-Cloud Result Streaming (#618)

### 3.1 Object key convention

Workers push per-sample outputs directly to object storage. No data transits the user's local machine.

```
s3://{result_bucket}/{campaign_id}/
├── samples/
│   ├── {sample_id}/kpis.json          # extracted KPIs (always uploaded)
│   ├── {sample_id}/eplusout.sql        # raw sim output (only if --archive_intermediates)
│   └── {sample_id}/_manifest.json      # per-sample completion marker
└── _aggregated/
    ├── aggregated_results.csv          # written by the Aggregator Task
    └── failed_simulations.csv
```

**`_manifest.json` (the completion signal a worker writes last, atomically):**
```jsonc
{
  "campaign_id": "01J0ABCDEFGH",
  "sample_id": "s0042",
  "index": 42,
  "status": "ok | failed",
  "kpis_key": "samples/s0042/kpis.json",
  "exit_code": 0,
  "first_severe_error": null,
  "finished_at": "2026-06-19T12:00:00Z"
}
```

### 3.2 Worker → Coordinator completion report

**Endpoint:** `PATCH /api/v1/coordinator/campaigns/{campaign_id}/status` (implemented: `update_coordinator_campaign_status`).

Each worker, after uploading its `_manifest.json`, reports completion:
```jsonc
{ "sample_id": "s0042", "status": "ok", "manifest_key": "samples/s0042/_manifest.json" }
```

### 3.3 Completion detection

The Coordinator declares an array job 100% complete via EITHER:

- **Webhook (preferred):** AWS EventBridge → `POST /api/v1/coordinator/campaigns/{id}/array-complete` fired on Batch `SUCCEEDED` terminal state. Carries the `array_job_id`; Coordinator verifies child count.
- **Polling fallback:** `poll_array_job()` on a timer (exponential backoff, 5s→60s) until `arrayProperties.size` children report `status=ok|failed`.

### 3.4 Aggregator Task

On completion detection the Coordinator submits ONE terminal aggregator job (a Batch job, not an array child):

```
POST /api/v1/coordinator/campaigns/{id}/aggregate
→ 202 { "aggregator_job_id": "..." }
```

The aggregator:
1. Lists `{bucket}/{campaign_id}/samples/*/_manifest.json`.
2. Reads each `kpis.json`, compiles `aggregated_results.csv`.
3. Extracts the first `  * Severe` line per failed sample into `failed_simulations.csv` (AGENTS.md §8 gotcha #4 — `grep -m 1`).
4. Writes both to `{bucket}/{campaign_id}/_aggregated/`.
5. Optionally computes Pareto front (issue #141) when `--algorithm` is multi-objective.
6. Flips campaign status `aggregating → succeeded`.

### 3.5 Notification — `POST /api/v1/coordinator/campaigns/{id}/notify`

**Implemented:** `notify_campaign()` (currently a stub router). The gap (#618) is wiring real backends.

```jsonc
{
  "campaign_id": "01J0ABCDEFGH",
  "event": "campaign.succeeded",
  "download_url": "https://...presigned.../_aggregated/aggregated_results.csv",
  "expires_in_seconds": 604800
}
```

**Adapter contract** (new, `osimflow/notify.py`): `NotifyBackend` ABC with `send(event, payload) -> None`. Implementations:
- `SNSNotifyBackend` — publishes to `--alert-destinations` SNS ARN.
- `EmailNotifyBackend` — SendGrid/SES; sends the presigned `download_url`.
- `WebhookNotifyBackend` — POSTs JSON to `--webhook-url` (issue #283 already defines the webhook callback shape; reuse it).

The presigned URL lifetime is governed by `--s3-artifact-presigned-url-expiration`.

---

## 4. Cross-cutting acceptance gates

These cut across #617 and #618 and are shared Epic-level acceptance criteria:

- [ ] A 50,000-sample campaign submits with exactly **one** `submit_job` call (array) + one aggregator job = 2 Batch calls total.
- [ ] Killing the submitting CLI after `202 Accepted` does not affect remote execution (verified by a disconnect E2E test).
- [ ] Zero result bytes flow to the submitting host; all outputs land in object storage (assert via network egress check in the E2E test).
- [ ] Bug #622 fix holds: no SDK argument reaches any executor `submit()` as both positional and keyword.
- [ ] Bug #621 fix holds: a mid-flight cancel is honored within the local generation loop AND the dry-run path (`_run_dry_run`).
- [ ] Bug #620 fix holds: cancellation does not raise `FileNotFoundError` on `cache.sqlite-shm`/`-wal` under multi-process teardown.

---

## 5. Out of scope (explicit)

- Real-time streaming of hourly time-series to the client (AGENTS.md §8 gotcha #8 — daily/monthly aggregates only in the CSV; hourly stays in per-sample `.sql`).
- A web UI for campaign authoring (#617 task 2 mentions "web interface or API endpoint" — this contract specifies the **API endpoint** path; a web UI is a separate follow-on).
- Changing the existing `ResultStorage` ABC or `build_result_storage` factory — workers reuse `S3Storage.upload()`.
