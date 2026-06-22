# Trace: Traditional OpenStudio-Server Helm Chart / LHS Batch Analysis (1000 Samples)

## Overview

This document traces every repository, function, method, class, and system boundary
involved in running a traditional OpenStudio-server deployment for a 1000-sample LHS
(Latin Hypercube Sampling) analysis via the `execute_sequential` rake command.  It covers
the complete call chain from the Rakefile down to individual simulation workers, and
identifies every external repository, gem, Docker image, and network hop required.

---

## 1. Entry Point: `rake execute_sequential`

**File:** `/home/alex/Projects/openstudio-bem-to-surrogate-gem/Rakefile` (line 246)

```ruby
task :execute_sequential do
  ARGV.drop(1).each { |a| task a.to_sym do; end }

  path_parametric_space = ARGV[1]

  base = get_base(resolve_project_config: true)

  OpenStudio::BEMToSurrogate::BuildOSM.create_osm(base.configs.project_structure.path_osm)

  base.buildosw.create_osw

  base.buildosa.create_and_submit_osa(path_parametric_space, create: true, submit: true, sequential: true)

  sleep 30
end
```

**Steps:**

1. `get_base` — loads `configs.yml` (or `configs.yml.template`)
2. `BuildOSM.create_osm` — creates the seed `.osm` file
3. `BuildOSW#create_osw` — assembles the seed `.osw` workflow JSON
4. `BuildOSA#create_and_submit_osa(..., sequential: true)` — builds the OSA
   analysis bundle and submits it to the OpenStudio server
5. `sleep 30` — keeps the Rake process alive while the remote cluster runs

---

## 2. Configuration Loading: `get_base`

**File:** `openstudio-bem-to-surrogate-gem/lib/openstudio/bem_to_surrogate.rb`

```ruby
def get_base(resolve_project_config:, yml_filepath: DEFAULT_YAML_CONFIG)
  OpenStudio::BEMToSurrogate::Base.from_yaml(
    yml_filepath,
    log_to_stdout: true,
    log_to_file: true,
    log_level: Logger::INFO,
    resolve_project_config:
  )
end
```

**Key config fields (from `configs.yml.template`):**

```yaml
project_structure:
  project_name: 179D
  path_osm_name: seed_model.osm
  path_osw_name: seed_workflow.osw
  path_osa_name: osa_workflow.json
  dir_output: "./outputs"
  dir_measures: "./lib/measures"
  dir_weather: "./spec/files/weather"
external_tools:
  ruby_path: null                    # resolved from PATH
  server_uri: http://os-server:8080  # Docker service name in the cluster
  os_meta_path: "../OpenStudio-server/bin/openstudio_meta"
osa_settings:
  analysis_settings:
    analysis_type: lhs              # ← LHS, not batch_datapoints
    algorithm_settings:
      seed: 179
      number_of_samples: 1900       # configurable; 1000 for this trace
      sample_method: all_variables
```

**Key classes:**
- `OpenStudio::BEMToSurrogate::Base` — (`base.rb`)
- `OpenStudio::BEMToSurrogate::Configuration` — (`config.rb`)
- `OpenStudio::BEMToSurrogate::ProjectStructure` — (`config.rb`)

---

## 3. Seed Model Creation: `BuildOSM.create_osm`

**File:** `openstudio-bem-to-surrogate-gem/lib/openstudio/bem_to_surrogate/create_osm.rb`

```ruby
module OpenStudio::BEMToSurrogate::BuildOSM
  def self.create_osm(save_osm_path, empty_model: true)
    if empty_model
      model = OpenStudio::Model::Model.new   # ← OpenStudio C++ SDK bindings
    else
      model = OpenStudio::Model.exampleModel
    end
    osm_path = OpenStudio::Path.new(File.expand_path(save_osm_path))
    model.save(osm_path, true)
  end
end
```

**Repository:** `NREL/openstudio-bem-to-surrogate-gem`
**Ruby gem dependency chain:**
- `openstudio` gem (contains `OpenStudio::Model`, `OpenStudio::WorkflowJSON`,
  `OpenStudio::MeasureStep`, etc.) — ships the OpenStudio C++ SDK bindings
