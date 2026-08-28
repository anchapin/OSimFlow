# Azure Batch, Google Batch, PBS, Dask-JobQueue, and Docker Swarm Deployment Guide

This guide covers deployment for five OSimFlow executors that share similar operational patterns. Each section is self-contained; jump to the executor you need.

**Estimated setup time:** 20–60 minutes per executor (one-time). Subsequent campaigns require zero infrastructure changes.

---

## Azure Batch

### Prerequisites

| Requirement | Details |
|---|---|
| Azure subscription | With permissions to create Batch accounts, storage accounts, and Managed Identity |
| Azure CLI | `az login` performed; `az account set --subscription <sub>` |
| Docker | For pushing custom images to Azure Container Registry |
| OSimFlow | `pip install -e ".[azure]"` |

### Architecture

```
┌────────────────┐     ┌─────────────────────────────────┐
│ Your Machine   │     │  Azure Batch                    │
│ (osimflow CLI) │────▶│                                  │
└────────────────┘     │  ┌──────────┐ ┌──────────────┐ │
                       │  │  Pool     │ │ Batch Account │ │
                       │  │ (VM nodes)│ │  + Storage   │ │
                       │  └──────────┘ └──────────────┘ │
                       └─────────────────────────────────┘
```

### Step-by-Step Setup

#### 1. Create Resource Group and Batch Account

```bash
RESOURCE_GROUP="osimflow-rg"
LOCATION="eastus"
BATCH_ACCOUNT="osimflow-batch-$(date +%s)"

az group create --name "$RESOURCE_GROUP" --location "$LOCATION"

az batch account create \
  --name "$BATCH_ACCOUNT" \
  --resource-group "$RESOURCE_GROUP" \
  --location "$LOCATION"
```

#### 2. Create Azure Container Registry (for custom images)

```bash
ACR_NAME="osimflowacr$(date +%s | cut -c1-10)"

az acr create \
  --resource-group "$RESOCATION_GROUP" \
  --name "$ACR_NAME" \
  --sku Standard

# Login for docker push
az acr login --name "$ACR_NAME"
docker tag nrel/openstudio:3.11.0 "$ACR_NAME.azurecr.io/openstudio:3.11.0"
docker push "$ACR_NAME.azurecr.io/openstudio:3.11.0"
```

#### 3. Configure Pool

```bash
az batch pool create \
  --pool-id "osimflow-pool" \
  --vm-size "Standard_D4s_v3" \
  --target-dedicated-nodes 4 \
  --image "/subscriptions/.../offer/publisher/image:sku" \
  --batch-account "$BATCH_ACCOUNT" \
  --resource-group "$RESOURCE_GROUP"
```

### Running a Campaign

```bash
osimflow run \
  --executor azure_batch \
  --azure-batch-account-name "$BATCH_ACCOUNT" \
  --azure-batch-account-url "https://$BATCH_ACCOUNT.$LOCATION.batch.azure.com" \
  --azure-batch-pool-id "osimflow-pool" \
  --azure-use-spot \
  --input_variables variables.yml \
  --template_sim_package ./example_package \
  --n_samples 100 \
  --outdir ./results
```

### Troubleshooting

| Symptom | Resolution |
|---|---|
| `BatchDomainNotFound` | Ensure Batch account is linked to the correct Active Directory tenant |
| Pool allocation timeout | Increase `--azure-batch-max-retries` or check VM quota |
| Container image pull failure | Verify ACR image exists and Batch nodes can reach `*.azurecr.io` |

---

## Google Batch

### Prerequisites

| Requirement | Details |
|---|---|
| Google Cloud project | With Batch API enabled |
| gcloud CLI | `gcloud auth login` and `gcloud project set-project <project>` |
| Service account | With roles: `roles/batch.jobsEditor`, `roles/storage.objectAdmin` |
| OSimFlow | `pip install -e ".[google]"` |

### Architecture

