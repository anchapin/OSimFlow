# AGENTS.md

> **Audience:** AI coding assistants (Claude Code, Cursor, GitHub Copilot, Windsurf, Aider, Claude/Cursor/Cline/etc.) operating in this repository. Read this file before proposing or writing code. Human contributors are also welcome to read it — it's the canonical project-orientation document for both audiences.

---

## 1. Project summary

**OSimFlow** is a community-driven open-source **Nextflow DSL2** framework that wraps the **OpenStudio CLI** to run large-scale, reproducible, parametric building-energy simulation campaigns. It targets **OpenStudio users** — energy modelers, researchers, and design-optimization practitioners — who need to launch hundreds to thousands of `openstudio.cli run` invocations across cloud (**AWS Batch**) or on-premise HPC (**Slurm**) without writing bespoke orchestration glue for each campaign.

The full vision, scope, and technical architecture are defined in [`docs/OSimFlow.md`](docs/OSimFlow.md) (PRD). This file is the AI-assistant counterpart: it tells you the conventions, the gotchas, and the routing logic so you don't have to re-derive them from the PRD every time.

**Current status:** **Pre-MVP / skeleton.** The repository contains the PRD, project docs, and Nextflow/Python *stubs* for the six processes from PRD §4.2. Nothing is wired up yet. The first implementation commit will follow.

**MVP target:** PRD §5.2 — multi-environment orchestration, OpenStudio version selection, robustness/refinement. Estimated 3–4 weeks of focused work.

---

## 2. Stack at a glance

| Layer | Technology | Notes |
|---|---|---|
| Workflow orchestration | **Nextflow DSL2** | `nextflow.enable.dsl=2`. One process per `.nf` file under `modules/`. |
| Simulation engine | **OpenStudio CLI** + **OpenStudio Python bindings** | Invoked as `openstudio.cli run -w workflow.osw` inside the dynamic container. |
| Containerization | **Docker** (local/cloud) and **Singularity** (HPC) | Two pre-built images: `openstudio_cli_image:<version>` and `scientific_python_image`. |
| Cloud platform (prioritized) | **AWS Batch** | Profiles in `conf/aws_batch.config`. |
| On-prem HPC (prioritized) | **Slurm** | Profiles in `conf/slurm.config`. |
| Statistical sampling | **`scipy.stats`** | Latin Hypercube Sampling (LHS) of design variables. |
| Data processing | **Python 3.11+**, **`pandas`**, **`pyarrow`** (Parquet) | KPI extraction, aggregation, error parsing. |
| Plotting | **`matplotlib`** + **`seaborn`** | 1–3 static summary plots (PNG/PDF). |
| Container registry | **`ghcr.io`** | Tags: `ghcr.io/anchapin/openstudio_cli_image:<version>`, `ghcr.io/anchapin/scientific_python_image:latest`. |
| Monitoring | **Nextflow Tower** | Native compatibility; provide `-with-tower` flag. |
| CI/CD | **GitHub Actions** | See `.github/workflows/openstudio-cli-image.yml`. |

---

## 3. Directory map

| Path | Purpose |
|---|---|
| `main.nf` | Top-level workflow entry point. Orchestrates the six processes. |
| `nextflow.config` | Global Nextflow config: `nextflow.enable.dsl=2`, default params, profile registration. |
| `modules/PROCESS_GENERATE_LHS_SAMPLES.nf` | Reads `variables.yml`, calls `bin/generate_lhs.py`. |
| `modules/PROCESS_APPLY_PARAMETERS.nf` | Applies a parameter set to the `template_sim_package` (`.osm` or `.osw`). Runs pre-flight checks. |
| `modules/PROCESS_RUN_OPENSTUDIO_SIM.nf` | Runs `openstudio.cli run` in `openstudio_cli_image:<version>`. |
| `modules/PROCESS_EXTRACT_KPIS.nf` | Parses `eplusout.sql`/CSV for KPIs. |
| `modules/PROCESS_AGGREGATE_RESULTS.nf` | Collects all KPIs into one CSV/Parquet + produces `failed_simulations.csv`. |
| `modules/PROCESS_GENERATE_BASIC_PLOTS.nf` | Generates 1–3 static summary plots. |
| `conf/docker.config` | Local/CI execution profile (Docker). |
| `conf/slurm.config` | HPC execution profile (Slurm + Singularity). |
| `conf/aws_batch.config` | Cloud execution profile (AWS Batch). |
| `bin/generate_lhs.py` | LHS sampler (scipy.stats). |
| `bin/apply_params_to_model.py` | Default parameter-application logic. |
| `bin/extract_kpis.py` | Default KPI extractor. |
| `bin/aggregate_results.py` | Result aggregation + error-summary extraction. |
| `bin/generate_plots.py` | Matplotlib/seaborn plot generator. |
| `user_scripts/` | User-provided "Bring Your Own Script" (BYOS) overrides. See `user_scripts/README.md`. |
| `docs/OSimFlow.md` | The PRD — the source of truth for scope and architecture. |
| `docs/CONTRIBUTING.md` | Contributor onboarding (stub for Phase 3). |
| `docs/GOVERNANCE.md` | Community governance model (stub for Phase 3). |
| `tests/` | End-to-end integration tests (placeholder; PRD §5.2). |
| `.github/workflows/openstudio-cli-image.yml` | CI/CD stub for building `ghcr.io/anchapin/openstudio_cli_image:<version>`. |
| `LICENSE` | MIT. |
| `README.md` | One-paragraph project pitch + status. |

