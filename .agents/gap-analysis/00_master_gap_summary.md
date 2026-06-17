# OSimFlow vs openstudio-server: Master Gap Analysis

**Date:** 2026-06-16
**Purpose:** Comprehensive gap analysis to identify missing functionality for users migrating from openstudio-server PAT/GUI to OSimFlow CLI
**Reference Repositories:** NREL/openstudio-server, OpenStudio Analysis Framework (OSAF)

---

## Executive Summary

This document synthesizes findings from four specialized gap analysis agents covering:
1. **Algorithms & Sampling Methods** (`01_algorithms_gap_analysis.md`)
2. **Infrastructure & Cloud Deployment** (`02_infrastructure_gap_analysis.md`)
3. **User Experience & Workflow Features** (`03_user_experience_gap_analysis.md`)
4. **Data Handling & Outputs** (`04_data_handling_gap_analysis.md`)

### Overall Migration Parity Assessment

| Dimension | openstudio-server | OSimFlow | Parity |
|-----------|-------------------|----------|--------|
| Core Simulation Execution | ✅ | ✅ | **100%** |
| Algorithm/Sampling | ~90% | ~50% | **Major Gap** |
| Infrastructure/Cloud | ~70% | ~85% | **OSimFlow Leads** |
| User Experience/GUI | ~95% | ~40% | **Major Gap** |
| Data Handling/Outputs | ~80% | ~70% | **Partial Gap** |

---

## Critical Gaps (Must Address for Migration)

### 1. GAP-CRIT-001: No Calibration Algorithms (BM25)
- **Category:** Algorithms
- **Severity:** Critical
- **Description:** openstudio-server provides BM25 and auto-calibration algorithms that minimize error between simulated and measured energy end uses using utility bill data. This is essential for ASHRAE 14-tier compliance and model calibration workflows.
- **User Impact:** Users cannot perform auto-calibration workflows that match simulation outputs to actual utility data without custom BYOS scripts.
- **Affected Use Cases:** Model calibration to measured data, inverse modeling, ASHRAE 14-tier compliance
- **Estimated Effort:** 6-8 weeks
- **Recommended Action:** Implement `CalibrationAlgorithm` base class + BM25 + statistical matching

### 2. GAP-CRIT-002: No R-NSGA-II (Reference-based NSGA-II)
- **Category:** Algorithms
- **Severity:** Critical
- **Description:** R-NSGA-II uses reference points/parego decomposition for multi-objective optimization. It is the preferred algorithm in many PAT workflows and widely used in the research community.
- **User Impact:** Multi-objective optimization users relying on R-NSGA-II must migrate to NSGA-II or SPEA2, which lack reference point adaptation.
- **Estimated Effort:** 4-6 weeks
- **Recommended Action:** Implement via pymoo's reference direction support or custom adaptation

### 3. GAP-CRIT-003: No Uncertainty Quantification Framework
- **Category:** Algorithms
- **Severity:** Critical
- **Description:** openstudio-server provides explicit UQ including probability of failure, confidence intervals, and distribution propagation analysis via R-based Monte Carlo propagation.
- **User Impact:** Users requiring probabilistic risk analysis must implement custom solutions.
- **Estimated Effort:** 8-10 weeks
- **Recommended Action:** Add `UncertaintyQuantification` class with Monte Carlo propagation and probability-of-failure analysis

### 4. GAP-CRIT-004: No Visual GUI for Analysis Definition (PAT-style)
- **Category:** User Experience
- **Severity:** Critical
- **Description:** OSimFlow provides no graphical interface for defining parametric analyses. Users must manually edit YAML. PAT users expect a visual spreadsheet-like interface with dropdowns, distribution pickers, and measure argument browsers.
- **User Impact:** Non-CLI users (energy modelers, architects) cannot adopt OSimFlow without significant workflow restructuring.
- **Estimated Effort:** 12-16 weeks (full PAT parity)
- **Recommended Action:** Build lightweight web-based variable editor via `osimflow serve --ui`

### 5. GAP-CRIT-005: No Measure Management System
- **Category:** User Experience
- **Severity:** Critical
- **Description:** PAT provides a measure browser showing available OpenStudio measures with argument documentation. OSimFlow has no built-in measure registry — users must manually know measure names, arguments, and directory structure.
- **User Impact:** New users face significant friction understanding available measures and their arguments.
- **Estimated Effort:** 6-8 weeks
- **Recommended Action:** Build `MeasureRegistry` plugin system with BCL discovery

### 6. GAP-CRIT-006: No Web-Based ResultsViewer
- **Category:** Data Handling
- **Severity:** Critical
- **Description:** openstudio-server ships a Ruby-on-Rails web ResultsViewer for browsing simulation outputs, plotting time series, and downloading data. OSimFlow has no equivalent.
- **User Impact:** Non-CLI users cannot browse results interactively. PAT users lose primary results exploration workflow.
- **Estimated Effort:** 10-12 weeks
- **Recommended Action:** Build `OSimFlowResultsViewer` FastAPI app with Plotly.js charts

