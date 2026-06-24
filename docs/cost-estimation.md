# Cost Estimation & Optimization Guide

This guide helps you estimate and optimize the cost of running OSimFlow campaigns on AWS Batch and Slurm.

## Core Formula

The total compute cost for a campaign is:

```
total_cost = N_samples × time_per_sim_hours × hourly_rate_per_job
```

Where `hourly_rate_per_job` depends on the executor and instance type:

```
hourly_rate_per_job = (vCPUs × price_per_vCPU_hour) + (memory_GB × price_per_GB_hour)
```

For a typical OpenStudio simulation run via OSimFlow (4 vCPUs, 8 GB memory, ~30 min per sample):

```
per_sample_cost = 0.5 hours × hourly_rate_per_job
```

## AWS Batch Cost Model

### Compute Pricing (US-East-1, as of early 2025)

| Instance Family | Use Case | vCPU-Hour (On-Demand) | vCPU-Hour (Spot) | Spot Savings |
|---|---|---|---|---|
| **m5** (general) | Default simulations | ~$0.096 | ~$0.029 | ~70% |
| **c5** (compute) | CPU-heavy models | ~$0.085 | ~$0.026 | ~69% |
| **r5** (memory) | Large models (>16 GB) | ~$0.126 | ~$0.038 | ~70% |

Memory is priced at the same per-vCPU rate (included in the vCPU-hour price for Batch).

### Per-Sample Cost Formula (AWS Batch)

```python
cost_per_sample = (
    vcpus * price_per_vcpu_hour
    + memory_gb * price_per_gb_hour   # often $0 in Batch (bundled)
    + storage_gb * price_per_gb_month / 720  # EBS, amortized
) * time_hours
```

In practice, AWS Batch pricing is dominated by vCPU-hours. For OSimFlow's default simulation step (4 vCPUs, 8 GB memory, 240 min `time_min` cap):

| Instance | Pricing | 30-min sim | 1-hr sim | 2-hr sim |
|---|---|---|---|---|
| m5 On-Demand | $0.096/vCPU-hr | $0.19 | $0.38 | $0.77 |
| m5 Spot | $0.029/vCPU-hr | $0.06 | $0.12 | $0.23 |
| c5 Spot | $0.026/vCPU-hr | $0.05 | $0.10 | $0.21 |

### Additional AWS Costs

| Cost Component | Rate | Notes |
|---|---|---|
| EBS storage | ~$0.08/GB/month | Per-task scratch; ~20 GB per sim |
| S3 storage | $0.023/GB/month | Archived results + intermediates |
| Data transfer (out) | $0.09/GB (first 10 TB) | Downloading results out of AWS |
| ECR | $0.10/GB/month | If hosting a custom image |
| Docker Hub pulls | 100/6 hrs (free tier) | NREL `nrel/openstudio` image; may hit rate limits on large campaigns |

### Spot Instance Strategy

OSimFlow workloads are **embarrassingly parallel** and **checkpointable via the cache** — ideal for Spot:

1. **Use Spot for all simulation steps.** The `RUN_OPENSTUDIO_SIM` step is the cost center (4 vCPUs × N samples). A Spot interruption only loses the interrupted sample; the cache preserves all completed samples.
2. **Set `time_min` conservatively.** The Campaign's `step_run_openstudio_sim` passes `time_min=240` (4 hours) to the executor. If your simulations typically finish in 30 minutes, Spot is very safe — most interruptions happen on timescales of hours, not minutes.
3. **Rerun after interruption.** Re-running with the same `--outdir` hits the cache on every completed sample. The cost of a full Spot interruption is only the cost of re-running the interrupted samples.
4. **Batch compute environment configuration.** Set your Batch compute environment to use Spot instances with a fallback to On-Demand (`BEST_FIT_PROGRESSIVE` allocation strategy). This maximizes savings while ensuring the campaign eventually completes.

Configure Spot in your Batch compute environment (not in OSimFlow itself — the executor submits jobs; the compute environment decides the pricing):

