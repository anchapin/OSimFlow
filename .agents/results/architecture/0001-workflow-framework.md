# ADR 0001 — Workflow Framework Choice

**Status:** accepted
**Date:** 2026-06-09
**Deciders:** OSimFlow maintainers
**Supersedes:** none
**Superseded by:** none

> **Implementation status (2026-06-09):** Ratified. The Nextflow skeleton
> has been removed; the custom Python driver has been landed at the
> project root as the `osimflow/` package. End-to-end verification
> recorded in `.agents/results/decision-verdict.md` §6.

## Context

OSimFlow is a community-driven framework for running N parallel OpenStudio energy simulations. The current foundation (Nextflow DSL2) is documented in `docs/OSimFlow.md` §4.3 but no implementation has been written. The team must choose the orchestration framework before any code is written, because the choice determines:

- The contributor learning curve and the project's documentation budget.
- How dynamic container tags (`openstudio_cli_image:<version>`) are managed across profiles.
- How `bin/*.py` Python glue participates (or does not participate) in caching.
- How the "Bring Your Own Script" (BYOS) extension surface is defined.
- How resume / partial-failure recovery is implemented.

The computational shape of the workload is: 1 LHS generator → N parameter applicators → N OpenStudio runs (heavy, 5 min–4 h each) → N KPI extractors → 1 aggregator → 1 plotter. A textbook embarrassingly-parallel fan-out / fan-in DAG with one expensive bottleneck.

## Decision

**Switch to a custom Python driver built on `submitit` (Slurm) + `dask-jobqueue` (alternative HPC) + a thin Boto3-based AWS Batch adapter, with `Snakemake` as the strongly-considered fallback.**

Reject the default of "implement the existing Nextflow design."

## Rationale

### Why custom Python first

1. The target contributor base is energy modelers who live primarily in Python and Jupyter, not DSL authors. AGENTS.md §6 and PRD §6 already flag the Nextflow learning curve as a concern.
2. The four-place container directive duplication visible in the current skeleton (`PROCESS_RUN_OPENSTUDIO_SIM.nf:24` + `conf/docker.config:33-35` + `conf/slurm.config:37-42` + `conf/aws_batch.config:42-47`) is structural, not fixable with experience.
3. `bin/*.py` is referenced by path in Nextflow, not by content, so script edits do not invalidate the cache. This is a silent-invalidation footgun because most real logic will live in `bin/`.
4. The BYOS contract is currently defined twice: as a Nextflow `val(custom_*)` plumbing path and as a Python CLI. A Python-native framework collapses this to one function signature.
5. `submitit` is Slurm-native and battle-tested; the mental model (`handle = submit(fn, ...); result = handle.result()`) is exactly right for a 1000-sample fan-out.

### Why Snakemake is the fallback

If the custom-Python spike exceeds 3 days, if Tower-native monitoring is a hard requirement, or if the team judges ongoing maintenance of a custom executor layer as a poor use of MVP time, Snakemake gives ~80% of the benefits with no custom code. The rule / Snakefile / wildcard model has a smaller learning curve than Nextflow, and BEM-adjacent communities already use it.

### Why not keep Nextflow

- The cost of switching today is zero lines of code (pre-MVP, no public release, no contributor muscle memory).
- The cost of switching after MVP ship is rewriting a community's first impressions.
- PRD §6's "comprehensive documentation" mitigation is a permanent tax on the team that compounds with every contributor.
- The four-place container duplication and the Python-glue-cache-invisibility bug class are structural, not experiential.

## Alternatives Considered

| Framework | Outcome |
|---|---|
| **Nextflow DSL2** (current) | Rejected — structural friction with target audience and workload. |
| **Snakemake** | Recommended fallback. |
| **Parsl** | Considered; `@python_app` decorators are an excellent fit, but the executor layer is less mature than `submitit` and the team is small enough that introducing both `submitit` *and* Parsl would be premature. Keep in mind for v2. |
| **Apache Airflow** | Rejected — wrong domain (ETL, not embarrassingly-parallel scientific compute). |
| **Cromwell / WDL** | Rejected — overhead disproportionate to a 6-process DAG; target audience mismatch. |
| **Prefect / Dagster** | Rejected — weak HPC story; built for data pipelines with backfills, not long-running scientific tasks with GB-scale intermediates. |

## Consequences

### Positive

- Contributors who already know Python can submit a PR on day one.
- The "dynamic container tag" problem collapses to one string in a `submit()` call.
- The BYOS interface becomes a function signature — `inspect.signature` is the validator.
- Resume semantics are explicit and auditable in code the project owns.
- Per-executor debugging is direct: a Slurm handle has a real job ID, not a Nextflow task hash.

### Negative

- Monitoring is not free. Tower-equivalent observability must be designed explicitly (per-run JSON trace + a small dashboard). Budget 1–2 days for this.
- AWS Batch integration requires more glue than Nextflow's `awsbatch` executor. Mitigated by isolating a `BatchExecutor` adapter class.
- Resume semantics diverge from Nextflow's battle-tested `-resume`. Mitigated by an explicit test suite for cache invalidation correctness.
- The team owns more code (~500–800 lines for the framework itself, plus ~100 lines per executor adapter).

### Neutral

- `bin/*.py` survives a switch unchanged — they are framework-agnostic.
- `docs/OSimFlow.md` §4.3 (the technology stack section) needs editing but the rest of the PRD survives.
- The `.github/workflows/openstudio-cli-image.yml` is unchanged.

## Validation

See `result-architecture.md` §Validation Steps for the full plan. Summary:

1. 5-sample spike on a Slurm test partition in ≤ 3 days.
2. Snakemake spike in parallel, same workload.
3. Cache invalidation prototype.
4. Monitoring decision (Tower vs. bring-your-own).
5. AWS Batch 10-sample smoke run.
6. `variables.yml` schema parity check.

## Decision Criteria for Approval

Approve the switch to custom Python if:

- Validation step 1 lands the 5-sample spike in ≤ 3 days, AND
- Validation step 3 confirms cache invalidation behaves correctly for `bin/*.py` edits and OpenStudio version changes, AND
- The team accepts that monitoring will be brought-your-own (not Tower-native), OR
- Validation step 4 reveals that Tower is not a hard requirement.

Approve the switch to Snakemake if:

- Custom Python spike exceeds 3 days, OR
- Tower native monitoring is a hard requirement, OR
- The team judges ongoing maintenance of a custom executor layer as a poor use of MVP-time.

Reject the switch and keep Nextflow only if:

- Both spikes are blocked by environment / access issues (e.g. no Slurm partition, no AWS Batch quota) that would invalidate the comparison.
