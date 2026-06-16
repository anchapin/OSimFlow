# Measure Runner Helper Guide

> A practical guide to running OpenStudio measures programmatically and writing
> custom BYOS KPI extractors for OSimFlow campaigns.

## Table of Contents

- [1. Overview](#1-overview)
- [2. MeasureRunner Helper Pattern](#2-measurerunner-helper-pattern)
- [3. BYOS Script Structure](#3-byos-script-structure)
- [4. Measure Discovery with MeasureRegistry](#4-measure-discovery-with-measureregistry)
- [5. Running a Single Measure](#5-running-a-single-measure)
- [6. Examples](#6-examples)

---

## 1. Overview

OSimFlow uses a **BYOS (Bring Your Own Script)** pattern to let users supply
custom Python scripts that override default `bin/` logic.  The contract is
straightforward:

- Scripts live in `user_scripts/` and are selected via CLI flags such as
  `--custom_apply_script` or `--custom_kpi_extractor`.
- OSimFlow discovers the function by **name** (`apply_parameters` or
  `extract_kpis`) and validates the signature with `inspect.signature` before
  calling it.
- Scripts run in an **isolated subprocess** by default (`--byos-trust-level
  subprocess`), so they cannot access the orchestrator's memory or
  credentials.

The two primary BYOS use-cases this guide addresses are:

1. **Custom KPI extraction** — replacing or extending the default
   `bin/extract_kpis.py` logic to pull bespoke metrics from `eplusout.sql`.
2. **Measure discovery and validation** — using `MeasureRegistry` to index
   measures and validate variable mappings before a campaign runs.

For the full BYOS contract reference, see [`user_scripts/README.md`](../user_scripts/README.md).

---

## 2. MeasureRunner Helper Pattern

When you need to run a measure outside the OSimFlow campaign DAG — for
debugging, single-model studies, or pre-flight validation — the pattern is to
wrap the OpenStudio CLI yourself.  OSimFlow does not ship a one-line "run a
measure" helper; instead it exposes the primitives (`MeasureRegistry`,
`openstudio.cli`) so you can assemble the behaviour you need.

### 2.1 Calling `openstudio.cli run` Programmatically

```python
import subprocess
from pathlib import Path

def run_measure(
    model_path: Path,
    measure_path: Path,
    arguments: dict[str, str | float | bool],
    workflow_osw: Path,
    openstudio_version: str = "3.11.0",
) -> subprocess.CompletedProcess:
    """Run a single measure on a model via the OpenStudio CLI.

    Args:
        model_path:     Path to the ``.osm`` model file.
        measure_path:   Path to the measure directory.
        arguments:      Dict of argument name -> value.
        workflow_osw:   Path to the ``workflow.osw`` that includes the measure.
        openstudio_version: OpenStudio container tag (default: ``"3.11.0"``).

    Returns:
        CompletedProcess with returncode, stdout, stderr.
    """
    env = {
        **os.environ,
        "OSIMFLOW_OPENSTUDIO_VERSION": openstudio_version,
        # The CLI is inside the container; pass arguments via env or bind-mount.
    }
    cmd = [
        "openstudio",          # Assumes openstudio CLI is on PATH
        "run",
        "-w", str(workflow_osw),
        "-m", str(model_path),
    ]
    # Argument values are typically written to the OSW or passed via --measure_arg
    return subprocess.run(cmd, env=env, capture_output=True, text=True)
```

> **Note:** In a real containerised OSimFlow job the CLI runs inside a Docker or
> Singularity container started by the executor.  The `openstudio` binary is not
> on the host PATH — it is available once the container runtime launches the
> `nrel/openstudio:<version>` image.

### 2.2 Parsing `eplusout.sql`

Every OpenStudio/EnergyPlus run produces an `eplusout.sql` SQLite database.
The canonical KPI table is `TabularDataWithStrings`:

```python
import sqlite3
from pathlib import Path

def query_eui(sql_path: Path) -> dict[str, float]:
    """Return EUI in kWh/m²/yr from eplusout.sql."""
    conn = sqlite3.connect(sql_path)
    cur = conn.cursor()
    cur.execute(
        """
        SELECT Value
        FROM TabularDataWithStrings
        WHERE ReportName = 'AnnualBuildingUtilityPerformanceSummary'
          AND ReportForString = 'Entire Facility'
          AND TableName = 'Site and Source Energy'
          AND RowName = 'Total Site Energy'
          AND ColumnName = 'Energy Per Total Building Area'
          AND Units = 'MJ/m2'
        """
    )
    row = cur.fetchone()
    conn.close()
    if row is None:
        return {}
    mj_m2 = float(row[0])
    return {"eui_kwh_m2_yr": round(mj_m2 / 3.6, 3)}
```

See [Section 6.1](#61-example-custom-kpi-extraction-from-eplusoutsql) for a
full working example.

### 2.3 Handling `eplusout.err`

The error file is the first place to look when a simulation fails.  OSimFlow's
own `bin/aggregate_results.py` uses this pattern to extract a one-line summary:

```python
import re
from pathlib import Path

def first_severe_error(err_path: Path) -> str | None:
    """Return the first '  * Severe' line from eplusout.err, or None."""
    if not err_path.exists():
        return None
    text = err_path.read_text(errors="replace")
    # EnergyPlus writes "  * Severe" with two leading spaces + asterisk.
    matches = re.findall(r"^\s{2}\*\s+Severe[^\n]*", text, re.MULTILINE)
    return matches[0].strip() if matches else None
```

---

## 3. BYOS Script Structure

A BYOS KPI extractor must define a function named **`extract_kpis`** with the
following signature:

```python
def extract_kpis(simulation_dir: Path, sample_id: str, out: Path) -> Path:
    """
    Args:
        simulation_dir: contains eplusout.sql, eplusout.err, etc.
        sample_id:      the sample's identifier, e.g. "0001".
        out:            output directory for the KPI JSON file.

    Returns:
        Path to the written KPI JSON file.
    """
```

The function must write a JSON file with this shape:

```json
{
  "sample_id": "0001",
  "kpis": {
    "eui": 142.5,
    "peak_demand": 82.3
  }
}
```

The Campaign loader (`osimflow.byos.load_user_function`) discovers the function
by name and passes it directly to the executor.  There is no separate CLI
surface for BYOS scripts — the Python function *is* the contract.

### Minimal BYOS KPI Extractor Template

```python
#!/usr/bin/env python3
"""BYOS custom KPI extractor — replace the _extract function body."""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path

log = logging.getLogger("my_kpi_extractor")


def extract_kpis(simulation_dir: Path, sample_id: str, out: Path) -> Path:
    out.mkdir(parents=True, exist_ok=True)
    kpi_path = out / f"kpi_{sample_id}.json"

    kpis = _extract(simulation_dir)

    kpi_path.write_text(json.dumps({"sample_id": sample_id, "kpis": kpis}, indent=2))
    log.info("wrote KPIs for sample %s -> %s", sample_id, kpi_path)
    return kpi_path


def _extract(simulation_dir: Path) -> dict[str, object]:
    """TODO: Replace this body with your KPI extraction logic."""
    sql_path = simulation_dir / "eplusout.sql"
    if not sql_path.exists():
        log.warning("eplusout.sql not found in %s", simulation_dir)
        return {}

    # TODO: Add your SQL queries here.
    return {}
```

Run it with:

```bash
set -euo pipefail

osimflow run \
  --executor local \
  --custom_kpi_extractor user_scripts/my_kpis.py \
  --input_variables variables.yml \
  --template_sim_package ./example_package \
  --n_samples 10 \
  --outdir ./results
```

---

## 4. Measure Discovery with MeasureRegistry

`osimflow.measures.MeasureRegistry` scans a `template_sim_package` for measures,
reads their argument definitions, and validates that every variable in
`variables.yml` maps to a real measure argument before simulations start.

### 4.1 Indexing Measures

```python
from pathlib import Path
from osimflow.measures import MeasureRegistry

registry = MeasureRegistry()
registry.index_measures(Path("/path/to/template_sim_package"))
```

This recursively searches `/path/to/template_sim_package/measures/` for
`measure.rb` (Ruby) and `measure.py` (Python) files and registers each one
under its directory name.

### 4.2 Reading Measure Arguments

```python
from pathlib import Path
from osimflow.measures import MeasureRegistry

registry = MeasureRegistry()

# Index first, then read arguments for a specific measure.
measure_path = Path("/path/to/template_sim_package/measures/SetWindowToWallRatio")
arguments = registry.read_measure_arguments(measure_path)

for arg in arguments:
    print(f"  {arg.name} ({arg.type}, required={arg.required}, default={arg.default})")
```

`read_measure_arguments` supports both Ruby (`OpenStudio::Measure::OSArgument`)
and Python (`openstudio.measure.OSArgument`) argument declarations.

### 4.3 Validating Variable Mappings

Before running a campaign, validate that every variable in `variables.yml`
corresponds to a real measure argument:

```python
from pathlib import Path
import yaml
from osimflow.measures import MeasureRegistry, UnmappedVariableError

registry = MeasureRegistry()
registry.index_measures(Path("/path/to/template_sim_package"))

with open("variables.yml") as f:
    variables = yaml.safe_load(f)["variables"]

try:
    registry.validate_variables_mapping(variables, registry)
    print("All variables are valid.")
except UnmappedVariableError as e:
    print(e)
    raise
```

Raises `UnmappedVariableError` (or `AmbiguousVariableError`) with a diagnostic
message if a variable has no matching argument.

### 4.4 Listing Available Measures

```python
registry = MeasureRegistry()
registry.index_measures(Path("/path/to/template_sim_package"))

for measure in registry.list_available_measures():
    print(f"{measure['name']} ({measure['language']})")
    for arg in measure["arguments"]:
        print(f"  --{arg['name']} [{arg['type']}] default={arg['default']}")
```

---

## 5. Running a Single Measure

To run a single measure on a model outside the OSimFlow campaign DAG, invoke
the OpenStudio CLI directly.  This is useful for debugging, pre-flight
validation, or CI tests.

### 5.1 CLI Command Structure

```bash
set -euo pipefail

OPENSTUDIO_VERSION="3.11.0"

openstudio run \
  -w workflow.osw \
  -m model.osm
```

The `workflow.osw` is the key artifact — it lists the measures and their
argument values in order.  To run a single measure you typically:

1. **Create a minimal OSW** that contains only the target measure.
2. **Set argument values** either by editing the OSW JSON directly or by
   passing `-a key=value` arguments (depending on the OpenStudio CLI version).

### 5.2 Required Arguments

| Argument | Description |
|---|---|
| `-w workflow.osw` | Path to the workflow file (can be a minimal OSW with one measure) |
| `-m model.osm` | Path to the input model file |

### 5.3 Measure Arguments via OSW

Edit the `workflow.osw` JSON to set argument values:

```json
{
  "steps": [
    {
      "measure_dir_name": "SetWindowToWallRatio",
      "arguments": {
        "wwr": 0.40,
        "sill_height": 0.90
      }
    }
  ]
}
```

### 5.4 Output Handling

After the CLI completes:

- `eplusout.sql` — contains simulation results (query with `sqlite3`).
- `eplusout.err` — contains warnings and errors.
- `report.sqlite` — contains tabular data (alternative to `eplusout.sql`).

Check the return code: `0` means success; non-zero typically means the
simulation failed.

---

## 6. Examples

### 6.1 Example: Custom KPI Extraction from `eplusout.sql`

This example extracts EUI and peak demand from the EnergyPlus SQLite output.
It is based on the working BYOS script at
[`user_scripts/examples/custom_kpi_eui.py`](../user_scripts/examples/custom_kpi_eui.py).

```python
#!/usr/bin/env python3
"""BYOS example: extract EUI and peak demand from eplusout.sql."""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path

log = logging.getLogger("custom_kpi_eui")

_MJ_TO_KWH = 1.0 / 3.6


def extract_kpis(simulation_dir: Path, sample_id: str, out: Path) -> Path:
    out.mkdir(parents=True, exist_ok=True)
    kpi_path = out / f"kpi_{sample_id}.json"

    sql_path = simulation_dir / "eplusout.sql"
    if not sql_path.exists():
        log.warning("eplusout.sql not found in %s", simulation_dir)
        kpis = {}
    else:
        kpis = _query_kpis(sql_path)

    kpi_path.write_text(json.dumps({"sample_id": sample_id, "kpis": kpis}, indent=2))
    log.info("wrote KPIs for sample %s -> %s", sample_id, kpi_path)
    return kpi_path


def _query_kpis(sql_path: Path) -> dict[str, float]:
    """Query EUI and peak demand from eplusout.sql.

    Uses the ``TabularDataWithStrings`` table, which EnergyPlus 9.x–24.x
    produce by default.
    """
    kpis: dict[str, float] = {}

    try:
        conn = sqlite3.connect(sql_path)
        cur = conn.cursor()

        # --- EUI ---
        cur.execute(
            """
            SELECT Value
            FROM TabularDataWithStrings
            WHERE ReportName = 'AnnualBuildingUtilityPerformanceSummary'
              AND ReportForString = 'Entire Facility'
              AND TableName = 'Site and Source Energy'
              AND RowName = 'Total Site Energy'
              AND ColumnName = 'Energy Per Total Building Area'
              AND Units = 'MJ/m2'
            """
        )
        row = cur.fetchone()
        if row is not None:
            mj_m2 = float(row[0])
            kpis["total_site_energy_MJ_m2_yr"] = round(mj_m2, 3)
            kpis["eui_kwh_m2_yr"] = round(mj_m2 * _MJ_TO_KWH, 3)

        # --- Peak demand ---
        cur.execute(
            """
            SELECT Value
            FROM TabularDataWithStrings
            WHERE ReportName = 'DemandEndUseComponentsSummary'
              AND ReportForString = 'Entire Facility'
              AND TableName = 'Total End Uses'
              AND RowName = 'Electricity'
              AND ColumnName = 'Whole Building'
            """
        )
        row = cur.fetchone()
        if row is not None:
            peak_w = float(row[0])
            kpis["peak_demand_w"] = round(peak_w, 3)
            kpis["peak_demand_kw"] = round(peak_w / 1000.0, 3)

        conn.close()
    except Exception as e:
        log.error("failed to query eplusout.sql at %s: %s", sql_path, e)

    return kpis
```

Run it:

```bash
set -euo pipefail

osimflow run \
  --executor local \
  --custom_kpi_extractor user_scripts/examples/custom_kpi_eui.py \
  --input_variables variables.yml \
  --template_sim_package ./example_package \
  --n_samples 10 \
  --outdir ./results
```

### 6.2 Example: Using MeasureRegistry to Discover and Validate Measures

This example shows how to use `MeasureRegistry` to index measures in a
template package and validate that all variables in `variables.yml` are
mapped to real measure arguments before launching a campaign.

```python
#!/usr/bin/env python3
"""Validate variables.yml against discovered measures before a campaign."""

from __future__ import annotations

import sys
import yaml
from pathlib import Path

from osimflow.measures import (
    MeasureRegistry,
    UnmappedVariableError,
    AmbiguousVariableError,
)


def validate_campaign(template_pkg: Path, variables_yml: Path) -> bool:
    """Return True if all variables map to discovered measure arguments."""
    registry = MeasureRegistry()
    registry.index_measures(template_pkg)

    with open(variables_yml) as f:
        variables = yaml.safe_load(f).get("variables", [])

    try:
        registry.validate_variables_mapping(variables, registry)
        print("✓ All variables are valid.")
        return True
    except UnmappedVariableError as e:
        print("✗ Variable validation FAILED:")
        print(e)
        return False
    except AmbiguousVariableError as e:
        print("✗ Ambiguous variables — use dotted form to disambiguate:")
        print(e)
        return False


if __name__ == "__main__":
    template_pkg = Path("example_package")
    variables_yml = Path("variables.yml")

    ok = validate_campaign(template_pkg, variables_yml)
    sys.exit(0 if ok else 1)
```

### 6.3 Example: Running a Single Measure for Debugging

This shell snippet creates a minimal OSW for a single measure and runs it on
a model.  It is intended for interactive debugging, not for campaign use.

```bash
set -euo pipefail

TEMPLATE="example_package"
MEASURE="SetWindowToWallRatio"
MODEL="model.osm"
OUTDIR="/tmp/measure_debug"

# Create a minimal OSW targeting one measure.
python3 - <<'EOF'
import json
from pathlib import Path

osw = {
    "steps": [
        {
            "measure_dir_name": "SetWindowToWallRatio",
            "arguments": {
                "wwr": 0.40,
                "sill_height": 0.90,
            }
        }
    ]
}
Path("workflow_debug.osw").write_text(json.dumps(osw, indent=2))
EOF

mkdir -p "$OUTDIR"

# Run the measure via openstudio CLI.
# The openstudio binary is on PATH inside the container started by OSimFlow.
openstudio run \
  -w workflow_debug.osw \
  -m "$MODEL" \
  -o "$OUTDIR"

echo "Output written to $OUTDIR"
ls -la "$OUTDIR"
```

---

## See Also

- [`user_scripts/README.md`](../user_scripts/README.md) — full BYOS contract reference
- [`user_scripts/examples/`](../user_scripts/examples/) — worked BYOS scripts
- [`user_scripts/templates/`](../user_scripts/templates/) — BYOS script templates
- [`osimflow/measures.py`]((../osimflow/measures.py)) — `MeasureRegistry` API
- [`osimflow/_work_scripts/extract_kpis.py`](../osimflow/_work_scripts/extract_kpis.py) — default KPI extraction logic
- [`docs/user-guide.md`](user-guide.md) — OSimFlow user guide
- [OpenStudio CLI reference](https://openstudio.net/docs/cli/)
