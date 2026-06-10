# Resource Allocation Guide

This guide explains how OSimFlow's per-step resource directives (`cpus`, `memory_mb`, `time_min`) map to each executor substrate, provides recommended defaults per DAG step, and offers tuning guidance for real workloads.

---

## Resource Parameters

Every `executor.submit()` call accepts three resource directives:

| Parameter | Unit | Controls |
|---|---|---|
| `cpus` | integer cores | Number of CPU cores allocated to the task |
| `memory_mb` | megabytes (MiB) | Memory ceiling for the task |
| `time_min` | integer minutes | Wall-clock time limit before the task is killed |

On the **LocalExecutor** these are advisory (logged but not enforced). On **Slurm**, **AWS Batch**, and **Nomad** they are enforced by the substrate scheduler.

---

## Per-Step Defaults

The `DEFAULT_STEP_RESOURCES` constant in `osimflow/executors/__init__.py` defines the built-in defaults used when the Campaign does not specify explicit overrides:

| DAG Step | `cpus` | `memory_mb` | `time_min` | Rationale |
|---|---|---|---|---|
| `GENERATE_LHS_SAMPLES` | 1 | 2048 | 5 | Single-shot; `scipy.stats.qmc` is fast and single-threaded. |
| `APPLY_PARAMETERS` | 1 | 512 | 10 | Per-sample file I/O (copy template, mutate `.osw`/`.osm`). Very lightweight. |
| `RUN_OPENSTUDIO_SIM` | 4 | 8192 | 240 | The heavy step. EnergyPlus is largely single-threaded but benefits from extra cores for SQL queries and post-processing. 8 GB covers most models up to ~50 zones. |
| `EXTRACT_KPIS` | 1 | 2048 | 10 | Per-sample SQLite read + JSON write. Low CPU; memory proportional to `.sql` size. |
| `AGGREGATE_RESULTS` | 2 | 4096 | 15 | One-shot; loads all KPI JSONs + reads `eplusout.err` summaries. Memory scales with sample count. |
| `GENERATE_BASIC_PLOTS` | 1 | 2048 | 10 | `matplotlib` / `seaborn` rendering. Low CPU; modest memory for the DataFrame. |

### How to Override

**Per-campaign** via `CampaignConfig`: not yet exposed as CLI flags. The current mechanism is programmatic — pass resource kwargs directly to `executor.submit()` in a custom `Campaign` subclass.

**Per-executor** via constructor defaults: `SlurmExecutor(cpus_per_task=4, mem_gb=8, time_h=2)` sets the baseline that per-submit overrides build on.

---

## Mapping Tables

### OSimFlow → Slurm (submitit)

| OSimFlow | submitit parameter | `#SBATCH` directive |
|---|---|---|
| `cpus=4` | `slurm_cpus_per_task=4` | `#SBATCH --cpus-per-task=4` |
| `memory_mb=8192` | `slurm_mem_gb=8` | `#SBATCH --mem=8GB` |
| `time_min=240` | `slurm_time=240` | `#SBATCH --time=04:00:00` |

Notes:
- `memory_mb` is converted to GB (rounded up) via `max(1, (memory_mb + 1023) // 1024)`.
- `time_min` maps directly to `slurm_time` in minutes; submitit formats it as `HH:MM:SS` in the `#SBATCH` header.
- Advanced directives (`qos`, `constraint`, `gres`) are forwarded from `SlurmExecutor` constructor and applied to every submission. Requires submitit >= 1.5.

### OSimFlow → AWS Batch (Boto3)

| OSimFlow | Boto3 field | API location |
|---|---|---|
| `cpus=4` | `vcpus=4` | `containerOverrides.vcpus` |
| `memory_mb=8192` | `memory=8192` | `containerOverrides.memory` (MiB) |
| `time_min=240` | `attemptDurationSeconds=14400` | `timeout.attemptDurationSeconds` |