```bash
aws batch create-compute-environment \
  --compute-environment-name osimflow-spot \
  --type MANAGED \
  --compute-resources '
    {
      "type": "SPOT",
      "allocationStrategy": "BEST_FIT_PROGRESSIVE",
      "minvCpus": 0,
      "maxvCpus": 256,
      "desiredvCpus": 0,
      "instanceTypes": ["m5", "c5"],
      "subnets": ["subnet-xxx"],
      "securityGroupIds": ["sg-xxx"],
      "instanceRole": "arn:aws:iam::xxx:instance-profile/ecsInstanceRole"
    }
  '
```

## Slurm Cost Model

Slurm costs depend on whether you run on an **on-premise cluster** or a **cloud-based Slurm** (e.g., AWS ParallelCluster, SLURM on GCP).

### On-Premise Slurm

On-premise clusters typically have **no per-job monetary cost** — the hardware is already purchased. The relevant costs are:

| Cost | Description |
|---|---|
| **Chargeback rate** | Many institutions charge departments per CPU-hour (e.g., $0.02–$0.10/CPU-hour). Check with your HPC admin. |
| **Opportunity cost** | Your fairshare allocation consumed by a campaign is unavailable to other users. |
| **Storage** | Home directory and scratch filesystem quotas. |

### Cloud-Based Slurm (AWS ParallelCluster, etc.)

Cloud Slurm costs are the same EC2 prices as AWS Batch, plus:

| Cost Component | Rate | Notes |
|---|---|---|
| Head node | ~$0.05–$0.10/hr | Always-on; shut down when not running campaigns |
| Shared storage (EFS) | ~$0.30/GB/month | Shared home directory across compute nodes |
| Cluster management | — | ParallelCluster is free; you pay for the EC2 instances |

### Partition Selection

OSimFlow's `--slurm-partition` flag controls which Slurm queue receives jobs:

```bash
# Short partition: lower wall-time limit, faster scheduling
osimflow run --executor slurm --slurm-real --slurm-partition short ...

# GPU partition: for future GPU-accelerated OpenStudio workflows
osimflow run --executor slurm --slurm-real --slurm-partition gpu \
  --slurm-constraint gpu --slurm-gres gpu:1 ...
```

**Partition sizing tip:** Don't submit 10,000 jobs to a partition with a max of 100 concurrent slots — Slurm will queue the rest, adding latency. Estimate your concurrency:

```
max_concurrent = min(N_samples, partition_max_jobs)
campaign_wall_time = ceil(N_samples / max_concurrent) × time_per_sim
```

### Fairshare Considerations

On shared Slurm clusters, your fairshare score determines scheduling priority. To minimize impact on other users:

1. **Use `--slurm-account`** if your cluster requires account-based billing.
2. **Submit during off-peak hours** for large campaigns (>500 samples).
3. **Set realistic `time_min`.** Overestimating wall-time hurts your fairshare; underestimating kills jobs. Start with a 3-sample pilot run to measure actual wall-time, then add a 25% buffer.

## Resource Directives

OSimFlow's Campaign passes per-step resource directives to the executor via `submit()`:

| Step | Default vCPUs | Default Memory | Default Time | Container |
|---|---|---|---|---|
| `GENERATE_LHS_SAMPLES` | 1 | 1024 MB | 5 min | scientific_python |
| `APPLY_PARAMETERS` | 1 | 512 MB | 5 min | scientific_python |
| `RUN_OPENSTUDIO_SIM` | **4** | **8192 MB** | **240 min** | nrel/openstudio |
| `EXTRACT_KPIS` | 1 | 1024 MB | 10 min | scientific_python |
| `AGGREGATE_RESULTS` | 2 | 4096 MB | 15 min | scientific_python |
| `GENERATE_BASIC_PLOTS` | 1 | 1024 MB | 10 min | scientific_python |

The `RUN_OPENSTUDIO_SIM` step dominates cost — it runs N times with the heaviest resource allocation. The other steps are lightweight by comparison.

