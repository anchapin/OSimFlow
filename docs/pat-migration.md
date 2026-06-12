# PAT (Parametric Analysis Tool) Migration Guide

> **Audience:** OpenStudio PAT desktop users who want to transition their parametric analyses to OSimFlow.

## Overview

The OpenStudio Parametric Analysis Tool (PAT) is a Java desktop GUI that creates parametric building-energy analyses. PAT saves studies as `.osa` archives (ZIP files containing `analysis.json`, seed models, measures, and weather files). OSimFlow can import these `.osa` archives and run the same analyses at scale on local machines, HPC clusters (Slurm), or cloud (AWS Batch).

This guide covers:

1. **One-time OSA export** from PAT
2. **CLI import** via `osimflow import-osa`
3. **API integration** via the PAT-compatible shim layer
4. **Endpoint mapping** for automation migration
5. **Known limitations**

---

## Step 1: Export OSA from PAT

1. Open your analysis in PAT.
2. Go to **File → Export Analysis** (or **File → Save As**).
3. Save the `.osa` file to a known location (e.g., `~/analyses/my_study.osa`).

The `.osa` file is a ZIP archive containing:
- `analysis.json` — variable definitions, distributions, algorithm settings
- Seed model (`.osm`)
- Measure scripts
- Weather files (`.epw`)

## Step 2: CLI Import

Use the `osimflow import-osa` subcommand to convert the `.osa` file to OSimFlow's `variables.yml` format:

```bash
# Convert .osa to variables.yml
osimflow import-osa \
  --osa-path ~/analyses/my_study.osa \
  --output variables.yml

# Run the campaign
osimflow run \
  --input_variables variables.yml \
  --template_sim_package ~/analyses/my_study_package \
  --n_samples 100 \
  --outdir ./results \
  --openstudio_version 3.11.0
```