Notes:
- Memory is passed 1:1 (MB ≈ MiB for Batch's purposes).
- `time_min` is converted to seconds (`time_min * 60`).
- The vCPU-to-memory ratio in the job definition must accommodate the maximum you request. AWS Batch jobs fail immediately if the job definition's memory is lower than the override.

### OSimFlow → Nomad

| OSimFlow | Nomad field | API location |
|---|---|---|
| `cpus=4` | `CPU=4000` | `Task.Resources.CPU` (in MHz; 1 cpu = 1000 MHz) |
| `memory_mb=8192` | `MemoryMB=8192` | `Task.Resources.MemoryMB` |
| `time_min=240` | *(not directly mapped)* | Task-level `KillTimeout` at the group level |

Notes:
- Nomad uses MHz for CPU allocation; OSimFlow multiplies `cpus * 1000`.
- `time_min` is advisory on Nomad — the `KillTimeout` is set at the task group level, not per-submission.

### OSimFlow → Local

| OSimFlow | Behavior |
|---|---|
| `cpus` | Logged; no enforcement (thread pool ignores it). |
| `memory_mb` | Logged; no enforcement. |
| `time_min` | Logged; no enforcement. Use `OSIMFLOW_STUB_SIM=1` for local testing. |

---

## Scaling Guidelines

### By Model Complexity

| Model Type | Zones | Recommended `cpus` | Recommended `memory_mb` | Recommended `time_min` |
|---|---|---|---|---|
| Small residential (1-zone) | 1–5 | 2 | 4096 | 60 |
| Medium residential (5–20 zones) | 5–20 | 4 | 8192 | 120 |
| Large commercial (20–100 zones) | 20–100 | 4 | 16384 | 240 |
| Complex campus (100+ zones) | 100+ | 8 | 32768 | 480 |

### By Number of Samples

The per-sample steps (`APPLY_PARAMETERS`, `RUN_OPENSTUDIO_SIM`, `EXTRACT_KPIS`) fan out. The batch steps (`GENERATE_LHS_SAMPLES`, `AGGREGATE_RESULTS`, `GENERATE_BASIC_PLOTS`) run once.

- **AGGREGATE_RESULTS** memory scales with sample count: loading all KPI JSONs into a single `pandas` DataFrame. For >1000 samples, increase `memory_mb` to 8192.
- **GENERATE_BASIC_PLOTS** is cheap regardless of sample count — `matplotlib` renders from the aggregated CSV, not the raw data.

---

## OpenStudio-Specific Considerations

1. **EnergyPlus is largely single-threaded.** The simulation engine itself uses one core. Extra CPUs help with:
   - Post-processing SQL queries (`eplusout.sql` is SQLite; concurrent reads are possible).
   - OpenStudio measure application (Ruby/Python measures that do file I/O).
   - Container overhead (the OS and daemons share the cores).

2. **Memory is the bottleneck, not CPU.** EnergyPlus loads the entire building model into memory. A 50-zone commercial model typically needs 4–8 GB. A 200-zone campus model can exceed 16 GB.

3. **`eplusout.sql` can be large.** For models with hourly or sub-hourly output, the SQLite file can reach 100+ MB per sample. The `EXTRACT_KPIS` step reads this file; allocate enough memory to hold it.

4. **Time limits are safety nets.** A healthy residential simulation finishes in 5–30 minutes. A large commercial model can take 1–4 hours. Set `time_min` to 2–3x your expected runtime to handle occasional slow convergence.

---

## Troubleshooting

### OOM Kills (Exit Code 137 / 137)

**Symptom:** Tasks fail with no useful error message; exit code 137 (or `OOM killed` in Nomad events).

**Fix:** Increase `memory_mb` for `RUN_OPENSTUDIO_SIM`. Start with 2x the current value. On Slurm, check `sacct -j <jobid> --format=MaxRSS` to see peak memory usage.

### Timeouts

**Symptom:** Tasks fail with `TIMEOUT` (Slurm) or the Batch `statusReason` mentions duration.

**Fix:** Increase `time_min`. On Slurm, check `sacct` for `Elapsed` vs `Timelimit`. On AWS Batch, the `attemptDurationSeconds` is the hard cap — there is no graceful extension.

### Underutilization (CPU < 20%)

**Symptom:** Jobs complete successfully but `sacct` shows low CPU usage.

**Fix:** Reduce `cpus` for the step. EnergyPlus rarely benefits from >4 cores. For `APPLY_PARAMETERS` and `EXTRACT_KPIS`, 1 core is always sufficient.

### Slurm Queue Delays

**Symptom:** Jobs spend a long time in `PENDING` state.

**Fix:** This is a scheduler backlog, not an OSimFlow issue. Options:
- Request fewer resources (lower `cpus`, `memory_mb`) — smaller jobs backfill more easily.
- Use `--slurm-qos` for a higher-priority queue.
- Reduce `time_min` — shorter jobs are easier for the scheduler to place.

### AWS Batch Job Definition Mismatch

**Symptom:** Batch jobs fail immediately with a resource error.

**Fix:** Ensure the job definition's `resourceRequirements` (vCPU and memory) are at least as large as the maximum `cpus` and `memory_mb` OSimFlow will request via `containerOverrides`. The override cannot exceed the job definition limits.

---

## Cross-References

- [Slurm Deployment Guide](deployment/slurm.md) — full Slurm setup instructions.
- [AWS Batch Deployment Guide](deployment/aws-batch.md) — full Batch setup instructions.
- [Cost Estimation Guide](cost-estimation.md) — per-campaign pricing estimates.
- [AGENTS.md §8.9](../AGENTS.md) — cache invalidation on `bin/*.py` edits.
- `osimflow/executors/__init__.py` — `DEFAULT_STEP_RESOURCES` constant and per-executor mapping logic.
- `osimflow/campaign.py` — per-step resource values passed to `executor.submit()`.
