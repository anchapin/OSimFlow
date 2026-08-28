# Chaos Engine

OSimFlow includes an opt-in chaos fault-injection system that lets you validate campaign resilience by introducing controlled failures and stress conditions. Use it to verify that your campaign handles node failures, network degradation, and resource exhaustion gracefully before going to production.

> **Note:** Chaos injection is a deliberately destructive testing tool. Never enable it against a production campaign that you cannot afford to interrupt.

## CLI Flags

All flags live under `osimflow run`. Run `osimflow run --help | grep -A 50 chaos` for the full list.

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `--chaos-enabled` | bool | `false` | Enable chaos injection |
| `--chaos-scenarios` | list[str] | `[]` | Which scenarios to activate: `kill_switch`, `network_delay`, `cpu_spike`, `memory_pressure` |
| `--chaos-schedule` | string | `none` | When injection fires: `none`, `before_step`, `after_step`, `per_sample` |
| `--chaos-probability` | float | `1.0` | Probability (0.0–1.0) that any given call triggers the injector |
| `--chaos-delay-s` | float | `0.1` | Base delay in seconds (network delay injector) |
| `--chaos-jitter-s` | float | `0.05` | Random jitter added to delay |
| `--chaos-duration-s` | float | `0.5` | Fault duration in seconds |
| `--chaos-intensity` | float | `0.5` | Fault intensity as a fraction 0.0–1.0 (CPU spike / memory pressure) |
| `--chaos-size-mb` | int | `64` | Memory allocation size in MB (memory pressure injector) |
| `--chaos-fail-after` | int | `2` | Number of calls before the kill switch activates (kill_switch only) |

## Scenario Catalog

### `kill_switch`

Sends a termination signal to the target worker process. Use this to validate that the campaign handles unexpected process termination gracefully and that the retry logic correctly re-queues failed samples.

