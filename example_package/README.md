# example_package — Minimal 1-Zone Shoebox Office

Minimal simulation package used as the default `--template_sim_package`
for integration tests, performance benchmarks, and the README quickstart.

## Model description

A 1-zone rectangular office building (shoebox model):

| Property | Value |
|---|---|
| Building type | Office |
| Floor area | 46.5 m² (10m × 4.65m) |
| Stories | 1 |
| Thermal zones | 1 |
| HVAC | Ideal loads air system |
| Window | South-facing, 40% WWR |
| Climate | Chicago TMY3 (USA_IL_Chicago-OHare) |
| OpenStudio version | 3.6.1+ |

### Constructions

- **Exterior walls**: Concrete finish (20mm) + insulation (100mm, k=0.04 W/mK, R≈2.5 m²K/W)
- **Roof**: Membrane (10mm) + insulation (100mm, k=0.04 W/mK, R≈2.5 m²K/W)
- **Floor**: Concrete slab on grade (150mm)
- **Window**: Simple glazing (U≈3.0 W/m²K)

### Internal loads

| Load | Value | Schedule |
|---|---|---|
| Lighting | 10 W/m² | Always On |
| People | 0.05 people/m² | Always On |
| Equipment | 5 W/m² | Always On |
| Infiltration | 0.0003 m³/s per m² exterior | Always On |

### Thermostat

- Heating setpoint: 20°C (constant)
- Cooling setpoint: 25°C (constant)

## File format conventions

### model.osm (test-mode JSON)

The `.osm` file uses the **test-mode JSON convention** documented in
[`osimflow/apply_params.py`](../osimflow/apply_params.py). This convention
allows the parameter application logic to run on hosts that do NOT have the
OpenStudio Python bindings installed. The `attributes` object maps parameter
names to their default values.

When the OpenStudio bindings ARE available (e.g. inside the
`nrel/openstudio:<version>` container), the apply logic can also handle real
OSM files. For actual simulation with `openstudio.cli run`, generate a real
`.osm` using the OpenStudio Application or SDK.

### workflow.osw

The `.osw` (OpenStudio Workflow) file is a JSON file that defines the
simulation workflow. It contains:

- `seed_file`: references `model.osm`
- `steps`: array of measures to apply before simulation

The example includes two measures:
1. **SetThermostatSchedule** — sets heating/cooling setpoints
2. **SetEnvelopePerformance** — sets window-to-wall ratio and wall R-value

## Parameterizable variables

The default values in `model.osm` and `workflow.osw` map to the `variables.yml`
at the repository root. The following parameters are available for parametric
study:

| Variable | Location | Default | Distribution |
|---|---|---|---|
| `window_u_value` | model.osm attribute | 3.0 | uniform(1.0, 5.0) |
| `infiltration_rate` | model.osm attribute | 0.7 | lognormal(0.5, 0.2) |
| `hvac_setpoint` | model.osm attribute | 22.0 | normal(22.0, 1.0) |
| `lighting_power_density` | model.osm attribute | 10.0 | triangular(5.0, 15.0, 10.0) |
| `thermal_conductivity` | model.osm attribute | 0.5 | beta(2.0, 5.0) |
| `internal_gain` | model.osm attribute | 5.0 | gamma(2.0, 5.0) |
| `equipment_lifetime` | model.osm attribute | 15.0 | exponential(10.0) |

Additionally, the `.osw` exposes these measure arguments (usable via the dotted
form `MeasureName.argument` in `variables.yml`):

| Measure argument | Default |
|---|---|
| `SetThermostatSchedule.heating_setpoint` | 20.0 |
| `SetThermostatSchedule.cooling_setpoint` | 25.0 |
| `SetEnvelopePerformance.wwr` | 0.4 |
| `SetEnvelopePerformance.wall_r_value` | 3.5 |

## Usage

```bash
osimflow run \
  --executor local \
  --input_variables variables.yml \
  --template_sim_package ./example_package \
  --n_samples 5 \
  --outdir ./results
```

## Weather file

No weather file (`.epw`) is bundled with this package. When running with
`openstudio.cli`, provide a weather file via the `weather_file` field in
`workflow.osw` or use the `--epw_file` target in `variables.yml`. For local
testing without the real OpenStudio CLI, the stub mode does not require a
weather file.
