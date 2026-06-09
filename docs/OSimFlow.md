# OSimFlow MVP Product Requirements Document (PRD)

## 1. Introduction

### 1.1. Project Goal
The **primary goal** of the OSimFlow MVP is to **generate a comprehensive and actionable plan for developing a Minimum Viable Product (MVP) that integrates OpenStudio CLI with Nextflow**. This integration aims to **enable scalable batch simulations across diverse computing environments (cloud and on-premise)**, incorporating essential pre-processing (e.g., Latin Hypercube Sampling) and post-processing functionalities for OpenStudio users.

### 1.2. Vision Statement
**OSimFlow empowers OpenStudio users to effortlessly launch and manage large-scale parametric energy simulation campaigns with high reproducibility, scalability, and environmental agnosticism**, fostering a collaborative ecosystem for building performance analysis.

### 1.3. Problem Addressed
OSimFlow addresses a **critical need within the OpenStudio user base**: enabling scalable, reproducible, and efficient execution of complex simulation campaigns that are often computationally intensive. It aims to **democratize access to high-performance computing for energy modelers**, which is currently a significant barrier for many.

### 1.4. Key Differentiators & Novelty (for MVP)
OSimFlow is designed as a **foundational, community-driven open-source Nextflow framework explicitly tailored for the building simulation domain**. Its novelty lies in its dedicated application, sophisticated integration, and domain-specific feature set. Key novel aspects for the MVP include:
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
*   **Nextflow Orchestration**: Parallel and distributed execution orchestrated by **Nextflow (DSL2)**, with prioritized robust support for **AWS Batch** (cloud) and **Slurm** (on-premise HPC).
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
    *   Leveraging Nextflow's native caching, resume functionality, and robust error handling.
    *   **A `failed_simulations.csv` will be generated**, including **concise error summaries extracted from simulation logs** (e.g., "Severe Error: No solution found," "EnergyPlus crash") to aid debugging and failure pattern analysis.
*   **Intermediate File Optimization**: Strategies implemented within processes to **minimize the data footprint of intermediate files** in the Nextflow work directory (e.g., deleting large `eplusout.err` if successful).
*   **Enhanced Reproducibility Archiving**: An **optional `--archive_intermediates` flag will enable the publishing of *all campaign inputs*** (`template_sim_package`, `variables.yml`) and ***critical per-sample outputs*** (modified `.osw/.osm` and `eplusout.sql` for debugging) to dedicated subdirectories within the specified `--outdir` for **deep provenance**.
*   **Nextflow Tower Monitoring**: **Native compatibility with Nextflow Tower** for real-time monitoring of workflow progress, resource utilization, and advanced workflow analytics, ensuring full observability.

### 3.2. Out-of-Scope (Future Enhancements)

*   Custom interactive dashboards or highly sophisticated, custom-explorable data visualization tools built *directly into OSimFlow*.
*   Custom web-based UI for workflow submission or monitoring (beyond Nextflow Tower).
*   Integration with advanced optimization algorithms (beyond sampling).
*   First-class support for *all* esoteric OpenStudio CLI commands; the initial focus is on `openstudio.cli run`.
*   Deep, first-class support for Azure Batch or PBS Pro (prioritized for future phases and community contribution, but current Nextflow profiles might offer basic compatibility).
*   Direct support for generating EnergyPlus Input Data Files (`.idf`) or Weather Files (`.epw`); OSimFlow operates on `.osm` models or `.osw` workflows primarily.

## 4. Technical Architecture Overview

OSimFlow's architecture is based on **Nextflow DSL2's modularity**, comprising a `main.nf` workflow orchestrating a series of interconnected, containerized processes. Each process performs a distinct, isolated task, communicating data via Nextflow channels.

### 4.1. Core Workflow (`main.nf`)
The `main.nf` workflow orchestrates the overall simulation campaign.

### 4.2. Key Modules/Processes (`modules/` directory)
Each `PROCESS_` is a Nextflow module defined in its own `.nf` file, encapsulating logic and execution environment (container).

*   **`PROCESS_GENERATE_LHS_SAMPLES.nf`**
    *   **Description**: Reads `variables.yml`, validates its structure and types. Uses `bin/generate_lhs.py` (which internally uses `scipy.stats`) to produce N unique parameter sets (samples).
    *   **Inputs**: `path(variables_yml)` (`--input_variables`), `val(n_samples)` (`--n_samples`).
    *   **Outputs**: Channel emitting `tuple(sample_id, parameter_set_dict)` for each sample.
    *   **Container**: `scientific_python_image`.

*   **`PROCESS_APPLY_PARAMETERS.nf`**
    *   **Description**: Takes the `template_sim_package` and a `parameter_set_dict`. Executes `bin/apply_params_to_model.py` (or a `--custom_apply_script`) to modify the base model (`.osm`) or `workflow.osw` (e.g., adjusting measure arguments). Performs pre-flight checks for parameter applicability. Optimized output ensures only relevant files are passed downstream. Handles optional deep archiving.
    *   **Inputs**: `tuple(sample_id, parameter_set_dict)`, `path(template_sim_package)`.
    *   **Outputs**: Channel emitting `tuple(sample_id, modified_sim_package_dir)` containing the parameterized simulation inputs.
    *   **Container**: `scientific_python_image`.

