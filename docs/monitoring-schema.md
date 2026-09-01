# OSimFlow — `run.json` Monitoring Schema

> **Audience:** anyone consuming `run.json` programmatically — dashboards,
> post-hoc analysis scripts, CI summarisers, or a developer debugging a
> failed campaign.

OSimFlow writes a single `${outdir}/run.json` at the end of every
campaign. The file is produced by `osimflow/monitoring.py:RunTrace` and
is the primary observability artifact (per
`.agents/results/monitoring-decision.md`).

---

## Quick Reference

| Top-level key | Type | Description |
|---|---|---|
| `schema_version` | `int` | Schema revision. Currently `1`. |
| `campaign_id` | `str` | ISO-ish timestamp at campaign start (`YYYY-MM-DDTHH-MM-SS`). |
| `status` | `str` | Campaign lifecycle state (`"running"`, `"success"`, `"cancelled"`, `"failed"`, `"paused"`). |
| `started_at` | `float` | Unix epoch (seconds) when the campaign began. |
| `finished_at` | `float \| null` | Unix epoch when the campaign ended. `null` if the run is still in progress or crashed before finalization. |
| `elapsed_s` | `float` | Wall-clock seconds (`finished_at - started_at`). |
| `config` | `object` | Key-value snapshot of the campaign configuration. |
| `summary` | `object` | Aggregate counts across all samples. |
| `quality_summary` | `object` | Aggregate output-quality-check counts across samples. |
| `steps` | `array` | Per-step trace rows (one per DAG step). |
| `per_sample` | `array` | Per-sample trace rows (one per LHS sample). |
| `generations` | `array` | Per-generation summaries (issue #270). Present only for multi-generation (iterative) campaigns. |
| `baseline_comparison` | `object` | Baseline improvement metrics (issue #64). Present only when a baseline is configured. |
| `init_script_duration_s` | `float` | Wall-clock seconds spent in the pre-campaign `--init-script` (issue #108). |
| `finalize_script_duration_s` | `float` | Wall-clock seconds spent in the post-campaign `--finalize-script` (issue #108). |
| `total_cost_usd` | `float` | Estimated total campaign cost (issue #126). Always present; `0.0` when cost tracking is off. |
| `spot_savings_usd` | `float` | Estimated savings from spot/preemptible capacity (issue #126). Always present; `0.0` default. |
| `cost_summary` | `object` | Campaign-level cost breakdown from `CostTracker.finalize()` (issue #447). Present when `--enable_cost_tracking` is set. |
| `cache_hit_rate` | `float` | Fraction of per-sample work served from cache, `0.0`–`1.0` (issue #426). |
| `chaos_invocations` | `array` | Chaos fault-injection records (issue #1013). Always present as a list; `[]` when chaos never fired. |
| `chaos_schedule` | `str` | Which chaos schedule was active (`"before_step"`, `"after_step"`, or `"per_sample"`) (issue #1191). |
| `circuit_breaker_states` | `object` | Final circuit-breaker state per breaker name (issue #1191). |
| `alerts_fired` | `array` | Alerts dispatched during the campaign, including per-alert `delivery_status` (issues #1191, #1308). |
| `paused_at` | `float` | Unix epoch when the campaign was paused (issue #553). Present only when paused. |
| `error_summary` | `str` | Campaign-level error message set when an unhandled exception fails the campaign (issue #737). |

> **Note:** unless marked "always present" or "present only when…", fields
> whose value is `None` are omitted from the JSON output entirely —
> consumers must treat every key except `schema_version`, `campaign_id`,
> `status`, `started_at`, `elapsed_s`, `config`, `summary`,
> `quality_summary`, `steps`, `per_sample`, `total_cost_usd`,
> `spot_savings_usd`, and `chaos_invocations` as optional.

---

## `config` object

Key-value pairs captured from `CampaignConfig` at construction time.

| Key | Type | Description |
|---|---|---|
| `executor` | `str` | Executor backend name (`local`, `slurm`, `aws_batch`, `nomad`). |
| `openstudio_version` | `str` | OpenStudio CLI version tag (e.g. `"3.11.0"`). |
| `n_samples` | `int` | Number of LHS samples requested. |
| `archive_intermediates` | `bool` | Whether `--archive_intermediates` was set. |
| `custom_apply_script` | `str \| null` | Path to the BYOS apply script, or `null`. |
| `custom_kpi_extractor` | `str \| null` | Path to the BYOS KPI extractor, or `null`. |

> **Note:** the set of keys may grow as new CLI flags are added. Consumers
> should treat unknown keys as ignorable extras.

---

## `summary` object

| Key | Type | Description |
|---|---|---|
| `n_samples` | `int` | Total samples attempted. |
| `n_succeeded` | `int` | Samples where every step succeeded (status `"ok"`). |
| `n_failed` | `int` | Samples where at least one step failed (status `"failed"`). |

---

## `quality_summary` object

Aggregate output-quality counts, computed from the per-sample
`quality_valid` / `quality_warnings` fields:

| Key | Type | Description |
|---|---|---|
| `n_quality_failures` | `int` | Samples whose quality validation failed (`quality_valid` is `false`). |
| `n_quality_warnings` | `int` | Samples with quality warnings but no hard failure. |
| `n_quality_ok` | `int` | Samples that passed quality checks cleanly. |

---

## `steps[]` array — `StepTrace`

One entry per DAG step, in execution order.

| Field | Type | Description |
|---|---|---|
| `step` | `str` | DAG step name (see list below). |
| `cache` | `str` | Cache status for this step. |
| `elapsed_s` | `float` | Wall-clock seconds for this step. |
| `exit_code` | `int` | `0` on success, `1` on failure. |

### Step names

These correspond to the seven-step DAG in `osimflow/campaign.py`:

| Step name | Fan-out | Notes |
|---|---|---|
| `GENERATE_LHS_SAMPLES` | Single-shot | LHS parameter generation. |
| `PREFLIGHT_RUN_MODEL` | Single-shot | Validates the seed model before fan-out (issue #107). |
| `APPLY_PARAMETERS` | N samples | Per-sample parameter application. |
| `RUN_OPENSTUDIO_SIM` | N samples | Heavy simulation step. |
| `EXTRACT_KPIS` | N samples | Per-sample KPI extraction. |
| `AGGREGATE_RESULTS` | Single-shot | CSV + Parquet + failed-sim summary. |
| `GENERATE_BASIC_PLOTS` | Single-shot | Plots are never cached (`SKIPPED`). |

### Cache status values

| Value | Meaning |
|---|---|
| `HIT` | Single-shot step — all work served from cache. |
| `MISS` | Single-shot step — no cache entry; fresh computation. |
| `HIT×N` | Fan-out step — all N samples served from cache. |
| `MISS×N` | Fan-out step — all N samples freshly computed. |
| `SKIPPED` | Step is not cached (currently only `GENERATE_BASIC_PLOTS`). |

> **Future:** mixed cache hits (`HIT×K / MISS×M` for `K+M = N`) are not yet
> emitted — the current implementation records the *majority* cache status.
> See `osimflow/campaign.py` for the per-step logic.

---

## `per_sample[]` array — `SampleTrace`

One entry per LHS sample. Fields with `null` values are omitted from the
JSON output (the `to_dict()` method filters them out).

| Field | Type | Always present? | Description |
|---|---|---|---|
| `sample_id` | `str` | Yes | Unique sample identifier (e.g. `"sample_000"`). |
| `status` | `str` | Yes | `"ok"`, `"failed"`, or `"cached"`. |
| `elapsed_s` | `float` | Yes | Per-sample total wall-clock (currently `0.0` — not yet tracked per-sample). |
| `apply_exit_code` | `int` | Yes | Exit code from `APPLY_PARAMETERS` (`0` = success). |
| `sim_exit_code` | `int` | Yes | Exit code from `RUN_OPENSTUDIO_SIM` (`0` = success). |
| `extract_exit_code` | `int` | Yes | Exit code from `EXTRACT_KPIS` (`0` = success). |
| `eplusout_sql` | `str` | No | Absolute path to `eplusout.sql` if the simulation produced one. |
| `error_summary` | `str` | No | One-line error summary (e.g. `"SIM: RuntimeError('…')"`). |
| `stdout_log` | `str` | No | Absolute path to `${outdir}/work/sim/<sample_id>/stdout.log`. |
| `stderr_log` | `str` | No | Absolute path to `${outdir}/work/sim/<sample_id>/stderr.log`. |
| `quality_valid` | `bool` | No | Result of per-sample output quality validation (`true`/`false`); omitted when quality checks did not run. |
| `quality_warnings` | `int` | No | Number of quality warnings raised for this sample. |
| `quality_failures` | `int` | No | Number of quality-check failures for this sample. |
| `generation` | `int` | No | Generation index for iterative algorithms (issue #106). |
| `worker_id` | `str` | No | Worker that executed the sample — Batch job ID, Slurm job ID, Nomad alloc ID, or `"local"` (issue #105). |
| `worker_ip` | `str` | No | IP address or hostname of the worker (issue #105). |
| `worker_region` | `str` | No | AWS region or Nomad datacenter of the worker (issue #105). |
| `cost_usd` | `float` | No | Estimated cost for this sample (issue #126). |
| `billed_duration_seconds` | `float` | No | Wall-time billed for this sample (issue #126). |
| `register_values` | `object` | No | `runner.registerValue` outputs captured from the OpenStudio CLI (issue #251). |
| `trace_id` | `str` | No | Per-sample trace ID for distributed observability correlation (issue #436). |

### Sample status values

| Value | Meaning |
|---|---|
| `ok` | Every step that ran for this sample returned exit code 0. |
| `failed` | At least one step returned a non-zero exit code. |
| `cached` | Reserved; not currently emitted in `to_dict()` output (samples that hit cache are still marked `"ok"` or `"failed"` based on the cached exit codes). |

---

## `generations[]` array — `GenerationTrace`

Present only when the campaign ran an iterative algorithm that executed
more than one generation (issue #270).

| Field | Type | Description |
|---|---|---|
| `generation` | `int` | Zero-based generation index. |
| `n_samples` | `int` | Samples evaluated in this generation. |
| `n_succeeded` | `int` | Samples that succeeded. |
| `n_failed` | `int` | Samples that failed. |
| `converged` | `bool` | Whether the algorithm signalled convergence after this generation. |
| `best_objective` | `float` | Best objective value seen so far, if the algorithm tracks one. |
| `elapsed_s` | `float` | Wall-clock seconds for this generation. |

---

## Cost and cache-efficiency fields

| Key | Type | Always present? | Description |
|---|---|---|---|
| `total_cost_usd` | `float` | Yes (`0.0` default) | Estimated total campaign cost (issue #126). |
| `spot_savings_usd` | `float` | Yes (`0.0` default) | Estimated savings from spot/preemptible capacity (issue #126). |
| `cost_summary` | `object` | No | Campaign-level cost breakdown produced by `CostTracker.finalize()`; present when `--enable_cost_tracking` is set (issue #447). |
| `cache_hit_rate` | `float` | No | Fraction of per-sample work served from cache, `0.0`–`1.0`. Set after `AGGREGATE_RESULTS` (issue #426). |

Per-sample counterparts (`cost_usd`, `billed_duration_seconds`) live in
`per_sample[]`.

---

## Hook timing fields

| Key | Type | Description |
|---|---|---|
| `init_script_duration_s` | `float` | Wall-clock seconds spent running the pre-campaign `--init-script` (issue #108). |
| `finalize_script_duration_s` | `float` | Wall-clock seconds spent running the post-campaign `--finalize-script` (issue #108). |

---

## Chaos fields (issues #1013, #1191)

### `chaos_schedule`

`str | null` — set once at campaign start from `cfg.chaos.schedule` so
`run.json` records which schedule was active. One of
`"before_step"`, `"after_step"`, or `"per_sample"`.

### `chaos_invocations`

`array` — **always present** (the value is `[]` when chaos was never
enabled or never fired, so downstream tooling can rely on the key).
One entry per `Campaign._maybe_inject_chaos` call that actually
exercised a fault injector (the engine was enabled, the schedule
matched, and at least one registered injector ran):

| Field | Type | Description |
|---|---|---|
| `step` | `str` | DAG step the injection was attached to. |
| `when` | `str` | Schedule phase that matched: `"before_step"`, `"after_step"`, or `"per_sample"`. |
| `target_id` | `str` | Sample or step identifier the engine targeted. |
| `results` | `array` | One row per `ChaosResult` produced by the injectors that ran. |

Each `results[]` row:

| Field | Type | Description |
|---|---|---|
| `fault_type` | `str` | Registered fault type (e.g. `"cpu_spike"`, `"memory_pressure"`, `"network_delay"`, `"kill_switch"`). |
| `target_id` | `str` | Identifier of the injected target. |
| `injected` | `bool` | Whether the fault actually fired. |
| `duration_s` | `float` | How long the fault was held. |
| `error` | `str` | Injector error message, if any. |

```json
"chaos_invocations": [
  {
    "step": "RUN_OPENSTUDIO_SIM",
    "when": "per_sample",
    "target_id": "sample_002",
    "results": [
      {
        "fault_type": "network_delay",
        "target_id": "sample_002",
        "injected": true,
        "duration_s": 5.0,
        "error": null
      }
    ]
  }
]
```

---

## `circuit_breaker_states`

`object | null` — final circuit-breaker states, populated from live
`CircuitBreaker` instances at campaign end (issue #1191). Keys are the
breaker names (e.g. `"cache:<campaign_id>"` for the Redis data plane,
`"docs:<namespace>"` for the Redis document store,
`"jobqueue:<campaign_id>"` for the distributed job queue); values are
the final state: `"closed"`, `"open"`, or `"half_open"`.

```json
"circuit_breaker_states": {
  "cache:2026-06-10T14-30-00": "closed",
  "docs:myproject": "open",
  "jobqueue:2026-06-10T14-30-00": "closed"
}
```

---

## `alerts_fired`

`array | null` — one entry per alert dispatched by `AlertManager`
during the campaign (issue #1191; the `RunTrace.record_alert` wiring
is issue #1308). Each entry is a serialised
`osimflow.alerting.Alert`:

| Field | Type | Description |
|---|---|---|
| `rule_name` | `str` | Alert rule that fired. |
| `event_type` | `str` | Campaign event that triggered the rule. |
| `severity` | `str` | Severity level: `"INFO"`, `"WARNING"`, or `"CRITICAL"`. |
| `message` | `str` | Human-readable alert message. |
| `delivery_status` | `str` | Outcome of dispatching to the configured destinations: `"delivered"`, `"partial"`, `"failed"`, `"no_destinations"`, or `"unknown"`. |
| `timestamp` | `float` | Unix epoch when the alert fired. |

```json
"alerts_fired": [
  {
    "rule_name": "sim_failure_rate",
    "event_type": "campaign_summary",
    "severity": "WARNING",
    "message": "failure rate 12% exceeds threshold 5%",
    "delivery_status": "delivered",
    "timestamp": 1749565854.1
  }
]
```

---

## Lifecycle fields

| Key | Type | Description |
|---|---|---|
| `status` | `str` | Campaign lifecycle state: `"running"`, `"success"`, `"cancelled"`, `"failed"`, or `"paused"`. |
| `paused_at` | `float` | Unix epoch when the campaign was paused (issue #553). Present only when the campaign has been paused. |
| `error_summary` | `str` | Campaign-level error message set when an unhandled exception causes failure (issue #737). |

---

## Example `run.json`

Below is a synthetic but schema-accurate excerpt from a 3-sample local
campaign. All values match the types produced by `RunTrace.to_dict()`.

```json
{
  "schema_version": 1,
  "campaign_id": "2026-06-10T14-30-00",
  "status": "success",
  "started_at": 1749565800.0,
  "finished_at": 1749565855.3,
  "elapsed_s": 55.3,
  "config": {
    "executor": "local",
    "openstudio_version": "3.11.0",
    "n_samples": 3,
    "archive_intermediates": false,
    "custom_apply_script": null,
    "custom_kpi_extractor": null
  },
  "summary": {
    "n_samples": 3,
    "n_succeeded": 2,
    "n_failed": 1
  },
  "quality_summary": {
    "n_quality_failures": 0,
    "n_quality_warnings": 1,
    "n_quality_ok": 2
  },
  "steps": [
    {
      "step": "GENERATE_LHS_SAMPLES",
      "cache": "MISS",
      "elapsed_s": 0.45,
      "exit_code": 0
    },
    {
      "step": "APPLY_PARAMETERS",
      "cache": "MISS\u00d7N",
      "elapsed_s": 1.20,
      "exit_code": 0
    },
    {
      "step": "RUN_OPENSTUDIO_SIM",
      "cache": "MISS\u00d7N",
      "elapsed_s": 48.10,
      "exit_code": 0
    },
    {
      "step": "EXTRACT_KPIS",
      "cache": "MISS\u00d7N",
      "elapsed_s": 3.50,
      "exit_code": 0
    },
    {
      "step": "AGGREGATE_RESULTS",
      "cache": "MISS",
      "elapsed_s": 0.80,
      "exit_code": 0
    },
    {
      "step": "GENERATE_BASIC_PLOTS",
      "cache": "SKIPPED",
      "elapsed_s": 1.25,
      "exit_code": 0
    }
  ],
  "per_sample": [
    {
      "sample_id": "sample_000",
      "status": "ok",
      "elapsed_s": 0.0,
      "apply_exit_code": 0,
      "sim_exit_code": 0,
      "extract_exit_code": 0,
      "eplusout_sql": "/tmp/results/work/sim/sample_000/eplusout.sql",
      "quality_valid": true,
      "quality_warnings": 1,
      "quality_failures": 0,
      "worker_id": "local",
      "cost_usd": 0.0021,
      "trace_id": "trace-0a1b2c3d",
      "stdout_log": "/tmp/results/work/sim/sample_000/stdout.log",
      "stderr_log": "/tmp/results/work/sim/sample_000/stderr.log"
    },
    {
      "sample_id": "sample_001",
      "status": "ok",
      "elapsed_s": 0.0,
      "apply_exit_code": 0,
      "sim_exit_code": 0,
      "extract_exit_code": 0,
      "eplusout_sql": "/tmp/results/work/sim/sample_001/eplusout.sql",
      "quality_valid": true,
      "worker_id": "local",
      "cost_usd": 0.0020,
      "trace_id": "trace-4e5f6a7b",
      "stdout_log": "/tmp/results/work/sim/sample_001/stdout.log",
      "stderr_log": "/tmp/results/work/sim/sample_001/stderr.log"
    },
    {
      "sample_id": "sample_002",
      "status": "failed",
      "elapsed_s": 0.0,
      "apply_exit_code": 0,
      "sim_exit_code": 1,
      "extract_exit_code": 1,
      "error_summary": "SIM: RuntimeError('openstudio.cli returned 1')",
      "worker_id": "local",
      "cost_usd": 0.0004,
      "trace_id": "trace-8c9d0e1f",
      "stdout_log": "/tmp/results/work/sim/sample_002/stdout.log",
      "stderr_log": "/tmp/results/work/sim/sample_002/stderr.log"
    }
  ],
  "total_cost_usd": 0.0045,
  "spot_savings_usd": 0.0,
  "cache_hit_rate": 0.0,
  "chaos_invocations": [],
  "chaos_schedule": "per_sample",
  "circuit_breaker_states": {
    "cache:2026-06-10T14-30-00": "closed"
  },
  "alerts_fired": [
    {
      "rule_name": "sim_failure_rate",
      "event_type": "campaign_summary",
      "severity": "WARNING",
      "message": "failure rate 33% exceeds threshold 5%",
      "delivery_status": "delivered",
      "timestamp": 1749565854.1
    }
  ]
}
```

---

## Schema evolution

The `schema_version` field is `1`. Backward-incompatible changes will
increment this number. Additive changes (new optional fields) will not
increment it — consumers should ignore unknown keys.

### Changelog

| Version | Change |
|---|---|
| 1 | Initial schema. `per_sample.elapsed_s` is always `0.0` (per-sample wall-clock not yet tracked). |
| 1 (additive) | Lifecycle: `status`, `paused_at` (#553), `error_summary` (#737). |
| 1 (additive) | Quality: `quality_summary` and per-sample `quality_valid` / `quality_warnings` / `quality_failures`. |
| 1 (additive) | Workers & cost: per-sample `worker_id` / `worker_ip` / `worker_region` (#105), `cost_usd` / `billed_duration_seconds` (#126), top-level `total_cost_usd` / `spot_savings_usd` (#126), `cost_summary` (#447), `cache_hit_rate` (#426). |
| 1 (additive) | Iterative algorithms: `generations` (#270), per-sample `generation` (#106); observability `trace_id` (#436), `register_values` (#251). |
| 1 (additive) | Hooks: `init_script_duration_s` / `finalize_script_duration_s` (#108). |
| 1 (additive) | Resilience: `chaos_invocations` (#1013), `chaos_schedule`, `circuit_breaker_states`, `alerts_fired` (with `delivery_status`) (#1191, #1308). |
| 1 (additive) | DAG: `PREFLIGHT_RUN_MODEL` step row added (#107). |

---

## Source reference

The canonical implementation lives in:

| Component | Source file |
|---|---|
| `StepTrace` dataclass | `osimflow/monitoring.py` |
| `SampleTrace` dataclass | `osimflow/monitoring.py` |
| `GenerationTrace` dataclass | `osimflow/monitoring.py` |
| `RunTrace` class and `to_dict()` | `osimflow/monitoring.py` |
| Trace population (step hooks) | `osimflow/campaign.py` |
| Chaos engine feeding `chaos_invocations` | `osimflow/chaos.py` |
| Circuit breakers feeding `circuit_breaker_states` | `osimflow/circuit_breaker.py` |
| `Alert` payloads feeding `alerts_fired` | `osimflow/alerting.py` |
| `sample_log_paths()` helper | `osimflow/monitoring.py` |
