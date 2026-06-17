# OSimFlow vs openstudio-server: Gap Analysis Summary

**Date:** June 13, 2026  
**Prepared by:** Multi-agent gap analysis (8 specialized sub-agents)  
**Worktrees:** 8 isolated worktrees created for parallel analysis

---

## Executive Summary

OSimFlow is a Python-based CLI tool for orchestrating OpenStudio simulation campaigns, while openstudio-server (OSAF) is a full-stack web application with distributed computing capabilities. This gap analysis identifies **8 Critical** and **15 Major** gaps that must be addressed before OSimFlow can serve as a full replacement for openstudio-server.

**Key Finding:** The fundamental difference is architectural intent. OSimFlow is designed as a single-user CLI tool with a "bring your own infrastructure" model. openstudio-server is a persistent multi-user web application with built-in distributed computing. They are complementary rather than direct replacements at this stage.

---

## Critical Gaps (8)

| Issue # | Gap ID | Title | Source Agent |
|---------|--------|-------|--------------|
| #331 | EXEC-001 | Azure and Google Batch executors are non-functional stubs | Execution Backend |
| #330 | ARCH-007 | No persistent distributed state store for multi-node campaigns | Architecture |
| #333 | SEC-004 | REST API has authentication but no TLS enforcement | Security |
| #332 | EXT-007 | Iterative algorithm observe() feedback loop never wired | BYOS/Extensibility |
| #334 | EXT-012 | No measure management system | BYOS/Extensibility |
| #335 | ARCH-001 | No distributed task queue infrastructure | Architecture |
| #336 | API-011 | API campaign launch only supports LocalExecutor | API/Interface |
| #337 | GAP-001 | No web-based user interface (PAT) | Feature Parity |

---

## Major Gaps (15)

| Issue # | Gap ID | Title | Source Agent |
|---------|--------|-------|--------------|
| #338 | ARCH-004 | No horizontal worker auto-scaling infrastructure | Architecture |
| #339 | ARCH-005 | No distributed shared storage model | Architecture |
| #340 | OPS-001 | No distributed log aggregation | Operational |
| #341 | OPS-002 | No worker-level health checks or liveness probes | Operational |
| #342 | GAP-008 | No persistent results database with query API | Feature Parity |
| #343 | SEC-006 | LocalExecutor BYOS scripts have no CPU/memory limits | Security |
| #344 | SEC-009 | NomadExecutor HTTP has no TLS verification | Security |
| #345 | ALGO-001 | No native Genetic Algorithm implementation | Algorithms |
| #346 | ALGO-009 | SobolAlgorithm generates samples but computes no sensitivity indices | Algorithms |
| #347 | API-001 | No variable management API | API/Interface |
| #348 | API-002 | No measure management API | API/Interface |
| #349 | API-008 | No Python/Ruby client SDK | API/Interface |
| #350 | EXEC-002 | No container orchestration layer (Docker Swarm/Kubernetes) | Execution Backend |
| #351 | EXEC-004 | No PBS/LSF support in HPC executor | Execution Backend |
| #352 | EXEC-006 | Spot/preemptible instance handling only in AWS Batch | Execution Backend |

---

## Gap Count by Category

| Category | Critical | Major | Minor | Total |
|----------|----------|-------|-------|-------|
| Architecture | 3 | 2 | 5 | 10 |
| Feature Parity | 2 | 3 | 3 | 8 |
| API/Interface | 2 | 4 | 6 | 12 |
| Algorithms | 0 | 2 | 8 | 10 |
| Execution Backend | 1 | 4 | 3 | 8 |
| Operational | 0 | 4 | 6 | 10 |
| BYOS/Extensibility | 2 | 8 | 11 | 21 |
| Security | 1 | 5 | 6 | 12 |
| **Total** | **11** | **32** | **48** | **91** |

---

## Worktrees Created

Each sub-agent worked in an isolated Git worktree:

1. `worktrees/gap-analysis-architecture/` - Architecture patterns comparison
2. `worktrees/gap-analysis-feature-parity/` - Feature coverage comparison
3. `worktrees/gap-analysis-api-interface/` - API endpoint comparison
4. `worktrees/gap-analysis-algorithms/` - Algorithm coverage comparison
5. `worktrees/gap-analysis-execution-backend/` - Executor backend comparison
6. `worktrees/gap-analysis-operational/` - Operational readiness comparison
7. `worktrees/gap-analysis-byos-extensibility/` - Extensibility comparison
8. `worktrees/gap-analysis-security/` - Security model comparison

---

## Recommended Priority Order

### Phase 1 (MVP Parity - 4-6 weeks)
1. **SEC-004** - Add TLS to REST API (1 day)
2. **API-011** - Extend API to support all executors (1 week)
3. **EXT-007** - Wire iterative algorithm observe() loop (1 week)
4. **EXEC-001** - Implement or remove Azure/Google stubs (1 week)
5. **SEC-006** - Add resource limits to BYOS subprocess (2 days)

### Phase 2 (Production Readiness - 8-12 weeks)
1. **ARCH-001** - Integrate dask-jobqueue for distributed task queue
2. **ARCH-007** - Add Redis for distributed state/caching
3. **EXT-012** - Build MeasureRegistry for measure management
4. **OPS-001** - Add distributed log aggregation
5. **ALGO-001** - Implement Genetic Algorithm
6. **ALGO-009** - Add Sobol sensitivity index computation

### Phase 3 (Full Parity - 12-16 weeks)
1. **GAP-001** - Build web-based PAT-style UI
2. **ARCH-004** - Add horizontal worker auto-scaling
3. **EXEC-002** - Add Kubernetes/Helm deployment
4. **GAP-008** - Add persistent results database

---

## What OSimFlow Does Well (Not Gaps)

- Clean algorithm plugin framework (AlgorithmRegistry + BaseAlgorithm ABC)
- Explicit SHA-256 cache invalidation with WAL-mode SQLite
- Pluggable observability backends (CloudWatch, Prometheus, OTel)
- Subprocess BYOS isolation (security advantage over OSS)
- Structured JSON logging with rotation
- Rich CLI with comprehensive flags
-OSA export/import for PAT compatibility

---

## Conclusion

OSimFlow is not yet ready as a full replacement for openstudio-server, but it has solid foundations and a clear path to parity. The most critical gaps are architectural (distributed task queue, state store) rather than feature-level. Addressing Phase 1 items would enable OSimFlow to handle most single-user campaign orchestration use cases. Full PAT parity requires the web UI and measure management system, which are significant undertakings.

**Estimated effort to MVP parity:** 4-6 weeks  
**Estimated effort to full PAT parity:** 12-16 weeks
