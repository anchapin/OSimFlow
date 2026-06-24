# Container Image Customization Guide

> **Related issues:** #397
> **See also:** [`docs/container-image-strategy.md`](container-image-strategy.md) for the broader ECR mirroring strategy and cost discussion.

OSimFlow consumes two pre-built container images. This guide explains how to build, tag, and push custom variants when you need pre-installed measures, custom Ruby gems, patched OpenStudio builds, or additional Python packages.

---

## 1. Overview

OSimFlow's container strategy uses two images:

| Image | Registry | Purpose |
|---|---|---|
| `nrel/openstudio:<version>` | Docker Hub | OpenStudio CLI + runtime |
| `ghcr.io/anchapin/scientific_python_image:latest` | GitHub Container Registry | Scientific Python stack (pandas, scipy, etc.) |

Both images are consumed at runtime — OSimFlow never builds them during a campaign. Customization is needed when:

- A **custom Ruby gem** must be available in the container without internet access (HPC air-gapped environments).
- A **pre-installed OpenStudio measure** (Ruby or Python) must ship with the image rather than being bundled in the `template_sim_package`.
- A **patched OpenStudio build** is required (e.g., a bug fix not yet in a released NREL image).
- Additional **Python packages** are needed in the scientific Python image (e.g., `tensorflow`, `xgboost`).

---

## 2. Base Images

### `nrel/openstudio` (NREL, Docker Hub)

NREL publishes official OpenStudio images to Docker Hub. Tags follow the pattern `nrel/openstudio:<version>` (e.g., `nrel/openstudio:3.11.0`).

**What the image provides:**

- OpenStudio CLI (`openstudio.cli`) and all core utilities
- EnergyPlus runtime
- Ruby interpreter with the standard gem set
- Python 3.x with OpenStudio Python bindings

NREL's Dockerfiles are public — see the [NREL/OpenStudio-resources](https://github.com/NREL/OpenStudio-resources) repository for the build pattern. The images are based on `ubuntu:22.04` and install OpenStudio via the official installer.

### `ghcr.io/anchapin/scientific_python_image` (project-owned, GitHub Container Registry)

The project's scientific Python image is published at `ghcr.io/anchapin/scientific_python_image`. Its Dockerfile lives at `infra/offline/Dockerfile.offline` (Stage 2 is the runtime).

**What the image provides:**

- Python 3.12 (`python:3.12-slim` base)
- Scientific stack: `numpy`, `scipy`, `pandas`, `pyarrow`, `matplotlib`, `seaborn`
- OSimFlow installed from source with `[aws,slurm]` extras
- Non-root user `osimflow` (UID 1000) for HPC environments

---

## 3. Building a Custom OpenStudio Image

Extend `nrel/openstudio` when you need custom Ruby gems or pre-installed measures.

### Dockerfile template

```dockerfile
# custom-openstudio/Dockerfile
# Extends nrel/openstudio with custom gems and measures.
# Build: docker build -t my-registry/custom-openstudio:3.11.0 .
# Push: docker push my-registry/custom-openstudio:3.11.0

FROM nrel/openstudio:3.11.0

# Add custom Ruby gems via Gemfile
COPY Gemfile /tmp/Gemfile
COPY Gemfile.lock /tmp/Gemfile.lock
WORKDIR /tmp
RUN bundle install --jobs 4

# Copy pre-bundled measures into the OpenStudio search path
# The default measure search path is /opt/openstudio/measures
COPY ./my_measures /opt/openstudio/measures/my_measures

# Optional: install additional system packages
# RUN apt-get update && apt-get install -y --no-install-recommends \
#     libgomp1 \
# && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/openstudio
```

### Gemfile example

```ruby
# custom-openstudio/Gemfile
source "https://rubygems.org"

# Pre-installed measures shipped with the image
gem "openstudio-workflow", "~> 2.0"
gem "openstudio-common-measures", "~> 1.0"
```

### Adding Python packages via requirements.txt

If you need additional Python packages at container build time (not at OSimFlow runtime), add them to a `requirements.txt` and install with `pip`:

```dockerfile
FROM nrel/openstudio:3.11.0

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
```

> **Note:** Python packages installed in the OpenStudio image are available to the `openstudio` CLI Python environment, not to the OSimFlow worker's Python environment. For OSimFlow-side packages, extend the scientific Python image instead (§5).

### Building and tagging locally

```bash
set -euo pipefail

IMAGE_TAG="my-registry.example.com/custom-openstudio:3.11.0"
docker build -f custom-openstudio/Dockerfile \
    -t "${IMAGE_TAG}" \
    custom-openstudio/

# Smoke test
docker run --rm "${IMAGE_TAG}" openstudio --version
```

