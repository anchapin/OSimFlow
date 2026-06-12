# OSimFlow Campaign Orchestration

Guides AI agents through running, configuring, and troubleshooting OSimFlow parametric building-energy simulation campaigns. Covers the full campaign lifecycle: from CLI invocation through the 7-step DAG to result collection and resume semantics.

## Triggers

- "run a campaign", "run a simulation", "run osimflow"
- "parametric study", "parametric simulation", "simulation sweep"
- "osimflow run", "campaign execution"
- "LHS samples", "Latin Hypercube", "sampling strategy"
- "executor selection", "local executor", "slurm executor", "aws batch"
- "BYOS", "bring your own script", "custom script"
- "resume campaign", "cache hit", "partial run"
- "variables.yml", "template_sim_package"

## Quick Reference

### CLI Entry Point

```bash
osimflow run \
  --executor <local|slurm|aws_batch|nomad> \
  --input_variables variables.yml \
  --template_sim_package ./example_package \
  --n_samples <N> \
  --outdir ./results \
  --openstudio_version 3.11.0
```

### Key Files

| File | Purpose |
|---|---|
| `osimflow/campaign.py` | `Campaign` class — the orchestrator (~300 LoC) |
| `osimflow/config.py` | `CampaignConfig` dataclass + `load_config()` |
| `osimflow/work.py` | Per-step work functions (BYOS overridable) |
| `osimflow/__main__.py` | CLI parser and entry point |
| `osimflow/cache.py` | `SQLiteCache` + `CacheKey` — resume semantics |
| `osimflow/monitoring.py` | `RunTrace` — writes `run.json` |

### The 7-Step DAG

The `Campaign` class drives this exact sequence:

1. **GENERATE_LHS_SAMPLES** — single-shot; produces `samples.json`
2. **PREFLIGHT_RUN_MODEL** — single-shot; validates seed model before cloud spend
3. **APPLY_PARAMETERS** — fan-out over N samples
4. **RUN_OPENSTUDIO_SIM** — fan-out over N samples (heavy, 5 min – 4 h each)
5. **EXTRACT_KPIS** — fan-out over N samples
6. **AGGREGATE_RESULTS** — single-shot; produces `aggregated_results.csv` + `failed_simulations.csv`
7. **GENERATE_BASIC_PLOTS** — single-shot; produces PNG/PDF plots

Steps 3–5 fan out in parallel across the executor; steps 6–7 wait for all fan-out tasks.

### Common CLI Flags

| Flag | Default | Purpose |
|---|---|---|
| `--executor` | `local` | Execution backend |
| `--max-workers` | CPU count | Local executor parallelism |
| `--n_samples` | required | Number of LHS samples |
| `--openstudio_version` | `3.11.0` | Container image tag |
| `--algorithm` | `lhs` | Sampling strategy |
| `--dry-run` | off | Force LocalExecutor, 1 sample, steps 1–4 only |
| `--sample N` | off | Re-run single sample from existing `samples.json` |
| `--skip-preflight` | off | Skip PREFLIGHT_RUN_MODEL step |
| `--max-generations` | `1` | DAG generations (iterative algorithms) |
| `--mlflow_tracking_uri` | off | Optional MLflow logging |
| `--custom_apply_script` | off | BYOS parameter application |
| `--custom_kpi_extractor` | off | BYOS KPI extraction |
| `--log_level` | `INFO` | Logging verbosity |

## Detailed Guide

### Running a Campaign (Local)

For development, testing, or small studies:

```bash
osimflow run \
  --executor local \
  --max-workers 4 \
  --input_variables variables.yml \
  --template_sim_package ./example_package \
  --n_samples 10 \
  --outdir ./results \
  --openstudio_version 3.11.0
```

The local executor runs steps in a `ThreadPoolExecutor`. Each step's stdout/stderr lands at `${outdir}/work/sim/<sample_id>/{stdout,stderr}.log`.

