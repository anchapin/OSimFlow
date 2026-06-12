# OSimFlow Measures and Parameters

Guides AI agents through working with OpenStudio measures in the OSimFlow context — defining parameters, applying them to models, and understanding the measure-to-variables mapping.

## Triggers

- "measure", "OpenStudio measure", "OS measure"
- "parameter application", "apply parameters", "parameterize model"
- ".osw", "workflow file", "OpenStudio workflow"
- "variables.yml", "input variables", "LHS variables"
- "measure_argument", "measure argument"
- "apply_params", "apply_params_to_model"
- "add a parameter", "new parameter", "parameterize"
- "template_sim_package"

## Quick Reference

### Key Files

| File | Purpose |
|---|---|
| `osimflow/work.py` | `default_apply_parameters` — default parameter application logic |
| `bin/apply_params_to_model.py` | CLI script for parameter application |
| `osimflow/campaign.py` | `step_apply_parameters` — the DAG step that fans out |
| `variables.yml` | User-supplied input variable definitions |
| `template_sim_package/` | User-supplied directory with base `.osm`/`.osw` + measures |

### The Measure-Parameter Contract

OSimFlow does **not** directly edit `.osm` files. Instead, it:
1. Reads `variables.yml` to know which parameters to vary
2. Generates LHS sample values for each parameter
3. Applies each sample's values to the model by setting **measure arguments** in the `.osw`
4. Runs `openstudio.cli run -w workflow.osw` which executes the measures

### Measure Argument Naming Convention

Parameters in `variables.yml` use the format:

```
MeasureName.argument_name
```

For example:
- `InsulationMeasure.r_value` → sets the `r_value` argument on the `InsulationMeasure`
- `WindowMeasure.shgc` → sets the `shgc` argument on the `WindowMeasure`
- `HVACMeasure.cop` → sets the `cop` argument on the `HVACMeasure`

The measure name must match the directory name in `template_sim_package/measures/`.

### variables.yml Structure

```yaml
variables:
  - name: "InsulationRValue"                        # Human-readable name
    distribution: "uniform"                          # scipy distribution name
    min: 2.0                                         # Distribution parameter
    max: 10.0                                        # Distribution parameter
    measure_argument: "InsulationMeasure.r_value"    # MeasureName.argument_name

  - name: "WindowSHGC"
    distribution: "uniform"
    min: 0.2
    max: 0.6
    measure_argument: "WindowMeasure.shgc"
```

### Supported Distributions

Any distribution from `scipy.stats`:

| Distribution | Key Parameters | Example |
|---|---|---|
| `uniform` | `min`, `max` | Continuous uniform |
| `normal` | `mean`, `std` | Normal/Gaussian |
| `loguniform` | `min`, `max` | Log-uniform |
| `triangular` | `min`, `max`, `mode` | Triangular |
| ` randint` | `min`, `max` | Discrete uniform (integers) |

## Detailed Guide

### How Measures Relate to variables.yml

The relationship chain is:

```
variables.yml          → defines WHAT to vary and HOW (distribution)
    ↓
samples.json           → concrete values for each sample (LHS output)
    ↓
.osw (per sample)      → measure_arguments updated with sample values
    ↓
openstudio.cli run     → measures read arguments, modify the model
    ↓
.osm (per sample)      → modified building model
    ↓
EnergyPlus             → runs the simulation
```

### Adding a New Parameter to an Existing Measure

Step-by-step:

1. **Identify the measure and its arguments.** Check the measure's Ruby/Python source in `template_sim_package/measures/<MeasureName>/`. Look for the `arguments` method — each argument has a `name`, `display_name`, `type`, and `default_value`.

2. **Add the parameter to `variables.yml`:**

   ```yaml
   variables:
     # ... existing variables ...

     - name: "InfiltrationRate"
       distribution: "uniform"
       min: 0.1
       max: 0.5
       measure_argument: "InfiltrationMeasure.flow_rate"
   ```

3. **Run a dry-run to validate:**

   ```bash
   osimflow run --dry-run \
     --input_variables variables.yml \
     --template_sim_package ./example_package \
     --outdir ./dry-results
   ```

   The pre-flight check in `step_apply_parameters` will fail fast if the `measure_argument` doesn't map to a real measure argument.

4. **Verify the parameter applies correctly** by inspecting the modified `.osw` in `./dry-results/work/`.

### The bin/apply_params_to_model.py Contract

This script is the default parameter-application logic. It:

1. Reads a single sample's parameter values from a JSON file
2. Loads the template `.osw`
3. Updates measure arguments in the `.osw` using the `measure_argument` mapping
4. Writes the modified `.osw` to the sample's work directory

**Function signature (for BYOS override):**