- `openstudio-analysis` gem — contains `OpenStudio::Analysis`, `ServerApi`
- `openstudio-aws` gem — AWS-specific submission logic
- `openstudio-standards` gem — ASHRAE 90.1/179D standard library

**Output:** `seeds/seed_model.osm` — an empty or example OpenStudio Model file

---

## 4. OSW Workflow Creation: `BuildOSW#create_osw`

**File:** `openstudio-bem-to-surrogate-gem/lib/openstudio/bem_to_surrogate/create_osw.rb`

```ruby
def create_osw(path_measure_space = nil, ask_with_stdin: true)
  # 1. Locate and parse measure_space.json
  path_measure_space, measure_space_from_existing_project_folder = locate_measure_space(...)

  # 2. Initialize WorkflowJSON
  osw = OpenStudio::WorkflowJSON.new
  osw.setOswDir(@configs.project_structure.dir_osw.to_s)
  osw.setOswPath(@configs.project_structure.path_osw.to_s)

  # 3. Set seed OSM
  seed_file = @configs.project_structure.path_osm.relative_path_from(@configs.project_structure.dir_osw)
  osw.setSeedFile(seed_file.to_s)

  # 4. Copy weather files (EPW/DDY/STAT) into run directory
  @configs.project_structure.dir_weather.glob("*.{epw,ddy,stat}").each { |f| FileUtils.cp(f, dir_run_weather) }
  osw.setWeatherFile(weather_temp.basename.to_s)

  # 5. Copy measures into run directory
  OpenStudio::BEMToSurrogate::Utilities.copy_measure(@dir_measures, dir_run_measures, measure_name)

  # 6. Build measure steps from measure_space['measure_space']
  measure_space['measure_space'].each do |measure_name, measure_arguments|
    measureStep = OpenStudio::MeasureStep.new(measure_name)
    measure_arguments.each { |arg, value| measureStep.setArgument(arg, value) }
    measureSteps << measureStep
  end
  osw.setMeasureSteps(OpenStudio::MeasureType.new('ModelMeasure'), measureSteps)

  # 7. Build reporting measure steps from measure_space['measure_space_reporting']
  measure_space['measure_space_reporting'].each do |measure_name, measure_arguments|
    measureStep = OpenStudio::MeasureStep.new(measure_name)
    measure_arguments.each { |arg, value| measureStep.setArgument(arg, value) }
    measureSteps << measureStep
  end
  osw.setMeasureSteps(OpenStudio::MeasureType.new('ReportingMeasure'), measureSteps)

  # 8. Save
  osw.save
end
```

**Key classes / methods:**
- `OpenStudio::WorkflowJSON` — (`openstudio` gem) — OSW file format handler
- `OpenStudio::MeasureStep` — wraps a single measure with its arguments
- `OpenStudio::MeasureType` — enum: `'ModelMeasure'` | `'ReportingMeasure'`
- `OpenStudio::BEMToSurrogate::Utilities.copy_measure` — (`utilities.rb`) —
  copies measure directories from gems into the run directory

**Output:** `seeds/seed_workflow.osw` — the OpenStudio Workflow JSON that
defines the seed model + measure stack

---

## 5. OSA Creation and Submission: `BuildOSA#create_and_submit_osa`

### 5.1 `create_and_submit_osa`

**File:** `openstudio-bem-to-surrogate-gem/lib/openstudio/bem_to_surrogate/create_osa.rb`

```ruby
def create_and_submit_osa(path_parametric_space = nil, create: false, submit: false, sequential: false)
  if create
    parametric_spaces = _read_parametric_spaces(path_parametric_space)
    parametric_spaces.each do |batch_name, parametric_space|
      create_osa_for_batch(batch_name, parametric_space)   # ← builds OSA JSON + ZIP
      if submit && sequential
        submit_osa_for_batch(batch_name)                  # ← spawns ruby subprocess
      end
    end
  end
  if submit && !sequential
    submit_osa   # non-sequential: submit all at once
  end
end
```