---

## 4. Build & run commands

> All commands assume CWD = repo root. Stubs are not yet runnable end-to-end; these examples show the *target* invocation shape so AI assistants can keep the user-facing contract stable.

```bash
# Print the campaign help
nextflow run . --help

# Local smoke run: 10 samples via Docker, default OpenStudio version
nextflow run . -profile docker \
  --input_variables variables.yml \
  --n_samples 10 \
  --template_sim_package ./example_package \
  --outdir ./results

# HPC run via Slurm + Singularity, pinned OpenStudio version
nextflow run . -profile slurm \
  --openstudio_version 3.4.0 \
  --input_variables variables.yml \
  --n_samples 500

# Cloud run on AWS Batch
nextflow run . -profile aws_batch \
  --openstudio_version 3.5.0 \
  --archive_intermediates

# Monitor with Nextflow Tower
nextflow run . -profile docker -with-tower

# User-provided custom KPI extractor
nextflow run . -profile docker \
  --custom_kpi_extractor user_scripts/my_kpis.py
```

---

## 5. Testing

> **Placeholder.** PRD §5.2 calls for "comprehensive end-to-end integration tests for execution across local, docker, aws_batch, and slurm profiles." Tests will live under `tests/` once the pipeline is implemented.

When implementing tests:
- Use small `n_samples` (1–3) and a tiny template package.
- Verify the four output artifacts: `aggregated_results.csv`, `failed_simulations.csv`, KPI JSON per sample, and 1+ plot files.
- Mock or skip the `openstudio_cli_image` build by using a pre-built tag from `ghcr.io`.
- Add a "Performance Benchmarking" smoke test (PRD §5.2) that records wall-clock + memory for a 3-sample run.

---

## 6. Code style

### Python (bin/, user_scripts/)
- **PEP 8** + **type hints** everywhere. Public functions must have full annotations.
- Use `pathlib.Path` over `os.path`. Use `logging` (not `print`).
- Exceptions: catch, log with `exc_info=True`, **re-raise**. Never swallow.
- CLI entry points: use `argparse` with mutually-exclusive groups for the BYOS override args.
- For OpenStudio Python bindings, isolate all `import openstudio` calls behind a `try/except` and provide a clear error message if the bindings aren't installed (relevant in `scientific_python_image` builds that don't include the heavy C++ stack).

### Nextflow (modules/*.nf, main.nf, nextflow.config)
- **DSL2 only.** Every process in its own file under `modules/`.
- Every process gets a `tag` so Tower logs are readable (e.g., `tag "$sample_id"`).
- Use `publishDir` with `mode: 'copy'` and an explicit `pattern` to control what lands in `--outdir`.
- Inputs that are files/directories use `path` or `tuple val(...), path(...)`; never `file` (deprecated in DSL2).
- Container directives live in `conf/*.config`, not inline in process files.
- `cache 'lenient'` is the default; opt into `cache 'strict'` only for processes with verifiable side effects.
- **Naming**: `PROCESS_<UPPER_SNAKE>` for files; `<verb>_<noun>` for process names (e.g., `RUN_OPENSTUDIO_SIM`).

### Shell / CLI
- All user-facing scripts use `set -euo pipefail`.
- Long options over short ones in documentation (e.g., `--openstudio_version` not `-o`).

---

## 7. Domain glossary

| Term | Meaning |
|---|---|
| **LHS** | Latin Hypercube Sampling — a stratified random sampling method. We use `scipy.stats.qmc.LatinHypercube`. |
| **`.osm`** | OpenStudio Model file (the parametric building energy model). |
| **`.osw`** | OpenStudio Workflow file — orchestrates which measures run on a model. |
| **`.idf`** | EnergyPlus Input Data File. **Out of scope** for OSimFlow (PRD §3.2). |
| **`.epw`** | EnergyPlus Weather file. **Out of scope** for OSimFlow. |
| **`eplusout.sql`** | SQLite output of an EnergyPlus simulation — primary source for KPI extraction. |
| **`eplusout.err`** | EnergyPlus error log. Used by `failed_simulations.csv` to extract one-line summaries (`grep -m 1 "  * Severe"`). |
| **`eplusout.log`** | EnergyPlus full log. Verbose; usually not archived unless debugging. |
| **EUI** | Energy Use Intensity (kWh/m²/yr or kBtu/ft²/yr) — the canonical headline KPI. |
| **Measure** | An OpenStudio plug-in (Ruby or Python) that modifies a model or workflow. Arguments are exposed in `.osw`. |
| **`template_sim_package`** | A user-supplied directory containing a base `.osm`/`.osw` and any required measure scripts. The campaign's starting point. |
| **`variables.yml`** | User-supplied input file declaring which parameters vary and their LHS distributions. |
| **BYOS** | "Bring Your Own Script" — user-provided Python scripts in `user_scripts/` that override default `bin/` logic. |
| **Tower** | Seqera Platform (formerly Nextflow Tower) for monitoring Nextflow runs. |
| **`openstudio_cli_image:<version>`** | The dynamic container image tag, selected via `--openstudio_version`. |

