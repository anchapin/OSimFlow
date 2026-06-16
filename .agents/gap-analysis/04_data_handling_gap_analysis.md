# OSimFlow vs openstudio-server: Data Handling Gap Analysis

**Date:** June 16, 2026
**Focus Area:** Data Handling, Outputs, and Results Management
**Phase:** Data Handling & Results Management

---

## 1. Executive Summary

OSimFlow provides file-based data handling (YAML inputs, CSV/Parquet/SQLite outputs) that covers the core parametric simulation workflow. It lacks the persistent database query API, web-based ResultsViewer, native Excel import, and comprehensive data comparison tools that openstudio-server provides through MongoDB and its web UI.

**Critical Gaps (3):**
- No persistent queryable results database with API
- No web-based ResultsViewer/browser for simulation outputs
- No native Excel (.xlsx/.xls) variable import

**Major Gaps (5):**
- No cross-run data aggregation and comparison
- No in-browser visualization of time-series data
- No cost/KPI custom reporting engine
- Limited error classification granularity in failure reports
- No automatic report generation (PDF/HTML)

**Minor Gaps (4):**
- Missing R integration (only Parquet bridge exists)
- No DOE/analysis gem compatibility layer
- Time-series archival relies on `--archive_intermediates` flag
- No data versioning or provenance tracking across runs

---

## 2. Data Handling Comparison Table

| Capability | openstudio-server | OSimFlow | Status |
|---|---|---|---|
| **Input Formats** | | | |
| OSA (`.osa` ZIP / `analysis.json`) | Full parse + execute | Import via `osimflow import-osa` | ✅ Partial |
| Excel (PAT spreadsheet `.xlsx`) | Native PAT import | Via `bin/excel_to_variables.py` | ⚠️ One-way only |
| YAML (`variables.yml`) | No | Native | ✅ OSimFlow native |
| R script variables | Native `.R` analysis | No | ❌ Missing |
| **Output Formats** | | | |
| MongoDB (persistent DB) | Native | No | ❌ Missing |
| CSV | Export only | Native (`aggregated_results.csv`) | ✅ Full |
| Parquet | No | Native (`aggregated_results.parquet`) | ✅ Full |
| SQLite per-sample (`eplusout.sql`) | Yes | Yes (real OpenStudio) | ✅ Full |
| JSON (per-sample KPIs) | BSON | Native (`kpi_*.json`) | ✅ Full |
| Time-series aggregated | Via SQL | CSV + Parquet (`timeseries_aggregated`) | ✅ Full |
| **Results Storage** | | | |
| Persistent database query API | Yes (HTTP API) | No | ❌ Missing |
| S3/GCS/Azure Blob storage | No | Via `ResultStorage` plug-in | ✅ Full |
| Local filesystem | Yes | Yes | ✅ Full |
| **KPI Extraction** | | | |
| EUI (site/source) | Yes | Yes | ✅ Full |
| End-use breakdown | Yes | Yes | ✅ Full |
| Peak demand | Yes | Yes | ✅ Full |
| Unmet hours | Yes | Yes | ✅ Full |
| Custom KPIs (BYOS) | R scripts | Python `extract_kpis()` | ✅ Full |
| Cost KPIs | Yes | No (BYOS required) | ⚠️ Gap |
| Quality/threshold validation | Basic | Yes (plausibility checks) | ✅ Full |
| **Aggregation** | | | |
| Per-sample aggregate | Via MongoDB | `aggregated_results.csv/parquet` | ✅ Full |
| Cross-run comparison | MongoDB queries | No (manual) | ❌ Gap |
| Baseline comparison | Yes | Yes (`baseline:` in YAML) | ✅ Full |
| Pareto front tracking | Yes | Yes (`pareto/gen_N.json`) | ✅ Full |
| **Visualization** | | | |
| Web ResultsViewer | Yes (Ruby on Rails) | No | ❌ Missing |
| Built-in plots | Limited | PNG/PDF (`generate_plots.py`) | ✅ Basic |
| Interactive dashboards | Yes (web UI) | MLflow UI (optional) | ⚠️ Via MLflow |
| Time-series browser | Yes | No (raw SQL query only) | ❌ Gap |
| **Error Handling** | | | |
| First Severe Error extraction | Yes | Yes | ✅ Full |
| Error categorization | Basic | Yes (9 categories) | ✅ Full |
| Root cause diagnosis | Via R scripts | Yes (suggestions) | ✅ Full |
| Failure recommendations | Yes | Yes | ✅ Full |
| **Cache/Resume** | | | |
| SHA-256 content hash | No | Yes (SQLite WAL cache) | ✅ OSimFlow advantage |
| Explicit resume semantics | MongoDB state | Cache hits on re-run | ✅ OSimFlow advantage |
| **Data Export** | | | |
| OSA export (`.osa`) | N/A | Yes (`OSAExporter`) | ✅ Full |
| R DataFrame bridge | Native | Parquet only | ⚠️ Gap |
| `analysis.json` export | N/A | Yes | ✅ Full |

