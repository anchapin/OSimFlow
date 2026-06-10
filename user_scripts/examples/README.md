# user_scripts/examples/ — BYOS Worked Examples

This directory contains complete, runnable BYOS (Bring Your Own Script)
examples that you can copy and adapt for your own simulation campaigns.

## Quick start

1. Copy the example that matches your use case into `user_scripts/`:
   ```bash
   cp user_scripts/examples/custom_kpi_eui.py user_scripts/my_kpis.py
   ```
2. Edit the copied file to customise the KPI query or parameter logic.
3. Run the campaign with the `--custom_*` flag:
   ```bash
   osimflow run \
       --executor local \
       --custom_kpi_extractor user_scripts/my_kpis.py \
       --input_variables variables.yml \
       --template_sim_package ./example_package \
       --n_samples 10 \
       --outdir ./results
   ```

## Examples

### KPI extractors (override with `--custom_kpi_extractor`)

| File | Description |
|---|---|
| `custom_kpi_eui.py` | Extracts EUI (kWh/m2/yr) from `eplusout.sql` via the `Site and Source Energy` table. The most common pattern (~80% of use cases). |
| `custom_kpi_enduses.py` | Extracts end-use energy breakdown (heating, cooling, fans, lighting, etc.) from the `End Uses` table. Demonstrates multi-row, multi-column tabular data parsing. |

### Parameter applicators (override with `--custom_apply_script`)

| File | Description |
|---|---|
| `custom_apply_wwr.py` | Modifies window-to-wall ratio in the `.osw` measure arguments. The simplest parameterization pattern — pure JSON mutation, no OpenStudio bindings needed. |
| `custom_apply_epw_swap.py` | Swaps the `.epw` weather file path in the model. Demonstrates file-path parameterization for multi-climate studies. |

## Templates

For a from-scratch implementation, start from the templates in
`user_scripts/templates/`:

- `kpi_extractor_template.py` — skeleton with TODO markers for custom KPI extraction.
- `apply_params_template.py` — skeleton for custom parameter application.

## How BYOS validation works

When you pass `--custom_kpi_extractor my_script.py` or
`--custom_apply_script my_script.py`, the framework:

1. **Loads** the file via `importlib.util` (`osimflow/byos.py`).
2. **Discovers** the function by name:
   - `extract_kpis` for KPI extractors.
   - `apply_parameters` for parameter applicators.
3. **Calls** the function directly with the correct arguments — no
   subprocess, no CLI parsing in your script.

The function signature **must** match:

```python
# KPI extractor
def extract_kpis(simulation_dir: Path, sample_id: str, out: Path) -> Path:
    ...

# Parameter applicator
def apply_parameters(
    template: Path,
    parameters: dict[str, object],
    sample_id: str,
    out: Path,
) -> Path:
    ...
```

If the function name is not found, the campaign fails with a clear
error message. The deprecated name `apply` (without `_parameters`) also
works but emits a `DeprecationWarning`.

## Return value contract

Both functions must return a `Path`:

- **KPI extractor** — returns the path to a JSON file with the shape:
  ```json
  {"sample_id": "0001", "kpis": {"eui_kwh_m2_yr": 120.5}}
  ```

- **Parameter applicator** — returns the per-sample output directory
  containing the modified model/workflow files.

## Security

BYOS scripts are treated as **untrusted** (see AGENTS.md §10). The
Campaign validates the function signature with `inspect.signature` and
runs scripts in the executor's working directory with no elevated
privileges.
