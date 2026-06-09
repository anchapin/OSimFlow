# OSimFlow MVP Product Requirements Document (PRD)

## 1. Introduction

### 1.1. Project Goal
The **primary goal** of the OSimFlow MVP is to **generate a comprehensive and actionable plan for developing a Minimum Viable Product (MVP) that integrates OpenStudio CLI with a scalable, Python-native orchestration framework**. This integration aims to **enable scalable batch simulations across diverse computing environments (cloud and on-premise)**, incorporating essential pre-processing (e.g., Latin Hypercube Sampling) and post-processing functionalities for OpenStudio users.

> **Note on the implementation language.** The PRD was originally written when the project intended to use Nextflow. After an architecture spike (see `.agents/results/decision-verdict.md` and `architecture/0001-workflow-framework.md`), the project switched to a custom Python driver. The product vision, target audience, and MVP scope in this PRD are unchanged; the architecture (§4) and technology stack (§4.3) sections have been updated to reflect the new foundation.

### 1.2. Vision Statement
**OSimFlow empowers OpenStudio users to effortlessly launch and manage large-scale parametric energy simulation campaigns with high reproducibility, scalability, and environmental agnosticism**, fostering a collaborative ecosystem for building performance analysis.

### 1.3. Problem Addressed
OSimFlow addresses a **critical need within the OpenStudio user base**: enabling scalable, reproducible, and efficient execution of complex simulation campaigns that are often computationally intensive. It aims to **democratize access to high-performance computing for energy modelers**, which is currently a significant barrier for many.

### 1.4. Key Differentiators & Novelty (for MVP)
OSimFlow is designed as a **foundational, community-driven open-source Python framework explicitly tailored for the building simulation domain**. Its novelty lies in its dedicated application, sophisticated integration, and domain-specific feature set. Key novel aspects for the MVP include:
*   **User-Configurable OpenStudio CLI Versioning with Automated CI/CD**: Allows users to **specify the exact OpenStudio/EnergyPlus CLI version** for their simulations via a CLI parameter (`--openstudio_version`), supported by an **automated CI/CD pipeline building and tagging dedicated container images (`ghcr.io/osimflow/openstudio_cli_image:<version>`)**. This directly addresses a critical need for precise version control and reproducibility in building energy simulations.
*   **Enhanced Failure Analysis and Domain-Specific Debugging**: Beyond generic Nextflow error handling, OSimFlow introduces a novel layer of domain-specific robustness. It will **generate a `failed_simulations.csv` that includes concise error summaries extracted from OpenStudio/EnergyPlus simulation logs** (e.g., "Severe Error: No solution found," "EnergyPlus crash") to provide actionable, context-aware debugging information.
*   **Pre-flight Parameter Applicability Validation**: Beyond basic schema validation, the MVP includes a "pre-flight check" that validates if parameters specified in `variables.yml` are **actually applicable to the target `template_sim_package`** (e.g., ensuring a measure argument exists) before simulations begin. This **prevents invalid simulations from even starting**, saving significant compute time and frustration.
*   **Enhanced Reproducibility Archiving of Critical Domain-Specific Data**: An optional `--archive_intermediates` flag will enable the publishing of **all campaign inputs** (`template_sim_package`, `variables.yml`) and **critical per-sample outputs** (modified `.osw/.osm` and `eplusout.sql` for debugging) to dedicated subdirectories. This provides **deep provenance** and explicit archiving of domain-specific critical files.
*   **Intelligent Intermediate File Optimization**: Strategies implemented within processes to **minimize the data footprint of intermediate files** in the Nextflow work directory (e.g., deleting large `eplusout.err` if successful).

## 2. Target Audience

The primary target audience for OSimFlow is **OpenStudio users**. This includes energy modelers, researchers, and practitioners involved in **large-scale parametric energy simulations, uncertainty analyses, and design optimization studies** within the building sector.

## 3. MVP Scope

The Minimum Viable Product (MVP) for OSimFlow will **focus on delivering a robust, core framework for common batch simulation needs**, while setting the stage for future growth and community contributions.