---

## 8. Common gotchas (from PRD §6)

These are *known traps* the PRD explicitly calls out. When you write code, check yourself against this list:

1. **Large `eplusout.err` files** — delete from the work directory on successful simulation (PRD §1.4 *Intelligent Intermediate File Optimization*). Don't publish to `--outdir` unless `--archive_intermediates` is set.
2. **Pre-flight parameter checks** — `PROCESS_APPLY_PARAMETERS` must verify that every LHS variable actually maps to an existing measure argument or `.osm` attribute *before* the simulation runs (PRD §1.4 *Pre-flight Parameter Applicability Validation*). Fail fast with a clear error.
3. **OpenStudio version pinning** — version lives in the **container tag**, not in `variables.yml` or env vars. The `openstudio_cli_image:<version>` is dynamically selected in `PROCESS_RUN_OPENSTUDIO_SIM` from `--openstudio_version`.
4. **Failed simulation summaries** — `failed_simulations.csv` must contain the *first* "Severe Error" line from each `eplusout.err`, not the whole file. Use `grep -m 1 "  * Severe"`.
5. **`--archive_intermediates`** — when set, publish: all campaign inputs (`template_sim_package`, `variables.yml`) **and** per-sample `.osw/.osm` + `eplusout.sql`. Don't blindly archive `eplusout.err`/`eplusout.log` — too large.
6. **AWS Batch security** — IAM roles for EC2 instances, not long-lived access keys (PRD §6 *Cloud Security Practices*).
7. **OpenStudio Measure dependencies** — custom Ruby/Python measure deps must be packaged *inside* the `template_sim_package`, not installed at runtime.
8. **Large time-series data** — hourly outputs for thousands of samples get huge fast. Default to daily/monthly aggregates in `aggregated_results.csv`; keep hourly data only in per-sample `.sql` files behind `--archive_intermediates`.

---

## 9. Task routing hints for AI agents

Use these patterns to decide where to make a change.

| If the user asks to… | Edit |
|---|---|
| Add a new KPI | `bin/extract_kpis.py` **and** `modules/PROCESS_EXTRACT_KPIS.nf` (update `publishDir` pattern if it should land in `--outdir`). |
| Add a new sampling distribution | `bin/generate_lhs.py` (extend `scipy.stats.qmc.LatinHypercube` mapping) **and** `docs/` examples in `variables.yml` spec. |
| Add a new execution platform | New `conf/<platform>.config` **and** register in `nextflow.config` under `profiles { ... }`. |
| Add a new process | New `modules/PROCESS_<NAME>.nf` **and** wire into `main.nf` channels **and** update the directory map in this file. |
| Change a default OpenStudio version | `.github/workflows/openstudio-cli-image.yml` (new build matrix entry) **and** `nextflow.config` `params.default_openstudio_version`. |
| Add a user-facing CLI flag | `main.nf` `params.<flag>` **and** the `--help` snippet in this file's §4 **and** `nextflow.config` default. |
| Change KPI output schema | `bin/extract_kpis.py` (output dict shape) **and** `bin/aggregate_results.py` (column ordering) **and** update the `variables.yml` example in `docs/`. |
| Fix a bug in parameter application | `bin/apply_params_to_model.py` first; only touch `PROCESS_APPLY_PARAMETERS.nf` if you also need different Nextflow semantics (retry, cache, publishDir). |

---

## 10. Security & data handling

- **Never commit** `.osm`, `.osw`, `.idf`, `.epw`, `eplusout.*` files. The `.gitignore` already excludes them; double-check before staging.
- For very large inputs that *must* be tracked, use **`git-lfs`** — don't bypass the gitignore.
- **AWS**: IAM roles for EC2 compute environment only. No long-lived AWS access keys in the repo or in `nextflow.config`.
- **Singularity on shared HPC**: never bind-mount secrets; pass via Nextflow `secret` directive, not environment.
- **BYOS user scripts**: when a user supplies a script, treat it as untrusted. Validate the function signature, sandbox the working directory, and apply a per-script timeout in the wrapping Python entrypoint.

---

## 11. References

- [PRD (docs/OSimFlow.md)](docs/OSimFlow.md) — sections to cite by number:
  - §1.4 — Key Differentiators
  - §3.1 — In-Scope Features
  - §4.2 — Key Modules/Processes
  - §5.2 — Phase 3 Deliverables
  - §6 — Potential Challenges & Considerations
- [CONTRIBUTING.md](docs/CONTRIBUTING.md) — *to be written*
- [GOVERNANCE.md](docs/GOVERNANCE.md) — *to be written*
- [Nextflow DSL2 docs](https://www.nextflow.io/docs/latest/dsl2.html)
- [OpenStudio CLI reference](https://openstudio.net/docs/cli/)
- [Seqera Platform / Tower](https://seqera.io/platform/)