*   **`PROCESS_RUN_OPENSTUDIO_SIM.nf`**
    *   **Description**: The core simulation engine. Receives the `modified_sim_package_dir`. Utilizes the dynamically selected `openstudio_cli_image:<version>` to execute `openstudio.cli run -w workflow.osw` within a container. Captures all simulation outputs, logs (`eplusout.err`, `eplusout.log`, `stdout/stderr`), and the `.sql` file.
    *   **Inputs**: `tuple(sample_id, modified_sim_package_dir)`, `val(openstudio_version)`.
    *   **Outputs**: Channel emitting `tuple(sample_id, simulation_output_dir)` containing `eplusout.sql`, `report.csv`, etc., and raw logs.
    *   **Container**: `openstudio_cli_image:<user_specified_version>`.

*   **`PROCESS_EXTRACT_KPIS.nf`**
    *   **Description**: Processes outputs from a single simulation. Executes `bin/extract_kpis.py` (or a `--custom_kpi_extractor`) to parse `eplusout.sql` and other relevant files, extracting user-defined KPIs. Results are stored in a structured JSON per sample. Leverages `openstudio_reporting_api`.
    *   **Inputs**: `tuple(sample_id, simulation_output_dir)`.
    *   **Outputs**: Channel emitting `tuple(sample_id, kpi_json_file)`.
    *   **Container**: `scientific_python_image`.

*   **`PROCESS_AGGREGATE_RESULTS.nf`**
    *   **Description**: Collects all individual `kpi_json_files`. Executes `bin/aggregate_results.py` to consolidate them into a single `aggregated_results.csv` or `.parquet` file. Critically, it also identifies failed samples by analyzing their simulation logs (passed as a collection), extracts concise error summaries (e.g., `grep -m 1 "Severe Error"`), and compiles `failed_simulations.csv`.
    *   **Inputs**: `path(kpi_json_file.collect())`, `path(simulation_output_dir.collect())`.
    *   **Outputs**: `path(aggregated_results_csv)`, `path(failed_simulations_csv)`.
    *   **Container**: `scientific_python_image`.

*   **`PROCESS_GENERATE_BASIC_PLOTS.nf`**
    *   **Description**: Takes the `aggregated_results.csv` and `failed_simulations.csv`. Executes `bin/generate_plots.py` (using `matplotlib/seaborn`) to create 1-3 summary visualizations (e.g., EUI histogram, scatter plots of design variables vs. KPIs) as PNG or PDF.
    *   **Inputs**: `path(aggregated_results_csv)`, `path(failed_simulations_csv)`.
    *   **Outputs**: `path(kpi_summary_plots_png_pdf)`.
    *   **Container**: `scientific_python_image`.

### 4.3. Technology Stack
*   **Workflow Orchestration**: Nextflow (DSL2)
*   **Simulation Engine**: OpenStudio CLI, OpenStudio Python bindings
*   **Containerization**: Docker, Singularity
*   **Cloud Platforms**: AWS Batch (prioritized)
*   **On-Premise HPC**: Slurm (prioritized)
*   **Statistical Sampling**: `scipy.stats` (for LHS)
*   **Plotting**: `matplotlib/seaborn`
*   **Programming Languages**: Python (for scripting and data processing)
*   **Container Registry**: `ghcr.io` (for OpenStudio CLI images)
*   **Monitoring**: Nextflow Tower

## 5. Development Phases & Deliverables (MVP Specific)

OSimFlow will be developed with an **agile, open-source mindset** to encourage community growth and adaptability. Development will occur on `github.com/osimflow/osimflow`.

The completion of the MVP will be marked by **Phase 3: Multi-Environment Orchestration, Versioning & Refinement**, estimated at **3-4 weeks**.

### 5.1. Phase 3 Goal
**Enable robust execution on prioritized cloud/on-premise platforms, support OpenStudio version selection, and refine overall robustness and documentation**.

### 5.2. Phase 3 Deliverables
*   **Comprehensive Nextflow configuration profiles** (`conf/aws_batch.config`, `conf/slurm.config`).
*   **Detailed deployment guides for AWS and Slurm** (setup of S3, AWS Batch CE/Queue, ECR, Slurm `nextflow.config`).
*   **Automated CI/CD for at least two pre-built `openstudio_cli_image` versions** (e.g., 3.4.0 and a newer one) available via `ghcr.io`.
*   **Implementation of `--openstudio_version` CLI parameter functionality** to dynamically select the container image tag in `PROCESS_RUN_OPENSTUDIO_SIM`.
*   **Comprehensive end-to-end integration tests** for execution across local, docker, aws_batch, and slurm profiles, verifying output integrity.
*   **Full User Guide** (installation, basic usage, `variables.yml` spec, resource allocation guidance, Nextflow Tower integration).
*   **Initial Developer Guides** (e.g., how to contribute a new Nextflow module, extended BYOS details, common troubleshooting/FAQ).
*   **Initial "Performance Benchmarking" workflow within CI/CD** to track execution time/resource use for a small sample against different environments.
*   **First official release: OSimFlow v0.1.0**.

## 6. Potential Challenges & Considerations

*   **Learning Curve**: The **potential steep learning curve for users unfamiliar with CLI/Nextflow** (especially those accustomed to GUIs) is a concern.
    *   **Mitigation**: Comprehensive documentation strategy (User Guide, Developer Guides) and native compatibility with Nextflow Tower for visual monitoring are planned. Clear interfaces and example BYOS scripts will also help.
*   **Community Engagement Uncertainty**: The inherent **uncertainty of community engagement** for a new open-source project is acknowledged.
    *   **Mitigation**: **GitHub-centric development** with **`CONTRIBUTING.md` and `GOVERNANCE.md`** defining the community engagement model will be established early.
*   **Resource Allocation Guidance**: Strategy for defining Nextflow process labels and how these will map to specific instance types and resource requests for AWS Batch and Slurm needs to be expanded.
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