For `sequential: true`, the OSA is submitted **immediately after** each batch is
created — one `openstudio_meta run_analysis` call per batch.

### 5.2 `create_osa_for_batch`

**File:** `create_osa.rb` line 97

**Steps:**

1. **Initialize OSA object**
   ```ruby
   osa = OpenStudio::Analysis.create("#{batch_name}_#{time_stamp}")
   ```

2. **Read measure space** (`measure_space.json` or `measure_space_<batch>.json`)

3. **Convert OSW → OSA**
   ```ruby
   Dir.chdir(@configs.project_structure.dir_osw) do
     osa.convert_osw(@configs.project_structure.path_osw)
   end
   ```
   This reads `seed_workflow.osw` and translates it into the OSA object graph
   (seed file, weather file, measure steps with arguments).

4. **Add output variables** — from `measure_space['output_variables']`
   ```ruby
   osa.add_output(display_name: var_name, name: "#{measure_name}.#{var_name}",
                  objective_function: true)   # for objective function variables
   ```

5. **Add LHS distributions to variables**
   - Parses `measure_space` and `parametric_space` (the `algorithm_setting` block)
   - For each variable with a distribution, calls `osa.workflow.find_measure(name)` then
     `measure_found.make_variable(measure_argument, measure_argument, distribution)`
   - Distribution types: `uniform` (continuous), `discrete` (categorical), `array`
     (explicit list of values with weights)

6. **Apply algorithm settings**
   ```ruby
   osa.algorithm.set_attribute('seed', 179)
   osa.algorithm.set_attribute('number_of_samples', 1000)
   osa.algorithm.set_attribute('sample_method', 'all_variables')
   ```

7. **Prune weather files** — `_refresh_weather_dir_for_batch` copies only the EPW/DDY/STAT
   files needed by the analysis (avoids shipping a full weather library)

8. **Save OSA JSON** — `File.write(path_osa, JSON.pretty_generate(osa.to_hash))`

9. **Save OSA ZIP** — `osa.save_osa_zip(path_osa_zip, all_weather_files, all_seed_files)`
   This bundles:
   - `analysis.json` — the OSA formulation
   - `seed_model.osm`
   - `weather/`
   - `measures/` (Ruby/Python measure scripts)
   - `files/` (auxiliary resources)

### 5.3 `submit_osa_file`

**File:** `create_osa.rb` line 432

```ruby
def submit_osa_file(file_path)
  cmd_ruby  = File.expand_path(@configs.external_tools.ruby_path)
  meta_cli  = File.expand_path(@configs.external_tools.os_meta_path)
  server_uri = @configs.external_tools.server_uri

  cmd = "#{cmd_ruby} #{meta_cli} run_analysis --debug --verbose '#{path_osa_full}' '#{server_uri}' -a #{analysis_type}"
  # Example: ruby /path/to/openstudio_meta run_analysis \
  #            --debug --verbose \
  #            '/project/osa_workflow.json' \
  #            'http://os-server:8080' \
  #            -a lhs
  success = Bundler.with_unbundled_env { system(cmd) }
end
```

---

## 6. `openstudio_meta run_analysis` CLI

**File:** `/home/alex/Projects/worktrees/openstudio-server/bin/openstudio_meta` (line ~851)

```ruby
# openstudio_meta run_analysis [options] project server_dns
o.on('-a', '--analysis TYPE', 'Analysis type to run') {|a| options[:analysis_type] = a }
o.on('-z', '--zip NAME', 'relative path/name of project zip file') {|z| options[:zip_file] = z }

project_path  = argv.shift.to_s        # e.g. /project/osa_workflow.json
server_dns    = argv.shift.to_s        # e.g. http://os-server:8080
analysis_type = options[:analysis_type] # e.g. 'lhs'

# Process project file (for .xlsx, .csv, .json — we use JSON path)
if ::File.extname(project_path).casecmp('.json').zero?
  temp_filepath = File.dirname(project_path) + '/' + File.basename(project_path).gsub('.json', '')
  analysis_type = options[:analysis_type]   # 'lhs'
end

# Create ServerApi client
server_api = OpenStudio::Analysis::ServerApi.new(hostname: server_dns)
unless server_api.machine_status
  $logger.error "Server at #{server_api.hostname} is not responding"
  exit 1
end

# Submit the analysis
formulation_file  = temp_filepath + '.json'          # /project/osa_workflow/analysis.json
analysis_zip_file = temp_filepath + '.zip'        # /project/osa_workflow.zip
server_api.run(formulation_file, analysis_zip_file, analysis_type, run_options)
```