### Pushing to a private registry

**ECR (AWS):**

```bash
set -euo pipefail

ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
REGION="us-east-1"
ECR_URI="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com"

# Authenticate Docker to ECR
aws ecr get-login-password --region "${REGION}" \
    | docker login --username AWS --password-stdin "${ECR_URI}"

# Tag and push
docker tag my-registry/custom-openstudio:3.11.0 \
    "${ECR_URI}/custom-openstudio:3.11.0"
docker push "${ECR_URI}/custom-openstudio:3.11.0"
```

**GCR (Google Cloud):**

```bash
set -euo pipefail

PROJECT_ID="my-gcp-project"
REGION="us-central1"
GCR_URI="${PROJECT_ID}.region Artifact registry or gcr.io"

docker tag my-registry/custom-openstudio:3.11.0 \
    "${GCR_URI}/custom-openstudio:3.11.0"
docker push "${GCR_URI}/custom-openstudio:3.11.0"
```

**Docker Hub:**

```bash
set -euo pipefail

docker login -u "${DOCKERHUB_USERNAME}" --password-stdin <<< "${DOCKERHUB_TOKEN}"
docker tag my-registry/custom-openstudio:3.11.0 \
    "${DOCKERHUB_USERNAME}/custom-openstudio:3.11.0"
docker push "${DOCKERHUB_USERNAME}/custom-openstudio:3.11.0"
```

---

## 4. Using `--ecr-repository`

The `--ecr-repository` CLI flag tells the `AWSBatchExecutor` to pull from a custom ECR repository instead of Docker Hub. This is the primary mechanism for pointing OSimFlow at a customized OpenStudio image.

### CLI reference

```
osimflow run --ecr-repository <uri> [other flags...]
```

**Example:**

```bash
osimflow run \
  --executor aws_batch \
  --aws-batch-queue osimflow-batch-queue \
  --aws-batch-job-definition osimflow-openstudio-job-def \
  --ecr-repository 123456789.dkr.ecr.us-east-1.amazonaws.com/osimflow/custom-openstudio \
  --openstudio_version 3.11.0 \
  --input_variables variables.yml \
  --template_sim_package ./example_package \
  --n_samples 100 \
  --outdir ./results
```

### How it works

The `AWSBatchExecutor._resolve_container_image()` method in `osimflow/executors/__init__.py` resolves the image URI:

```python
def _resolve_container_image(self, version: str | None) -> str:
    tag = version or "latest"
    if self.ecr_repository:
        return f"{self.ecr_repository}:{tag}"
    return f"nrel/openstudio:{tag}"
```

When `--ecr-repository` is set, the executor returns `<ecr_repo>:<version>` (e.g., `123456.dkr.ecr.us-east-1.amazonaws.com/osimflow/custom-openstudio:3.11.0`). When omitted, it falls back to `nrel/openstudio:<version>` on Docker Hub.

### Configuration in `CampaignConfig`

The `ecr_repository` field in `osimflow/config.py` stores the URI:

```python
ecr_repository: str | None = (
    None  # e.g. "123456.dkr.ecr.us-east-1.amazonaws.com/osimflow/openstudio"
)
```

It is set via `--ecr-repository` on the CLI and propagated to the `AWSBatchExecutor` constructor.

---

## 5. Building the Scientific Python Image

Extend `ghcr.io/anchapin/scientific_python_image` when you need additional Python packages in the OSimFlow worker's runtime environment.

### Dockerfile template

```dockerfile
# custom-scipy/Dockerfile
# Extends the OSimFlow scientific Python image with custom packages.
# Build: docker build -f custom-scipy/Dockerfile -t my-registry/custom-scipy:latest .
# Push: docker push my-registry/custom-scipy:latest

FROM ghcr.io/anchapin/scientific_python_image:latest

# Install additional Python packages
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# Optional: install GPU support via CUDA base image
# Replace the FROM line above with:
# FROM ghcr.io/anchapin/scientific_python_image:latest AS base
# FROM nvidia/cuda:12.4-runtime-ubuntu22.04 AS cuda
# COPY --from=base /opt/conda /opt/conda
# ENV PATH="/opt/conda/bin:$PATH"
```

### Adding custom Python packages

```bash
# custom-scipy/requirements.txt
tensorflow
xgboost
scikit-learn
```

```bash
set -euo pipefail

docker build -f custom-scipy/Dockerfile \
    -t my-registry/custom-scipy:latest \
    custom-scipy/
```

### Multi-arch builds (amd64/arm64)

For cross-platform HPC or cloud workloads:

```bash
set -euo pipefail

docker buildx create --name mybuilder
docker buildx use mybuilder
docker buildx inspect --bootstrap

docker buildx build \
    --platform linux/amd64,linux/arm64 \
    -f custom-scipy/Dockerfile \
    -t my-registry/custom-scipy:latest \
    --push \
    custom-scipy/
```

> **Prerequisite:** `docker buildx` with a multi-arch builder (e.g., `docker buildx create --driver docker-container`). GitHub Actions runners are `linux/amd64` only; use a remote builder (e.g., AWS EC2 arm64 instances) for `linux/arm64` builds.

---

## 6. Singularity Images for HPC

HPC clusters typically run Singularity instead of Docker. Convert a Docker image to Singularity SIF format for use on Slurm, PBS, or other HPC schedulers.

### Converting Docker to Singularity

```bash
set -euo pipefail

# Pull the Docker image and convert to SIF
singularity build /scratch/custom-openstudio-3.11.0.sif \
    docker://my-registry/custom-openstudio:3.11.0
```

> **Note:** The Docker image must be available on a registry the HPC host can reach. If the cluster has no internet access, build the SIF on a connected machine and `scp` it to the cluster.

### HPC-friendly best practices

**Bind mounts:** Singularity bind-mounts `$HOME`, `/tmp`, and the current working directory by default. Explicit additional binds:

```bash
set -euo pipefail

singularity exec \
    --bind /projects:/projects \
    --bind /data:/data \
    /scratch/custom-openstudio-3.11.0.sif \
    openstudio --version
```

**Environment variables:** Pass environment variables that the simulation needs:

```bash
singularity exec \
    --env OSIMFLOW_OFFLINE=1 \
    --env AWS_REGION=us-east-1 \
    /scratch/custom-openstudio-3.11.0.sif \
    openstudio run -w workflow.osw
```

**Non-root execution:** Most HPC clusters prohibit root. The `nrel/openstudio` image runs as a non-root user by default. If you build a custom image, create a non-root user:

```dockerfile
RUN useradd -m -u 1000 osimflow && \
    chown -R osimflow:osimflow /opt/openstudio
USER osimflow
```

**CUDA/GPU support:** For GPU-enabled simulations:

```bash
singularity exec \
    --nv \
    --bind /etc/localtime:/etc/localtime \
    /scratch/custom-openstudio-3.11.0.sif \
    openstudio run -w workflow.osw
```

The `--nv` flag injects the host CUDA libraries into the container.

---

## 7. CI/CD Integration

Automate image builds and pushes with GitHub Actions.

### Docker build-push action

```yaml
# .github/workflows/custom-image.yml
name: Build Custom OpenStudio Image

on:
  push:
    branches: [main]
    tags: ["custom-image-v*"]
  workflow_dispatch:

jobs:
  build:
    runs-on: ubuntu-latest
    permissions:
      contents: read
      packages: write

    steps:
      - uses: actions/checkout@v4

      - name: Set up Docker Buildx
        uses: docker/setup-buildx-action@v3

      - name: Login to GitHub Container Registry
        uses: docker/login-action@v3
        with:
          registry: ghcr.io
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}

      - name: Build and push
        uses: docker/build-push-action@v6
        with:
          context: custom-openstudio/
          push: true
          tags: |
            ghcr.io/${{ github.repository }}/custom-openstudio:${{ github.sha }}
            ghcr.io/${{ github.repository }}/custom-openstudio:latest
          cache-from: type=gha
          cache-to: type=gha,mode=max
```

### ECR push with lifecycle policy

For AWS-native CI, push to ECR with the lifecycle policy applied via Terraform:

```bash
set -euo pipefail

ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
REGION="us-east-1"
ECR_URI="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com"

aws ecr get-login-password --region "${REGION}" \
    | docker login --username AWS --password-stdin "${ECR_URI}"

docker tag my-registry/custom-openstudio:3.11.0 \
    "${ECR_URI}/custom-openstudio:3.11.0"
docker push "${ECR_URI}/custom-openstudio:3.11.0"
```

The ECR lifecycle policy in `infra/aws/terraform/ecr.tf` keeps the last 5 tagged images matching `3.*`, so older custom image tags are automatically expired.

### Multi-region replication

Reference the sync script at `infra/aws/scripts/sync-openstudio-to-ecr.sh` for multi-region ECR replication:

```bash
set -euo pipefail

./infra/aws/scripts/sync-openstudio-to-ecr.sh \
    --version 3.11.0 \
    --region us-east-1 \
    --regions us-east-1,us-west-2,eu-west-1
```

The script uses exponential backoff (2, 4, 8, 16, 32 seconds) on `docker pull` failures to handle Docker Hub rate limits gracefully. It ensures the ECR repository exists before pushing and supports comma-separated region lists for one-shot multi-region replication.
