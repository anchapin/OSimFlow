# Production Operations & Reliability Gap Analysis

**Phase:** 1 — Baseline Comparison
**Author:** ops-reliability-analyst
**Date:** 2026-06-13
**Baseline:** openstudio-server (v3.x, Rails monolith + MongoDB + Redis + Sidekiq)
**Target:** OSimFlow (v0.x, Python CLI + library hybrid)

---

## Executive Summary

This analysis compares OSimFlow's production operations and reliability capabilities against openstudio-server. **OSimFlow has made significant progress** since the original baseline comparison — issues #252 (retry), #268 (API auth), #269 (BYOS isolation), #335 (distributed queue), #338 (elastic scaling), #341 (heartbeat), and #343 (resource limits) have closed many of the originally identified gaps.

**Remaining gaps are concentrated in three areas:**
1. **Observability maturity** — OSimFlow has the backends but lacks alerting, dashboards, and distributed tracing
2. **Disaster recovery** — no backup strategy for SQLite stores, no campaign state export/import
3. **Multi-user operational controls** — no RBAC, no audit logging, no multi-tenant resource quotas

**Severity distribution:** 2× P0, 5× P1, 4× P2, 2× P3

---

## Comparison Matrix

| Dimension | openstudio-server | OSimFlow (current) | Gap |
|---|---|---|---|
| **Health Monitoring** | `/up` endpoint, Sidekiq Web UI, MongoDB health checks | `/health` endpoint (read-only), run.json artifacts | MODERATE |
| **Alerting** | Built-in email + webhook notifications per-project | Pluggable backends (CloudWatch/Prometheus/OTel) — no alert rules | MODERATE |
| **Retry/Resilience** | Sidekiq retry with exponential backoff, dead-letter queue | Per-sample retry with exponential backoff, transient error detection, Dask dead-letter queue | MINOR |
| **Observability** | Background job logs, project event logs | Structured JSON logging, tqdm progress, RunTrace/StepTrace/SampleTrace, pluggable metrics backends | MINOR |
| **Security** | Devise auth, project-level ACL, API tokens | API key auth (constant-time), CORS, rate limiting, BYOS subprocess isolation, IAM roles | MINOR |
| **Resource Management** | None (Sidekiq workers are fixed) | Per-step resource directives, BYOS CPU/memory limits, DaskJobQueue elastic scaling | OSIMFLOW AHEAD |
| **Disaster Recovery** | MongoDB backup/restore, replica sets | SQLite WAL mode — no backup tooling, no export/import | SIGNIFICANT |
| **Multi-user Controls** | Project ACL, user roles, API tokens per-user | Single API key, no RBAC, no audit logging | SIGNIFICANT |
| **Distributed Tracing** | None | None | PARITY (both lack) |
| **Campaign State** | Durable in MongoDB | SQLite cache + filesystem job queue + run.json | MODERATE |

---

## Gap Registry

### OPS-001: No Campaign Health Dashboard

- **Severity:** P0
- **openstudio-server equivalent:** Sidekiq Web UI + `/up` endpoint + per-project dashboard
- **Description:** OSimFlow's `/health` endpoint returns basic liveness. There is no aggregated view showing active campaigns, sample progress, failed samples, resource utilization, or queue depth across running campaigns.
- **Impact:** Operators cannot assess system health at a glance. For HPC/cloud deployments with multiple concurrent campaigns, this creates blind spots that delay incident response.
- **Recommended Solution:** Extend the REST API with `/api/v1/dashboard` aggregating: (1) active campaign count + status, (2) total samples in progress/failed/completed, (3) executor utilization (workers active vs idle), (4) cache hit/miss ratio. Use run.json data already collected.
- **Effort:** M (2-3 weeks)
- **Priority:** P1

### OPS-002: No Alerting Rules or Notification Delivery