### How Resources Flow to Executors

| Parameter | LocalExecutor | SlurmExecutor | AWSBatchExecutor |
|---|---|---|---|
| `cpus` | Advisory (logged) | `#SBATCH --cpus-per-task` | `containerOverrides.vcpus` |
| `memory_mb` | Advisory (logged) | `#SBATCH --mem` (converted to GB, rounded up) | `containerOverrides.memory` (MiB) |
| `time_min` | Advisory (logged) | `#SBATCH --time` (minutes) | `timeout.attemptDurationSeconds` (converted to seconds) |

On `LocalExecutor`, resource directives are advisory — the jobs run in a thread pool with `--max-workers` controlling parallelism, not per-job CPU/memory limits.

### Right-Sizing

The defaults (4 vCPUs, 8 GB memory) are conservative. To reduce costs:

1. **Profile a 3-sample pilot.** Run a small campaign and check actual CPU/memory usage in `run.json` or CloudWatch/Slurm accounting.
2. **Reduce `cpus` if OpenStudio doesn't use them.** EnergyPlus parallelism is model-dependent; some models top out at 2 threads. Reducing from 4 to 2 vCPUs halves the vCPU-hour cost.
3. **Reduce `memory_mb` if possible.** Models under 500 zones typically use < 4 GB. Large models (>2000 zones) may need the full 8 GB.

Note: Per-step resource overrides are set in `osimflow/campaign.py`. To customize, modify the `cpus`, `memory_mb`, and `time_min` arguments in the `executor.submit()` calls, or use the BYOS interface to provide your own work functions.

## Cost Estimation Examples

All examples assume the `RUN_OPENSTUDIO_SIM` step dominates cost (which it does — the other steps combined are <5% of total compute). Estimates use m5 Spot pricing ($0.029/vCPU-hr) and a 4-vCPU, 30-minute-per-sim configuration.

### Formula

```
per_sample_cost = 4 vCPUs × $0.029/vCPU-hr × 0.5 hr = $0.058
total_sim_cost  = N_samples × $0.058
```

### Worked Examples

| Samples | Sim Time/Sample | Spot Cost (m5) | On-Demand Cost (m5) | Notes |
|---|---|---|---|---|
| **100** | 30 min | **$5.80** | $19.20 | Small design study |
| **100** | 1 hr | $11.60 | $38.40 | Complex models |
| **500** | 30 min | **$29** | $96 | Parametric sweep |
| **500** | 1 hr | $58 | $192 | Large sweep, heavy models |
| **1,000** | 30 min | **$58** | $192 | Full LHS campaign |
| **1,000** | 1 hr | $116 | $384 | Standard production run |
| **5,000** | 30 min | **$290** | $960 | Large-scale optimization |
| **5,000** | 2 hr | $1,160 | $3,840 | Worst case: complex + long |

### Storage Costs (per campaign)

| Component | Size Estimate | Monthly S3 Cost |
|---|---|---|
| Campaign results (CSV/plots) | <10 MB | Negligible |
| Per-sample KPI JSONs (N=1000) | ~50 MB | Negligible |
| Archived intermediates (`--archive_intermediates`) | 100 GB (1000 × 100 MB `eplusout.sql`) | **~$2.30/mo** |
| Full archive (`.osw` + `.osm` + `.sql`) | 200 GB | ~$4.60/mo |

## Cost Reduction Strategies

### 1. Cache Reuse (Biggest Savings)

OSimFlow's `SQLiteCache` makes re-runs essentially free. The cache key includes:

- Input hash (variables.yml, parameter values)
- Code hash (all `bin/*.py` + `osimflow/work.py`)
- OpenStudio version
- Container image digest

**Re-running a completed campaign costs ~$0** — every step is a cache hit. This means:

- Iterative KPI extraction is free (change `bin/extract_kpis.py`, re-run — only the extract step re-runs).
- Spot interruptions are cheap (re-run to pick up interrupted samples).
- Development is free (run once on Spot, iterate on plots/analysis locally).