```
┌────────────────┐     ┌─────────────────────────────────┐
│ Your Machine   │     │  Google Cloud                   │
│ (osimflow CLI) │────▶│                                  │
└────────────────┘     │  ┌──────────┐ ┌──────────────┐ │
                       │  │  Batch    │ │ Cloud Storage │ │
                       │  │  Job       │ │ (results)     │ │
                       │  └──────────┘ └──────────────┘ │
                       └─────────────────────────────────┘
```

### Step-by-Step Setup

#### 1. Enable Batch API and Create Service Account

```bash
PROJECT_ID="my-project"
gcloud services enable batch.googleapis.com --project "$PROJECT_ID"

SA_EMAIL="osimflow-batch@$PROJECT_ID.iam.gserviceaccount.com"
gcloud iam service-accounts create osimflow-batch --project "$PROJECT_ID"

gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:$SA_EMAIL" \
  --role="roles/batch.jobsEditor"

gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:$SA_EMAIL" \
  --role="roles/storage.objectAdmin"
```

#### 2. Create a GCS Bucket

```bash
gsutil mb -p "$PROJECT_ID" "gs://osimflow-results-$(date +%s)"
```

### Running a Campaign

```bash
osimflow run \
  --executor google_batch \
  --google-batch-project-id "$PROJECT_ID" \
  --google-batch-region "us-central1" \
  --google-batch-service-account "$SA_EMAIL" \
  --google-use-spot \
  --input_variables variables.yml \
  --template_sim_package ./example_package \
  --n_samples 100 \
  --outdir ./results
```

### Troubleshooting

| Symptom | Resolution |
|---|---|
| `PERMISSION_DENIED` on job create | Verify service account has `roles/batch.jobsEditor` |
| Spot instance quota exceeded | Request increased quota or use `--google-fallback-to-on-demand` |

---

## PBS (Portable Batch System)

### Prerequisites

| Requirement | Details |
|---|---|
| PBS cluster access | Valid `qsub`, `qstat`, `qdel` access |
| OpenStudio container | `nrel/openstudio:<version>` available on the cluster (or use `--container-digest` for exact image) |
| OSimFlow | `pip install -e ".[slurm,pbs]"` |

### Architecture

```
┌────────────────┐     ┌─────────────────────────────────┐
│ Your Machine   │     │  PBS Cluster                     │
│ (osimflow CLI) │────▶│                                  │
└────────────────┘     │  ┌──────────┐ ┌──────────────┐ │
                       │  │  qsub     │ │ compute nodes │ │
                       │  │  (job)    │ │ (OpenStudio)  │ │
                       │  └──────────┘ └──────────────┘ │
                       └─────────────────────────────────┘
```

### Step-by-Step Setup

#### 1. Verify PBS Access

```bash
qstat --version  # Confirm PBS is available
qsig -s enroute <job_id>  # Test signaling
```

#### 2. Configure Slurm/PBS Common Settings

```bash
osimflow run \
  --executor pbs \
  --pbs-queue "batch" \
  --pbs-real \
  --pbs-server "pbs-server.example.com" \
  --input_variables variables.yml \
  --template_sim_package ./example_package \
  --n_samples 100 \
  --outdir ./results
```

### Resource Sizing

| Samples | Suggested Nodes | walltime |
|---|---|---|
| 100 | 4 | 2:00:00 |
| 500 | 20 | 4:00:00 |
| 1000 | 40 | 8:00:00 |

### Troubleshooting

| Symptom | Resolution |
|---|---|
| `qsub: Bad UID for job execution` | Check your `qsub` ACLs with `qmgr` |
| Job walltime exceeded | Increase `--time_min` or reduce `--max-workers` |

---

## Dask-JobQueue (Slurm/PBS/K8s-backed)

### Prerequisites

