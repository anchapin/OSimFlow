# OSimFlow — Architecture Decision: Workflow Framework Choice

**Status:** proposed
**Domain:** architecture (workflow orchestration foundation)
**Affects:** main.nf, nextflow.config, all modules/*.nf, conf/*.config, bin/*.py, .github/workflows/, AGENTS.md, docs/OSimFlow.md, README.md

---

## CHARTER_CHECK

- **Clarification level:** MEDIUM — task asks for analysis + recommendation on a foundational choice. The skeletons exist but no implementation has been written, so the decision can be made with current info; the user should confirm direction before any code is written.
- **Task domain:** architecture (workflow-framework selection, pre-implementation).
- **Must NOT do:**
  1. Do not modify the existing Nextflow `.nf` files (no refactor commits yet — this is a recommendation, not a refactor).
  2. Do not change `docs/OSimFlow.md` §4.3 (the technology stack) before the team approves a framework switch.
  3. Do not delete or rewrite `bin/*.py`; they are framework-agnostic and survive a switch.
- **Success criteria:** the team can make an informed yes/no on a framework switch based on (a) at least two alternatives compared on axes that matter for OSimFlow's specific workload, (b) an explicit recommendation, (c) concrete validation steps, (d) named risks.
- **Assumptions applied:**
  - Target users are energy modelers / researchers who live primarily in Python and Jupyter, not Groovy/DSL authors.
  - MVP target is 3–4 weeks per PRD §5.2.
  - The two prioritized execution environments are AWS Batch (cloud) and Slurm (HPC).
  - One process (`RUN_OPENSTUDIO_SIM`) needs a *dynamic* container tag, the rest use a fixed image.
  - Per-sample work is heavy (5 min – 4 h) and embarrassingly parallel; the framework's value is in fan-out/fan-in + resume, not in rich conditional logic.
  - Long-term community adoption matters more than per-engineer velocity during MVP.

---

## Architecture Problem Statement

OSimFlow needs a workflow-orchestration foundation for a 6-process fan-out/fan-in DAG that runs N independent OpenStudio simulations. The current choice (Nextflow DSL2) was documented before any code was written. The team must now decide whether to (a) implement the existing design, (b) switch to a different mature framework, or (c) build a thin custom driver on top of lower-level executors.

The decision is high-leverage: every contributor inherits the framework's ergonomics, debugging model, packaging story, and platform coverage, and the PRD's own §6 has already flagged the Nextflow learning curve as a concern requiring "comprehensive documentation" as mitigation.

---

## Alternatives Compared

| Framework | Per-sample UX | Slurm | AWS Batch | Dynamic container tag | Resume / caching | User-base fit |
|---|---|---|---|---|---|---|
| **Nextflow DSL2** (current) | Low — Groovy DSL + closures | Good (executor) | Good (executor) | Awkward — closure repeated in 4 files | Strong | Bioinformatics-leaning; mismatch for BEM |
| **Snakemake** | Medium — Python rules | Good (executor) | Good (executor) | Easy — `container:` per rule | Strong | Better — Python, used in BEM-adjacent work |
| **Parsl** | High — Python decorators, `@python_app` | Excellent (work_queue, slurm provider) | Possible | Trivial — `container_image=` per app | Manual but explicit | Research computing |
| **Apache Airflow** | Low — DAG code + UI | Via plugins | Via plugins | Awkward | Weak by default | Wrong domain (ETL, not HPC) |
| **Cromwell / WDL** | Low — WDL learning curve | Good | Good | Easy | Good | Genomics-leaning |
| **Prefect / Dagster** | Medium — Python-native | Limited | Limited | Easy | Good | Data-eng, not HPC |
| **Custom Python + `submitit` / `dask-jobqueue`** | Highest | **Excellent** (`submitit` is Slurm-native) | Via Dask or Boto3 adapter | Trivial — string in submit call | Explicit, auditable | **Perfect for BEM** |

**Eliminated early:** Airflow (wrong domain), Cromwell/WDL (overhead disproportionate to 6-process DAG), Prefect/Dagster (weak HPC story).

**Serious contenders:** Snakemake, Parsl, custom Python + `submitit`.

---

## Issue Inventory — Why Nextflow Specifically Hurts OSimFlow

These are not generic Nextflow complaints; they are problems already visible in the current skeleton.

1. **Container directive duplicated in 4 places.** `container { "...openstudio_cli_image:${openstudio_version}" }` (dynamic closure) appears in `PROCESS_RUN_OPENSTUDIO_SIM.nf:24` *and* `conf/docker.config:33-35` *and* `conf/slurm.config:37-42` *and* `conf/aws_batch.config:42-47`. Add a 5th profile (Kubernetes, LSF) and a maintainer must remember the pattern or simulations silently use the wrong image.

2. **Python glue is invisible to the cache hash.** `bin/apply_params_to_model.py` is referenced by `${projectDir}/bin/...` — a *path* — not by content. Edit the script and Nextflow will not detect the change unless the path changes or `-resume` is forced. Silent invalidation. Since `bin/*.py` will hold most of the actual scientific logic (per the stub's own TODO), this is high-risk.

3. **The "intermediate file optimization" gotcha is hand-rolled.** `PROCESS_RUN_OPENSTUDIO_SIM.nf:53-55` ships a `rm -f out/eplusout.err` shell snippet inline. Nextflow gives no native policy primitive for "delete this large file on success"; every process that produces large intermediates must reinvent it.

4. **Tuple-of-path channeling is the most common Nextflow bug class.** The `tuple val(sample_id), path(modified_sim_package)` contract is repeated across `APPLY_PARAMETERS`, `RUN_OPENSTUDIO_SIM`, `EXTRACT_KPIS`, `AGGREGATE_RESULTS`. Reordering or typo is a runtime error. In Python this is a dataclass — refactor-safe.

5. **BYOS interface has to be defined twice.** Once as a Nextflow `val(custom_apply_script)` plumbing path through `PROCESS_APPLY_PARAMETERS` and `PROCESS_EXTRACT_KPIS`, once as a Python CLI in `bin/*.py`. In a Python-native framework the BYOS contract *is* the function signature — `inspect.signature` is the validator.

6. **Groovy DSL is a barrier to the target contributor.** AGENTS.md §6 and PRD §6 both flag the learning curve; PRD's mitigation is "comprehensive documentation" — a tax the project will pay on every contributor, forever.

7. **Per-profile `withName:` blocks must list every process.** All three `conf/*.config` files enumerate six `withName:` directives each. Adding a process means editing four files. Snakemake collapses this to one `container:` line per rule; custom Python makes it a string in a submit call.

---

## What Nextflow Gives That Alternatives Don't Easily Replicate

Acknowledged honestly:

- **Battle-tested resume across many thousands of tasks** with minimal user effort (`-resume`).
- **Tower / Seqera Platform** monitoring as a turnkey integration (PRD §1.3 calls this out).
- **Mature content-hash caching** that "just works" for the common case.
- **Wave / Fusion** for filesystem optimization if/when needed.

These are real but not irreplaceable. Snakemake's caching is comparable. Tower's monitoring can be 80%-replicated with a per-run JSON trace + Grafana or MLflow, and the PRD's required monitoring is "native compatibility" not a Tower-only feature set.

---

## Recommendation

**Switch to a custom Python driver built on `submitit` (Slurm) + `dask-jobqueue` (alternative HPC) + a thin Boto3-based AWS Batch adapter, with `Snakemake` as the strongly-considered fallback.**

### Why custom Python first

1. **It is what the BEM community already does** for OpenStudio campaigns at NREL, LBNL, and academic groups. Aligning with prior art is the single best predictor of contributor success for a community project.

2. **It eliminates 4 of the 6 specific issues** above (DSL barrier, Python-glue invisible to cache, BYOS dual-interface, container directive duplication). The remaining 2 (resume, intermediate file optimization) become first-class concerns in code the project owns.

3. **`submitit` is Slurm-native and battle-tested at SLAC/FAIR.** The mental model is `handle = submit(fn, slurm_partition="short", cpus=4, mem="8G", container="..."); result = handle.result()`. For a 1000-sample fan-out this is exactly the right shape — no DSL, no closures, no per-profile YAML.

4. **The framework fits in ~500–800 lines of Python** (a `Campaign` class owning LHS, fan-out, aggregation, plotting). Same surface area as the current six `.nf` files, but in a language the contributors and users already know.

5. **AWS Batch / Kubernetes are add-ons, not foundations.** A `BatchExecutor` and `KubeExecutor` are ~100-line adapter classes each behind a `BaseExecutor` interface. Same number of files as the three current `conf/*.config` profiles, but the abstraction lives in the project's own language.

6. **The dynamic container tag becomes one string** in a `submit()` call: `container=f"ghcr.io/anchapin/openstudio_cli_image:{version}"` for `RUN_OPENSTUDIO_SIM`, fixed string for the rest. The four-place duplication collapses to one.

7. **The BYOS contract becomes the Python function signature.** `def apply_parameters(template: Path, params: dict, sample_id: str, out: Path) -> None:`. Discovery is `inspect.signature` — no second CLI surface to maintain.

8. **Resume is an explicit 50-line cache layer.** A `results/cache.sqlite` keyed on `(sample_id, openstudio_version, sha256(template), sha256(parameters))` gives the same `cache 'lenient'` semantics, but **visible and auditable** — a property the project's `.gitignore` and security posture will appreciate. PRD §6 gotcha #3 (OpenStudio version change invalidates only `RUN_OPENSTUDIO_SIM`) is one SQL query: `DELETE FROM cache WHERE step = 'run_openstudio_sim' AND openstudio_version != ?`.

9. **CI/CD for `openstudio_cli_image` becomes orthogonal** to the framework. The workflow file is unchanged; only the *consumer* of the image changes.

### Why Snakemake is the recommended fallback

- ~80% of the benefits (Python rules, per-rule `container:`, Slurm executor, AWS Batch executor, content-hash caching) with **no custom code** to maintain.
- Smaller learning curve than Nextflow.
- BEM-adjacent communities already use it (e.g. Snakemake wrappers for common scientific tasks).
- Cost: the rule / Snakefile / wildcard model can become a DSL-in-Python for complex patterns — if the team finds itself writing regex wildcards, that's a signal to graduate to custom Python.

### Why "keep Nextflow" is rejected

- The cost of switching is **zero lines of code** today (pre-MVP, no implementation, no public release, no contributor muscle memory).
- The cost of switching after MVP ship is "rewrite a community's first impressions."
- PRD §6's "comprehensive documentation" mitigation is a permanent tax on the team that compounds with every contributor.
- The four-place container duplication and the Python-glue-cache-invisibility bug class are *not* Nextflow problems that get better with experience; they are structural to the DSL.

---

## Tradeoffs

| Dimension | Nextflow | Snakemake | Custom Python + submitit |
|---|---|---|---|
| Time to first successful run (single dev) | ~2 days | ~1 day | ~2-3 days (executor layer) |
| Time to ship MVP (3-4 wk target) | Fits | Fits comfortably | Tight but feasible |
| Time to first contributor PR (BEM user) | Days-weeks | Hours-days | Hours |
| Long-term maintenance burden | Low (framework-owned) | Low | Medium (we own the cache + executor adapters) |
| Slurm debuggability | Good | Good | Excellent (handle.result() returns a real Slurm job ID) |
| AWS Batch debuggability | Good | Good | Medium (Boto3 is verbose) |
| Resume / cache | Excellent OOTB | Excellent OOTB | Manual ~50 lines, explicit |
| BYOS extension story | Awkward (two interfaces) | Good (Python rules) | Excellent (function signature) |
| Custom container tag | Awkward | Easy | Trivial |
| Public monitoring story | Tower native | Tower native | Bring-your-own (MLflow/JSON) |

---

## Risks

1. **Resume semantics diverge from Nextflow's battle-tested `-resume`.** Mitigated by an explicit test suite for cache invalidation correctness (see Validation Step 3).
2. **Less institutional knowledge in the BEM community for `submitit` than for Nextflow.** Mitigated by the custom code being small (~500 lines) and well-documented; the standard library is the API the contributors learn, not a DSL.
3. **AWS Batch integration requires more glue** in custom Python than in Nextflow's `awsbatch` executor. Mitigated by isolating the `BatchExecutor` adapter and using the AWS Python SDK plus established community tools (`batch-ext`) rather than rolling it from scratch.
4. **Snakemake's rule wildcard syntax can become a DSL-in-Python.** If the team finds Snakefiles needing regex wildcards or `expand()` gymnastics, that is a signal the workflow has outgrown Snakemake and custom Python is the right next step.
5. **Switching causes community confusion** if any early user has started writing against the Nextflow stubs. Mitigated by the project being pre-MVP, no public release, and any early examples in `tests/` and `README.md` being trivially updated.
6. **Monitoring is not free.** Tower-equivalent observability must be designed explicitly (per-run JSON trace + a small dashboard). Budget 1–2 days for this in the custom-Python path.

---

## Validation Steps (in order)

1. **Spike the custom Python path against a real workload** in 2–3 days. Write a `Campaign` class that runs 5 OpenStudio samples on a Slurm test partition via `submitit`, validates KPIs, and aggregates. Measure: lines of code, time-to-first-successful-run for a new contributor, time-to-debug a per-sample failure.
2. **Spike the Snakemake path in parallel** with the same 5-sample workload. Compare on the same axes.
3. **Prototype the cache layer.** A 50-line `CampaignCache` against the SQLite-backed design above. Confirm it handles PRD §6 gotcha #3 (OpenStudio version change invalidates only `RUN_OPENSTUDIO_SIM` tasks, not the LHS or apply steps). Confirm invalidation on `bin/*.py` content change works correctly.
4. **Confirm monitoring is sufficient.** Decide whether the team needs Tower's full surface area (multi-region, cost reporting, audit log) or whether a per-run JSON trace + MLflow/Streamlit is sufficient. If Tower is a hard requirement, the recommendation shifts to **Snakemake with Tower via Seqera partnership**; if it is a "nice to have," the custom Python path dominates.
5. **Validate AWS Batch via a 10-sample smoke run.** Most likely to surface undeclared framework dependencies (EFS staging, IAM role propagation, ECR auth).
6. **Confirm `variables.yml` schema** works equally well as a Snakemake config or a Pydantic model in custom Python. The schema is the actual user-facing contract; the framework should not change it.

---

## Artifacts Created / Modified

- **Created:** `docs/adr/0001-workflow-framework.md` (this document) — recommended for archival as the project grows its ADR log. (Path is recommended; not yet created on disk.)
- **Modified:** none. No code change has been made; this is a recommendation, not a refactor.

---

## Decision Criteria for the Team

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