---

## 3. Identified Gaps

### GAP-001: No Persistent Queryable Results Database

- **Description:** openstudio-server stores all simulation results in MongoDB with a full HTTP query API. OSimFlow writes results to flat files (CSV/Parquet) in `outdir/` with no persistent database and no query API. Cross-campaign analysis requires external tools.
- **Severity:** Major
- **openstudio-server approach:** MongoDB `data_points` collection with full CRUD API, enabling real-time web dashboard queries and programmatic access via `GET /analyses/:id/data_points`.
- **Evidence:** `osimflow/storage.py` provides `ResultStorage` plug-in for cloud blob storage but no query/filter API. `osimflow/cache.py` uses SQLite only for cache metadata, not for results. `docs/gap-analysis-summary.md` issue #342: "No persistent results database with query API."
- **Consequence if wrong:** Users managing >100 campaigns must manually parse flat files or build custom ETL pipelines. No ad-hoc querying of historical runs without external tooling.

---

### GAP-002: No Web-Based ResultsViewer

- **Description:** openstudio-server ships a Ruby-on-Rails web ResultsViewer for browsing simulation outputs, plotting time series, and downloading data. OSimFlow has no equivalent — results are consumed via CLI output files, optional MLflow UI, or direct file access.
- **Severity:** Critical
- **openstudio-server approach:** `ResultsViewer` embedded in the PAT web interface, providing in-browser CSV/JSON export, time-series charting, and per-sample drill-down.
- **Evidence:** `docs/OSimFlow.md` PRD §3.1 lists "data aggregation and statistical analysis" as in-scope but the implementation is file-only. `docs/gap-analysis-summary.md` issue #337: "No web-based user interface (PAT)." The MLflow integration (`--mlflow_tracking_uri`) provides a partial alternative but requires separate infrastructure.
- **Consequence if wrong:** Non-CLI users (architects, managers) cannot browse results interactively. PAT users migrating to OSimFlow lose the primary results exploration workflow.

---

### GAP-003: No Native Excel Variable Import

- **Description:** openstudio-server/PAT accepts Excel `.xlsx` spreadsheets (PAT Analysis Spreadsheet format) as direct input. OSimFlow's `bin/excel_to_variables.py` converts Excel to `variables.yml` but only supports a subset of distribution types and produces one-way conversion only.
- **Severity:** Major
- **openstudio-server approach:** PAT reads `.xlsx` files containing variables, distributions, measure mappings, and algorithm settings directly into the analysis definition.
- **Evidence:** `osimflow/_work_scripts/excel_to_variables.py` exists and supports uniform, normal, lognormal, triangular, discrete, categorical distributions. However, `docs/migration-openstudio-server.md` §3 documents the two-step import process (PAT export → OSimFlow import) but notes that "static variables with `distribution: static`" may be dropped. `osimflow/importers/osa.py` handles OSA format but not native PAT Excel format.
- **Consequence if wrong:** Users with existing PAT Excel spreadsheets cannot import them directly. The `excel_to_variables.py` round-trip may lose distribution metadata or custom PAT fields not in the OSimFlow schema.

---

### GAP-004: No Cross-Run Data Aggregation and Comparison

