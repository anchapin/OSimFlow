# Offline Deployment Guide

> **Audience:** OSimFlow operators, IT administrators, and DevOps engineers
> who need to run parametric simulation campaigns in **network-isolated
> environments** — air-gapped HPC clusters, government secure enclaves,
> factories with no internet egress, VPC-restricted cloud accounts, or
> corporate networks that block Docker Hub / PyPI. The existing
> [`air-gapped-deployment.md`](air-gapped-deployment.md) is a quick-start
> companion; this guide is the comprehensive reference.

## TL;DR

OSimFlow normally requires internet access for three things:

1. **Docker Hub image pulls** — `docker.io/nrel/openstudio:<version>`
   fetched at job submission time (see
   [`openstudio-image-distribution.md`](openstudio-image-distribution.md)).
2. **pip package installs** — the Python environment is built from PyPI.
3. **Weather file downloads** — `.epw` files fetched from the
   EnergyPlus weather repository (see `osimflow/weather.py:download_epw`).

The `--offline` flag (issue #261) disables all three network dependencies.
The `--offline-bundle` flag points OSimFlow at a pre-bundled directory
containing every asset the campaign needs, so no outbound network call
is ever made.

---

## Table of Contents

- [1. What Offline Mode Does](#1-what-offline-mode-does)
- [2. Prerequisites](#2-prerequisites)
- [3. Creating an Offline Bundle](#3-creating-an-offline-bundle)
- [4. Using `--offline` and `--offline-bundle`](#4-using---offline-and---offline-bundle)
- [5. ECR / Private Registry Mirroring](#5-ecr--private-registry-mirroring)
- [6. Air-Gapped HPC (Slurm / Singularity)](#6-air-gapped-hpc-slurm--singularity)
- [7. Air-Gapped AWS Batch](#7-air-gapped-aws-batch)
- [8. Docker / Podman Air-Gapped Runtime](#8-docker--podman-air-gapped-runtime)
- [9. Troubleshooting](#9-troubleshooting)
- [References](#references)

---

## 1. What Offline Mode Does

When you pass `--offline` to the `osimflow run` subcommand, OSimFlow changes
its behaviour to eliminate all outbound network traffic:

| Behaviour | Online (default) | Offline (`--offline`) |
|---|---|---|
| Docker image resolution | Pulls `nrel/openstudio:<version>` from Docker Hub if not cached locally | Uses only locally-loaded images; never pulls |
| pip dependency installation | Resolves from PyPI | Installs from wheels in `--offline-bundle/pip/` via `--no-index --find-links` |
| Version-check pings | Probes PyPI / Docker Hub for latest versions | Skipped entirely |
| Weather file acquisition | May download `.epw` from a URL | Reads `.epw` files only from `--offline-bundle/weather/` or the `template_sim_package` |
| `osimflow health` network check | Pings a public endpoint | Skipped (pass `--offline` to the `health` subcommand too) |

The offline flag is defined in `osimflow/config.py` as the `offline` and
`offline_bundle` fields on the `CampaignConfig` dataclass, and surfaced on
the CLI in `osimflow/__main__.py`.

> **Note:** `download_epw()` in `osimflow/weather.py` is **opt-in** and is
> never called automatically by the campaign pipeline. It exists as a
> utility for setup scripts. In offline mode, place `.epw` files in the
> bundle's `weather/` subdirectory or inside the `template_sim_package`
> directly.

### When to use offline mode

- **Air-gapped HPC** — Slurm clusters with no compute-node internet access.
- **Secure / classified environments** — government or defence networks
  with no egress to public registries.
- **VPC-restricted cloud** — AWS Batch inside a VPC with no NAT gateway
  (uses VPC endpoints + ECR instead of Docker Hub).
- **Corporate networks behind a proxy** — where Docker Hub and PyPI are
  blocked by firewall policy and pre-mirroring is the approved path.
- **Reproducibility / pinning** — when you want a fully self-contained
  bundle that produces identical results regardless of upstream changes.

---

## 2. Prerequisites

Before going offline, you need to assemble **four** asset categories on a
machine **with** internet access:

| Category | Contents | How to obtain |
|---|---|---|
| **Container images** | `nrel/openstudio:<version>` (the simulation runtime); optionally `ghcr.io/anchapin/scientific_python_image:latest` | `scripts/bundle_offline.py --docker-only`, or the [ECR sync script](#5-ecr--private-registry-mirroring) for cloud, or manual `docker save` |
| **Python dependencies** | All pip wheels for OSimFlow + the extras your campaigns use (`aws`, `slurm`, `mlflow`, `sensitivity`, `optimization`, `api`, `tui`) | `scripts/bundle_offline.py --pip-only` |
| **Weather files** | `.epw` files referenced in `variables.yml` | `scripts/bundle_offline.py --weather-only`, or copy from the EnergyPlus weather site while online |
| **Campaign inputs** | `template_sim_package/` directory (base `.osm`/`.osw` + measure scripts) and `variables.yml` | Manual copy — these are user-supplied, never bundled by the script |

### Tooling required (on the online build machine)

- **Docker** (or Podman) — for pulling and saving container images.
- **Python 3.12+** — for running `scripts/bundle_offline.py`.
- **`curl`** — used by the bundle script to download `.epw` files.
- **AWS CLI v2** (cloud-only) — for the ECR sync script.
- **`singularity` / `apptainer`** (HPC-only) — for converting Docker images
  to SIF format; see [§6](#6-air-gapped-hpc-slurm--singularity).

### Disk space estimate

| Asset | Approximate size |
|---|---|
| `nrel/openstudio:3.11.0` Docker image | ~2 GB |
| Scientific Python image | ~1.5 GB |
| pip wheels (all extras) | ~500 MB – 1 GB |
| Weather files (per file) | ~1–3 MB |
| **Total bundle** | **~4–8 GB** |

---

## 3. Creating an Offline Bundle

The `scripts/bundle_offline.py` script is the primary tool for assembling
a self-contained offline bundle. It produces a tar.gz archive containing
a versioned `offline/` directory with a `bundle_manifest.json` (metadata +
SHA-256 checksums for integrity verification).

### 3.1 Bundle everything (recommended)

```bash
set -euo pipefail

# On the ONLINE machine — install OSimFlow with the extras you need first
pip install -e ".[dev,aws,slurm]"

# Bundle everything: pip wheels + Docker images + weather files
python scripts/bundle_offline.py \
    --openstudio-version 3.11.0 \
    --pip-extras "dev,aws,slurm" \
    --variables variables.yml \
    --weather-dir ./example_package/weather \
    --output /tmp/osimflow-offline.tar.gz
```

The script downloads:

- All pip wheels for the requested extras into `offline/pip/`.
- The `nrel/openstudio:3.11.0` Docker image as a tar archive into
  `offline/docker/`.
- The scientific Python image
  (`ghcr.io/anchapin/scientific_python_image:latest`) as a tar archive into
  `offline/docker/` (best-effort; a warning is logged if the pull fails).
- Any `.epw` files referenced in `variables.yml` (or all `.epw` files in
  `--weather-dir`) into `offline/weather/`.

### 3.2 Bundle a subset

The script supports four mutually exclusive modes:

```bash
# Pip wheels only
python scripts/bundle_offline.py --pip-only \
    --pip-extras "aws,slurm" \
    --output /tmp/pip-bundle.tar.gz

# Docker images only
python scripts/bundle_offline.py --docker-only \
    --openstudio-version 3.11.0 \
    --output /tmp/docker-bundle.tar.gz

# Weather files only
python scripts/bundle_offline.py --weather-only \
    --weather-dir ./weather \
    --output /tmp/weather-bundle.tar.gz

# Everything (default when no --*-only flag is set)
python scripts/bundle_offline.py \
    --openstudio-version 3.11.0 \
    --pip-extras "dev,aws,slurm" \
    --output /tmp/osimflow-offline.tar.gz
```

### 3.3 Bundle script flag reference

| Flag | Default | Description |
|---|---|---|
| `--output`, `-o` | `osimflow-offline.tar.gz` | Output tarball path |
| `--openstudio-version` | `3.11.0` | OpenStudio version to bundle. Choices: `3.7.0`, `3.8.0`, `3.9.0`, `3.10.0`, `3.11.0` |
| `--pip-extras` | `dev,aws,slurm` | Comma-separated pip extras to include (e.g. `aws,slurm,mlflow,sensitivity,optimization,api,tui`) |
| `--variables` | *(none)* | Path to `variables.yml` to extract weather-file references from |
| `--weather-dir` | *(none)* | Directory containing `.epw` weather files to bundle |
| `--pip-only` | off | Bundle pip packages only |
| `--docker-only` | off | Bundle Docker images only |
| `--weather-only` | off | Bundle weather files only |
| `--verbose`, `-v` | off | Increase verbosity (`-v`, `-vv`, `-vvv`) |

### 3.4 Bundle directory structure

```
offline/
├── pip/
│   ├── osimflow-0.1.0-py3-none-any.whl
│   ├── numpy-1.26.4-cp312-cp312-manylinux_2_17_x86_64.whl
│   └── ... (all pip wheels for the requested extras)
├── docker/
│   ├── nrel_openstudio_3.11.0.tar
│   └── ghcr.io_anchapin_scientific_python_image_latest.tar
├── weather/
│   └── USA_CA_San.Francisco.Intl.AP.724940_TMY3.epw
└── bundle_manifest.json   # metadata: versions, SHA-256 checksums, created date
```

The `bundle_manifest.json` records the OpenStudio version, pip extras,
creation timestamp, and a SHA-256 hash for every bundled file — use it to
verify integrity after transfer:

```bash
set -euo pipefail

# Verify a single file's checksum after extraction
sha256sum -c <(python -c "
import json, sys
m = json.load(open('offline/bundle_manifest.json'))
for w in m['pip_wheels']:
    if w['name'] == sys.argv[1]:
        print(f'{w[\"sha256\"]}  offline/pip/{w[\"name\"]}')
" osimflow-0.1.0-py3-none-any.whl)
```

### 3.5 Transfer to the offline machine

Copy the tarball via the approved data-transfer mechanism (USB, NFS mount,
SCP via jump host, approved media transfer):

```bash
set -euo pipefail
scp -P 2222 /tmp/osimflow-offline.tar.gz airgap-user@host:/data/osimflow/
```

### 3.6 Extract and verify on the offline machine

```bash
set -euo pipefail

# Extract the bundle
mkdir -p /opt/osimflow
tar -xzf /data/osimflow/osimflow-offline.tar.gz -C /opt/osimflow/

# Load Docker images from the tar archives
docker load -i /opt/osimflow/offline/docker/nrel_openstudio_3.11.0.tar
docker load -i /opt/osimflow/offline/docker/ghcr.io_anchapin_scientific_python_image_latest.tar
docker images | grep -E "openstudio|scientific"

# Install pip packages from local wheels (no PyPI access needed)
pip install --no-index --find-links=/opt/osimflow/offline/pip/ osimflow

# Verify the install
osimflow --version
```

---

## 4. Using `--offline` and `--offline-bundle`

### 4.1 Basic offline campaign run

```bash
set -euo pipefail

osimflow run \
    --offline \
    --offline-bundle /opt/osimflow/offline \
    --executor local \
    --input_variables /data/models/variables.yml \
    --template_sim_package /data/models/example_package \
    --n_samples 50 \
    --outdir /data/results/run01 \
    --openstudio_version 3.11.0
```

### 4.2 What the flags mean

| Flag | Subcommand | Effect |
|---|---|---|
| `--offline` | `run` | Enables air-gapped mode. Skips Docker Hub pulls, PyPI version checks, and online weather downloads. |
| `--offline-bundle <path>` | `run` | Points to the offline bundle directory (containing `pip/`, `docker/`, `weather/` subdirectories). Required when `--offline` is set. |
| `--offline` | `health` | Skips the network connectivity check in the health subcommand. |

The bundle path is read from `CampaignConfig.offline_bundle` (defined in
`osimflow/config.py`). When `--offline` is active, the campaign:

- Uses locally-loaded Docker images instead of pulling from Docker Hub.
- Passes `--no-index --find-links=<bundle>/pip/` to any internal pip
  invocations instead of reaching PyPI.
- Skips weather file URL downloads; reads only from the bundle's
  `weather/` subdirectory or the `template_sim_package`.
- Skips version-check pings to PyPI / Docker Hub.
- Uses the local ECR/repository if `--ecr-repository` points to a
  pre-loaded private registry (see [§5](#5-ecr--private-registry-mirroring)).

### 4.3 Offline health check

Before running a campaign, verify your offline environment is healthy:

```bash
# The health subcommand also accepts --offline to skip the network probe
osimflow health \
    --outdir /data/results \
    --offline \
    --json
```

This checks Python version, core packages, SQLite, write permissions, disk
space, and external tools (OpenStudio / Docker / Podman) — but skips the
network connectivity check. Exit code 0 means all critical checks pass.

### 4.4 Offline dry-run

Validate your offline setup with a single-sample dry-run before launching
the full campaign:

```bash
set -euo pipefail

osimflow run \
    --offline \
    --offline-bundle /opt/osimflow/offline \
    --dry-run \
    --executor local \
    --input_variables /data/models/variables.yml \
    --template_sim_package /data/models/example_package \
    --outdir /data/results/dry-run \
    --openstudio_version 3.11.0
```

The `--dry-run` flag forces `LocalExecutor`, 1 sample, and steps 1–4 only
(see `AGENTS.md` §4 for the DAG step names).

### 4.5 Environment-variable alternatives

If you run OSimFlow inside the pre-built offline Docker image
(see [§8](#8-docker--podman-air-gapped-runtime)), the environment variables
are set automatically:

| Environment variable | Equivalent flag | Default in offline image |
|---|---|---|
| `OSIMFLOW_OFFLINE=1` | `--offline` | `1` |
| `OSIMFLOW_OFFLINE_BUNDLE=<path>` | `--offline-bundle <path>` | `/opt/osimflow/offline` |
| `PIP_NO_INDEX=1` | (internal pip) | `1` |
| `PIP_FIND_LINKS=file://<bundle>/pip` | (internal pip) | `file:///opt/osimflow/offline/pip` |

---

## 5. ECR / Private Registry Mirroring

For cloud deployments (and large on-premise teams), mirroring the
OpenStudio image to a private registry is more robust than shipping tar
files. This section covers the AWS ECR path; for the full rationale (rate
limits, reliability, cost), see
[`container-image-strategy.md`](container-image-strategy.md).

### 5.1 Mirror with the sync script

`infra/aws/scripts/sync-openstudio-to-ecr.sh` pulls a tagged image from
Docker Hub and pushes it to ECR in one or more regions, with exponential
backoff (2, 4, 8, 16, 32 seconds) to handle Docker Hub rate limits:

```bash
set -euo pipefail

# Single region
./infra/aws/scripts/sync-openstudio-to-ecr.sh \
    --version 3.11.0 \
    --region us-east-1

# Multi-region replication
./infra/aws/scripts/sync-openstudio-to-ecr.sh \
    --version 3.11.0 \
    --region us-east-1 \
    --regions us-east-1,us-west-2,eu-west-1
```

**Prerequisites:**

- Docker daemon running and logged in.
- AWS CLI v2 configured with credentials that can access ECR.
- The ECR repository must exist (provisioned by
  `infra/aws/terraform/ecr.tf`; see
  [`aws-batch-terraform.md`](aws-batch-terraform.md) for the full
  provisioning guide).

The resulting image URI is:

```
<account-id>.dkr.ecr.<region>.amazonaws.com/osimflow/openstudio:<version>
```

### 5.2 Point OSimFlow at the private registry

Once the image is mirrored, pass the ECR URI via `--ecr-repository`:

```bash
set -euo pipefail

osimflow run \
    --executor aws_batch \
    --aws-batch-queue osimflow-batch-queue \
    --aws-batch-job-definition osimflow-openstudio-job-def \
    --ecr-repository 123456789012.dkr.ecr.us-east-1.amazonaws.com/osimflow/openstudio \
    --openstudio_version 3.11.0 \
    --offline \
    --input_variables variables.yml \
    --template_sim_package ./example_package \
    --n_samples 1000 \
    --outdir ./results
```

The `--ecr-repository` flag overrides the default Docker Hub source so
that the Batch job definition references the mirrored image. Combined with
`--offline`, this ensures no Docker Hub traffic occurs.

### 5.3 ECR lifecycle policy

The Terraform-defined ECR repository
(`infra/aws/terraform/ecr.tf`) applies a lifecycle policy that **keeps
the 5 most recent tagged images** whose tags start with `3.`. Older images
are automatically expired. Adjust `countNumber` in the policy to change
retention. See [`container-image-strategy.md`](container-image-strategy.md)
for cost estimates (~$0.10/GB/month).

### 5.4 Non-AWS private registries

For on-premise or non-AWS environments, use any OCI-compatible registry
(Harbor, Nexus, GitLab Container Registry, Quay). The pattern is the same:

1. Pull `nrel/openstudio:<version>` from Docker Hub (online).
2. `docker tag` it with your private registry URI.
3. `docker push` to your registry.
4. Pass the registry URI via `--ecr-repository` (the flag name is
   historical; it accepts any registry URI, not just ECR).

---

## 6. Air-Gapped HPC (Slurm / Singularity)

Most HPC clusters run Singularity (or its successor, Apptainer) instead of
Docker, and compute nodes typically have no internet access. This section
covers the full offline HPC workflow.

### 6.1 Build the Singularity image (online machine)

Convert the Docker image to a Singularity Image Format (SIF) file while
you still have internet access:

```bash
set -euo pipefail

# Option A: Pull directly from Docker Hub and convert to SIF
singularity pull docker://nrel/openstudio:3.11.0 \
    --name openstudio-3.11.0.sif

# Or with Apptainer (the successor to Singularity)
apptainer pull docker://nrel/openstudio:3.11.0 \
    --name openstudio-3.11.0.sif

# Option B: Convert from a Docker tar archive (if you already have one)
singularity build openstudio-3.11.0.sif \
    docker-archive://nrel-openstudio-3.11.0.tar
```

Place the `.sif` file on shared storage accessible to all compute nodes:

```bash
set -euo pipefail
mkdir -p /scratch/$USER/singularity-images
mv openstudio-3.11.0.sif /scratch/$USER/singularity-images/
```

### 6.2 Transfer the offline bundle + SIF

```bash
set -euo pipefail

# Transfer the bundle and the SIF together to the HPC login node
rsync -avP /opt/osimflow/offline/ airgap-hpc:/opt/osimflow/offline/
scp openstudio-3.11.0.sif airgap-hpc:/scratch/$USER/singularity-images/
```

### 6.3 Run a Slurm campaign offline

On the HPC login node (air-gapped):

```bash
set -euo pipefail

module load singularity  # or: module load apptainer

osimflow run \
    --offline \
    --offline-bundle /opt/osimflow/offline \
    --executor slurm \
    --slurm-real \
    --slurm_partition short \
    --slurm_account my-allocation \
    --input_variables /data/models/variables.yml \
    --template_sim_package /data/models/example_package \
    --n_samples 500 \
    --outdir /scratch/$USER/results/run01 \
    --openstudio_version 3.11.0
```

Key points for offline Slurm:

- **`--slurm-real`** is required — without it, `submitit` uses the
  `DebugExecutor` (local), which is the documented default (see
  `AGENTS.md` §8, gotcha #10).
- The pre-built SIF on shared storage is used automatically by the
  `SlurmExecutor` when it detects Singularity as the container runtime.
- The `--offline-bundle` path is passed through the `SINGULARITY_BINDPATH`
  environment variable so compute nodes can read pip wheels and weather
  files from the bundle.

For the full Slurm setup guide (cluster configuration, Singularity
integration, resource directives), see
[`docs/deployment/slurm.md`](deployment/slurm.md).

### 6.4 Slurm without any container runtime

If your cluster has the OpenStudio CLI installed natively (no containers),
offline mode still works — the work function in `osimflow/work.py` detects
`openstudio.cli` on `PATH` via `shutil.which` and invokes it directly. In
that case you only need the pip wheels and weather files in the bundle,
not the Docker images.

---

## 7. Air-Gapped AWS Batch

For AWS Batch deployments with no internet egress (no NAT gateway),
combine ECR mirroring with VPC endpoints so jobs never leave the AWS
network.

### 7.1 Architecture

```
 ┌─────────────────────────────────────────────────────────┐
 │                  VPC (no internet egress)                │
 │                                                         │
 │  ┌──────────┐    ┌──────────────┐    ┌───────────────┐  │
 │  │  Batch   │───►│  VPC Endpoint│───►│  ECR (private)│  │
 │  │ compute  │    │  (com.amazonaws                         │
 │  │  nodes   │    │   .ecr.dkr)  │    │  mirrored     │  │
 │  └────┬─────┘    └──────────────┘    │  OSimFlow img │  │
 │       │                               └───────────────┘  │
 │       │          ┌──────────────┐    ┌───────────────┐  │
 │       └─────────►│  VPC Endpoint│───►│      S3       │  │
 │                  │  (com.amazonaws   │  (artifacts)  │  │
 │                  │   .s3)       │    └───────────────┘  │
 │                  └──────────────┘                       │
 └─────────────────────────────────────────────────────────┘
```

### 7.2 Prerequisites

1. **Mirror the OpenStudio image to ECR** using the sync script (see
   [§5.1](#51-mirror-with-the-sync-script)).
2. **Configure VPC endpoints** for ECR and S3 inside your VPC. The
   Terraform module in `infra/aws/terraform/` provisions the VPC,
   security group, S3 bucket, IAM roles, Batch compute environment, job
   queue, and job definition. See
   [`aws-batch-terraform.md`](aws-batch-terraform.md) for the full
   provisioning guide.
3. **Use a Batch job definition that references the ECR image**, not
   Docker Hub. The job definition container image should be:
   ```
   <account-id>.dkr.ecr.<region>.amazonaws.com/osimflow/openstudio:<version>
   ```
4. **Disable NAT gateway egress** (or omit it entirely) so the compute
   environment has no route to the public internet.

### 7.3 Run the campaign

```bash
set -euo pipefail

osimflow run \
    --executor aws_batch \
    --aws-batch-queue osimflow-batch-queue \
    --aws-batch-job-definition osimflow-openstudio-job-def \
    --ecr-repository 123456789012.dkr.ecr.us-east-1.amazonaws.com/osimflow/openstudio \
    --openstudio_version 3.11.0 \
    --offline \
    --result-storage-backend s3 \
    --result-storage-bucket osimflow-campaign-artifacts \
    --input_variables variables.yml \
    --template_sim_package ./example_package \
    --n_samples 1000 \
    --outdir ./results
```

### 7.4 IAM considerations

The Batch task role (provisioned in
`infra/aws/terraform/iam.tf`) is scoped to the campaign S3 bucket and
CloudWatch Logs — never to ECR pull permissions (those are on the
task-execution role). No long-lived AWS access keys are used; credentials
come from the IAM role on the compute environment. See `AGENTS.md` §10
for the security model.

---

## 8. Docker / Podman Air-Gapped Runtime

For environments that use Docker or Podman (not Singularity), OSimFlow
ships a pre-built offline image and a Docker Compose configuration.

### 8.1 Build the offline Docker image

The multi-stage Dockerfile at `infra/offline/Dockerfile.offline` bundles
pip wheels and Docker images into a single portable image:

```bash
set -euo pipefail

# Build on the online machine
docker build -f infra/offline/Dockerfile.offline \
    --build-arg PIP_EXTRAS=dev,aws,slurm \
    --build-arg OS_VERSION=3.11.0 \
    -t osimflow-offline:latest .

# Save to tar for air-gapped transfer
docker save osimflow-offline:latest -o osimflow-offline.tar
```

The resulting image has `OSIMFLOW_OFFLINE=1` and
`OSIMFLOW_OFFLINE_BUNDLE=/opt/osimflow/offline` baked in as environment
variables, so you don't need to pass `--offline` / `--offline-bundle`
explicitly when running inside it.

### 8.2 Transfer and load

```bash
set -euo pipefail

# On the air-gapped machine
docker load -i /data/images/osimflow-offline.tar
docker load -i /data/images/nrel-openstudio-3.11.0.tar

# Smoke test
docker run --rm osimflow-offline:latest --help
```

### 8.3 Run via Docker Compose

The Compose file at `infra/offline/docker-compose.airgapped.yml`
configures volumes, resource limits, and an isolated network (no internet
egress):

```bash
set -euo pipefail

# Set paths for bind mounts
export OFFLINE_BUNDLE_PATH=/opt/osimflow/offline
export INPUT_PATH=/data/inputs
export OUTPUT_PATH=/data/outputs

# Run a campaign
docker compose -f infra/offline/docker-compose.airgapped.yml \
    run --rm osimflow \
    osimflow run \
        --offline \
        --offline-bundle /opt/osimflow/offline \
        --executor local \
        --input_variables /data/inputs/variables.yml \
        --template_sim_package /data/inputs/example_package \
        --n_samples 10 \
        --outdir /data/outputs/run01 \
        --openstudio_version 3.11.0
```

The Compose file also includes an optional `pip-mirror` service (under the
`pip-mirror` profile) for serving wheels to multiple machines over an
internal HTTP server. See
[`infra/offline/local-pip-mirror/README.md`](../infra/offline/local-pip-mirror/README.md)
for the full pip-mirror setup.

### 8.4 Podman

Podman is a drop-in replacement for Docker in rootless HPC environments.
The commands are identical — substitute `podman` for `docker`. OSimFlow
also has a dedicated Podman guide at [`podman-guide.md`](podman-guide.md).

---

## 9. Troubleshooting

### 9.1 "image not found" when running in offline mode

**Cause:** The Docker/Singularity image was not loaded into the local
cache before the campaign started.

**Fix:**

```bash
# Verify the image is present
docker images | grep openstudio

# If missing, load it from the bundle
docker load -i /opt/osimflow/offline/docker/nrel_openstudio_3.11.0.tar
```

For Singularity, verify the `.sif` file exists on shared storage and the
path is accessible from compute nodes:

```bash
ls -lh /scratch/$USER/singularity-images/openstudio-3.11.0.sif
```

### 9.2 pip install fails with "No matching distribution"

**Cause:** The `--offline-bundle` path is wrong, the `pip/` directory is
empty, or the wheels were bundled for the wrong Python version / platform.

**Fix:**

```bash
# Verify the bundle path and contents
ls /opt/osimflow/offline/pip/*.whl | head -5

# Verify the manifest
cat /opt/osimflow/offline/bundle_manifest.json | python -m json.tool | head -20

# Re-install from the correct path
pip install --no-index --find-links=/opt/osimflow/offline/pip/ osimflow
```

Ensure the Python version on the offline machine matches the one used to
build the bundle (Python 3.12+). Wheels are platform-specific — a
`manylinux` wheel built on x86_64 will not install on arm64.

### 9.3 Weather file missing

**Cause:** The bundle did not include the `.epw` file referenced in
`variables.yml`.

**Fix:** Re-run the bundle script with the correct `--weather-dir` (and
optionally `--variables`) on the online machine:

```bash
set -euo pipefail
python scripts/bundle_offline.py \
    --weather-only \
    --variables variables.yml \
    --weather-dir /data/models/example_package/weather \
    --output /tmp/weather-bundle.tar.gz
```

Extract and copy the `.epw` files into the existing bundle's `weather/`
directory on the offline machine.

### 9.4 DNS resolution errors

**Cause:** The environment has no DNS (fully air-gapped), but a process is
still trying to resolve a hostname (e.g., a Docker registry URI or a
weather-file URL).

**Fix:**

- Ensure `--offline` is set on **every** `osimflow run` invocation.
- If using `--ecr-repository`, make sure the URI points to a VPC endpoint
  (private DNS), not a public hostname.
- For Singularity, ensure the image is referenced as a local `.sif` path,
  not `docker://...` (which triggers a network pull).
- Check `~/.pip/pip.conf` or `/etc/pip.conf` — remove any `index-url` that
  points to `pypi.org`; use `find-links` pointing to the local wheel
  directory instead.

### 9.5 Certificate validation / TLS errors

**Cause:** A corporate proxy intercepts TLS, or a self-signed certificate
is used for a private registry.

**Fix:**

- For private registries with self-signed certs, add the CA certificate to
  the system trust store:
  ```bash
  set -euo pipefail
  cp corporate-ca.pem /etc/pki/ca-trust/source/anchors/
  update-ca-trust
  ```
- For Docker, configure the daemon to trust the registry:
  ```
  # /etc/docker/daemon.json
  {
    "insecure-registries": ["registry.internal:5000"]
  }
  ```
  Then restart Docker: `sudo systemctl restart docker`.
- For pip, point to the CA bundle:
  ```bash
  pip install --cert /etc/pki/ca-trust/corporate-ca.pem \
      --no-index --find-links=/opt/osimflow/offline/pip/ osimflow
  ```

### 9.6 Docker Hub rate limit (429 Too Many Requests)

**Cause:** Even on the online build machine, you may hit Docker Hub's
100-pulls/6h anonymous limit when building bundles repeatedly.

**Fix:** The sync script already handles this with exponential backoff.
For the bundle script, log in to Docker Hub first:

```bash
docker login  # use a free account for 200 pulls/6h
```

Or mirror to ECR / a private registry and build bundles from there (see
[§5](#5-ecr--private-registry-mirroring)).

### 9.7 Bundle checksum mismatch after transfer

**Cause:** The tarball was corrupted during transfer (network error,
truncated copy, USB filesystem issue).

**Fix:** Compare the SHA-256 of the tarball on both machines:

```bash
# Online machine
sha256sum /tmp/osimflow-offline.tar.gz

# Offline machine
sha256sum /data/osimflow/osimflow-offline.tar.gz
```

If they differ, re-transfer. The `bundle_manifest.json` inside the bundle
also contains per-file checksums for verifying individual assets after
extraction (see [§3.4](#34-bundle-directory-structure)).

---

## References

- [`scripts/bundle_offline.py`](../scripts/bundle_offline.py) — the bundle
  creation script (run `python scripts/bundle_offline.py --help` for the
  full flag reference).
- [`infra/offline/Dockerfile.offline`](../infra/offline/Dockerfile.offline)
  — multi-stage Docker build for air-gapped deployment.
- [`infra/offline/docker-compose.airgapped.yml`](../infra/offline/docker-compose.airgapped.yml)
  — Docker Compose configuration for air-gapped runtime.
- [`infra/offline/local-pip-mirror/README.md`](../infra/offline/local-pip-mirror/README.md)
  — local pip mirror setup for multi-machine deployments.
- [`infra/aws/scripts/sync-openstudio-to-ecr.sh`](../infra/aws/scripts/sync-openstudio-to-ecr.sh)
  — ECR mirroring script with exponential-backoff retry.
- [`container-image-strategy.md`](container-image-strategy.md) — why and
  how to mirror images to ECR (rate limits, reliability, cost).
- [`openstudio-image-distribution.md`](openstudio-image-distribution.md)
  — where the OpenStudio CLI container comes from.
- [`aws-batch-terraform.md`](aws-batch-terraform.md) — zero-to-running
  AWS Batch deployment guide (VPC, IAM, compute environment).
- [`air-gapped-deployment.md`](air-gapped-deployment.md) — the shorter
  quick-start companion to this guide.
- [`podman-guide.md`](podman-guide.md) — rootless container runtime
  alternative to Docker.
- [`deployment/slurm.md`](deployment/slurm.md) — Slurm / HPC deployment
  guide with Singularity integration.
- [Issue #261](https://github.com/anchapin/OSimFlow/issues/261) — upstream
  tracking issue for offline mode.
- [Issue #399](https://github.com/anchapin/OSimFlow/issues/399) — this
  documentation guide.
