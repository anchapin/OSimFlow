# Kubernetes

OSimFlow Kubernetes deployment via Helm charts.

## Helm chart

The [`helm/`](./helm/) directory contains a Helm chart for deploying OSimFlow batch workloads on Kubernetes.

### Quick start

```bash
# Install the chart
helm install osimflow ./helm/osimflow \
  --set openstudio.version=3.11.0

# Run a campaign using the KubernetesExecutor
osimflow run \
  --executor kubernetes \
  --input_variables variables.yml \
  --template_sim_package ./example_package \
  --n_samples 100 \
  --outdir ./results \
  --openstudio_version 3.11.0
```

### Configuration

```bash
# Override defaults
helm install osimflow ./helm/osimflow \
  --set openstudio.version=3.9.0 \
  --set executor.namespace=osimflow-jobs \
  --set executor.service_account=osimflow-task \
  --set resources.cpu=2 \
  --set resources.memory=4096Mi \
  --set job.backoff_limit=5
```

### REST API server

```bash
helm install osimflow ./helm/osimflow \
  --set api.enabled=true \
  --set ingress.enabled=true \
  --set ingress.host=osimflow.example.com \
  --set openstudio.version=3.11.0
```

### Campaign worker Deployment (issue #583)

Deploy a worker that runs OSimFlow campaigns natively on Kubernetes:

```bash
helm install osimflow ./helm/osimflow \
  --set worker.enabled=true \
  --set worker.campaign_args="--input_variables /data/variables.yml --template_sim_package /data/example_package --n_samples 100 --openstudio_version 3.11.0" \
  --set openstudio.version=3.11.0
```

Multi-replica workers with Redis-backed coordination:

```bash
helm install osimflow ./helm/osimflow \
  --set worker.enabled=true \
  --set worker.replica_count=3 \
  --set worker.job_queue=redis \
  --set worker.redis.enabled=true \
  --set worker.campaign_args="--input_variables /data/variables.yml --template_sim_package /data/example_package --n_samples 500 --openstudio_version 3.11.0" \
  --set openstudio.version=3.11.0
```

See [docs/kubernetes-deployment.md](../../docs/kubernetes-deployment.md) for full documentation.

## KubernetesExecutor

The `KubernetesExecutor` in `osimflow/executors/` submits each sample as a separate Kubernetes Job. It:

- Uses the `kubernetes` Python client (lazy-imported)
- Runs inside a cluster via in-cluster service account credentials
- Maps OSimFlow resource directives (`cpus`, `memory_mb`, `time_min`) to K8s resource requests/limits
- Exports `OSIMFLOW_OS_VERSION` and `OSIMFLOW_CONTAINER` as container env vars (same as `SlurmExecutor`, `AWSBatchExecutor`, and `NomadExecutor`)
- Polls Job status with exponential backoff until terminal state

## Security

- Credentials are sourced from the in-cluster service account token (standard K8s pattern for Pods)
- No `kubeconfig` or long-lived API keys are accepted by the executor
- Jobs run with `restartPolicy: Never` (no restart on failure — the executor handles retries via `backoffLimit`)
- `privileged: false` (no host-level access)

## See also

- [osimflow-deploy README](../README.md)
- [Container image strategy](../../docs/container-image-strategy.md)
- [openstudio-server Kubernetes docs](https://github.com/NREL/openstudio-server/wiki/Kubernetes)