### 3.1. In-Scope Features for MVP

*   **Core OpenStudio CLI Execution**: **Seamless execution of `openstudio.cli run` for `.osw` (OpenStudio Workflow) files**.
*   **User-Configurable OpenStudio Version**: A key feature allowing users to **specify the exact OpenStudio/EnergyPlus version** for their simulations via a `--openstudio_version` CLI parameter. This will be supported by a **clear, automated CI/CD pipeline building `ghcr.io/osimflow/openstudio_cli_image:<version>` tags**.
*   **Latin Hypercube Sampling (LHS)**: **Dynamic generation of simulation input parameter sets using `scipy.stats`** for efficient exploration of the design space.
*   **Parallel and distributed execution orchestrated by a custom Python driver**, with prioritized robust support for **AWS Batch** (cloud) and **Slurm** (on-premise HPC). The driver is built on `submitit` (Slurm) and `dask-jobqueue` (HPC), with a thin Boto3-based AWS Batch adapter.
*   **Containerization**: **Full workflow reproducibility guaranteed by Docker** (for local/cloud) and **Singularity** (for HPC), leveraging the pre-built `openstudio_cli_image` and `scientific_python_image`.
*   **Flexible Model Parameterization**: **Automated modification of a base OpenStudio model/workflow**. This will support:
    *   **Direct modification of a base `.osm` model using `openstudio` Python bindings**.
    *   **Dynamic modification of measure arguments within a *template* `workflow.osw`** based on LHS sample parameters.
    *   **"Bring Your Own Script" (BYOS)**: Users can provide **custom Python scripts in `user_scripts/` to define unique model parameterization logic**, adhering to a clear input/output interface.
*   **Essential Post-Processing & Basic Visualization**:
    *   Automated extraction of Key Performance Indicators (KPIs) from **OpenStudio's SQLite and CSV outputs**, leveraging `openstudio_reporting_api`.
    *   Aggregation into structured formats (CSV, Parquet).
    *   **Generation of 1-3 static summary plots** (e.g., EUI histogram, scatter plot of key variables vs. EUI) as PNG/PDF files.
*   **User-Provided Custom Post-Processing Scripts (BYOS)**: Users can supply their own Python scripts for specialized KPI extraction via `user_scripts/`, following a defined interface specification.
*   **Robust Input Validation**: **Basic schema validation of the `variables.yml` input file** and **pre-flight checks for parameter applicability** to the target `template_sim_package` (e.g., ensuring measure arguments exist) before simulation, providing clear error messages.
*   **CLI Interface**: A **well-defined command-line interface** for specifying inputs (base model/workflow, variables), launching workflows, selecting execution profiles (`-profile`), and controlling OpenStudio versions (`--openstudio_version`).
*   **Robustness & Enhanced Failure Analysis**:
    *   Leveraging the custom driver's explicit SQLite-backed cache, resume-by-replay functionality, and structured exception handling. Cache keys are content-hashed on inputs, code, and container digest — so editing a `bin/*.py` script correctly invalidates the affected step.
    *   **A `failed_simulations.csv` will be generated**, including **concise error summaries extracted from simulation logs** (e.g., "Severe Error: No solution found," "EnergyPlus crash") to aid debugging and failure pattern analysis.
*   **Intermediate File Optimization**: Strategies implemented within work functions to **minimize the data footprint of intermediate files** in the campaign work directory (e.g., deleting large `eplusout.err` if successful).
*   **Enhanced Reproducibility Archiving**: An **optional `--archive_intermediates` flag will enable the publishing of *all campaign inputs*** (`template_sim_package`, `variables.yml`) and ***critical per-sample outputs*** (modified `.osw/.osm` and `eplusout.sql` for debugging) to dedicated subdirectories within the specified `--outdir` for **deep provenance**.
*   **Bring-Your-Own Monitoring**: The campaign writes a single per-campaign `run.json` artifact with per-step timing, per-sample status, and cache hit/miss counts. Compatible with MLflow (optional) for run comparison. Decision documented in `.agents/results/monitoring-decision.md`.