### Running on Slurm

```bash
# Development (uses submitit.DebugExecutor — runs locally, no cluster needed)
osimflow run \
  --executor slurm \
  --input_variables variables.yml \
  --n_samples 5 \
  --outdir ./results

# Production (real Slurm)
osimflow run \
  --executor slurm \
  --slurm-real \
  --slurm_partition short \
  --slurm_account myproject \
  --openstudio_version 3.11.0 \
  --input_variables variables.yml \
  --n_samples 500 \
  --outdir ./results
```

**Important:** Without `--slurm-real`, jobs run locally via `submitit.DebugExecutor`. Always pass `--slurm-real` in production.

Advanced Slurm directives (requires `submitit >= 1.5`):

```bash
osimflow run \
  --executor slurm \
  --slurm-real \
  --slurm_partition gpu \
  --slurm_qos high \
  --slurm_constraint gpu \
  --slurm_gres gpu:1 \
  --openstudio_version 3.11.0 \
  --input_variables variables.yml \
  --n_samples 200
```

### Running on AWS Batch

Prerequisites:
1. `pip install osimflow[aws]` (brings in `boto3`)
2. Registered Batch job definition with matching container image
3. AWS credentials from IAM role on the compute environment
4. `AWS_REGION` set in environment

```bash
osimflow run \
  --executor aws_batch \
  --aws-batch-queue osimflow-batch-queue \
  --aws-batch-job-definition osimflow-openstudio-job-def \
  --openstudio_version 3.11.0 \
  --input_variables variables.yml \
  --template_sim_package ./example_package \
  --n_samples 1000 \
  --outdir ./results \
  --archive_intermediates
```

