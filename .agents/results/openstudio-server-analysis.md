# OpenStudio Server — Feature Analysis & Comparison Baseline

**Date:** 2026-06-12
**Purpose:** Baseline feature list for gap analysis against OSimFlow
**Status:** Research complete

---

## 1. Executive Summary

OpenStudio Server (OSS) is NREL's web-based platform for large-scale parametric building energy simulation. It's the "official" counterpart to the OpenStudio CLI, providing a full-stack solution with a GUI (PAT), background job processing (Sidekiq/Redis), and persistent storage (MongoDB).

**Key architectural difference from OSimFlow:** OSS is a **monolithic web application** requiring MongoDB + Redis + Rails + Sidek. OSimFlow is a **lightweight CLI + library** that runs anywhere Python runs, with no server infrastructure required.

---

## 2. Core Feature Matrix

| Feature | OpenStudio Server | OSimFlow | Gap |
|---------|------------------|----------|-----|
| **Primary Interface** | Web UI (Management Console) + PAT Desktop GUI | CLI + Python library | Architecture difference (not a gap) |
| **Simulation Execution** | Docker containers via Sidek workers | OpenStudio CLI via container (Docker/Singularity) | Equivalent |
| **Version Pinning** | Via `analysis.json` workflow spec | `--openstudio_version` CLI flag | Equivalent |
| **Sampling Algorithms** | Full/Fractional Factorial, LHS, Morris, Sobol, Repeated Runs, Opt-out | LHS, Sobol, Halton, Morris, FAST99, NSGA-II, PSO, DE, DA | OSimFlow has MORE algorithms |
| **Execution Backends** | Docker (local/K8s) | Local, Slurm, AWS Batch, Azure Batch, Google Batch, Dask-JobQueue, K8s, Nomad, PBS | OSimFlow has MORE backends |
| **Result Storage** | MongoDB (data points, projects) | SQLite cache + `run.json` + optional MLflow | Different approaches |
| **Web Dashboard** | Built-in Management Console | Optional REST API (FastAPI) + TUI (rich) | OSS has built-in; OSimFlow is opt-in |
| **Report Generation** | HTML/PDF with Highcharts | PNG/PDF via matplotlib/seaborn | OSS has richer reports |
| **Data Formats** | `analysis.json` (OSA), data points in MongoDB | `variables.yml`, `run.json`, `aggregated_results.csv`, Parquet | OSS uses OSA; OSimFlow uses simpler YAML |
| **Import/Export** | PAT-native; `openstudio-analysis-gem` | OSA import/export (issue #104, #142, #134) | OSimFlow can read/write OSA |
| **BYOS (Custom Scripts)** | Limited; measure arguments only | Full Python scripts via `user_scripts/` | OSimFlow MORE extensible |
| **Cache/Resume** | Not explicit; relies on MongoDB state | SQLite-backed content-hashed cache | OSimFlow is MORE transparent |
| **Pre-flight Validation** | Basic schema validation | Parameter applicability checks before sim | OSimFlow is MORE thorough |
| **Failed Sim Tracking** | Error logs in MongoDB | `failed_simulations.csv` with error summaries | Equivalent |
| **Observability** | Built-in via Management Console | Pluggable: CloudWatch, Prometheus, OpenTelemetry | OSS built-in; OSimFlow pluggable |
| **Deployment** | Docker Compose or Kubernetes Helm | `pip install` + CLI | OSimFlow is simpler to install |

---

## 3. Detailed Feature Breakdown

### 3.1 Sampling & Design of Experiments

**OpenStudio Server (via `openstudio-analysis-gem`):**
- Full Factorial
- Fractional Factorial
- Latin Hypercube Sampling
- Morris One-at-a-Time (OAT) sensitivity
- Sobol sensitivity analysis
- Repeated Runs
- Opt-out (no sampling, run once)
- Algorithms defined in `analysis.json` `sampler` section

**OSimFlow:**
- Latin Hypercube Sampling (`scipy.stats.qmc.LatinHypercube`)
- Sobol quasi-random sequences (`scipy.stats.qmc.Sobol`)
- Halton quasi-random sequences (`scipy.stats.qmc.Halton`)
- Morris sensitivity (`SALib`)
- FAST99 sensitivity (`SALib`)
- NSGA-II multi-objective (`pymoo`)
- Particle Swarm Optimization (`pymoo`)
- Differential Evolution (`scipy.optimize`)
- Dual Annealing (`scipy.optimize`)
- Extensible via `BaseAlgorithm` ABC

**Assessment:** OSimFlow has significantly MORE sampling and optimization algorithms. OSS has basic DOE methods; OSimFlow has advanced optimization and sensitivity analysis.

### 3.2 Execution Backends

**OpenStudio Server:**
- Docker containers (local machine)
- Kubernetes via Helm charts (horizontal scaling)
- Worker nodes managed by Sidek job queue

**OSimFlow:**
- Local (ThreadPoolExecutor)
- Slurm (via `submitit`)
- AWS Batch (via `boto3`)
- Azure Batch (via Azure SDK)
- Google Cloud Batch (via Google SDK)
- Dask-JobQueue (elastic HPC)
- Kubernetes (via K8s Python client)
- HashiCorp Nomad
- PBS/Torque (via `submitit`)

**Assessment:** OSimFlow has significantly MORE execution backends. OSS is limited to Docker/K8s; OSimFlow covers all major HPC and cloud platforms.

### 3.3 Data Management & Storage

**OpenStudio Server:**
- **MongoDB**: Primary data store for projects, analyses, data points, simulations
- **Redis**: Job queue backend for Sidek
- **Data points**: Stored per-sample in MongoDB with full metadata
- **Project hierarchy**: Project → Analysis → Simulation → Data Point

**OSimFlow:**
- **SQLite cache**: Content-hashed keys for step-level resume
- **`run.json`**: Per-campaign monitoring artifact (per-step timing, per-sample status)
- **`aggregated_results.csv`**: Consolidated KPI output
- **Parquet**: Columnar storage for large result sets
- **Optional MLflow**: Run comparison and artifact tracking

**Assessment:** Different approaches. OSS uses a database-first model (MongoDB); OSimFlow uses file-first (CSV/Parquet/JSON). OSimFlow is simpler but less queryable at scale.

### 3.4 Web Interface & Visualization

**OpenStudio Server:**
- **Management Console** (web UI at `localhost:8080`):
  - Project creation and management
  - Analysis monitoring (real-time status)
  - Result visualization (Highcharts)
  - Data point browsing
  - Error log viewing
- **PAT Desktop GUI**:
  - Windows/Mac application
  - Create projects, define measures/options
  - Launch analyses on server
  - View results

**OSimFlow:**
- **CLI**: Full-featured command-line interface
- **Optional REST API** (FastAPI): Programmatic access
  - `/api/v1/campaign` — campaign management
  - `/api/v1/events` — SSE live events
  - `/api/v1/steps` — step status
- **Optional TUI** (rich): Terminal-based live dashboard
- **`run.json`**: Machine-readable monitoring artifact

**Assessment:** OSS has a richer built-in GUI experience. OSimFlow is CLI-first with optional API/TUI. Architecture difference, not a deficiency.

### 3.5 Report Generation

**OpenStudio Server:**
- HTML reports with interactive Highcharts visualizations
- PDF export
- Per-analysis and per-data-point reports
- Built-in templates for common building energy metrics

**OSimFlow:**
- Static PNG/PDF plots (matplotlib/seaborn)
- 1-3 summary plots per campaign (EUI histogram, scatter plots)
- `aggregated_results.csv` for downstream analysis
- BYOS for custom visualizations

**Assessment:** OSS has richer built-in reporting. OSimFlow focuses on data export for user-defined visualization.

### 3.6 Extensibility

**OpenStudio Server:**
- **Measures**: Ruby/Python OpenStudio measures (standard OpenStudio extension point)
- **Algorithm customization**: Limited; must modify `openstudio-analysis-gem` or add new sampler classes
- **UI customization**: Must modify Rails app
- **API**: RESTful, but documentation is limited

**OSimFlow:**
- **BYOS Scripts**: Full Python scripts for parameter application, KPI extraction, aggregation
- **Algorithm plugins**: `BaseAlgorithm` ABC with `AlgorithmRegistry`
- **Executor plugins**: `BaseExecutor` with 9 implementations
- **Observability plugins**: `ObservabilityBackend` ABC
- **Storage plugins**: `ResultStorage` ABC (Local, S3, GCS, Azure Blob)
- **CLI hooks**: `--init-script`, `--finalize-script` for pre/post campaign logic

**Assessment:** OSimFlow is MORE extensible across multiple dimensions. OSS is more "batteries included" but harder to customize.

---

## 4. Infrastructure Comparison

### 4.1 Deployment Complexity

| Aspect | OpenStudio Server | OSimFlow |
|--------|------------------|----------|
| **Dependencies** | Docker, Docker Compose, MongoDB, Redis, Rails, Sidek | Python 3.12+, pip |
| **Install** | `docker-compose up` (pulls multiple images) | `pip install osimflow` |
| **Config** | `analysis.json` (verbose JSON schema) | `variables.yml` (simple YAML) + CLI flags |
| **Storage** | MongoDB (persistent volume) | SQLite file (in output dir) |
| **Scaling** | Kubernetes Helm chart (horizontal) | Executor selection (Slurm, Batch, etc.) |
| **Networking** | Port 8080 for Management Console | None (CLI) or optional FastAPI port |

**Assessment:** OSimFlow is significantly simpler to deploy and operate. No database, no job queue, no web server required.

### 4.2 Resource Requirements

| Resource | OpenStudio Server | OSimFlow |
|----------|------------------|----------|
| **Minimum RAM** | 4GB+ (MongoDB + Rails + workers) | 512MB (Python only) |
| **Disk** | MongoDB data + Docker images | SQLite cache + output files |
| **CPU** | Worker threads for simulations | ThreadPoolExecutor or remote executors |
| **Network** | MongoDB replication (if clustered) | None (local) or cloud API access |

---

## 5. Known Pain Points (OpenStudio Server)

1. **Heavy infrastructure**: MongoDB + Redis + Rails + Sidek is complex for a simulation tool
2. **Ruby ecosystem lock-in**: Hard for Python-centric users to extend
3. **MongoDB dependency**: NoSQL adds operational complexity; no SQL queries for results
4. **No Python-native workflow**: Must use Ruby gem or PAT GUI
5. **PAT GUI is desktop-only**: Windows/Mac; no web-based project creation
6. **`analysis.json` complexity**: Verbose schema hard to author by hand
7. **Scaling requires Kubernetes**: True horizontal scaling needs K8s infrastructure
8. **Stale upstream**: Original `NREL/openstudio-server` hasn't had recent releases; `NatLabRockies` fork is newer
9. **Limited executor flexibility**: Tied to Docker-based worker system
10. **Result querying requires MongoDB**: Can't easily query results without database access

---

## 6. OSimFlow Advantages (vs. OpenStudio Server)

1. **Zero infrastructure**: No MongoDB, Redis, Rails, or Docker Compose needed
2. **Python-native**: Extensible by Python users without Ruby knowledge
3. **More execution backends**: 9+ backends vs. 2 (Docker/K8s)
4. **More algorithms**: 10+ sampling/optimization algorithms vs. 6
5. **Transparent caching**: SQLite-backed, content-hashed, resumable
6. **BYOS extensibility**: Full Python scripts for parameterization and KPI extraction
7. **CLI-first**: Scriptable, automatable, CI/CD friendly
8. **Simpler deployment**: `pip install` vs. Docker Compose + database
9. **Pluggable observability**: CloudWatch, Prometheus, OpenTelemetry
10. **OSA compatibility**: Can import/export PAT-compatible `analysis.json`

---

## 7. OSimFlow Gaps (vs. OpenStudio Server)

1. **No built-in web UI**: Management Console provides visual project management and real-time monitoring
2. **No interactive reports**: OSS has Highcharts-based interactive visualizations; OSimFlow has static plots
3. **No MongoDB-style data querying**: Can't run ad-hoc queries against simulation results
4. **Less mature**: OSS has been in production for years; OSimFlow is pre-MVP
5. **No PAT compatibility layer**: Can't directly open PAT projects (though OSA import/export helps)
6. **No built-in project management**: No concept of "projects" with multiple analyses
7. **Limited error visualization**: No built-in error log browsing UI

---

## 8. Shared Capabilities

Both platforms provide:
- OpenStudio CLI integration for running simulations
- Parametric sampling (LHS, Morris, Sobol)
- Container-based reproducibility
- Failed simulation tracking
- Result aggregation
- Multiple OpenStudio version support
- Extensibility via measures/scripts

---

## 9. Recommendations for Gap Analysis

### High-Priority Gaps to Address
1. **Interactive reporting**: Consider adding optional Plotly/Dash or Jupyter integration for richer visualization
2. **Project management**: Consider adding a lightweight "campaign registry" (OSimFlow already has `CampaignRegistry` — verify)

### Low-Priority Gaps (Architecture Differences)
1. **Web UI**: OSimFlow is CLI-first by design; the optional REST API + TUI covers programmatic and terminal users
2. **MongoDB-style querying**: Parquet + pandas provides equivalent analytical capability for most users

### OSimFlow Unique Strengths to Highlight
1. **Zero infrastructure**: Key differentiator for HPC users and small teams
2. **More execution backends**: Covers all major HPC and cloud platforms
3. **Python-native extensibility**: Lower barrier to customization
4. **Transparent, resumable caching**: Content-hashed keys ensure correctness

---

## Sources

- GitHub: `NREL/openstudio-server` (original), `NatLabRockies/OpenStudio-server` (Helm fork)
- NREL: "An open-source framework for conducting large-scale building energy model parametric analysis" (DOI: 10.1080/19401493.2020.1778788)
- PAT Interface Guide: `nrel/pat-interface`
- `openstudio-analysis-gem`: GitHub README
- OSimFlow PRD: `docs/OSimFlow.md`
