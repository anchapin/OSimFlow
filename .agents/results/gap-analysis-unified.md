# Unified Gap Analysis: OSimFlow vs openstudio-server

**Date:** 2026-06-13
**Last Updated:** 2026-06-16
**Total Gaps:** 69
**Resolved:** 22 (see below)
**Categories:** UX, ARCH, SIM, DEVEX, OPS

---

## Summary by Category

| Category | Total | Resolved | Open |
|----------|-------|----------|------|
| UX | 7 | 0 | 7 |
| ARCH | 18 | 6 | 12 |
| SIM | 27 | 7 | 20 |
| DEVEX | 5 | 3 | 2 |
| OPS | 12 | 6 | 6 |

---

## Resolved Gaps

The following gaps have been addressed via dedicated issues and PRs:

| Gap ID | Issue | PR | Resolution |
|--------|-------|-----|------------|
| DEVEX-001 | #432 | #518 | Plugin Discovery for AlgorithmRegistry and Executor Factory |
| DEVEX-003 | #433 | #477 | Auto-generate Python client from OpenAPI spec |
| DEVEX-012 | #436 | #530 | Per-Sample Trace IDs to ObservabilityBackend |
| SIM-001 | #405 | #496 | DOE Analysis & Visualization |
| SIM-004 | #408 | #519 | Interactive Parameter Selection GUI |
| SIM-005 | #409 | #526 | Argument Type Auto-Coercion |
| SIM-007 | #411 | #476 | CLI Health Checks |
| SIM-013 | #417 | #527 | Job Priority Levels |
| SIM-027 | #431 | #521 | Cross-measure Argument Validation |
| ARCH-003 | #342 | #364 | SQLite Results Database with Query API |
| ARCH-012 | #398 | #511 | Pre-flight Config Validation API Endpoint |
| ARCH-013 | #399 | #484 | Offline Mode Documentation |
| ARCH-018 | #404 | #481 | Campaign Comparison REST API Endpoint |
| OPS-003 | #439 | #529 | Audit Logging |
| OPS-004 | #440 | #525 | SQLite Backup/Export/Import |
| OPS-006 | #442 | #523 | RBAC and Multi-User Access Controls |
| OPS-007 | #443 | #522 | Worker Auto-Recovery for Redis Subscribers |
| OPS-008 | #444 | #524 | Campaign Pause/Resume |
| OPS-009 | #445 | #528 | Rate Limiting Per-User and Per-Campaign |

---

## All Gaps

### UX-001: No Interactive Web GUI for Campaign Creation & Management

- **Category:** UX
- **Severity:** Not specified
- **Priority:** P2
- **Effort:** Not specified
- **Status:** OPEN
- **Description:** See detailed analysis

---

### UX-002: No Visual Project/Variable Designer

- **Category:** UX
- **Severity:** Not specified
- **Priority:** P2
- **Effort:** Not specified
- **Status:** OPEN
- **Description:** See detailed analysis

---

### UX-003: No Per-Sample Drill-Down in Web Interface

- **Category:** UX
- **Severity:** Not specified
- **Priority:** P2
- **Effort:** Not specified
- **Status:** OPEN
- **Description:** See detailed analysis

---

### UX-004: Streamlit Dashboard is Separate from Main API Server

- **Category:** UX
- **Severity:** Not specified
- **Priority:** P3
- **Effort:** Not specified
- **Status:** OPEN
- **Description:** See detailed analysis

---

### UX-005: Onboarding Requires Understanding 50+ CLI Flags

- **Category:** UX
- **Severity:** Not specified
- **Priority:** P2
- **Effort:** Not specified
- **Status:** OPEN
- **Description:** See detailed analysis

---

### UX-006: No Real-Time Error Diagnosis in Web Interface

- **Category:** UX
- **Severity:** Not specified
- **Priority:** P2
- **Effort:** Not specified
- **Status:** OPEN
- **Description:** See detailed analysis

---

### UX-007: No Campaign Comparison in Web Interface

- **Category:** UX
- **Severity:** Not specified
- **Priority:** P3
- **Effort:** Not specified
- **Status:** OPEN
- **Description:** See detailed analysis

---

### ARCH-001: No Built-in Web Management Interface

- **Category:** ARCH
- **Severity:** Not specified
- **Priority:** P2
- **Effort:** Not specified
- **Status:** OPEN
- **Description:** | OSS provides a Management Console (web UI at localhost:8080) for project creation, analysis monitoring, and result visualization. OSimFlow has no built-in web interface — only an optional FastAPI REST API (`osimflow/api/`) and TUI (`osimflow/tui.py`). |

---

