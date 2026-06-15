# MongoDB and Distributed Storage

OSimFlow uses SQLite as its default storage backend for three distinct subsystems: the campaign cache (`SQLiteCache`), the document store (`SQLiteDocumentStore`), and the results database (`ResultsDatabase`). SQLite is ideal for single-node, single-writer workloads, but scales poorly beyond that. This guide covers the limitations of the SQLite-based defaults, available alternatives, the architecture for plugging in distributed backends, and a practical migration path.

## Storage Subsystems Overview

OSimFlow manages three kinds of state, each with its own storage module:

| Subsystem | Module | Default backend | Purpose |
|---|---|---|---|
| Campaign cache | `osimflow/cache.py` | `SQLiteCache` | Explicit, hash-keyed resume layer for per-step, per-sample outputs |
| Document store | `osimflow/document_store.py` | `SQLiteDocumentStore` | MongoDB-equivalent JSON document persistence for KPIs, run metadata |
| Results database | `osimflow/results_db.py` | SQLite (direct) | Per-(sample, KPI) relational table for historical analysis |
| Result artifacts | `osimflow/storage.py` | `LocalStorage` | File-level upload/download for simulation outputs |

The first three are SQLite-based; the fourth already supports S3, GCS, and Azure Blob via the `ResultStorage` abstraction.

## SQLite Limitations at Scale

SQLite is a excellent single-node database. It is not designed for the multi-node, multi-writer, or network-accessible scenarios that large OSimFlow deployments encounter.

### Single-writer lock

SQLite uses a file-level write lock. Only one writer can hold the lock at a time; all other writers return `SQLITE_BUSY`. In a multi-node Slurm or AWS Batch campaign, concurrent writers fighting for the lock cause retries, timeouts, and throughput collapse.

### Not network-accessible

SQLite reads and writes a single file on a local filesystem. It has no client/server protocol. In a distributed campaign where workers run on different nodes (Slurm compute nodes, Batch container instances), there is no shared filesystem by default. The `DistributedCache` in `osimflow/distributed_cache.py` works around this for cache invalidation by layering Redis pub/sub on top of per-node SQLite caches, but the data itself remains local to each node.

### 2 GB+ file size limits

SQLite stores the entire database in a single file. While modern SQLite handles files up to 281 TB theoretically, the practical limit in HPC environments is often the filesystem quota or the point at which `VACUUM` and backup become painfully slow. A campaign with millions of cache entries or KPI records can approach this territory.

### WAL mode does not help with concurrency

`SQLiteCache` and `SQLiteDocumentStore` both use WAL mode (`PRAGMA journal_mode=WAL`) which improves read concurrency but does not solve the write-lock problem. The cache's own docstring (see `osimflow/cache.py` lines 96-117) explicitly calls out the pytest-xdist race condition with auxiliary WAL files.

## When to Consider Alternatives

Not every deployment needs a distributed backend. The following workloads outgrow SQLite:

- **1000+ campaigns** tracked in a single registry — the registry SQLite file grows large, and concurrent reads from `osimflow list` / `osimflow show` become slow.
- **Multi-node HPC** (Slurm, PBS, Dask-JobQueue) — workers on different nodes cannot share a SQLite file without a shared network filesystem (NFS mount), which adds infrastructure complexity and is often prohibited on institutional HPC.
- **Cloud distributed deployments** — AWS Batch, Azure Batch, and Google Cloud Batch run each job in an isolated container with an ephemeral filesystem. There is no shared disk between jobs.
- **Multi-tenant or multi-campaign dashboards** — querying campaign history across thousands of runs requires a database that supports concurrent reads and writes from many clients simultaneously.
- **High-frequency result ingestion** — if KPI extraction runs faster than a single SQLite writer can keep up (unlikely for EnergyPlus, but possible with fast surrogate models), a distributed document store is needed.

A single-node local campaign with hundreds of samples and a handful of campaigns will continue to work well with SQLite for years.

## Document Store Alternatives

`osimflow/document_store.py` defines the `DocumentStore` abstract base class (ABC) with a MongoDB-equivalent interface: `insert_one`, `find_one`, `find_many`, `update_one`, `delete_one`, `create_index`, `list_collections`, `count_documents`. `SQLiteDocumentStore` implements this interface using SQLite JSON1 functions.

### MongoDB