**Key classes / gems:**
- `OpenStudio::Analysis::ServerApi` — (`openstudio-analysis` gem, `server_api.rb`)
- `openstudio-aws` gem — AWS-specific batch run logic

---

## 7. `ServerApi#run` — Client-Side Submission

**File:**
`openstudio-bem-to-surrogate-gem/.bundle/ruby/3.2.0/gems/openstudio-analysis-1.5.2/lib/openstudio/analysis/server_api.rb`
(line 768)

```ruby
def run(formulation_filename, analysis_zip_filename, analysis_type, options = {})
  # 1. Create a new project on the server
  project_id = new_project(project_options)

  # 2. Upload analysis formulation + ZIP; returns analysis_id
  analysis_id = new_analysis(project_id, {
    formulation_file: formulation_filename,
    upload_file: analysis_zip_filename,
    reset_uuids: true
  })

  # 3. Start the analysis (dispatch LHS algorithm)
  run_options = {
    analysis_action: 'start',
    analysis_type: analysis_type,          # 'lhs'
    simulate_data_point_filename: 'simulate_data_point.rb',
    run_data_point_filename: 'run_data_point.rb'
  }
  start_analysis(analysis_id, run_options)

  # 4. For LHS (a BATCH_RUN_METHOD), also start a 'batch_run' child task
  if BATCH_RUN_METHODS.include?(analysis_type)
    # LHS is in BATCH_RUN_METHODS
    start_analysis(analysis_id, {
      analysis_action: 'start',
      analysis_type: 'batch_run',
      ...
    })
  end

  analysis_id
end
```

**HTTP endpoints called on the server (Faraday HTTP client):**

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/projects.json` | Create project |
| `POST` | `/projects/:id/analyses.json` | Upload analysis ZIP |
| `POST` | `/analyses/:id/start.json` | Start analysis / batch_run |
| `GET`  | `/analyses/:id/status.json` | Poll status |

**BATCH_RUN_METHODS constant (server_api.rb line 14):**
```ruby
BATCH_RUN_METHODS = ['lhs', 'preflight', 'single_run', 'repeat_run',
                     'doe', 'diag', 'baseline_perturbation', 'batch_datapoints'].freeze
```

---

## 8. Server-Side: OpenStudio Server Rails Application

**Repository:** `NREL/openstudio-server` (Docker image: `nrel/openstudio-server:latest`)

### 8.1 Docker Compose / Stack Services

**File:** `worktrees/openstudio-server/docker-compose.deploy.yml`

```yaml
services:
  db:             # MongoDB 8.0.12  — primary data store
    image: mongo:8.0.12
    ports: [27017]

  queue:           # Redis 6.0.9 — job queue
    image: redis:6.0.9
    ports: [6379]

  web:             # Rails API server (port 80/443)
    image: nrel/openstudio-server:latest
    command: /usr/local/bin/start-server

  web-background: # DelayedJob / Sidekiq background workers (analyses queue)
    image: nrel/openstudio-server:latest
    command: /usr/local/bin/start-web-background

  worker:         # Simulation workers — pull from 'simulations' queue
    image: nrel/openstudio-server:latest
    command: /usr/local/bin/start-workers
    volumes: [/mnt/openstudio]    # bind-mount for simulation outputs

  rserve:         # Rserve for R-based analysis algorithms
    image: nrel/openstudio-rserve:latest
