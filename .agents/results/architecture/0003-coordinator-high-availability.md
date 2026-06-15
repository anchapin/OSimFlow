# ADR 0003 — Coordinator High Availability

**Status:** accepted
**Date:** 2026-06-14
**Deciders:** OSimFlow maintainers
**Supersedes:** none
**Superseded by:** none
**Related issue:** #392

> **Implementation status (2026-06-14):** Documented. This ADR records the
> known single-instance limitation of the Campaign coordinator and
> documents the supported HA patterns for production deployments.

## Context

Issue #392 ([ARCH-006] No Built-in High Availability for Coordinator)
identified that OSimFlow's Campaign orchestrator runs as a single-instance
process with no built-in coordinator failover mechanism. This contrasts
with NREL's OpenStudio-server, which supports a multi-instance coordinator
topology.

### What "coordinator" means in OSimFlow

The **coordinator** is the `Campaign` class (`osimflow/campaign.py`).
It owns the campaign DAG and is responsible for:

1. Generating LHS samples (single-shot)
2. Running pre-flight model validation (single-shot)
3. Fan-out to per-sample parameter application
4. Fan-out to per-sample OpenStudio simulation execution
5. Fan-out to per-sample KPI extraction
6. Aggregating results and generating plots (single-shot)

The coordinator submits work to an executor (Local, Slurm, AWS Batch,
Kubernetes, etc.) but the executor work is stateless and
idempotent — the coordinator is the stateful entity.

### Current single-instance constraints

The Campaign class has these single-instance characteristics:

1. **Signal handling**: `_CancelRegistry` is a process-global singleton
   that holds the currently-running Campaign for SIGINT/SIGTERM handling.
   Only one Campaign can be registered at a time per process.

2. **Job queue**: `JobQueue` (`osimflow/jobqueue.py`) is a
   filesystem-based persistence layer with no distributed locking.
   Concurrent writers to the same `outdir` are not supported.

3. **Campaign registry**: `CampaignRegistry` uses a SQLite file as its
   backing store. Multiple processes writing to the same registry file
   would cause lock contention or corruption.

4. **Cache**: `SQLiteCache` uses WAL mode but is not designed for
   concurrent writer access from multiple processes.

5. **run.json**: The per-campaign monitoring trace is written by a
   single process. Concurrent coordinators writing to the same `outdir`
   would produce interleaved JSON.

## Decision

Document the supported HA patterns rather than implementing a full
coordinator failover mechanism. The effort to implement safe
coordinator failover (distributed locking, leader election, shared
state) is XL and is not justified given the current priority (Low).

### Supported HA patterns

OSimFlow supports two high-availability deployment patterns:

#### Pattern 1: Campaign-level redundancy (shared filesystem)

```
                    ┌─────────────────────────────────────┐
                    │         Shared network storage        │
                    │  (NFS, EFS, Azure Files, GCS FUSE)   │
                    │                                      │
                    │  outdir/  ← all coordinators write   │
                    │  cache.db                           │
                    │  run.json                           │
                    │  work/queue/                        │
                    └─────────────────────────────────────┘
                                ▲           ▲
                                │           │
                    ┌───────────┴──┐   ┌──┴───────────┐
                    │ Coordinator A │   │ Coordinator B │
                    │ (Campaign.run)│   │ (Campaign.run)│
                    └───────────────┘   └───────────────┘
```

**How it works:**
- Multiple coordinator processes mount the same `outdir` via a shared
  filesystem (NFS, EFS, Azure Files, GCS FUSE, etc.)
- Each coordinator writes to the same `run.json`, `cache.db`, and
  `work/queue/` directories
- The `JobQueue.recover()` mechanism resets in-flight jobs from a
  crashed coordinator so a surviving coordinator can pick them up
- Campaign-level cache provides resume: completed steps are not
  re-executed even if a different coordinator handles the retry

**Requirements:**
- Shared filesystem with atomic rename support (required for
  `JobQueue._write_job` atomic write-then-rename pattern)
