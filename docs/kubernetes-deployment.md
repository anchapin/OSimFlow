<!-- docs-skip -->
# Kubernetes Deployment Guide

This guide walks you through running OSimFlow campaigns on a Kubernetes cluster.

**Audience:** IT administrators or engineers setting up Kubernetes infrastructure for running parametric building-energy simulation campaigns. The cluster can be on AWS EKS, Azure AKS, GCP GKE, or any other Kubernetes provider.

## Prerequisites

- A Kubernetes cluster (AWS EKS, Azure AKS, GCP GKE, or self-hosted)
- `kubectl` configured with credentials to the cluster
- A container registry with the `nrel/openstudio` image (or a custom registry)
- The `kubernetes` Python package: `pip install kubernetes`

## How It Works

The `KubernetesExecutor` creates a Kubernetes Job for each simulation sample. Each job runs the OpenStudio container with the simulation work. The executor polls the job status with exponential backoff until the pod reaches a terminal state.

Resource directives (`cpus`, `memory_mb`, `time_min`) are mapped to Kubernetes resource requests and limits:

| OSimFlow Directive | Kubernetes Field |
|---|---|
| `cpus` | `requests.cpu` / `limits.cpu` (inside `V1ResourceRequirements`) |
| `memory_mb` | `requests.memory` / `limits.memory` (inside `V1ResourceRequirements`) |
| `time_min` | `activeDeadlineSeconds` |

Per-sample `OSIMFLOW_OS_VERSION` and `OSIMFLOW_CONTAINER` are set as environment variables on the container, matching the convention used by `SlurmExecutor` and `AWSBatchExecutor`.

## Quick Start

### 1. Configure kubectl

```bash
aws eks update-kubeconfig --name my-cluster  # AWS EKS
# or
az aks get-credentials --name my-cluster --resource-group my-rg  # Azure AKS
# or
gcloud container clusters get-credentials my-cluster  # GCP GKE
```

### 2. Verify cluster access

```bash
kubectl get nodes
```

### 3. Run a campaign

```bash
osimflow run \
  --executor kubernetes \
  --kubernetes-namespace osimflow \
  --input_variables variables.yml \
  --template_sim_package ./example_package \
  --n_samples 50 \
  --outdir ./results \
  --openstudio_version 3.11.0
```

## CLI Flags

| Flag | Default | Description |
|---|---|---|
| `--kubernetes-namespace` | `default` | Kubernetes namespace for jobs |
| `--kubernetes-poll-interval-s` | `5.0` | Poll interval for job status (seconds) |
| `--kubernetes-max-poll-interval-s` | `60.0` | Max poll interval (seconds, exponential backoff cap) |

## Security

Credentials are sourced from the in-cluster service account or from `~/.kube/config`. The `KubernetesExecutor` does **not** accept explicit credentials; using the configured kubeconfig or in-cluster service account is the recommended path.

For production deployments, use RBAC to restrict the service account to the minimum required permissions (`create`, `get`, `list` on Jobs and Pods in the target namespace).

## Resource Allocation

The executor sets both requests and limits to the same values, ensuring the scheduler knows the guaranteed resources while also enforcing an upper bound:

```yaml
resources:
  requests:
    cpu: "4"
    memory: "8192Mi"
  limits:
    cpu: "4"
    memory: "8192Mi"
```

Adjust `cpus` and `memory_mb` per sample based on the simulation complexity. The DAG step defaults in `osimflow/executors/__init__.py` provide sensible starting points.

## Multi-Cloud Example

OSimFlow campaigns run identically on any Kubernetes cluster:

```bash
# AWS EKS
osimflow run --executor kubernetes --kubernetes-namespace osimflow ...

# Azure AKS
osimflow run --executor kubernetes --kubernetes-namespace osimflow ...

# GCP GKE
osimflow run --executor kubernetes --kubernetes-namespace osimflow ...
```

The same `--input_variables`, `--template_sim_package`, and `--n_samples` work across all providers.