```

**Deployment target:**
- Docker Swarm (overlay network) via `docker stack deploy`
- Kubernetes/Helm is **NOT** native to `openstudio-server`; the community
  project at **`NREL/openstudio-server-helm`** (separate repo) provides
  a Helm chart wrapping the same Docker images for K8s deployments

### 8.2 Analysis Lifecycle on the Server

**Rails controller:** `server/app/controllers/analyses_controller.rb`

```
POST /analyses/:id/start   →  AnalysesController#start
                              → enqueues Analysis.start_run DelayedJob
```

**DelayedJob / Sidekiq worker:**

1. **LHS algorithm** (`OpenStudio::Analysis::Algorithm::Lhs` or server-side
   Ruby LHS implementation) generates **1000 sample datapoints** using
   `scipy.stats.qmc.LatinHypercube` (or pure-Ruby equivalent).
   Each datapoint is a hash of `{variable_name => sampled_value}`.

2. Each datapoint is **serialized to MongoDB** (`datapoints` collection)
   with status `queued`.

3. For each datapoint, a **job is pushed to Redis** (`simulations` queue):
   ```json
   { "analysis_id": "...", "datapoint_id": "...", "osw": {...} }
   ```

4. **Workers** (`start-workers`) pop jobs from `simulations` queue and run:
   ```bash
   openstudio run -w /path/to/datapoint_<N>/datapoint.osw
   ```

5. On completion (success/failure), the worker **updates MongoDB**:
   - `status: 'completed'` | `'failed'`
   - results written to `eplusout.sql` (EnergyPlus SQLite output)
   - KPI variables extracted into the datapoint document

6. When all 1000 datapoints are complete, the analysis status transitions to
   `'completed'` and results are available via `GET /analyses/:id/results.json`.

### 8.3 Key Server-Side Gems / Ruby Files

**Repository:** `NREL/openstudio-server`

| Path | Purpose |
|------|---------|
| `server/app/models/analysis.rb` | Analysis MongoDB model, LHS algorithm dispatch |
| `server/app/models/datapoint.rb` | Datapoint model |
| `server/app/controllers/analyses_controller.rb` | REST API for analysis lifecycle |
| `server/app/workers/start_r.rb` | DelayedJob worker that runs algorithms |
| `server/app/workers/run_sim.rb` | DelayedJob worker that runs individual datapoints |
| `server/lib/analysis_library/lhs.rb` | LHS sample generation |
| `server/lib/analysis_library/batch.rb` | batch_datapoints runner |
| `server/lib/analysis_library/single.rb` | single_run runner |
| `server/lib/analysis_library/diag.rb` | diag (OAT) runner |
| `server/lib/analysis_library/doe.rb` | DOE runner |
| `server/lib/analysis_library/preflight.rb` | preflight runner |
| `server/lib/openstudio_aws.rb` | AWS spot instance management |
| `server/lib/openstudio_backend.rb` | Redis queue + MongoDB client helpers |

---

## 9. OpenStudio CLI / Simulation Execution (Per Sample)

**Repository:** `NREL/OpenStudio-server` Docker image ships:
- `openstudio` CLI (OpenStudio C++ application)
- `openstudio.cli` (Ruby CLI wrapper)
- EnergyPlus (`energyplus`)
- Ruby 2.7+ with OpenStudio Ruby bindings

**Per-sample execution inside a worker container:**

```bash
cd /mnt/openstudio/analyses/<analysis_id>/datapoint_<N>/
openstudio run -w datapoint_<N>.osw --debug --verbose
```

Where `datapoint_<N>.osw` is a modified copy of the seed OSW with:
- `seed_file` pointing to the seed `.osm`
- `measure_steps` with arguments replaced by the LHS-sampled values
- `weather_file` pointing to the correct `.epw`

**Output files per datapoint:**
| File | Purpose |
|------|---------|
| `eplusout.sql` | EnergyPlus SQLite results — queried for KPIs |
| `eplusout.err` | Error log — scanned for "Severe" errors |
| `eplusout.log` | Full EnergyPlus log |
| `run.log` | OpenStudio CLI stdout/stderr |
| `datapoint.json` | Datapoint metadata + status |

**KPI extraction** (by the `reporting_179_d` ReportingMeasure, or custom):
- Queries `eplusout.sql` for objective function variables
- Returns JSON dict written to `results.json`

---

## 10. Helm Chart for OpenStudio Server (Community / NREL)

**Repository:** `NREL/openstudio-server-helm` (separate from core `openstudio-server`)

The Helm chart wraps the same Docker images from `docker-compose.deploy.yml`
as a Kubernetes `Deployment`/`StatefulSet` set:

```yaml
# values.yaml (conceptual)
image:
  repository: nrel/openstudio-server
  tag: latest