### ARCH-002: No Interactive Report Generation

- **Category:** ARCH
- **Severity:** Not specified
- **Priority:** P3
- **Effort:** Not specified
- **Status:** OPEN
- **Description:** | OSS generates HTML reports with interactive Highcharts visualizations and PDF export. OSimFlow generates static PNG/PDF plots via matplotlib/seaborn (`bin/generate_plots.py`). |

---

### ARCH-003: No Persistent Data Store (MongoDB Equivalent)

- **Category:** ARCH
- **Severity:** Not specified
- **Priority:** P2
- **Effort:** Not specified
- **Status:** RESOLVED (#342, PR #364)
- **Description:** | OSS stores all data points, analyses, and projects in MongoDB with a structured schema (`Project → Analysis → Simulation → Data Point`). OSimFlow uses file-based storage: SQLite cache, `run.json`, `aggregated_results.csv`, Parquet. |

---

### ARCH-004: No Built-in Project Management Hierarchy

- **Category:** ARCH
- **Severity:** Not specified
- **Priority:** P3
- **Effort:** Not specified
- **Status:** OPEN
- **Description:** | OSS has a `Project → Analysis → Simulation → Data Point` hierarchy with persistent state. OSimFlow has flat campaigns with `outdir`-based isolation. |

---

### ARCH-005: No Horizontal Scaling for Web Interface

- **Category:** ARCH
- **Severity:** Not specified
- **Priority:** P3
- **Effort:** Not specified
- **Status:** OPEN
- **Description:** | OSS scales horizontally via Kubernetes Helm chart with multiple Sidekiq workers. OSimFlow's optional REST API (`osimflow serve`) is single-process FastAPI. |

---

### ARCH-006: No Built-in High Availability for Coordinator

- **Category:** ARCH
- **Severity:** Not specified
- **Priority:** P3
- **Effort:** Not specified
- **Status:** OPEN
- **Description:** | OSS can run with replicated MongoDB and multiple Sidekiq workers for HA. OSimFlow's `Campaign` class is single-process — if the coordinator crashes, the campaign pauses until manually resumed. |

---

### ARCH-007: No Distributed Job Queue (Redis Equivalent)

- **Category:** ARCH
- **Severity:** Not specified
- **Priority:** P2
- **Effort:** Not specified
- **Status:** RESOLVED (#330, PR #489)
- **Description:** | OSS uses Redis + Sidekiq for distributed job queuing with worker pools. OSimFlow dispatches jobs directly to executors (Slurm, Batch, etc.) without an intermediate queue. |

---

### ARCH-008: No Real-time Monitoring Dashboard

- **Category:** ARCH
- **Severity:** Not specified
- **Priority:** P2
- **Effort:** Not specified
- **Status:** OPEN
- **Description:** | OSS Management Console provides real-time progress bars, status updates, and log streaming. OSimFlow has TUI (terminal-only) and SSE events (requires API server). |

---

### ARCH-009: No Multi-User Authentication/Authorization

- **Category:** ARCH
- **Severity:** Not specified
- **Priority:** P2
- **Effort:** Not specified
- **Status:** RESOLVED (#442, PR #523)
- **Description:** | OSS Management Console has built-in user authentication. OSimFlow's API has optional `--api-key` for basic auth but no multi-user RBAC. |

---

### ARCH-010: No Event Sourcing for Campaign State

- **Category:** ARCH
- **Severity:** Not specified
- **Priority:** P3
- **Effort:** Not specified
- **Status:** OPEN
- **Description:** | OSS stores every data point mutation in MongoDB with full history. OSimFlow writes final state to `run.json` and intermediate state to SQLite cache (overwritten on resume). |

---

### ARCH-011: No Container Image Build Pipeline

- **Category:** ARCH
- **Severity:** Not specified
- **Priority:** P3
- **Effort:** Not specified
- **Status:** OPEN
- **Description:** | OSS builds its own Docker images with Rails app + workers. OSimFlow consumes `nrel/openstudio` images from Docker Hub (or ECR mirror) but doesn't build custom images. |

---

### ARCH-012: No Configuration Validation UI

- **Category:** ARCH
- **Severity:** Not specified
- **Priority:** P2
- **Effort:** Not specified
- **Status:** RESOLVED (#398, PR #511)
- **Description:** | OSS validates `analysis.json` schema via web UI with inline error messages. OSimFlow validates `variables.yml` at CLI startup but errors are terminal. |

---

### ARCH-013: No Offline Mode Documentation

- **Category:** ARCH
- **Severity:** Not specified
- **Priority:** P3
- **Effort:** Not specified
- **Status:** RESOLVED (#399, PR #484)
- **Description:** | OSS runs entirely offline (Docker images cached locally). OSimFlow has `--offline` flag (issue #261) and offline bundle support, but documentation is sparse. |

---

### ARCH-014: No Distributed Tracing Integration

- **Category:** ARCH
- **Severity:** Not specified
- **Priority:** P3
- **Effort:** Not specified
- **Status:** OPEN
- **Description:** | OSS has basic request logging. OSimFlow has pluggable observability backends (CloudWatch, Prometheus, OpenTelemetry) but no distributed tracing (OpenTelemetry traces). |

---

### ARCH-015: No Secret Management Integration

- **Category:** ARCH
- **Severity:** Not specified
- **Priority:** P3
- **Effort:** Not specified
- **Status:** OPEN
- **Description:** | OSS manages secrets via environment variables. OSimFlow uses IAM roles for cloud executors but has no integration with Vault, AWS Secrets Manager, or similar. |

---

### ARCH-016: No Blue/Green Deployment Support

- **Category:** ARCH
- **Severity:** Not specified
- **Priority:** P3
- **Effort:** Not specified
- **Status:** OPEN
- **Description:** | OSS can be deployed with blue/green strategy via Kubernetes. OSimFlow is installed via pip with no built-in deployment orchestration. |

---

### ARCH-017: No Metrics Exporter for Infrastructure Monitoring

- **Category:** ARCH
- **Severity:** Not specified
- **Priority:** P3
- **Effort:** Not specified
- **Status:** OPEN
- **Description:** | OSS exports metrics to monitoring systems. OSimFlow has `run.json` and optional MLflow, but no Prometheus exporter or StatsD integration for infrastructure-level metrics (queue depth, worker count, resource utilization). |

---

### ARCH-018: No Campaign Comparison API

- **Category:** ARCH
- **Severity:** Not specified
- **Priority:** P2
- **Effort:** Not specified
- **Status:** RESOLVED (#404, PR #481)
- **Description:** | OSS allows side-by-side comparison of analyses via Management Console. OSimFlow has `osimflow compare` CLI subcommand but no API endpoint for programmatic comparison. |

---

### SIM-001: Missing DOE Analysis & Visualization

- **Category:** SIM
- **Severity:** Not specified
- **Priority:** P2
- **Effort:** Not specified
- **Status:** RESOLVED (#405, PR #496)
- **Description:** See detailed analysis

---

### SIM-002: No Custom DOE Pattern Generation

- **Category:** SIM
- **Severity:** Not specified
- **Priority:** P3
- **Effort:** Not specified
- **Status:** OPEN
- **Description:** See detailed analysis

---

### SIM-003: No Automatic Measure Discovery for Sampling

- **Category:** SIM
- **Severity:** Not specified
- **Priority:** P2
- **Effort:** Not specified
- **Status:** OPEN
- **Description:** See detailed analysis

---

### SIM-004: No Interactive Parameter Selection GUI

- **Category:** SIM
- **Severity:** Not specified
- **Priority:** P3
- **Effort:** Not specified
- **Status:** RESOLVED (#408, PR #519)
- **Description:** See detailed analysis

---

### SIM-005: No Argument Type Auto-Coercion

- **Category:** SIM
- **Severity:** Not specified
- **Priority:** P2
- **Effort:** Not specified
- **Status:** RESOLVED (#409, PR #526)
- **Description:** See detailed analysis

---

### SIM-006: No Parameter Sensitivity Analysis Integration

- **Category:** SIM
- **Severity:** Not specified
- **Priority:** P3
- **Effort:** Not specified
- **Status:** OPEN
- **Description:** See detailed analysis

---

### SIM-007: No CLI Health Checks

- **Category:** SIM
- **Severity:** Not specified
- **Priority:** P2
- **Effort:** Not specified
- **Status:** RESOLVED (#411, PR #476)
- **Description:** See detailed analysis

---

### SIM-008: No Automatic Version Detection

- **Category:** SIM
- **Severity:** Not specified
- **Priority:** P3
- **Effort:** Not specified
- **Status:** OPEN
- **Description:** See detailed analysis

---

### SIM-009: No Server-Managed CLI Lifecycle

- **Category:** SIM
- **Severity:** Not specified
- **Priority:** P3
- **Effort:** Not specified
- **Status:** OPEN
- **Description:** See detailed analysis

---

### SIM-010: No Background Job Processing Framework

- **Category:** SIM
- **Severity:** Not specified
- **Priority:** P3
- **Effort:** Not specified
- **Status:** OPEN
- **Description:** See detailed analysis

---

### SIM-011: No Container Health Monitoring

- **Category:** SIM
- **Severity:** Not specified
- **Priority:** P3
- **Effort:** Not specified
- **Status:** OPEN
- **Description:** See detailed analysis

---

### SIM-012: No Automatic Retry Across Steps

- **Category:** SIM
- **Severity:** Not specified
- **Priority:** P2
- **Effort:** Not specified
- **Status:** OPEN
- **Description:** See detailed analysis

---

### SIM-013: No Job Priority Levels

- **Category:** SIM
- **Severity:** Not specified
- **Priority:** P3
- **Effort:** Not specified
- **Status:** RESOLVED (#417, PR #527)
- **Description:** See detailed analysis

---

### SIM-014: No Automatic Data Point Merging

- **Category:** SIM
- **Severity:** Not specified
- **Priority:** P2
- **Effort:** Not specified
- **Status:** OPEN
- **Description:** See detailed analysis

---

### SIM-015: No Data Point Lifecycle Management

- **Category:** SIM
- **Severity:** Not specified
- **Priority:** P3
- **Effort:** Not specified
- **Status:** OPEN
- **Description:** See detailed analysis

---

### SIM-016: No Data Point Reanalysis

- **Category:** SIM
- **Severity:** Not specified
- **Priority:** P3
- **Effort:** Not specified
- **Status:** OPEN
- **Description:** See detailed analysis

---

### SIM-017: No MongoDB/Distributed Storage Option

- **Category:** SIM
- **Severity:** Not specified
- **Priority:** P2
- **Effort:** Not specified
- **Status:** OPEN
- **Description:** See detailed analysis

---

### SIM-018: No JSON Schema Validation for .osw Workflows

- **Category:** SIM
- **Severity:** Not specified
- **Priority:** P2
- **Effort:** Not specified
- **Status:** OPEN
- **Description:** See detailed analysis

---

### SIM-019: No Weather File Validation Before Simulation

- **Category:** SIM
- **Severity:** Not specified
- **Priority:** P2
- **Effort:** Not specified
- **Status:** OPEN
- **Description:** See detailed analysis

---

### SIM-020: No Automatic Climate Zone Detection

- **Category:** SIM
- **Severity:** Not specified
- **Priority:** P3
- **Effort:** Not specified
- **Status:** OPEN
- **Description:** See detailed analysis

---

### SIM-021: No Distributed Cache

- **Category:** SIM
- **Severity:** Not specified
- **Priority:** P2
- **Effort:** Not specified
- **Status:** RESOLVED (#330, PR #489)
- **Description:** See detailed analysis

---

### SIM-022: No Cache Statistics/Hit Rate Monitoring

- **Category:** SIM
- **Severity:** Not specified
- **Priority:** P3
- **Effort:** Not specified
- **Status:** OPEN
- **Description:** See detailed analysis

---

### SIM-023: No Cache Warming/Prefetching

- **Category:** SIM
- **Severity:** Not specified
- **Priority:** P3
- **Effort:** Not specified
- **Status:** OPEN
- **Description:** See detailed analysis

---

### SIM-024: No Automatic Measure Argument Discovery

- **Category:** SIM
- **Severity:** Not specified
- **Priority:** P2
- **Effort:** Not specified
- **Status:** OPEN
- **Description:** See detailed analysis

---

### SIM-025: No Measure Dependency Resolution

- **Category:** SIM
- **Severity:** Not specified
- **Priority:** P3
- **Effort:** Not specified
- **Status:** OPEN
- **Description:** See detailed analysis

---

### SIM-026: No Measure Version Management

- **Category:** SIM
- **Severity:** Not specified
- **Priority:** P3
- **Effort:** Not specified
- **Status:** OPEN
- **Description:** See detailed analysis

---

### SIM-027: No Cross-measure Argument Validation

- **Category:** SIM
- **Severity:** Not specified
- **Priority:** P3
- **Effort:** Not specified
- **Status:** RESOLVED (#431, PR #521)
- **Description:** See detailed analysis

---

### DEVEX-001: Add `entry_points` discovery to `AlgorithmRegistry` and executor factory. This is the highest-impact extensibility improvement.

- **Category:** DEVEX
- **Severity:** Not specified
- **Priority:** P1
- **Effort:** Not specified
- **Status:** RESOLVED (#432, PR #518)
- **Description:** See detailed analysis

---

### DEVEX-003: Auto-generate a Python client from the OpenAPI spec. Low effort, high integration value.

- **Category:** DEVEX
- **Severity:** Not specified
- **Priority:** P2
- **Effort:** Not specified
- **Status:** RESOLVED (#433, PR #477)
- **Description:** See detailed analysis

---

### DEVEX-008: Add `schemathesis` contract tests for the REST API. Prevents accidental breaking changes.

- **Category:** DEVEX
- **Severity:** Not specified
- **Priority:** P2
- **Effort:** Not specified
- **Status:** OPEN
- **Description:** See detailed analysis

---

### DEVEX-009: Document or create a programmatic measure runner helper. Reduces BYOS friction for measure composition.

- **Category:** DEVEX
- **Severity:** Not specified
- **Priority:** P2
- **Effort:** Not specified
- **Status:** OPEN
- **Description:** See detailed analysis

---

### DEVEX-012: Extend `ObservabilityBackend` with per-sample trace IDs. Critical for distributed debugging at scale.

- **Category:** DEVEX
- **Severity:** Not specified
- **Priority:** P1
- **Effort:** Not specified
- **Status:** RESOLVED (#436, PR #530)
- **Description:** See detailed analysis

---

### OPS-001: No Campaign Health Dashboard

- **Category:** OPS
- **Severity:** Not specified
- **Priority:** P2
- **Effort:** Not specified
- **Status:** OPEN
- **Description:** See detailed analysis

---

### OPS-002: No Alerting Rules or Notification Delivery

- **Category:** OPS
- **Severity:** Not specified
- **Priority:** P2
- **Effort:** Not specified
- **Status:** OPEN
- **Description:** See detailed analysis

---

### OPS-003: No Audit Logging

- **Category:** OPS
- **Severity:** Not specified
- **Priority:** P2
- **Effort:** Not specified
- **Status:** RESOLVED (#439, PR #529)
- **Description:** See detailed analysis

---

### OPS-004: No SQLite Backup or Export/Import

- **Category:** OPS
- **Severity:** Not specified
- **Priority:** P2
- **Effort:** Not specified
- **Status:** RESOLVED (#440, PR #525)
- **Description:** See detailed analysis

---

### OPS-005: No Distributed Tracing

- **Category:** OPS
- **Severity:** Not specified
- **Priority:** P3
- **Effort:** Not specified
- **Status:** OPEN
- **Description:** See detailed analysis

---

### OPS-006: No RBAC or Multi-User Access Controls

- **Category:** OPS
- **Severity:** Not specified
- **Priority:** P2
- **Effort:** Not specified
- **Status:** RESOLVED (#442, PR #523)
- **Description:** See detailed analysis

---

### OPS-007: No Worker Auto-Recovery

- **Category:** OPS
- **Severity:** Not specified
- **Priority:** P2
- **Effort:** Not specified
- **Status:** RESOLVED (#443, PR #522)
- **Description:** See detailed analysis

---

### OPS-008: No Campaign Pause/Resume

- **Category:** OPS
- **Severity:** Not specified
- **Priority:** P2
- **Effort:** Not specified
- **Status:** RESOLVED (#444, PR #524)
- **Description:** See detailed analysis

---

### OPS-009: No Rate Limiting Per-User or Per-Campaign

- **Category:** OPS
- **Severity:** Not specified
- **Priority:** P3
- **Effort:** Not specified
- **Status:** RESOLVED (#445, PR #528)
- **Description:** See detailed analysis

---

### OPS-010: No Campaign Resource Quotas

- **Category:** OPS
- **Severity:** Not specified
- **Priority:** P3
- **Effort:** Not specified
- **Status:** OPEN
- **Description:** See detailed analysis

---

### OPS-011: No Campaign Cost Tracking

- **Category:** OPS
- **Severity:** Not specified
- **Priority:** P2
- **Effort:** Not specified
- **Status:** OPEN
- **Description:** See detailed analysis

---

### OPS-012: No Chaos/Resilience Testing

- **Category:** OPS
- **Severity:** Not specified
- **Priority:** P3
- **Effort:** Not specified
- **Status:** OPEN
- **Description:** See detailed analysis

---

## Analysis Files

- [Unified Gap List](.agents/results/gap-analysis-unified.md) - This file
- [UX Analysis](.worktrees/gap-arch-agent/GAP_ANALYSIS_ARCHITECTURE.md)
- [Architecture Analysis](.worktrees/gap-arch-agent/GAP_ANALYSIS_ARCHITECTURE.md)
- [Simulation Analysis](.worktrees/gap-sim-agent/GAP_ANALYSIS_SIMULATION.md)
- [DevEx Analysis](.worktrees/gap-devex-agent/GAP_ANALYSIS_DEVEX.md)
- [Operations Analysis](.worktrees/gap-ops-agent/GAP_ANALYSIS_OPS.md)