---

## Major Gaps (Should Address for Production Readiness)

### Algorithms (4 gaps)
| Gap ID | Gap Name | Estimated Effort |
|--------|----------|-----------------|
| GAP-ALGO-004 | No Fractional Factorial DOE | 2-3 weeks |
| GAP-ALGO-005 | No Parameter Study Algorithm | 2-3 weeks |
| GAP-ALGO-006 | No RGENOUD (gradient approximation) | 4-5 weeks |
| GAP-ALGO-007 | DGSM Sensitivity Not Wrapped | 2 weeks |

### Infrastructure (6 gaps)
| Gap ID | Gap Name | Estimated Effort |
|--------|----------|-----------------|
| GAP-INF-001 | No Helm Chart for K8s Deployment | 3-4 weeks |
| GAP-INF-002 | Google Cloud Batch Executor Immature | 4-5 weeks |
| GAP-INF-003 | No Native Auto-Scaling for Cloud | 5-6 weeks |
| GAP-INF-004 | Job Array Support Missing | 3-4 weeks |
| GAP-INF-005 | GPU Resource Management Incomplete | 2-3 weeks |
| GAP-INF-006 | Distributed Locking Missing | 2-3 weeks |

### User Experience (5 gaps)
| Gap ID | Gap Name | Estimated Effort |
|--------|----------|-----------------|
| GAP-UX-002 | No Real-Time Web Dashboard | 4-6 weeks |
| GAP-UX-003 | No Interactive Results Visualization | 6-8 weeks |
| GAP-UX-005 | No Visual Workflow/Measure Ordering | 3-4 weeks |
| GAP-UX-006 | No Live Progress During `osimflow run` | 1-2 weeks |
| GAP-UX-007 | Variable Definition Usability (YAML vs Excel) | 2-3 weeks |

### Data Handling (5 gaps)
| Gap ID | Gap Name | Estimated Effort |
|--------|----------|-----------------|
| GAP-DATA-001 | No Persistent Queryable Results Database | 4-6 weeks |
| GAP-DATA-003 | No Native Excel Variable Import | 2-3 weeks |
| GAP-DATA-004 | No Cross-Run Data Aggregation | 4-5 weeks |
| GAP-DATA-005 | No In-Browser Time-Series Visualization | 6-8 weeks |
| GAP-DATA-006 | No Automatic Cost KPI Calculation | 2-3 weeks |

---

## Minor Gaps (Nice to Have)

### Algorithms (1 gap)
- GAP-ALGO-008: PAWN Sensitivity Method Not Available

### Infrastructure (13 gaps)
- No SingularityExecutor (use Docker runtime detection)
- No LSF/HTCondor Executors
- No Datadog/Commercial APM Integration
- No End-to-End Distributed Tracing
- No Real-Time Log Streaming
- No WebDAV/Generic HTTP Storage
- No Tiered Storage Lifecycle

### User Experience (2 gaps)
- GAP-UX-008: No Visual Campaign History Browser
- GAP-UX-009: TUI is Display-Only, Not Interactive

### Data Handling (4 gaps)
- GAP-DATA-007: No PDF/HTML Report Generation
- GAP-DATA-008: Limited Time-Series Data Handling
- GAP-DATA-009: No R/Rserve Integration
- GAP-DATA-010: No Data Provenance Tracking
- GAP-DATA-011: DOE/Analysis Gem Compatibility Missing
- GAP-DATA-012: Error Classification Not User-Extensible

---

## Summary by Numbers

| Severity | Count |
|----------|-------|
| **Critical** | 6 |
| **Major** | 20 |
| **Minor** | 20 |
| **Total** | 46 |

---

## Priority Recommendations

### Phase 1 (Weeks 1-8): Critical Algorithm Gaps
1. Implement R-NSGA-II via pymoo
2. Add CalibrationAlgorithm base class + BM25
3. Add FractionalFactorialAlgorithm
4. Add UncertaintyQuantification framework

### Phase 2 (Weeks 9-16): UX & Data Gaps
5. Improve Excel import integration
6. Add `--watch` flag for live progress
7. Build MeasureRegistry plugin system
8. Implement ResultDatabase SQLite abstraction

### Phase 3 (Weeks 17-24): Full Feature Parity
9. Build web-based variable editor
10. Build OSimFlowResultsViewer
11. Complete Google Cloud Batch executor
12. Add Helm chart for Kubernetes

---

## References

- Original gap analyses:
  - `01_algorithms_gap_analysis.md`
  - `02_infrastructure_gap_analysis.md`
  - `03_user_experience_gap_analysis.md`
  - `04_data_handling_gap_analysis.md`
- OSimFlow PRD: `docs/OSimFlow.md`
- Migration Guide: `docs/migration-openstudio-server.md`
- Algorithm Migration: `docs/algorithm-migration.md`
