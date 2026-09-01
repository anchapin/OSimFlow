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

### model.osm (committed test-mode JSON placeholder)

The committed `model.osm` uses the **test-mode JSON convention** documented in
[`osimflow/apply_params.py`](../osimflow/apply_params.py). This convention
allows the parameter-application logic to run on hosts that do NOT have the
OpenStudio Python bindings installed, which is what the stub-mode integration
tests (`test_preflight_validation.py`, `test_local_executor.py`,
`test_cache_resume.py`, …) rely on. The `attributes` object maps parameter
names to their default values.

This JSON placeholder **cannot** drive a real `openstudio.cli run`
invocation. To get a real, simulation-capable fixture see
[Real-sim fixture](#real-sim-fixture) below.

A snapshot of this JSON placeholder is also kept as
`model.osm.placeholder` so stub mode can always be restored after fetching a
real model (the fetcher overwrites `model.osm` in place):

```bash
cp example_package/model.osm.placeholder example_package/model.osm
```

### Real-sim fixture

A genuine OpenStudio `.osm` model and a real EnergyPlus `.epw` weather file
are **not committed** — per `AGENTS.md` §10 and the repository `.gitignore`,
`.osm` and `.epw` files are never tracked. Instead, fetch them at dev/test
time from stable public sources (NREL):

```bash
python scripts/fetch_example_fixture.py            # into ./example_package/
python scripts/fetch_example_fixture.py --force     # re-download
python scripts/fetch_example_fixture.py --dest /tmp/pkg
```

The fetcher:

- Downloads a small (~300 KB) single-zone **SmallOffice** seed model
  (`OS:Version` 1.14.0) from `NREL/openstudio-resources` — an office building
  with thermal zones, spaces, constructions, and ideal-loads HVAC, compatible
  with the thermostat/envelope measures referenced by `workflow.osw`.
- Downloads the canonical **`USA_CO_Golden-NREL.724666_TMY3.epw`** TMY3
  weather file (~1.6 MB) from the `NREL/EnergyPlus` `v24.2.0` release.
- Verifies each download (non-empty + `OS:Version` / `LOCATION` sanity check),
  retries 3× with exponential backoff, and writes via an atomic rename.
- Is idempotent: re-running prints `real fixture already present, use --force
  to refetch` and exits 0.
- Preserves the JSON placeholder as `model.osm.placeholder` on first run.

The weather file drives the actual simulation climate (EnergyPlus uses the
EPW `LOCATION` header, which overrides the model's `OS:Site`). The fetched
`SmallOffice.osm` lists "Houston Bush Intercontinental" as its site name, but
when run against the Golden `.epw` the simulation uses Golden, CO weather.

> **Note on `workflow.osw` measures.** The committed `workflow.osw` references
> `SetThermostatSchedule` and `SetEnvelopePerformance`. Since #1486 those
> measures are bundled under `example_package/measures/` and `workflow.osw`
> points at them via `"measure_paths": ["measures"]`. A real
> `openstudio.cli run -w workflow.osw` therefore resolves its measure set on
> disk with no manual BCL download. The fetcher ships the real model + weather;
> the measure set is now part of the package itself.

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

The `example_package/measures/` directory is bundled with the repository, so
`workflow.osw` resolves its `SetThermostatSchedule` and
`SetEnvelopePerformance` measures on disk without a manual BCL download. This
means the same package works for stub-mode (`make test`) and for a real
`openstudio.cli run -w workflow.osw` invocation (the nightly
`openstudio-cli-e2e` workflow, gated on
`OSIMFLOW_RUN_REAL_OPENSTUDIO=1`).

## Weather file

No weather file (`.epw`) is committed (`.epw` is gitignored). For a real
simulation, fetch the bundled Golden-NREL TMY3 file with
`python scripts/fetch_example_fixture.py` (see
[Real-sim fixture](#real-sim-fixture)) and reference it via the `weather_file`
field in `workflow.osw` or the `--epw_file` target in `variables.yml`. For
local testing without the real OpenStudio CLI, the stub mode does not require
a weather file.