The import converts PAT's variable definitions, distributions, and algorithm settings to OSimFlow's `variables.yml` schema. See [Distribution Mapping](#distribution-mapping) for details.

## Step 3: API Integration

If your tooling currently talks to PAT's REST API, OSimFlow provides a compatibility shim layer.

### Start the API server

```bash
# Install API extra
pip install osimflow[api]

# Start the server (read-write mode for analysis creation)
osimflow serve \
  --outdir ./campaigns \
  --host 0.0.0.0 \
  --port 8000 \
  --read-write
```

### Create an analysis (PAT-style)

```bash
# From an OSA file
curl -X POST http://localhost:8000/api/v1/pat/analyses \
  -H "Content-Type: application/json" \
  -d '{
    "osa_path": "/path/to/my_study.osa",
    "template_sim_package": "/path/to/template_package",
    "n_samples": 50,
    "auto_start": true
  }'

# From inline analysis JSON
curl -X POST http://localhost:8000/api/v1/pat/analyses \
  -H "Content-Type: application/json" \
  -d '{
    "analysis": {
      "problem": {
        "algorithm": {"type": "lhs", "number_of_samples": 20},
        "variables": [
          {
            "name": "insul_r",
            "variable_type": "variable",
            "distribution": {"type": "uniform", "minimum": 5.0, "maximum": 30.0}
          }
        ]
      }
    },
    "template_sim_package": "/path/to/template_package",
    "n_samples": 20,
    "auto_start": false
  }'
```

### Poll status (PAT-style)

```bash
curl http://localhost:8000/api/v1/pat/analyses/{analysis_id}/status
```

Response:

```json
{
  "analysis_id": "pat-a1b2c3d4",
  "status": "running",
  "started_at": 1718000000.0,
  "finished_at": null,
  "elapsed_s": null,
  "data_points": {
    "total": 50,
    "completed": 12,
    "failed": 1,
    "pending": 37
  }
}
```

### List data points (PAT-style)

```bash
curl http://localhost:8000/api/v1/pat/analyses/{analysis_id}/data_points
```

Response:

```json
{
  "analysis_id": "pat-a1b2c3d4",
  "total": 50,
  "data_points": [
    {
      "data_point_id": "sample_000",
      "status": "ok",
      "elapsed_s": 245.3,
      "results": {"eui_kwh_m2_yr": 120.5}
    },
    {
      "data_point_id": "sample_001",
      "status": "failed",
      "elapsed_s": 12.1,
      "error_summary": "Severe Error: ..."
    }
  ]
}
```

---

## API Endpoint Mapping

| PAT Concept | PAT Endpoint | OSimFlow Equivalent |
|---|---|---|
| Create analysis | `POST /api/v1/pat/analyses` | `POST /api/v1/campaigns` |
| Analysis status | `GET /api/v1/pat/analyses/{id}/status` | `GET /api/v1/campaigns/{id}` |
| Data points | `GET /api/v1/pat/analyses/{id}/data_points` | `GET /api/v1/campaigns/{id}/samples` |
| Cancel analysis | *(use OSimFlow native)* | `POST /api/v1/campaigns/{id}/cancel` |
| Live events | *(use OSimFlow native)* | `GET /api/v1/events` (SSE) |
| Results | *(use OSimFlow native)* | `GET /api/v1/results` |
| Failures | *(use OSimFlow native)* | `GET /api/v1/failures` |

### Terminology Mapping

| PAT Term | OSimFlow Term | Notes |
|---|---|---|
| Analysis | Campaign | A single parametric study |
| Data Point | Sample | One simulation run with specific parameter values |
| Algorithm | Algorithm | Same concept; OSimFlow supports more (PSO, NSGA-II, etc.) |
| Variable | Variable | Distribution parameters use different key names (see below) |
| Measure | Measure | OpenStudio measure reference format is identical |

---

## Distribution Mapping

| PAT Distribution | OSimFlow Distribution | Parameter Mapping |
|---|---|---|
| `uniform` | `uniform` | `minimum` → `min`, `maximum` → `max` |
| `normal` | `normal` | `mean` → `mean`, `stddev` → `sigma` |
| `lognormal` | `lognormal` | `mean` → `mean`, `stddev` → `sigma` |
| `triangular` | `triangular` | `minimum` → `min`, `maximum` → `max`, `mode` → `mode` |
| `discrete` | `discrete` | `values` → `values` |
| `categorical` | `categorical` | `values` → `values` |
| `pivot` | `categorical` (with `pivot: true`) | Mapped automatically |
| (no distribution) | `static` | `default_value` → `value` |

---

## Algorithm Mapping

| PAT Algorithm | OSimFlow Algorithm | Notes |
|---|---|---|
| `lhs` | `lhs` | Latin Hypercube Sampling (default) |
| `latin_hypercube` | `lhs` | Alias |
| `sobol` | `sobol` | Quasi-random sequence |
| `nsga_nrel` | `nsga2` | Multi-objective (requires `pip install osimflow[optimization]`) |
| `pso` | `pso` | Particle Swarm Optimization |
| `ga` / `optim` | `de` | Differential Evolution |
| `morris` | `morris` | Sensitivity analysis (requires `pip install osimflow[sensitivity]`) |
| `fast99` | `fast99` | Fourier Amplitude Sensitivity Test |
| `doe` | `lhs` | Falls back to LHS |

---

## Known Limitations

1. **PAT GUI features are not replicated.** OSimFlow is a CLI + API tool. The PAT desktop GUI (Java/Swing) is not part of OSimFlow. Use the REST API or CLI instead.

2. **No real-time PAT ↔ OSimFlow sync.** PAT and OSimFlow do not share state. Export from PAT is a one-way transfer.

3. **PAT's server mode is different.** PAT connects to the OpenStudio Server (OSS) for cloud runs. OSimFlow uses its own executor abstraction (LocalExecutor, SlurmExecutor, AWSBatchExecutor). The PAT compat layer translates REST calls, but does not emulate OSS's full API surface.

4. **Workflow measures.** PAT allows ordering measures in a workflow. OSimFlow respects the `workflow.osw` in the template simulation package, which must be set up correctly before import.

5. **Cloud run management.** PAT uses OSS for job management. OSimFlow uses AWS Batch, Slurm, or local execution directly. Cloud credentials are sourced from the IAM role (AWS) or Slurm account — not from PAT's server configuration.

6. **Optimization algorithms.** PAT supports `nsga_nrel` (NREL's NSGA-II variant). OSimFlow maps this to the standard `nsga2` algorithm from `pymoo`. Results should be equivalent but may differ in convergence details.

7. **Round-trip fidelity.** OSimFlow's `osimflow/exporters/osa.py` can produce `.osa` files, but `beta`, `gamma`, and `exponential` distributions are exported as `uniform` (lossy). Use `variables.yml` directly when exact distribution fidelity is needed.

---

## Quick Reference: PAT → OSimFlow CLI

```bash
# PAT "New Analysis" equivalent
osimflow import-osa --osa-path study.osa --output variables.yml

# PAT "Run Locally" equivalent
osimflow run \
  --executor local \
  --input_variables variables.yml \
  --template_sim_package ./template \
  --n_samples 100 \
  --outdir ./results

# PAT "Run on Cloud" equivalent (AWS Batch)
osimflow run \
  --executor aws_batch \
  --aws-batch-queue my-queue \
  --aws-batch-job-definition my-job-def \
  --input_variables variables.yml \
  --template_sim_package ./template \
  --n_samples 1000 \
  --outdir ./results

# PAT "View Results" equivalent
osimflow serve --outdir ./results --port 8000
# Then open http://localhost:8000/static/index.html
```