- **Description:** openstudio-server stores all runs in MongoDB, enabling cross-analysis comparison via aggregation pipelines. OSimFlow treats each campaign as an independent `outdir/` with no built-in cross-run comparison. Comparing two campaigns requires external tools (pandas, DuckDB).
- **Severity:** Major
- **openstudio-server approach:** MongoDB queries joining `analyses` and `data_points` collections across analysis IDs, with web UI comparison views.
- **Evidence:** `bin/aggregate_results.py` aggregates within a single campaign (`aggregated_results.csv`). No module exists for multi-campaign comparison. `docs/gap-analysis-summary.md` issue #342 notes "no persistent results database with query API" which is the enabling infrastructure.
- **Consequence if wrong:** Teams running iterative design studies must manually align `outdir/` directories for comparison. No automatic baseline drift detection or multi-campaign statistical summaries.

---

### GAP-005: No In-Browser Time-Series Visualization

- **Description:** openstudio-server's ResultsViewer renders interactive time-series charts (hourly/daily/monthly) for any output variable across samples. OSimFlow's `generate_plots.py` produces static PNG/PDF plots only. Time-series data is queryable via SQL from `eplusout.sql` but not browsable interactively.
- **Severity:** Major
- **openstudio-server approach:** Ruby/JavaScript web interface with D3-based interactive charts, zoom, filter by sample, and CSV/JSON export.
- **Evidence:** `bin/generate_plots.py` produces static EUI histograms and Pareto plots. `docs/time-series-management.md` documents the time-series aggregation system but no interactive viewer. `docs/gap-analysis-summary.md` issue #337 (no PAT UI) is the upstream gap.
- **Consequence if wrong:** Users needing interactive time-series exploration (e.g., zone temperature profiles across 100 samples) must write custom SQL queries or use third-party tools.

---

### GAP-006: No Automatic Cost KPI Calculation

- **Description:** openstudio-server computes cost KPIs (energy cost, lifecycle cost, capital cost) natively using `analysis_gem` Ruby integrations. OSimFlow has no built-in cost calculator — cost analysis requires BYOS scripts.
- **Severity:** Minor
- **openstudio-server approach:** `OpenStudio::Analysis::CostDatabase` stores cost assumptions per measure/variable, computed automatically during aggregation.
- **Evidence:** `bin/extract_kpis.py` extracts EUI, end-uses, peak demand, unmet hours. No cost-related queries. `osimflow/_work_scripts/extract_kpis.py` does not reference cost tables in `eplusout.sql`. Users wanting cost KPIs must write custom extractors.
- **Consequence if wrong:** Teams using OSimFlow for LCC/TEP analysis must build and maintain custom BYOS extractors for cost computation.

---

### GAP-007: No PDF/HTML Report Generation

- **Description:** openstudio-server can generate PDF/HTML reports summarizing analysis results, EUI comparisons, and failed simulations. OSimFlow has no report generator — output is raw files (CSV, JSON, PNG).
- **Severity:** Minor
- **openstudio-server approach:** Server-side ERB templates rendering PDF/HTML reports on demand.
- **Evidence:** `bin/generate_plots.py` produces PNG/PDF plots but no structured report. No report generation module in `osimflow/` package or `bin/`.
- **Consequence if wrong:** Teams needing formal documentation of simulation results must build custom report generators on top of `aggregated_results.csv`.

---

### GAP-008: Limited Time-Series Data Handling

- **Description:** OSimFlow's time-series handling requires explicit `--ts_resolution` flag and `--archive_intermediates` to preserve per-sample `eplusout.sql` files. Without `--archive_intermediates`, only aggregated summaries are kept. openstudio-server preserves all time-series data in MongoDB by default.
- **Severity:** Minor
- **openstudio-server approach:** MongoDB GridFS or native document storage for time-series data, with automatic aggregation to desired resolution.
- **Evidence:** `docs/time-series-management.md` documents the time-series size formula and `--ts_resolution` flag. `osimflow/work.py` `_run_openstudio_sim_impl` creates per-sample `eplusout.sql` but `archive_intermediates` must be explicitly set. `bin/aggregate_results.py` has `TimeSeriesAggregator` class for SQL-based aggregation.
- **Consequence if wrong:** Users who forget `--archive_intermediates` lose access to per-sample hourly data. Storage estimation requires manual calculation using the formula in `docs/time-series-management.md`.

---

### GAP-009: No R/Rserve Integration (Only Parquet Bridge)

