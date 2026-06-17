# Infrastructure & Cloud Deployment Gap Analysis
# OSimFlow vs openstudio-server

**Date:** 2026-06-16
**Phase:** Gap Analysis
**Comparison Target:** openstudio-server (OpenStudio Analysis Framework/OSAF)

---

## 1. Infrastructure Comparison Table

| Dimension | OSimFlow | openstudio-server | Status |
|-----------|----------|-------------------|--------|
| **Deployment Model** | CLI + library hybrid; campaign-driven | Docker/Helm deployable instance; server-based | Different approaches |
| **HPC Scheduler Support** | Slurm (submitit), PBS (submitit), Dask-JobQueue (Slurm/PBS/K8s) | Slurm, Grid Engine, PBS | Parity |
| **Cloud Providers** | AWS Batch, Azure Batch, Google Cloud Batch | AWS (via Pat-Run), Azure, Google Cloud | Partial parity |
| **Container Orchestration** | Docker (local/cloud), Singularity (HPC), Kubernetes executor | Docker-based containers via PAT | Partial parity |
| **Local Execution** | LocalExecutor (thread pool) | Local server mode | Parity |
| **Execution Interface** | `BaseExecutor.submit()` → `Handle` pattern | Server API + PAT GUI | Different models |
| **Distributed Cache** | Redis pub/sub (DistributedCache) + SQLite local | Not explicitly documented | OSimFlow leads |
| **Job Queue Management** | Filesystem-based (JobQueue), DaskTaskQueue | Rserve + OpenStudio Analysis Gem | Different approaches |
| **Storage Backends** | Local, S3, GCS, Azure Blob | S3 (via analysis gem) | Parity |
| **Observability** | CloudWatch, Prometheus, OpenTelemetry, MLflow, run.json | Limited to PAT logging | OSimFlow leads |
| **Resource Auto-Scaling** | DaskJobQueueExecutor (elastic HPC) | Not explicitly documented | OSimFlow leads |
| **Container Registry** | Docker Hub (nrel/openstudio), ghcr.io (scientific_python_image) | Docker Hub | Parity |
| **Infrastructure as Code** | Terraform (AWS Batch), Nomad HCL configs | Not IaC-first | Partial |

---

## 2. Identified Gaps

### 2.1 Container Orchestration

#### Gap: Helm/Kubernetes Native Deployment
- **Gap Name:** No Helm Chart for Kubernetes Deployment
- **Description:** openstudio-server can be deployed via Docker or Helm. OSimFlow has a `KubernetesExecutor` but lacks a Helm chart or Kubernetes-native deployment manifest for the OSimFlow server/API component.
- **Severity:** Major
- **openstudio-server Approach:** Helm charts provide a production-ready deployment with configurable parameters, rolling updates, and rollback capabilities.
- **Evidence:** OSimFlow has `KubernetesExecutor` in `osimflow/executors/kubernetes_executor.py` but no `infra/kubernetes/` Helm charts or K8s deployment manifests for the OSimFlow control plane.

#### Gap: Singularity HPC Container Support
- **Gap Name:** Singularity Container Runtime Integration
- **Description:** OSimFlow mentions Singularity support in AGENTS.md but lacks explicit Singularity-specific executor or container runtime detection/invocation logic.
- **Severity:** Minor
- **openstudio-server Approach:** Uses Docker containers consistently across platforms.
- **Evidence:** AGENTS.md §2 states "Docker (local/cloud) and Singularity (HPC)" but no SingularityExecutor exists in `osimflow/executors/`.

---

### 2.2 Cloud Provider Support

#### Gap: Google Cloud Batch Executor Maturity
- **Gap Name:** Google Cloud Batch Executor is Stub/Placeholder
- **Description:** `GoogleBatchExecutor` exists but is mentioned as less mature than AWS/Azure implementations. Lacks spot/preemptible instance handling, cost estimation, and comprehensive error handling.
- **Severity:** Major
- **openstudio-server Approach:** Supports Google Cloud through PAT integration.
- **Evidence:** `osimflow/executors/google_batch_executor.py` — only basic SDK wiring; no spot price ceiling, fallback to on-demand, or cost tracking like AWSBatchExecutor has.

#### Gap: Multi-Cloud Job Coordination
- **Gap Name:** No Cross-Cloud Job Federation
- **Description:** OSimFlow executors are single-cloud; a campaign cannot transparently fan out across AWS Batch and Google Cloud Batch simultaneously.
- **Severity:** Minor
- **openstudio-server Approach:** Not explicitly documented as a feature.
- **Evidence:** Each executor class (`AWSBatchExecutor`, `AzureBatchExecutor`, `GoogleBatchExecutor`) is self-contained with no federation abstraction.

---

### 2.3 HPC Scheduler Support