- All coordinators must run the same OSimFlow version
- All coordinators must use the same `outdir` (via `--outdir`)
- At most one coordinator actively processing a given sample at any
  time (enforced by the job queue's `in_progress/` state)

**What happens when a coordinator crashes:**
1. The crashed coordinator's in-flight jobs remain in
   `work/queue/in_progress/`
2. On restart (any coordinator), `JobQueue.recover()` moves those jobs
   back to `pending/`
3. The cache ensures already-completed steps are not re-run
4. `run.json` is updated incrementally via atomic rename, so partial
   writes from the crashed coordinator are discarded and the last
   successful checkpoint is used

**What does NOT work in this pattern:**
- Concurrent coordinators processing the same sample simultaneously
  (the cache prevents re-execution of individual steps, but the job
  queue does not have distributed locking for the submit phase)
- True leader election (any coordinator can pick up any pending job)

#### Pattern 2: Campaign-per-worker (horizontal scaling)

```
┌────────────────┐     ┌────────────────┐     ┌────────────────┐
│  Coordinator A │     │  Coordinator B │     │  Coordinator C │
│  (1 of N slots)│     │  (1 of N slots)│     │  (1 of N slots)│
│  --max-workers│     │  --max-workers│     │  --max-workers│
│    50         │     │    50          │     │    50          │
└───────┬────────┘     └───────┬────────┘     └───────┬────────┘
        │                     │                     │
        ▼                     ▼                     ▼
   ┌─────────────────────────────────────────────────────────┐
   │              Executor (Slurm / AWS Batch / K8s)          │
   │                                                         │
   │   [job1] [job2] [job3] ... [job149] [job150] ... [job500]│
   └─────────────────────────────────────────────────────────┘
```

**How it works:**
- Each coordinator handles a disjoint subset of samples
- Coordinator A processes samples 0–49, Coordinator B processes 50–99,
  etc. (partitioned by external scheduler or script)
- Each coordinator writes to its own `outdir` (no shared filesystem
  required)
- The executors (Slurm, AWS Batch, Kubernetes) provide the distributed
  compute layer — they are already HA and handle job scheduling,
  retries, and fault tolerance
- A lightweight external orchestrator (cron, Airflow DAG, etc.) launches
  each coordinator as a separate job

**Requirements:**
- External job scheduler to partition samples and launch coordinators
- Each coordinator has its own `--outdir`
- Sample partitioning must be deterministic (same sample always goes
  to same coordinator) to avoid duplicate work

**Advantages:**
- No shared filesystem required
- True horizontal scaling: add more coordinators to process more
  samples in parallel
- Fault isolation: a crashed coordinator only affects its subset of
  samples
- No changes to OSimFlow code required

**Disadvantages:**
- Requires external orchestration to manage the coordinator pool
- The sample partitioning logic must be implemented outside OSimFlow

## Alternatives Considered

### Implement distributed coordinator with leader election

**Rejected — Effort XL.**

A proper distributed coordinator implementation would require:

1. A distributed lock manager (Redis, etcd, Zookeeper) to ensure only
   one coordinator processes a given sample at a time
2. Leader election so only one coordinator picks up recovered jobs
3. A shared state store (Redis, PostgreSQL) for the campaign registry
   and run.json
4. Integration tests for the distributed path
5. Operational complexity for the Redis/etcd cluster

This is a significant engineering investment that is not justified
given the current priority (Low) and effort estimate (XL).

### Use a message queue (Celery, RQ, Dramatiq)

**Rejected — Architectural mismatch.**

OSimFlow's computational shape is an embarrassingly-parallel DAG with
fan-out over samples. A message queue is a good fit for the executor
layer (Slurm, AWS Batch, Kubernetes already provide this), but adding a
queue for the coordinator-to-executor communication would require
re-architecting the executor interface. The existing executor abstraction
(`BaseExecutor.submit()` → `Handle`) is already the right interface and
does not need a queue layer.

## Consequences

### Positive

- Users understand the HA options available today without code changes
- Pattern 1 (shared filesystem) is zero-configuration for environments
  that already have shared storage
- Pattern 2 (campaign-per-worker) is truly horizontal and fault-isolated

### Negative

- Neither pattern provides true automatic failover with leader election
- Pattern 1 requires shared filesystem infrastructure
- Pattern 2 requires external orchestration that OSimFlow does not own

### Neutral

- The `JobQueue.recover()` mechanism already handles the crash-recovery
  case for a single coordinator; the HA patterns above extend this to
  multi-coordinator scenarios
- The cache is already the mechanism that prevents duplicate work across
  coordinator restarts

## Gap Closure Criteria

This ADR can be superseded when OSimFlow implements any of:

1. A distributed lock manager (Redis/etcd) for safe concurrent coordinator
   access to the shared `outdir`
2. Leader election via a consensus protocol (Raft, etc.)
3. A shared campaign registry with concurrent write support
4. Native Kubernetes operator with coordinator failover support

## References

- Issue #392: [ARCH-006] No Built-in High Availability for Coordinator
- `osimflow/campaign.py`: Campaign orchestrator implementation
- `osimflow/jobqueue.py`: Filesystem-based job queue for crash recovery
- `osimflow/cache.py`: SQLite-backed explicit cache
- `osimflow/registry.py`: Campaign registry
- `docs/kubernetes-deployment.md`: K8s executor deployment guide
- `docs/nomad-production.md`: Nomad HA deployment guide
