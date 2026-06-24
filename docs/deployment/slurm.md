# Slurm Deployment Guide

This guide covers deploying OSimFlow on Slurm clusters — both on-premise HPC and cloud-based Slurm (e.g., AWS ParallelCluster, SLURM on GCP). OSimFlow uses `submitit.AutoExecutor` to submit and manage Slurm jobs.

**Estimated setup time:** 10–15 minutes if you already have cluster access. The main prerequisite is a working Slurm environment with Singularity/Apptainer or Docker support.

**Cost:** On-premise clusters typically have no per-job monetary cost. Cloud-based Slurm costs are the same as AWS Batch for the compute — see [Cost Estimation Guide](../cost-estimation.md).

---

## Prerequisites

| Requirement | Details |
|---|---|
| Slurm cluster access | `sbatch`, `squeue`, `scontrol` available on PATH |
| Python 3.12+ | On the submit host (the machine where you run `osimflow`) |
| `submitit` | `pip install -e ".[slurm]"` or `pip install submitit>=1.5` |
| Singularity/Apptainer | On compute nodes (for containerized OpenStudio), **or** Docker (if your cluster supports it) |
| SSH access | To the submit host (where `sbatch` is available) |

---

## Architecture Overview

```
┌──────────────────────────────────────────────┐
│  Submit Host                                 │
│  (where you run `osimflow run`)              │
│                                              │
│  ┌─────────────┐     ┌───────────────────┐   │
│  │  OSimFlow    │────▶│  submitit         │   │
│  │  Campaign    │     │  AutoExecutor     │   │
│  └─────────────┘     └────────┬──────────┘   │
│                               │ sbatch        │
└───────────────────────────────┼───────────────┘
                                │
                                ▼
┌──────────────────────────────────────────────┐
│  Slurm Cluster                               │
│                                              │
│  ┌─────────────┐  ┌───────────────────────┐  │
│  │  Partition   │  │  Compute Nodes        │  │
│  │  (queue)     │  │  (Singularity/Docker) │  │
│  └─────────────┘  └───────────────────────┘  │
│                                              │
│  Logs: $OSIMFLOW_SLURM_LOGS/                 │
└──────────────────────────────────────────────┘
```

OSimFlow's `SlurmExecutor` creates a fresh `submitit.AutoExecutor` per submission, allowing per-sample resource directives (vCPUs, memory, time) to appear in the `#SBATCH` header of each job.

---

## Setup

### 1. Install OSimFlow with Slurm Support

On the submit host:

```bash
pip install -e ".[slurm]"
```

This brings in `submitit`. Verify the installation:

```bash
python -c "import submitit; print(submitit.__version__)"
```

**Recommendation:** Use `submitit >= 1.5` for access to advanced directives (`--slurm-qos`, `--slurm-constraint`, `--slurm-gres`). Older versions silently ignore these flags (see `_apply_slurm_params` in `osimflow/executors/__init__.py`).

### 2. Set Up the Log Directory

submitit writes job logs (stdout/stderr) to a folder on a shared filesystem accessible from both the submit host and compute nodes:

```bash
# Default: /tmp/osimflow-slurm-logs (not shared — use a shared path in production)
export OSIMFLOW_SLURM_LOGS=/scratch/$USER/osimflow-slurm-logs
mkdir -p "$OSIMFLOW_SLURM_LOGS"
```

**Important:** This directory must be on a filesystem that all compute nodes can see (e.g., NFS, Lustre, GPFS). If compute nodes cannot write to this path, submitit will still submit jobs but log collection will be incomplete.

### 3. Set Up Singularity/Apptainer

Most HPC clusters use Singularity (or its successor Apptainer) for containerized workloads. Convert the Docker image once:

```bash
# Pull the NREL OpenStudio Docker image and convert to Singularity
singularity pull docker://nrel/openstudio:3.11.0 \
  --name openstudio-3.11.0.sif

# Or with Apptainer (the successor to Singularity)
apptainer pull docker://nrel/openstudio:3.11.0 \
  --name openstudio-3.11.0.sif
```

Place the `.sif` file on shared storage accessible to compute nodes:

```bash
mkdir -p /scratch/$USER/singularity-images
mv openstudio-3.11.0.sif /scratch/$USER/singularity-images/
```

See [OpenStudio Image Distribution](../openstudio-image-distribution.md) for available versions and image details.

### 4. Verify Slurm Access

```bash
# Check available partitions
sinfo -o "%P %a %l %D %N"

# Test submission
sbatch --wrap="hostname" --partition=short
```

---

## Running a Campaign

### Basic: Debug Mode (Default)