Spot pricing controls (issue #131):
- `--aws-batch-max-spot-price-usd <USD>` — Spot price ceiling per vCPU-hour
- `--aws-batch-fallback-to-on-demand` — Fall back to on-demand when Spot fails
- `--aws-batch-max-retries 3` — Max Spot interruption retries

### Dry-Run Mode

Quick validation without real simulation:

```bash
osimflow run \
  --dry-run \
  --input_variables variables.yml \
  --template_sim_package ./example_package \
  --outdir ./dry-results
```

Forces `LocalExecutor`, 1 sample, steps 1–4 only.

### BYOS (Bring Your Own Script)

Override default per-step logic with user-provided scripts:

```bash
osimflow run \
  --executor local \
  --custom_apply_script user_scripts/my_apply.py \
  --custom_kpi_extractor user_scripts/my_kpis.py \
  --input_variables variables.yml \
  --template_sim_package ./example_package \
  --n_samples 10 \
  --outdir ./results
```

**BYOS contract:**
- Scripts live in `user_scripts/`
- The Campaign loads them via `importlib.util`
- Function signatures are validated with `inspect.signature`
- See `user_scripts/README.md` for the exact function signatures

### Cache and Resume

Re-running with the same `--outdir` is a cache hit on every completed step:

```bash
# First run: ~50s for 5 samples
osimflow run --executor local --n_samples 5 --outdir ./results ...

# Second run: ~0.1s — all steps are cache hits
osimflow run --executor local --n_samples 5 --outdir ./results ...
```

**Cache key composition:**
- Step name + input parameters + code hash of `bin/*.py` files
- Editing a `bin/*.py` script invalidates the cache for that step
- The `run.json` file tracks per-step cache hit/miss status

**Single-sample re-run:**

```bash
osimflow run --sample 42 --outdir ./results ...
```

Re-runs sample N from existing `samples.json` — useful for debugging a single failure.

### Monitoring

The campaign writes `${outdir}/run.json` with:
- Per-step timing (start, end, duration)
- Per-sample status (success, failed, cache_hit)
- Error summaries for failed samples

This is the primary monitoring artifact. No external service required.

Optional MLflow integration:

```bash
pip install osimflow[mlflow]
osimflow run --mlflow_tracking_uri http://localhost:5000 ...
```

### Sampling Algorithms

The `--algorithm` flag dispatches through `AlgorithmRegistry`:

| Algorithm | Flag | Type |
|---|---|---|
| Latin Hypercube | `--algorithm lhs` (default) | Single-shot |
| Sobol | `--algorithm sobol` | Single-shot |
| Halton | `--algorithm halton` | Single-shot |
| Differential Evolution | `--algorithm de` | Iterative |
| Dual Annealing | `--algorithm da` | Iterative |
| NSGA-II | `--algorithm nsga2` | Iterative (requires `[optimization]`) |
| PSO | `--algorithm pso` | Iterative (requires `[optimization]`) |
| Morris | `--algorithm morris` | Single-shot (requires `[sensitivity]`) |
| FAST99 | `--algorithm fast99` | Single-shot (requires `[sensitivity]`) |

Iterative algorithms use `--max-generations` to control the loop count (default: 1).

## Common Patterns

### Minimal variables.yml

```yaml
variables:
  - name: "InsulationRValue"
    distribution: "uniform"
    min: 2.0
    max: 10.0
    measure_argument: "InsulationMeasure.r_value"

  - name: "WindowSHGC"
    distribution: "uniform"
    min: 0.2
    max: 0.6
    measure_argument: "WindowMeasure.shgc"
```

### Check campaign status

```bash
# View the monitoring trace
cat ./results/run.json | python -m json.tool

# Check for failed simulations
cat ./results/failed_simulations.csv
```

### Init and finalize hooks

```bash
osimflow run \
  --init-script ./setup_env.sh \
  --finalize-script ./cleanup.sh \
  --executor slurm \
  --slurm-real \
  ...
```

Shell hooks run before and after the campaign DAG. Useful for environment setup and cleanup.

## Gotchas

1. **Missing `workflow.osw`** — When `openstudio.cli` is available but no `workflow.osw` exists in the `modified_sim_package`, the work function raises `RuntimeError`. The `template_sim_package` must always contain a `workflow.osw`.

2. **OpenStudio version lives in the container tag** — Version is set via `--openstudio_version`, which becomes the `nrel/openstudio:<version>` container tag. It is NOT in `variables.yml` or environment variables.

3. **`--slurm-real` required in production** — Without it, `submitit.DebugExecutor` runs everything locally. This is intentional for development but a silent failure in production.

4. **Large `eplusout.err` files** — The campaign deletes these from the work directory on successful simulation. Don't disable this unless you have disk space to spare.

5. **Cache invalidation on `bin/*.py` edits** — The cache key includes SHA-256 of every `bin/*.py` file. Editing a script invalidates the cache for the affected step. Do NOT bypass this hashing.

6. **Stub vs real OpenStudio CLI** — `run_openstudio_sim` invokes `openstudio.cli run -w workflow.osw` when the CLI is on PATH. When unavailable, it falls back to a stub (sleep + placeholder). Set `OSIMFLOW_STUB_SIM=1` to force stub mode. Set `OSIMFLOW_RUN_REAL_OPENSTUDIO=1` for real E2E tests.

7. **AWS Batch security** — IAM roles only. No long-lived access keys. The executor sources credentials from the compute environment IAM role.

8. **Pre-flight parameter validation** — `step_apply_parameters` verifies every LHS variable maps to an existing measure argument or `.osm` attribute before simulation runs. Fail fast with clear error.

9. **Executor resource directives** — `cpus`, `memory_mb`, `time_min` are advisory on `LocalExecutor`, propagated to Slurm via `submitit`, translated to Boto3 `containerOverrides` for AWS Batch. Extend the `submit()` signature to add new resource kinds.

10. **Per-sample stdout/stderr** — Located at `${outdir}/work/sim/<sample_id>/{stdout,stderr}.log`. Check these first when a sample fails.