[MongoDB](https://www.mongodb.com/) is the reference implementation for the `DocumentStore` interface. To add a `MongoDocumentStore`, implement the `DocumentStore` ABC using `pymongo`:

```python
from pymongo import MongoClient
from osimflow.document_store import DocumentStore

class MongoDocumentStore(DocumentStore):
    name = "mongodb"

    def __init__(self, connection_url: str, database: str = "osimflow"):
        self._client = MongoClient(connection_url)
        self._db = self._client[database]

    def insert_one(self, collection: str, document: dict[str, Any]) -> str:
        result = self._db[collection].insert_one(document)
        return str(result.inserted_id)

    # ... implement all abstract methods using pymongo
```

The interface is intentionally designed to make this mapping straightforward. Index creation maps directly to `create_index`. Query operators (`$gt`, `$gte`, `$lt`, `$lte`, `$in`, `$nin`, `$regex`, `$exists`) are native to MongoDB and do not require the translation layer that `SQLiteDocumentStore` carries.

#### Managed MongoDB services

| Service | Protocol | Notes |
|---|---|---|
| MongoDB Atlas | `mongodb+srv://` | Fully managed, auto-sharding, built-in Atlas Search |
| Amazon DocumentDB | `mongodb://` | MongoDB-compatible; IAM auth, VPC-only, automaticfailover |
| Azure Cosmos DB for MongoDB | `mongodb://` | MongoDB wire protocol, global distribution |

All three accept the same `pymongo` client. Connection pooling, TLS, and authentication are handled by the driver.

### PostgreSQL with JSONB

PostgreSQL with the `jsonb` column type provides document storage with full SQL join capability. The `psycopg2` or `asyncpg` drivers are widely used. Unlike MongoDB, PostgreSQL requires a fixed schema for relational columns; JSONB columns store semi-structured data alongside them.

For OSimFlow, this means the `campaigns` table could remain relational (campaign_id, created_at, algorithm) while the `results` JSONB column stores per-sample KPI objects. This hybrid approach is useful if you already run PostgreSQL.

```python
import psycopg2
from psycopg2.extras import Json

def insert_result(conn, sample_id: str, kpis: dict) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO results (sample_id, kpis) VALUES (%s, %s)",
            (sample_id, Json(kpis))
        )
```

### Tradeoffs vs SQLite

| Factor | SQLite | MongoDB / DocumentDB | PostgreSQL JSONB |
|---|---|---|---|
| Deployment complexity | Zero (single file) | Medium (requires MongoDB or cloud service) | Medium (requires PostgreSQL) |
| Network access | No | Yes (native driver protocol) | Yes (native driver protocol) |
| Concurrent writers | 1 | Many (wired for it) | Many (MVCC) |
| Query language | SQL subset | MongoDB query DSL | SQL + JSONB operators |
| Operational burden | None | Medium–High | Medium |
| Cost | Free, local-only | Atlas from ~$0/mth; DocumentDB ~$0.025/vCPU-h | Managed from ~$0.006/vCPU-h (RDS) |

## Results Database Options

`osimflow/results_db.py` uses a direct SQLite connection to store per-sample KPI results in a relational schema. The `ResultsDatabase` class is not currently abstracted behind an ABC, so swapping it requires a code change.

### PostgreSQL / TimescaleDB

Replace the `results_db.py` SQLite backend with `psycopg2` or `asyncpg` for a network-accessible, concurrent-writer results store. TimescaleDB (a PostgreSQL extension) adds time-series optimization (chunking, compression, continuous aggregates) which is useful if you run hundreds of campaigns and query across time ranges.

```python
import psycopg2
from psycopg2.extras import execute_values

def store_kpis(conn, campaign_id: str, kpis: list[dict]) -> None:
    """Bulk-insert KPI rows into PostgreSQL."""
    rows = [
        (campaign_id, r["sample_id"], r["kpi_name"], r["kpi_value"], r.get("unit"))
        for r in kpis
    ]
    execute_values(
        conn.cursor(),
        """INSERT INTO results
           (campaign_id, sample_id, kpi_name, kpi_value, unit)
           VALUES %s
           ON CONFLICT DO NOTHING""",
        rows
    )
```

### Parquet files on S3

`osimflow/storage.py` already provides the `ResultStorage` ABC with S3, GCS, and Azure Blob implementations. Rather than storing KPI rows in a database, write them as Parquet files:

```
results/
  campaign_2025-01-01_run1/
    kpis/
      part-00000.parquet
      part-00001.parquet
    aggregated_results.csv
    failed_simulations.csv
```

This approach uses the existing `osimflow/storage.py` infrastructure with no new database. Query with AWS Athena, Google BigQuery, or Spark. The tradeoff is that ad-hoc SQL queries require those services; there is no interactive query console.

## Cache Alternatives

`osimflow/distributed_cache.py` provides `DistributedCache`, a wrapper around `SQLiteCache` that broadcasts invalidation events via Redis pub/sub so all Slurm workers or Batch jobs share a coherent cache view. This is a partial solution — each node still has a local SQLite cache, and only invalidation is distributed.

### Full Redis backend

For a fully distributed cache where all cache data lives in Redis (no per-node SQLite files), implement a `RedisCache` class:

```python
import redis
from osimflow.cache import CacheKey

class RedisCache:
    """Network-accessible cache backed by Redis."""

    def __init__(self, redis_url: str, key_prefix: str = "osimflow:cache"):
        self._redis = redis.from_url(redis_url)
        self._prefix = key_prefix

    def _key_for(self, key: CacheKey) -> str:
        import json
        return f"{self._prefix}:{key.step}:{key.sample_id}"

    def lookup(self, key: CacheKey) -> Path | None:
        val = self._redis.hgetall(self._key_for(key))
        if not val or val.get("exit_code") != "0":
            return None
        return Path(val["output_path"])

    def store(self, key: CacheKey, output_path: Path, exit_code: int) -> None:
        self._redis.hset(self._key_for(key), mapping={
            "output_path": str(output_path),
            "exit_code": str(exit_code),
        })
```

This approach keeps all cache state in Redis. Workers on any node read and write the same cache entries. The `RedisCache` pairs well with the existing `DistributedCache` invalidation broadcast, or can replace it entirely if all cache entries are stored in Redis from the start.

## Adding a MongoDB Document Store

The `DocumentStore` ABC in `osimflow/document_store.py` is the extension point. The factory function `build_document_store` currently only handles `sqlite`. To add MongoDB support:

### 1. Implement the class

Create `osimflow/mongo_document_store.py` with:

```python
from pymongo import MongoClient, ASCENDING, DESCENDING
from osimflow.document_store import (
    DocumentStore,
    DocumentNotFoundError,
    DuplicateDocumentError,
    DocumentStoreError,
)

class MongoDocumentStore(DocumentStore):
    name = "mongodb"

    def __init__(self, connection_url: str, database: str = "osimflow"):
        self._client = MongoClient(connection_url)
        self._db = self._client[database]

    def insert_one(self, collection: str, document: dict[str, Any]) -> str:
        # ... pymongo insert_one, handle DuplicateKeyError
        pass

    def find_one(self, collection: str, filter_spec: dict[str, Any] | None = None) -> dict[str, Any] | None:
        # ... pymongo find_one with filter translation
        pass

    # implement all remaining abstract methods
```

### 2. Update the factory

In `osimflow/document_store.py`, add `MongoDocumentStore` to the `build_document_store` factory:

```python
def build_document_store(
    backend: str,
    db_path: Path,
    *,
    mongo_url: str | None = None,
) -> DocumentStore:
    if backend == "sqlite":
        return SQLiteDocumentStore(db_path=db_path)
    if backend == "mongodb":
        if mongo_url is None:
            raise ValueError("mongo_url required for mongodb backend")
        return MongoDocumentStore(connection_url=mongo_url)
    raise ValueError(f"unknown document_store_backend: {backend!r}")
```

### 3. Wire to CLI

Add `--document-store-backend` and `--mongo-url` arguments to `osimflow/__main__.py` and pass them through `CampaignConfig`.

## Migration Guide

Migrating from SQLite defaults to a distributed backend involves three steps: export, load, and configure.

### 1. Export existing artifacts

OSimFlow writes all persistent data to the campaign `outdir`. Identify the files to migrate:

```bash
outdir/
  cache.sqlite           # SQLiteCache — step/sample cache entries
  document_store.db      # SQLiteDocumentStore — KPI documents
  results.db             # ResultsDatabase — structured KPI table
  run.json               # RunTrace — campaign metadata
```

Export cache entries (for cache migration):

```bash
sqlite3 cache.sqlite <<'EOF'
.mode json
.output cache_entries.json
SELECT * FROM cache_entries;
EOF
```

Export document store collections:

```bash
sqlite3 document_store.db <<'EOF'
.mode json
.output kpis.json
SELECT doc FROM doc_kpis;
EOF
```

Export results database:

```bash
sqlite3 results.db <<'EOF'
.mode csv
.headers on
.output results.csv
SELECT * FROM results;
EOF
```

### 2. Load into new backend

#### MongoDB

```python
import json
from pymongo import MongoClient

client = MongoClient("mongodb://user:pass@host:27017")
db = client["osimflow"]

# Load KPI documents
with open("kpis.json") as f:
    for doc in json.load(f):
        db.kpis.insert_one(doc)

# Load cache entries (serialize CacheKey fields)
with open("cache_entries.json") as f:
    for entry in json.load(f):
        db.cache_entries.insert_one(entry)
```

#### PostgreSQL

```bash
psql -h pg.example.com -U osimflow -d osimflow <<'EOF'
CREATE TABLE results (
    sample_id TEXT NOT NULL,
    kpi_name TEXT NOT NULL,
    kpi_value REAL NOT NULL,
    unit TEXT,
    timestamp REAL NOT NULL,
    campaign_id TEXT NOT NULL,
    generation INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (sample_id, kpi_name, campaign_id, generation)
);
\copy results from 'results.csv' CSV HEADER;
EOF
```

### 3. Update configuration

Point OSimFlow at the new backend by updating the CLI flags or environment variables:

```bash
# Document store
osimflow run \
  --document-store-backend mongodb \
  --mongo-url "mongodb://user:pass@mongo.example.com:27017/osimflow" \
  ...

# Result storage (already abstracted)
osimflow run \
  --result-storage-backend s3 \
  --result-storage-bucket my-campaign-results \
  ...
```

For Kubernetes or AWS Batch, pass the connection URL via a Kubernetes Secret or AWS Secrets Manager:

```yaml
# Kubernetes Secret
apiVersion: v1
kind: Secret
metadata:
  name: osimflow-mongo
type: Opaque
stringData:
  MONGO_URL: "mongodb://user:pass@mongo.example.com:27017/osimflow"
```

Reference it in your job template:

```yaml
env:
  - name: MONGO_URL
    valueFrom:
      secretKeyRef:
        name: osimflow-mongo
        key: MONGO_URL
```

## Operational Considerations

### Backup strategies

- **MongoDB Atlas**: Continuous backups with point-in-time recovery. For self-managed MongoDB, use `mongodump` with `--oplog` for a consistent snapshot.
- **PostgreSQL**: `pg_basebackup` for filesystem-level backups; `pg_dump` for logical exports. TimescaleDB adds native continuous aggregates.
- **Redis**: `BGSAVE` for RDB snapshots; `AOF` (append-only file) for durability. For distributed caches, use Redis Cluster replication.
- **S3/GCS Parquet**: Enable versioning and lifecycle policies. Parquet files are immutable once written, so versioned S3 buckets provide the audit trail.

### Connection pooling

All network database clients should use connection pooling:

- **MongoDB**: `MongoClient` maintains a pool of connections internally (default 100). Share a single client instance across the campaign.
- **PostgreSQL**: Use `psycopg2.pool.ThreadedConnectionPool` or `asyncpg.create_pool` (default 10).
- **Redis**: `redis.ConnectionPool` (default 50 connections per host).

Never create a new client per query — the connection establishment overhead dominates.

### Retry logic

Transient network errors (timeouts, connection reset, 503 Service Unavailable) are common in distributed systems. All database operations should be wrapped with exponential backoff:

```python
import time

def with_retry(fn, max_retries: int = 3, base_delay: float = 1.0):
    for attempt in range(max_retries):
        try:
            return fn()
        except (ConnectionError, TimeoutError) as exc:
            if attempt == max_retries - 1:
                raise
            delay = base_delay * (2 ** attempt)
            time.sleep(delay)
```

MongoDB and PostgreSQL drivers have built-in retry logic for certain operations (e.g., `mongocryptd` retries, PostgreSQL transaction retries). Check the driver documentation.

### Consistency guarantees

Distributed databases offer different consistency models:

- **MongoDB**: Tunable consistency — reads can be `majority`, `linearizable`, or `local`. For OSimFlow campaign data, `majority` is a good default: reads see most recent acknowledged writes without the latency cost of linearizability.
- **PostgreSQL**: Default is read committed. For stronger guarantees, use `SERIALIZABLE` isolation (higher latency, lower throughput). TimescaleDB's hypertables inherit PostgreSQL's MVCC model.
- **Redis**: Strong consistency for single-key operations; eventual consistency for Lua scripts and pub/sub. Cache invalidation in `DistributedCache` uses pub/sub, so there is a brief window where a worker on one node may have stale cache after another node invalidates.

### Monitoring

Regardless of the backend, monitor:

- **Connection pool utilization**: `db.serverStatus().connections` (MongoDB), `pg_stat_activity` (PostgreSQL), `redis.info("clients")` (Redis)
- **Query latency percentiles**: p50, p95, p99. Alert on p99 > 1 s.
- **Disk / bucket usage**: Campaign artifact growth can be large.
- **Replication lag**: For managed MongoDB Atlas or DocumentDB replicas, monitor `oplog.tail_lag` or `rs.status().replicationLag`.

The existing `osimflow/observability.py` pluggable observability backend can be extended to emit database metrics alongside campaign metrics.
