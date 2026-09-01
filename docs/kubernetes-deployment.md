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

### Ephemeral Runner & Object-Storage Transport (issue #996)

Each Job executes campaign work with the same **ephemeral-runner pattern** the `NomadExecutor` uses (`osimflow/executors/__init__.py` + `osimflow/remote_runner.py`): the Job container runs

```text
python -m osimflow.remote_runner
```

(or an explicit `remote_command` override via `/bin/sh -c`). The runner decodes the task payload, executes the step work function entirely in container-local storage (`emptyDir`-style; no RWX/NFS volume and no orchestrator-filesystem access needed), and pushes result artifacts to object storage when a result-storage backend is configured. The executor handle then downloads those artifacts (`materialize_object_storage_result`) so Campaign callbacks receive local paths — the same contract as Nomad.

Environment variables carried on every Job:

| Env Var | Purpose |
|---|---|
| `OSIMFLOW_TASK_PAYLOAD` | JSON-serialized step call (`schema_version`, `name`, `step`, encoded `args`/`kwargs`, `result_hint`) — identical serialization to `NomadExecutor._build_task_payload()` |
| `OSIMFLOW_RESULT_TRANSPORT_MODE` | `shared_fs` (default) or `object_storage` |
| `OSIMFLOW_RESULT_STORAGE_BACKEND` | Result backend (`s3`, `gs`, `azure`) when object-storage transport is active |
| `OSIMFLOW_RESULT_STORAGE_BUCKET` | Bucket/container name |
| `OSIMFLOW_RESULT_STORAGE_PREFIX` | Key prefix (the campaign `outdir` name) |
| `OSIMFLOW_RESULT_STORAGE_ENDPOINT` | Custom S3-compatible endpoint (e.g. MinIO) |
| `OSIMFLOW_OS_VERSION` / `OSIMFLOW_CONTAINER` | Pinned OpenStudio version / resolved container image |
| `OSIMFLOW_STUB_SIM` | Propagated from the orchestrator when set, so pods honour the stub-vs-real CLI choice |

To run a zero-shared-FS campaign, configure a result backend (and MinIO works for on-prem clusters via `--result-storage-endpoint`):

```bash
osimflow run \
  --executor kubernetes \
  --kubernetes-namespace osimflow \
  --result-storage-backend s3 \
  --result-storage-bucket osimflow-results \
  --result-storage-endpoint http://minio.minio.svc:9000 \
  --input_variables variables.yml \
  --template_sim_package ./example_package \
  --n_samples 50 \
  --outdir ./results \
  --openstudio_version 3.11.0
```

**Worker image prerequisite:** the container images must ship the `osimflow` package for `python -m osimflow.remote_runner` to resolve. The Python-side steps (apply/KPI/aggregate/plots) use the image resolved from `OSIMFLOW_PYTHON_CONTAINER_IMAGE` (default `ghcr.io/anchapin/scientific_python_image:latest`); the sim steps use `nrel/openstudio:<version>`, which must be extended with `pip install osimflow` (e.g. `FROM nrel/openstudio:3.11.0` + `RUN pip install osimflow` in a thin derived image pushed to your registry).

