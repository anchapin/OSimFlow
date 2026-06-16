# OSimFlow vs openstudio-server: User Experience & Workflow Features Gap Analysis

**Date:** June 16, 2026
**Phase:** 03 — User Experience & Workflow Features
**Reference:** OSimFlow PRD (§1.4, §3.1) | PAT Migration Guide | openstudio-server documentation

---

## 1. UX Comparison Table

The table below compares user-facing features across key UX dimensions.

| Feature Area | openstudio-server / PAT | OSimFlow | Gap Status |
|---|---|---|---|
| **1. Interface Type** | Electron-based PAT desktop GUI + web application | CLI (`osimflow run`) + optional Rich TUI + FastAPI server | **Major Gap** |
| **2. Analysis Definition** | Excel spreadsheet → PAT export → `.osa` ZIP | YAML file (`variables.yml`) edited manually | **Minor Gap** |
| **3. Variable Definition** | PAT spreadsheet with dropdowns, distribution pickers, visual validation | `variables.yml` with YAML distributions, manual schema | **Minor Gap** |
| **4. Analysis Format** | `.osa` ZIP archive (JSON + seed model + measures + weather) | `variables.yml` + `template_sim_package/` directory | **No Gap** |
| **5. Variable Import** | PAT export / OpenStudio-analysis-spreadsheet gem | `osimflow import-osa` CLI for `.osa` files | **No Gap** |
| **6. Measure Workflow** | PAT drag-drop measure ordering in workflow.osw; measure arguments via PAT UI | Manual `.osw` editing or external tools; `workflow.osw` must be pre-configured | **Major Gap** |
| **7. Measure Management** | Web UI measure browser + Pat落差 measures library | No built-in measure registry or discovery (BYOS override only) | **Critical Gap** |
| **8. Real-Time Monitoring** | Web dashboard with WebSocket updates (real-time per-sample status) | `run.json` polling; SSE events via `osimflow serve` (campaign must be served, not during `osimflow run`) | **Major Gap** |
| **9. Progress Visualization** | PAT progress bar + ResultsViewer with charts | Rich TUI progress display; basic 1–3 static plots (PNG/PDF) post-campaign | **Major Gap** |
| **10. Results Viewer** | ResultsViewer desktop app with interactive charts, parallel coordinates | `aggregated_results.csv` / `.parquet` + matplotlib PNG plots; MLflow UI (optional, self-hosted) | **Major Gap** |
| **11. Pre-flight Validation** | PAT validates measure arguments before run | Pre-flight parameter check validates variable names against `.osw`/`.osm` before simulation | **No Gap** |
| **12. Parameter Sweep Execution** | PAT local run or server-attached run | `osimflow run` with executor abstraction | **No Gap** |
| **13. Cloud Run Management** | PAT connects to openstudio-server (Ruby on Rails + MongoDB) | Direct executor submission (AWS Batch, Slurm, Nomad); no persistent server | **No Gap** (different model) |
| **14. BYOS / Extension** | R scripts via `analysis.json`; custom reporting | Python BYOS scripts via `--custom_apply_script` / `--custom_kpi_extractor`; function-signature validation | **Minor Gap** |
| **15. Multi-Campaign Registry** | MongoDB queries across analyses | `osimflow list` / `osimflow compare` via SQLite registry | **No Gap** |
| **16. Interactive Parameter Tweaking** | PAT allows changing variables and re-running without full re-import | Full `variables.yml` re-edit required; `--sample` flag for single-sample re-run | **Minor Gap** |
| **17. Documentation & Examples** | OpenStudio docs + PAT tutorials | `docs/user-guide.md`, `docs/variables-schema.md`, `docs/pat-migration.md`, `docs/migration-openstudio-server.md` | **No Gap** |

---

## 2. Identified Gaps

### Gap UX-001: No Visual GUI for Analysis Definition
- **Gap Name:** Absence of PAT-Style Desktop GUI
- **Description:** OSimFlow provides no graphical interface for defining parametric analyses. Users must manually edit `variables.yml` in a text editor. PAT users expect a visual spreadsheet-like interface with dropdowns, distribution pickers, and measure argument browsers.
- **Severity:** Critical
- **openstudio-server Approach:** PAT (Electron-based desktop app) provides visual analysis definition, measure ordering, and one-click run. The OpenStudio-analysis-spreadsheet gem enables Excel-based variable definition.
- **Evidence:** OSimFlow PRD §3.2 explicitly lists web-based UI as out-of-scope; `docs/pat-migration.md` states "PAT GUI features are not replicated. OSimFlow is a CLI + API tool."
- **If Wrong:** Non-CLI users (energy modelers, architects) cannot adopt OSimFlow without significant workflow restructuring. PAT users face a steep learning curve switching to YAML editing.

---