- **Severity:** P0
- **openstudio-server equivalent:** Per-project email/webhook notifications on simulation failure
- **Description:** OSimFlow has `--webhook-url` for campaign completion callbacks (issue #283) and pluggable observability backends, but there are no configurable alert rules (e.g., "alert if >5% samples fail", "alert if simulation exceeds 2× expected time", "alert if disk usage >80%").
- **Impact:** Operators must actively poll run.json or monitoring dashboards to detect problems. Failed campaigns can run for hours before manual detection.
- **Recommended Solution:** Add an `AlertRule` configuration layer that evaluates conditions against run.json metrics and SampleTrace data. Support webhook and observability-backend delivery channels. Define built-in rules: failure_rate_threshold, sample_timeout, disk_usage, campaign_stalled.
- **Effort:** L (3-4 weeks)
- **Priority:** P1

### OPS-003: No Audit Logging

- **Severity:** P1
- **openstudio-server equivalent:** Rails ActiveRecord audit trail, per-project event log
- **Description:** OSimFlow logs operational events to structured JSON files but has no append-only audit log for security-relevant actions: who started/stopped a campaign, who changed API keys, who accessed which campaign data.
- **Impact:** In multi-user deployments, there is no accountability trail for compliance or incident forensics.
- **Recommended Solution:** Add an `AuditLog` class that writes append-only JSONL records to `${outdir}/audit.jsonl`. Record: timestamp, actor (API key hash or CLI user), action (campaign.start, campaign.stop, api.key.rotate), resource (campaign_id), outcome. Integrate with `osimflow serve` endpoints.
- **Effort:** M (2 weeks)
- **Priority:** P2

### OPS-004: No SQLite Backup or Export/Import

- **Severity:** P1
- **openstudio-server equivalent:** MongoDB backup/restore with replica sets
- **Description:** OSimFlow stores campaign metadata in `~/.osimflow/registry.db` and per-campaign cache in `${outdir}/.cache/` — both SQLite databases. There is no backup tooling, no point-in-time recovery, and no way to export/import campaign state between machines.
- **Impact:** Loss of the registry database or outdir corrupts campaign history. HPC users cannot transfer partial campaign state between clusters. No compliance-grade data retention.
- **Recommended Solution:** Add `osimflow backup create <outdir> <dest>` that: (1) uses SQLite `.backup()` API for consistent snapshot, (2) archives run.json + samples.json + cache into a tarball, (3) supports S3/GCS destination via existing storage backends. Add `osimflow backup restore` for the reverse operation.
- **Effort:** M (2 weeks)
- **Priority:** P1

### OPS-005: No Distributed Tracing

- **Severity:** P1
- **openstudio-server equivalent:** None (both lack)
- **Description:** Neither system has distributed tracing, but OSimFlow's distributed executor model (Slurm, AWS Batch, Dask) makes cross-node tracing more critical. When a sample fails on a remote node, there is no correlation between the orchestrator's request, the executor's dispatch, and the worker's execution.
- **Impact:** Debugging distributed campaign failures requires manual log correlation across multiple files and nodes.
- **Recommended Solution:** Add OpenTelemetry span propagation through the executor chain: Campaign → Executor.submit() → Worker → Work function. Use the existing `--observability opentelemetry` backend. Propagate trace context via environment variables (`TRACEPARENT`) to container jobs.
- **Effort:** L (3-4 weeks)
- **Priority:** P2

### OPS-006: No RBAC or Multi-User Access Controls

- **Severity:** P1
- **openstudio-server equivalent:** Devise auth + project-level ACL + role-based access
- **Description:** OSimFlow's API has a single shared API key. There is no concept of users, roles, or per-campaign access control. Any valid API key can start/stop/read any campaign.
- **Impact:** In multi-tenant deployments (shared HPC cluster, team use), there is no way to restrict who can modify or delete campaign data.
- **Recommended Solution:** Short-term: add per-campaign API key scoping (campaign owner key vs read-only key). Long-term: add lightweight RBAC with `admin`, `operator`, `viewer` roles stored in the registry database.
- **Effort:** L (4+ weeks for full RBAC), S (1 week for per-campaign scoping)
- **Priority:** P2 (full RBAC), P1 (per-campaign scoping)

### OPS-007: No Worker Auto-Recovery

- **Severity:** P2
- **openstudio-server equivalent:** Sidekiq retry + dead-letter queue + process supervision
- **Description:** OSimFlow has per-sample retry with exponential backoff (issue #252) and worker heartbeat files (issue #341), but there is no mechanism to automatically restart failed workers or reschedule abandoned samples when a node goes down.
- **Impact:** If a Slurm node fails mid-campaign, samples assigned to that node are lost. The campaign can complete with gaps. Manual intervention is required to identify and re-run failed samples.
- **Recommended Solution:** Add a `WorkerWatchdog` that: (1) monitors heartbeat files, (2) detects stale workers (no heartbeat for >2× interval), (3) reschedules abandoned samples via the existing retry mechanism, (4) emits alert via OPS-002. Integrate with DaskTaskQueue's dead-letter support.
- **Effort:** M (2-3 weeks)
- **Priority:** P2

### OPS-008: No Campaign Pause/Resume

- **Severity:** P2
- **openstudio-server equivalent:** Project pause/resume via web UI
- **Description:** OSimFlow supports `.stop` flag for graceful cancellation and cache-based resume (re-running with same `--outdir`), but there is no way to pause a running campaign, inspect intermediate results, and then resume without restarting from scratch.
- **Impact:** Users cannot inspect partial results during long-running campaigns without cancelling and re-running. The cache resume is all-or-nothing per step.
- **Recommended Solution:** Add `--pause` flag that: (1) sets a `.pause` flag file, (2) workers check for `.pause` before starting new samples, (3) in-progress samples complete normally, (4) operator can inspect results, (5) remove `.pause` to resume. Build on existing `.stop` flag pattern.
- **Effort:** S (1 week)
- **Priority:** P2

### OPS-009: No Rate Limiting Per-User or Per-Campaign

- **Severity:** P2
- **openstudio-server equivalent:** None (both lack fine-grained rate limiting)
- **Description:** OSimFlow has global rate limiting via slowapi (60/min default), but no per-user or per-campaign rate limits. A single client can consume all API capacity.
- **Impact:** In multi-user deployments, one misbehaving client can degrade the API for all users.
- **Recommended Solution:** Extend slowapi configuration with per-key rate limits stored in the registry database. Allow operators to configure limits via `osimflow serve --rate-limit-per-key 30`.
- **Effort:** S (1 week)
- **Priority:** P3

### OPS-010: No Campaign Resource Quotas

- **Severity:** P2
- **openstudio-server equivalent:** None (both lack)
- **Description:** OSimFlow has per-step resource defaults but no enforcement of maximum resources per campaign. A single campaign can consume all available cluster resources.
- **Impact:** In shared environments, one campaign can starve others of resources.
- **Recommended Solution:** Add `--max-cpus`, `--max-memory-gb` flags to campaign config. Enforce at executor submit time by tracking cumulative resource allocation and rejecting submissions that exceed quotas.
- **Effort:** M (2 weeks)
- **Priority:** P3

### OPS-011: No Campaign Cost Tracking

- **Severity:** P3
- **openstudio-server equivalent:** None (both lack)
- **Description:** OSimFlow's `Handle` dataclass has `cost_usd` and `billed_duration_seconds` fields, and AWS Batch executor tracks Spot pricing, but there is no aggregated cost dashboard or cost-per-campaign reporting.
- **Impact:** Users running cloud campaigns have no visibility into cost accrual during execution.
- **Recommended Solution:** Aggregate cost data from Handle fields into RunTrace. Add `osimflow cost report <outdir>` CLI command and `/api/v1/cost` endpoint. Show: estimated cost, cost per sample, cost per KPI.
- **Effort:** S (1 week)
- **Priority:** P3

### OPS-012: No Chaos/Resilience Testing

- **Severity:** P3
- **openstudio-server equivalent:** None (both lack)
- **Description:** OSimFlow has integration tests for cache invalidation and executor debug mode, but no chaos testing or fault injection to validate resilience under failure conditions.
- **Impact:** Retry logic, heartbeat detection, and crash recovery are untested under realistic failure scenarios.
- **Recommended Solution:** Add a `tests/chaos/` suite that: (1) kills worker processes mid-simulation, (2) corrupts cache files, (3) fills disk during simulation, (4) simulates network partitions for cloud executors. Use `pytest` fixtures with `subprocess.Popen` and signal injection.
- **Effort:** M (2 weeks)
- **Priority:** P3

---

## Already Addressed (Not Gaps)

These items from the original baseline comparison have been resolved:

| Item | Resolution | Issue |
|---|---|---|
| No retry mechanism | Per-sample retry with exponential backoff, transient error detection | #252 |
| No API authentication | API key auth with constant-time comparison, CORS, rate limiting | #268 |
| BYOS runs in orchestrator process | Subprocess isolation by default | #269 |
| No distributed task queue | DaskTaskQueue with dead-letter, retry, task states | #335 |
| No elastic worker scaling | DaskJobQueueExecutor with auto-scaling | #338 |
| No worker health monitoring | WorkerHeartbeat with configurable interval | #341 |
| No BYOS resource limits | CPU/memory limits via subprocess wrapper | #343 |
| SQLite WAL mode | Enabled with busy_timeout for concurrency | #247 |

---

## Priority Summary

| Priority | Gaps | Total Effort |
|---|---|---|
| P0 | OPS-001, OPS-002 | 5-7 weeks |
| P1 | OPS-003, OPS-004, OPS-005, OPS-006 (partial) | 8-11 weeks |
| P2 | OPS-007, OPS-008, OPS-009 | 4-5 weeks |
| P3 | OPS-010, OPS-011, OPS-012 | 5 weeks |

**Recommended Phase 1 scope:** OPS-001 (health dashboard) + OPS-004 (backup/export) + OPS-008 (pause/resume) = 4-5 weeks total, addresses the highest-impact gaps with lowest effort.

---

## Architecture Notes

### What OSimFlow Does Better Than openstudio-server

1. **Resource management:** Per-step resource directives, BYOS subprocess limits, elastic Dask scaling — openstudio-server has none of this
2. **Cache/resume:** Content-hash-based cache with WAL mode — more sophisticated than openstudio-server's scratch-dir approach
3. **Observability pluggability:** CloudWatch/Prometheus/OTel backends — openstudio-server is limited to Rails logs
4. **Executor abstraction:** Local/Slurm/AWS Batch/Azure/GCP/Dask/PBS/Nomad — openstudio-server only supports local + SSH
5. **Transient error detection:** `_is_transient_error` with exit code analysis — openstudio-server has no equivalent

### Architectural Constraints (Cannot Change)

- **CLI + library hybrid** — not a web service; operations must work in batch context
- **Single-process orchestrator** — no multi-master; Campaign class is the single source of truth
- **SQLite for metadata** — scales to thousands of campaigns, not millions; appropriate for the use case
- **No authentication layer by design** — CLI tool, not multi-tenant SaaS; API auth is opt-in

---

## Sources

- openstudio-server analysis: `.agents/results/openstudio-server-analysis.md`
- OSimFlow codebase: `osimflow/` package (monitoring.py, observability.py, cache.py, jobqueue.py, byos.py, storage.py, taskqueue.py, api/app.py, api/events.py, campaign.py, registry.py)
- Git history: issues #247, #252, #268, #269, #283, #335, #338, #341, #343
- AGENTS.md: project constraints and architecture decisions
