# user_scripts/ — BYOS (Bring Your Own Script) overrides

This directory is where users drop **custom Python scripts** that override the
default OSimFlow behavior. A script here is selected via a CLI flag such as
`--custom_apply_script user_scripts/my_apply.py` or
`--custom_kpi_extractor user_scripts/my_kpis.py`.

> **Status:** The exact interface spec is part of the Phase 3 deliverables
> (PRD §5.2 / §3.1 *User-Provided Custom Post-Processing Scripts*). This file
> is a placeholder for the final spec.

## Function-name convention

BYOS scripts must define a function named **`apply_parameters`** (for
parameter-application overrides) or **`extract_kpis`** (for KPI extraction
overrides). The function-name `apply` (legacy) is accepted but emits a
`DeprecationWarning`.

The canonical loader is `osimflow.byos.load_user_function`.

## Planned interface (sketch)

### `apply_params_to_model` override

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

BYOS scripts are treated as **untrusted** (see AGENTS.md §10). The OSimFlow
runner validates the function signature, sandboxes the working directory, and
applies a per-script timeout.