Without `--slurm-real`, jobs run **locally** via `submitit.DebugExecutor`. This is useful for testing without a cluster:

```bash
osimflow run \
  --executor slurm \
  --input_variables variables.yml \
  --template_sim_package ./example_package \
  --n_samples 5 \
  --outdir ./results \
  --openstudio_version 3.11.0
```

The debug executor logs the exact `sbatch` script it *would have* submitted — check the `submitit` logger output.

### Production: Real Slurm

Add `--slurm-real` to submit to the actual Slurm cluster:

```bash
osimflow run \
  --executor slurm \
  --slurm-real \
  --slurm-partition short \
  --input_variables variables.yml \
  --template_sim_package ./example_package \
  --n_samples 100 \
  --outdir ./results \
  --openstudio_version 3.11.0
```

### With Account and Partition

```bash
osimflow run \
  --executor slurm \
  --slurm-real \
  --slurm-partition medium \
  --slurm-account myproject \
  --input_variables variables.yml \
  --template_sim_package ./example_package \
  --n_samples 500 \
  --outdir ./results \
  --openstudio_version 3.11.0
```

### Advanced: GPU Partition

Requires `submitit >= 1.5`:

```bash
osimflow run \
  --executor slurm \
  --slurm-real \
  --slurm-partition gpu \
  --slurm-account myproject \
  --slurm-qos high \
  --slurm-constraint gpu \
  --slurm-gres gpu:1 \
  --input_variables variables.yml \
  --template_sim_package ./example_package \
  --n_samples 200 \
  --outdir ./results \
  --openstudio_version 3.11.0
```

This generates `#SBATCH` directives:

```
#SBATCH --partition=gpu
#SBATCH --account=myproject
#SBATCH --qos=high
#SBATCH --constraint=gpu
#SBATCH --gres=gpu:1
```

---

## CLI Flags Reference

| Flag | Default | Description |
|---|---|---|
| `--slurm-partition` | `short` | Slurm partition (queue) to submit to |
| `--slurm-account` | None | Slurm account for billing/fairshare |
| `--slurm-real` | `False` | Submit to real Slurm (omit for local debug) |
| `--slurm-qos` | None | Quality of Service. Requires `submitit >= 1.5` |
| `--slurm-constraint` | None | Node feature constraint. Requires `submitit >= 1.5` |
| `--slurm-gres` | None | Generic resources (e.g., `gpu:1`). Requires `submitit >= 1.5` |

Additionally, the `SlurmExecutor` constructor sets defaults for:

| Parameter | Default | Maps to `#SBATCH` |
|---|---|---|
| `cpus_per_task` | 2 | `--cpus-per-task=2` |
| `mem_gb` | 4 | `--mem=4G` |
| `time_h` | 2 | `--time=120` (minutes) |

Per-step resource directives from the Campaign override these defaults at submit time. For example, the `RUN_OPENSTUDIO_SIM` step uses 4 vCPUs, 8 GB memory, and 240 minutes.

---

## Resource Allocation

OSimFlow's Campaign passes per-step resource directives to the executor. The `SlurmExecutor` translates these to `#SBATCH` headers:

| Campaign Parameter | `#SBATCH` Directive | Default (sim step) |
|---|---|---|
| `cpus` | `--cpus-per-task` | 4 |
| `memory_mb` | `--mem` (rounded up to GB) | 8G |
| `time_min` | `--time` (minutes) | 240 |

Memory is converted from MB to GB (rounded up) because `submitit` uses integer GB on `slurm_mem_gb`. For example, `memory_mb=8192` becomes `--mem=8G`, and `memory_mb=5500` becomes `--mem=6G`.

### How Per-Submit Overrides Work

The `SlurmExecutor` creates a **fresh** `AutoExecutor` per `submit()` call with the per-sample resource directives. This is the submitit-recommended pattern — `AutoExecutor.update_parameters()` does not propagate per-call kwargs, so a fresh executor per submission ensures the `#SBATCH` header reflects the actual resources requested.

See `osimflow/executors/__init__.py:SlurmExecutor.submit()` for the implementation.

---

## Singularity Container Integration

When running containerized OpenStudio on Slurm, the container image must be available to compute nodes. There are three approaches:

### Approach 1: Pre-built SIF on Shared Storage (Recommended)

```bash
# On the submit host (or a login node with internet access)
singularity pull docker://nrel/openstudio:3.11.0 \
  --name /scratch/$USER/singularity-images/openstudio-3.11.0.sif
```

The work script (or BYOS override) executes the container:

