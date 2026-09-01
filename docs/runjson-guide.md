# OSimFlow — Interpreting `run.json`

> **Audience:** BEM practitioners running OSimFlow campaigns. This guide
> teaches you to diagnose any campaign issue from the `run.json` monitoring
> artifact alone.

Every OSimFlow campaign writes a single `${outdir}/run.json` file at
completion. This file is your primary observability tool — it contains
per-step timing, per-sample status, cache behaviour, and error summaries.

**This guide answers these common questions:**

- Did all my samples succeed, or did some fail silently?
- How long did the simulation step take? Is my resource allocation right?
- Why did the second run complete in 0.1 seconds? Did it actually do anything?
- Which specific samples failed? Can I see the error without digging into logs?

---

## 1. Quick health check

After a campaign finishes, read the top-level summary:

```bash
jq '{campaign_id, elapsed_s, summary}' results/run.json
```

```json
{
  "campaign_id": "2026-06-10T14-30-00",
  "elapsed_s": 55.3,
  "summary": {
    "n_samples": 500,
    "n_succeeded": 497,
    "n_failed": 3
  }
}
```

**Three numbers tell the whole story:**

| Field | What to check |
|---|---|
| `summary.n_succeeded` | Should equal `n_samples` in a clean run. |
| `summary.n_failed` | Should be `0`. Any non-zero value means investigate. |
| `elapsed_s` | Total wall-clock. Compare against expectations for your sample count. |

---

## 2. Complete schema reference

The formal schema is documented in [`monitoring-schema.md`](monitoring-schema.md).
Below is a practitioner-oriented walk-through of every field.

### 2.1 Top-level fields