#### Gap: IBM Spectrum LSF Support
- **Gap Name:** No LSF Scheduler Executor
- **Description:** OSimFlow supports Slurm, PBS, Dask-JobQueue (which can wrap Slurm/PBS/K8s), but not IBM Spectrum LSF — common in enterprise HPC environments.
- **Severity:** Minor
- **openstudio-server Approach:** Not explicitly documented; Slurm/Grid Engine/PBS mentioned.
- **Evidence:** No `LSFExecutor` in `osimflow/executors/`.

#### Gap: HTCondor Support
- **Gap Name:** No HTCondor Scheduler Executor
- **Description:** HTCondor is a widely-used workload management system for batch processing and HTC (High Throughput Computing). Not supported.
- **Severity:** Minor
- **openstudio-server Approach:** Not explicitly documented.
- **Evidence:** No `HTCondorExecutor` in `osimflow/executors/`.

---

### 2.4 Resource Management and Auto-Scaling

#### Gap: Dynamic Resource Provisioning Beyond Dask
- **Gap Name:** No Native Auto-Scaling for Cloud Executors
- **Description:** DaskJobQueueExecutor provides elastic HPC via dask-jobqueue, but AWS/Azure/GCP Batch executors lack native auto-scaling based on job queue depth. They rely on pre-configured compute environments.
- **Severity:** Major
- **openstudio-server Approach:** Server-based approach with inherent resource pooling.
- **Evidence:** `AWSBatchExecutor` submits to fixed queues; no dynamic queue creation or auto-scaling based on campaign size.

#### Gap: GPU Resource Management
- **Gap Name:** No Explicit GPU Resource Scheduling
- **Description:** SlurmExecutor supports `--slurm-gres` for GPUs, but the KubernetesExecutor and cloud executors lack explicit GPU resource handling (device plugins, node selectors, tolerations for GPU nodes).
- **Severity:** Minor
- **openstudio-server Approach:** Not explicitly documented.
- **Evidence:** `KubernetesExecutor` in `osimflow/executors/kubernetes_executor.py` maps `cpus` and `memory_mb` to K8s requests/limits but no GPU-specific handling.

---

### 2.5 Distributed Caching and Coordination

#### Gap: Redis-Only Cache Coordination
- **Gap Name:** No Alternative Coordination Backends
- **Description:** DistributedCache only supports Redis pub/sub. etcd, NATS, or AWS ElastiCache could serve as alternatives but are not supported.
- **Severity:** Minor
- **openstudio-server Approach:** Not explicitly documented; likely uses local file system or Rserve-based coordination.
- **Evidence:** `osimflow/distributed_cache.py` only implements Redis coordination.

#### Gap: Distributed Locking for Shared State
- **Gap Name:** No Distributed Locking Mechanism
- **Description:** OSimFlow has no distributed locking (e.g., Redis-based locks) to prevent race conditions when multiple workers access shared resources like the campaign registry.
- **Severity:** Minor
- **openstudio-server Approach:** Not explicitly documented.
- **Evidence:** `osimflow/registry.py` uses SQLite which is not designed for concurrent writes across nodes.

---

### 2.6 Job Scheduling and Queue Management

#### Gap: Priority-Based Job Scheduling
- **Gap Name:** No Priority Queue Support
- **Description:** OSimFlow executors submit jobs with fixed priority or no priority. Slurm QoS is the only priority mechanism, but it is executor-level, not per-job-level.
- **Severity:** Minor
- **openstudio-server Approach:** Not explicitly documented.
- **Evidence:** No per-submit priority parameter in `BaseExecutor.submit()` signature.

#### Gap: Job Array Support
- **Gap Name:** No Native Job Array Submission
- **Description:** Slurm job arrays allow submitting many similar jobs with a single `sbatch --array` command. OSimFlow submits each sample as a separate job, which has higher scheduler overhead.
- **Severity:** Major
- **openstudio-server Approach:** Uses OpenStudio Analysis Gem which may use job arrays internally.
- **Evidence:** `SlurmExecutor` in `osimflow/executors/__init__.py` submits individual jobs; no job array optimization.

---

### 2.7 Monitoring and Observability

#### Gap: Datadog/Commercial Backend Support
- **Gap Name:** No Datadog or Commercial APM Integration
- **Description:** OSimFlow has CloudWatch, Prometheus, and OpenTelemetry, but no Datadog, New Relic, or other commercial APM backends.
- **Severity:** Minor
- **openstudio-server Approach:** Limited to PAT logging and Rserve logs.
- **Evidence:** `osimflow/observability.py` only implements CloudWatch, Prometheus, OpenTelemetry, and NullBackend.

#### Gap: Distributed Tracing Across Executors
- **Gap Name:** No End-to-End Distributed Tracing
- **Description:** While OSimFlow generates per-sample `trace_id` values, there is no integration with distributed tracing systems (Jaeger, Tempo, Honeycomb) to correlate a single sample's journey across the orchestrator → executor → work function → container runtime.
- **Severity:** Minor
- **openstudio-server Approach:** Not explicitly documented.
- **Evidence:** `new_trace_id()` in `osimflow/observability.py` generates IDs but they are not propagated to OpenTelemetry traces.