| Requirement | Details |
|---|---|
| Scheduler already running | Dask cluster with `dask-scheduler` running |
| Python environment | Same Python version on scheduler and workers |
| OSimFlow | `pip install -e ".[dask]"` |

### Architecture

```
┌────────────────┐     ┌─────────────────────────────────┐
│ Your Machine   │     │  Dask Cluster                    │
│ (osimflow CLI) │────▶│                                  │
└────────────────┘     │  ┌──────────┐ ┌──────────────┐ │
                       │  │ Scheduler│ │ JobQueue workers │ │
                       │  │ (master) │ │ (Slurm/PBS/K8s) │ │
                       │  └──────────┘ └──────────────┘ │
                       └─────────────────────────────────┘
```

### Step-by-Step Setup

#### 1. Start Dask Scheduler on HPC Login Node

```bash
dask-scheduler --port 8786 --dashboard-address 8787
```

#### 2. Start Workers (on compute nodes via job script)

```bash
#!/bin/bash
#SBATCH --job-name osimflow-dask-worker
#SBATCH --ntasks 4
#SBATCH --time 04:00:00

dask-worker tcp://<scheduler-host>:8786 --nthreads 1 --memory-limit 8GB
```

#### 3. Run Campaign

```bash
osimflow run \
  --executor dask_jobqueue \
  --dask-scheduler-address "tcp://<scheduler-host>:8786" \
  --dask-walltime "04:00:00" \
  --dask-cpus-per-worker 4 \
  --dask-memory-per-worker "8GB" \
  --input_variables variables.yml \
  --template_sim_package ./example_package \
  --n_samples 100 \
  --outdir ./results
```

### Troubleshooting

| Symptom | Resolution |
|---|---|
| Worker registration timeout | Check network connectivity between scheduler and workers |
| `KilledWorkerException` | Increase `--dask-memory-per-worker` or reduce batch size |

---

## Docker Swarm

### Prerequisites

| Requirement | Details |
|---|---|
| Docker Swarm initialized | `docker swarm init` on manager node |
| Overlay network | Pre-configured or created automatically |
| OSimFlow | `pip install -e ".[docker]"` |

### Architecture

```
┌────────────────┐     ┌─────────────────────────────────┐
│ Your Machine   │     │  Docker Swarm                    │
│ (osimflow CLI) │────▶│                                  │
└────────────────┘     │  ┌──────────┐ ┌──────────────┐ │
                       │  │ Manager   │ │ Worker nodes  │ │
                       │  │ (orchest.)│ │ (simulations) │ │
                       │  └──────────┘ └──────────────┘ │
                       └─────────────────────────────────┘
```

### Step-by-Step Setup

#### 1. Initialize Swarm

```bash
docker swarm init --advertise-addr <MANAGER_IP>
```

#### 2. Create Overlay Network

```bash
docker network create -d overlay osimflow-net
```

#### 3. Run Campaign

```bash
osimflow run \
  --executor docker_swarm \
  --docker-swarm-network osimflow-net \
  --docker-swarm-image "nrel/openstudio:3.11.0" \
  --input_variables variables.yml \
  --template_sim_package ./example_package \
  --n_samples 100 \
  --outdir ./results
```

### Troubleshooting

| Symptom | Resolution |
|---|---|
| `network osimflow-net not found` | Ensure manager and workers share the same swarm cluster |
| Service placement failed | Check `docker service ps <name>` for node-level constraints |

---

## Shared Troubleshooting Patterns

| Symptom | Common Cause | Resolution |
|---|---|---|
| Executor not found | Missing pip extra | Reinstall with `pip install -e ".[<executor>]"` |
| Container image pull failure | Network/firewall | Pre-pull image on all nodes or use `--container-digest` |
| Authentication error | Expired credentials | Re-authenticate (AWS: `aws configure`, Azure: `az login`, GCP: `gcloud auth application-default login`) |
| Resource quota exceeded | Cloud provider limits | Request quota increase or use spot/preemptible instances |
