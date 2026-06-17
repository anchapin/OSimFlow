## Gap ID
ARCH-007 / OPS-003 (duplicate)

## Source
gap-analysis-architecture, gap-analysis-operational

## Description
OSimFlow's SQLiteCache is designed for single-node campaigns. For multi-node distributed campaigns (e.g., multiple Slurm workers or AWS Batch jobs), there is no persistent distributed state store. Workers cannot coordinate cache state, and the orchestrator cannot recover from restarts.

openstudio-server uses Redis + MongoDB for distributed state coordination.

## Evidence
- `osimflow/cache.py` — SQLiteCache with local WAL mode
- No Redis, etcd, or Consul integration
- `run.json` is local filesystem-dependent

## Severity
Critical

## Recommended Mitigation
- Phase 1: Add Redis pub/sub for cache invalidation broadcast
- Phase 2: Migrate `run.json` to MongoDB or PostgreSQL
- Phase 3: Add etcd for distributed leader election

## Labels
gap-analysis, architecture, distributed, cache, critical
