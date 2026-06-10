# user_scripts/ — BYOS (Bring Your Own Script) overrides

This directory is where users drop **custom Python scripts** that override the
default OSimFlow behavior. A script here is selected via a CLI flag such as
`--custom_apply_script user_scripts/my_apply.py` or
`--custom_kpi_extractor user_scripts/my_kpis.py`.

> **Status:** The exact interface spec is part of the Phase 3 deliverables
> (PRD §5.2 / §3.1 *User-Provided Custom Post-Processing Scripts*). This file
> is a placeholder for the final spec.

## Planned interface (sketch)

### `apply_params_to_model` override

```python
def apply(ctx: dict) -> dict:
    """Modify a copy of the template model according to `ctx['parameters']`.

    Args:
        ctx: {
            "template_dir": Path,         # template_sim_package contents
            "parameters":   dict,         # {var_name: sampled_value, ...}
            "sample_id":    str,          # e.g. "0001"
            "openstudio":   module,       # imported `openstudio` Python bindings
        }

    Returns:
        A dict describing what to write into the per-sample directory, e.g.:
        {
            "osm_path":  Path,            # modified .osm file
            "osw_path":  Path,            # modified .osw file
            "extra":     list[Path],      # any additional files to bundle
            "warnings":  list[str],       # non-fatal issues to surface in run.json
        }
    """
```

### `extract_kpis` override

```python
def extract(ctx: dict) -> dict:
    """Compute KPIs from one sample's simulation output.

    Args:
        ctx: {
            "sample_id":     str,
            "simulation_dir": Path,       # contains eplusout.sql, report.csv, etc.
            "openstudio":    module,      # openstudio Python bindings (if available)
        }

    Returns:
        A flat dict of {kpi_name: value, ...}. Values must be JSON-serializable.
    """
```

## Security note

BYOS scripts are treated as **untrusted** (see AGENTS.md §10). The OSimFlow
runner validates the function signature, sandboxes the working directory, and
applies a per-script timeout.