### 2. Spot/Preemptible Instances

Use Spot for all production campaigns (see [Spot Instance Strategy](#spot-instance-strategy)). Savings: **60–70%**.

### 3. Right-Size Resources

| Change | Savings | Risk |
|---|---|---|
| 4 → 2 vCPUs | 50% compute cost | Slower sim if model uses >2 threads |
| 8 → 4 GB memory | None on Batch (bundled) | OOM kill if model exceeds memory |
| 240 → 60 min `time_min` | None (only caps max runtime) | Premature kill on slow samples |

Profile first, then reduce.

### 4. Minimize `--archive_intermediates` Storage

The `--archive_intermediates` flag archives per-sample `.osw`, `.osm`, and `eplusout.sql` files. This is valuable for reproducibility but adds S3/EBS costs:

- **Recommended:** Enable for campaigns <500 samples. Disable for 5000+ sample campaigns unless you need the raw SQL outputs.
- **Alternative:** Archive only the `aggregated_results.csv` + `failed_simulations.csv` (always produced) and re-run individual samples on demand if you need to debug.

### 5. OpenStudio Version Selection

Newer OpenStudio versions may include EnergyPlus performance improvements. Check the [NREL OpenStudio changelog](https://github.com/NREL/OpenStudio/wiki) for performance notes. Switching from 3.11.0 to 3.11.0 (or newer) might reduce per-sample wall-time by 10–20% at no additional cost.

### 6. Batch Concurrency Optimization

For AWS Batch, the `maxvCpus` on your compute environment controls how many tasks run in parallel:

```
optimal_maxvCpus = min(
    desired_concurrent_samples × vcpus_per_sample,
    account_vCPU_limit
)
```

Too low → campaign takes longer (same cost, worse wall-clock). Too high → no benefit (same cost, faster wall-clock).

A good default for a 1000-sample campaign: `maxvCpus = 64` (16 concurrent 4-vCPU tasks, ~30 min wall-clock if each sim takes 30 min).

### 7. Clean Up Intermediate Files

OSimFlow automatically deletes empty `eplusout.err` files after successful simulations (PRD §1.4). For additional savings:

- Delete `eplusout.log` files from completed samples (verbose, rarely needed).
- Keep only `eplusout.sql` for post-hoc analysis; discard `.osm`/`.osw` intermediates after KPI extraction if you don't need `--archive_intermediates`.

### 8. Instance-Type Selection

EnergyPlus (OpenStudio's underlying engine) is **CPU-bound but memory-sensitive**: most models scale well on 2–4 vCPUs, but very large models can out-of-memory (OOM) on small instances. The Spot example in [Spot Instance Strategy](#spot-instance-strategy) sets `"instanceTypes": ["m5", "c5"]` — pick the family deliberately based on your workload:

| Family | Examples | vCPU:Memory | Best for OpenStudio when… |
|---|---|---|---|
| **`c5` / `c6i`** (compute-optimized) | c5.large (2 vCPU, 4 GB) | 1:2 | Small/medium models (≤200 zones), many fast parallel samples; CPU utilization consistently >80% |
| **`m5` / `m6i`** (general-purpose) | m5.large (2 vCPU, 8 GB) | 1:4 | **Safe default.** Balanced; fits the 4 vCPU / 8 GB sim defaults without waste |
| **`r5` / `r6i`** (memory-optimized) | r5.large (2 vCPU, 16 GB) | 1:8 | Very large models (>500 zones: hospitals, campuses) that OOM on `m5` |

**Worked example — 50-zone office model, 1000 samples:**

1. **Start with m5.large** (2 vCPU, 8 GB). The model fits comfortably in memory and 2 vCPUs match EnergyPlus's typical thread ceiling for mid-size buildings. At ~30 min/sample on m5 Spot ($0.029/vCPU-hr): `1000 × 0.5 hr × 2 vCPU × $0.029 ≈ $29`.
2. **Profile a 3-sample pilot.** Check CPU and memory in `run.json` (or CloudWatch / Slurm `seff`). If peak memory is <4 GB and CPU stays >80%, **switch to c5.large** — same vCPU count, ~10% cheaper per vCPU-hour, and the tighter memory budget is not a constraint.
3. **If samples OOM on 8 GB**, the model is memory-bound — **move to r5.large** (16 GB) rather than over-provisioning vCPUs you won't use.

**Executor-specific selection:**

- **AWS Batch:** set `instanceTypes` in the compute environment (see the [Spot example](#spot-instance-strategy)). Use `--aws-batch-instance-type m5.large` so the Spot price-ceiling check (`describe_spot_price_history`) is scoped to one instance type — without it, the check uses a cross-family minimum that can be misleading (issue #792).
- **Slurm:** use `--slurm-partition` to target a partition whose nodes match the workload. Most HPC sites expose a `compute` (m5-like), `highmem` (r5-like), and sometimes a `fast`/`c5-like` partition. Pick the partition that matches your model size, not the one with the shortest queue — an OOM kill wastes far more than queue wait time.
- **Dask (`dask_jobqueue`):** right-size workers with `--dask-cpus-per-worker` (default 2) and `--dask-memory-per-worker` (default `4GiB`). For memory-heavy models, keep `--dask-cpus-per-worker` low (1–2) and raise `--dask-memory-per-worker` (e.g. `8GiB`) rather than adding workers you can't feed.

### 9. Idle-Compute Auto-Shutdown

A campaign runs for minutes to hours, but a cluster left idle between campaigns burns budget continuously. Scale to zero when idle.

**AWS Batch — `minvCpus: 0` + `desiredvCpus: 0` (scale to zero):**

The Batch example in [Spot Instance Strategy](#spot-instance-strategy) already sets both fields — here is what they do:

- `minvCpus: 0` — the compute environment is allowed to have **zero** instances when no jobs are queued. With any non-zero value, Batch keeps that many vCPUs permanently warm, which is pure idle spend between campaigns.
- `desiredvCpus: 0` — the initial/target steady-state size. Batch scales this up as jobs arrive and **scales it back to 0** when the queue drains, terminating the underlying EC2 instances.

Both must be `0` to truly scale to zero. The Terraform equivalent (see [AWS Batch Terraform Deployment Guide](aws-batch-terraform.md)) is `desired_vcpus = 0` on the compute environment; the same applies to `min_vcpus`. Verify in the console that the compute environment reaches `0` instances after a campaign finishes — a stuck warm instance is a common silent cost leak.

**Slurm — power-saving (`SuspendProgram` / `ResumeProgram`):**

Configure Slurm's built-in node suspend/resume in `/etc/slurm/slurm.conf` so idle compute nodes are powered down:

```
# slurm.conf — power-save section
SuspendProgram        = /etc/slurm/suspend.sh
ResumeProgram         = /etc/slurm/resume.sh
SuspendTime           = 300        # seconds idle before suspend (5 min)
SuspendTimeout        = 60         # max seconds to suspend a node
ResumeTimeout         = 600        # max seconds to resume a node
TreeWidth             = 50
```

`SuspendProgram` is invoked on nodes idle longer than `SuspendTime`; `ResumeProgram` starts them when jobs are queued. On cloud Slurm (AWS ParallelCluster, etc.) these scripts call `aws ec2 stop-instances` / `start-instances`. On on-prem clusters with no per-node power cost, leave this unconfigured — there is no money to save. OSimFlow submits via `submitit`, which is power-save-aware: suspended nodes simply show as `DOWN~` until resumed.

**Dask — `--dask-min-workers 0`:**

The `dask_jobqueue` executor scales workers between a floor and ceiling. Set the floor to zero so the cluster releases all workers when the campaign finishes:

```bash
osimflow run --executor dask_jobqueue \
  --dask-min-workers 0 \
  --dask-max-workers 64 \
  --dask-cpus-per-worker 2 \
  --dask-memory-per-worker 4GiB \
  --n_samples 1000 --outdir ./results \
  --input_variables variables.yml --template_sim_package ./pkg
```

`--dask-min-workers 0` (the default) lets the Dask cluster scale down to zero idle workers once the work is done, so you stop paying for compute between campaigns. A non-zero floor keeps that many workers permanently allocated — useful only for low-latency ad-hoc analysis after the campaign, not for batch simulation throughput.

## Quick Reference: CLI Flags for Cost Optimization

| Flag | Default | Cost impact |
|---|---|---|
| `--aws-batch-max-spot-price-usd` | (unset) | Caps Spot spend per vCPU-hour; rejects jobs above the ceiling |
| `--aws-batch-fallback-to-on-demand` | off | Prevents total campaign failure when Spot is exhausted (at a price premium) |
| `--aws-batch-instance-type` | (unset) | Scopes the Spot ceiling check to one instance type (e.g. m5.large); avoids misleading cross-family minimums |
| `--aws-batch-max-retries` | 3 | Bounds wasted compute on repeated Spot interruptions |
| `--slurm-partition` | `short` | Routes jobs to the cheapest queue that fits the model (see [Partition Selection](#partition-selection)) |
| `--slurm-account` | (unset) | Chargeback routing on institutional clusters |
| `--dask-min-workers` | 0 | Idle-worker floor; `0` releases all workers between campaigns (scale to zero) |
| `--dask-max-workers` | 10 | Concurrency ceiling for Dask campaigns |
| `--dask-cpus-per-worker` | 2 | Per-worker vCPU sizing (right-size to the model) |
| `--dask-memory-per-worker` | `4GiB` | Per-worker memory; raise for large models instead of adding workers |
| `--max-workers` | 4 | Local-executor parallelism (advisory on cloud executors) |
| `--enable-cost-tracking` | off | Records per-campaign cost estimates in `run.json` (issue #447) |
| `--cost-on-demand-price` | (unset) | On-demand $/vCPU-hour for cost tracking |
| `--cost-spot-price` | (unset) | Spot $/vCPU-hour for cost tracking |
| `--archive_intermediates` | off | Enables S3/EBS spend on per-sample `.osw`/`.osm`/`.sql` |

```bash
# Cheapest: local executor, small run, no archive
osimflow run --executor local --n_samples 10 --outdir ./results \
  --input_variables variables.yml --template_sim_package ./pkg

# AWS Batch: Spot-optimized, 500 samples
osimflow run --executor aws_batch \
  --aws-batch-queue osimflow-spot-queue \
  --n_samples 500 --outdir ./results \
  --input_variables variables.yml --template_sim_package ./pkg

# Slurm: right-sized partition, real cluster
osimflow run --executor slurm --slurm-real \
  --slurm-partition short --slurm-account myproject \
  --n_samples 1000 --outdir ./results \
  --input_variables variables.yml --template_sim_package ./pkg

# Dask: scale to zero when idle, right-sized workers
osimflow run --executor dask_jobqueue \
  --dask-min-workers 0 --dask-max-workers 64 \
  --dask-cpus-per-worker 2 --dask-memory-per-worker 4GiB \
  --n_samples 1000 --outdir ./results \
  --input_variables variables.yml --template_sim_package ./pkg

# Re-run (cache hit): ~$0 regardless of executor
osimflow run --executor aws_batch --n_samples 1000 \
  --outdir ./results \
  --input_variables variables.yml --template_sim_package ./pkg
```

## References

- [PRD §6 — Potential Challenges & Considerations](OSimFlow.md#6-potential-challenges--considerations)
- [OpenStudio Image Distribution](openstudio-image-distribution.md)
- [AGENTS.md §4 — CLI Flags](../AGENTS.md)
- [AWS Batch Pricing](https://aws.amazon.com/batch/pricing/)
- [EC2 Spot Pricing](https://aws.amazon.com/ec2/spot/pricing/)
- [submitit Documentation](https://github.com/facebookincubator/submitit)
