# Packaging OpenStudio Measures into `template_sim_package`

> **Audience:** Energy modelers and researchers who want to run parametric
> campaigns with custom OpenStudio measures. This guide covers how to package
> measures and their dependencies so that `osimflow run` can find and execute
> them reliably across local, HPC (Slurm), and cloud (AWS Batch) environments.

## Table of contents

- [1. What is a `template_sim_package`](#1-what-is-a-template_sim_package)
- [2. OpenStudio measures explained](#2-openstudio-measures-explained)
- [3. Packaging Ruby measures](#3-packaging-ruby-measures)
- [4. Packaging Python measures](#4-packaging-python-measures)
- [5. Step-by-step: Package a BCL measure](#5-step-by-step-package-a-bcl-measure)
- [6. Step-by-step: Package a custom measure](#6-step-by-step-package-a-custom-measure)
- [7. Testing your package locally](#7-testing-your-package-locally)
- [8. Common mistakes and troubleshooting](#8-common-mistakes-and-troubleshooting)
- [9. Advanced patterns](#9-advanced-patterns)

---

## 1. What is a `template_sim_package`

A `template_sim_package` is a **self-contained directory** that holds everything
`openstudio.cli run` needs to execute a simulation: the seed model, the workflow
definition, weather files, and any measures (with their dependencies).

### Required files

| File | Purpose |
|---|---|
| `workflow.osw` | OpenStudio Workflow JSON — tells the CLI which measures to run and in what order. |
| `model.osm` | Seed model (referenced by `workflow.osw`'s `"seed_file"` field). |

### Optional files

| File / Directory | Purpose |
|---|---|
| `*.epw` | EnergyPlus weather file (referenced by `"weather_file"` in `workflow.osw`). |
| `measures/` | Directory containing measure scripts. Referenced via `"measure_paths"` in `workflow.osw`. |
| `resources/` | Supporting data files (schedules, constructions, etc.) used by measures. |
| `Gemfile` | Ruby gem dependencies for Ruby-based measures (see [§3](#3-packaging-ruby-measures)). |
| `requirements.txt` | Python dependencies for Python-based measures (see [§4](#4-packaging-python-measures)). |

### Minimal directory structure

This is the layout used by the project's `example_package/`:

```
template_sim_package/
├── model.osm              # Seed model
├── workflow.osw            # Workflow definition
├── weather.epw             # Weather file (optional; can be empty string in .osw)
└── README.md               # Documentation for the package (optional)
```

### Directory structure with measures

When you add measures, the conventional layout is:

```
template_sim_package/
├── model.osm
├── workflow.osw
├── weather.epw
├── measures/
│   ├── SetThermostatSchedule/
│   │   ├── measure.rb              # or measure.py
│   │   ├── measure.xml             # BCL metadata (optional but recommended)
│   │   └── resources/
│   │       └── schedules.csv       # Supporting data (if any)
│   ├── SetEnvelopePerformance/
│   │   ├── measure.rb
│   │   └── measure.xml
│   └── MyCustomMeasure/
│       ├── measure.py
│       ├── measure.xml
│       └── requirements.txt        # Python deps (Python measures only)
├── Gemfile                         # Ruby gem deps (Ruby measures only)
└── README.md
```

### How the `.osw` references measures

The `workflow.osw` file controls measure discovery. There are two mechanisms:

**1. `measure_paths` array** — directories the CLI searches for measures:

```json
{
  "seed_file": "model.osm",
  "weather_file": "weather.epw",
  "measure_paths": ["measures"],
  "steps": [
    {
      "measure_dir_name": "SetThermostatSchedule",
      "arguments": { "heating_setpoint": 20.0, "cooling_setpoint": 25.0 }
    }
  ]
}
```

When `measure_paths` is `["measures"]`, the CLI looks for
`measures/SetThermostatSchedule/measure.rb` (or `measure.py`).

**2. Empty `measure_paths` (default)** — when `measure_paths` is `[]` or
omitted, the CLI searches the directory containing the `.osw` file and its
`measures/` subdirectory.

### How OSimFlow uses the package

During a campaign, OSimFlow:

1. **Copies** the entire `template_sim_package` to a per-sample work directory.
2. **Modifies** `workflow.osw` argument values based on each LHS sample (via the
   `apply_parameters` step).
3. **Invokes** `openstudio.cli run -w workflow.osw` inside the container
   (or locally when the CLI is on `$PATH`).

Because the package is copied as a unit, **all measure dependencies must be
self-contained within the directory**. There is no opportunity to install gems
or pip packages at simulation time — the compute nodes may not have internet
access, and installing at runtime would break reproducibility.

---

## 2. OpenStudio measures explained

A **measure** is a script that modifies an OpenStudio model or workflow before
simulation. Measures are the primary mechanism for parametric variation in
OSimFlow.

### Measure anatomy (Ruby)

```
MyMeasure/
├── measure.rb          # The measure script
├── measure.xml         # Metadata: name, description, arguments schema
└── tests/              # Optional: unit tests
    └── my_measure_test.rb
```

A Ruby measure implements the `OpenStudio::Measure::ModelMeasure` interface:

```ruby
class MyMeasure < OpenStudio::Measure::ModelMeasure
  def name
    return "My Custom Measure"
  end

  def description
    return "Does something useful to the model."
  end

  def arguments(model)
    args = OpenStudio::Measure::OSArgumentVector.new
    arg = OpenStudio::Measure::OSArgument.makeDoubleArgument("r_value", true)
    arg.setDefaultValue(3.5)
    args << arg
    return args
  end

  def run(model, runner, user_arguments)
    super(model, runner, user_arguments)
    r_value = runner.getDoubleArgumentValue("r_value", user_arguments)
    runner.registerInfo("Setting R-value to #{r_value}")
    # ... modify the model ...
    return true
  end
end
```

### Measure anatomy (Python)

Python measures are supported in OpenStudio 3.x+ and follow the same directory
convention:

```
MyPythonMeasure/
├── measure.py          # The measure script
├── measure.xml         # Metadata (same schema as Ruby)
└── requirements.txt    # Python dependencies (if any)
```

A Python measure uses the `openstudio.measure` module:

```python
import openstudio


class MyPythonMeasure(openstudio.measure.ModelMeasure):
    def name(self):
        return "My Python Measure"

    def description(self):
        return "Does something useful to the model."

    def arguments(self, model):
        args = openstudio.measure.OSArgumentVector()
        arg = openstudio.measure.OSArgument.makeDoubleArgument("r_value", True)
        arg.setDefaultValue(3.5)
        args.append(arg)
        return args

    def run(self, model, runner, user_arguments):
        super().run(model, runner, user_arguments)
        r_value = runner.getDoubleArgumentValue("r_value", user_arguments)
        runner.registerInfo(f"Setting R-value to {r_value}")
        # ... modify the model ...
        return True
```

### The `measure.xml` file

The XML file declares the measure's metadata and argument schema. It is used
by the BCL and by `openstudio.cli` for validation:

```xml
<measure>
  <schema_version>3.1</schema_version>
  <name>My Custom Measure</name>
  <uid>a1b2c3d4-e5f6-7890-abcd-ef1234567890</uid>
  <version_id>f9e8d7c6-b5a4-3210-fedc-ba0987654321</version_id>
  <description>Modifies envelope performance.</description>
  <modeler_description>Sets wall R-value and window-to-wall ratio.</modeler_description>
  <arguments>
    <argument>
      <name>r_value</name>
      <display_name>Wall R-Value</display_name>
      <description>Thermal resistance of exterior walls (m2K/W).</description>
      <type>Double</type>
      <required>true</required>
      <default_value>3.5</default_value>
    </argument>
  </arguments>
</measure>
```

---

## 3. Packaging Ruby measures

### BCL measures (no custom gem dependencies)

Measures from the [Building Component Library](https://bcl.nrel.gov/) are
self-contained — they do not require additional gems beyond what ships with
the `nrel/openstudio` container. To use them:

1. Download the measure from BCL (see [§5](#5-step-by-step-package-a-bcl-measure)).
2. Place it in `measures/<measure_dir_name>/`.
3. Add a `steps` entry in `workflow.osw` with matching `measure_dir_name`.

No `Gemfile` is needed.

### Custom Ruby measures with gem dependencies

If your measure uses gems not bundled with the OpenStudio distribution:

**1. Create a `Gemfile` at the package root:**

```ruby
# Gemfile
source "https://rubygems.org"

gem "json_pure", "~> 2.6"
gem "minitest", "~> 5.0"
```

**2. Install gems locally into the package:**

```bash
cd template_sim_package/
bundle config set --local path "vendor/bundle"
bundle install
```

This creates `vendor/bundle/` inside the package with all gem code.

**3. Require gems in your measure:**

```ruby
# In measure.rb
require "json_pure"
require "minitest"
```

**4. Verify the Gemfile is committed with the package.**

The resulting directory:

```
template_sim_package/
├── model.osm
├── workflow.osw
├── measures/
│   └── MyRubyMeasure/
│       ├── measure.rb
│       └── measure.xml
├── Gemfile
├── Gemfile.lock
└── vendor/
    └── bundle/
        └── ruby/
            └── 3.2.0/
                └── gems/
                    ├── json_pure-2.6.1/
                    └── minitest-5.20.0/
```

> **Important:** `vendor/bundle/` can be large. Consider adding a
> `.gitattributes` or using `git-lfs` if the gem tree exceeds a few MB.
> Alternatively, include only the `Gemfile` and `Gemfile.lock` and run
> `bundle install --deployment` as part of the container build (if you
> build a custom image).

### Version compatibility

The Ruby version inside the `nrel/openstudio` container varies by
OpenStudio version. Check the container's Ruby version before pinning gems:

```bash
docker run --rm nrel/openstudio:3.7.0 ruby --version
```

Pin gem versions in the `Gemfile` that are compatible with the container's
Ruby version and the OpenStudio SDK version.

---

## 4. Packaging Python measures

Python measures run inside the OpenStudio Python environment, which ships with
the `nrel/openstudio` container. Dependencies that are not part of the standard
library or the container's Python environment must be bundled.

### Using `requirements.txt`

**1. Create `requirements.txt` inside the measure directory:**

```
# measures/MyPythonMeasure/requirements.txt
numpy>=1.24,<2.0
pandas>=2.0
```

**2. Install dependencies into the measure's local directory:**

```bash
pip install --target measures/MyPythonMeasure/vendor \
    -r measures/MyPythonMeasure/requirements.txt
```

**3. Add the vendor path to `sys.path` in your measure:**

```python
# measures/MyPythonMeasure/measure.py
import sys
from pathlib import Path

_vendor = Path(__file__).parent / "vendor"
if _vendor.is_dir() and str(_vendor) not in sys.path:
    sys.path.insert(0, str(_vendor))

import numpy as np  # noqa: E402 (after path manipulation)
import pandas as pd  # noqa: E402
```

**4. Verify `vendor/` is included with the package.**

Resulting directory:

```
template_sim_package/
├── model.osm
├── workflow.osw
├── measures/
│   └── MyPythonMeasure/
│       ├── measure.py
│       ├── measure.xml
│       ├── requirements.txt
│       └── vendor/
│           ├── numpy/
│           ├── pandas/
│           └── ...
└── weather.epw
```

### OpenStudio Python bindings

The `openstudio` Python package is **already installed** inside the
`nrel/openstudio` container. Do not add it to `requirements.txt`. If you are
testing locally without the container, install it separately:

```bash
pip install openstudio==3.7.0
```

See [§7](#7-testing-your-package-locally) for local testing strategies.

### Container Python version

Check the Python version in the container you plan to use:

```bash
docker run --rm nrel/openstudio:3.7.0 python3 --version
```

Pin dependency versions compatible with that Python version.

---

## 5. Step-by-step: Package a BCL measure

This example packages the "Set Window to Wall Ratio by Facade" measure from the
NREL Building Component Library.

### Step 1: Find the measure on BCL

Browse to [https://bcl.nrel.gov/](https://bcl.nrel.gov/) and search for
"Window to Wall Ratio". Find the measure and note its directory name (e.g.,
`SetWindowToWallRatioByFacade`).

### Step 2: Download the measure

Use the OpenStudio CLI to download the measure directly:

```bash
openstudio.cli measure download BCL SetWindowToWallRatioByFacade
```

Or download the `.zip` from the BCL website and extract it.

### Step 3: Place the measure in your package

```bash
mkdir -p template_sim_package/measures
cp -r SetWindowToWallRatioByFacade template_sim_package/measures/
```

### Step 4: Update `workflow.osw`

```json
{
  "seed_file": "model.osm",
  "weather_file": "weather.epw",
  "measure_paths": ["measures"],
  "steps": [
    {
      "measure_dir_name": "SetWindowToWallRatioByFacade",
      "arguments": {
        "wwr": 0.4,
        "offset": 0.8,
        "facade": "South"
      }
    }
  ]
}
```

### Step 5: Verify the directory structure

```
template_sim_package/
├── model.osm
├── workflow.osw
├── weather.epw
└── measures/
    └── SetWindowToWallRatioByFacade/
        ├── measure.rb
        └── measure.xml
```

### Step 6: Test locally

```bash
cd template_sim_package/
openstudio.cli run -w workflow.osw
```

See [§7](#7-testing-your-package-locally) for detailed testing instructions.

---

## 6. Step-by-step: Package a custom measure

This example creates a minimal custom Ruby measure that sets the lighting
power density (LPD) for all spaces in a model.

### Step 1: Create the measure directory

```bash
mkdir -p template_sim_package/measures/SetLightingPowerDensity
```

### Step 2: Write `measure.xml`

```xml
<?xml version="1.0"?>
<measure>
  <schema_version>3.1</schema_version>
  <name>Set Lighting Power Density</name>
  <uid>12345678-1234-5678-1234-567812345678</uid>
  <version_id>87654321-4321-8765-4321-876543218765</version_id>
  <description>Sets the lighting power density for all spaces.</description>
  <modeler_description>
    Iterates over all spaces and sets the lighting density (W/ft2).
  </modeler_description>
  <arguments>
    <argument>
      <name>lpd_w_per_ft2</name>
      <display_name>Lighting Power Density (W/ft2)</display_name>
      <type>Double</type>
      <required>true</required>
      <default_value>1.0</default_value>
    </argument>
  </arguments>
</measure>
```

### Step 3: Write `measure.rb`

```ruby
class SetLightingPowerDensity < OpenStudio::Measure::ModelMeasure
  def name
    return "Set Lighting Power Density"
  end

  def description
    return "Sets the lighting power density for all spaces in the model."
  end

  def arguments(model)
    args = OpenStudio::Measure::OSArgumentVector.new
    arg = OpenStudio::Measure::OSArgument.makeDoubleArgument("lpd_w_per_ft2", true)
    arg.setDefaultValue(1.0)
    arg.setDescription("Lighting power density in W/ft2")
    args << arg
    return args
  end

  def run(model, runner, user_arguments)
    super(model, runner, user_arguments)

    lpd = runner.getDoubleArgumentValue("lpd_w_per_ft2", user_arguments)
    runner.registerInfo("Setting LPD to #{lpd} W/ft2 for all spaces")

    model.getSpaces.each do |space|
      space.setLightingPowerPerFloorArea(lpd)
    end

    return true
  end
end

SetLightingPowerDensity.new.registerWithApplication
```

### Step 4: Update `workflow.osw`

```json
{
  "seed_file": "model.osm",
  "weather_file": "weather.epw",
  "measure_paths": ["measures"],
  "steps": [
    {
      "measure_dir_name": "SetLightingPowerDensity",
      "arguments": {
        "lpd_w_per_ft2": 1.0
      }
    }
  ]
}
```

### Step 5: Test

```bash
cd template_sim_package/
openstudio.cli run -w workflow.osw
```

### Step 6: Add to `variables.yml` for parametric variation

To sweep LPD in a campaign, add the measure argument to your `variables.yml`
using the dotted form `MeasureName.argument`:

```yaml
variables:
  - name: SetLightingPowerDensity.lpd_w_per_ft2
    distribution:
      type: uniform
      min: 0.5
      max: 1.5
```

See [§9.3](#93-measure-argument-passing-via-variablesyml) for more details.

---

## 7. Testing your package locally

### Prerequisites

You need the OpenStudio CLI on your `$PATH`. Options:

- **Docker** (recommended — matches the production container):

  ```bash
  alias openstudio.cli='docker run --rm -v "$(pwd)":/work -w /work nrel/openstudio:3.7.0 openstudio.cli'
  ```

- **Direct installation** — install OpenStudio from
  [https://openstudio.net/](https://openstudio.net/) and add the CLI to
  your PATH.

### Validation checklist

Run these checks before submitting the package to a campaign:

**1. Directory structure is correct:**

```bash
ls template_sim_package/workflow.osw
ls template_sim_package/model.osm
ls -R template_sim_package/measures/
```

**2. `workflow.osw` is valid JSON:**

```bash
python3 -c "import json; json.load(open('template_sim_package/workflow.osw'))"
```

**3. All referenced measures exist:**

```bash
cd template_sim_package/
python3 -c "
import json
osw = json.load(open('workflow.osw'))
for step in osw.get('steps', []):
    name = step['measure_dir_name']
    import os
    for mp in osw.get('measure_paths', ['measures', '.']):
        path = os.path.join(mp, name, 'measure.rb')
        path_py = os.path.join(mp, name, 'measure.py')
        if os.path.exists(path) or os.path.exists(path_py):
            print(f'  OK: {name}')
            break
    else:
        print(f'  MISSING: {name}')
"
```

**4. The CLI can run the workflow:**

```bash
cd template_sim_package/
openstudio.cli run -w workflow.osw
echo "Exit code: $?"
```

A successful run produces `run/` directory with EnergyPlus output files
(`eplusout.sql`, `eplusout.err`, etc.).

**5. Check the error file for warnings:**

```bash
grep -i "severe\|fatal" template_sim_package/run/eplusout.err || echo "No severe errors"
```

### Testing with OSimFlow (stub mode)

You can test the full campaign pipeline without real simulations by using the
built-in stub mode:

```bash
OSIMFLOW_STUB_SIM=1 osimflow run \
  --executor local \
  --template_sim_package ./template_sim_package \
  --input_variables variables.yml \
  --n_samples 3 \
  --outdir ./test_results
```

Set `OSIMFLOW_RUN_REAL_OPENSTUDIO=1` (and have the CLI on PATH) to test with
actual simulations.

### Testing inside the container

To exactly replicate the production environment:

```bash
docker run --rm \
  -v "$(pwd)/template_sim_package":/work \
  -w /work \
  nrel/openstudio:3.7.0 \
  openstudio.cli run -w workflow.osw
```

---

## 8. Common mistakes and troubleshooting

### Forgetting to include measure dependencies

**Symptom:** Measure fails with `LoadError: cannot load such file -- <gem_name>`
or `ModuleNotFoundError: No module named <package>`.

**Fix:** All gems and pip packages must be installed *inside* the
`template_sim_package` directory. Use `bundle install --path vendor/bundle`
(for Ruby) or `pip install --target measures/Name/vendor` (for Python). See
[§3](#3-packaging-ruby-measures) and [§4](#4-packaging-python-measures).

**Rule of thumb:** The compute node may not have internet access. If a
dependency isn't bundled, the measure will fail.

### Wrong directory structure

**Symptom:** `openstudio.cli` reports `Could not find measure 'MyMeasure'`.

**Common causes:**

1. **Measure directory name doesn't match `measure_dir_name` in `.osw`.**
   The directory name under `measures/` must exactly match the
   `measure_dir_name` field in `workflow.osw`.

   ```json
   // workflow.osw
   { "measure_dir_name": "SetThermostatSchedule" }
   ```

   ```
   measures/
   └── SetThermostatSchedule/    <-- must match exactly (case-sensitive)
       └── measure.rb
   ```

2. **`measure_paths` doesn't point to the right directory.** If your measures
   are in `measures/`, the `.osw` must include `"measure_paths": ["measures"]`.

3. **Nested directory.** The measure script must be directly inside the measure
   directory, not in a subdirectory:

   ```
   # WRONG
   measures/SetThermostatSchedule/src/measure.rb

   # CORRECT
   measures/SetThermostatSchedule/measure.rb
   ```

### Hardcoded paths

**Symptom:** Measure works locally but fails in the container or on HPC.

**Common cause:** The measure or workflow uses absolute paths like
`/Users/me/project/model.osm`.

**Fix:** Use relative paths everywhere in `workflow.osw` and in measure code:

```json
{
  "seed_file": "model.osm",
  "weather_file": "weather.epw"
}
```

In Ruby:

```ruby
# WRONG
File.read("/home/user/project/data/schedules.csv")

# CORRECT
File.read(File.join(File.dirname(__FILE__), "resources", "schedules.csv"))
```

OSimFlow copies the entire package to a per-sample work directory before running,
so all paths must be relative to the package root.

### Version mismatches between OpenStudio and measure API

**Symptom:** `NoMethodError`, `undefined method`, or Python `AttributeError`
when the measure calls an OpenStudio SDK method.

**Common cause:** The measure was written for a different OpenStudio version
than the one selected via `--openstudio_version`.

**Fix:**

1. Check which OpenStudio version you're using:
   ```bash
   docker run --rm nrel/openstudio:3.7.0 openstudio.cli openstudio_version
   ```

2. Review the [OpenStudio SDK changelog](https://openstudio.net/docs/cli/)
   for breaking API changes between versions.

3. Pin the container version explicitly when testing:
   ```bash
   osimflow run --openstudio_version 3.7.0 ...
   ```

4. If downloading measures from BCL, check the measure's compatibility
   information in `measure.xml`:
   ```xml
   <openstudio_version>3.7.0</openstudio_version>
   ```

### Case sensitivity

Linux containers (and HPC systems) have case-sensitive filesystems. A measure
directory named `setthermostatschedule` will not be found if the `.osw` says
`SetThermostatSchedule`. Always match case exactly.

### Missing `weather.epw`

**Symptom:** Simulation runs but produces no useful output, or OpenStudio
reports a warning about missing weather data.

**Fix:** Either include the `.epw` file in the package and reference it in
`workflow.osw`, or leave `"weather_file": ""` and use OSimFlow's weather
discovery (`--weather_dir`) or download features.

---

## 9. Advanced patterns

### 9.1 Multiple measures in a workflow

You can chain multiple measures in `workflow.osw`. They execute in order —
each measure sees the model as modified by the previous ones:

```json
{
  "seed_file": "model.osm",
  "weather_file": "weather.epw",
  "measure_paths": ["measures"],
  "steps": [
    {
      "measure_dir_name": "SetEnvelopePerformance",
      "arguments": { "wwr": 0.4, "wall_r_value": 3.5 }
    },
    {
      "measure_dir_name": "SetThermostatSchedule",
      "arguments": { "heating_setpoint": 20.0, "cooling_setpoint": 25.0 }
    },
    {
      "measure_dir_name": "SetLightingPowerDensity",
      "arguments": { "lpd_w_per_ft2": 1.0 }
    }
  ]
}
```

**Order matters.** If `SetEnvelopePerformance` adds windows and
`SetThermostatSchedule` modifies zones, the order may affect results. Place
geometry-modifying measures first, then operational parameter measures.

### 9.2 Conditional measure execution

OpenStudio measures support an `"__SKIP__"` argument value. When set to `true`,
the measure is skipped:

```json
{
  "measure_dir_name": "SetThermostatSchedule",
  "arguments": {
    "__SKIP__": true,
    "heating_setpoint": 20.0,
    "cooling_setpoint": 25.0
  }
}
```

You can use this in a BYOS `apply_parameters` script to conditionally disable
measures per sample:

```python
def apply_parameters(workflow_osw_path, params):
    import json
    with open(workflow_osw_path) as f:
        osw = json.load(f)

    for step in osw["steps"]:
        if step["measure_dir_name"] == "SetThermostatSchedule":
            if params.get("skip_thermostat", False):
                step["arguments"]["__SKIP__"] = True
            else:
                step["arguments"].pop("__SKIP__", None)

    with open(workflow_osw_path, "w") as f:
        json.dump(osw, f, indent=2)
```

### 9.3 Measure argument passing via `variables.yml`

OSimFlow's parameter application step can modify measure arguments in
`workflow.osw` using the dotted form `MeasureName.argument`:

```yaml
# variables.yml
variables:
  - name: SetThermostatSchedule.heating_setpoint
    distribution:
      type: uniform
      min: 18.0
      max: 22.0

  - name: SetThermostatSchedule.cooling_setpoint
    distribution:
      type: uniform
      min: 23.0
      max: 27.0

  - name: SetEnvelopePerformance.wwr
    distribution:
      type: uniform
      min: 0.2
      max: 0.6

  - name: SetEnvelopePerformance.wall_r_value
    distribution:
      type: uniform
      min: 2.0
      max: 7.0

  - name: model.infiltration_rate
    distribution:
      type: lognormal
      mean: 0.5
      sigma: 0.2
```

The dotted form `MeasureName.argument` tells OSimFlow to set that argument in
the matching `workflow.osw` step. The `model.attribute` form sets attributes
directly on the `.osm` model file.

**Pre-flight validation:** OSimFlow checks that every `MeasureName.argument` in
`variables.yml` corresponds to a real measure argument in the `.osw` before
starting the campaign. If a measure argument doesn't exist, the campaign fails
fast with a clear error message.

### 9.4 Mixing BCL and custom measures

You can freely mix BCL-downloaded measures with custom ones:

```
template_sim_package/
├── model.osm
├── workflow.osw
├── weather.epw
└── measures/
    ├── SetWindowToWallRatioByFacade/    # BCL measure
    │   ├── measure.rb
    │   └── measure.xml
    ├── SetThermostatSchedule/           # Custom measure
    │   ├── measure.rb
    │   └── measure.xml
    └── MyPythonPostProcessor/           # Custom Python measure
        ├── measure.py
        ├── measure.xml
        └── requirements.txt
```

```json
{
  "seed_file": "model.osm",
  "weather_file": "weather.epw",
  "measure_paths": ["measures"],
  "steps": [
    { "measure_dir_name": "SetWindowToWallRatioByFacade", "arguments": { "wwr": 0.4 } },
    { "measure_dir_name": "SetThermostatSchedule", "arguments": { "heating_setpoint": 20.0 } },
    { "measure_dir_name": "MyPythonPostProcessor", "arguments": {} }
  ]
}
```

### 9.5 Measure resources and data files

Measures that need external data (schedules, look-up tables) should place
them in a `resources/` subdirectory within the measure:

```
measures/
└── SetSpaceTypeLoadsWithSchedules/
    ├── measure.rb
    ├── measure.xml
    └── resources/
        ├── office_schedules.csv
        └── load_factors.json
```

In the measure script, reference resources relative to the measure directory:

```ruby
resources_dir = File.join(File.dirname(__FILE__), "resources")
schedules = CSV.read(File.join(resources_dir, "office_schedules.csv"))
```

```python
resources_dir = Path(__file__).parent / "resources"
schedules = (resources_dir / "office_schedules.csv").read_text()
```

---

## Quick reference

| Task | Command |
|---|---|
| Download a BCL measure | `openstudio.cli measure download BCL <measure_name>` |
| Install Ruby gems into package | `cd template_sim_package/ && bundle install --path vendor/bundle` |
| Install Python deps into measure | `pip install --target measures/Name/vendor -r measures/Name/requirements.txt` |
| Test workflow locally | `cd template_sim_package/ && openstudio.cli run -w workflow.osw` |
| Test in container | `docker run --rm -v "$(pwd)":/work -w /work nrel/openstudio:3.7.0 openstudio.cli run -w workflow.osw` |
| Check container Ruby version | `docker run --rm nrel/openstudio:3.7.0 ruby --version` |
| Check container Python version | `docker run --rm nrel/openstudio:3.7.0 python3 --version` |
| Run OSimFlow stub test | `OSIMFLOW_STUB_SIM=1 osimflow run --executor local --template_sim_package ./template_sim_package --n_samples 3 --outdir ./test_out` |

---

## See also

- [AGENTS.md §8 — Common gotchas](../AGENTS.md) (gotcha #7: measure dependencies)
- [OpenStudio CLI documentation](https://openstudio.net/docs/cli/)
- [NREL Building Component Library](https://bcl.nrel.gov/)
- [OpenStudio image distribution](openstudio-image-distribution.md) — how OSimFlow selects the container
- [PRD §6 — Potential challenges](OSimFlow.md) — full list of known gotchas
- [user_scripts/README.md](../user_scripts/README.md) — BYOS script overrides (for parameter application and KPI extraction, not measure packaging)
