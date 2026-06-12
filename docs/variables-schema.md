# variables.yml Schema Reference

The `variables.yml` file is the primary interface for defining parametric
variables in an OSimFlow campaign. It declares which parameters vary, their
probability distributions, and how each parameter maps to the building energy
model.

OSimFlow uses **Latin Hypercube Sampling (LHS)** via `scipy.stats.qmc` to
generate stratified parameter sets that efficiently explore the design space.

---

## Table of Contents

1. [File Structure](#file-structure)
2. [Top-Level Keys](#top-level-keys)
3. [Variable Entry Schema](#variable-entry-schema)
4. [Supported Distributions](#supported-distributions)
5. [Variable Name Mapping](#variable-name-mapping)
6. [Pre-flight Parameter Validation](#pre-flight-parameter-validation)
7. [Advanced Patterns](#advanced-patterns)
8. [Complete Example](#complete-example)

---

## File Structure

`variables.yml` is a YAML file with four optional top-level sections:

```yaml
variables:
  - name: ...
    distribution: ...
    ...

baseline:
  sample_id: baseline
  parameters:
    window_u_value: 2.5
    infiltration_rate: 0.5

objective:
  name: eui
  direction: minimize
  weight: 1.0

constraints:
  - name: cost
    max: 5000
    min: 100
```

- **`variables`** (required) — list of parameter definitions.
- **`baseline`** (optional) — a fixed-parameter baseline sample for
  ASHRAE 90.1 comparison mode. See
  [Baseline Comparison](#baseline-comparison).
- **`objective`** (optional) — objective function configuration for
  optimisation algorithms (DE, DA, NSGA-II, PSO, SPEA2). See
  [Objective and Constraint Configuration](#objective-and-constraint-configuration).
- **`constraints`** (optional) — list of constraint definitions.
  See [Objective and Constraint Configuration](#objective-and-constraint-configuration).

---

## Top-Level Keys

| Key | Type | Required | Description |
|---|---|---|---|
| `variables` | list of dicts | Yes | Parameter definitions. Each entry defines one parametric variable. |
| `baseline` | dict | No | Baseline sample configuration. Contains `sample_id` (str) and `parameters` (dict of fixed values). |
| `objective` | dict | No | Objective function configuration for optimisation algorithms. Contains `name` (KPI name), `direction` (`minimize`\|`maximize`), and `weight` (float, default 1.0). |
| `constraints` | list of dicts | No | Constraint definitions. Each entry has `name` (KPI name), `max` (upper bound), and optionally `min` (lower bound). Violations are penalised with a large positive value (1e9) added to the objective. |

### Baseline Comparison

When a `baseline` section is present, OSimFlow prepends a fixed-parameter
sample to the LHS set. After the campaign completes, percentage improvement
statistics are computed for every KPI relative to the baseline values. The
baseline sample appears in `aggregated_results.csv` alongside the parametric
samples.

```yaml
baseline:
  sample_id: ashrae_90_1
  parameters:
    window_u_value: 2.5
    infiltration_rate: 0.5
    lighting_power_density: 10.0
```

---

## Objective and Constraint Configuration

When using an optimisation algorithm (`--algorithm de`, `dual_annealing`,
`nsga2`, `pso`, `spea2`), the `objective` and `constraints` sections
configure the goal and any bounds on the solution space (issue #282).

### `objective`

| Key | Type | Required | Default | Description |
|---|---|---|---|---|
| `name` | string | No | `eui` | KPI name to optimise. |
| `direction` | string | No | `minimize` | `minimize` or `maximize`. |
| `weight` | float | No | `1.0` | Aggregation weight for multi-objective algorithms. |

```yaml
objective:
  name: eui
  direction: minimize
  weight: 1.0
```

For **single-objective** algorithms (DE, Dual Annealing, PSO), `direction`
controls whether the algorithm minimises or maximises the named KPI.
For **multi-objective** algorithms (NSGA-II, SPEA2), `weight` scales the
objective before the algorithm applies non-dominated sorting.

### `constraints`

Each entry defines an upper bound (`max`) and optionally a lower bound
(`min`) on a KPI. When a constraint is violated, a penalty of `1e9` is
added to the objective value, making infeasible solutions strictly worse
than any feasible solution.

| Key | Type | Required | Default | Description |
|---|---|---|---|---|
| `name` | string | Yes | — | KPI name to constrain. |
| `max` | float | Yes* | — | Upper bound. Required unless `min` is set. |
| `min` | float | No | `-inf` | Lower bound. |

*Either `max` or `min` (or both) must be provided.

```yaml
constraints:
  - name: cost
    max: 5000
  - name: thermal_discomfort
    max: 100
    min: 0
```

**Penalty handling:** The penalty is added directly to the objective
value before comparison. For minimisation, a constraint violation
`cost = 6000 > max = 5000` adds `1e9` to the objective, making the
solution worse than any feasible solution regardless of its raw KPI
value. For maximisation, the same logic applies after sign-flipping.

**Typical use:** Cost ceilings, thermal discomfort limits, maximum EUI
caps for code compliance, minimum ventilation rates.

#### Example: DE with objective and constraints

```yaml
variables:
  - name: window_u_value
    distribution: uniform
    min: 1.0
    max: 5.0

  - name: wall_r_value
    distribution: uniform
    min: 2.0
    max: 10.0

objective:
  name: eui
  direction: minimize
  weight: 1.0

constraints:
  - name: cost
    max: 5000
  - name: thermal_discomfort
    max: 50
```

---

## Variable Entry Schema

Every entry in the `variables` list must have at minimum a `name` and a
`distribution`. Additional keys depend on the chosen distribution.

### Universal Keys

| Key | Type | Required | Description |
|---|---|---|---|
| `name` | string | Yes | The parameter identifier. Must map to a measure argument or `.osm` attribute in the template (see [Variable Name Mapping](#variable-name-mapping)). |
| `distribution` | string | Yes | The probability distribution. One of: `uniform`, `lognormal`, `normal`, `triangular`, `beta`, `gamma`, `exponential`, `discrete`, `categorical`, `conditional`. |
| `target` | string | No | Special target type. Currently supports `epw_file` for weather file selection (see [Categorical / EPW Targets](#categorical--epw-targets)). |
| `mapping` | dict | No | For `categorical` distributions with `target: epw_file`: maps each category label to a file path inside the template package. |

### Distribution-Specific Keys

Each distribution requires specific parameters. See
[Supported Distributions](#supported-distributions) below for details.

---

## Supported Distributions

OSimFlow supports **nine** distributions, implemented via the
Percent Point Function (PPF / inverse CDF) of each `scipy.stats`
distribution. The LHS engine generates uniform samples in [0, 1] and
transforms them through the PPF.

### 1. `uniform` — Continuous Uniform

Evenly distributed between a minimum and maximum.

| Key | Type | Required | Default | Description |
|---|---|---|---|---|
| `min` | float | Yes | — | Lower bound (inclusive). |
| `max` | float | Yes | — | Upper bound (inclusive). |

```yaml
- name: window_u_value
  distribution: uniform
  min: 1.0
  max: 5.0
```

**Typical use:** Window U-value (W/m²·K), wall R-value ranges, lighting
power density, window-to-wall ratio.

### 2. `lognormal` — Log-Normal

Values are log-normally distributed. The `mean` and `sigma` parameters
describe the **underlying normal distribution** (i.e., `mean` is the mean
of `ln(X)`, not of `X` itself).

| Key | Type | Required | Default | Description |
|---|---|---|---|---|
| `mean` | float | Yes | — | Mean of the underlying normal distribution (μ of ln(X)). |
| `sigma` | float | Yes | — | Standard deviation of the underlying normal distribution (σ of ln(X)). |

```yaml
- name: infiltration_rate
  distribution: lognormal
  mean: 0.5
  sigma: 0.2
```

**Typical use:** Air infiltration rates (ACH), which are strictly positive
and right-skewed in practice. Note that the median of the resulting
distribution is `exp(mean)` = `exp(0.5)` ≈ 1.65 ACH.

### 3. `normal` — Normal (Gaussian)

Symmetric bell curve. Can produce negative values; use bounded
distributions if negative values are physically impossible.

| Key | Type | Required | Default | Description |
|---|---|---|---|---|
| `mean` | float | Yes | — | Distribution mean (μ). |
| `sigma` | float | Yes | — | Standard deviation (σ). |

```yaml
- name: hvac_setpoint
  distribution: normal
  mean: 22.0
  sigma: 1.0
```

**Typical use:** HVAC temperature setpoints (°C), thermostat schedules.
Most samples will fall within ±2σ of the mean (20–24°C).

### 4. `triangular` — Triangular

Linearly increasing then decreasing. Models parameters where a central
value is most likely.

| Key | Type | Required | Default | Description |
|---|---|---|---|---|
| `min` | float | Yes | — | Lower bound. |
| `max` | float | Yes | — | Upper bound. |
| `mode` | float | No | midpoint | Peak position. Defaults to `(min + max) / 2` (symmetric triangle). |

```yaml
- name: lighting_power_density
  distribution: triangular
  min: 5.0
  max: 15.0
  mode: 10.0
```

**Typical use:** Lighting power density (W/m²), equipment loads, internal
gains where a most-likely value is known.

### 5. `beta` — Beta

Flexible distribution on a bounded interval [loc, loc + scale]. The
`alpha` and `beta` parameters control the shape.

| Key | Type | Required | Default | Description |
|---|---|---|---|---|
| `alpha` | float | Yes | — | Shape parameter α (α > 0). |
| `beta` | float | Yes | — | Shape parameter β (β > 0). |
| `loc` | float | No | 0.0 | Lower bound of the support interval. |
| `scale` | float | No | 1.0 | Width of the support interval. |

```yaml
- name: thermal_conductivity
  distribution: beta
  alpha: 2.0
  beta: 5.0
  loc: 0.1
  scale: 2.0
```

**Typical use:** Bounded parameters where prior knowledge suggests a
non-uniform shape — e.g., window-to-wall ratio (bounded 0–1), thermal
conductivity within a known range.

**Shape guide:**
- `alpha = beta = 1` → uniform on [loc, loc + scale]
- `alpha > beta` → right-skewed
- `alpha < beta` → left-skewed
- `alpha = beta > 1` → symmetric, peaked at midpoint

### 6. `gamma` — Gamma

Right-skewed distribution for strictly positive values. Controlled by a
shape parameter and scale.

| Key | Type | Required | Default | Description |
|---|---|---|---|---|
| `alpha` | float | Yes | — | Shape parameter (α > 0). |
| `loc` | float | No | 0.0 | Location parameter (shift). |
| `scale` | float | No | 1.0 | Scale parameter (θ). |

```yaml
- name: internal_gain
  distribution: gamma
  alpha: 2.0
  loc: 0.0
  scale: 5.0
```

**Typical use:** Internal heat gains, occupancy schedules, equipment
power — quantities that are strictly positive and right-skewed.

### 7. `exponential` — Exponential

Models the interval between independent events. Controlled by a single
rate parameter.

| Key | Type | Required | Default | Description |
|---|---|---|---|---|
| `rate` | float | Yes | — | Rate parameter (λ). Mean = 1/rate. |

```yaml
- name: equipment_lifetime
  distribution: exponential
  rate: 10.0
```

**Typical use:** Equipment lifetime, time-to-failure, maintenance intervals.

### 8. `discrete` — Discrete Choices

Uniformly samples from an explicit list of numeric values.

| Key | Type | Required | Default | Description |
|---|---|---|---|---|
| `values` | list of number | Yes | — | Explicit list of possible values. |

```yaml
- name: number_of_floors
  distribution: discrete
  values: [1, 2, 3, 5, 10]
```

**Typical use:** Integer parameters (number of floors, number of people),
standardized construction type codes, predefined performance tiers.

### 9. `categorical` — Categorical / Nominal

Uniformly samples from a list of named categories. Produces structured
output (`{"label": ..., "index": ...}`) rather than a scalar.

| Key | Type | Required | Default | Description |
|---|---|---|---|---|
| `values` | list of string | Yes | — | Category labels. |
| `mapping` | dict | No | — | Maps each label to an arbitrary value (e.g., a file path). Required when `target: epw_file` is set. |

```yaml
- name: climate_zone
  distribution: categorical
  values: ["hot_humid", "mixed_humid", "cold"]
  mapping:
    hot_humid: weather/USA_FL_Miami.epw
    mixed_humid: weather/USA_IL_Chicago.epw
    cold: weather/USA_MN_Duluth.epw
  target: epw_file
```

**Typical use:** Climate zone selection, construction type, HVAC system
type — any nominal variable where values are labels rather than numbers.

#### Categorical / EPW Targets

When `target: epw_file` is set, the Campaign resolves each category label
through the `mapping` dict to produce a weather file path. At simulation
time, the `.osw` file's `weather_file` field is updated to the resolved
path. Pre-flight validation checks that every mapped `.epw` file exists
inside the `template_sim_package` directory and has a valid EPW header
(starting with `LOCATION`).

### 10. `conditional` — Dependent / Conditional Variables

Samples a variable from a distribution that depends on the value of another
variable. The parent variable is sampled first (it may itself be a
`conditional` variable), and then the conditional variable is sampled from
the sub-distribution matching the parent's value.

| Key | Type | Required | Description |
|---|---|---|---|
| `distribution` | string | Yes | Must be `"conditional"`. |
| `depends_on` | string | Yes | Name of the parent variable whose value determines which sub-distribution to use. |
| `conditions` | dict | Yes | Maps each possible parent value (as a string) to a sub-distribution definition. Each sub-distribution must have a `distribution` key plus the corresponding distribution parameters. |

```yaml
- name: hvac_system_type
  distribution: categorical
  values: [vav, cv, ptx]

- name: cooling_efficiency
  distribution: conditional
  depends_on: hvac_system_type
  conditions:
    vav: {distribution: uniform, min: 3.0, max: 5.0}
    cv: {distribution: uniform, min: 2.5, max: 4.0}
    ptx: {distribution: uniform, min: 4.0, max: 8.0}
```

In this example, `cooling_efficiency` is sampled from a different uniform
range depending on which HVAC system type was drawn for that sample.

**Typical use:** HVAC efficiency constrained by system type, window
properties constrained by construction type, ground-loop depth only
applicable for ground-source heat pumps.

#### How It Works

1. The LHS engine generates one uniform [0, 1] sample per variable (including
   conditional ones), maintaining the stratification property.
2. Variables are resolved in **dependency order**: independent variables first,
   then conditional variables whose parents have already been resolved.
3. For each conditional variable, the parent's resolved value is looked up in
   the `conditions` dict to select the appropriate sub-distribution.
4. The variable's LHS sample is then transformed through the sub-distribution's
   PPF.

#### Nested Conditions

Conditional variables can depend on other conditional variables, forming a
chain. OSimFlow validates the dependency graph at sampling time and rejects
circular dependencies:

```yaml
- name: hvac_system_type
  distribution: categorical
  values: [vav, wshp, ptx]

- name: cooling_efficiency
  distribution: conditional
  depends_on: hvac_system_type
  conditions:
    vav: {distribution: uniform, min: 3.0, max: 5.0}
    wshp: {distribution: uniform, min: 4.0, max: 8.0}
    ptx: {distribution: uniform, min: 2.5, max: 4.0}

- name: supply_air_temp_reset
  distribution: conditional
  depends_on: hvac_system_type
  conditions:
    vav: {distribution: uniform, min: 12.0, max: 18.0}
    wshp: {distribution: discrete, values: [15.0]}
    ptx: {distribution: discrete, values: [15.0]}
```

#### Error Cases

- **Missing `depends_on`:** A variable with `distribution: conditional` but no
  `depends_on` key raises a `ValueError`.
- **Missing parent:** If `depends_on` references a variable name not in the
  `variables` list, raises a `ValueError`.
- **Circular dependency:** If A depends on B and B depends on A (directly or
  transitively), raises a `ValueError`.
- **Unmatched condition:** If the parent's sampled value has no matching key
  in `conditions`, raises a `ValueError`. Ensure every possible parent value
  has a corresponding condition entry.

---

## Variable Name Mapping

Every variable `name` must correspond to a target in the template
simulation package. OSimFlow supports two mapping mechanisms:

### Measure Arguments (.osw)

Variables can target arguments defined in the `workflow.osw` steps. The
name must match an argument key inside a step's `arguments` dict.

Given this `workflow.osw`:

```json
{
  "steps": [
    {
      "measure_dir_name": "SetThermostatSchedule",
      "arguments": {
        "heating_setpoint": 20.0,
        "cooling_setpoint": 25.0
      }
    },
    {
      "measure_dir_name": "SetEnvelopePerformance",
      "arguments": {
        "wwr": 0.4,
        "wall_r_value": 3.5
      }
    }
  ]
}
```

Valid variable names:

```yaml
- name: wwr               # matches SetEnvelopePerformance.wwr
  distribution: uniform
  min: 0.15
  max: 0.80

- name: heating_setpoint  # matches SetThermostatSchedule.heating_setpoint
  distribution: normal
  mean: 20.0
  sigma: 1.0
```

#### Disambiguation with Dotted Names

When the same argument name appears in multiple measures, use the dotted
form `MeasureName.argument_name`:

```yaml
- name: SetEnvelopePerformance.heating_setpoint
  distribution: uniform
  min: 16.0
  max: 20.0
```

### Model Attributes (.osm)

Variables can target `.osm` model attributes using the dotted notation
`ObjectType_InstanceName.attribute`:

```yaml
- name: SpaceType_Office.lighting_power_density
  distribution: uniform
  min: 5.0
  max: 15.0

- name: Construction_ExtWall.u_value
  distribution: uniform
  min: 0.5
  max: 3.5

- name: ThermalZone_Core.cooling_setpoint
  distribution: normal
  mean: 24.0
  sigma: 1.0
```

Supported object types and their discoverable attributes:

| Object Type | Attribute | Setter |
|---|---|---|
| `SpaceType` | `lighting_power_density` | `setLightingPowerPerFloorArea` |
| `ThermalZone` | `cooling_setpoint` | Creates `ScheduleConstant` |
| `ThermalZone` | `heating_setpoint` | Creates `ScheduleConstant` |
| `Construction` | `u_value` | `setThermalConductance` |
| `Lights` | `lighting_level` | `setLightingLevel` |
| `People` | `people_per_floor_area` | `setPeopleperSpaceFloorArea` |

### Priority

When a template directory contains both `workflow.osw` and `model.osm`,
measure arguments (from `.osw`) take priority over model attributes (from
`.osm`) for plain (non-dotted) name collisions.

---

## Pre-flight Parameter Validation

Before any simulation runs, OSimFlow performs a **pre-flight validation
pass** (PRD §1.4) that checks every variable name against the template.
This catches typos and misconfigurations early.

### What is validated

1. **Existence check** — every variable name must appear in the union of
   measure arguments and `.osm` attributes discovered from the template.
2. **Ambiguity check** — if a plain argument name (no dot) appears in
   multiple measures, the user must switch to the dotted form.
3. **EPW file existence** — for variables with `target: epw_file`, every
   mapped `.epw` file must exist inside the template package.
4. **EPW format validation** — every referenced `.epw` file must have a
   valid header (first line starts with `LOCATION`).
5. **.osm path validation** — dotted attribute names like
   `SpaceType_Office.lighting_power_density` are validated against the
   loaded model to confirm the referenced object exists.

### Error behavior

When validation fails, the campaign stops immediately with a diagnostic
message:

```
PRE-FLIGHT VALIDATION FAILED:

  Parameter 'window_u_valu' not found in any measure step or .osm attribute.
  Did you mean 'window_u_value'?
```

Unsupported distributions produce a clear message listing all supported
options:

```
unsupported distribution 'poisson'; choose from uniform, lognormal, normal,
triangular, beta, gamma, exponential, discrete, categorical
```

---

## Advanced Patterns

### Bounded Normal Distribution

The `normal` distribution can produce negative values. To enforce a
physical bound (e.g., positive-only), use `beta` with large equal shape
parameters, or use `lognormal` for strictly positive values:

```yaml
# Instead of normal with potential negatives:
#   distribution: normal
#   mean: 0.5
#   sigma: 0.3
# Use lognormal for strictly positive infiltration rates:
- name: infiltration_rate
  distribution: lognormal
  mean: -0.7     # median = exp(-0.7) ≈ 0.5 ACH
  sigma: 0.5
```

### ASHRAE 90.1 Window-to-Wall Ratio

ASHRAE 90.1 limits WWR to 0.40 for most climate zones, with performance
path allowances up to 0.80. Use a bounded `beta` distribution to concentrate
samples in the code-compliant range:

```yaml
- name: wwr
  distribution: beta
  alpha: 2.0
  beta: 3.0
  loc: 0.15
  scale: 0.65   # produces values in [0.15, 0.80]
```

### Standardized Performance Tiers

Use `discrete` for parameters that come in standard increments:

```yaml
- name: wall_r_value
  distribution: discrete
  values: [2.5, 3.5, 5.0, 7.0, 10.0]
```

### Multi-Climate Campaign

Use `categorical` with `target: epw_file` to sweep across climate zones:

```yaml
- name: climate_file
  distribution: categorical
  values: ["2A_Hot_Humid", "4A_Mixed_Humid", "5A_Cool_Humid", "6A_Cold"]
  mapping:
    2A_Hot_Humid: weather/USA_FL_Tampa.epw
    4A_Mixed_Humid: weather/USA_IL_Chicago.epw
    5A_Cool_Humid: weather/USA_PA_Philadelphia.epw
    6A_Cold: weather/USA_MN_Duluth.epw
  target: epw_file
```

### HVAC Efficiency by System Type

A common BEM pattern: cooling efficiency (COP) ranges depend on the HVAC
system type. Use `conditional` to enforce physically realistic constraints:

```yaml
variables:
  - name: hvac_system_type
    distribution: categorical
    values: [packaged_rooftop, vav_reheat, wshp, gshp]

  - name: cooling_cop
    distribution: conditional
    depends_on: hvac_system_type
    conditions:
      packaged_rooftop: {distribution: uniform, min: 2.5, max: 4.0}
      vav_reheat: {distribution: uniform, min: 3.0, max: 5.0}
      wshp: {distribution: uniform, min: 3.5, max: 6.0}
      gshp: {distribution: uniform, min: 4.0, max: 8.0}
```

### Window Performance by Construction Type

Window U-value and SHGC ranges depend on the glazing type:

```yaml
variables:
  - name: glazing_type
    distribution: categorical
    values: [single_clear, double_low_e, triple_low_e]

  - name: window_u_value
    distribution: conditional
    depends_on: glazing_type
    conditions:
      single_clear: {distribution: discrete, values: [5.8]}
      double_low_e: {distribution: uniform, min: 1.1, max: 2.0}
      triple_low_e: {distribution: uniform, min: 0.5, max: 1.2}
```

### Baseline Comparison

Define a `baseline` section to include a fixed ASHRAE 90.1 baseline
reference sample. KPI improvement percentages are computed relative to
this sample:

```yaml
baseline:
  sample_id: baseline_90_1
  parameters:
    window_u_value: 3.5
    infiltration_rate: 0.7
    lighting_power_density: 10.0
    wwr: 0.40
```

---

## Complete Example

The following `variables.yml` demonstrates all supported distributions for
a realistic building energy parametric study targeting the
`example_package/` template:

```yaml
# variables.yml — parametric study for a commercial office building.
#
# Targets the example_package/ template which contains:
#   workflow.osw — SetThermostatSchedule, SetEnvelopePerformance measures
#   model.osm    — window_u_value, infiltration_rate, hvac_setpoint,
#                  lighting_power_density, thermal_conductivity,
#                  internal_gain, equipment_lifetime

variables:
  # Window U-value: continuous uniform 1.0–5.0 W/m²·K
  # Covers single-pane (5.0) through triple-pane low-e (1.0).
  - name: window_u_value
    distribution: uniform
    min: 1.0
    max: 5.0

  # Air infiltration rate: lognormal, right-skewed, strictly positive.
  # Median ≈ exp(0.5) ≈ 1.65 ACH.
  - name: infiltration_rate
    distribution: lognormal
    mean: 0.5
    sigma: 0.2

  # HVAC setpoint: normal distribution centered on 22°C ± 1°C.
  # Most samples fall in [20, 24]°C.
  - name: hvac_setpoint
    distribution: normal
    mean: 22.0
    sigma: 1.0

  # Lighting power density: triangular, peak at 10 W/m².
  # Range 5–15 W/m² covers ASHRAE 90.1 compliant to high-intensity.
  - name: lighting_power_density
    distribution: triangular
    min: 5.0
    max: 15.0
    mode: 10.0

  # Thermal conductivity: beta on [0.1, 2.1].
  # Left-skewed: most values concentrated below the midpoint.
  - name: thermal_conductivity
    distribution: beta
    alpha: 2.0
    beta: 5.0
    loc: 0.1
    scale: 2.0

  # Internal heat gain: gamma, strictly positive, right-skewed.
  - name: internal_gain
    distribution: gamma
    alpha: 2.0
    loc: 0.0
    scale: 5.0

  # Equipment lifetime: exponential, models time-to-replacement.
  # Mean lifetime = 1/rate = 10 years.
  - name: equipment_lifetime
    distribution: exponential
    rate: 10.0

  # Window-to-wall ratio: bounded beta in [0.15, 0.80] per ASHRAE 90.1.
  # Disambiguated with dotted name targeting the SetEnvelopePerformance measure.
  - name: SetEnvelopePerformance.wwr
    distribution: beta
    alpha: 2.0
    beta: 3.0
    loc: 0.15
    scale: 0.65

  # Wall R-value: discrete standard increments.
  - name: SetEnvelopePerformance.wall_r_value
    distribution: discrete
    values: [2.5, 3.5, 5.0, 7.0, 10.0]

  # Climate zone sweep: categorical selection with EPW file mapping.
  - name: climate_zone
    distribution: categorical
    values: ["hot_humid", "mixed_humid", "cold"]
    mapping:
      hot_humid: weather/USA_FL_Miami.epw
      mixed_humid: weather/USA_IL_Chicago.epw
      cold: weather/USA_MN_Duluth.epw
    target: epw_file

# Optional: ASHRAE 90.1 baseline for comparison.
baseline:
  sample_id: baseline_90_1
  parameters:
    window_u_value: 3.5
    infiltration_rate: 0.7
    hvac_setpoint: 22.0
    lighting_power_density: 10.0
    thermal_conductivity: 0.5
    internal_gain: 5.0
    equipment_lifetime: 10.0
    climate_zone: mixed_humid
```

---

## Distribution Quick Reference

| Distribution | Parameters | Support | Typical BEM Use |
|---|---|---|---|
| `uniform` | `min`, `max` | [min, max] | U-values, WWR, R-values |
| `lognormal` | `mean`, `sigma` | (0, +∞) | Infiltration rates, airflow |
| `normal` | `mean`, `sigma` | (-∞, +∞) | Temperature setpoints |
| `triangular` | `min`, `max`, `mode` | [min, max] | LPD, equipment loads |
| `beta` | `alpha`, `beta`, `loc`, `scale` | [loc, loc+scale] | Bounded ratios, WWR |
| `gamma` | `alpha`, `loc`, `scale` | [loc, +∞) | Internal gains, occupancy |
| `exponential` | `rate` | [0, +∞) | Equipment lifetime |
| `discrete` | `values` | listed values | Floor count, R-value tiers |
| `categorical` | `values`, `mapping`, `target` | listed labels | Climate zone, HVAC type |
| `conditional` | `depends_on`, `conditions` | varies | Efficiency by system type |
