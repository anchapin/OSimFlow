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

1. Add `tests/integration/test_real_<substrate>_campaign.py` with the
   3-sample mini-campaign pattern (see `tests/integration/test_aws_batch_real.py`
   for the canonical template). The test MUST `pytestmark` skip when its
   env var is unset.
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

## Substrates without real-E2E coverage

None — every executor in `osimflow/executors/` (Local, Slurm, AWS
Batch, Azure Batch, Google Batch, Kubernetes, Nomad, PBS,
Dask-JobQueue, Docker Swarm) has a `tests/integration/test_real_*.py`
file with the standard skip-gate pattern.

If a future executor is added (e.g. `LsfExecutor`), add a
`test_real_<substrate>_campaign.py` row in the same PR or file an
issue tracking the gap.

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
