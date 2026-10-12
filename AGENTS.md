# AGENTS.md

> **Audience:** AI coding assistants (Claude Code, Cursor, GitHub Copilot,
> Windsurf, Aider, Cline, etc.) operating in this repository. Read this
> file before proposing or writing code. Sister files
> (`CLAUDE.md`, `.cursorrules`, `.clinerules`) point here; do not duplicate
> rules into them.

---

## Table of Contents

- [0. Precedence and project-type boundaries](#0-precedence-and-project-type-boundaries)
  - [0.1 Precedence](#01-precedence) · [0.2 Project type](#02-project-type--read-this-first) · [0.3 The contract checker](#03-the-contract-checker-hard-gate)
- [1. Stack at a glance](#1-stack-at-a-glance)
- [2. Build & run commands](#2-build--run-commands)
- [3. CLI flags (compact, alphabetical, grouped)](#3-cli-flags-compact-alphabetical-grouped)
- [4. Testing](#4-testing)
- [5. Directory map (contract-checked)](#5-directory-map-contract-checked)
- [6. Code style](#6-code-style)
- [7. Domain glossary](#7-domain-glossary)
- [8. Gotchas (from PRD §6 — verify before changing)](#8-gotchas-from-prd-6--verify-before-changing)
- [9. Task routing hints](#9-task-routing-hints)
- [10. Security & data handling](#10-security--data-handling)
- [11. References](#11-references)

---

## 0. Precedence and project-type boundaries

### 0.1 Precedence

- **This `AGENTS.md` overrides** the generic agent role prompt for
  project-scoped decisions (test commands, file paths, architecture,
  conventions, doc location).
- **The generic role prompt overrides `AGENTS.md`** for cross-project
  decisions (process, tool defaults, security defaults).

### 0.2 Project type — read this first

**OSimFlow is a CLI + library hybrid that wraps the OpenStudio CLI**
(`openstudio.cli run -w workflow.osw`) to run large-scale parametric
building-energy simulation campaigns. It runs locally, on HPC
(Slurm / PBS / Nomad / Kubernetes / Dask-JobQueue), or on cloud
(AWS / Azure / Google Batch / Docker Swarm). **It is not a web
service.** There are no HTTP routes, no ORM models, no authentication
layer. Do not apply a generic Router → Service → Repository → Models
pattern.

Real layered structure:

```
Orchestrator → Executor → Work function
```

- **Orchestrator** — `osimflow/campaign.py` (`Campaign` class) drives
  the 7-step DAG.
- **Executor** — `osimflow/executors/`; 10 concrete executors
  (`LocalExecutor`, `SlurmExecutor`, `AWSBatchExecutor`,
  `NomadExecutor`, `PBSExecutor`, `AzureBatchExecutor`,
  `GoogleBatchExecutor`, `DaskJobQueueExecutor`,
  `KubernetesExecutor`, `DockerSwarmExecutor`) all conform to
  `submit()` → `Handle` from `osimflow/executors/base.py`.
- **Work function** — `osimflow/work.py` (per-step logic) +
  `osimflow/_work_scripts/` (CLI scripts invoked by the work layer).
  `bin/*.py` are thin shims over `_work_scripts/` — keep them thin.

### 0.3 The contract checker (hard gate)

`tools/check_agents_contract.py` is run by pre-commit and CI
(`make contract` / `.github/workflows/agents-contract.yml`). It
verifies that every public symbol in `osimflow/__init__.py`, every
`bin/*.py`, every executor file under `osimflow/executors/`, every
non-underscore top-level symbol defined in those executor files
(issue #1574 — private helpers are explicitly skipped, see `docs/agents-reference.md`), every
config module under `osimflow/executor_configs/` (issue #1575), every
DAG step name, and every `--flag` registered on the CLI is
**mentioned in its own scope**: DAG steps under §2 of this file;
flags under "CLI flags" and every file/symbol/config module under
"Directory map" in `docs/agents-reference.md`. Matching is
section-scoped and whole-token (a mention in an unrelated section, or a
mere substring of a longer name, does not count). The flag list is
*derived*: the checker builds the real parser (which calls the
per-executor `add_arguments` hooks from `osimflow/executor_configs/`)
and walks its actions, falling back to a textual
`add_argument("--…")` scan of `osimflow/__main__.py` +
`osimflow/executor_configs/*.py` when the package is not importable.
Stale entries (files/flags listed but no longer in the code) are not
errors. When you add any of those, update the matching section in the
same change or `make contract` will fail.

---

## 1. Stack at a glance

| Layer | Tech |
|---|---|
| Orchestration | `osimflow/` package, `osimflow run` CLI |
| Executor | `BaseExecutor` + 10 implementations |
| Slurm | `submitit.AutoExecutor` (real) / `DebugExecutor` (dev) |
| AWS Batch | `boto3`, IAM role auth (no long-lived keys), spot + on-demand |
| Containers | Docker / Singularity; consumes **`nrel/openstudio:<version>`** from Docker Hub (ADR-0002 — the project does not build this image) |
| Sampling | `scipy.stats.qmc` (LHS + plugin algorithms) |
| Data | Python 3.12+, `pandas`, `pyarrow`, `matplotlib`, `seaborn` |
| Cache | `SQLiteCache` (single-node) or `DistributedCache` (Redis, `--redis-url`) |
| Document store | `SQLiteDocumentStore` (single-node) or `RedisDocumentStore` (Redis, `--redis-url` — issue #1014) |
| Observability | per-campaign `run.json` + pluggable backends (`--observability cloudwatch\|prometheus\|opentelemetry`) |
| CI | GitHub Actions: `lint`, `typecheck`, `test`, `contract`, `security` + per-substrate E2E (nightly / `workflow_dispatch`) |

PRD: `docs/OSimFlow.md` (cite by §). Foundation decision:
`.agents/results/decision-verdict.md` and
`.agents/results/architecture/0001-workflow-framework.md`.

---

## 2. Build & run commands

The `Makefile` is the canonical day-to-day interface. **Always run
through `.venv`** — the Makefile hard-codes `.venv/bin/{python,ruff,
mypy,pytest,pre-commit}` and a bare `pytest` resolves to a different
Python that lacks the `[dev,aws,slurm,api]` extras and fails with
`ModuleNotFoundError`.

**Toolchain split (issue #1477):** local development installs with
**pip** via `make install` (authoritative locally — no `uv` needed);
CI installs the same `pyproject.toml` with **uv** (pinned via
`setup-uv`'s `version:` input, kept **identical** in every workflow
that uses it: `ci.yml`, `bench.yml`, `release.yml`,
`release-installers.yml`, `nomad-e2e.yml`). If the resolvers
disagree, the CI-resolved set is the merge gate. Details in
`docs/DEVELOPMENT.md` §4 "Toolchain: local pip vs CI uv".

```bash
make help       # list all targets
make install    # pip install -e ".[dev,aws,azure,slurm,kubernetes,api,sensitivity,optimization,ga]"   (creates .venv)
make lint       # ruff check
make format     # ruff format (write)
make typecheck  # mypy --strict on osimflow/
make test       # pytest with CI flags, no coverage gate (issue #1476)
make test-cov   # exact CI test-job invocation, 82% gate  (ci.yml runs this; issues #1417, #1476)
make test-fast  # pytest tests/contract -x -q -m "not slow and not chaos and not full" (pre-commit mirror; issue #1624)
make doctor     # check venv / extras / OpenStudio CLI and print the next step (issue #1895; tools/doctor.py)
make install-locked  # optional: uv sync --locked, mirrors the CI resolver (issue #1895)
make smoke      # 3-sample stub-mode local campaign into ./results_smoke (no OpenStudio needed; issue #1479)
make contract   # regenerate BYOS runner + agents-contract + docs-sync + openapi-sync
make byos-generate  # regenerate osimflow/_byos_runner_generated.py only
make precommit  # pre-commit run --all-files          (pre-push safety net)
make act        # local CI mirror via nektos/act
```

Extras are independent: `[mlflow]`, `[sensitivity]` (SALib),
`[optimization]` (pymoo), `[ga]` (DEAP), `[api]` (FastAPI),
`[kubernetes]`, `[aws]`, `[azure]` (azure-batch 15.x + azure-identity —
part of the dev/typecheck install sets so the strict mypy gate actually
resolves `azure.batch`; issue #1582), `[viz]` (streamlit/plotly),
`[tui]` (rich).

### Run a campaign

```bash
# Local smoke run (stub mode — no real OpenStudio needed; issue #1479)
make smoke                    # preferred — uses $(PY) -m osimflow, no venv activation required
# or, equivalently, if you have the venv activated:
.venv/bin/osimflow run \
  --executor local \
  --input_variables example_package/variables.yml \
  --template_sim_package ./example_package \
  --n_samples 3 --outdir ./results_smoke \
  --openstudio_version 3.10.0

# Slurm (real cluster — debug=False)
.venv/bin/osimflow run --executor slurm --slurm-real --slurm-partition short \
  --input_variables variables.yml --n_samples 500 \
  --openstudio_version 3.10.0

# AWS Batch — IAM role on the Batch compute env, no long-lived keys.
.venv/bin/osimflow run --executor aws_batch \
  --aws-batch-queue osimflow-batch-queue \
  --aws-batch-job-definition osimflow-openstudio-job-def \
  --input_variables variables.yml --template_sim_package ./example_package \
  --n_samples 1000 --outdir ./results --archive_intermediates
```

Re-running with the same `--outdir` is a cache hit on every step
(50 s cold → 0.1 s warm — see `decision-verdict.md` §1). Per-step
artifacts land at `${outdir}/{work,sim,kpis,plots,run.json,…}`.

### DAG steps (driven by `osimflow/campaign.py`)

`GENERATE_LHS_SAMPLES`, `PREFLIGHT_RUN_MODEL`, `APPLY_PARAMETERS`,
`RUN_OPENSTUDIO_SIM`, `EXTRACT_KPIS`, `AGGREGATE_RESULTS`,
`GENERATE_BASIC_PLOTS`. Cross-step file deps are declared in
`_STEP_DEPENDENCIES` near the top of `osimflow/campaign.py` — do not
bypass `_verify_step_inputs` when adding a step.

### CLI subcommands

`run` (campaign), `import-osa` / `import-gem-osa` / `export` (PAT/OSA I/O), `serve`
(REST, `[api]` extra), `list` / `show` / `compare` / `status` /
`download` (registry), `backup` / `restore` (registry),
`mark-for-reanalysis` / `merge` (data-point lifecycle),
`measure` / `list-measures` (BCL), `aggregate-runs` /
`query-results` / `export-results` (cross-campaign),
`health` (per-executor substrate check; `--executor <name>` promotes
that one to CRITICAL — issue #1024),
`cancel` / `pause` / `resume` (campaign lifecycle),
`run-batches` (sequential multi-batch driver, issue #1874).

---

## 3. CLI flags

The full, alphabetical, grouped flag list lives in
[`docs/agents-reference.md`](docs/agents-reference.md) (section "CLI flags"). It is
contract-checked: every flag registered on the parser must be listed there.

## 4. Testing

```bash
make test           # CI-flag suite (xdist, 120s timeout; no contract/slow/nomad_e2e/chaos), no coverage gate
make test-fast      # contract only, fast subset (pre-commit mirror; deselects slow/chaos/full)
.venv/bin/pytest tests/integration/test_cache_invalidation.py -v
.venv/bin/pytest --cov=osimflow
```

The `pytest-fast` pre-commit hook and `make test-fast` run the
IDENTICAL invocation
(`.venv/bin/pytest -o addopts="" tests/contract -x -q -m "not slow and not chaos and not full"`,
single-sourced in `.pre-commit-config.yaml` and the Makefile). The
`full` mark (issue #1624) excludes `test_coverage_gate`, whose fixture
recursively runs the entire integration+unit suite under coverage
(>10 min serial — it stalled pre-commit past usability); the enforced
82% coverage gate actually runs in `make test-cov` / the CI `test`
job. Run the heavyweight duplicate explicitly with
`pytest tests/contract -m full`. Since issue #1629 the ci.yml
`contract` CI job also runs this exact bounded contract pytest
suite, so the pre-commit mirror is enforced on every PR.

CI runs `make test-cov` (the CI `test` job calls the Makefile
target; pytest flags are single-sourced in the Makefile —
`PYTEST_CI_FLAGS` / `PYTEST_COV_FLAGS`, issue #1476) and requires
82% aggregate coverage — gated by the Makefile's
`PYTEST_COV_FLAGS` (`--cov-fail-under=82`, composed only into
`make test-cov`); `pyproject.toml [tool.pytest.ini_options]`
holds only non-coverage pytest options. In addition, after pytest
exits, `make test-cov` invokes
`tools/check_module_coverage.py` (issue #1571, extended by #1557),
which reads the ``.coverage`` data file via `coverage json` and
asserts each `osimflow/_campaign_*.py` collaborator, each
`osimflow/executors/*.py` module, and each
`osimflow/_work_scripts/*.py` per-step script (issue #1557) is at
or above a seed floor (stored in the `FLOORS` dict in that script,
with comments pointing to the measured % on the seeding commit
minus a 1.0% epsilon). The per-module floor is additive to the 82% aggregate
gate — it exists because an aggregate-only threshold structurally
cannot notice a wholly-untested newly-extracted collaborator (the
extraction PRs from #1462/#1463/#1464 are exactly that window).
The script also fails when a new in-scope file appears without a
`FLOORS` entry, so a refactor that lands a brand-new
`_campaign_*.py` cannot sneak in under the aggregate gate. Ratchet
floors up only with a fresh measurement comment in the script's
docstring; do not lower them to compensate for the aggregate
gate.

**Subprocess coverage (issue #1557):** `make test-cov` exports
`COVERAGE_PROCESS_START=$(pwd)/pyproject.toml` so the auto-installed
`coverage.pth` boots coverage in every child Python spawned by a
test (`bin/*.py` shims → `python -m osimflow._work_scripts.*`).
`[tool.coverage.run]` declares `patch = ["subprocess"]` (which
implicitly sets `parallel = true`) so each subprocess writes its own
`.coverage.<host>.<pid>.<rand>` data file that the xdist master's
`combine()` picks up via `combinable_files`'s `.coverage.*` glob.
`[tool.coverage.report]` mirrors the same `omit` list so coverage
7.16's "report aggregate includes omitted files at 0%" default does
not deflate the 82% gate (the previous baseline of 85.20% on commit
30f3c79 was implicitly measured-only). Adding subprocess coverage
brought the `_work_scripts/*` modules — the severe-error classifier
(`aggregate_results.py`), pre-flight check (`apply_params_to_model.py`),
KPI schema (`extract_kpis.py`), sampler (`generate_lhs.py`),
plotter (`generate_plots.py`), Excel adapter (`excel_to_variables.py`)
— into the gate; their per-module floors are seeded in
`tools/check_module_coverage.py`. CI jobs in
`.github/workflows/ci.yml`: `lint` (ruff check + format --check),
`typecheck` (mypy --strict), `test` (pytest + 82%), `contract`
(drift checks + the bounded `tests/contract` pytest suite, issue
#1629), `security` (pip-audit + gitleaks), `mlflow-real` (real MLflow
smoke), per-PR Nomad E2E. The non-gating `slow` (-m slow) and
`chaos` (-m chaos; deselected from the required `test` gate via
PYTEST_CI_FLAGS, issue #1468) jobs live in the separate
`.github/workflows/slow-chaos.yml` (issue #1856): push to `main`,
nightly, `workflow_dispatch`, and a path-filtered PR variant — never
in `ci.yml`, which must not gain workflow-level `paths` filters. Per-substrate E2E
(`aws-batch-e2e.yml`, `slurm-e2e.yml`, `kubernetes-e2e.yml`,
`google-batch-e2e.yml`, `azure-batch-e2e.yml`,
`nomad-e2e.yml`, `openstudio-cli-e2e.yml`) are nightly or
`workflow_dispatch`-only; their integration tests are skip-gated in
normal CI. The full real-E2E coverage matrix (gate, trigger, CI
workflow, contract) lives at `docs/substrate-coverage.md`
(issue #1020). The Slurm / PBS / Dask / Docker Swarm / Kubernetes
callers share `.github/workflows/substrate-e2e-reusable.yml`
(`workflow_call`, issue #1896) and keep their own triggers and test paths.

For every new public surface, add a test in `tests/unit/` or
`tests/integration/`. Executor integration tests follow
`test_local_executor.py`, `test_slurm_executor_debug.py`,
`test_aws_batch_executor_stub.py` — 3-sample campaign asserting
the four artifacts (`aggregated_results.csv`,
`failed_simulations.csv`, KPI JSONs, plot files) plus `run.json`.
For real `bin/*.py` work, also add per-step unit tests and
pre-flight parameter-check tests (LHS variable must map to a real
measure argument / `.osm` attribute). Touching the per-sample work
hot path? Add a perf smoke at `tests/benchmarks/bench_campaign.py`
(3-sample cold + warm).

---

## 5. Directory map

The per-file/per-symbol directory map lives in
[`docs/agents-reference.md`](docs/agents-reference.md) (section "Directory map").
Adding a public file, executor, executor config module, `bin/*.py` script, or
`osimflow/__init__.py` symbol requires an entry there (`make contract` enforces it).

## 6. Code style

- **Python 3.12+**, PEP 8, full type hints on public functions
  (enforced by `mypy --strict` on `osimflow/`).
- `pathlib.Path` over `os.path`. `logging` over `print`.
- Exceptions: catch, log with `exc_info=True`, **re-raise**.
  Never swallow.
- No `from __future__ import annotations` (3.12+ target).
- CLI entry points use `argparse` with subcommands.
- `import openstudio` calls must be isolated behind
  `try/except` with a clear error message (the
  `scientific_python_image` build does not include the heavy
  C++ stack).
- **BYOS contract:** user supplies a Python file with a
  function of the right signature; the Campaign discovers
  and calls it via `inspect.signature`. Never define the same
  contract twice (once as a Python function, once as a CLI
  surface).
- **Cache key rule:** any code that affects per-step behavior
  must be hashed into the cache key. See
  `osimflow/campaign.py:_compute_code_hashes`. Editing
  `osimflow/_work_scripts/*.py` OR `bin/*.py` invalidates the
  affected step automatically via `code_hashes["bin"]` SHA-256
  (union, sorted + deduped — issue #1021). Editing
  `osimflow/work.py` (and modules it imports) is hashed
  separately as `code_hashes["work"]` for `AGGREGATE_RESULTS`.
  The resolved algorithm's own implementation code is
  additionally digest-hashed into the `GENERATE_*_SAMPLES`
  key (`_algorithm_code_digest` in
  `osimflow/_campaign_code_hashes.py`, issue #1636) —
  upgrading a same-named third-party algorithm plugin
  invalidates cached samples. Do not bypass this hashing.
- **Executor resource directives:** `cpus`, `memory_mb`,
  `time_min` are advisory on `LocalExecutor`, propagated to
  Slurm via `submitit`'s `update_parameters`, and translated
  to Boto3 `containerOverrides` for `AWSBatchExecutor` (legacy `vcpus`/`memory` on EC2; `resourceRequirements` with a validated, never-rounded vCPU/MiB pair on Fargate job definitions, fractional `cpus` allowed — issue #1808, `docs/aws-batch-terraform.md`). Add
  new resource kinds by extending the `submit()` signature,
  not by adding process-local config.
- **Quality ratchets (issue #1892):** ruff `BLE001`/`S110`/`T20`/`PGH003`
  and mypy `ignore-without-code` are enforced on `osimflow/`; existing
  violations are baselined per file in `pyproject.toml`
  `[tool.ruff.lint.per-file-ignores]` and may only shrink — never add
  entries; fix the code instead.
- **Enforcement:** ruff (lint + format), `mypy --strict`, and
  `make contract`. Run `make precommit` before pushing; CI
  mirrors the same checks. See `docs/DEVELOPMENT.md`.

Shell / CLI: `set -euo pipefail`, long options in docs
(`--openstudio_version` not `-o`). Per-sample stdout/stderr
land at `${outdir}/work/sim/<sample_id>/{stdout,stderr}.log`.

---

## 7. Domain glossary

| Term | Meaning |
|---|---|
| **LHS** | Latin Hypercube Sampling — `scipy.stats.qmc.LatinHypercube` |
| **`.osm`** | OpenStudio Model (the parametric building energy model) |
| **`.osw`** | OpenStudio Workflow — orchestrates which measures run |
| **`.idf`** | EnergyPlus Input Data File — **out of scope** for OSimFlow |
| **`.epw`** | EnergyPlus Weather file — **out of scope** for OSimFlow |
| **`eplusout.sql`** | SQLite output of an EnergyPlus simulation — primary source for KPI extraction |
| **`eplusout.err`** | EnergyPlus error log; `failed_simulations.csv` extracts the *first* "Severe Error" line via `grep -m 1 "  * Severe"` |
| **`eplusout.log`** | EnergyPlus full log (verbose; do not archive unless debugging) |
| **EUI** | Energy Use Intensity (kWh/m²/yr or kBtu/ft²/yr) — the canonical headline KPI |
| **Measure** | OpenStudio plug-in (Ruby or Python) modifying a model or workflow; arguments exposed in `.osw` |
| **`template_sim_package`** | User-supplied dir: base `.osm`/`.osw` + any required measure scripts |
| **`variables.yml`** | User-supplied input: parameters + LHS distributions |
| **BYOS** | "Bring Your Own Script" — user-supplied Python in `user_scripts/` overriding default `bin/` logic |
| **`run.json`** | Per-campaign monitoring trace (per-step timing, per-sample status, cache hit/miss) |
| **`nrel/openstudio:<version>`** | Dynamic container image tag (Docker Hub), selected via `--openstudio_version` |

---

## 8. Gotchas (from PRD §6 — verify before changing)

1. **`eplusout.err` is huge** — delete it from the work dir
   after a successful `handle.result()` in
   `step_run_openstudio_sim`. Don't archive under
   `--archive_intermediates`.
2. **Pre-flight parameter checks** — `step_apply_parameters`
   (via `bin/apply_params_to_model.py`) must verify every LHS
   variable maps to an existing measure argument or `.osm`
   attribute **before** simulation. Fail fast with a clear
   error.
3. **OpenStudio version pinning** — the version lives in the
   **container tag** (`CONTAINER_OS.format(version=...)`)
   passed to the executor, NOT in `variables.yml` or env vars.
   See `docs/openstudio-image-distribution.md` for the
   cache-key shape.
4. **`failed_simulations.csv`** — must contain the *first*
   "Severe Error" line from each `eplusout.err` only.
   `grep -m 1 "  * Severe"`. Implemented in
   `bin/aggregate_results.py`.
5. **`--archive_intermediates`** — publishes campaign inputs
   (`template_sim_package`, `variables.yml`) **and** per-sample
   `.osw`/`.osm` + `eplusout.sql`. Do not blindly archive
   `eplusout.err` / `eplusout.log`.
6. **AWS Batch security** — IAM roles for EC2 instances, not
   long-lived keys. `AWSBatchExecutor` must source credentials
   from the IAM role on the compute env.
7. **OpenStudio Measure deps** — custom Ruby/Python measure
   deps must be packaged *inside* the `template_sim_package`,
   not installed at runtime.
8. **Large time-series** — hourly outputs get huge fast.
   Default to daily/monthly aggregates in
   `aggregated_results.csv`; keep hourly only in per-sample
   `.sql` behind `--archive_intermediates`.
9. **Cache invalidation on per-step code edits** — see §6
   cache key rule. Do not bypass.
10. **`SlurmExecutor` `debug=True` by default** — without
    `--slurm-real`, jobs run locally via
    `submitit.DebugExecutor`. Always pass `--slurm-real` in
    production.
11. **Real vs stub OpenStudio CLI** — `run_openstudio_sim`
    invokes `openstudio.cli run -w workflow.osw` when the CLI
    is on PATH (`shutil.which`). When absent, falls back to
    the stub (sleep + placeholder output). Force stub with
    `OSIMFLOW_STUB_SIM=1`. Real E2E test
    (`tests/integration/test_real_openstudio_campaign.py`) is
    skip-gated on `OSIMFLOW_RUN_REAL_OPENSTUDIO=1`.
12. **Missing `workflow.osw` in real CLI mode** — when the
    CLI is available but no `workflow.osw` exists in the
    `modified_sim_package`, the work function raises
    `RuntimeError` before invoking the CLI. The
    `template_sim_package` must always contain a
    `workflow.osw`.
13. **Distributed cache uses pid-private local SQLite files**
    — since #993 `Campaign` builds its cache via
    `build_cache`: plain `SQLiteCache` for single-node local
    mode; Redis-backed `DistributedCache` (with pid-private
    local SQLite under the hood) when `--redis-url` is set.
    Concurrent processes never lock one database.
14. **Real-substrate E2E coverage matrix (issue #1020)** —
    every executor in `osimflow/executors/` has a
    `tests/integration/test_real_<substrate>_campaign.py`
    companion: Slurm #941, AWS Batch #942, Azure #958, Google
    #959, Kubernetes, Nomad (HA), PBS, Dask-JobQueue,
    Docker Swarm, and OpenStudio CLI #939. The full matrix —
    gate env var, trigger, CI workflow, contract — lives at
    `docs/substrate-coverage.md`. The OpenStudio CLI real-E2E
    (`.github/workflows/openstudio-cli-e2e.yml`) is
    PR-blocking for `release/**` branches (issue #1020
    acceptance criterion) in addition to its nightly /
    `workflow_dispatch` runs. Real-substrate tests follow
    the canonical skip-gate pattern (`pytestmark` skipif on
    the env var) so PR CI is not coupled to live
    infrastructure.
15. **Distributed document store mirrors the cache pattern
    (issue #1014)** — `build_document_store` returns a plain
    `SQLiteDocumentStore` for single-node mode and a Redis-backed
    `RedisDocumentStore` when `--redis-url` is set.  Authoritative
    state lives in Redis (one hash per collection, JSON-encoded
    documents, per-collection auto-increment counter, atomic
    `HSETNX` unique-index enforcement); per-process LRU absorbs
    repeated reads.  The same T8.1 SQLite lock reproducer that
    #993 fixed for the cache is now closed for the document store.
    `RedisDocumentStore` fails loud on Redis outages (raises
    `DocumentStoreError`) instead of silently falling back to
    local-only state — the document store is the source of truth,
    so a Redis outage must not silently diverge workers.

---

## 9. Task routing hints

| If the user asks to… | Edit |
|---|---|
| Add a new KPI | `osimflow/_work_scripts/extract_kpis.py` (or `bin/extract_kpis.py` shim) **and** `osimflow/monitoring.py:StepTrace` schema |
| Add a new sampling algorithm | new module in `osimflow/algorithms/`, subclass `BaseAlgorithm`, register via `AlgorithmRegistry.register` in `osimflow/algorithms/__init__.py`; or declare an entry point under `[project.entry-points."osimflow.algorithms"]` in a third-party `pyproject.toml` (auto-discovered) |
| Add a new execution platform | new file in `osimflow/executors/`, subclass `BaseExecutor` from `base.py`, register via `ExecutorRegistry.register` in `osimflow/executors/__init__.py`, add the choice to `osimflow/__main__.py:_build_executor`; own the platform's `XConfig` + `add_arguments` hook in a new `osimflow/executor_configs/<name>.py` module (issue #1575); transport options arrive via the single `transport: ResultTransportConfig \| None` parameter on `_do_submit()` / the handle (issue #1541 — do NOT add per-field `result_transport_mode` / `result_storage_*` kwargs); or declare an entry point under `[project.entry-points."osimflow.executors"]` |
| Verify a third-party executor plug-in against the contract (issue #1478) | subclass `osimflow.testing.ExecutorConformanceSuite` in the plug-in's test module and point its `executor_factory` at the plug-in; for non-pytest use `osimflow.testing.run_executor_conformance`. Suite covers submit/Handle lifecycle, transport.py result-reference handling, resource directives, fanout chunk size, and health-check registration. |
| Verify a third-party algorithm plug-in against the contract (issue #1565) | subclass `osimflow.testing.AlgorithmConformanceSuite` in the plug-in's test module and point its `algorithm_factory` at the plug-in (typically `AlgorithmRegistry.get("my_plugin_name")`); for non-pytest use `osimflow.testing.run_algorithm_conformance`. Suite covers `n_samples` exactness, seed determinism, variable-name/order stability, dtype coercion via `osimflow.config.coerce_variable_type`, and cache-key reproducibility. Example: `class TestMyAlgoConformance(AlgorithmConformanceSuite): algorithm_factory = staticmethod(lambda: AlgorithmRegistry.get("my_plugin"))`. |
| Add a new step to the DAG | new method on `Campaign` in `osimflow/campaign.py`, call it from `Campaign.run`, emit `StepTrace` hooks, declare inputs/outputs in `_STEP_DEPENDENCIES`; update §2 of this file |
| Change a default OpenStudio version | `pyproject.toml` default **and** the `osimflow run --openstudio_version` default in `osimflow/__main__.py` |
| Add a user-facing CLI flag | executor-specific: the `add_arguments` hook in `osimflow/executor_configs/<name>.py` **and** (if it maps to a config field) the matching `CampaignConfig`/`XConfig` field + `load_config` parser. Campaign-wide: `osimflow/__main__.py:_build_parser` (`add_argument`) **and** the matching `CampaignConfig` field in `osimflow/config.py` **and** the `load_config` parser. Either way `docs/agents-reference.md` ("CLI flags") must list the new flag (`make contract` enforces it — the list is derived from the hooks/parser, issue #1575) |
| Change KPI output schema | `osimflow/_work_scripts/extract_kpis.py` (dict shape) **and** `osimflow/_work_scripts/aggregate_results.py` (column ordering); update `docs/agents-reference.md` / §8 of this file if it affects the contract |
| Fix a bug in parameter application | `osimflow/work.py:default_apply_parameters` first; only touch `osimflow/campaign.py:step_apply_parameters` if you also need different `Campaign` semantics (retry, cache, monitoring) |
| Work with safe expression evaluation | `osimflow/_eval_safe.py` (`safe_eval`, `ExpressionError`); used by chaos engine for variable expansion |
| Add a new cache invalidation rule | `osimflow/campaign.py:_compute_code_hashes` **and** a test in `tests/integration/test_cache_invalidation.py` |
| Add a distributed-cache invalidation rule (issues #993, #1389) | `osimflow/distributed_cache.py:DistributedCache` **and** a test in `tests/integration/test_distributed_cache_invalidation.py` (skip-gated on `fakeredis` / live Redis; asserts cross-worker Redis pub/sub broadcast) |
| Add an export format | new module in `osimflow/exporters/`, add the `--target` choice to `osimflow/__main__.py` export subcommand |
| Wire a real OpenStudio CLI invocation | `osimflow/work.py:run_openstudio_sim` — replace the stub body with `subprocess.run(["openstudio.cli", "run", ...])` and add per-sample stdout/stderr capture (the stub is already there for `OSIMFLOW_STUB_SIM=1`) |
| Change AWS Batch infrastructure (VPC, IAM, compute env) | `infra/aws/terraform/`; IAM roles in `iam.tf`, job definition in `job-definition.tf`; `terraform validate` is in CI on `infra/` path changes |
| Add a REST API endpoint | new route in `osimflow/api/app.py` **and** a test in `tests/unit/api/test_api_core.py`; re-run `python scripts/generate_openapi.py --output docs/openapi.json` afterwards; add a typed method + test in `osimflow/client.py` / `tests/unit/api/test_client.py` |
| Add or modify a health check | `osimflow/health.py` (`_check_*` function, register in `run_health_checks`) **and** a test in `tests/unit/config/test_health_check.py` |
| Sweep stale remote branches | `scripts/sweep-stale-branches.sh` (dry-run default; `--apply` deletes proven-merged; `--include-orphaned` also opts into abandoned branches; `--worktree` scans stale local worktrees) + `.github/workflows/branch-cleanup.yml` (nightly dry-run, posts to issue #1003; manual `apply=true`+`confirm=DELETE`); see `docs/branch-protection.md` §"Stale branch sweep" (issue #1003) |

### Tool selection

Tool family priority (when both standard tools and
context-mode / codebase-memory-mcp are exposed):
- Read a small file you intend to edit → `Read`.
- Read / transform a large file without showing full
  contents → `ctx_execute_file`.
- Find a function / class / route by name →
  `codebase-memory-mcp_search_graph`.
- Trace callers / callees (impact analysis) →
  `codebase-memory-mcp_trace_path`.
- Search for a string literal in a known path → `Grep`.
- Run a shell command with large / unpredictable output →
  `ctx_execute`.
- Read documentation from a URL → `ctx_fetch_and_index`.

---

## 10. Security & data handling

- **Never commit** `.osm`, `.osw`, `.idf`, `.epw`,
  `eplusout.*` files. `.gitignore` excludes them; double-check
  before staging. For very large inputs that must be tracked,
  use `git-lfs` — don't bypass the gitignore.
- **AWS:** IAM roles for EC2 compute environments only. No
  long-lived AWS access keys in the repo or in any config
  file. `AWSBatchExecutor` must source credentials from the
  IAM role on the compute env. Terraform
  (`infra/aws/terraform/iam.tf`) provisions least-privilege
  roles: a task role scoped to the campaign S3 bucket and
  CloudWatch Logs, a task-execution role for ECR image pulls,
  and a Batch service role.
- **S3 storage endpoints** (issue #1386): `--result-storage-endpoint`
  and `--s3-artifact-endpoint` must use `https://` unless
  `--allow-insecure-storage-endpoint` is set (fail-closed;
  loopback hosts exempt). Plaintext HTTP endpoints leak AWS
  SigV4 signing material in cleartext — do not enable
  `--allow-insecure-storage-endpoint` in production.
- **Coordinator URL** (issue #1550): `--coordinator-url` /
  `OSIMFLOW_COORDINATOR_URL` must use `https://` for non-loopback
  hosts. Enforced by `osimflow.manifest._validate_coordinator_url`
  at `Campaign.run()` entry (fail-fast) and in the coordinator
  clients (`report_sample_completion` refuses to send;
  `osimflow run --detach` handoff rejects with exit 1). Loopback
  (`localhost`, `127.0.0.0/8`, `::1`, `0.0.0.0`) is exempt. The
  escape hatch **reuses** `--allow-insecure-storage-endpoint`
  (no new CLI flag — keeps the escape-hatch surface minimal and
  mirrors the storage gate): a plaintext coordinator transmits the
  `Authorization: Bearer $OSIMFLOW_API_KEY` header and per-sample
  results in cleartext on every sample completion.
- **Cross-measure conflict escape hatch** (issue #1638): setting
  `OSIMFLOW_ALLOW_CROSS_MEASURE_CONFLICT` (any **non-empty** value) in the
  environment downgrades the pre-flight `CrossMeasureConflictError`
  (§8 gotcha #2, `osimflow/apply_params.py:_check_cross_measure_conflicts`)
  to a `log.warning`. Expert-only: when arguments conflict across measures,
  which measure's value wins at runtime is **undefined**. This is the only
  documented env-var bypass of a pre-flight check — no undocumented
  exceptions should exist. Tested in
  `tests/unit/apply_params/test_apply_params.py:TestPreflightCrossMeasureConflict`;
  documented in `docs/user-guide.md` §9. The email notifier's
  `OSIMFLOW_SMTP_*` knobs (credentials from env only, never committed
  config) are documented in `docs/user-guide.md` §4.4.
- **Singularity on shared HPC:** never bind-mount secrets;
  pass via env vars or `submitit`'s
  `ex.update_parameters(setup=...)`, not as container mounts.
- **BYOS user scripts:** treat user-supplied scripts as
  untrusted. Loaded via `importlib.util` and signature-
  validated with `inspect.signature`. Default trust level is
  `subprocess` (isolated child process);
  `--byos-trust-level inprocess` is the legacy in-process load
  and should be rejected in production via
  `--require-trusted-scripts`. `LocalExecutor` runs in a
  thread pool with no resource limits — when wiring
  `SlurmExecutor` to production, set a per-job timeout
  (`time_min`) to bound blast radius.
- **Nomad ACL tokens** (`infra/nomad/acl/tokens/*.json`) are
  git-ignored. Never commit.
- `gitleaks` runs in pre-commit + CI as a final safety net.

---

## 11. References

- [PRD (docs/OSimFlow.md)](docs/OSimFlow.md) — §1.4 (Key
  Differentiators), §3.1 (In-Scope Features), §4.2 (Key
  Modules/Processes), §5.2 (Phase 3 Deliverables), §6
  (Potential Challenges).
- [Architecture decision
  (`.agents/results/architecture/0001-workflow-framework.md`)](.agents/results/architecture/0001-workflow-framework.md)
  — why a custom Python driver.
- [ADR-0002
  (`.agents/results/architecture/0002-adopt-nrel-upstream-image.md`)](.agents/results/architecture/0002-adopt-nrel-upstream-image.md)
  — adopt `nrel/openstudio` directly.
- [ADR-0003
  (`.agents/results/architecture/0003-coordinator-high-availability.md`)](.agents/results/architecture/0003-coordinator-high-availability.md)
  — campaign-level HA patterns (shared-filesystem redundancy +
  campaign-per-worker).
- [ADR-0004
  (`.agents/results/architecture/0004-redis-ha-scope.md`)](.agents/results/architecture/0004-redis-ha-scope.md)
  — single-instance Redis as the scoped decision for the four
  Redis-backed planes; campaign-restart-by-replay is the recovery
  story; `osimflow health --redis-url` surfaces the deployment mode.
- [ADR-0005
  (`.agents/results/architecture/0005-queue-modules-roles.md`)](.agents/results/architecture/0005-queue-modules-roles.md)
  — `TaskHandle` subclasses the executor `Handle` (one handle
  contract, issue #1543); documents the distinct roles of the three
  queue modules: work dispatch (`taskqueue.py`) vs. crash-recovery
  journal (`jobqueue.py`) vs. control-plane broadcast
  (`distributed_jobqueue.py`).
- [Decision verdict
  (`.agents/results/decision-verdict.md`)](.agents/results/decision-verdict.md)
  — spike outcome that ratified the foundation.
- [Monitoring decision
  (`.agents/results/monitoring-decision.md`)](.agents/results/monitoring-decision.md)
  — BYO monitoring (per-campaign `run.json`).
- [DEVELOPMENT.md](docs/DEVELOPMENT.md) — day-to-day guide
  (architecture, structure, dev env, tests, style, adding
  executors/steps/flags, BYOS, cache, CI, debugging). Read
  this when you want depth.
- [User Guide (docs/user-guide.md)](docs/user-guide.md) —
  install, config, run, interpret results, troubleshoot.
- [Observability guide
  (docs/observability.md)](docs/observability.md) —
  pluggable backends (CloudWatch, Prometheus,
  OpenTelemetry).
- [AWS Batch Terraform guide
  (docs/aws-batch-terraform.md)](docs/aws-batch-terraform.md)
  — zero-to-running AWS Batch deployment (issue #130).
- [Branch protection
  (docs/branch-protection.md)](docs/branch-protection.md) —
  settings-as-code for `main` branch rules (5 required status
  checks, linear history, no reviews/no force-pushes).
  Applied post-merge via
  `scripts/apply_branch_protection.sh`; see issue #975.
- [Nomad production
  (docs/nomad-production.md)](docs/nomad-production.md) —
  HA topology, ACL model, security checklist, TLS notes.
- [CONTRIBUTING.md](docs/CONTRIBUTING.md) — contributor
  onboarding, governance entry point, PR-review checklist.
- [GOVERNANCE.md](docs/GOVERNANCE.md) — community
  governance.
- [`submitit` documentation](https://github.com/facebookincubator/submitit)
  — Slurm executor backend.
- [OpenStudio CLI reference](https://openstudio.net/docs/cli/).