### 3.2. Out-of-Scope (Future Enhancements)

*   Custom interactive dashboards or highly sophisticated, custom-explorable data visualization tools built *directly into OSimFlow*.
*   Custom web-based UI for workflow submission or monitoring (beyond Nextflow Tower).
*   Integration with advanced optimization algorithms (beyond sampling).
*   First-class support for *all* esoteric OpenStudio CLI commands; the initial focus is on `openstudio.cli run`.
*   Deep, first-class support for Azure Batch or PBS Pro (prioritized for future phases and community contribution; new executors can be added by subclassing `BaseExecutor` in `osimflow/executors/`).
*   Direct support for generating EnergyPlus Input Data Files (`.idf`) or Weather Files (`.epw`); OSimFlow operates on `.osm` models or `.osw` workflows primarily.

## 4. Technical Architecture Overview

OSimFlow's architecture is a **custom Python driver** (`osimflow/campaign.py`) that owns a 6-step DAG of independent steps. Each step submits its work to a `BaseExecutor` (`osimflow/executors/`), which can target local threads, Slurm, AWS Batch, or any future substrate that conforms to the same `submit()` → `Handle` interface. Caching is explicit and SQLite-backed (`osimflow/cache.py`), with content-hashed keys covering inputs, code, and container digest.

> **Why a custom driver, not Nextflow?** The architecture decision record at `.agents/results/architecture/0001-workflow-framework.md` documents the rationale. The empirical spike results in `.agents/results/decision-verdict.md` confirm the chosen path satisfies the §5.2 MVP acceptance criteria. The PRD scope, target audience, and MVP deliverables are unchanged by the framework choice.

### 4.1. Core Workflow (`osimflow/campaign.py`)
The `Campaign` class owns the 6-step DAG. Its `run()` method is the public entry point:

```python
from osimflow import Campaign, CampaignConfig, LocalExecutor, load_config

cfg = load_config({...})  # from CLI args
executor = LocalExecutor(max_workers=4)
campaign = Campaign(cfg, executor)
result = campaign.run()
```

The DAG:

```
   variables.yml ──> GENERATE_LHS_SAMPLES ──┐
                                            ├──> APPLY_PARAMETERS ──> RUN_OPENSTUDIO_SIM ──┐
                                                                                            ├──> EXTRACT_KPIS ──┐
                                                                                            │                   ├──> AGGREGATE_RESULTS ──> GENERATE_BASIC_PLOTS
                                                                                            └───────────────────┘
```

The actual step bodies live in `osimflow/work.py` and call the existing `bin/*.py` stubs as subprocesses (so the BYOS contract is identical to the public function signature).

### 4.2. Key Steps (`osimflow/campaign.py` methods)

Each step is a `Campaign` method that:
1. Computes a `CacheKey` from inputs + code hash + container digest.
2. Looks up the cache. On hit, returns the cached path; on miss, submits to the executor.
3. Emits `StepTrace` hooks to the `RunTrace` for the `run.json` artifact.

*   **`step_generate_lhs`**
    *   **Description**: Reads `variables.yml`, validates its structure and types, and produces N unique parameter sets. The MVP uses an in-process Latin Hypercube implementation; the production implementation lives in `bin/generate_lhs.py` and is called as a subprocess.
    *   **Inputs**: `variables.yml` (path), `n_samples` (int).
    *   **Outputs**: `list[dict]` of `{"sample_id": str, "values": dict}`.
    *   **Container**: `scientific_python_image:latest`.

*   **`step_apply_parameters`**
    *   **Description**: For each sample, takes the `template_sim_package` and a `parameter_set_dict`. Calls `osimflow.work.default_apply_parameters` (which delegates to `bin/apply_params_to_model.py`, or a user-supplied `apply_parameters` function via `--custom_apply_script`). Performs pre-flight checks for parameter applicability.
    *   **Inputs**: `template_sim_package` (path), per-sample `parameter_set_dict`.
    *   **Outputs**: `dict[sample_id, modified_sim_package_dir]`.
    *   **Container**: `scientific_python_image:latest`.