**Service account permissions:** when using object-storage transport, the worker service account (or the nodes' cloud identity — IRSA on EKS, Workload Identity on GKE, Azure AD Workload Identity on AKS) needs read/write access to the result bucket in addition to the Kubernetes RBAC permissions below. For S3-compatible endpoints, provide the credentials via the environment the pods run in (e.g. a Kubernetes Secret projected into the Job env).

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
| `--kubernetes-backoff-limit` | `0` | Native Job `backoffLimit` (issue #997). Defaults to 0, mirroring the orchestrator-side retry from `--max-sample-retries` (issue #252). Set to >0 to enable K8s-native pod retry as an alternative: the kubelet restarts the failed pod up to this many times without a resubmit round-trip through the orchestrator. Pick one mechanism, not both — running K8s-native retry and `--max-sample-retries` together will double-count failures. |
| `--kubernetes-ttl-seconds-after-finished` | unset | Native Job `ttlSecondsAfterFinished` in seconds (issue #997). When set, the API server garbage-collects completed/failed Jobs after this delay, releasing etcd footprint and pod resources across a large sweep. Recommended for campaigns of >100 samples. |
| `--kubernetes-queue-name` | unset | Kueue ClusterQueue name applied as the `kueue.x-k8s.io/queue-name` label on Job metadata (issue #997). When set, Kueue manages the Job through suspend/resume and honors fair-sharing, priority, and preemption across the cluster. Inert on clusters without Kueue installed. |

## Kueue Interplay (issue #997)

When `--kubernetes-queue-name` is set on a cluster with [Kueue](https://kueue.sigs.k8s.io/) installed, Kueue manages the Job lifecycle at the API-server level:

- **Quota exhaustion suspends Jobs.** When a ClusterQueue runs out of quota, Kueue leaves the Job's pod in `Pending` until quota is freed by another workload completing. The executor's existing `_wait_for_terminal` polling already tolerates `Pending` (it returns `{"status": {"phase": "Pending"}}` when no pod exists yet and continues polling), so the orchestrator does NOT need to know whether the pod is suspended vs. waiting for a node — it simply waits.
- **`activeDeadlineSeconds` only counts active execution.** Kueue's suspend/resume window does not count toward the per-Job `activeDeadlineSeconds` (which is set from `time_min`). A Queue that holds a Job suspended for an hour does not advance that Job's deadline. This is a feature, not a bug — quotas can be reclaimed without paying the wall-clock penalty against the active job.
- **The orchestrator's `--max-sample-retries` and K8s `backoffLimit` are alternatives.** Pick one: K8s-native retry is fast (no resubmit round-trip) but only retries within the same Job; orchestrator-side retry can replace the entire Job with a different parameter sample (useful in `--detach`/`Coordinator` mode where resubmit latency matters).

For a shared multi-tenant cluster, set `--kubernetes-queue-name` to a Kueue ClusterQueue managed by your platform team; you do not need to create the queue — only reference it. The executor fails fast if Kueue rejects the Job (e.g., `kueue.x-k8s.io/queue-name` resolves to a nonexistent ClusterQueue), and the `_wait_for_terminal` loop surfaces the pod's failure reason through the per-sample error log.

## Security

Credentials are sourced from the in-cluster service account or from `~/.kube/config`. The `KubernetesExecutor` does **not** accept explicit credentials; using the configured kubeconfig or in-cluster service account is the recommended path.

For production deployments, use RBAC to restrict the service account to the minimum required permissions (`create`, `get`, `list` on Jobs and Pods in the target namespace). When object-storage transport is enabled (issue #996), the **worker** service account additionally needs object-storage read/write permissions on the result bucket (see *Ephemeral Runner & Object-Storage Transport* above).

### Strict Pod SecurityContext (issue #1383)

By default, the `KubernetesExecutor` submits every pod with a hardened security context. The `security_context_strict` flag is a **constructor flag** on `KubernetesExecutor` (`osimflow/executors/kubernetes_executor.py`) — there is **no CLI flag**; it can only be set when instantiating the executor programmatically. The default is `True`.

When strict, both the per-sample Job pods and the version-check pod carry:

| Scope | Fields |
|---|---|
| Pod `securityContext` | `runAsNonRoot: true`, `runAsUser: 1000` |
| Container `securityContext` | `runAsNonRoot: true`, `readOnlyRootFilesystem: true`, `allowPrivilegeEscalation: false`, `capabilities.drop: ["ALL"]` |
| Pod spec | `automountServiceAccountToken: false` |

Equivalent manifest fragment:

```yaml
spec:
  automountServiceAccountToken: false
  securityContext:
    runAsNonRoot: true
    runAsUser: 1000
  containers:
    - name: osimflow
      securityContext:
        runAsNonRoot: true
        readOnlyRootFilesystem: true
        allowPrivilegeEscalation: false
        capabilities:
          drop: ["ALL"]
```

This profile satisfies the Pod Security Standards **`restricted`** level, so campaign pods are admitted on clusters enforcing `restricted` via namespace labels. Pod Security admission exempts unset fields from its checks, so the executor's unset `seccompProfile` is not a blocker; clusters whose admission configuration additionally requires `seccompProfile: RuntimeDefault` can layer it separately.

`automountServiceAccountToken: false` blocks the cluster-default service-account token pivot raised in the issue #1177 threat model.

Set `security_context_strict=False` to fall back to the legacy permissive manifest — only for clusters whose admission controllers reject the strict profile (e.g. older admission controllers without PodSecurity support).

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

## Coordinator High Availability

The Kubernetes cluster provides high availability for the **executor layer**
(pods running simulation work are rescheduled automatically on node
failure). However, the OSimFlow **coordinator** (`Campaign` class) is a
single-instance process. See
[ADR-0003](.agents/results/architecture/0003-coordinator-high-availability.md)
for the full analysis and supported HA patterns.

Two patterns are supported for coordinator HA on Kubernetes:

**Pattern 1 — Shared filesystem**: Mount a shared PersistentVolume (NFS,
Azure Files, GCS FUSE, etc.) as the `--outdir` on all coordinator pods.
The `JobQueue.recover()` mechanism handles crash recovery automatically.
See ADR-0003 §Pattern 1 for details.

**Pattern 2 — Campaign-per-worker (recommended)**: Deploy multiple
coordinator Jobs via Kubernetes, each processing a disjoint subset of
samples with its own `--outdir`. Use a `JobSet` or `Flux` to manage the
coordinator pool. Sample partitioning is handled by an external script
or Apache Airflow DAG that submits N coordinator Jobs with
non-overlapping sample ranges. See ADR-0003 §Pattern 2 for details.

## Helm Chart Worker Deployment (issue #583)

The OSimFlow Helm chart (`osimflow-deploy/kubernetes/helm/osimflow/`) can deploy a
**worker Deployment** that runs the campaign coordinator natively within the
cluster, using `KubernetesExecutor` to fan out per-sample Jobs. This enables
fully containerized OSimFlow campaigns on Kubernetes without requiring an
external executor (Slurm, AWS Batch).

### Enable the Worker

```bash
helm install osimflow ./osimflow-deploy/kubernetes/helm/osimflow \
  --set worker.enabled=true \
  --set worker.campaign_args="--input_variables /data/variables.yml --template_sim_package /data/example_package --n_samples 100 --openstudio_version 3.11.0" \
  --set openstudio.version=3.11.0
```

### Worker Configuration

| Value | Default | Description |
|---|---|---|
| `worker.enabled` | `false` | Enable the worker Deployment |
| `worker.replica_count` | `1` | Number of worker replicas |
| `worker.executor` | `kubernetes` | Executor type for fan-out (only `kubernetes` supported) |
| `worker.job_queue` | `none` | Task queue backend: `none`, `redis`, or `dask` |
| `worker.campaign_args` | `""` | Campaign arguments passed to `osimflow run` |
| `worker.image` | openstudio image | Custom worker image (optional) |
| `worker.redis.enabled` | `false` | Deploy a Redis sidecar for job queue |
| `worker.redis.host` | `osimflow-redis` | Redis hostname |
| `worker.dask.scheduler_address` | `""` | Dask scheduler address (e.g. `tcp://scheduler:8786`) |

### Worker with Redis Job Queue

For multi-replica worker deployments with Redis-backed coordination:

```bash
helm install osimflow ./osimflow-deploy/kubernetes/helm/osimflow \
  --set worker.enabled=true \
  --set worker.replica_count=3 \
  --set worker.job_queue=redis \
  --set worker.redis.enabled=true \
  --set worker.campaign_args="--input_variables /data/variables.yml --template_sim_package /data/example_package --n_samples 500 --openstudio_version 3.11.0" \
  --set openstudio.version=3.11.0
```

### Worker with Dask Job Queue

```bash
helm install osimflow ./osimflow-deploy/kubernetes/helm/osimflow \
  --set worker.enabled=true \
  --set worker.job_queue=dask \
  --set worker.dask.scheduler_address="tcp://dask-scheduler:8786" \
  --set worker.campaign_args="--input_variables /data/variables.yml --template_sim_package /data/example_package --n_samples 500 --openstudio_version 3.11.0" \
  --set openstudio.version=3.11.0
```

### Mounting Campaign Data

Campaign inputs (`variables.yml`, `template_sim_package`) are mounted at `/data`
via an `emptyDir` volume by default. For production, replace the `emptyDir` volume
with a `PersistentVolumeClaim` to persist results across pod restarts:

```yaml
# values-overrides.yaml
worker:
  enabled: true
  campaign_args: "--input_variables /data/variables.yml --template_sim_package /data/example_package --n_samples 100 --openstudio_version 3.11.0"

# In your overrides, replace the emptyDir with a PVC:
volumes:
  data:
    persistentVolumeClaim:
      claimName: osimflow-data-pvc
```

### Resource Limits

Worker resources default to:

```yaml
worker:
  resources:
    requests:
      cpu: 500m
      memory: 1Gi
    limits:
      cpu: 2000m
      memory: 4Gi
```

Per-sample Jobs use the values from `resources.cpu` and `resources.memory` in
`values.yaml`, which default to 1 CPU core and 2 GiB memory per sample.

### REST API + Worker Together

Deploy both the REST API server and the worker in the same Helm release:

```bash
helm install osimflow ./osimflow-deploy/kubernetes/helm/osimflow \
  --set api.enabled=true \
  --set worker.enabled=true \
  --set worker.campaign_args="--input_variables /data/variables.yml --template_sim_package /data/example_package --n_samples 100 --openstudio_version 3.11.0" \
  --set openstudio.version=3.11.0
```

The API server (`osimflow serve`) provides monitoring endpoints; the worker
Deployment runs the actual campaign execution.
