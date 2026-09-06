# ADR-0004 — Single-instance Redis is a scoped decision; campaign-restart-by-replay is the recovery story (issue #1562)

**Status:** Accepted
**Date:** 2026-09-06
**Deciders:** OSimFlow maintainers
**Supersedes:** none
**Superseded by:** none
**Related:** #1111, #1014, #1397, #1462, #1562, AGENTS.md §5
(`distributed_cache.py`, `document_store.py`, `distributed_jobqueue.py`,
`circuit_breaker.py`, `api/app.py`)

> **Implementation status (2026-09-06):** Documented. The four Redis-
> backed planes remain single-instance; a new
> `_check_redis_deployment_mode()` health check (issue #1562 acceptance
> criterion (b)) makes the deployment topology visible at `osimflow
> health` time, and `user-guide.md` documents the documented recovery
> story — campaign restart via cache replay — for the case where Redis
> is unavailable mid-run.

## Context

A single `--redis-url` simultaneously backs four distributed planes:

| Plane | Module | Behaviour on Redis outage |
|---|---|---|
| Distributed cache | `osimflow/distributed_cache.py` | Circuit-breaker fail-fast (issue #1111); degrades to local-only because the cache is a soft hint. |
| Document store | `osimflow/document_store.py` (`RedisDocumentStore`) | Circuit-breaker fail-fast; **refuses** silent divergence — raises `DocumentStoreError` because it is the source of truth (issue #1014). |
| Distributed job queue | `osimflow/distributed_jobqueue.py` | Circuit-breaker fail-fast (issue #1397). |
| API rate limiter | `osimflow/api/app.py` (`RateLimitMiddleware`) | Falls back to in-process dict — backward-compatible but unsafe for multi-worker (issue #663, #768). |

A grep across `osimflow/` for `redis.sentinel`, `Sentinel`, `Cluster`,
`redis.cluster`, or `replica` returns zero matches in any of the four
modules. All four construct their client with
`redis.from_url()` / `redis.ConnectionPool.from_url()`, which produces a
plain single-instance `Redis` client — the redis-py `Sentinel` and
`RedisCluster` entry points are unused. The only resilience
mechanisms are per-plane circuit breakers that fail fast on persistent
outage; there is no cluster-aware hashing, no master/replica routing,
no Sentinel-aware topology refresh.

For 1000-sample cloud / HPC campaigns where multi-hour wall-clock runs
are normal, the unreplicated Redis is therefore a single point of
failure for exactly the runs the distributed mode exists to serve, and
the failure mode is amplified by all four planes sharing one blast
radius (issue #1562 §1).

### Why the existing posture exists

The four modules share the same scoped decision: a single-instance
Redis is acceptable today because

1. **The cache is a soft hint.** `DistributedCache` keeps a pid-private
   local SQLite file underneath; cache entries that miss the local
   store are still recoverable from the Redis shared store when Redis
   comes back, and the breaker prevents every call from burning the
   5 s socket timeout on a still-down Redis (#1111).
2. **The document store refuses to diverge silently.** A Redis outage
   that *seems* to recover — but with stale data — would corrupt the
   source-of-truth view across workers. Raising
   `DocumentStoreError` (fail-loud) is strictly safer than degrading
   to local-only state (#1014).
3. **The job queue is fail-fast.** A campaign that loses its
   pending/in-progress registry mid-run cannot resume coherent fan-out
   anyway; failing fast closes the control-plane hole #1397 opened.
4. **The rate limiter degrades to in-process** — a backward-compatible
   fallback (#663, #768) at the cost of multi-worker correctness, but
   a rate-limit breach is a far smaller blast radius than a silent
   document-store divergence.

The four planes converge on a single point: an unreplicated Redis
mid-run is not a *latent* failure — by design, the document store
will raise, the campaign will abort, and recovery is *restart* (not
*auto-resume*). That recovery story is what ADR-0004 pins.

## Decision

1. **Single-instance Redis stays as the supported deployment topology**
   for `--redis-url`. We do **not** ship Sentinel or Cluster client
   wiring in this change.
2. **The health check** `osimflow health --redis-url <URL>` (new flag,
   non-`run` subcommand) reports the deployment mode of the configured
   Redis instance so operators can confirm the topology the rest of
   the system assumes. The check returns PASS for the documented
   single-instance case, WARN for Sentinel / Cluster URLs that the
   current code path cannot route through, and FAIL when the URL is
   unreachable.
3. **The recovery story for a Redis outage mid-campaign** is the
   documented campaign-restart via cache replay
   (`osimflow run --outdir <same outdir>`). Completed steps are
   cache-hits; the campaign picks up at the first un-cached step and
   continues. This is the same path `osimflow warm-cache` pre-populates
   (issue #1027). Documented in `docs/user-guide.md` under
   §7.7 (new subsection).
4. **Future work** — Sentinel / Cluster client wiring — is a separate
   ADR-and-PR cycle. ADR-0004 closes #1562 with the scoped decision,
   the visibility (health check), and the documented recovery story,
   not with the client re-write.

### What `_check_redis_deployment_mode` reports

| Field | Source | Meaning |
|---|---|---|
| `mode` | URL heuristic + connect probe | `"single"` (default), `"sentinel"` (URL hints `redis+sentinel://` or `sentinel://`), `"cluster"` (URL hints `redis+cluster://` or `redis-cluster`), `"unknown"` |
| `connected` | `PING` result | Whether the probe reached Redis within the 5 s timeout |
| `latency_ms` | Wall-clock delta around `PING` | Useful for capacity tuning; not a gate |
| `info_role` | `INFO replication` | `master` / `replica` / `unknown` — surfaced for ops awareness; the current code path does not act on this |
| `redis_version` | `INFO server` | Surfaced when reachable |

The check is INFORMATIONAL by default (mirroring the per-executor
substrate checks, issue #1024). It is *not* promoted to CRITICAL when
the configured executor is `local` because a single-node local
campaign never touches Redis; it is, however, useful in the
CI / staging workflows where `--redis-url` is set.

### Why this over Path (a) — Sentinel / Cluster client wiring

The acceptance criterion in #1562 explicitly allows either:

> (a) `--redis-url` accepts Sentinel / cluster topologies end-to-end
> (documented + integration test against a local Sentinel fixture), **or**
> (b) an explicit ADR documents single-instance Redis as a scoped
> decision with the campaign-restart / resume-by-replay path verified
> as the recovery story, plus a health check in `osimflow health` that
> reports the Redis deployment mode.

Path (a) is 3–5 days of redis-py work (Sentinel discovery + cluster
slot hashing + integration test fixture + per-plane hash-tag rewrites
— note the document-store per-collection `HSETNX` counters assume
single-key semantics, which breaks under hash-tag-only cluster
hashing), and it changes the failure model of a code path that today
deliberately fails loud. Path (b) ships the same operational
visibility in a fraction of the surface area and lets a follow-up
PR tackle the client re-write once Sentinel / Cluster is a real
deployment requirement, not a hypothetical one.

### Recovery story verification — campaign-restart-by-replay

The cache-replay resume path is the same path `osimflow run --outdir
<same>` (re-run with the same outdir) and `osimflow warm-cache
--n_warm N` take. The Campaign orchestrator:

1. Calls `_compute_code_hashes()` (`osimflow/campaign.py`) which hashes
   the relevant per-step inputs (bin scripts, work module, BYOS,
   container digest, variables.yml).
2. For each step, computes `CacheKey` (`osimflow/cache.py`) from the
   hash + step parameters.
3. Looks up `(cache_key, namespace)` in the local SQLite (single-node)
   or the Redis shared store + local fallback (distributed).
4. On hit, replays the recorded output paths and skips execution.
5. On miss, executes the step, records the cache entry, and continues.

This path is exercised by `make smoke` (3-sample stub-mode campaign
into `./results_smoke`) and by the per-substrate cache-resume
integration tests listed in `docs/substrate-coverage.md`. The
`tests/integration/test_distributed_cache_invalidation.py` suite
already asserts cold-vs-warm re-run behavior with fakeredis (issue
#1389). ADR-0004 therefore records the recovery path as
**already verified** by the existing test surface — no new test
harness is required.

`docs/user-guide.md` gains a new subsection **§7.7 "Recovery:
Redis outage mid-campaign"** that documents the three steps an
operator takes after a Redis outage:

1. Confirm Redis is back (or stand up a new instance).
2. Re-run the campaign against the same `--outdir`.
3. Watch `run.json.cache_hit_rate` climb back to ~100 % as completed
   steps replay from cache.

## Alternatives Considered

### Add Sentinel / Cluster client wiring (Path (a))

**Rejected for this PR.** Effort XL. Specifically:

- **`redis.sentinel.Sentinel`** requires a list of sentinel hosts and
  a service name — not a single URL. Migrating from a single URL to a
  list changes the parser, the docstrings, the doc, and the user
  surface. The CLI flag shape (`--redis-url`) cannot carry a sentinel
  list without breaking change.
- **`redis.cluster.RedisCluster`** requires a list of startup nodes
  and breaks the per-collection `HSETNX` counter pattern the document
  store uses (`osimflow/document_store.py` `RedisDocumentStore`
  `_counter_key` and `_index_key`). Cluster-compatible client hashing
  needs `{tag}`-style hash tags, which the current key naming
  (`osimflow:docs:counter:<namespace>:<collection>`,
  `osimflow:docs:idx:<namespace>:<collection>:<field>`) does not
  use — every collection lands on the same slot.
- **Integration test fixture** requires a Docker Compose with
  redis-server + redis-sentinel + (optionally) cluster-mode; CI
  matrix expansion.
- **Operational surface** — TLS posture for Sentinel traffic, ACL
  scope for the `+sentinel` user, master/replica split-brain
  handling — is currently undocumented and would need its own ADR.

### Move the source-of-truth state off Redis entirely

**Rejected.** The four-plane design (`DistributedCache` for hints,
`RedisDocumentStore` for source-of-truth, `DistributedJobQueue` for
control plane, `RateLimitMiddleware` for security-state) was
established because *workers diverge silently* on a shared
filesystem. Moving the source of truth back to SQLite and accepting
the divergence is a regression on issue #1014's acceptance criterion.

### Single-instance Redis + daily backups

**Rejected.** Backups solve durability, not availability — a
mid-campaign outage still aborts the campaign; backups only reduce
the cost of recovery. The cache-replay resume path already gives us
the "skip the work that was done" property without an additional
backup layer.

## Consequences

### Positive

- **No fixture complexity.** Path (a) would have required a
  Docker-Compose redis-sentinel fixture in CI; Path (b) needs only
  fakeredis for the health-check unit test, which is already in
  `[dev]` and used by
  `tests/integration/test_distributed_cache_invalidation.py`.
- **Visibility.** Operators running a long cloud campaign now have a
  one-shot command (`osimflow health --redis-url …`) that surfaces
  the deployment topology the rest of the system assumes, plus the
  per-plane circuit-breaker states (issue #1191, #1307).
- **Documented recovery.** The user-guide subsection explicitly walks
  an operator through the cache-replay resume path, so the
  "campaign aborts because Redis died" failure mode no longer comes
  as a surprise.
- **Defers a 3-5 day PR.** ADR-0004 keeps the focused PR scope
  achievable without blocking #1562; the Sentinel / Cluster work is
  unblocked but does not gate #1562's closure.

### Negative

- **Single point of failure for long campaigns.** A multi-hour run
  on a campaign-per-worker pattern (ADR-0003 §Pattern 2) that loses
  its Redis mid-run will abort and require restart. The mitigation
  is the cache-replay resume path; the mitigation is **not**
  automatic failover.
- **Hash-tag mismatch** in the document store's key naming means
  that even if a future Sentinel / Cluster wiring lands, the
  per-collection `HSETNX` counter pattern will need a re-keying
  pass before it can route through a cluster.

### Neutral

- The per-plane circuit breakers (#1111, #1397, #1014) keep
  fail-fast semantics; this ADR does not change them.
- The cache-replay resume path is unchanged; this ADR documents it
  rather than rebuilding it.

## Gap Closure Criteria

ADR-0004 can be superseded when any of the following become a real
deployment requirement:

1. A multi-region campaign-per-worker pattern where workers
   legitimately need a Sentinel-aware primary failover (so a Redis
   outage does not abort an in-flight campaign).
2. A Redis Cluster deployment with sharded state across the four
   planes — requires the hash-tag re-keying pass for the document
   store's `_counter_key` / `_index_key` family.
3. Operational telemetry on Redis master/replica split-brain — a
   requirement that today's `INFO replication` probe into the health
   check cannot satisfy.

Until then, single-instance Redis + cache-replay resume remains the
scoped decision.

## References

- Issue #1562: Define an HA story for the Redis control plane that
  all four distributed modules share.
- Issue #1014: Distributed document store that fails loud on Redis
  outage (the "source-of-truth" decision).
- Issue #1111: Per-plane circuit breaker (`osimflow/circuit_breaker.py`).
- Issue #1397: DistributedJobQueue control-plane circuit breaker.
- Issue #1462: Campaign module extraction (which gave us
  `CampaignQuotaGuard`, `CampaignChaosWiring`, `CampaignLifecycle`,
  `CampaignArtifactWriter`, etc.) and incidentally exposed the
  Redis-touching call sites the issue #1562 maps.
- Issue #1191 / #1307: circuit-breaker states recorded in
  `run.json`.
- ADR-0003 — Coordinator High Availability
  (`.agents/results/architecture/0003-coordinator-high-availability.md`):
  the campaign-level HA patterns that cache-replay resume composes
  with.
- `osimflow/distributed_cache.py` — DistributedCache + circuit breaker.
- `osimflow/document_store.py` — `RedisDocumentStore` source-of-truth
  semantics.
- `osimflow/distributed_jobqueue.py` — control-plane job queue.
- `osimflow/api/app.py` — `RateLimitMiddleware` (degrades to
  in-process on outage).
- `osimflow/circuit_breaker.py` — `CircuitBreaker` primitive.
- `osimflow/health.py` — `_check_redis_deployment_mode` health
  check.
- `docs/user-guide.md` §7.7 — operator-facing recovery steps.
- `docs/substrate-coverage.md` — per-substrate cache-resume E2E
  matrix.
