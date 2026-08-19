# Distributed Cache

OSimFlow's campaign cache accelerates re-runs by storing the output of every step/sample combination keyed to its inputs. On a single machine this works out of the box with a plain `SQLiteCache`. When a campaign spans multiple Slurm nodes or AWS Batch jobs — or when two campaign processes coordinate on the same `outdir` — configure a Redis URL (`--redis-url` or `OSIMFLOW_REDIS_URL`, issue #993): shared cache entries then live in a Redis shared store, invalidations are broadcast via pub/sub, and each process keeps a pid-private local SQLite file so there is never SQLite lock contention.

## 1. Overview

### The gap (before the shared entry store)

`SQLiteCache` (`osimflow/cache.py`) is a local-only, content-addressable cache. The primary key is:

```
(step, sample_id, openstudio_version, inputs_sha256, code_sha256, container_digest, generation)
```

Without a distributed backend, two Slurm nodes running different samples with the same cache key cannot share a hit — the second node recomputes what the first node already computed. Similarly, two campaign processes sharing one `outdir` contend on the same `cache.sqlite` file (`SQLITE_BUSY` under load — the T8.1 reproducer, fluxion#1790). Neither is a bug in `SQLiteCache`; it is the design for single-node operation. The gap is cross-node coherence without shared-file contention — closed since issue #993 (T8.2) by the Redis-backed shared entry store.

### When it matters

- Multi-node Slurm campaigns (`--executor slurm --slurm-real`) where different nodes run different samples.
- AWS Batch multi-node jobs where different tasks compute different steps.
- Any campaign where two or more workers might evaluate the same inputs independently.
- Concurrent campaign processes against the same `outdir` (resume, re-analysis, cache warming) that must not lock each other out of one SQLite file.

## 2. Distributed campaign state

`osimflow/distributed_cache.py` provides `DistributedCache`, a drop-in replacement for `SQLiteCache` that coordinates campaign state through Redis (issue #993, T8.2):

1. **Shared entry store** — cache entries that must be shared across nodes/processes are written to a Redis *hash* under a stable, outdir-derived namespace. A `lookup` that misses the local SQLite file falls back to the shared store and backfills the local file, so every worker converges on the same view of completed work.
2. **Invalidation broadcast** — every `invalidate_*` call deletes the affected fields from the shared hash *and* publishes a pub/sub message so peers drop their local copies.

Each process keeps a **pid-private local SQLite file** (`cache.p<pid>.sqlite`), so two concurrent campaign processes coordinating on the same state never open — and never lock — the same SQLite database (the fix for the T8.1 lock reproducer, fluxion#1790).

### Architecture

```
┌───────────────────────────────────────────────────────────────────┐
│                        One campaign (outdir)                      │
│                                                                   │
│  ┌─────────────┐    ┌─────────────┐    ┌─────────────┐            │
│  │  Worker A   │    │  Worker B   │    │  Worker C   │            │
│  │             │    │             │    │             │            │
│  │ cache.p1.sqlite │ │ cache.p2.sqlite │ │ cache.p3.sqlite │      │
│  │  (pid-private, │  │  (pid-private, │  │  (pid-private, │      │
│  │   write-through)│ │   write-through)│ │   write-through)│     │
│  └──────┬──────┘    └──────┬──────┘    └──────┬──────┘            │
│         │  HSET/HGET/HDEL  │                │   (sync client)     │
│         └──────────────────┼────────────────┘                     │
│                            │                                       │
│              Redis hash: osimflow:cache:entries:<namespace>        │
│              Redis pub/sub: osimflow:cache:invalidate:<namespace>  │
└───────────────────────────────────────────────────────────────────┘
```

`build_cache()` is the factory:

```python
from osimflow.distributed_cache import build_cache, campaign_state_namespace

cache = build_cache(
    db_path=Path("outdir/work/cache.sqlite"),
    redis_url="redis://localhost:6379/0",   # None → plain SQLiteCache
    campaign_id=campaign_state_namespace(Path("outdir")),
)
```

When `redis_url` is `None`, `build_cache` returns a plain `SQLiteCache` at `db_path` — single-node behaviour is unchanged. When a Redis URL is provided, it returns a `DistributedCache` whose shared state lives in Redis.

If Redis is unreachable, every shared-store operation logs a warning and degrades to local-only behaviour — a Redis outage never fails the campaign.

### Redis key naming

Shared entry store (hash; field = pipe-encoded cache key, value = JSON entry)::

    osimflow:cache:entries:<namespace>

Invalidation channel (pub/sub)::

    osimflow:cache:invalidate:<namespace>

The namespace is derived from the resolved `outdir` (`campaign_state_namespace`, a SHA-256 prefix), so all processes/nodes targeting the same `outdir` share one namespace while concurrent campaigns on different outdirs stay isolated. A per-run timestamp id would give each process a *different* namespace and defeat sharing.

### Message format

**Invalidate step** (all samples for a step):

```json
{"action": "invalidate_step", "step": "RUN_OPENSTUDIO_SIM"}
```

**Invalidate sample** (single sample on a step):

```json
{"action": "invalidate_sample", "step": "APPLY_PARAMETERS", "sample_id": "s0001"}
```

### Security

Redis credentials are carried in the URL (`user:pass@host:port/db`). TLS is supported via the `rediss://` scheme. No credentials are hardcoded anywhere in the config.

Example URLs:

```text
redis://localhost:6379/0                        # local, no auth
redis://user:pass@redis.example.com:6379/0      # AUTH
rediss://user:pass@redis.example.com:6379/0     # AUTH + TLS
```

## 3. Redis Backend Setup

### Docker Redis for local development

```bash
set -euo pipefail

# Start a Redis container
docker run -d \
  --name osimflow-redis \
  -p 6379:6379 \
  redis:7-alpine

# Verify
docker exec osimflow-redis redis-cli ping
# PONG
```

OSimFlow connects to `redis://localhost:6379/0` by default when `--redis-url` is set or `OSIMFLOW_REDIS_URL` is exported.

### AWS ElastiCache / MemoryDB for production

Use **ElastiCache Redis** (cluster mode disabled) or **MemoryDB for Redis** for a managed, multi-AZ deployment. The cluster must have **cluster mode disabled** because `DistributedCache` uses pub/sub (Redis pub/sub requires a single shard; cluster mode routes channels differently).

#### Terraform snippet

```hcl
resource "aws_elasticache_replication_group" "osimflow" {
  replication_group_id       = "osimflow-redis"
  engine                     = "redis"
  engine_version             = "7.0"
  node_type                  = "cache.t4g.micro"
  number_cache_clusters      = 2
  multi_az_enabled           = true
  automatic_failover_enabled = true
  security_group_ids         = [aws_security_group.osimflow.id]
  port                       = 6379
}
```

Retrieve the endpoint from `aws_elasticache_replication_group.osimflow.primary_endpoint_address`.

#### IAM policy for ElastiCache (AUTH)

If your ElastiCache cluster has AUTH enabled, pass credentials in the URL:

```bash
export OSIMFLOW_REDIS_URL="redis://user:password@my-cluster.xxxxx.use1.cache.amazonaws.com:6379/0"
```

For TLS connections use `rediss://`.

### Configuration

| Method | Example | Notes |
|--------|---------|-------|
| CLI flag | `--redis-url redis://localhost:6379/0` | Overrides env var |
| Environment variable | `export OSIMFLOW_REDIS_URL=redis://localhost:6379/0` | Used when `--redis-url` is not set |

The `CampaignConfig.redis_url` field is wired from both sources and passed to `build_cache()`.

## 4. Using Distributed Cache

`DistributedCache` is instantiated automatically by the `Campaign` orchestrator when `redis_url` is configured. No per-step changes are required — the `Campaign` calls `build_cache()` internally.

### Campaign integration

The `Campaign` class calls `build_cache()` during initialization (issue #993):

```python
from osimflow.campaign import Campaign
from osimflow.config import load_config
from osimflow.distributed_cache import campaign_state_namespace

config = load_config(vars(args))   # redis_url from --redis-url / env
campaign = Campaign(cfg=config, executor=executor)

# Internally:
# cache = build_cache(
#     db_path=config.cache_db,
#     redis_url=config.redis_url,
#     campaign_id=campaign_state_namespace(config.outdir),
# )
```

When `redis_url` is `None` (the default), `Campaign` uses a plain `SQLiteCache` at `outdir/work/cache.sqlite` — single-node behaviour is unchanged. When `redis_url` is set, the shared state lives in Redis and each process uses a pid-private local SQLite file (`outdir/work/cache.p<pid>.sqlite`).

### Distributed job queue coordination

`osimflow/distributed_jobqueue.py` provides `DistributedJobQueue` with the same Redis pub/sub pattern, keeping the filesystem-based job queue (`JobQueue`) coherent across nodes. The job queue handles the fan-out/fan-in lifecycle (pending → in_progress → completed/failed).

Redis channel:

```
osimflow:jobqueue:<campaign_id>
```

Actions broadcast: `enqueue`, `mark_completed`, `mark_failed`, `recover`.

The job queue factory is `build_job_queue()`:

```python
from osimflow.distributed_jobqueue import build_job_queue

queue = build_job_queue(
    queue_dir=Path("outdir/work/queue"),
    redis_url="redis://localhost:6379/0",
    campaign_id="2025-01-01T12-00-00",
)
```

### Full example: Slurm multi-node campaign

```bash
set -euo pipefail

export OSIMFLOW_REDIS_URL="redis://user:pass@redis.example.com:6379/0"

osimflow run \
  --executor slurm \
  --slurm-real \
  --slurm-partition short \
  --openstudio_version 3.11.0 \
  --input_variables variables.yml \
  --template_sim_package ./example_package \
  --n_samples 500 \
  --outdir ./results
```

Each Slurm job (one step of one sample) connects to Redis, subscribes to the campaign's invalidation channel, and invalidates its local cache when any other worker invalidates.

## 5. Cache Key Strategy

The `CacheKey` class (`osimflow/cache.py`) defines the content-addressable cache key:

```python
@dataclasses.dataclass(frozen=True)
class CacheKey:
    step: str
    sample_id: str
    openstudio_version: str
    inputs_sha256: str
    code_sha256: str
    container_digest: str
    generation: int = 0
```

### How to ensure cache hits across nodes

For a cache hit to occur on any node, **all seven fields must match exactly**:

| Field | What changes it |
|-------|----------------|
| `step` | DAG step name (e.g. `RUN_OPENSTUDIO_SIM`) |
| `sample_id` | Sample identifier (e.g. `s0001`) |
| `openstudio_version` | `--openstudio_version` CLI flag |
| `inputs_sha256` | Hash of `variables.yml` + seed model + measures |
| `code_sha256` | SHA-256 of all `bin/*.py` files |
| `container_digest` | OpenStudio container image tag |
| `generation` | DAG generation number (for iterative algorithms) |

**Practical rules for cache hits across nodes:**

With the shared entry store (issue #993), a matching key found by *any* worker is visible to every other worker: `lookup` falls back to the Redis hash when the local file misses, and backfills the local file on a shared hit. The rules below are therefore about keeping the *keys* identical across workers:

1. **Same input variables** — the same `variables.yml` produces the same `inputs_sha256`.
2. **Same seed model** — the same `template_sim_package` directory tree produces the same `inputs_sha256`.
3. **Same `bin/*.py` scripts** — no edits to `bin/apply_params_to_model.py`, `bin/extract_kpis.py`, etc. between runs. Any edit invalidates `code_sha256` for all samples.
4. **Same OpenStudio version** — the same `--openstudio_version` produces the same `container_digest`.
5. **Same generation** — iterative algorithms (NSGA-II, PSO) increment `generation` each loop; cache is per-generation by default.

### Computing the hashes

`SQLiteCache` computes the hashes internally via `sha256_of_files()` and `sha256_of_dict()`. You can inspect them (single-node mode uses `cache.sqlite`; distributed mode uses per-process `cache.p<pid>.sqlite` files):

```bash
set -euo pipefail

# Inspect the cache schema
sqlite3 outdir/work/cache.sqlite ".schema"

# Count entries by step
sqlite3 outdir/work/cache.sqlite "SELECT step, COUNT(*) FROM cache_entries GROUP BY step"

# View a specific entry
sqlite3 outdir/work/cache.sqlite "SELECT * FROM cache_entries WHERE step='RUN_OPENSTUDIO_SIM' LIMIT 1"

# Distributed mode: inspect the Redis shared store instead
redis-cli HLEN osimflow:cache:entries:<namespace>
redis-cli HSCAN osimflow:cache:entries:<namespace> 0 MATCH 'RUN_OPENSTUDIO_SIM|*' COUNT 20
```

## 6. Cache Invalidation

### When cache is invalidated

| Trigger | What is invalidated | How |
|--------|-------------------|-----|
| Edit a `bin/*.py` file | All steps using that script | `code_sha256` changes |
| Change `--openstudio_version` | `RUN_OPENSTUDIO_SIM` entries only | `container_digest` changes |
| Change `template_sim_package` content | `APPLY_PARAMETERS` + `RUN_OPENSTUDIO_SIM` | `inputs_sha256` changes |
| Change `variables.yml` | `GENERATE_LHS_SAMPLES` + all downstream steps | `inputs_sha256` changes |
| Campaign code upgrade | All entries | New `code_sha256` for all steps |
| Explicit `--skip-cache` (future) | All entries | Full cache purge |

### How distributed invalidation works

`DistributedCache.invalidate_step()` and `invalidate_sample()` delete from the local SQLite file, delete the matching fields from the Redis shared store, and publish a JSON message to the invalidation channel:

```python
def invalidate_step(self, step: str) -> int:
    n = self._local.invalidate_step(step)
    self._shared_invalidate(f"{step}|*")          # Redis HSCAN + HDEL
    self._publish({"action": "invalidate_step", "step": step})
    return n
```

Every other worker subscribed to `osimflow:cache:invalidate:<namespace>` receives the message and calls its local `SQLiteCache.invalidate_*`, keeping all local caches coherent. A worker that joins late (or missed the broadcast) still sees the invalidation because the shared hash fields are gone.

### Subscriber management

The Redis subscriber runs in a background daemon thread, started lazily on the first `invalidate_*` call. It stops when `close()` is called. The subscriber handles its own reconnection on error.

### Manual invalidation

To force a cache clear across all workers without restarting the campaign:

```python
from osimflow.distributed_cache import DistributedCache

cache = DistributedCache(
    db_path=Path("outdir/work/cache.sqlite"),
    redis_url="redis://localhost:6379/0",
    campaign_id="2025-01-01T12-00-00",
)
cache.invalidate_step("RUN_OPENSTUDIO_SIM")
cache.close()
```

This broadcasts the invalidation to all other workers.

## 7. Performance Considerations

### Latency tradeoffs

| Operation | Local `SQLiteCache` | `DistributedCache` |
|-----------|--------------------|--------------------|
| `lookup` (local hit) | ~0.1–1 ms (local disk) | Same (local SQLite fast path) |
| `lookup` (local miss) | miss | +1 Redis round-trip if the shared store holds the key |
| `store` | ~1–5 ms (local disk) | +1 Redis round-trip (HSET) |
| `invalidate_*` | ~1–5 ms | ~5–20 ms (Redis HDEL + pub/sub round-trip) |
| Subscriber receive | N/A | ~1–10 ms (depends on network) |

The distributed path adds a bounded network round-trip (5s socket timeout, then graceful degradation to local-only) to `store` and to *missed* lookups. The invalidation calls are infrequent (triggered by config changes, not per-sample).

### Redis connection pooling

`DistributedCache` uses a single async Redis connection per worker. The connection is created lazily and reused across all publish operations. No explicit pooling is needed at the application level.

For high-frequency publish scenarios (e.g., many workers invalidating simultaneously), consider:

- Using a **Redis Cluster** with more shards for pub/sub fan-out.
- Setting `REDIS_MAX_CONNECTIONS` in the environment (handled by `redis.asyncio`).

### Cache TTL settings

`SQLiteCache` does not currently enforce a TTL. Stale entries are removed only by explicit invalidation or by reaching the storage limit. For long-running campaigns:

- Monitor the local cache files: `ls -lh outdir/work/cache.sqlite` (single-node) or `ls -lh outdir/work/cache.p*.sqlite` (distributed mode — one per process; they are scratch files and safe to delete between runs).
- For the Redis shared store, set a TTL policy on the `osimflow:cache:entries:*` keys (e.g. via `redis-cli --bigkeys` review and an `EXPIRE` sweep) if campaigns are long-lived.
- Set a cron job to purge entries older than N days if needed:

```bash
set -euo pipefail

# Purge cache entries older than 30 days (by modification time)
find outdir/work/cache.sqlite -mtime +30 -delete
```

### Network partition handling

If a worker loses Redis connectivity:

1. The subscriber thread logs a warning and attempts to reconnect.
2. Shared-store operations (`store` / shared `lookup` / shared invalidation) log a warning and degrade to local-only — they never raise, so a Redis outage cannot fail the campaign.
3. Local cache operations continue normally (the pid-private `SQLiteCache` is always available).
4. Invalidation broadcasts from other workers are missed until reconnection.
5. On reconnection, the subscriber re-subscribes to the channel and resumes receiving broadcasts.

Workers that miss invalidation events may produce stale cache entries for that campaign. The next explicit invalidation (e.g., changing `variables.yml`) will correct the state.

## 8. Alternatives

### S3-backed read-only cache

For read-heavy workloads where workers can share a common object store, you can configure a **result storage backend** (`--result-storage-backend s3`) and point it to a shared S3 bucket. This is a **read-only share** — workers write to their local `SQLiteCache` and upload outputs to S3; other workers cannot use S3 as a cache source directly.

See [`docs/aws-batch-terraform.md`](aws-batch-terraform.md) for S3 result storage configuration.

### NFS / shared filesystem for cache directory

If your HPC cluster has a shared NFS mount (e.g., `/shared/osimflow`), you can point all workers to the same `outdir`:

```bash
set -euo pipefail

osimflow run \
  --executor slurm \
  --slurm-real \
  --outdir /shared/osimflow/campaigns/run-001 \
  ...
```

With a shared `outdir`, all workers can point at the same directory. Since issue #993 the recommended combination on shared filesystems is `--redis-url` + shared `outdir`: the shared *state* (cache entries) is coordinated through Redis while each process keeps a pid-private SQLite file, so the classic `SQLITE_BUSY` contention of many writers on one NFS-backed `cache.sqlite` is gone. Running purely on NFS without Redis remains possible but:

- **Performance**: NFS latency (~1–5 ms per I/O) may be higher than local SSD.
- **Lock contention**: without Redis, all workers share the same `cache.sqlite` file; SQLite WAL mode helps, but concurrent writers on NFS can cause `SQLITE_BUSY` errors under heavy load (the T8.1 reproducer).
- **No broadcast invalidation**: all workers see the same SQLite file directly.

Redis is preferred for multi-node campaigns on shared-nothing HPC infrastructure.

### Redis vs. memcached

Redis provides both the **shared entry store** (the campaign's cross-node cache index) and the **pub/sub broadcast** for invalidations. The actual simulation outputs are stored on local disk (or S3). Memcached cannot serve this role because it does not support pub/sub or hash-field deletion — invalidation would require polling or a custom solution.

### Summary comparison

| Approach | Cache sharing | Broadcast invalidation | SQLite lock contention | Setup complexity | Performance |
|----------|--------------|------------------------|------------------------|-----------------|-------------|
| Local `SQLiteCache` (default) | None | None | N/A (single file) | None | Best (local SSD) |
| `DistributedCache` + Redis | Per-campaign (shared hash) | Yes (pub/sub) | None (pid-private files) | Redis required | Near-local (local fast path) |
| Shared NFS `outdir` (no Redis) | Full (same file) | None | Possible (`SQLITE_BUSY`) | None (if NFS exists) | Poor (NFS latency) |
| S3 result storage | Read-only share | None | N/A | AWS S3 | N/A (not a cache) |
