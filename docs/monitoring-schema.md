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
| `started_at` | `float` | Unix epoch (seconds) when the campaign began. |
| `finished_at` | `float \| null` | Unix epoch when the campaign ended. `null` if the run is still in progress or crashed before finalization. |
| `elapsed_s` | `float` | Wall-clock seconds (`finished_at - started_at`). |
| `config` | `object` | Key-value snapshot of the campaign configuration. |
| `summary` | `object` | Aggregate counts across all samples. |
| `steps` | `array` | Per-step trace rows (one per DAG step). |
| `per_sample` | `array` | Per-sample trace rows (one per LHS sample). |

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

## `steps[]` array — `StepTrace`

One entry per DAG step, in execution order.

| Field | Type | Description |
|---|---|---|
| `step` | `str` | DAG step name (see list below). |
| `cache` | `str` | Cache status for this step. |
| `elapsed_s` | `float` | Wall-clock seconds for this step. |
| `exit_code` | `int` | `0` on success, `1` on failure. |

### Step names

These correspond to the six-step DAG in `osimflow/campaign.py`:

| Step name | Fan-out | Notes |
|---|---|---|
| `GENERATE_LHS_SAMPLES` | Single-shot | LHS parameter generation. |
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

### Sample status values

| Value | Meaning |
|---|---|
| `ok` | Every step that ran for this sample returned exit code 0. |
| `failed` | At least one step returned a non-zero exit code. |
| `cached` | Reserved; not currently emitted in `to_dict()` output (samples that hit cache are still marked `"ok"` or `"failed"` based on the cached exit codes). |

---

## Example `run.json`

Below is a synthetic but schema-accurate excerpt from a 3-sample local
campaign. All values match the types produced by `RunTrace.to_dict()`.

```json
{
  "schema_version": 1,
  "campaign_id": "2026-06-10T14-30-00",
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
      "stdout_log": "/tmp/results/work/sim/sample_002/stdout.log",
      "stderr_log": "/tmp/results/work/sim/sample_002/stderr.log"
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

---

## Source reference

The canonical implementation lives in:

| Component | Source file |
|---|---|
| `StepTrace` dataclass | `osimflow/monitoring.py` |
| `SampleTrace` dataclass | `osimflow/monitoring.py` |
| `RunTrace` class and `to_dict()` | `osimflow/monitoring.py` |
| Trace population (step hooks) | `osimflow/campaign.py` |
| `sample_log_paths()` helper | `osimflow/monitoring.py` |