- **Description:** openstudio-server natively executes R scripts for custom analysis via Rserve. OSimFlow has no R integration — R users must use the Parquet bridge documented in `docs/r-dataframe-export.md`.
- **Severity:** Minor
- **openstudio-server approach:** `analysis_gem` Ruby gem integrates with R via Rserve, allowing arbitrary R statistical analysis on MongoDB data points.
- **Evidence:** `docs/r-dataframe-export.md` explicitly notes "OSS uses R/Rserve for statistical computing" and OSimFlow is "Python-native with no built-in R integration." The Parquet bridge is documented as the recommended workaround.
- **Consequence if wrong:** R-based research teams (common in academic energy modeling) must adopt Python BYOS patterns or use the Parquet bridge, adding friction to migration.

---

### GAP-010: No Data Provenance / Version Tracking

- **Description:** openstudio-server records run metadata (who launched, when, what algorithm/settings) in MongoDB. OSimFlow records campaign configuration in `run.json` and `CampaignConfig`, but has no concept of result immutability, version tagging, or provenance chains across campaign iterations.
- **Severity:** Minor
- **openstudio-server approach:** MongoDB document versioning with audit trail, analysis snapshot isolation.
- **Evidence:** `osimflow/monitoring.py` `RunTrace` class records `campaign_id`, `started_at`, `config` snapshot. No result versioning or provenance tracking module exists. `osimflow/registry.py` provides campaign registry but not result immutability.
- **Consequence if wrong:** Teams cannot prove that a specific result file corresponds to a specific campaign configuration. Regulatory/performance guarantee workflows may require immutable result chains.

---

### GAP-011: DOE/Analysis Gem Compatibility Layer Missing

- **Description:** openstudio-server's `openstudio-analysis-gem` provides Ruby-level integration for measure management, analysis gems, and DOE workflows. OSimFlow has no Ruby gem integration layer — measures are sourced from `template_sim_package/measures/` directories only.
- **Severity:** Minor
- **openstudio-server approach:** Ruby gem ecosystem via Bundler, `openstudio-analysis-gem` managing measure dependencies and version pinning.
- **Evidence:** `osimflow/work.py` `_validate_measure_entry_points` checks for `measure.rb` or `measure.py` in measure directories but does not integrate with Ruby gem ecosystem. `docs/packaging-measures.md` exists suggesting measure packaging guidance but no gem integration. `docs/gap-analysis-summary.md` issue #334: "No measure management system."
- **Consequence if wrong:** Teams relying on Ruby-based analysis gems (DOE workflows, custom optimization algorithms in Ruby) cannot use them with OSimFlow without rewriting as Python BYOS.

---

### GAP-012: Error Classification Has No User-Extendable Taxonomy

- **Description:** OSimFlow's `aggregate_results.py` defines 9 fixed failure categories (convergence, surface_geometry, hvac_sizing, schedule, material_construction, weather_file, memory_timeout, timestep_instability, generic_severe) with hard-coded regex patterns. Users cannot add custom categories or override patterns without modifying the source code.
- **Severity:** Minor
- **openstudio-server approach:** Error categorization via R scripts with user-defined patterns, plus domain-specific error libraries.
- **Evidence:** `osimflow/_work_scripts/aggregate_results.py` lines 35-115 define `FAILURE_PATTERNS` and `CATEGORY_SUGGESTIONS` as module-level constants with no plugin mechanism. No `--custom_failure_patterns` CLI flag exists.
- **Consequence if wrong:** Users encountering EnergyPlus errors outside the 9 predefined categories cannot extend classification without patching `aggregate_results.py`. Custom domain-specific error patterns (e.g., occupancy-related errors, specific HVAC equipment failures) require code changes.

---

## 4. Recommendations

### Immediate (Phase 1 — MVP Parity, 4-6 weeks)

1. **Add `--excel_import` CLI flag to `osimflow run`**
   - Extend `bin/excel_to_variables.py` to cover all PAT spreadsheet fields including custom metadata columns and `algorithm:` block.
   - Validate distribution round-trip fidelity against PAT export.

2. **Document Parquet bridge as full R integration story**
   - Expand `docs/r-dataframe-export.md` with correlation analysis, sensitivity analysis (SALib), and time-series forecasting examples.
   - Add `vignette()`-style notebook demonstrating R-only workflow from Parquet import to publication-quality plots.

