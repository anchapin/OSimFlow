# OSimFlow — Developer Guide

> **Audience:** A Python developer joining the project who wants to
> understand the architecture, make changes, and land a PR.
> For contributor onboarding and PR process, see
> [`CONTRIBUTING.md`](CONTRIBUTING.md).

---

## Table of Contents

1. [Quick Start](#1-quick-start)
2. [Architecture Overview](#2-architecture-overview)
3. [Project Structure](#3-project-structure)
4. [Development Environment](#4-development-environment)
5. [Running Tests](#5-running-tests)
6. [Code Style](#6-code-style)
7. [Adding a New Executor](#7-adding-a-new-executor)
8. [Adding a New DAG Step](#8-adding-a-new-dag-step)
9. [Adding a New CLI Flag](#9-adding-a-new-cli-flag)
10. [The BYOS Contract](#10-the-byos-contract)
11. [Cache System](#11-cache-system)
12. [Monitoring](#12-monitoring)
13. [CI Pipeline](#13-ci-pipeline)
14. [Debugging Tips](#14-debugging-tips)

---

## 1. Quick Start

```bash
# Clone the repo
git clone https://github.com/anchapin/OSimFlow.git
cd OSimFlow

# Install with all dev extras (creates .venv/ via the Makefile)
make install

# Install pre-commit hooks (runs on every git commit)
.venv/bin/pre-commit install

# Verify everything works
make test-fast   # unit + contract tests, ~10s
make test        # full suite

# Run a smoke-test campaign (stub mode — no real OpenStudio needed)
osimflow run \
  --executor local \
  --input_variables example_package/variables.yml \
  --template_sim_package ./example_package \
  --n_samples 3 \
  --outdir ./smoke_results \
  --openstudio_version 3.4.0
```

> **Why `make install`?** The Makefile hard-codes `.venv/bin/python`,
> `.venv/bin/pytest`, etc. so there is exactly one supported way to run
> the project. A bare `pytest` will resolve to a different Python that
> lacks the `[dev,aws,slurm]` extras and fail with
> `ModuleNotFoundError`. See [`CONTRIBUTING.md`](CONTRIBUTING.md) §2
> for details.

---

## 2. Architecture Overview

OSimFlow uses a three-layer architecture:

```
┌─────────────────────────────────────────────────────────────────┐
│  Orchestrator (osimflow/campaign.py)                            │
│                                                                 │
│  Campaign.run() drives the 6-step DAG:                          │
│                                                                 │
│    1. GENERATE_LHS_SAMPLES  ──→  samples.json                  │
│    2. APPLY_PARAMETERS      ──→  N modified sim packages       │
│    3. RUN_OPENSTUDIO_SIM    ──→  N simulation outputs          │
│    4. EXTRACT_KPIS          ──→  N KPI JSON files              │
│    5. AGGREGATE_RESULTS     ──→  aggregated_results.csv        │
│    6. GENERATE_BASIC_PLOTS  ──→  *.png plots                   │
│                                                                 │
│  Steps 2-4 fan out over N samples.                              │
│  Each step is cached (SQLiteCache).                             │
├─────────────────────────────────────────────────────────────────┤
│  Executor (osimflow/executors/__init__.py)                      │
│                                                                 │
│  BaseExecutor                                                   │
│   ├── LocalExecutor   — ThreadPoolExecutor (dev/CI)             │
│   ├── SlurmExecutor   — submitit.AutoExecutor (HPC)             │
│   ├── AWSBatchExecutor — boto3 Batch (cloud)                    │
│   └── NomadExecutor   — HTTP API (on-prem)                     │
│                                                                 │
│  All expose: submit(fn, *args, ...) → Handle                    │
│  Handle exposes: .result(timeout), .done(), .job_id             │
├─────────────────────────────────────────────────────────────────┤
│  Work Functions (osimflow/work.py + bin/*.py)                   │
│                                                                 │
│  The actual per-step logic. Work functions call bin/*.py        │
│  scripts via subprocess. Users can override via BYOS.           │
└─────────────────────────────────────────────────────────────────┘
```

### Data flow

```
variables.yml ──→ step_generate_lhs ──→ samples.json
                                              │
                   ┌─────────────────────────┘
                   ▼
         step_apply_parameters (per-sample)
                   │
                   ▼
         step_run_openstudio_sim (per-sample, heavy)
                   │
                   ▼
         step_extract_kpis (per-sample)
                   │
                   ▼
         step_aggregate_results ──→ aggregated_results.csv
                                       failed_simulations.csv
                   │
                   ▼
         step_generate_plots ──→ eui_histogram.png, ...
```

### Key design principles

- **Embarrassingly parallel.** Per-sample work (5 min to 4 h) runs
  independently. No cross-sample communication.
- **Cache-first.** Re-running with the same inputs is nearly free.
  The cache is content-addressed (SHA-256 of inputs + code).
- **Executor-agnostic.** The Campaign never knows whether work runs
  locally, on Slurm, or on AWS Batch. The `submit()` / `Handle`
  abstraction hides the substrate.
- **BYOS (Bring Your Own Script).** Users override `apply_parameters`
  and `extract_kpis` by supplying a Python file with the right
  function signature.

---

## 3. Project Structure

```
OSimFlow/
├── osimflow/                    # The main Python package
│   ├── __init__.py              # Public API: Campaign, executors, etc.
│   ├── __main__.py              # CLI entry point (osimflow run ...)
│   ├── campaign.py              # Campaign orchestrator (~1100 LoC)
│   ├── cache.py                 # SQLiteCache + CacheKey
│   ├── config.py                # CampaignConfig dataclass
│   ├── monitoring.py            # RunTrace, StepTrace, SampleTrace
│   ├── work.py                  # Per-step work functions
│   ├── byos.py                  # BYOS script loader
│   ├── apply_params.py          # Parameter pre-flight + application
│   ├── weather.py               # EPW file discovery and validation
│   ├── mlflow_hook.py           # Optional MLflow integration
│   ├── importers/               # .osa import support
│   │   └── osa.py
│   └── executors/
│       └── __init__.py          # BaseExecutor + 4 implementations
├── bin/                         # CLI scripts called by work.py
│   ├── generate_lhs.py          # LHS sampler (scipy.stats)
│   ├── apply_params_to_model.py # Default parameter application
│   ├── extract_kpis.py          # Default KPI extractor
│   ├── aggregate_results.py     # Result aggregation
│   └── generate_plots.py        # Matplotlib/seaborn plots
├── user_scripts/                # BYOS override scripts (user-supplied)
├── tests/
│   ├── unit/                    # Unit tests
│   ├── integration/             # Integration / E2E tests
│   │   ├── test_local_executor.py
│   │   ├── test_slurm_executor_debug.py
│   │   ├── test_aws_batch_executor_stub.py
│   │   ├── test_cache_invalidation.py
│   │   └── test_cache_resume.py
│   ├── contract/                # Contract tests (AGENTS.md / docs sync)
│   └── benchmarks/              # Performance benchmarks
├── tools/
│   ├── check_agents_contract.py # AGENTS.md / code drift checker
│   └── check_docs_sync.py       # Docs path resolution checker
├── docs/
│   ├── DEVELOPMENT.md           # This file
│   ├── CONTRIBUTING.md          # Contributor guide
│   ├── OSimFlow.md              # Product Requirements Document
│   └── openstudio-image-distribution.md
├── .github/workflows/
│   └── ci.yml                   # CI pipeline (lint, test, typecheck, ...)
├── pyproject.toml               # Project metadata, deps, tool config
├── Makefile                     # Developer commands
└── AGENTS.md                    # AI assistant conventions
```

---

## 4. Development Environment

### Prerequisites

| Tool | Version | Notes |
|---|---|---|
| Python | 3.12+ | The package uses 3.12-only syntax. |
| git | 2.x | For version control. |
| make | GNU or BSD | The Makefile is the canonical entry point. |
| Docker | optional | Only for testing against real `openstudio.cli`. |

### Setup

```bash
make install                    # pip install -e ".[dev,aws,slurm]"
.venv/bin/pre-commit install    # Install git hooks
```

### IDE recommendations

- **VS Code**: Install the Python, Ruff, and mypy extensions. The
  `.vscode/` settings should point to `.venv/bin/python`.
- **PyCharm**: Set the project interpreter to `.venv/bin/python`.

### Pre-commit hooks

Pre-commit runs on every `git commit` and enforces:

- **ruff** — linting + formatting
- **mypy** — strict type checking on `osimflow/`
- **gitleaks** — secrets detection
- **AGENTS.md contract** — catches stale docs
- **docs sync** — catches broken file references
- **unit + contract tests** — fast gate

If a hook fails, fix the issue and re-commit. Do not skip hooks with
`--no-verify` in normal development.

```bash
# Run all hooks manually (the pre-push safety net)
make precommit
```

---

## 5. Running Tests

All tests run through the project `.venv`. Use `make` targets or
invoke `.venv/bin/pytest` directly.

### Common commands

| Command | What it does |
|---|---|
| `make test` | Full pytest suite |
| `make test-fast` | Contract + unit only (~10s, pre-commit mirror) |
| `make test-cov` | Full suite + 85% coverage gate |
| `make lint` | ruff check (read-only) |
| `make format` | ruff format (writes) |
| `make typecheck` | mypy --strict on osimflow/ |
| `make contract` | AGENTS.md + docs drift checks |

### Running a single test file

```bash
.venv/bin/pytest tests/integration/test_cache_invalidation.py -v
```

### Running with coverage

```bash
make test-cov
# Or manually:
.venv/bin/pytest --cov=osimflow --cov-report=term-missing --cov-fail-under=85
```

The coverage gate is 85%. If it fails, the output shows which lines
are uncovered. Add tests for the missing public-API paths.

### Integration tests

The integration tests in `tests/integration/` run end-to-end campaigns
against each executor substrate:

| File | What it tests |
|---|---|
| `test_local_executor.py` | `LocalExecutor` happy path |
| `test_slurm_executor_debug.py` | `SlurmExecutor(debug=True)` (no real cluster) |
| `test_aws_batch_executor_stub.py` | `AWSBatchExecutor` with mocked boto3 |
| `test_cache_resume.py` | Re-run same campaign; warm run must be 5x faster |
| `test_cache_invalidation.py` | 8 cache-invalidation scenarios |

These all use the built-in stub mode (`OSIMFLOW_STUB_SIM` not needed;
it's the default when `openstudio.cli` is not on PATH).

### Real OpenStudio E2E test

```bash
OSIMFLOW_RUN_REAL_OPENSTUDIO=1 .venv/bin/pytest tests/integration/test_local_executor.py -v
```

This requires `openstudio.cli` on PATH or Docker with the
`nrel/openstudio` image.

---

## 6. Code Style

The rules are enforced by CI. Run `make precommit` before pushing.

### Key rules

- **PEP 8** with 100-char line length (configured in `pyproject.toml`).
- **Type hints everywhere** on public functions. Enforced by
  `mypy --strict`.
- **`pathlib.Path`** over `os.path`. Use `logging` (not `print`).
- **Exceptions**: catch, log with `exc_info=True`, **re-raise**. Never
  swallow.
- **Python 3.12+**. No `from __future__ import annotations`.
- **CLI**: `argparse` with subcommands (`osimflow run ...`).
- **No comments** unless asked. Code should be self-documenting.

### Tooling

| Tool | Config location | What it checks |
|---|---|---|
| ruff (lint) | `pyproject.toml [tool.ruff.lint]` | E, W, F, I, UP, B, SIM, PL |
| ruff (format) | `pyproject.toml [tool.ruff]` | 100-char line length |
| mypy | `pyproject.toml [tool.mypy]` | strict mode on `osimflow/` |
| pre-commit | `.pre-commit-config.yaml` | All of the above + gitleaks |

### Per-file relaxations

- `bin/*.py` — relaxed ruff rules (PL, SIM ignored). These are CLI
  scripts with top-level side effects.
- `tests/` — relaxed ruff rules. Uses pytest patterns.
- `__init__.py` — `F401` (unused import) ignored for re-exports.

---

## 7. Adding a New Executor

All executors live in `osimflow/executors/__init__.py` and subclass
`BaseExecutor`. Here is the step-by-step guide.

### Step 1: Subclass BaseExecutor

```python
# osimflow/executors/__init__.py

class MyNewExecutor(BaseExecutor):
    """My new executor."""

    name = "my_new"

    def __init__(self, endpoint: str = "http://localhost:8080"):
        # Lazy-import heavy dependencies so the local/slurm paths
        # do not pay the import cost.
        ...

    def submit(
        self,
        fn: Callable[..., Any],
        *args: Any,
        name: str = "task",
        cpus: int = 1,
        memory_mb: int = 1024,
        time_min: int = 60,
        container: str | None = None,
        **kwargs: Any,
    ) -> Handle:
        # Submit the work to your substrate.
        # Return a Handle that wraps a Future-like object.
        ...
        return Handle(job_id="...", _future=fut)

    def shutdown(self) -> None:
        # Clean up resources (connections, pools, etc.)
        ...
```

### Step 2: Wire into the CLI

Edit `osimflow/__main__.py`:

1. Add the executor to `_build_executor`:

```python
def _build_executor(args: argparse.Namespace) -> BaseExecutor:
    ...
    if args.executor == "my_new":
        return MyNewExecutor(endpoint=args.my_new_endpoint)
    raise ValueError(f"unknown executor: {args.executor}")
```

2. Add the `--executor` choice to `_build_parser`:

```python
run.add_argument(
    "--executor",
    choices=["local", "slurm", "aws_batch", "nomad", "my_new"],
    default="local",
)
```

3. Add any executor-specific flags:

```python
run.add_argument("--my-new-endpoint", default="http://localhost:8080")
```

### Step 3: Wire into CampaignConfig

If your executor has new config fields, add them to `CampaignConfig`
in `osimflow/config.py` and resolve them in `load_config()`.

### Step 4: Add to `osimflow/__init__.py`

Export the new executor class from the package's public API.

### Step 5: Write tests

Add `tests/integration/test_my_new_executor.py`. Follow the pattern
from `test_aws_batch_executor_stub.py` — mock the external service
and verify the Handle contract.

### Step 6: Update AGENTS.md

Add the new executor to:
- §2 (Stack at a glance)
- §3 (Directory map — if new files were added)
- §4 (Build & run commands — CLI flags)
- §5 (Testing — integration test reference)
- §9 (Task routing table)

The contract check (`make contract`) will fail if you forget.

---

## 8. Adding a New DAG Step

Each step is a method on the `Campaign` class in
`osimflow/campaign.py`. Here is the step-by-step guide.

### Step 1: Add the method

Follow the pattern of an existing step. Here is a minimal example:

```python
def step_my_new_step(self, inputs: SomeType) -> SomeOutputType:
    """Description of what this step does."""
    t0 = time.time()

    # Build the cache key
    inputs_hash = sha256_of_dict({"inputs": str(inputs)})
    key = CacheKey(
        step="MY_NEW_STEP",
        sample_id="ALL",       # or per-sample: sid
        openstudio_version="N/A",
        inputs_sha256=inputs_hash,
        code_sha256=self.code_hashes["bin"],
        container_digest=CONTAINER_PY,
    )

    # Check cache
    cached = self.cache.lookup(key)
    if cached:
        self.trace.step_finished(
            "MY_NEW_STEP", cache="HIT",
            elapsed_s=time.time() - t0, exit_code=0,
        )
        return cached

    # Submit work
    handle = self.executor.submit(
        my_work_function, inputs,
        name="my_new_step",
        cpus=1, memory_mb=1024, time_min=5,
        container=CONTAINER_PY,
    )
    try:
        result = handle.result(timeout=120)
        self.cache.store(key, Path(result), exit_code=0)
        self.trace.step_finished(
            "MY_NEW_STEP", cache="MISS",
            elapsed_s=time.time() - t0, exit_code=0,
        )
        return result
    except Exception as e:
        log.error("MY_NEW_STEP failed: %s", e)
        self.trace.step_finished(
            "MY_NEW_STEP", cache="MISS",
            elapsed_s=time.time() - t0, exit_code=1,
        )
        raise
```

### Step 2: Call it from Campaign.run()

Edit the `run()` method to call your step in the right order:

```python
def run(self) -> dict[str, object]:
    ...
    samples = self.step_generate_lhs()
    parameterized = self.step_apply_parameters(samples)
    simulated = self.step_run_openstudio_sim(parameterized)
    # Your new step goes here (or wherever is appropriate):
    my_result = self.step_my_new_step(simulated)
    kpi_files = self.step_extract_kpis(simulated)
    ...
```

### Step 3: Add the work function

If the step does real work, add a function in `osimflow/work.py` and
a corresponding `bin/*.py` script. The work function calls the bin
script via `subprocess.run`.

### Step 4: Emit trace hooks

For fan-out steps (per-sample), also call:

```python
self.trace.step_started("MY_NEW_STEP", total=len(samples))
# ... per sample ...
self.trace.step_item_done("MY_NEW_STEP", status="ok")
```

### Step 5: Update the per-sample accumulator

If the step is per-sample, update `self._sample_state[sid]` with the
exit code so `_finalize_samples()` can compute the sample's overall
status.

### Step 6: Update AGENTS.md and docs

Add the new step to:
- `AGENTS.md` §3 (Directory map)
- `AGENTS.md` §4 (DAG step names)
- The directory map in this file (§3)

---

## 9. Adding a New CLI Flag

Adding a flag touches three files in sequence.

### Step 1: Add the argparse argument

In `osimflow/__main__.py`, add to `_build_parser()`:

```python
run.add_argument(
    "--my-new-flag",
    type=str,
    default="default_value",
    help="Description of what this flag does.",
)
```

### Step 2: Add to CampaignConfig

In `osimflow/config.py`, add the field to the `CampaignConfig`
dataclass:

```python
@dataclasses.dataclass
class CampaignConfig:
    ...
    my_new_flag: str = "default_value"
```

And resolve it in `load_config()`:

```python
return CampaignConfig(
    ...
    my_new_flag=str(args.get("my_new_flag", "default_value")),
)
```

### Step 3: Use it in the Campaign

In `osimflow/campaign.py`, read `self.cfg.my_new_flag` wherever
needed.

### Step 4: Update AGENTS.md

Add to §4 (CLI flags) and the task-routing table in §9.

---

## 10. The BYOS Contract

BYOS (Bring Your Own Script) lets users override the default
`apply_parameters` and `extract_kpis` functions by supplying a Python
file.

### How it works

1. The user writes a `.py` file in `user_scripts/`:

```python
# user_scripts/my_kpis.py
from pathlib import Path
import json

def extract_kpis(simulation_dir: Path, sample_id: str, out: Path) -> Path:
    """Custom KPI extractor."""
    kpi = {"sample_id": sample_id, "kpis": {"eui": 123.4}}
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"kpi_{sample_id}.json"
    path.write_text(json.dumps(kpi))
    return path
```

2. The user passes it on the CLI:

```bash
osimflow run \
  --executor local \
  --custom_kpi_extractor user_scripts/my_kpis.py \
  ...
```

3. The loader (`osimflow/byos.py`) discovers the function:

   - Uses `importlib.util` to load the `.py` file as a module.
   - Searches for a callable named `apply_parameters` or `extract_kpis`.
   - Returns the callable.

4. The Campaign calls it directly instead of the default.

### Function signatures

The contract is the function signature itself:

```python
def apply_parameters(
    template: Path,
    parameters: dict[str, object],
    sample_id: str,
    out: Path,
) -> Path:
    """Return the path to the modified simulation package."""
    ...

def extract_kpis(
    simulation_dir: Path,
    sample_id: str,
    out: Path,
) -> Path:
    """Return the path to the KPI JSON file."""
    ...
```

### Validation

The Campaign validates the function signature with
`inspect.signature` at construction time. A mismatch raises a clear
error before any work starts.

### Testing BYOS scripts

```bash
# Unit test: load and call the function directly
.venv/bin/pytest tests/ -k "byos" -v

# Integration: run a campaign with the custom script
osimflow run \
  --executor local \
  --custom_kpi_extractor user_scripts/my_kpis.py \
  --input_variables variables.yml \
  --template_sim_package ./example_package \
  --n_samples 3 \
  --outdir ./byos_test_results
```

---

## 11. Cache System

The cache (`osimflow/cache.py`) provides content-addressed resume
semantics. Re-running a campaign with the same inputs is nearly free.

### How it works

- **Backend**: SQLite database at `${outdir}/work/cache.sqlite`.
- **Key**: A composite of `(step, sample_id, openstudio_version,
  inputs_sha256, code_sha256, container_digest)`.
- **Lookup**: Exact match on all six fields. Returns the output path
  if the entry exists, has exit_code=0, and the output file still
  exists on disk.
- **Store**: `INSERT OR REPLACE`. The latest successful run wins.

### Cache key construction

Each `step_*` method in `campaign.py` constructs a `CacheKey`:

```python
key = CacheKey(
    step="APPLY_PARAMETERS",       # step name
    sample_id=sid,                  # per-sample or "ALL"
    openstudio_version="N/A",       # or real version for sim step
    inputs_sha256=inputs_hash,      # SHA-256 of input data
    code_sha256=self.code_hashes["bin"],  # SHA-256 of bin/*.py files
    container_digest=CONTAINER_PY,  # container image tag
)
```

### Invalidation rules

| Change | What it invalidates |
|---|---|
| Edit a `bin/*.py` file | All steps that hash `code_hashes["bin"]` |
| Edit `osimflow/work.py` | All steps (via `code_hashes["work"]`) |
| Change `variables.yml` | `GENERATE_LHS_SAMPLES` (and downstream) |
| Change `--openstudio_version` | `RUN_OPENSTUDIO_SIM` only |
| Change `template_sim_package` | `APPLY_PARAMETERS` + `RUN_OPENSTUDIO_SIM` |
| Delete an output file | The specific cache entry (detected on lookup) |

### Code hashing

The Campaign hashes all `bin/*.py` files and `osimflow/work.py` at
construction time:

```python
def _compute_code_hashes(self) -> dict[str, str]:
    from . import work
    bin_dir = Path(__file__).resolve().parent.parent / "bin"
    files = sorted(bin_dir.glob("*.py"))
    work_file = Path(inspect.getfile(work))
    return {
        "bin": sha256_of_files(files),
        "work": sha256_of_files([work_file]),
    }
```

This means editing any `bin/*.py` file invalidates the cache for
every step that references `self.code_hashes["bin"]`.

### Inspecting the cache

```bash
sqlite3 ./results/work/cache.sqlite ".schema"
sqlite3 ./results/work/cache.sqlite "SELECT step, sample_id, exit_code FROM cache_entries;"
```

### Testing cache behavior

```bash
.venv/bin/pytest tests/integration/test_cache_invalidation.py -v
.venv/bin/pytest tests/integration/test_cache_resume.py -v
```

---

## 12. Monitoring

OSimFlow uses BYO monitoring: a per-campaign `run.json` written to
`${outdir}/run.json`.

### Trace data model

```
RunTrace
├── campaign_id: str           # e.g. "2026-01-15T14-30-00"
├── started_at: float          # Unix timestamp
├── finished_at: float
├── elapsed_s: float
├── config: dict               # executor, n_samples, os_version, ...
├── summary: dict
│   ├── n_samples: int
│   ├── n_succeeded: int
│   └── n_failed: int
├── steps: list[StepTrace]
│   └── StepTrace
│       ├── step: str          # e.g. "RUN_OPENSTUDIO_SIM"
│       ├── cache: str         # "HIT", "MISS", "HIT×N", "MISS×N", "SKIPPED"
│       ├── elapsed_s: float
│       └── exit_code: int
└── per_sample: list[SampleTrace]
    └── SampleTrace
        ├── sample_id: str
        ├── status: str        # "ok", "failed", "cached"
        ├── elapsed_s: float
        ├── apply_exit_code: int
        ├── sim_exit_code: int
        ├── extract_exit_code: int
        ├── eplusout_sql: str | None
        ├── error_summary: str | None
        ├── stdout_log: str | None
        └── stderr_log: str | None
```

### Per-sample log files

Each sample's simulation output is captured to:

```
${outdir}/work/sim/<sample_id>/stdout.log
${outdir}/work/sim/<sample_id>/stderr.log
```

These are created by the Campaign before `submit()` and passed to
the executor so the subprocess writes directly to them.

### Reading run.json

```bash
# Summary
python -c "import json; r=json.load(open('results/run.json')); print(r['summary'])"

# Failed samples
python -c "
import json
r = json.load(open('results/run.json'))
for s in r['per_sample']:
    if s['status'] == 'failed':
        print(s['sample_id'], s.get('error_summary', ''))
"
```

### Optional MLflow integration

When `--mlflow_tracking_uri` is set, the Campaign logs params,
metrics, and artifacts to an MLflow tracking server. This requires
`pip install osimflow[mlflow]`.

---

## 13. CI Pipeline

### What runs on every PR

The CI workflow (`.github/workflows/ci.yml`) is split into parallel
jobs:

```
┌──────────┐  ┌────────────┐  ┌───────────┐  ┌──────────┐
│   lint    │  │ typecheck  │  │ contract  │  │ security │
│  (ruff)   │  │ (mypy)     │  │ (agents + │  │(pip-audit│
│   ~30s    │  │  ~60s      │  │  docs)    │  │  ~30s    │
└────┬─────┘  └─────┬──────┘  │  ~10s     │  └──────────┘
     │               │         └───────────┘
     └───────┬───────┘
             ▼
     ┌───────────────┐
     │     test       │
     │  (pytest,      │
     │  85% coverage) │
     │    ~2-5 min    │
     └───────────────┘
```

| Job | What it runs | Approx time |
|---|---|---|
| `lint` | `ruff check .` + `ruff format --check .` | ~30s |
| `typecheck` | `mypy --strict osimflow/` | ~60s |
| `contract` | AGENTS.md drift + docs path resolution | ~10s |
| `security` | `pip-audit` against the dependency set | ~30s |
| `test` | `pytest --cov=osimflow --cov-fail-under=85` | ~2-5 min |

A green check on every required job is the gate to merge.

### Running CI locally

```bash
# Full local mirror (requires nektos/act)
make act

# Or run each check individually
make lint
make typecheck
make test-cov
make contract
```

### Using `act` (local CI mirror)

[nektos/act](https://github.com/nektos/act) runs GitHub Actions
workflows locally in a Docker container:

```bash
# Install act
# macOS:  brew install act
# Linux:  curl -fsSL https://raw.githubusercontent.com/nektos/act/master/install.sh | sudo bash

make act
```

**Known limitations:**
- Container-based jobs may not work on macOS.
- The runner environment differs from `ubuntu-latest` — expect some
  flakiness.
- The `bench` job (push to main only) is not a PR gate.

### The AGENTS.md contract

The contract check (`make contract`) verifies that `AGENTS.md` stays
in sync with the code:

1. **AGENTS.md / code drift** (`tools/check_agents_contract.py`):
   Ensures every public symbol, CLI flag, `bin/*.py` script, and
   campaign step mentioned in `AGENTS.md` exists in the codebase.

2. **Docs path resolution** (`tools/check_docs_sync.py`):
   Ensures every file path referenced in `docs/*.md` exists on disk.

If either check fails, update `AGENTS.md` and/or the docs in the same
commit as your code change.

---

## 14. Debugging Tips

### "My campaign is slow"

Check `run.json` for the bottleneck:

```bash
python -c "
import json
r = json.load(open('results/run.json'))
for s in r['steps']:
    print(f\"{s['step']:30s} cache={s['cache']:8s} elapsed={s['elapsed_s']:.1f}s\")
"
```

Look for steps with `cache=MISS×N` that have high `elapsed_s`. Common
causes:

- First run (cold cache) — expected. Re-run for the warm-cache path.
- `RUN_OPENSTUDIO_SIM` is genuinely heavy (5 min to 4 h per sample).
- Local executor is sequential — increase `--max-workers`.

### "My `bin/*.py` edit didn't invalidate the cache"

The cache hashes all `bin/*.py` files at Campaign construction time.
If your edit is in `user_scripts/`, it is **not** hashed — BYOS
scripts are not part of the cache key. This is by design: the user
opts into a custom script and owns the reproducibility.

If you edited a `bin/*.py` file and the cache didn't invalidate:

1. Verify the file is in `bin/` (not `user_scripts/`).
2. Delete the cache database: `rm results/work/cache.sqlite`.
3. Re-run the campaign.

### "ModuleNotFoundError: submitit / boto3"

You're using the system `pytest` instead of the project `.venv`:

```bash
# Wrong:
pytest

# Correct:
make test
# or:
.venv/bin/pytest
```

### "mypy complains about a third-party stub"

Add the type stub to `pyproject.toml` under `[project.optional-dependencies] dev`:

```toml
dev = [
    ...
    "types-PyYAML",
    # "types-requests",  # add when needed
]
```

Then `make install` to pick it up.

### "My `osimflow` import is slow"

Check whether you imported `osimflow.executors` — it pulls in `submitit`
(which imports `sentry_sdk` in some versions). Import only what you
need:

```python
from osimflow import Campaign, CampaignConfig  # fast
from osimflow.executors import LocalExecutor    # triggers submitit import
```

### "SlurmExecutor runs locally"

Without `--slurm_real`, the `SlurmExecutor` uses `submitit`'s
`DebugExecutor`, which runs jobs as local subprocesses. This is the
documented `submitit` pattern for development. Always pass
`--slurm_real` in production.

### "Real OpenStudio CLI fails — no workflow.osw"

When `openstudio.cli` is available but no `workflow.osw` exists in
the modified simulation package, the work function raises
`RuntimeError`. Ensure the `template_sim_package` contains a
`workflow.osw`.

To force stub mode even when the CLI is installed:

```bash
OSIMFLOW_STUB_SIM=1 osimflow run --executor local ...
```

### "Coverage gate fails"

```bash
make test-cov
```

The output shows uncovered lines per file. Add tests for the missing
public-API paths; the gate will clear.

### "pre-commit is slow on the first run"

`pre-commit` downloads hook repositories (ruff, black, gitleaks) on
first invocation. Subsequent runs use the cache. Clear it with:

```bash
pre-commit clean
pre-commit install
```

### Getting help

- Search existing issues:
  <https://github.com/anchapin/OSimFlow/issues>
- Open a new issue with the `question` label.
- See [`CONTRIBUTING.md`](CONTRIBUTING.md) §9 for community channels.
