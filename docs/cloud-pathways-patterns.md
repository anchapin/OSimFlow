# Cloud Pathways Patterns: Mapping and Gaps for OSimFlow

**Status:** Draft design note — for review, no implementation yet
**Date:** 9 Nov 2026
**Companion documents:** The same patterns are proposed for OpenStudio
Server in NatLabRockies/openstudio-server-helm#163
(`docs/cloud-pathways-patterns-design.md`) and
NatLabRockies/OpenStudio-server#896
(`doc/cloud_pathways_patterns_design.md`). OSimFlow is the inverse case:
most of the patterns already exist here, so this note is a mapping and a
gap list, not a proposal to build them from scratch.
**Source:** Google Cloud Blog, "Cloud Pathways, a distributed ML runtime,
is GA" (9 Nov 2026)

## Recommendation

Do **not** adopt Cloud Pathways. It is a JAX runtime for Google Cloud TPUs
(v5e / v5p / Trillium v6e / Ironwood 7x) on GKE, requiring JAX 0.10.1+.
OSimFlow campaigns are CPU-based OpenStudio simulations — no accelerator,
no JAX, no inter-simulation communication.

Unlike OpenStudio Server, however, OSimFlow already *is* a
single-controller distributed runtime for parametric simulation campaigns,
across ten executor backends (Local, Slurm, AWS Batch, Azure Batch, Google
Batch, Kubernetes, Nomad, PBS, Dask-JobQueue, Docker Swarm). The useful
exercise is checking its design against Pathways' production lessons.

## Mapping: patterns OSimFlow already has

### 1. Single-controller orchestration — already the architecture

Pathways replaces per-host multi-controller execution with one client
orchestrating the whole run. OSimFlow's driver does the same for
campaigns: one Python process owns sampling, the campaign DAG, and result
collection, and every executor conforms to a single
`submit()` → `Handle` contract. This note proposes no change here — it
records that Pathways' central bet validates the existing contract, and
that new executors should preserve it rather than grow per-backend
orchestration logic.

### 2. Completion-based fault tolerance — already implemented, differently

Pathways checkpoints so a job survives losing a slice. OSimFlow's
equivalent is stronger for this workload: the content-addressable campaign
cache (local SQLite, or the Redis-backed distributed cache for multi-node
runs) keys completed work by step, sample, and input/code hashes, the
chaos engine exercises failure handling, and retry re-queues failed
samples. A lost worker costs its in-flight samples; completed samples are
not recomputed on resume.

### 3. Colocated work — partially

On Kubernetes, each sample already runs as its own Job with an ephemeral
runner and object-storage transport, so staging and execution happen on
the worker, not through the driver.

## Gaps Pathways suggests

1. **Elastic capacity change as a first-class event.** Pathways resizes a
   running job when a slice is lost (N-1) instead of treating loss purely
   as failure. OSimFlow handles worker *failure* (retry/resume) but not
   graceful campaign-level shrink/grow: e.g. a Kubernetes campaign
   continuing at reduced parallelism during node drain or spot
   reclamation, with a capacity-change event in `run.json`, rather than a
   burst of sample failures. Candidate scope: Kubernetes and cloud Batch
   executors first.
2. **Watch-based status instead of polling at scale.** Pathways dispatches
   asynchronously from one controller. The Kubernetes executor polls Job
   status with exponential backoff, which is fine at tens of samples and
   wasteful at thousands. A watch/streaming status path for the Kubernetes
   executor is the scalable equivalent.
3. **A true staging sidecar on Kubernetes.** Today the ephemeral runner
   stages inputs in the simulation container, so a container crash can
   lose staging/upload with it. A sidecar that owns fetch, hash
   verification, result upload, and the completion record — surviving the
   simulation container — mirrors the OpenStudio Server proposal and
   Pathways' colocated Python sidecars.
4. **Shared, interactive campaign service.** Pathways' interactive mode
   and shared service let multiple users share a long-running accelerator
   pool. OSimFlow's analogue — a shared campaign service multiple users
   submit to interactively (e.g. from notebooks) instead of one driver
   process per campaign — is worth a spike only after gaps 1–3; it
   multiplies scheduling, quota, and fairness questions.

## Proposed test

Extend the existing chaos-engine scenarios with a capacity-loss scenario
on the Kubernetes executor: drain a node mid-campaign and report
(1) campaign completes with only in-flight samples re-run, (2) the event
is recorded as capacity loss, not sample failures, and (3) wall-clock vs.
an undisturbed run at the same sample count.

---

*Prepared from the GA announcement and this repo's current docs
(`docs/distributed-cache.md`, `docs/chaos-engine.md`,
`docs/kubernetes-deployment.md`, `docs/runjson-guide.md`). No Pathways
evaluation, GCP account, or TPU access is proposed.*
