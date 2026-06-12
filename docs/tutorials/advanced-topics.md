# Advanced Topics in OSimFlow
<!-- docs-skip -->

Advanced configuration, customization, and optimization techniques for experienced OSimFlow users.

**Audience:** Users with OSimFlow experience who want to customize behavior, scale campaigns, or integrate custom workflows.

---

## Table of Contents

1. [Custom Algorithms](#1-custom-algorithms)
2. [Bring Your Own Script (BYOS)](#2-bring-your-own-script-byos)
3. [Multi-Environment Campaigns](#3-multi-environment-campaigns)
4. [Advanced Caching Strategies](#4-advanced-caching-strategies)
5. [Performance Optimization](#5-performance-optimization)
6. [Observability and Monitoring](#6-observability-and-monitoring)

---

## 1. Custom Algorithms

OSimFlow supports pluggable sampling and optimization algorithms.

### 1.1 Built-in Algorithms

| Algorithm | Type | Use Case |
|---|---|---|
| `lhs` | Sampling | Latin Hypercube Sampling (default) |
| `sobol` | Sampling | Sobol quasi-random sequence |
| `halton` | Sampling | Halton quasi-random sequence |
| `morris` | Sensitivity | Morris method sensitivity analysis |
| `fast99` | Sensitivity | FAST99 sensitivity analysis |
| `de` | Optimization | Differential Evolution |
| `da` | Optimization | Dual Annealing |
| `nsga2` | Optimization | NSGA-II multi-objective |
| `pso` | Optimization | Particle Swarm Optimization |

### 1.2 Using Non-Default Algorithms

```bash
# Sobol sampling for sensitivity analysis
osimflow run \
  --executor local \
  --algorithm sobol \
  --input_variables variables.yml \
  --template_sim_package ./template \
  --n_samples 500 \
  --outdir ./sobol_results

# Differential Evolution optimization
osimflow run \
  --executor slurm \
  --algorithm de \
  --input_variables variables.yml \
  --template_sim_package ./template \
  --n_samples 100 \
  --max-generations 30 \
  --outdir ./de_results

# NSGA-II for multi-objective optimization
osimflow run \
  --executor slurm \
  --algorithm nsga2 \
  --input_variables multi_objective_variables.yml \
  --template_sim_package ./template \
  --n_samples 100 \
  --max-generations 50 \
  --outdir ./nsga2_results
```

### 1.3 Creating a Custom Algorithm

Create a new module in `osimflow/algorithms/`:

```python
# osimflow/algorithms/my_algorithm.py
from osimflow.algorithms import BaseAlgorithm

class MyAlgorithm(BaseAlgorithm):
    name = "my_algorithm"

    def sample(self, n_samples: int) -> list[dict]:
        # Your sampling logic here
        samples = []
        for i in range(n_samples):
            samples.append({
                "param1": self._generate_param1(),
                "param2": self._generate_param2(),
            })
        return samples
```

Register the algorithm:

```python
# osimflow/algorithms/__init__.py
from osimflow.algorithms.my_algorithm import MyAlgorithm

AlgorithmRegistry.register(MyAlgorithm)
```

---

## 2. Bring Your Own Script (BYOS)

BYOS allows you to replace default work functions with custom Python scripts.

### 2.1 BYOS Trust Levels

| Level | Description | Use Case |
|---|---|---|
| `subprocess` (default) | Runs scripts in isolated child process | Production, untrusted scripts |
| `inprocess` | Loads scripts into orchestrator process | Development, trusted scripts |

```bash
# Using inprocess mode (faster but less isolated)
osimflow run \
  --executor local \
  --input_variables variables.yml \
  --template_sim_package ./template \
  --n_samples 10 \
  --outdir ./results \
  --custom_apply_script ./my_custom_apply.py \
  --byos-trust-level inprocess
```

### 2.2 Custom Parameter Application Script

Create `my_custom_apply.py`:

```python
"""
Custom parameter application for building geometry modifications.

Expected interface:
    apply_parameters(model_path: Path, parameters: dict, out_dir: Path) -> bool
"""

from pathlib import Path
import json

def apply_parameters(
    model_path: Path,
    parameters: dict,
    out_dir: Path
) -> bool:
    """
    Apply custom parameter modifications to the model.

    Args:
        model_path: Path to the input .osm file
        parameters: Dictionary of {variable_name: value} pairs
        out_dir: Directory to write modified model

    Returns:
        True if successful, False otherwise
    """
    try:
        # Load the model
        with open(model_path, 'r') as f:
            model_content = f.read()

        # Apply custom modifications
        modified_content = model_content

        for param_name, param_value in parameters.items():
            if param_name == "building_height":
                modified_content = modify_building_height(
                    modified_content, param_value
                )
            elif param_name == "window_area":
                modified_content = modify_window_area(
                    modified_content, param_value
                )
            # ... handle other parameters

        # Write modified model
        out_path = out_dir / model_path.name
        with open(out_path, 'w') as f:
            f.write(modified_content)

        return True

    except Exception as e:
        print(f"Error applying parameters: {e}")
        return False


def modify_building_height(content: str, height: float) -> str:
    """Modify building height in OSM content."""
    # Implementation specific to OSM format
    ...


def modify_window_area(content: str, area: float) -> str:
    """Modify window area in OSM content."""
    # Implementation specific to OSM format
    ...
```

### 2.3 Custom KPI Extraction Script

Create `my_kpi_extractor.py`:

```python
"""
Custom KPI extraction for domain-specific metrics.

Expected interface:
    extract_kpis(sql_path: Path, kpi_config: dict) -> dict
"""

from pathlib import Path
import sqlite3

def extract_kpis(
    sql_path: Path,
    kpi_config: dict
) -> dict:
    """
    Extract KPIs from EnergyPlus SQL output file.

    Args:
        sql_path: Path to eplusout.sql file
        kpi_config: Configuration dict with extraction settings

    Returns:
        Dictionary of {kpi_name: value} pairs
    """
    conn = sqlite3.connect(sql_path)
    cursor = conn.cursor()

    kpis = {}

    # Extract standard EUI
    cursor.execute("""
        SELECT value
        FROM ReportData
        WHERE report_name = 'AnnualBuildingUtilityPerformanceSummary'
        AND table_name = 'Site and Source Energy'
        AND row_name = 'Site Energy Intensity'
        AND column_name = 'Energy Per Conditioned Floor Area'
    """)
    result = cursor.fetchone()
    kpis['eui_kwh_m2'] = float(result[0]) if result else None

    # Extract custom KPIs
    cursor.execute("""
        SELECT value
        FROM ReportData
        WHERE ...
    """)
    # ... additional KPI extraction

    conn.close()

    return kpis
```

### 2.4 BYOS Script Validation

OSimFlow validates your script's function signature before running:

```python
import inspect
from pathlib import Path

def validate_byos_script(script_path: Path, expected_signature: inspect.Signature):
    """Validate that a BYOS script has the expected interface."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("bys_script", script_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    # Find the expected function
    func = getattr(module, expected_signature.name, None)
    if func is None:
        raise ValueError(f"Script missing {expected_signature.name} function")

    # Validate signature
    actual_sig = inspect.signature(func)
    if actual_sig != expected_signature:
        raise ValueError(
            f"Signature mismatch.\n"
            f"Expected: {expected_signature}\n"
            f"Got: {actual_sig}"
        )

    return True
```

---

## 3. Multi-Environment Campaigns

Run campaigns across different computing environments.

### 3.1 Local Executor (Development)

```bash
osimflow run \
  --executor local \
  --max-workers 4 \
  --input_variables variables.yml \
  --template_sim_package ./template \
  --n_samples 10 \
  --outdir ./local_results
```

### 3.2 Slurm (HPC Cluster)

```bash
osimflow run \
  --executor slurm \
  --slurm-real \
  --slurm-partition short \
  --slurm-account myproject \
  --input_variables variables.yml \
  --template_sim_package ./template \
  --n_samples 500 \
  --outdir ./slurm_results
```

**Advanced Slurm options:**

```bash
osimflow run \
  --executor slurm \
  --slurm-real \
  --slurm-partition gpu \
  --slurm-qos high \
  --slurm-constraint gpu \
  --slurm-gres gpu:1 \
  --input_variables variables.yml \
  --template_sim_package ./template \
  --n_samples 200 \
  --outdir ./gpu_results
```

### 3.3 AWS Batch (Cloud)

```bash
osimflow run \
  --executor aws_batch \
  --aws-batch-queue osimflow-batch-queue \
  --aws-batch-job-definition osimflow-openstudio-job-def \
  --aws-batch-max-spot-price-usd 2.50 \
  --aws-batch-fallback-to-on-demand \
  --input_variables variables.yml \
  --template_sim_package ./template \
  --n_samples 1000 \
  --outdir ./batch_results \
  --archive_intermediates
```

### 3.4 Nomad (On-Premise Container Orchestration)

```bash
osimflow run \
  --executor nomad \
  --nomad-address http://nomad-server:4646 \
  --nomad-datacentre dc1 \
  --input_variables variables.yml \
  --template_sim_package ./template \
  --n_samples 500 \
  --outdir ./nomad_results
```

---

## 4. Advanced Caching Strategies

OSimFlow caches step outputs to enable fast warm restarts.

### 4.1 Cache Invalidation Triggers

Cache is automatically invalidated when:

1. `variables.yml` content changes
2. `template_sim_package` content changes
3. Algorithm configuration changes
4. `bin/*.py` scripts are edited (code hash)
5. OpenStudio version changes

### 4.2 Manual Cache Control

```bash
# Force fresh run (ignore cache)
osimflow run \
  --executor local \
  --input_variables variables.yml \
  --template_sim_package ./template \
  --n_samples 10 \
  --outdir ./results \
  --force

# Resume from cache (default behavior)
osimflow run \
  --executor local \
  --input_variables variables.yml \
  --template_sim_package ./template \
  --n_samples 10 \
  --outdir ./results
```

### 4.3 Selective Step Rerun

Edit `run.json` to selectively rerun steps:

```json
{
  "steps": {
    "generate_lhs_samples": { "cached": true },
    "apply_parameters": { "cached": false, "reason": "modified variables" },
    "run_openstudio_sim": { "cached": true },
    "extract_kpis": { "cached": true },
    "aggregate_results": { "cached": true }
  }
}
```

---

## 5. Performance Optimization

### 5.1 Parallel Execution

```bash
# Increase worker count for local executor
osimflow run \
  --executor local \
  --max-workers 8 \
  --input_variables variables.yml \
  --template_sim_package ./template \
  --n_samples 100 \
  --outdir ./results
```

### 5.2 Batch Size Tuning

For Slurm/AWS Batch, tune job array size:

```bash
# Large batch for throughput
osimflow run \
  --executor slurm \
  --slurm-array-batch-size 100 \
  --input_variables variables.yml \
  --template_sim_package ./template \
  --n_samples 1000 \
  --outdir ./results
```

### 5.3 Memory Optimization

```bash
# Limit memory per job (AWS Batch)
osimflow run \
  --executor aws_batch \
  --aws-batch-memory-mb 4096 \
  --aws-batch-vcpus 2 \
  --input_variables variables.yml \
  --template_sim_package ./template \
  --n_samples 500 \
  --outdir ./results
```

### 5.4 Intermediate File Cleanup

Enable intelligent cleanup of large intermediate files:

```bash
osimflow run \
  --executor local \
  --input_variables variables.yml \
  --template_sim_package ./template \
  --n_samples 100 \
  --outdir ./results \
  --cleanup-intermediates
```

---

## 6. Observability and Monitoring

### 6.1 Built-in Monitoring (run.json)

OSimFlow automatically writes `run.json` with:

- Per-step timing and cache status
- Per-sample status (completed/failed)
- Resource utilization metrics

```bash
# Monitor in real-time
watch -n 2 'cat results/run.json | python -m json.tool'
```

### 6.2 CloudWatch (AWS)

```bash
osimflow run \
  --executor aws_batch \
  --observability cloudwatch \
  --cloudwatch-log-group /aws/osimflow/campaigns \
  --cloudwatch-namespace OSimFlow/Metrics \
  --input_variables variables.yml \
  --template_sim_package ./template \
  --n_samples 500 \
  --outdir ./results
```

### 6.3 Prometheus

```bash
osimflow run \
  --executor local \
  --observability prometheus \
  --prometheus-port 9090 \
  --input_variables variables.yml \
  --template_sim_package ./template \
  --n_samples 100 \
  --outdir ./results
```

### 6.4 OpenTelemetry

```bash
osimflow run \
  --executor slurm \
  --observability opentelemetry \
  --otel-endpoint http://otel-collector:4317 \
  --input_variables variables.yml \
  --template_sim_package ./template \
  --n_samples 500 \
  --outdir ./results
```

### 6.5 MLflow Integration

Track experiments with MLflow:

```bash
osimflow run \
  --executor local \
  --mlflow_tracking_uri http://localhost:5000 \
  --input_variables variables.yml \
  --template_sim_package ./template \
  --n_samples 50 \
  --outdir ./results
```

---

## See Also

- **[Your First Campaign](your-first-campaign.md)** - Beginner walkthrough
- **[Getting Started](getting-started.md)** - Installation and basics
- **[User Guide](../user-guide.md)** - Complete reference