> **Status:** This injector logs a warning when the kill switch fires but does **not** actually terminate a running worker process in the current wiring. It functions as a simulated kill switch (`KillSwitchSimulator`) — the warning fires and the sample is marked failed, but no OS signal is sent. A future release (issue #1179) will wire this to `BaseExecutor.kill_sample` for executors that support per-sample cancellation.

**Parameters:**

| Parameter | Flag | Default | Description |
|-----------|------|---------|-------------|
| `fail_after` | `--chaos-fail-after` | `2` | Number of calls after which the kill switch activates |

**Behavior:** After `fail_after` calls for a given `target_id`, the injector logs `Kill switch activated for target {target_id}`. The sample is marked failed and the campaign's retry logic re-queues it if retries are configured.

### `network_delay`

Introduces an artificial delay before outbound network operations, simulating degraded network conditions between the campaign coordinator and worker nodes.

**Parameters:**

| Parameter | Flag | Default | Description |
|-----------|------|---------|-------------|
| `delay_s` | `--chaos-delay-s` | `0.1` | Base delay in seconds |
| `jitter_s` | `--chaos-jitter-s` | `0.05` | Random jitter added to the delay |
| `probability` | `--chaos-probability` | `1.0` | Probability that any call actually experiences the delay |

**Behavior:** A threading `Event` blocks the operation for `delay_s ± jitter_s` seconds. The delay is released automatically after the duration.

### `cpu_spike`

Spawns CPU-intensive background threads that burn cycles for the specified duration, simulating high CPU load on the worker node.

**Parameters:**

| Parameter | Flag | Default | Description |
|-----------|------|---------|-------------|
| `duration_s` | `--chaos-duration-s` | `0.5` | Duration of the CPU spike in seconds |
| `intensity` | `--chaos-intensity` | `0.5` | Fraction of available cores to consume (0.0–1.0) |
| `probability` | `--chaos-probability` | `1.0` | Probability that any call triggers the spike |

**Behavior:** Spawns `max(1, int(intensity * 4))` busy-wait threads. The threads terminate automatically after `duration_s`.

### `memory_pressure`

Allocates a large memory buffer that is held for the specified duration, simulating high memory usage conditions on the worker node.

**Parameters:**

| Parameter | Flag | Default | Description |
|-----------|------|---------|-------------|
| `size_mb` | `--chaos-size-mb` | `64` | Size of the memory allocation in MB |
| `duration_s` | `--chaos-duration-s` | `0.5` | Duration to hold the memory in seconds |
| `probability` | `--chaos-probability` | `1.0` | Probability that any call triggers the pressure |

**Behavior:** Allocates `size_mb` MB of memory in a `bytearray`. The buffer is released automatically after `duration_s`.

## Schedule Semantics

The `--chaos-schedule` flag controls when the chaos engine fires relative to campaign steps:

| Schedule | Meaning |
|----------|---------|
| `none` | Chaos is disabled (no injection regardless of `--chaos-enabled`) |
| `before_step` | Inject before each DAG step begins |
| `after_step` | Inject after each DAG step completes |
| `per_sample` | Inject once per individual sample (apply / sim / kpi call) |

The default `none` means chaos is defined but not automatically scheduled — the `ChaosEngine` is still wired to the campaign and can be triggered programmatically via `Campaign._maybe_inject_chaos`.

## Using Multiple Scenarios

Pass multiple scenario names to `--chaos-scenarios` to combine injectors:

```bash
osimflow run \
  --executor slurm \
  --chaos-enabled \
  --chaos-scenarios kill_switch network_delay \
  --chaos-schedule per_sample \
  --chaos-delay-s 2.0 \
  --chaos-fail-after 3 \
  --input_variables variables.yml \
  --template_sim_package ./pkg \
  --n_samples 100 \
  --outdir ./results
```

## Production Example

A typical resilience validation run:

```bash
# 1. Dry run: verify the campaign structure is correct
osimflow run \
  --executor slurm \
  --input_variables variables.yml \
  --template_sim_package ./pkg \
  --n_samples 5 \
  --outdir ./results-dry

# 2. Enable chaos with a low probability to smoke-test the wiring
osimflow run \
  --executor slurm \
  --chaos-enabled \
  --chaos-scenarios network_delay \
  --chaos-schedule before_step \
  --chaos-probability 0.05 \
  --chaos-delay-s 1.0 \
  --input_variables variables.yml \
  --template_sim_package ./pkg \
  --n_samples 50 \
  --outdir ./results-chaos-smoke

# 3. Full chaos campaign: 10% kill switch + 20% network delay
osimflow run \
  --executor slurm \
  --chaos-enabled \
  --chaos-scenarios kill_switch network_delay \
  --chaos-schedule per_sample \
  --chaos-probability 0.10 \
  --chaos-delay-s 2.0 \
  --chaos-fail-after 5 \
  --input_variables variables.yml \
  --template_sim_package ./pkg \
  --n_samples 500 \
  --outdir ./results-chaos-full
```

## Interpreting Results

### `run.json.chaos_invocations`

Every chaos injection is recorded in `run.json.chaos_invocations` — an array of chaos event objects written to the campaign output directory. Each entry has:

```json
{
  "fault_type": "network_delay",
  "target_id": "sim_sample-0042",
  "injected": true,
  "duration_s": 2.05,
  "error": null
}
```

| Field | Meaning |
|-------|---------|
| `fault_type` | Which injector fired: `kill_switch`, `network_delay`, `cpu_spike`, `memory_pressure` |
| `target_id` | The sample or scenario ID the fault was applied to |
| `injected` | `true` if the fault was applied, `false` if the injector chose not to fire (e.g., probability check) |
| `duration_s` | How long the fault lasted (transient faults only; 0 for `kill_switch`) |
| `error` | Error message if the injection failed, otherwise `null` |

### Detecting Failures

After a chaos-enabled run:

```bash
# Count failed samples
grep -c '"status": "failed"' results-chaos/run.json

# Find the most common failure type
grep '"status": "failed"' results-chaos/run.json \
  | jq -r '.sample_id' \
  | sort \
  | uniq -c \
  | sort -rn \
  | head -20

# Check chaos invocation rate
jq '[.chaos_invocations[] | select(.injected == true)] | length' results-chaos/run.json
```

### Comparing Chaos vs. Baseline

```bash
# Baseline (no chaos)
osimflow run --executor slurm --n_samples 500 --outdir ./baseline ...
jq '{duration: .duration_s, failed: [.samples[] | select(.status == "failed")] | length}' baseline/run.json

# With chaos
jq '{duration: .duration_s, failed: [.samples[] | select(.status == "failed")] | length}' results-chaos/run.json
```

A healthy campaign should see similar failure rates in both runs (chaos failures are expected and retried). A significantly higher failure rate in the chaos run indicates that retries are not absorbing the injected faults.

## Architecture

```
ChaosEngine
  └── registers list[FaultInjector]
        ├── KillSwitchInjector   (kill_switch)
        ├── NetworkDelayInjector (network_delay)
        ├── CPUSpikeInjector     (cpu_spike)
        └── MemoryPressureInjector (memory_pressure)

  inject(target_id) → list[ChaosResult]
        └── calls each injector.inject(target_id)

ChaosScenario  (named collection)
  ├── name: str
  ├── injectors: list[FaultInjector]
  ├── probability: float
  └── max_concurrent: int
```

The `ChaosEngine` is constructed by `Campaign` at startup when `--chaos-enabled` is set and at least one `--chaos-scenarios` is listed. It is called at the configured schedule (`before_step` / `after_step` / `per_sample`) via `Campaign._maybe_inject_chaos`.

## Python API

```python
from osimflow.chaos import (
    ChaosEngine,
    KillSwitchInjector,
    NetworkDelayInjector,
    CPUSpikeInjector,
    MemoryPressureInjector,
    ChaosScenario,
    run_chaos_scenario,
)

# Standalone usage
engine = ChaosEngine()
engine.register(KillSwitchInjector(fail_after=3))
engine.register(NetworkDelayInjector(delay_s=1.5, jitter_s=0.2))
results = engine.inject("sample-0042")

# Run a function within a chaos scenario
scenario = ChaosScenario(
    name="network-resilience",
    injectors=[NetworkDelayInjector(delay_s=2.0)],
    probability=0.1,
)
result, chaos_results = run_chaos_scenario(scenario, my_step_function, arg1, arg2)
```

## Adding a Custom Injector

Subclass `FaultInjector` and implement `inject(target_id) -> ChaosResult`:

```python
from osimflow.chaos import FaultInjector, ChaosResult, FaultType

class DiskPressureInjector(FaultInjector):
    name = "disk_pressure"

    def __init__(self, size_mb: int = 1024, probability: float = 0.5):
        self._size_mb = size_mb
        self._probability = probability

    @property
    def fault_type(self) -> FaultType:
        return FaultType.MEMORY_PRESSURE  # or define a new FaultType

    def inject(self, target_id: str) -> ChaosResult:
        if random.random() > self._probability:
            return ChaosResult(self.fault_type, target_id, injected=False)
        # ... apply fault
        return ChaosResult(self.fault_type, target_id, injected=True, duration_s=5.0)
```