### Gap UX-002: No Real-Time Web Dashboard
- **Gap Name:** Real-Time Progress Dashboard
- **Description:** openstudio-server's web dashboard provides live per-sample status via WebSocket as simulations run. OSimFlow's real-time capability (SSE) only works when serving via `osimflow serve` — it is not available during a live `osimflow run` invocation on a remote executor.
- **Severity:** Major
- **openstudio-server Approach:** Persistent web application at port 8080 with real-time WebSocket updates for every data point transition.
- **Evidence:** `docs/migration-openstudio-server.md` §7 states "Real-time per-sample status: Yes (WebSocket)" for OSS; OSimFlow has "No (updates on step completion)" via MLflow.
- **If Wrong:** Users running long campaigns on HPC/cloud must poll `run.json` or connect to MLflow to observe progress. Remote monitoring is less seamless than the OSS dashboard.

---

### Gap UX-003: No Interactive Results Visualization
- **Gap Name:** ResultsViewer / Interactive Charts
- **Description:** PAT's ResultsViewer provides interactive charting (histograms, scatter plots, parallel coordinates, sensitivity charts). OSimFlow produces 1–3 static summary plots (EUI histogram, scatter) as PNG/PDF and relies on MLflow for a web-based comparison UI, but this is opt-in and requires separate setup.
- **Severity:** Major
- **openstudio-server Approach:** ResultsViewer desktop app (or web-based charts) for interactive exploration of results after completion.
- **Evidence:** OSimFlow PRD §3.1 lists "Generation of 1–3 static summary plots" as in-scope; interactive dashboards are out-of-scope per §3.2.
- **If Wrong:** Analysts expecting PAT-style interactive result exploration must use external tools (pandas, matplotlib, Plotly) or set up MLflow manually.

---

### Gap UX-004: No Measure Management System
- **Gap Name:** Measure Discovery and Management
- **Description:** PAT provides a measure browser showing available OpenStudio measures with argument documentation. OSimFlow has no built-in measure registry — users must manually know measure names, arguments, and directory structure.
- **Severity:** Critical (per gap-analysis-summary.md GAP-EXT-012)
- **openstudio-server Approach:** PAT measure browser integrated with OpenStudio measure libraries (BCL — Building Component Library).
- **Evidence:** `docs/migration-openstudio-server.md` §6 discusses measure paths but no discovery mechanism exists in OSimFlow; AGENTS.md §0.3 references "No Models in ORM sense" but measure management is a separate gap.
- **If Wrong:** Users must manually reference OpenStudio documentation or BCL to understand available measures and their arguments, increasing friction for new users.

---

### Gap UX-005: No Visual Workflow/Measure Ordering
- **Gap Name:** Measure Ordering and Workflow Visualization
- **Description:** PAT shows the OpenStudio workflow as an ordered list of measures with drag-drop reordering. OSimFlow respects the `workflow.osw` order but provides no UI to visualize or modify the workflow — users must edit the `.osw` JSON directly.
- **Severity:** Major
- **openstudio-server Approach:** PAT UI shows workflow.osw steps with argument inspection and reordering.
- **Evidence:** `docs/pat-migration.md` Known Limitations §4: "Workflow measures. PAT allows ordering measures in a workflow. OSimFlow respects the `workflow.osw` in the template simulation package, which must be set up correctly before import."
- **If Wrong:** Users accustomed to PAT's drag-drop workflow editor must manually maintain `workflow.osw` JSON, increasing the risk of misconfiguration.

---

### Gap UX-006: Real-Time Progress During `osimflow run`
- **Gap Name:** Live Progress Without Separate Server
- **Description:** OSimFlow's SSE live events (`GET /api/v1/events`) require running `osimflow serve` as a separate process. During `osimflow run` on Slurm or AWS Batch, there is no built-in mechanism for real-time progress streaming to the user's terminal without polling `run.json`.
- **Severity:** Major
- **openstudio-server Approach:** Web dashboard continuously streams status during the analysis run.
- **Evidence:** `docs/api.md` §296–330: SSE events require `--read-write` serve mode; AGENTS.md CLI flags show no `--watch` or `--live` flag for `osimflow run` itself.
- **If Wrong:** Users running campaigns on remote HPC/cloud cannot see live progress in their terminal without either polling files or maintaining a separate serve process alongside their run.

---

### Gap UX-007: Variable Definition Usability
- **Gap Name:** Spreadsheet-Based Variable Definition
- **Description:** PAT uses an Excel spreadsheet interface where users fill in variable names, distributions, and ranges with dropdown validation. OSimFlow requires hand-editing YAML with exact schema knowledge. No autocomplete, no dropdowns, no visual validation.
- **Severity:** Major
- **openstudio-server Approach:** OpenStudio-analysis-spreadsheet gem produces `analysis.json` from Excel; PAT imports the spreadsheet directly.
- **Evidence:** `docs/variables-schema.md` shows full YAML schema but no UI tooling; `bin/excel_to_variables.py` exists (AGENTS.md) for Excel-to-variables conversion but is not integrated into the main CLI workflow.
- **If Wrong:** Users with large variable sets (20+ parameters) face error-prone YAML editing with no IDE-like autocomplete or validation feedback until pre-flight validation runs.

---