*   **`step_run_openstudio_sim`**
    *   **Description**: The core simulation engine. For each parameterized sample, calls `osimflow.work.run_openstudio_sim` (which delegates to `bin/run_openstudio_sim.py`, or invokes `openstudio.cli run -w workflow.osw` inside the dynamically selected `openstudio_cli_image:<version>` container). Captures `eplusout.sql`, `eplusout.err`, `eplusout.log`, and `stdout/stderr` to the per-sample work directory.
    *   **Inputs**: per-sample `modified_sim_package_dir`, `openstudio_version` (str).
    *   **Outputs**: `dict[sample_id, simulation_output_dir]`.
    *   **Container**: `openstudio_cli_image:<user_specified_version>` (dynamic).

*   **`step_extract_kpis`**
    *   **Description**: For each simulated sample, calls `osimflow.work.extract_kpis` (which delegates to `bin/extract_kpis.py`, or a user-supplied `extract_kpis` function via `--custom_kpi_extractor`). Parses `eplusout.sql` and other relevant files, extracting user-defined KPIs into a JSON per sample.
    *   **Inputs**: per-sample `simulation_output_dir`.
    *   **Outputs**: `list[kpi_json_file]`.
    *   **Container**: `scientific_python_image:latest`.

*   **`step_aggregate_results`**
    *   **Description**: Collects all individual `kpi_json_files`. Calls `osimflow.work.aggregate_results` (which delegates to `bin/aggregate_results.py`) to consolidate them into a single `aggregated_results.csv` and `.parquet` file. Identifies failed samples by analyzing their `eplusout.err` and compiles `failed_simulations.csv`.
    *   **Inputs**: `list[kpi_json_file]`, `list[simulation_output_dir]`.
    *   **Outputs**: `dict{"csv": Path, "parquet": Path, "failed": Path}`.
    *   **Container**: `scientific_python_image:latest`.

*   **`step_generate_plots`**
    *   **Description**: Takes the `aggregated_results.csv` and `failed_simulations.csv`. Calls `osimflow.work.generate_plots` (which delegates to `bin/generate_plots.py`) to create 1-3 summary visualizations (EUI histogram, scatter plots of design variables vs. KPIs) as PNG or PDF.
    *   **Inputs**: `aggregated_results_csv` (path), `failed_simulations_csv` (path).
    *   **Outputs**: `list[plot_file]`.
    *   **Container**: `scientific_python_image:latest`.

### 4.3. Technology Stack
*   **Workflow Orchestration**: Custom Python driver (`osimflow/`)
*   **Executor abstraction**: `submitit` (Slurm), `boto3` (AWS Batch, future), `concurrent.futures.ThreadPoolExecutor` (local)
*   **Simulation Engine**: OpenStudio CLI, OpenStudio Python bindings
*   **Containerization**: Docker, Singularity
*   **Cloud Platforms**: AWS Batch (prioritized)
*   **On-Premise HPC**: Slurm (prioritized)
*   **Statistical Sampling**: `scipy.stats.qmc.LatinHypercube` (for LHS)
*   **Plotting**: `matplotlib/seaborn`
*   **Programming Languages**: Python 3.11+
*   **Container Registry**: `ghcr.io` (for OpenStudio CLI images)
*   **Monitoring**: Bring-your-own — per-campaign `run.json` trace + optional MLflow (see `.agents/results/monitoring-decision.md`)
*   **CI/CD**: GitHub Actions (workflow to be added post-MVP)

## 5. Development Phases & Deliverables (MVP Specific)

OSimFlow will be developed with an **agile, open-source mindset** to encourage community growth and adaptability. Development will occur on `github.com/osimflow/osimflow`.