#### Gap: Real-Time Log Streaming
- **Gap Name:** No Real-Time Log Aggregation Infrastructure
- **Description:** OSimFlow captures per-sample stdout/stderr to files but has no built-in log streaming (e.g., CloudWatch Logs streaming, ELK stack integration). The `--log-aggregation-url` flag exists but is not fully implemented.
- **Severity:** Minor
- **openstudio-server Approach:** PAT GUI provides some log visibility.
- **Evidence:** `osimflow/logging.py` has `JSONFormatter` and `LogAggregator` but no real-time streaming backends.

---

### 2.8 Storage Backends

#### Gap: WebDAV/Generic HTTP Storage
- **Gap Name:** No WebDAV or Generic HTTP-Based Storage
- **Description:** OSimFlow supports Local, S3, GCS, Azure Blob but no generic HTTP/WebDAV storage for enterprise NAS or custom storage backends.
- **Severity:** Minor
- **openstudio-server Approach:** Uses S3 and local file system.
- **Evidence:** `osimflow/storage.py` only implements S3, GCS, Azure, and Local backends.

#### Gap: Result Storage for Hybrid Cloud
- **Gap Name:** No Tiered Storage (Hot/Warm/Cold)
- **Description:** OSimFlow uploads results to a single storage backend. No tiering strategy (e.g., immediate S3, archival to Glacier after N days).
- **Severity:** Minor
- **openstudio-server Approach:** Not explicitly documented.
- **Evidence:** `ResultStorage` ABC and implementations have no lifecycle management.

---

## 3. Recommendations

### High Priority

1. **Google Cloud Batch Executor Completion**
   - Implement spot/preemptible instance handling analogous to AWSBatchExecutor
   - Add cost estimation and fallback-to-on-demand logic
   - Add regional flexibility similar to AWS implementation

2. **Helm Chart for Kubernetes Deployment**
   - Create `infra/kubernetes/osimflow/` Helm chart
   - Include ConfigMap for campaign configuration, StatefulSet for API server, Deployment for worker pods
   - Add Ingress for API access

3. **Job Array Optimization for Slurm**
   - Implement `SlurmArrayExecutor` or extend `SlurmExecutor` with array submission mode
   - Use `--array` for large campaigns to reduce scheduler overhead

### Medium Priority

4. **GPU Resource Scheduling**
   - Extend `KubernetesExecutor` with GPU node selection and resource limits
   - Add GPU-specific parameters to `BaseExecutor.submit()` (e.g., `gpu_count`, `gpu_type`)
   - Document GPU workflow in HPC deployment guides

5. **Distributed Locking for Multi-Node Campaigns**
   - Implement Redis-based distributed locking in `DistributedCache` or separate module
   - Protect campaign registry writes in multi-node scenarios
   - Use Redis SETNX or Redlock algorithm

6. **OpenTelemetry Trace Integration**
   - Propagate `trace_id` from `observability.py` into OpenTelemetry spans
   - Add Jaeger/Tempo exporter option for distributed trace visualization
   - Correlate per-sample trace IDs across orchestrator → executor → work function

### Lower Priority

7. **LSF and HTCondor Executors**
   - Add `LSFExecutor` for enterprise HPC environments
   - Add `HTCondorExecutor` for academic/throughput-computing contexts
   - Follow existing `SlurmExecutor`/`PBSExecutor` pattern

8. **Datadog Observability Backend**
   - Add `DatadogBackend` to `observability.py`
   - Support Datadog APM and log management
   - Add DatadogMetrics exporter for custom metrics

9. **WebDAV Storage Backend**
   - Implement `WebDAVStorage` in `osimflow/storage.py`
   - Support enterprise NAS and generic HTTP-based storage
   - Use `webdavclient3` library

10. **Tiered Storage Lifecycle**
    - Add storage lifecycle policies to S3/GCS/Azure implementations
    - Support automatic archival to cold storage after campaign completion
    - Add lifecycle configuration to Terraform examples

---

## 4. Summary

| Gap Category | Critical | Major | Minor |
|--------------|----------|-------|-------|
| Container Orchestration | 0 | 1 | 1 |
| Cloud Provider Support | 0 | 2 | 1 |
| HPC Scheduler Support | 0 | 0 | 2 |
| Resource Management | 0 | 2 | 1 |
| Distributed Cache/Coordination | 0 | 0 | 2 |
| Job Scheduling | 0 | 1 | 1 |
| Monitoring/Observability | 0 | 0 | 3 |
| Storage Backends | 0 | 0 | 2 |
| **Total** | **0** | **6** | **13** |

**Key Takeaway:** OSimFlow's infrastructure is more modern and flexible than openstudio-server in several dimensions (observability pluggability, distributed cache coordination, multi-cloud executor abstraction). The primary gaps are in **cloud executor maturity** (Google Cloud Batch), **Kubernetes-native deployment** (no Helm chart), and **HPC optimization** (job arrays). The distributed tracing gap is an emerging concern for large-scale multi-node campaigns where debugging sample-level failures requires correlating logs across orchestrator, executor, and container runtime layers.