services:
  web:
    replicaCount: 2
    port: 80
  worker:
    replicaCount: 10    # scale workers for parallel simulations
  rserve:
    replicaCount: 1

mongodb:
  external: true        # or included as a sub-chart
  connectionString: mongodb://...

redis:
  external: true
  url: redis://:password@redis:6379

persistence:
  enabled: true
  mountPath: /mnt/openstudio
  storageClass: gp3
  size: 500Gi

resources:
  worker:
    requests:
      cpu: "2"
      memory: "4Gi"
  web:
    requests:
      cpu: "1"
      memory: "2Gi"
```

**Kubernetes objects produced by the Helm chart:**
- `Deployment/web` — the Rails API server
- `Deployment/web-background` — background job processor
- `Deployment/worker` — simulation workers (scaled via `replicaCount`)
- `StatefulSet/rserve` — Rserve service
- `Service` — ClusterIP/LoadBalancer for the web service
- `ConfigMap` — environment variables (MongoDB/Redis connection strings)
- `Secret` — credentials
- `PersistentVolumeClaim` — shared storage (`/mnt/openstudio`) for simulation outputs

**For AWS EKS:**
- Use `aws eks create-cluster` + `eksctl` or Terraform
- Workers run as `Deployment` with `nodeSelector`/`taints` for HPC nodes
- NFS or EFS provisioned as the persistent volume (shared across pods)
- Optionally use AWS Batch as the worker backend (instead of in-cluster workers)

---

## 11. LHS Algorithm Internals

**Gem:** `openstudio-analysis` (`lib/openstudio/analysis/algorithm/`)
or server-side `server/lib/analysis_library/lhs.rb`

**Algorithm settings passed through the OSA:**
```json
{
  "algorithm": {
    "seed": 179,
    "number_of_samples": 1000,
    "sample_method": "all_variables",
    "type": "lhs"
  }
}
```

**LHS steps (conceptual Ruby/pseudo-code):**
```ruby
# 1. Determine number of variables (N)
variables = analysis.variables  # all make_variable() calls

# 2. Generate N-dimensional Latin Hypercube sample matrix
sampler = Scipy::Stats::QMC::LatinHypercube(d=variables.count, scramble=true, seed=179)
samples = sampler.random(n: 1000)   # shape: (1000, N)

# 3. For each variable, map the LHS uniform value to the distribution CDF
variables.each_with_index do |var, col|
  distribution = var.distribution   # {type: 'uniform', min: 0.5, max: 2.0}
  samples[:, col].each do |u|
    var_value = distribution.icdf(u)   # inverse CDF — maps to actual value
    datapoint[var.name] = var_value
  end
end

# 4. Create one datapoint document per row of samples matrix
samples.each do |row_values|
  Datapoint.create!(
    analysis_id: analysis.id,
    variables: Hash[variables.zip(row_values)],
    status: 'queued'
  )
