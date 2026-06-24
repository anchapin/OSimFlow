# Analysis Gem (Ruby) Migration Guide

> **Audience:** OpenStudio users who have built custom Ruby scripts with the
> [openstudio-analysis-gem](https://github.com/NREL/openstudio-analysis-gem)
> and want to migrate to OSimFlow's Python library.

## Overview

The **openstudio-analysis-gem** is a Ruby gem that provides a programmatic Ruby
API for building parametric analyses — defining variables, running the OpenStudio
CLI, and collecting results. It is the core library behind OpenStudio Server
(OSS) and PAT.

**OSimFlow's Python library (`osimflow/`)** is the pure-Python functional
equivalent. It covers the same capabilities:

| Analysis Gem (Ruby) | OSimFlow (Python) |
|---|---|
| `OpenStudio::Analysis::Factory` | `Campaign` + `AlgorithmRegistry` |
| `OpenStudio::Analysis::Driver::Local` | `LocalExecutor` |
| `OpenStudio::Analysis::Driver::Slurm` | `SlurmExecutor` |
| `OpenStudio::Analysis::Driver::AwsBatch` | `AWSBatchExecutor` |
| `analysis.json` / `.osa` export | `OSAExporter` in `osimflow/exporters/osa.py` |
| `analysis.json` / `.osa` import | `osa_to_variables_yml()` in `osimflow/importers/osa.py` |
| Ruby measure scripts | Python or Ruby measure scripts in `template_sim_package` |
| PAT spreadsheet (Excel) | `bin/excel_to_variables.py` (Excel → `variables.yml`) |

OSimFlow does **not** require Ruby, R, or Rserve. All orchestration is pure
Python.

---

## Why Migrate?

1. **No Ruby dependency** — OSimFlow runs on any Python 3.12+ environment.
2. **First-class Python API** — define campaigns programmatically in Python,
   not Ruby.
3. **Native algorithm registry** — LHS, Sobol, Halton, Morris, FAST99,
   Differential Evolution, Dual Annealing, NSGA-II, PSO — all accessible via
   `AlgorithmRegistry`.
4. **Multi-executor abstraction** — swap LocalExecutor, SlurmExecutor,
   AWSBatchExecutor, or NomadExecutor without changing campaign logic.
5. **Explicit cache semantics** — `SQLiteCache` with deterministic cache-key
   hashing; warm runs are orders of magnitude faster.
6. **OSA round-trip** — import `.osa` archives from PAT or Ruby scripts via
   `osa_to_variables_yml()`, export back via `OSAExporter.pack_osa()`.

---

## Quick Start: Ruby → Python

### Old (Ruby)

```ruby
# Ruby — using the openstudio-analysis-gem
require 'openstudio-analysis'

factory = OpenStudio::Analysis::Factory.new
factory.algorithm = OpenStudio::Analysis::Algorithm::Lhs.new(n_samples: 100)
factory.variables << OpenStudio::Analysis::Variable.new(
  name: 'insul_r',
  variable_type: 'variable',
  distribution: 'uniform',
  minimum: 5.0,
  maximum: 30.0
)
factory.measure = measure

driver = OpenStudio::Analysis::Driver::Local.new(factory)
driver.run
```

### New (Python)

```python
# Python — using the OSimFlow campaign API
from pathlib import Path
from osimflow import Campaign, CampaignConfig, LocalExecutor

config = CampaignConfig(
    input_variables=Path("variables.yml"),
    template_sim_package=Path("./template_package"),
    n_samples=100,
    algorithm="lhs",
    openstudio_version="3.11.0",
    outdir=Path("./results"),
)
campaign = Campaign(executor=LocalExecutor(), config=config)
campaign.run()
```

The `variables.yml` defines the variables:

```yaml
algorithm: lhs
variables:
  - name: insul_r
    distribution: uniform
    min: 5.0
    max: 30.0
```

---

## Programmatic Campaign Creation

The OSimFlow Python library can be used directly without a `variables.yml`
file — useful when your Ruby script builds the analysis dynamically:

```python
from pathlib import Path
from osimflow import Campaign, CampaignConfig, LocalExecutor
from osimflow.algorithms import AlgorithmRegistry

# Register a custom algorithm if needed
alg = AlgorithmRegistry.get("lhs")

# Build a campaign programmatically
config = CampaignConfig(
    input_variables=Path("variables.yml"),
    template_sim_package=Path("./example_package"),
    n_samples=500,
    algorithm="lhs",
    openstudio_version="3.11.0",
    outdir=Path("./results"),
    max_workers=16,
    archive_intermediates=True,
)
campaign = Campaign(executor=LocalExecutor(), config=config)
campaign.run()
```

### Iterative Optimization (Ruby → Python)

**Ruby (RGENOUD / GA):**

```ruby
OpenStudio::Analysis::Algorithm::Rgenoud.new(
  pop_size: 100,
  max_iter: 500,
  ObjectiveFunction.new { |dp| dp['eui'] }
)
```

**Python (Differential Evolution):**

```bash
osimflow run \
  --algorithm de \
  --input_variables variables.yml \
  --template_sim_package ./template \
  --n_samples 100 \
  --outdir ./results
```

For multi-objective problems:

```bash
osimflow run \
  --algorithm nsga2 \
  --input_variables variables.yml \
  --template_sim_package ./template \
  --n_samples 100 \
  --outdir ./results
```

---

## Importing Ruby Analysis Files

### From `.osa` Archive (Ruby export)

```bash
# CLI import
osimflow import-osa \
  --osa-path study.osa \
  --output variables.yml
```

```python
# Python API import
from pathlib import Path
from osimflow.importers.osa import parse_osa, osa_to_variables_yml

osa_data = parse_osa(Path("study.osa"))
osa_to_variables_yml(osa_data, Path("variables.yml"))
```

### From `analysis.json` (Ruby export)

```bash
osimflow import-osa \
  --osa-path analysis.json \
  --output variables.yml
```

```python
from osimflow.importers.osa import parse_analysis_json, osa_to_variables_yml

osa_data = parse_analysis_json(Path("analysis.json"))
osa_to_variables_yml(osa_data, Path("variables.yml"))
```

### Distribution Mapping (Ruby → OSimFlow)

| Ruby Analysis Gem | OSimFlow `variables.yml` | Notes |
|---|---|---|
| `uniform(min, max)` | `uniform`, `min`, `max` | |
| `normal(mean, stddev)` | `normal`, `mean`, `sigma` | |
| `lognormal(mean, stddev)` | `lognormal`, `mean`, `sigma` | |
| `triangular(min, max, mode)` | `triangular`, `min`, `max`, `mode` | |
| `discrete(values)` | `discrete`, `values` | |
| `categorical(values)` | `categorical`, `values` | |
| `Beta(*args)` | `beta`, `alpha`, `beta`, `loc`, `scale` | OSA: exported as `uniform` |
| `Gamma(shape)` | `gamma`, `alpha`, `loc`, `scale` | OSA: exported as `uniform` |
| `Exponential(rate)` | `exponential`, `rate` | OSA: exported as `uniform` |
| (no distribution, fixed) | `static`, `value` | locked/fixed variable |

### Algorithm Mapping (Ruby → OSimFlow)

| Ruby Analysis Gem | OSimFlow | Registration name |
|---|---|---|
| `Lhs` | `LHSAlgorithm` | `"lhs"` |
| `Sobol` | `SobolAlgorithm` | `"sobol"` |
| `Halton` | `HaltonAlgorithm` | `"halton"` |
| `Morris` | `MorrisAlgorithm` | `"morris"` (requires `SALib`) |
| `Fast99` | `FAST99Algorithm` | `"fast99"` (requires `SALib`) |
| `Rgenoud` / `Ga` | `DifferentialEvolutionAlgorithm` | `"de"` |
| `Optim` | `DifferentialEvolutionAlgorithm` | `"de"` |
| `NsgaNrel` | `NSGA2Algorithm` | `"nsga2"` (requires `pymoo`) |
| `Pso` | `PSOAlgorithm` | `"pso"` (requires `pymoo`) |

---

## PAT Spreadsheet Import (Excel → variables.yml)

PAT and the Ruby Analysis Gem both support exporting a **spreadsheet**
(.xlsx) where each row defines a variable and columns specify the distribution
and bounds. OSimFlow provides `bin/excel_to_variables.py` to convert this
spreadsheet to `variables.yml`.

### Standard PAT Spreadsheet Layout

| Column | Description |
|---|---|
| `var_name` | Variable name (must match a measure argument or `.osm` attribute) |
| `lower_bound` | Lower bound for continuous distributions |
| `upper_bound` | Upper bound for continuous distributions |
| `distribution` | Distribution name: `uniform`, `normal`, `lognormal`, `triangular`, `discrete`, `categorical` |
| `mean` | Mean (for `normal`, `lognormal`) |
| `stddev` | Standard deviation (for `normal`, `lognormal`) |
| `mode` | Mode / peak (for `triangular`) |
| `values` | Comma-separated list of values (for `discrete`, `categorical`) |
| `display_name` | Optional human-readable label |
| `measure_argument` | Optional `MeasureName.argument_name` dotted reference |

### Usage

```bash
# Install openpyxl (required)
pip install openpyxl

# Convert PAT spreadsheet to variables.yml
python -m osimflow._work_scripts.excel_to_variables \
  --input my_pat_analysis.xlsx \
  --output variables.yml

# Or via the bin wrapper (if osimflow is installed as a package)
bin/excel_to_variables.py \
  --input my_pat_analysis.xlsx \
  --output variables.yml
```

### Programmatic API

```python
from pathlib import Path
from osimflow._work_scripts.excel_to_variables import excel_to_variables_yml

excel_to_variables_yml(
    excel_path=Path("my_pat_analysis.xlsx"),
    output_path=Path("variables.yml"),
    sheet_name="Variables",       # default: "Variables"
    algorithm="lhs",              # default: "lhs"
)
```

### Programmatic with custom column mapping

If your spreadsheet uses different column names, pass a `column_map`:

```python
from osimflow._work_scripts.excel_to_variables import excel_to_variables_yml

excel_to_variables_yml(
    excel_path=Path("custom_layout.xlsx"),
    output_path=Path("variables.yml"),
    column_map={
        "name": "var_name",
        "min": "lower_bound",
        "max": "upper_bound",
        "distribution": "distribution",
        "mean": "mean",
        "sigma": "stddev",
        "mode": "mode",
        "values": "values",
        "display_name": "display_name",
        "measure_argument": "measure_argument",
    },
)
```

---

## Exporting to Ruby / PAT Format

To export an OSimFlow campaign back to the Ruby Analysis Gem / PAT format:

```python
from pathlib import Path
from osimflow import CampaignConfig
from osimflow.exporters.osa import OSAExporter

config = CampaignConfig(...)
exporter = OSAExporter()

# Export just analysis.json
path = exporter.export(config, outdir=Path("./output"))

# Package as .osa archive (seed model + measures + weather)
osa_path = exporter.pack_osa(config, outdir=Path("./output"))
```

The `.osa` archive can be opened in PAT or imported by Ruby scripts using the
`openstudio-analysis-gem`.

---

## Complete Migration Example

### Ruby Script (before)

```ruby
require 'openstudio-analysis'

# Define algorithm
algo = OpenStudio::Analysis::Algorithm::Lhs.new(n_samples: 100)

# Define variables
variables = [
  OpenStudio::Analysis::Variable.new(
    name: 'wall_r_value',
    variable_type: 'variable',
    distribution: 'uniform',
    minimum: 20.0,
    maximum: 50.0
  ),
  OpenStudio::Analysis::Variable.new(
    name: 'window_u_value',
    variable_type: 'variable',
    distribution: 'uniform',
    minimum: 1.0,
    maximum: 5.0
  ),
]

# Build and run
factory = OpenStudio::Analysis::Factory.new(algorithm: algo, variables: variables)
driver = OpenStudio::Analysis::Driver::Local.new(factory)
driver.run
```

### Python Script (after)

```python
from pathlib import Path
from osimflow import Campaign, CampaignConfig, LocalExecutor

# variables.yml — equivalent of the Ruby variable definitions
# (generated from Ruby export via osimflow import-osa, or from Excel via excel_to_variables.py)
import yaml

variables_yml = {
    "algorithm": "lhs",
    "variables": [
        {
            "name": "wall_r_value",
            "distribution": "uniform",
            "min": 20.0,
            "max": 50.0,
        },
        {
            "name": "window_u_value",
            "distribution": "uniform",
            "min": 1.0,
            "max": 5.0,
        },
    ],
}

Path("variables.yml").write_text(yaml.dump(variables_yml))

config = CampaignConfig(
    input_variables=Path("variables.yml"),
    template_sim_package=Path("./template_package"),
    n_samples=100,
    algorithm="lhs",
    openstudio_version="3.11.0",
    outdir=Path("./results"),
)
campaign = Campaign(executor=LocalExecutor(), config=config)
campaign.run()
```

---

## Summary

| Ruby Analysis Gem task | OSimFlow equivalent |
|---|---|
| Define algorithm | `--algorithm` CLI flag or `AlgorithmRegistry` |
| Define variables | `variables.yml` or programmatic `CampaignConfig` |
| Run locally | `LocalExecutor` |
| Run on Slurm | `SlurmExecutor` |
| Run on AWS Batch | `AWSBatchExecutor` |
| Import `.osa` / `analysis.json` | `osa_to_variables_yml()` |
| Export to `.osa` / `analysis.json` | `OSAExporter` |
| Excel/PAT spreadsheet → variables | `bin/excel_to_variables.py` |
| Ruby measure scripts | Ruby or Python measures in `template_sim_package` |
| RGENOUD / GA | `--algorithm de` |
| NSGA-II | `--algorithm nsga2` (requires `pip install osimflow[optimization]`) |
| Sensitivity analysis | `--algorithm morcus` / `--algorithm fast99` (requires `pip install osimflow[sensitivity]`) |