```python
def apply_parameters(
    template_sim_package: Path,
    sample_params: dict[str, float],
    outdir: Path,
    sample_id: str,
) -> Path:
    """
    Apply parameter values to the model.

    Args:
        template_sim_package: Path to the template package directory.
        sample_params: Dict mapping parameter names to sampled values.
        outdir: Output directory for this sample.
        sample_id: Unique identifier for this sample.

    Returns:
        Path to the modified simulation package (containing .osw/.osm).
    """
```

To override with a custom script:

```bash
osimflow run \
  --custom_apply_script user_scripts/my_apply.py \
  --input_variables variables.yml \
  ...
```

### The .osw Structure (Relevant Parts)

```json
{
  "steps": [
    {
      "measure_dir_name": "InsulationMeasure",
      "arguments": {
        "r_value": 5.0,
        "__skip": false
      }
    },
    {
      "measure_dir_name": "WindowMeasure",
      "arguments": {
        "shgc": 0.4,
        "__skip": false
      }
    }
  ],
  "seed_file": "model.osm",
  "weather_file": "weather.epw"
}
```

OSimFlow updates the `arguments` dict for each step based on the `measure_argument` mapping in `variables.yml`.

### Measure Directory Structure

```
template_sim_package/
├── model.osm                          # Seed model
├── workflow.osw                       # Workflow file
├── weather.epw                        # Weather file (optional, can be discovered)
└── measures/
    ├── InsulationMeasure/
    │   ├── measure.py                 # or measure.rb
    │   ├── measure.xml                # Measure metadata
    │   └── ...
    ├── WindowMeasure/
    │   ├── measure.py
    │   └── ...
    └── ...
```

### Pre-flight Parameter Validation

Before any simulation runs, `step_apply_parameters` validates that:
- Every `measure_argument` in `variables.yml` maps to an existing measure directory
- Every argument name in the `measure_argument` exists in the measure's argument definitions
- The seed model file exists

If validation fails, the campaign stops with a clear error message indicating which parameter is invalid.

## Common Patterns

### Adding a Measure That Doesn't Exist Yet

1. Create the measure directory: `template_sim_package/measures/NewMeasure/`
2. Write the measure script (`measure.py` or `measure.rb`) following OpenStudio measure conventions
3. Create `measure.xml` with argument definitions
4. Add the measure step to `workflow.osw`
5. Add the parameter to `variables.yml` with `measure_argument: "NewMeasure.arg_name"`
6. Test with `--dry-run`

### Disabling a Measure for Certain Samples

Set the `__skip` argument to `true` in the `.osw` for that step. This is not a standard OSimFlow workflow — you would need a BYOS `custom_apply_script` to conditionally skip measures.

### Using Integer Parameters

```yaml
variables:
  - name: "NumFloors"
    distribution: "randint"
    min: 1
    max: 10
    measure_argument: "BuildingGeometry.num_floors"
```

### Using Triangular Distribution

```yaml
variables:
  - name: "WWR"
    distribution: "triangular"
    min: 0.2
    max: 0.8
    mode: 0.4
    measure_argument: "FenestrationMeasure.window_to_wall_ratio"
```

## Gotchas

1. **Measure names must match directory names** — The `MeasureName` in `measure_argument: "MeasureName.arg"` must exactly match the directory name in `template_sim_package/measures/`. Case-sensitive.

2. **Argument names must match measure definition** — The `arg` part must match the `name` field in the measure's `arguments` method, not the `display_name`.

3. **Measure dependencies must be in the package** — Custom Ruby/Python measure dependencies must be packaged inside the `template_sim_package`, not installed at runtime. The container may not have network access.

4. **Pre-flight catches bad mappings** — Don't skip the pre-flight step (`--skip-preflight`) unless you know the parameters are valid. It saves time on cloud runs by failing before expensive simulation.

5. **The `.osw` is the source of truth** — OSimFlow modifies the `.osw`, not the `.osm` directly. The measures run at simulation time to modify the model. If you need direct `.osm` edits, use a BYOS `custom_apply_script`.

6. **Default argument values come from the measure** — If a parameter is not listed in `variables.yml`, its value comes from the measure's default (defined in `measure.xml` or the measure script). OSimFlow only varies parameters explicitly listed in `variables.yml`.

7. **One measure_argument per variable** — Each variable in `variables.yml` maps to exactly one measure argument. To vary the same measure argument with different distributions across studies, use separate campaigns with separate `variables.yml` files.

8. **Weather files** — `.epw` files are out of scope for parameter variation in OSimFlow (PRD §3.2). The weather file is part of the `template_sim_package`. Use `osimflow.weather.discover_epw_files` or `download_epw` to manage weather data separately.