end
```

**Distribution types supported:**
| Type | OSA `distribution[:type]` | Notes |
|------|--------------------------|-------|
| Uniform continuous | `uniform` | min, max |
| Discrete/categorical | `discrete` | explicit values array |
| Explicit list | `array` | values + weights arrays |
| Normal | `normal` | mean, std (if supported) |
| Lognormal | `lognormal` | (if supported) |

---

## 12. Full Call Chain Summary (1000-Sample LHS)

```
rake execute_sequential                         [Rakefile]
  get_base                                      [base.rb]
    Configuration.from_yaml                     [config.rb]
  BuildOSM.create_osm                          [create_osm.rb]
    OpenStudio::Model::Model.new                [openstudio gem]
    model.save                                  [openstudio gem]
  BuildOSW#create_osw                           [create_osw.rb]
    OpenStudio::WorkflowJSON.new                [openstudio gem]
    osw.setSeedFile                              [openstudio gem]
    osw.setWeatherFile                           [openstudio gem]
    Utilities.copy_measure                        [utilities.rb]
    OpenStudio::MeasureStep.new                  [openstudio gem]
    osw.setMeasureSteps                          [openstudio gem]
    osw.save                                     [openstudio gem]
  BuildOSA#create_and_submit_osa                [create_osa.rb]
    _read_parametric_spaces                      [create_osa.rb]
    create_osa_for_batch                         [create_osa.rb]
      OpenStudio::Analysis.create                [openstudio-analysis gem]
      osa.convert_osw                            [openstudio-analysis gem]
      osa.add_output                             [openstudio-analysis gem]
      osa.workflow.find_measure                  [openstudio-analysis gem]
      measure.make_variable                       [openstudio-analysis gem]
      osa.algorithm.set_attribute                [openstudio-analysis gem]
      osa.save_osa_zip                           [openstudio-analysis gem]
    submit_osa_for_batch                         [create_osa.rb]
      ruby openstudio_meta run_analysis          [openstudio_meta]
        OpenStudio::Analysis::ServerApi.new       [server_api.rb]
        server_api.run                            [server_api.rb]
          POST /projects.json                     [Faraday HTTP]
          POST /projects/:id/analyses.json        [Faraday HTTP]
          POST /analyses/:id/start.json           [Faraday HTTP]
            AnalysesController#start             [Rails]
            Analysis.start_run (DelayedJob)       [server/lib/workers/]
              AnalysisLibrary::Lhs.run            [server/lib/analysis_library/lhs.rb]
                # Generates 1000 datapoint docs in MongoDB
                # Pushes 1000 jobs to Redis 'simulations' queue
              AnalysisLibrary::Batch.run          [server/lib/analysis_library/batch.rb]
            Worker pops job from 'simulations'    [server/app/workers/run_sim.rb]
              openstudio run -w datapoint.osw     [openstudio CLI]
              # Per-datapoint files: eplusout.sql, eplusout.err, results.json
              Update datapoint status in MongoDB
```

---

## 13. Repository Index

| Repository | Role |
|-----------|------|
| `NREL/openstudio-bem-to-surrogate-gem` | Parametric study orchestration (Rake tasks, OSA builder) |
| `NREL/openstudio-server` | Server API + worker + Docker images (`nrel/openstudio-server`) |
| `NREL/openstudio` | OpenStudio C++ SDK + CLI + Ruby/Python bindings |
| `NREL/openstudio-standards` | ASHRAE 90.1 / 179D standard library |
| `NREL/openstudio-common-measures` | Shared OpenStudio measures |
| `NREL/openstudio-model-articulation` | Model creation measures (create_bar, etc.) |
| `openstudio-analysis` gem | OSA data model, ServerApi, LHS/algorithm classes |
| `openstudio-aws` gem | AWS Batch / spot instance management |
| `NREL/openstudio-server-helm` | Helm chart for K8s deployment |
| `EnergyPlus/EnergyPlus` | Energy simulation engine (called by OpenStudio CLI) |
| `NREL/pat` | OpenStudio PAT (Parametric Analysis Tool) — desktop GUI |

---

## 14. Docker Images

| Image | Source | Role |
|-------|--------|------|
| `nrel/openstudio-server:latest` | `NREL/openstudio-server` `Dockerfile` | Rails API + workers |
| `nrel/openstudio-rserve:latest` | NREL/openstudio-rserve | Rserve for R algorithms |
| `nrel/openstudio:<version>` | NREL Docker Hub | OpenStudio CLI + EnergyPlus |
| `mongo:8.0.12` | Docker Hub | MongoDB database |
| `redis:6.0.9` | Docker Hub | Redis job queue |

---

## 15. Environment Variables Required at Runtime

| Variable | Service | Purpose |
|----------|---------|---------|
| `MONGO_INITDB_ROOT_USERNAME` | MongoDB | DB authentication |
| `MONGO_INITDB_ROOT_PASSWORD` | MongoDB | DB authentication |
| `REDIS_PASSWORD` | Redis | Queue authentication |
| `REDIS_URL` | All services | `redis://:password@queue:6379` |
| `SECRET_KEY_BASE` | Rails | Session security |
| `OS_SERVER_NUMBER_OF_WORKERS` | web, web-background | Concurrency |
| `QUEUES` | web-background | `background,analyses` |
| `QUEUES` | worker | `requeued,simulations` |