3. **Expose failure pattern extension via config**
   - Add optional `--failure_patterns_yaml` to `osimflow run` loading custom `FAILURE_PATTERNS` from a user-supplied YAML file.
   - Validate pattern syntax (regex + category name + suggestion) at campaign startup.

### Short-term (Phase 2 — Production Readiness, 8-12 weeks)

4. **Build `ResultDatabase` abstraction over SQLite**
   - Add `osimflow/database.py` with `ResultDatabase` class that ingests `run.json` + `aggregated_results.csv` into a persistent SQLite DB with schema version.
   - Provide `db.query("SELECT * FROM results WHERE eui > ?", threshold)` CLI equivalent to `osimflow query --sql "SELECT ..."`.
   - This satisfies GAP-001 partially without full MongoDB complexity.

5. **Add `--report_format pdf,html` to `osimflow run`**
   - Integrate a Python report generator (e.g., WeasyPrint for PDF, Jinja2 for HTML) producing structured reports from `run.json` + `aggregated_results.csv`.
   - Report includes: campaign summary, EUI histogram, failed simulation table, top 10 Pareto solutions, baseline comparison.

6. **Add cost KPI library to `bin/extract_kpis.py`**
   - Add `--energy_cost_usd_per_kwh`, `--gas_cost_usd_per_therm` flags to `aggregate_results.py`.
   - Compute `total_energy_cost_usd`, `lifecycle_cost_20yr` from end-use breakdown using built-in utility rate tables.

### Medium-term (Phase 3 — Full Parity, 12-16 weeks)

7. **Build `OSimFlowResultsViewer` (web UI)**
   - FastAPI app serving results from `ResultDatabase` with Plotly.js interactive charts for time-series and scatter plots.
   - Endpoints: `GET /results/<campaign_id>`, `GET /results/<campaign_id>/timeseries?variable=...&resolution=monthly`, `GET /results/<campaign_id>/export`.
   - See `docs/gap-analysis-summary.md` issue #337.

8. **Add cross-run comparison API**
   - Extend `ResultDatabase` to ingest multiple campaigns.
   - Provide `compare(campaign_ids: list[str], kpis: list[str])` returning comparison DataFrame with statistical tests.
   - Add `osimflow compare --campaigns run1 run2 --kpis eui,peak_demand` CLI subcommand.

9. **Implement `--data_provenance` flag**
   - On campaign completion, write SHA-256 hashes of all input files + `variables.yml` into a `provenance.json` in `outdir/`.
   - Add `osimflow verify --provenance_path outdir/provenance.json` CLI command to attest that outputs match inputs.

---

## 5. Summary of Gaps by Severity

| Gap ID | Gap Name | Severity | OSimFlow File(s) | openstudio-server Equivalent |
|---|---|---|---|---|
| GAP-001 | No Persistent Queryable Results Database | Major | `osimflow/storage.py` | MongoDB `data_points` |
| GAP-002 | No Web-Based ResultsViewer | Critical | — | Ruby on Rails ResultsViewer |
| GAP-003 | No Native Excel Variable Import | Major | `bin/excel_to_variables.py` | PAT `.xlsx` import |
| GAP-004 | No Cross-Run Data Aggregation | Major | `bin/aggregate_results.py` | MongoDB aggregation pipeline |
| GAP-005 | No In-Browser Time-Series Visualization | Major | `bin/generate_plots.py` | ResultsViewer charts |
| GAP-006 | No Automatic Cost KPI Calculation | Minor | `bin/extract_kpis.py` | `OpenStudio::Analysis::CostDatabase` |
| GAP-007 | No PDF/HTML Report Generation | Minor | — | Server-side ERB reports |
| GAP-008 | Limited Time-Series Data Handling | Minor | `osimflow/work.py` | MongoDB GridFS |
| GAP-009 | No R/Rserve Integration | Minor | `docs/r-dataframe-export.md` | Rserve |
| GAP-010 | No Data Provenance Tracking | Minor | `osimflow/monitoring.py` | MongoDB audit trail |
| GAP-011 | DOE/Analysis Gem Compatibility Missing | Minor | `osimflow/work.py` | `openstudio-analysis-gem` |
| GAP-012 | Error Classification Not User-Extensible | Minor | `osimflow/_work_scripts/aggregate_results.py` | R script error categorization |

**Total: 3 Critical, 5 Major, 4 Minor across 12 identified gaps.**