The completion of the MVP will be marked by **Phase 3: Multi-Environment Orchestration, Versioning & Refinement**, estimated at **3-4 weeks**.

### 5.1. Phase 3 Goal
**Enable robust execution on prioritized cloud/on-premise platforms, support OpenStudio version selection, and refine overall robustness and documentation**.

### 5.2. Phase 3 Deliverables
*   **Comprehensive executor adapters** (`osimflow/executors/`): `LocalExecutor` (done), `SlurmExecutor` (real-Slurm wiring, not just debug), `AWSBatchExecutor` (boto3 wiring).
*   **Detailed deployment guides for AWS and Slurm** (setup of S3, AWS Batch CE/Queue, ECR, Slurm partition, `submitit` job logs).
*   **Automated CI/CD for at least two pre-built `openstudio_cli_image` versions** (e.g., 3.4.0 and a newer one) available via `ghcr.io`.
*   **Implementation of `--openstudio_version` CLI parameter functionality** to dynamically select the container image tag in `step_run_openstudio_sim` (already done in the foundation — verify under real container).
*   **Comprehensive end-to-end integration tests** for execution across local, docker, aws_batch, and slurm profiles, verifying output integrity.
*   **Full User Guide** (installation, basic usage, `variables.yml` spec, resource allocation guidance, `run.json` interpretation).
*   **Initial Developer Guides** (e.g., how to contribute a new step to the `Campaign` DAG, extended BYOS details, common troubleshooting/FAQ).
*   **Initial "Performance Benchmarking" workflow within CI/CD** to track execution time/resource use for a small sample against different environments.
*   **First official release: OSimFlow v0.1.0**.

## 6. Potential Challenges & Considerations

*   **Learning Curve**: The **potential steep learning curve for users unfamiliar with CLI tools** (especially those accustomed to GUIs) is a concern.
    *   **Mitigation**: Comprehensive documentation strategy (User Guide, Developer Guides) and clear interfaces with example BYOS scripts. The custom Python driver removes the Nextflow DSL layer (which is the steepest part of the original learning curve) but does not eliminate the need for a User Guide.
*   **Community Engagement Uncertainty**: The inherent **uncertainty of community engagement** for a new open-source project is acknowledged.
    *   **Mitigation**: **GitHub-centric development** with **`CONTRIBUTING.md` and `GOVERNANCE.md`** defining the community engagement model will be established early.
*   **Resource Allocation Guidance**: Strategy for defining per-step `cpus`, `memory_mb`, `time_min` and how these map to Slurm `submitit` parameters and Boto3 `containerOverrides` for AWS Batch needs to be expanded.
*   **Large Data Management**: Explicit strategies for efficiently handling very large simulation output files (e.g., hourly time-series data for many samples) across environments need to be outlined.
*   **Cloud Security Practices**: Recommend using **IAM roles for EC2 instances** in the compute environment for AWS Batch configurations, as it is a more secure approach.
*   **OpenStudio Measure Dependency Packaging**: Explicitly detail how custom OpenStudio Measures and their specific Ruby/Python dependencies will be managed within the `template_sim_package`.
*   **Cost Optimization**: Consider elaborating on other cost-optimization strategies beyond Spot Instances, such as intelligent instance type selection and automated shutdown of idle compute resources.

## 7. Success Metrics (Implicit)

The success of the OSimFlow MVP will be measured by its ability to deliver on:
*   **Correctness**: Achieving highly correct and reliable results, as emphasized throughout the reviews.
*   **Reproducibility**: Ensuring high reproducibility of simulation campaigns, particularly through explicit version control and detailed archiving.
*   **Scalability**: Demonstrating scalable batch simulations across diverse computing environments.
*   **Impact**: Maximizing impact by addressing a critical need for OpenStudio users and accelerating research and design decisions.
*   **Extensibility**: Providing a flexible and adaptable framework that supports custom user needs and encourages community contributions via BYOS and modular design.
*   **Actionability**: Being detailed enough to guide initial implementation and provide a clear path for users to launch and manage simulations effectively.