### Gap UX-008: Campaign History and Comparison UI
- **Gap Name:** Visual Campaign History Browser
- **Description:** openstudio-server's MongoDB-backed web UI shows analysis history with one-click comparison. OSimFlow provides `osimflow list` (CLI table) and `osimflow compare` (CLI output), plus an API endpoint for comparison, but no graphical browser for visual campaign exploration.
- **Severity:** Minor
- **openstudio-server Approach:** Web UI with analysis list, status indicators, and click-to-compare.
- **Evidence:** `docs/api.md` §203–264: `POST /api/v1/campaigns/compare` exists; `osimflow list` / `osimflow show` / `osimflow compare` CLI subcommands exist (AGENTS.md); no web UI for browsing.
- **If Wrong:** Teams managing many campaigns must rely on CLI commands or API calls rather than a visual dashboard for campaign history exploration.

---

### Gap UX-009: TUI is Display-Only, Not Interactive
- **Gap Name:** Rich TUI Interactive Features
- **Description:** OSimFlow's optional Rich TUI (`osimflow run` with `--no-tui` override) provides colored output and progress bars but is display-only. It cannot accept user input during the run (e.g., to cancel, adjust parameters, or trigger new samples).
- **Severity:** Minor
- **openstudio-server Approach:** Web dashboard accepts user input during run (stop, modify analysis).
- **Evidence:** AGENTS.md §4: "Optional `rich`-based terminal UI for live campaign tracking (issue #197). Auto-detected when `rich` is installed and stdout is a TTY." No interactive input support described.
- **If Wrong:** Users expecting to interact with their campaign mid-run (pause, adjust, reprioritize) cannot do so via the TUI. Must use the separate REST API server for mutations.

---

## 3. Recommendations

### Phase 1 (MVP Parity — 4–6 weeks)

1. **UX-007 (Major):** Integrate `bin/excel_to_variables.py` more tightly into the CLI via `osimflow run --import-spreadsheet`. Add basic YAML validation with column-name suggestions ("Did you mean...?"). This directly reduces the hand-editing burden.

2. **UX-006 (Major):** Add `--watch` flag to `osimflow run` that polls `run.json` and streams human-readable progress to stdout every N seconds (e.g., `watch -n 10 osimflow status ./results`). Document the pattern explicitly in user-guide.md §6.

3. **UX-002 (Major):** Improve SSE usability by documenting how to run `osimflow serve` alongside `osimflow run` in a terminal multiplexer (tmux/screen) for live monitoring without a browser. Provide a reference `tmux` layout script in `docs/`.

### Phase 2 (Production Readiness — 8–12 weeks)

4. **UX-001 (Critical):** Investigate building a lightweight web-based analysis definition UI using the existing REST API as backend. This would not replicate full PAT but would provide a browser-based variable editor with form validation. Consider leveraging the existing FastAPI server + a simple HTML/JS frontend served via `osimflow serve --ui`.

5. **UX-004 (Critical):** Build a `MeasureRegistry` plugin system (issue #334 in gap-analysis-summary.md) that discovers measures from the template package and provides an API endpoint for measure + argument listing.

6. **UX-005 (Major):** Add `osimflow workflow inspect` and `osimflow workflow reorder` subcommands that read `workflow.osw` and provide a structured CLI view of measure ordering, with JSON-patch-based reordering.

### Phase 3 (Full Parity — 12–16 weeks)

7. **UX-001 + UX-003 (Critical):** Build or integrate a ResultsViewer-equivalent web component. This could be served via `osimflow serve --dashboard` using an existing JavaScript charting library (Plotly.js, Vega-Lite) consuming the REST API. This simultaneously addresses UX-003 (interactive results) and provides a visual browsing layer.

8. **UX-008 (Minor):** Extend the REST API campaign list endpoint with filtering (by date, status, executor) and pagination, then build a simple HTML campaign browser served statically via `osimflow serve`.

---

## 4. Summary

OSimFlow's UX model is fundamentally CLI-first and file-based, optimized for automation and reproducibility rather than interactive GUI exploration. This is a deliberate architectural choice (§3.2: "It is not a building model editor" and "It does not provide a GUI"), not a bug. openstudio-server's UX is web-first and interactive.

The primary user-facing gaps are:

| Severity | Count | Key Gaps |
|---|---|---|
| **Critical** | 2 | No PAT-style GUI (UX-001); No measure management system (UX-004) |
| **Major** | 5 | No real-time dashboard (UX-002); No interactive results viz (UX-003); No visual workflow ordering (UX-005); No live progress during run (UX-006); Poor variable definition usability (UX-007) |
| **Minor** | 2 | No visual campaign history browser (UX-008); TUI is display-only (UX-009) |

OSimFlow's documented migration path (PAT → `osimflow import-osa` → `osimflow run`) and CLI-level OSA compatibility are solid. The UX gaps are primarily about the interactive/visual layer that PAT users rely on for day-to-day parametric study setup and result exploration.

**Estimated effort to address Critical UX gaps:** 12–16 weeks (web UI + measure registry)