```bash
singularity exec /scratch/$USER/singularity-images/openstudio-3.11.0.sif \
  openstudio.cli run -w workflow.osw
```

### Approach 2: Pull on Demand (Slower)

Some clusters allow compute nodes to pull directly from Docker Hub:

```bash
singularity exec docker://nrel/openstudio:3.11.0 \
  openstudio.cli run -w workflow.osw
```

This downloads the image layers on first use. Subsequent runs use the cached SIF. This is slower for the first sample but avoids pre-building.

### Approach 3: Docker (If Supported)

Some Slurm clusters (e.g., AWS ParallelCluster with Docker support) can run Docker containers natively:

```bash
docker run --rm nrel/openstudio:3.11.0 openstudio.cli run -w workflow.osw
```

---

## Monitoring

### squeue — Check Job Queue

```bash
# All OSimFlow jobs
squeue -u $USER --name=osimflow*

# Detailed info
squeue -u $USER -o "%.18i %.9P %.20j %.8u %.2t %.10M %.6D %R"
```

### scontrol — Inspect a Specific Job

```bash
scontrol show job <job_id>
```

Look for `JobState`, `ExitCode`, `Reason`, and `StdOut`/`StdErr` paths.

### submitit Logs

submitit writes per-job logs to `$OSIMFLOW_SLURM_LOGS/`:

```bash
# List recent log files
ls -lt $OSIMFLOW_SLURM_LOGS/ | head -20

# Read a job's stdout
cat $OSIMFLOW_SLURM_LOGS/<job_id>/stdout.log
cat $OSIMFLOW_SLURM_LOGS/<job_id>/stderr.log
```

### run.json

OSimFlow writes `${outdir}/run.json` with per-step timing and per-sample status:

```bash
cat ./results/run.json | python -m json.tool
```

### Slurm Accounting

```bash
# Job history (may require admin privileges on some clusters)
sacct --format=JobID,JobName,Partition,Elapsed,State,ExitCode,MaxRSS \
  --starttime $(date -d '1 day ago' '+%Y-%m-%d')
```

---

## Troubleshooting

### "SlurmExecutor running in DEBUG mode" Warning

This means jobs are running locally, not on the cluster. Fix: add `--slurm-real`:

```bash
osimflow run --executor slurm --slurm-real ...
```

### Jobs Pending Indefinitely

| Cause | Diagnosis | Fix |
|---|---|---|
| Partition full | `squeue -p <partition> | wc -l` | Use a different partition, or wait for jobs to drain |
| Fairshare too low | `sshare -u $USER` | Submit during off-peak hours, or request a higher QoS |
| Wrong account | `sacctmgr show assoc where user=$USER` | Verify `--slurm-account` matches a valid association |
| Resources unavailable | `sinfo -p <partition>` shows `drain` or `down` | Contact your HPC admin |

### Job Failed with OOM (Out of Memory)

Check the job's `MaxRSS` in Slurm accounting:

```bash
sacct -j <job_id> --format=MaxRSS,MaxVMSize,State
```

If `MaxRSS` is close to the requested memory, increase `mem_gb` in the executor (edit `osimflow/campaign.py` or the `SlurmExecutor` constructor defaults).

### Job Failed with Time Limit

The default `time_min` for the simulation step is 240 minutes. If your models need more time:

1. Check the actual runtime of a small pilot run:

```bash
sacct --format=JobID,Elapsed --name=osimflow*
```

2. Adjust `time_min` in `osimflow/campaign.py:step_run_openstudio_sim`.

### Singularity: "image not found"

Verify the SIF path is accessible from compute nodes:

```bash
# On a compute node (e.g., via srun)
srun --partition=short --pty bash
ls /scratch/$USER/singularity-images/openstudio-3.11.0.sif
```

If the file is not visible, the shared filesystem may not be mounted on compute nodes. Contact your HPC admin, or copy the SIF to local scratch on each node.

### submitit Version Mismatch

`--slurm-qos`, `--slurm-constraint`, and `--slurm-gres` require `submitit >= 1.5`. On older versions, these flags are silently ignored (see `_apply_slurm_params` in the executor source). Check your version:

```bash
python -c "import submitit; print(submitit.__version__)"
```

Upgrade:

```bash
pip install "submitit>=1.5"
```

### "ModuleNotFoundError: No module named 'submitit'"

Install the Slurm extra:

```bash
pip install -e ".[slurm]"
```

---

## Example Job Scripts

### Pilot Run (3 samples, measure wall-time)

```bash
osimflow run \
  --executor slurm \
  --slurm-real \
  --slurm-partition short \
  --slurm-account myproject \
  --input_variables variables.yml \
  --template_sim_package ./example_package \
  --n_samples 3 \
  --outdir ./results-pilot \
  --openstudio_version 3.11.0
```