---

## 16. Network Topology (Docker Swarm / K8s)

```
                                    ┌─────────────────┐
                                    │   LoadBalancer  │
                                    │   (port 80/443) │
                                    └────────┬────────┘
                                             │
                                    ┌────────▼────────┐
                                    │   web (Rails)  │
                                    │  nrel/openstudio│
                                    │  -server:latest │
                                    └────────┬────────┘
                                             │
              ┌──────────────────────────────┼──────────────────────────────┐
              │                              │                              │
     ┌────────▼────────┐          ┌────────▼────────┐          ┌────────▼────────┐
     │ web-background   │          │    MongoDB      │          │      Redis      │
     │ (DelayedJob)     │          │   (port 27017)  │          │   (port 6379)   │
     └────────┬────────┘          └─────────────────┘          └────────┬────────┘
              │                                                              │
     ┌────────▼────────────────────────────────────────────────────────▼────────┐
     │                           worker (×N replicas)                          │
     │                   nrel/openstudio-server:latest                          │
     │                   /usr/local/bin/start-workers                           │
     │                   Pop from 'simulations' queue → run openstudio CLI      │
     │                   Bind-mount: /mnt/openstudio (shared PVC)               │
     └──────────────────────────────────────────────────────────────────────────┘
```

For Helm/K8s: each service becomes a `Deployment`; the bind-mount becomes a
`PersistentVolumeClaim` (NFS/EFS on AWS).

---

## 17. Key Differences vs OSimFlow

OSimFlow replaces the **entire stack above the dotted line** with a custom
Python driver:

| Traditional openstudio-server | OSimFlow replacement |
|------------------------------|----------------------|
| Rails API (`nrel/openstudio-server`) | `osimflow/campaign.py` (Python) |
| MongoDB | `osimflow/cache.py` (SQLite) + pluggable `ResultStorage` |
| Redis queue | `osimflow/distributed_cache.py` (Redis pub/sub) |
| Docker Swarm worker (`start-workers`) | `osimflow/executors/` (submitit/Slurm/AWS Batch/etc.) |
| `openstudio_meta run_analysis` | `osimflow/work.py` work functions |
| `ServerApi#run` (HTTP POST to Rails) | `Campaign._submit_step` → executor.submit() |
| LHS in `analysis_library/lhs.rb` | `osimflow/algorithms/lhs.py` (scipy.stats.qmc) |
| DelayedJob background jobs | `submitit` / `dask-jobqueue` |
| Helm chart (`openstudio-server-helm`) | N/A (OSimFlow is a CLI tool) |

The **common foundation** that OSimFlow retains:
- **OpenStudio CLI** (`nrel/openstudio:<version>` container) — unchanged
- **Seed model + measures + OSW** — same format
- **`bin/extract_kpis.py`** (replaces `reporting_179_d`) — queries `eplusout.sql`
- **`bin/aggregate_results.py`** — produces `failed_simulations.csv` + `aggregated_results.csv`
- **OpenStudio SDK** (`openstudio` Ruby gem) — unchanged for OSW/OSM manipulation

---

*Document version: 1.0 — traced from Rakefile `execute_sequential` through to K8s worker pods*
*Branch: `trace/openstudio-server-helm-chart-lhs-1000-samples`*
