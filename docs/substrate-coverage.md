# Substrate Coverage Matrix

> **Audience:** contributors, maintainers, release managers.
> **Owner:** `@anchapin`
> **Source of truth:** this file. Update it from the PR that adds or
> modifies a real-substrate test or workflow.

This document tracks which execution substrates and external systems
have **real end-to-end coverage** in `tests/integration/test_real_*.py`
plus their corresponding `*.github/workflows/*-e2e.yml` runners.
Real-substrate tests are **skipped by default** (env-var gated) so
PR CI is not coupled to live infrastructure; the matrix below records
the gate (env var), the trigger (schedule / `workflow_dispatch` /
required-for-release), and the assertion contract.

The matrix is the response to issue **#1020** (substrate coverage
matrix missing real-E2E for Nomad HA, PBS, Dask-JobQueue,
Docker Swarm, OpenStudio CLI).

## How to add coverage for a new substrate

1. Add a test under `tests/integration/` with one of the canonical
   naming patterns:
   - **`test_real_<substrate>_campaign.py`** — the default for new
     executor substrate campaigns (matches `test_real_nomad_ha_campaign.py`,
     `test_real_pbs_campaign.py`, `test_real_dask_campaign.py`,
     `test_real_docker_swarm_campaign.py`,
     `test_real_openstudio_campaign.py`).
   - **`test_<substrate>_<descriptor>.py`** — legacy / per-aspect
     executor tests (matches `test_slurm_real_cluster.py`,
     `test_kubernetes_executor_real.py`, `test_aws_batch_real.py`,
     `test_aws_batch_cache_resume.py`, `test_azure_batch_real.py`,
     `test_google_batch_real.py`, `test_aws_batch_real_openstudio.py`).
   - **`test_<system>_real_<aspect>.py`** — non-executor real-infra
     tests against external systems (matches
     `test_mlflow_real_tracking.py`, `test_observability_real_sinks.py`).

   The 3-sample mini-campaign pattern is the canonical executor shape
   (see `tests/integration/test_aws_batch_real.py`). Every test MUST
   `pytestmark` skip when its gate env var is unset.
2. Add or update a workflow under `.github/workflows/<substrate>-e2e.yml`
   that exports the env var and runs the test against real
   infrastructure (or the workflow must `workflow_dispatch`-only).
3. Add a row to the matrix below.
4. Reference the new file in `AGENTS.md` §3 (CLI flags) / §5
   (directory map) as appropriate, and run `make contract` to confirm
   the AGENTS-contract drift gate is still green.

## Matrix

Legend:

- **Gate** — the env var that must be set for the test to unskip.
- **Trigger** — how the workflow runs:
  - `pr` — runs on every PR (gated by Docker / cluster availability).
  - `nightly` — schedule only (daily 06:00 UTC).
  - `dispatch` — `workflow_dispatch` only.
  - `release-required` — PR-blocking for `release/*` branches
    (issue #1020 acceptance criterion for the OpenStudio CLI).
- **CI workflow** — `.github/workflows/<file>.yml` that drives it.
- **Contract** — the assertions the real test enforces (every row
  asserts at least these four output artifacts:
  `aggregated_results.csv`, `failed_simulations.csv`, KPI JSONs,
  `plots/`, plus `run.json` with all DAG steps recorded).

| # | Substrate | Real-E2E test | Gate | Trigger | CI workflow | Contract |
|---|-----------|---------------|------|---------|-------------|----------|
| 1 | LocalExecutor | `tests/integration/test_local_executor.py` | n/a (always runs) | `pr` | `ci.yml` (test) | 4-artifact + run.json |
| 2 | Slurm (real cluster) | `tests/integration/test_slurm_real_cluster.py` | `OSIMFLOW_SLURM_E2E=1` + `sbatch`/`srun` on PATH | `nightly` / `dispatch` | `.github/workflows/slurm-e2e.yml` | 4-artifact + run.json + structural JOBID proof (#941) |
| 3 | AWS Batch | `tests/integration/test_aws_batch_real.py` | `OSIMFLOW_AWS_BATCH_E2E=1` + queue/job-def/region env vars | `nightly` / `dispatch` | `.github/workflows/aws-batch-e2e.yml` | 4-artifact + run.json + executor=aws_batch (#942) |
| 4 | AWS Batch cache-warm | `tests/integration/test_aws_batch_cache_resume.py` | `OSIMFLOW_AWS_BATCH_E2E=1` + S3 result-storage env vars | `nightly` / `dispatch` | `.github/workflows/aws-batch-e2e.yml` | cold + warm cache HIT on re-run (#960) |
| 4b | AWS Batch real `openstudio.cli` in `nrel/openstudio` container | `tests/integration/test_aws_batch_real_openstudio.py` | `OSIMFLOW_AWS_BATCH_E2E=1` + `OSIMFLOW_AWS_BATCH_REAL_OPENSTUDIO=1` + `OSIMFLOW_AWS_BATCH_QUEUE` + `OSIMFLOW_AWS_BATCH_JOB_DEFINITION` + `OSIMFLOW_AWS_REGION`; job-def image must be `nrel/openstudio:<v>` | `nightly` / `dispatch` (`aws-batch-real-openstudio-e2e` job) | `.github/workflows/aws-batch-e2e.yml` | 4-artifact + run.json + real `eplusout.sql` (valid EnergyPlus tables) + executor=aws_batch (#942, #1472) |
| 5 | Azure Batch | `tests/integration/test_azure_batch_real.py` | `OSIMFLOW_AZURE_BATCH_E2E=1` + account/pool/location env vars | `nightly` / `dispatch` | `.github/workflows/azure-batch-e2e.yml` | 4-artifact + run.json + executor=azure_batch (#958) |
| 6 | Google Cloud Batch | `tests/integration/test_google_batch_real.py` | `OSIMFLOW_GOOGLE_BATCH_E2E=1` + project/region/SA env vars | `nightly` / `dispatch` | `.github/workflows/google-batch-e2e.yml` | 4-artifact + run.json + executor=google_batch (#959) |
| 7 | Kubernetes | `tests/integration/test_kubernetes_executor_real.py` | `OSIMFLOW_KUBERNETES_E2E=1` + reachable kubeconfig | `nightly` / `dispatch` | `.github/workflows/kubernetes-e2e.yml` | 4-artifact + run.json + ephemeral-runner payload (#996) |
| 8 | Nomad (single-node + HA) | `tests/integration/test_real_nomad_ha_campaign.py` (+ single-node under `tests/integration/nomad_e2e/`) | `OSIMFLOW_NOMAD_E2E=1` + `NOMAD_ADDR` reachable | `nightly` / `dispatch` (single-node runs on `pr` via `ci.yml`) | `.github/workflows/nomad-e2e.yml` | 4-artifact + run.json + executor=nomad (#1020) |
| 9 | PBS / Torque | `tests/integration/test_real_pbs_campaign.py` | `OSIMFLOW_PBS_E2E=1` + `qsub`/`qstat` on PATH | `nightly` / `dispatch` | (uses `nomad-e2e.yml`-style harness — add `.github/workflows/pbs-e2e.yml` when a CI runner is provisioned) | 4-artifact + run.json + executor=pbs (#351, #1020) |
| 10 | Dask-JobQueue | `tests/integration/test_real_dask_campaign.py` | `OSIMFLOW_DASK_E2E=1` + Dask scheduler reachable (`DASK_SCHEDULER_ADDRESS`) | `nightly` / `dispatch` | (uses Dask-JobQueue cluster spec; add `.github/workflows/dask-e2e.yml` when a CI runner is provisioned) | 4-artifact + run.json + executor=dask_jobqueue (#1020) |
| 11 | Docker Swarm | `tests/integration/test_real_docker_swarm_campaign.py` | `OSIMFLOW_DOCKER_SWARM_E2E=1` + Docker daemon reachable in Swarm mode | `nightly` / `dispatch` | (add `.github/workflows/docker-swarm-e2e.yml` when a CI runner is provisioned) | 4-artifact + run.json + executor=docker_swarm (#582, #1020) |
| 12 | OpenStudio CLI | `tests/integration/test_real_openstudio_campaign.py` | `OSIMFLOW_RUN_REAL_OPENSTUDIO=1` + `openstudio.cli` on PATH | `nightly` / `dispatch` + `release-required` for `release/*` branches | `.github/workflows/openstudio-cli-e2e.yml` | 4-artifact + run.json + real `eplusout.sql` (#939, #1020) |

## MLflow tracking (separate row)

| # | External system | Real-E2E test | Gate | Trigger | CI workflow | Contract |
|---|-----------------|---------------|------|---------|-------------|----------|
| M1 | MLflow file tracking | `tests/integration/test_mlflow_real_tracking.py` | `OSIMFLOW_MLFLOW_E2E=1` | `pr` (non-blocking, additive) | `ci.yml` (`mlflow-real` job) | hermetic `file://` store + run logging (#948) |

## Observability real sinks (separate row group)

The `observability.py` pluggable backends (`CloudWatchBackend`,
`PrometheusBackend`, `OpenTelemetryBackend`) each have a
real-sink round-trip test gated on the same umbrella env var plus
per-sink readiness vars. The CI workflow provisions the sinks only
when explicitly enabled (see `aws-actions`-style OIDC).

| # | Backend | Real-E2E test | Gate | Per-sink readiness | Trigger | CI workflow | Contract |
|---|---------|---------------|------|--------------------|---------|-------------|----------|
| O1 | CloudWatch | `tests/integration/test_observability_real_sinks.py::test_cloudwatch_real_sink` | `OSIMFLOW_OBSERVABILITY_REAL=1` | `OSIMFLOW_CW_NAMESPACE` + `OSIMFLOW_CW_LOG_GROUP` (region via `OSIMFLOW_CW_REGION` / `AWS_REGION`) | `dispatch` (no dedicated nightly workflow — run on demand with creds) | (none — `dispatch` runners provision the sinks manually) | push `status` metric with unique `SampleId` → `get_metric_data` round-trip (#1472) |
| O2 | Prometheus | `tests/integration/test_observability_real_sinks.py::test_prometheus_real_sink` | `OSIMFLOW_OBSERVABILITY_REAL=1` | `OSIMFLOW_PROMETHEUS_URL` (pushgateway host:port; optional `OSIMFLOW_PROMETHEUS_JOB`) | `dispatch` | (none — `dispatch` runners provision the pushgateway) | push `status` gauge with unique `sample_id` → scrape `/metrics` round-trip (#1472) |
| O3 | OpenTelemetry | `tests/integration/test_observability_real_sinks.py::test_opentelemetry_real_sink` | `OSIMFLOW_OBSERVABILITY_REAL=1` | `OSIMFLOW_OTEL_ENDPOINT` (OTLP gRPC) + `OSIMFLOW_OTEL_OUTPUT_FILE` | `dispatch` | (none — `dispatch` runners provision the OTLP collector) | export gauge with unique `sample_id` → poll collector file export (#1472) |

## Substrates without real-E2E coverage

Executor substrates: **none** — every executor in
`osimflow/executors/` (Local, Slurm, AWS Batch, Azure Batch, Google
Batch, Kubernetes, Nomad, PBS, Dask-JobQueue, Docker Swarm) has at
least one skip-gated real-E2E test in `tests/integration/`, using
either the `test_real_<substrate>_campaign.py` or
`test_<substrate>_<descriptor>.py` naming pattern (see "How to add
coverage for a new substrate" above for the canonical enumeration).

External system coverage beyond the executor table includes MLflow
(row M1) and the three observability backends (rows O1–O3). Each new
executor (e.g. `LsfExecutor`) or external backend should add a row in
the same PR or file an issue tracking the gap.

## Acceptance criteria (issue #1020)

The original acceptance criteria were:

> At minimum, add a stub-mode integration test (3-sample campaign
> against each remaining executor) that mocks the SDK call and asserts
> the four artifacts (`aggregated_results.csv`,
> `failed_simulations.csv`, KPI JSONs, plot files) plus `run.json`.
> Promote OpenStudio CLI real-E2E to PR-blocking for `release/*`
> branches.

Each row marked with `#1020` above addresses the "stub-mode
integration test" criterion. The OpenStudio CLI row's
`release-required` trigger column addresses the PR-blocking criterion.

## Skip-gate contract

Every real-substrate test file follows the same pattern (see
`tests/integration/test_aws_batch_real.py`):

```python
pytestmark = pytest.mark.skipif(
    os.environ.get("OSIMFLOW_<SUBSTRATE>_E2E") != "1",
    reason="Set OSIMFLOW_<SUBSTRATE>_E2E=1 to run real <SUBSTRATE> tests",
)
```

Tests run `3` samples (`n_samples=3`) — small enough to keep CI cost
bounded, large enough to exercise fan-out, per-sample status, and
aggregation. Some executors (Slurm, Kubernetes) add a secondary
PATH/reachability guard before constructing the executor; see those
files for the pattern.