| Field | Type | Meaning |
|---|---|---|
| `schema_version` | `int` | Schema revision. Currently `1`. |
| `campaign_id` | `str` | Timestamp at campaign start (`YYYY-MM-DDTHH-MM-SS`). |
| `status` | `str` | Lifecycle state: `"running"`, `"success"`, `"cancelled"`, `"failed"`, or `"paused"`. |
| `started_at` | `float` | Unix epoch when the campaign began. |
| `finished_at` | `float` | Unix epoch when the campaign ended. `null` if the run crashed. |
| `elapsed_s` | `float` | Wall-clock seconds (`finished_at - started_at`). |
| `config` | `object` | Campaign configuration snapshot (see below). |
| `summary` | `object` | Aggregate sample counts. |
| `quality_summary` | `object` | Aggregate output-quality counts (see §2.7). |
| `steps` | `array` | Per-step timing rows (one per DAG step). |
| `per_sample` | `array` | Per-sample status rows (one per LHS sample). |
| `generations` | `array` | Per-generation summaries — present only for iterative/multi-generation campaigns (§2.10). |
| `baseline_comparison` | `object` | Present only when `--baseline` is configured (issue #64). |
| `init_script_duration_s` | `float` | Wall-clock seconds in the pre-campaign `--init-script` (issue #108). |
| `finalize_script_duration_s` | `float` | Wall-clock seconds in the post-campaign `--finalize-script` (issue #108). |
| `total_cost_usd` | `float` | Estimated total campaign cost — always present, `0.0` default (issue #126; §2.8). |
| `spot_savings_usd` | `float` | Estimated spot/preemptible savings — always present, `0.0` default (issue #126; §2.8). |
| `cost_summary` | `object` | Cost breakdown; present when `--enable_cost_tracking` is set (issue #447; §2.8). |
| `cache_hit_rate` | `float` | Fraction of work served from cache, `0.0`–`1.0` (issue #426; §2.9). |
| `chaos_invocations` | `array` | Chaos fault-injection records — always present, `[]` when chaos never fired (issue #1013; §2.11). |
| `chaos_schedule` | `str` | Active chaos schedule (issue #1191; §2.11). |
| `circuit_breaker_states` | `object` | Final circuit-breaker state per breaker name (issue #1191; §2.12). |
| `alerts_fired` | `array` | Alerts dispatched, with `delivery_status` per alert (issues #1191, #1308; §2.13). |
| `paused_at` | `float` | Unix epoch when the campaign was paused — present only when paused (issue #553). |
| `error_summary` | `str` | Campaign-level error message — present only when the campaign failed (issue #737). |

> Everything not marked "always present" may be absent from the JSON
> entirely — `None` values are omitted by `RunTrace.to_dict()`.

### 2.2 `config` — campaign configuration snapshot

Captured at construction time so you can reproduce the run:

```json
{
  "executor": "local",
  "openstudio_version": "3.11.0",
  "n_samples": 500,
  "archive_intermediates": false,
  "custom_apply_script": null,
  "custom_kpi_extractor": null,
  "baseline_sample_id": null
}
```

| Key | Meaning |
|---|---|
| `executor` | Backend used: `local`, `slurm`, `aws_batch`, or `nomad`. |
| `openstudio_version` | OpenStudio CLI version tag. Determines the container image. |
| `n_samples` | Number of LHS samples requested. |
| `archive_intermediates` | Whether `--archive_intermediates` was set. |
| `custom_apply_script` | Path to a BYOS apply script, or `null`. |
| `custom_kpi_extractor` | Path to a BYOS KPI extractor, or `null`. |
| `baseline_sample_id` | Baseline sample ID if `--baseline` was set, or `null`. |

> **Tip:** If a campaign produces unexpected results, check `config` first.
> A wrong `openstudio_version` or an unintended `custom_kpi_extractor` is a
> common cause.

### 2.3 `steps[]` — per-step timing

One entry per DAG step, in execution order:

```json
{
  "step": "RUN_OPENSTUDIO_SIM",
  "cache": "MISS×N",
  "elapsed_s": 48.10,
  "exit_code": 0
}
```

| Field | Meaning |
|---|---|
| `step` | DAG step name (see table below). |
| `cache` | Cache status for this step (see table below). |
| `elapsed_s` | Wall-clock seconds for this step. |
| `exit_code` | `0` = success, `1` = failure. |

**Step names and fan-out behaviour:**

| Step name | Fan-out | Typical cost |
|---|---|---|
| `GENERATE_LHS_SAMPLES` | Single-shot | < 1 second |
| `APPLY_PARAMETERS` | N samples | 1–5 seconds per sample |
| `RUN_OPENSTUDIO_SIM` | N samples | 5 min – 4 hours per sample |
| `EXTRACT_KPIS` | N samples | 1–30 seconds per sample |
| `AGGREGATE_RESULTS` | Single-shot | 1–10 seconds |
| `GENERATE_BASIC_PLOTS` | Single-shot | 1–5 seconds |

**Cache status values:**

| Value | When it appears |
|---|---|
| `HIT` | Single-shot step served entirely from cache. |
| `MISS` | Single-shot step ran fresh. |
| `HIT×N` | Fan-out step where all N samples were cached. |
| `MISS×N` | Fan-out step where all N samples ran fresh. |
| `SKIPPED` | Step is not cached (only `GENERATE_BASIC_PLOTS`). |

### 2.4 `per_sample[]` — per-sample status

One entry per LHS sample. Fields with `null` values are omitted:

```json
{
  "sample_id": "sample_042",
  "status": "failed",
  "elapsed_s": 0.0,
  "apply_exit_code": 0,
  "sim_exit_code": 1,
  "extract_exit_code": 1,
  "error_summary": "SIM: RuntimeError('openstudio.cli returned 1')",
  "eplusout_sql": "/path/to/results/work/sim/sample_042/eplusout.sql",
  "stdout_log": "/path/to/results/work/sim/sample_042/stdout.log",
  "stderr_log": "/path/to/results/work/sim/sample_042/stderr.log"
}
```

| Field | Always present? | Meaning |
|---|---|---|
| `sample_id` | Yes | Unique identifier (e.g. `"sample_000"`). |
| `status` | Yes | `"ok"` or `"failed"`. |
| `elapsed_s` | Yes | Per-sample total wall-clock (currently `0.0` — tracking not yet implemented). |
| `apply_exit_code` | Yes | `0` = `APPLY_PARAMETERS` succeeded for this sample. |
| `sim_exit_code` | Yes | `0` = `RUN_OPENSTUDIO_SIM` succeeded for this sample. |
| `extract_exit_code` | Yes | `0` = `EXTRACT_KPIS` succeeded for this sample. |
| `eplusout_sql` | No | Path to `eplusout.sql` if the simulation produced one. |
| `error_summary` | No | One-line error message from the first step that failed. |
| `stdout_log` | No | Path to per-sample stdout log from the simulation step. |
| `stderr_log` | No | Path to per-sample stderr log from the simulation step. |
| `quality_valid` | No | `true`/`false` result of output quality validation; omitted when quality checks did not run. |
| `quality_warnings` | No | Number of quality warnings raised for this sample. |
| `quality_failures` | No | Number of quality-check failures for this sample. |
| `generation` | No | Generation index for iterative algorithms (issue #106). |
| `worker_id` | No | Worker that executed the sample — Batch job ID, Slurm job ID, Nomad alloc ID, or `"local"` (issue #105). |
| `worker_ip` | No | IP address or hostname of the worker (issue #105). |
| `worker_region` | No | AWS region or Nomad datacenter of the worker (issue #105). |
| `cost_usd` | No | Estimated cost for this sample (issue #126). |
| `billed_duration_seconds` | No | Wall-time billed for this sample (issue #126). |
| `register_values` | No | `runner.registerValue` outputs captured from the OpenStudio CLI (issue #251). |
| `trace_id` | No | Per-sample trace ID for joining CloudWatch / Prometheus / OTel metrics to this sample (issue #436). |

**A sample is `"ok"` only if all three exit codes are `0`.** Any non-zero
exit code makes the sample `"failed"`.

### 2.5 `baseline_comparison` — baseline metrics (optional)

Present only when `--baseline` is configured. Contains improvement
percentages relative to the baseline sample:

```json
{
  "baseline_eui_kwh_m2_yr": 185.42,
  "min_eui_kwh_m2_yr_improvement_pct": -5.2,
  "max_eui_kwh_m2_yr_improvement_pct": 32.1
}
```

Positive values indicate improvement (lower EUI) relative to the baseline.
Negative values indicate the parametric sample performed worse.

### 2.6 `status`, `paused_at`, `error_summary` — campaign lifecycle

`status` is the headline: `"success"` means every sample finished, `"failed"`
means the campaign aborted (check `error_summary`), `"paused"` means a
`osimflow pause` is in effect (`paused_at` records when), `"cancelled"`
means a `osimflow cancel` terminated it, and `"running"` appears only in
incremental checkpoints written mid-campaign.

```json
{
  "status": "failed",
  "error_summary": "CampaignError: 47 samples exceeded the failure threshold",
  "paused_at": null
}
```

A campaign-level `error_summary` is only set when an unhandled exception
fails the whole campaign (issue #737) — per-sample errors stay in
`per_sample[].error_summary`.

### 2.7 `quality_summary` — output quality at a glance

Aggregates the per-sample quality flags into three counts:

```json
"quality_summary": {
  "n_quality_failures": 2,
  "n_quality_warnings": 11,
  "n_quality_ok": 487
}
```

`n_quality_failures > 0` means some samples simulated "successfully"
(exit code 0) but their outputs failed sanity validation — treat these
like failures when computing KPIs. Per-sample detail lives in
`quality_valid` / `quality_warnings` / `quality_failures`.

### 2.8 Cost fields — `total_cost_usd`, `spot_savings_usd`, `cost_summary`

`total_cost_usd` and `spot_savings_usd` are always present (issue #126);
`0.0` when cost tracking is off or everything ran locally. Per-sample
breakdown lives in `per_sample[].cost_usd` and
`per_sample[].billed_duration_seconds`. With
`--enable_cost_tracking`, `CostTracker.finalize()` adds a richer
`cost_summary` object (issue #447):

```json
"total_cost_usd": 12.47,
"spot_savings_usd": 3.12,
"cost_summary": {
  "total_usd": 12.47,
  "spot_usd": 6.10,
  "on_demand_usd": 6.37
}
```

### 2.9 `cache_hit_rate` — was the cache pulling its weight?

A `0.0`–`1.0` fraction set after `AGGREGATE_RESULTS` (issue #426).
`0.0` on a cold run and `1.0` on a warm re-run are both healthy; a low
value on what you expected to be a warm run points at cache
invalidation (see §3.5).

### 2.10 `generations[]` — iterative algorithm progress

Present only for multi-generation campaigns (genetic algorithms,
adaptive search — issue #270). One row per generation:

```json
"generations": [
  {
    "generation": 0,
    "n_samples": 20,
    "n_succeeded": 20,
    "n_failed": 0,
    "converged": false,
    "best_objective": 142.7,
    "elapsed_s": 912.4
  }
]
```

Watch `best_objective` plateau across generations to decide whether
more generations are worth the compute.

### 2.11 Chaos fields — `chaos_schedule`, `chaos_invocations`

When `--chaos-enabled` is set, `chaos_schedule` records which injection
schedule was active (`"before_step"`, `"after_step"`, or
`"per_sample"` — issue #1191), and `chaos_invocations` lists every fault
that actually fired (issue #1013). The key is **always present** — `[]`
when chaos never fired — so scripts can iterate it unconditionally:

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

Each entry pairs the DAG step and schedule phase that matched with the
per-injector `results[]` rows (`fault_type`, `injected`, `duration_s`,
`error`). Use this to verify resilience experiments actually exercised
the faults you configured — see [`chaos-engine.md`](chaos-engine.md).

### 2.12 `circuit_breaker_states` — Redis outage forensics

Populated at campaign end from the live `CircuitBreaker` instances
(issue #1191). Keys are breaker names (`cache:<campaign_id>` for the
Redis data plane, `docs:<namespace>` for the document store,
`jobqueue:<campaign_id>` for the distributed job queue); values are the
final state: `"closed"` (healthy), `"open"` (fail-fast after repeated
failures), or `"half_open"` (probing recovery):

```json
"circuit_breaker_states": {
  "cache:2026-06-10T14-30-00": "closed",
  "docs:myproject": "open"
}
```

An `"open"` breaker at campaign end means Redis was down and the
campaign ran on its local fallbacks — cross-worker coordination was
degraded. See [`distributed-cache.md`](distributed-cache.md) for the
recovery semantics.

### 2.13 `alerts_fired` — what the alert rules told your destinations

One entry per alert dispatched by `AlertManager` (issue #1191; the
`RunTrace.record_alert` wiring is issue #1308):

```json
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
```

`delivery_status` is the dispatch outcome across your configured
`--alert-destinations`: `"delivered"` (all destinations accepted),
`"partial"` (some failed), `"failed"` (none accepted), or
`"no_destinations"` (no destinations configured). A `"failed"` status
means the problem was detected but nobody was told — check your webhook
/email configuration.

---

## 3. Common interpretation scenarios

### 3.1 All samples succeeded

```json
"summary": {
  "n_samples": 500,
  "n_succeeded": 500,
  "n_failed": 0
}
```

All `per_sample` entries have `status: "ok"` and all exit codes are `0`.

**What to check:**
- `steps[2].elapsed_s` (the `RUN_OPENSTUDIO_SIM` row) — is the total
  simulation time reasonable for your model complexity and sample count?
- `config.n_samples` — does it match what you intended?

### 3.2 Some samples failed

```json
"summary": {
  "n_samples": 500,
  "n_succeeded": 497,
  "n_failed": 3
}
```

Find the failed samples and their error messages:

```bash
jq '.per_sample[] | select(.status == "failed") | {sample_id, error_summary, sim_exit_code}' results/run.json
```

```json
{"sample_id": "sample_042", "error_summary": "SIM: RuntimeError('openstudio.cli returned 1')", "sim_exit_code": 1}
{"sample_id": "sample_187", "error_summary": "SIM: RuntimeError('openstudio.cli returned 1')", "sim_exit_code": 1}
{"sample_id": "sample_331", "error_summary": "APPLY: UnmappedParameterError('insulation_r_val')", "sim_exit_code": 0}
```

**Diagnosis pattern:**

| Symptom | Likely cause |
|---|---|
| `sim_exit_code: 1` + `error_summary` mentions `openstudio.cli` | EnergyPlus simulation error. Check the per-sample `stderr_log`. |
| `apply_exit_code: 1` + `error_summary` mentions `UnmappedParameterError` | A parameter name in `variables.yml` does not exist in the template. Fix the variable name. |
| `apply_exit_code: 1` + `error_summary` mentions `AmbiguousParameterError` | Parameter name appears in multiple measures. Use the dotted form: `MeasureName.argument_name`. |
| `sim_exit_code: 1` + no `eplusout_sql` | Simulation crashed before producing output. Check `stderr_log` for the EnergyPlus error. |
| `sim_exit_code: 1` + `eplusout_sql` present | Simulation ran but reported a severe error. Check `eplusout.err` for the first "Severe Error" line. |

To inspect a specific failed sample's logs:

```bash
# Find the log paths
jq -r '.per_sample[] | select(.sample_id == "sample_042") | .stderr_log' results/run.json
# Then read the log
cat /path/to/results/work/sim/sample_042/stderr.log
```

### 3.3 Warm run was dramatically faster (cache resume)

On a second run with the same `--outdir`, you will see:

```json
"steps": [
  {"step": "GENERATE_LHS_SAMPLES", "cache": "HIT",   "elapsed_s": 0.001},
  {"step": "APPLY_PARAMETERS",     "cache": "HIT×N", "elapsed_s": 0.005},
  {"step": "RUN_OPENSTUDIO_SIM",   "cache": "HIT×N", "elapsed_s": 0.008},
  {"step": "EXTRACT_KPIS",         "cache": "HIT×N", "elapsed_s": 0.003},
  {"step": "AGGREGATE_RESULTS",    "cache": "HIT",   "elapsed_s": 0.002},
  {"step": "GENERATE_BASIC_PLOTS", "cache": "SKIPPED","elapsed_s": 1.25}
]
```

**This is correct behaviour.** The cache recognised that no inputs changed
(variables.yml, template package, code hashes, OpenStudio version) and
served every step from the SQLite cache. The only step that re-runs is
`GENERATE_BASIC_PLOTS` (plots are intentionally never cached).

**Expected speed-up:** 100×–300× for the cached steps. The warm run
completes in under 1 second (excluding plots).

### 3.4 Step timing and resource allocation

The `RUN_OPENSTUDIO_SIM` step is the bottleneck. Check its timing:

```bash
jq '.steps[] | select(.step == "RUN_OPENSTUDIO_SIM") | {elapsed_s, cache}' results/run.json
```

```json
{"elapsed_s": 4523.7, "cache": "MISS×N"}
```

**Interpretation:**

| Observation | Action |
|---|---|
| 500 samples × 4524s total ≈ 9s/sample | Reasonable for a small model with the local executor. |
| 500 samples × 4524s total ≈ 9s/sample with `executor: slurm` | Confirm parallelism — with 50 workers, wall-clock should be ~90s for 500 samples. If it's 4524s, jobs may be queuing. |
| Per-sample time > 30 min for a medium model | Normal. Large models (detailed HVAC, many thermal zones) can take 1–4 hours per sample. |
| Per-sample time < 1s and `executor: local` | Likely running in stub mode (`OSIMFLOW_STUB_SIM=1`). Check `config.openstudio_version`. |

**Per-sample time formula:**

```
per_sample_approx = steps[RUN_OPENSTUDIO_SIM].elapsed_s / config.n_samples
parallel_time_approx = per_sample_approx * ceil(n_samples / max_workers)
```

### 3.5 Cache invalidation — "I didn't change anything!"

**Scenario:** All steps show `MISS` or `MISS×N` but you didn't modify any
inputs.

**Common causes of unexpected cache invalidation:**

| Cause | What changed the cache key |
|---|---|
| Edited a `bin/*.py` script | The cache key includes a SHA-256 of every `bin/*.py` file. |
| Edited `osimflow/work.py` | The `work` module hash is also part of the cache key. |
| Changed `--openstudio_version` | The container digest changes with the version tag. |
| Changed `variables.yml` | The `GENERATE_LHS_SAMPLES` input hash changes. |
| Changed the `template_sim_package` | Per-sample input hashes change. |
| Moved the `--outdir` | Log file paths are hashed into cache keys. |

**How to confirm:** check the `config` section of `run.json` from both runs
and compare:

```bash
jq '.config' results_old/run.json > /tmp/old_config.json
jq '.config' results_new/run.json > /tmp/new_config.json
diff /tmp/old_config.json /tmp/new_config.json
```

### 3.6 All samples succeeded but no `eplusout_sql` entries

If `per_sample[].eplusout_sql` is missing from most entries but all
`status` values are `"ok"`:

- **On a warm run (cache hit):** the `eplusout_sql` path was recorded
  when the sample first ran. On cache hit, the cached result directory
  is returned, and the path should still be present. If missing, the
  simulation may have run in stub mode.
- **Check `config.openstudio_version`:** stub mode activates when the
  OpenStudio CLI is not on `PATH`. Set `OSIMFLOW_STUB_SIM=0` and ensure
  the CLI is installed to get real simulations.

---

## 4. Programmatic analysis

### 4.1 Essential `jq` queries

**Campaign summary (one-liner):**

```bash
jq '{id: .campaign_id, samples: .summary, total_s: .elapsed_s, executor: .config.executor}' results/run.json
```

**Find all failed samples:**

```bash
jq '[.per_sample[] | select(.status == "failed")]' results/run.json
```

**Count samples by status:**

```bash
jq '.per_sample | group_by(.status) | map({status: .[0].status, count: length})' results/run.json
```

**Step timing breakdown:**

```bash
jq '.steps | map({step, elapsed_s, cache}) | sort_by(-.elapsed_s)' results/run.json
```

**Extract error messages from failed samples:**

```bash
jq -r '[.per_sample[] | select(.error_summary) | "\(.sample_id): \(.error_summary)"] | join("\n")' results/run.json
```

**Sim step duration and per-sample estimate:**

```bash
jq '.steps[] | select(.step == "RUN_OPENSTUDIO_SIM") | {total_s: .elapsed_s, per_sample_s: (.elapsed_s / (input | .config.n_samples))}' results/run.json
```

**Find which step failed (exit_code != 0):**

```bash
jq '[.steps[] | select(.exit_code != 0)]' results/run.json
```

**Chaos injections that actually fired:**

```bash
jq '[.chaos_invocations[] | {step, when, faults: [.results[] | select(.injected) | .fault_type]}]' results/run.json
```

**Alerts that fired but were never delivered:**

```bash
jq '[.alerts_fired[] | select(.delivery_status != "delivered")]' results/run.json
```

**Circuit breakers that ended the campaign non-closed:**

```bash
jq '.circuit_breaker_states | with_entries(select(.value != "closed"))' results/run.json
```

**Total and per-sample cost:**

```bash
jq '{total_cost_usd, spot_savings_usd, cache_hit_rate}' results/run.json
jq '[.per_sample[] | {sample_id, cost_usd, billed_duration_seconds}] | sort_by(-.cost_usd) | .[0:5]' results/run.json
```

### 4.2 Python analysis script

```python
import json
import sys
from pathlib import Path

def analyze_run(path: Path) -> None:
    data = json.loads(path.read_text())

    print(f"Campaign: {data['campaign_id']}")
    print(f"Executor: {data['config']['executor']}")
    print(f"OpenStudio: {data['config']['openstudio_version']}")
    print(f"Wall-clock: {data['elapsed_s']:.1f}s")
    print()

    summary = data["summary"]
    print(f"Samples: {summary['n_samples']} total, "
          f"{summary['n_succeeded']} succeeded, "
          f"{summary['n_failed']} failed")
    print()

    print("Step timing:")
    for step in data["steps"]:
        cache_tag = f" ({step['cache']})" if step["cache"] != "MISS" else ""
        print(f"  {step['step']:30s}  {step['elapsed_s']:10.2f}s  "
              f"exit={step['exit_code']}{cache_tag}")
    print()

    failed = [s for s in data["per_sample"] if s["status"] == "failed"]
    if failed:
        print(f"Failed samples ({len(failed)}):")
        for s in failed:
            err = s.get("error_summary", "no error_summary")
            print(f"  {s['sample_id']}: {err}")
            logs = []
            if s.get("stderr_log"):
                logs.append(f"    stderr: {s['stderr_log']}")
            if s.get("stdout_log"):
                logs.append(f"    stdout: {s['stdout_log']}")
            if logs:
                print("\n".join(logs))
    else:
        print("All samples succeeded.")

    baseline = data.get("baseline_comparison")
    if baseline:
        print()
        print("Baseline comparison:")
        for k, v in baseline.items():
            print(f"  {k}: {v}")

if __name__ == "__main__":
    analyze_run(Path(sys.argv[1]))
```

Usage:

```bash
python analyze_run.py results/run.json
```

Sample output:

```
Campaign: 2026-06-10T14-30-00
Executor: local
OpenStudio: 3.11.0
Wall-clock: 55.3s

Samples: 500 total, 497 succeeded, 3 failed

Step timing:
  GENERATE_LHS_SAMPLES           0.45s  exit=0
  APPLY_PARAMETERS               1.20s  exit=0 (MISS×N)
  RUN_OPENSTUDIO_SIM            48.10s  exit=0 (MISS×N)
  EXTRACT_KPIS                   3.50s  exit=0 (MISS×N)
  AGGREGATE_RESULTS              0.80s  exit=0
  GENERATE_BASIC_PLOTS           1.25s  exit=0 (SKIPPED)

Failed samples (3):
  sample_042: SIM: RuntimeError('openstudio.cli returned 1')
    stderr: /path/to/results/work/sim/sample_042/stderr.log
    stdout: /path/to/results/work/sim/sample_042/stdout.log
  sample_187: SIM: RuntimeError('openstudio.cli returned 1')
    stderr: /path/to/results/work/sim/sample_187/stderr.log
    stdout: /path/to/results/work/sim/sample_187/stdout.log
  sample_331: APPLY: UnmappedParameterError('insulation_r_val')
```

---

## 5. MLflow integration

When `--mlflow_tracking_uri` is set, OSimFlow logs campaign parameters,
metrics, and artifacts to an MLflow tracking server:

| What is logged | MLflow entity |
|---|---|
| Campaign config (executor, version, n_samples) | Parameters |
| Wall-clock time, success/failure counts | Metrics |
| `aggregated_results.csv`, `failed_simulations.csv`, `run.json` | Artifacts |

**To find your run in the MLflow UI:**

1. Open the MLflow UI at the tracking URI (e.g. `http://localhost:5000`).
2. Navigate to the experiment associated with OSimFlow.
3. The MLflow run name matches the `campaign_id` from `run.json` (e.g.
   `2026-06-10T14-30-00`).
4. Cross-reference: the `run.json` artifact in MLflow is identical to
   `${outdir}/run.json` on disk.

**Quick cross-reference:**

```bash
# Get the campaign_id from run.json, then search MLflow
CAMPAIGN_ID=$(jq -r '.campaign_id' results/run.json)
mlflow runs list --experiment-name osimflow --filter "tags.mlflow.runName = '$CAMPAIGN_ID'"
```

---

## 6. Full example: complete `run.json`

Below is a synthetic but schema-accurate `run.json` from a 5-sample
campaign with 1 failure, run locally with OpenStudio 3.11.0:

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
    "n_samples": 5,
    "archive_intermediates": false,
    "custom_apply_script": null,
    "custom_kpi_extractor": null,
    "baseline_sample_id": null
  },
  "summary": {
    "n_samples": 5,
    "n_succeeded": 4,
    "n_failed": 1
  },
  "quality_summary": {
    "n_quality_failures": 0,
    "n_quality_warnings": 1,
    "n_quality_ok": 4
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
      "cache": "MISS×N",
      "elapsed_s": 1.20,
      "exit_code": 0
    },
    {
      "step": "RUN_OPENSTUDIO_SIM",
      "cache": "MISS×N",
      "elapsed_s": 48.10,
      "exit_code": 0
    },
    {
      "step": "EXTRACT_KPIS",
      "cache": "MISS×N",
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
      "stdout_log": "/tmp/results/work/sim/sample_001/stdout.log",
      "stderr_log": "/tmp/results/work/sim/sample_001/stderr.log"
    },
    {
      "sample_id": "sample_002",
      "status": "ok",
      "elapsed_s": 0.0,
      "apply_exit_code": 0,
      "sim_exit_code": 0,
      "extract_exit_code": 0,
      "eplusout_sql": "/tmp/results/work/sim/sample_002/eplusout.sql",
      "stdout_log": "/tmp/results/work/sim/sample_002/stdout.log",
      "stderr_log": "/tmp/results/work/sim/sample_002/stderr.log"
    },
    {
      "sample_id": "sample_003",
      "status": "ok",
      "elapsed_s": 0.0,
      "apply_exit_code": 0,
      "sim_exit_code": 0,
      "extract_exit_code": 0,
      "eplusout_sql": "/tmp/results/work/sim/sample_003/eplusout.sql",
      "stdout_log": "/tmp/results/work/sim/sample_003/stdout.log",
      "stderr_log": "/tmp/results/work/sim/sample_003/stderr.log"
    },
    {
      "sample_id": "sample_004",
      "status": "failed",
      "elapsed_s": 0.0,
      "apply_exit_code": 0,
      "sim_exit_code": 1,
      "extract_exit_code": 1,
      "error_summary": "SIM: RuntimeError('openstudio.cli returned 1')",
      "worker_id": "local",
      "cost_usd": 0.0004,
      "trace_id": "trace-8c9d0e1f",
      "stdout_log": "/tmp/results/work/sim/sample_004/stdout.log",
      "stderr_log": "/tmp/results/work/sim/sample_004/stderr.log"
    }
  ],
  "total_cost_usd": 0.0089,
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
      "message": "failure rate 20% exceeds threshold 5%",
      "delivery_status": "delivered",
      "timestamp": 1749565854.1
    }
  ]
}
```

---

## 7. Troubleshooting quick reference

| What you see | What it means | What to do |
|---|---|---|
| `summary.n_failed > 0` | Some samples failed. | Run the `jq` query from §3.2 to find which ones and why. |
| All steps `HIT`/`HIT×N` | Cache resume — nothing re-ran. | This is correct. Only `GENERATE_BASIC_PLOTS` re-runs. |
| All steps `MISS`/`MISS×N` on second run | Cache was invalidated. | Check the cache invalidation table in DEVELOPMENT.md §11.2. Likely a code or input change. |
| `RUN_OPENSTUDIO_SIM` takes < 1s per sample | Stub mode. | Check `config.openstudio_version` and ensure the CLI is installed, or set `OSIMFLOW_STUB_SIM=0`. |
| `finished_at: null` | Campaign crashed before finalization. | Check the terminal output for the exception. `run.json` was not written. |
| `exit_code: 1` on `GENERATE_LHS_SAMPLES` | Invalid `variables.yml`. | Check the YAML syntax and distribution parameters. |
| `exit_code: 1` on `APPLY_PARAMETERS` | Pre-flight check failed. | Check for `UnmappedParameterError` or `AmbiguousParameterError` in the logs. |
| Missing `per_sample` entries | Campaign failed before samples ran. | Check `steps[].exit_code` to find which step failed first. |
| `circuit_breaker_states` has an `"open"` entry | Redis was unavailable. | The campaign ran on local fallbacks; cross-worker coordination was degraded. See `distributed-cache.md`. |
| `alerts_fired[].delivery_status` is `"failed"` | Alert destinations were unreachable. | Check your `--alert-destinations` / webhook configuration. |
| `chaos_invocations` entries with `injected: false` | Fault did not fire. | Verify the chaos scenario and `--chaos-schedule` — see `chaos-engine.md`. |

---

## 8. Related documentation

| Document | Content |
|---|---|
| [`monitoring-schema.md`](monitoring-schema.md) | Formal schema reference (all fields, types, versioning). |
| [`DEVELOPMENT.md`](DEVELOPMENT.md) | Developer workflow, build commands, CI. |
| [`.agents/results/monitoring-decision.md`](../.agents/results/monitoring-decision.md) | Design decision: why OSimFlow uses BYO `run.json` monitoring. |
| [`eplusout-sql-guide.md`](eplusout-sql-guide.md) | Interpreting the EnergyPlus SQL output that feeds KPI extraction. |

---

## 9. Source reference

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
