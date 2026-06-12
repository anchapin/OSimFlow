# user_scripts/ — BYOS (Bring Your Own Script) overrides

This directory is where users drop **custom Python scripts** that override the
default OSimFlow behavior. A script here is selected via a CLI flag such as
`--custom_apply_script user_scripts/my_apply.py` or
`--custom_kpi_extractor user_scripts/my_kpis.py`.

> **Status:** The interface spec is stabilised.  See the **worked examples**
> in [`examples/`](examples/) and the **templates** in [`templates/`](templates/)
> for ready-to-copy starting points.

## Function-name convention

BYOS scripts must define a function named **`apply_parameters`** (for
parameter-application overrides) or **`extract_kpis`** (for KPI extraction
overrides). The function-name `apply` (legacy) is accepted but emits a
`DeprecationWarning`.

The canonical loader is `osimflow.byos.load_user_function`.

## Worked examples and templates

The [`examples/`](examples/) directory contains four complete, documented
BYOS scripts covering the most common patterns:

- **`custom_kpi_eui.py`** — extract EUI (kWh/m2/yr) from `eplusout.sql`.
- **`custom_kpi_enduses.py`** — extract end-use energy breakdown.
- **`custom_apply_wwr.py`** — modify window-to-wall ratio in `.osw` measure arguments.
- **`custom_apply_epw_swap.py`** — swap `.epw` weather file for multi-climate studies.

The [`templates/`](templates/) directory has commented skeletons with `TODO`
markers for a quick start:

- **`kpi_extractor_template.py`** — fill in your SQL queries.
- **`apply_params_template.py`** — fill in your parameterisation logic.

See [`examples/README.md`](examples/README.md) for a step-by-step walkthrough.

## Interface reference

### `apply_parameters` override

```python
def apply_parameters(template: Path, parameters: dict, sample_id: str, out: Path) -> Path:
    """Modify a copy of the template model according to `parameters`.

    Args:
        template:  Path to the template_sim_package (.osm, .osw, or directory).
        parameters: dict mapping variable names to sampled values.
        sample_id:  the sample's identifier, e.g. "0001".
        out:        per-sample output directory.

    Returns:
        The per-sample output directory (``out``).
    """
```

### `extract_kpis` override

```python
def extract_kpis(simulation_dir: Path, sample_id: str, out: Path) -> Path:
    """Compute KPIs from one sample's simulation output.

    Args:
        simulation_dir: contains eplusout.sql, report.csv, etc.
        sample_id:      the sample's identifier, e.g. "0001".
        out:            output directory for the KPI JSON file.

    Returns:
        Path to the written KPI JSON file.
    """
```

## Security note

BYOS scripts are treated as **untrusted** (see AGENTS.md §10). By default,
OSimFlow runs BYOS scripts in an **isolated subprocess** (`--byos-trust-level
subprocess`) so they cannot access the orchestrator's memory, credentials, or
open file handles.

For local development or when you explicitly trust the script, you can opt in
to the legacy in-process mode:

```bash
osimflow run --byos-trust-level inprocess --custom_apply_script user_scripts/mine.py ...
```

**Cloud executors** (AWS Batch, Slurm) already run scripts inside a container
or job isolation boundary; the subprocess mode is an additional
defence-in-depth layer.

The OSimFlow runner validates the function signature, and in subprocess mode
applies process-level isolation with proper stdout/stderr capture.