After the pilot completes, check wall-times:

```bash
sacct --name=osimflow* --format=JobID,Elapsed,MaxRSS,State
```

Use the longest wall-time + 25% buffer to set `time_min` for the production run.

### Production Parametric Sweep (1000 samples)

```bash
osimflow run \
  --executor slurm \
  --slurm-real \
  --slurm-partition medium \
  --slurm-account myproject \
  --input_variables variables.yml \
  --template_sim_package ./example_package \
  --n_samples 1000 \
  --outdir ./results-production \
  --openstudio_version 3.11.0
```

### With Custom KPI Extractor (BYOS)

```bash
osimflow run \
  --executor slurm \
  --slurm-real \
  --slurm-partition short \
  --custom_kpi_extractor user_scripts/my_kpis.py \
  --input_variables variables.yml \
  --template_sim_package ./example_package \
  --n_samples 100 \
  --outdir ./results-custom-kpis \
  --openstudio_version 3.11.0
```

### Re-run After Interruption (Cache Hit)

```bash
# Same command, same --outdir — completed samples hit the cache
osimflow run \
  --executor slurm \
  --slurm-real \
  --slurm-partition short \
  --input_variables variables.yml \
  --template_sim_package ./example_package \
  --n_samples 100 \
  --outdir ./results-production \
  --openstudio_version 3.11.0
```

Only interrupted or failed samples will re-execute. The warm run should be at least 5x faster than the cold run (verified in `tests/integration/test_cache_resume.py`).

### Large Model (High Memory)

For models with >2000 thermal zones that need more memory:

```bash
# Override SlurmExecutor defaults via environment or BYOS work function
# See osimflow/campaign.py for where to adjust cpus, memory_mb, time_min
OSIMFLOW_SLURM_LOGS=/scratch/$USER/osimflow-logs \
osimflow run \
  --executor slurm \
  --slurm-real \
  --slurm-partition bigmem \
  --slurm-account myproject \
  --input_variables variables.yml \
  --template_sim_package ./large_model_package \
  --n_samples 50 \
  --outdir ./results-large \
  --openstudio_version 3.11.0
```

---

## Advanced Configuration

### OSIMFLOW_SLURM_LOGS Environment Variable

Controls where submitit writes per-job logs:

```bash
export OSIMFLOW_SLURM_LOGS=/scratch/$USER/osimflow-campaign-$(date +%Y%m%d)
mkdir -p "$OSIMFLOW_SLURM_LOGS"
```

If not set, defaults to `/tmp/osimflow-slurm-logs` (not shared across nodes — avoid in production).

### Fairshare Considerations

On shared clusters, your fairshare score determines scheduling priority. To minimize impact:

1. **Use `--slurm-account`** if your cluster requires account-based billing.
2. **Submit during off-peak hours** for large campaigns (>500 samples).
3. **Set realistic `time_min`.** Overestimating wall-time hurts your fairshare; underestimating kills jobs. Use the pilot-run approach above.
4. **Use backfill-friendly wall-times.** Shorter jobs get scheduled faster on busy clusters.

### Cloud-Based Slurm (AWS ParallelCluster)

For cloud-based Slurm, the same OSimFlow commands work. Additional considerations:

- **Shared storage:** ParallelCluster uses EFS or FSx for the shared home directory. Ensure `$OSIMFLOW_SLURM_LOGS` is on the shared filesystem.
- **Cost:** EC2 instances are billed the same as AWS Batch. See [Cost Estimation Guide](../cost-estimation.md#cloud-based-slurm).
- **Auto-scaling:** ParallelCluster auto-scales compute nodes. Set `--slurm-partition` to the queue backed by the desired instance type.
- **Head node:** The head node runs the Slurm controller and is always-on (~$0.05–0.10/hr). Shut it down between campaigns to save costs.

---

## References

- [Cost Estimation Guide](../cost-estimation.md) — per-campaign pricing, right-sizing, fairshare
- [OpenStudio Image Distribution](../openstudio-image-distribution.md) — container image selection and versioning
- [AGENTS.md §4](../../AGENTS.md) — CLI flags reference
- [AGENTS.md §10](../../AGENTS.md) — security (Singularity: no bind-mounted secrets)
- [submitit Documentation](https://github.com/facebookincubator/submitit) — the Slurm executor backend
- [Singularity Documentation](https://docs.sylabs.io/) — container runtime for HPC
- [Apptainer Documentation](https://apptainer.org/docs/) — successor to Singularity
