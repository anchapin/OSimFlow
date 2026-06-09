# Monitoring Decision — Tower vs Bring-Your-Own (MLflow / JSON trace)

**Status:** proposed
**Date:** 2026-06-09
**Affects:** all future campaign runs (monitoring is a cross-cutting concern)

This document is one of the four validation steps the architecture
spike calls for. It is intentionally a *decision matrix*, not a
recommendation — the team should ratify the choice, and the choice
materially affects the framework path (custom Python + BYO monitoring,
or Snakemake + Tower-native).

---

## What OSimFlow Actually Needs to Monitor

Translated from the PRD:

| Need | Source | Required? |
|---|---|---|
| Per-sample wall-clock + memory + exit code | PRD §1.4 "intermediate file optimization", §5.2 "Performance Benchmarking" | **Yes** — required by the deliverable. |
| Per-step cache hit/miss count | Cache layer (custom Python) / content-hash hits (Snakemake) | **Yes** — needed to debug long campaigns. |
| Campaign-level summary (total time, n succeeded, n failed) | Aggregation step | **Yes** — obvious. |
| Live progress (X of N samples running) | Executor | **Yes** — required for "is my 1000-sample job alive?". |
| Per-sample stdout/stderr tail | Executor / container logs | **Yes** — required for debugging failed sims. |
| Cost reporting (USD per campaign) | Cloud platform | Nice-to-have (PRD §6 *Cost Optimization*). |
| Multi-region / multi-account audit log | Cloud platform | Not required for MVP. |
| Live tail of a single running sample's `eplusout.log` | Container / executor | Nice-to-have. |
| Comparison across runs (campaign A vs campaign B) | Run history DB | Nice-to-have. |
| Slack / email notifications on campaign failure | Integration | Not required for MVP. |

The "must-have" list is modest. The "nice-to-have" list is what Tower
excels at.

---

## What Each Option Gives

### Tower / Seqera Platform

| Feature | Tower capability |
|---|---|
| Per-task wall-clock + memory + exit code | Yes (native) |
| Per-step cache hit/miss | Yes (Tower shows "cached" badges) |
| Campaign summary | Yes (run reports) |
| Live progress | Yes (real-time web UI) |
| Per-task stdout/stderr | Yes (live log streaming) |
| Cost reporting | Yes (AWS Batch / cloud integration) |
| Multi-region audit log | Yes |
| Per-sample live tail of `eplusout.log` | Partial (Tower shows the task's log, not application logs inside the container — depends on the executor writing them to a known location) |
| Run comparison | Yes (Tower Run Compare) |
| Slack / email notifications | Yes |
| Tower-native Nextflow | First-class |
| Tower-native Snakemake | **Yes, since 2023** (Seqera added Snakemake support) |
| Tower-native custom Python | **No** — Tower only knows about Nextflow / Snakemake / Cromwell / AWS Batch / Azure Batch / Google Batch runtimes. A custom Python driver that doesn't go through one of these adapters is invisible to Tower. |

**Critical finding from this decision matrix**: Tower is *not free* to use
with a custom-Python driver. The options for a custom-Python user to
get Tower's monitoring are:

1. **Wrap the campaign's per-sample submissions as Nextflow tasks.**
   This is "snakemake-in-nextflow" levels of meta — high overhead.
2. **Use Seqera's "Cloud Batch" runtime** — register each campaign as
   a Tower pipeline. Requires substantial glue (Pipeline Schema,
   `tower.yml`, schema files). This is the path that some BEM teams
   have taken; it works but is a significant side-effort.
3. **Use `seqerakit`** — the official Python SDK for the Seqera
   Platform API. Limited to *post-hoc* reporting; not live monitoring.
4. **Bring your own monitoring.** The pragmatic choice.

### Bring-Your-Own: JSON trace + MLflow

A custom-Python driver can write a structured `run.json` per campaign
and (optionally) ship metrics to MLflow. The `result-architecture.md`
proposal called for "per-run JSON trace + MLflow/Streamlit." This
section makes that concrete.

**Minimum viable monitoring** (no external services):

```jsonc
// results/<campaign_id>/run.json
{
  "campaign_id": "2026-06-09T15:50-cp-spike",
  "started_at": "2026-06-09T15:50:00Z",
  "finished_at": "2026-06-09T15:50:50Z",
  "elapsed_s": 50.49,
  "executor": "local",
  "openstudio_version": "3.4.0",
  "n_samples": 5,
  "n_succeeded": 5,
  "n_failed": 0,
  "steps": [
    {"step": "GENERATE_LHS_SAMPLES", "cache": "MISS", "elapsed_s": 0.01, "exit_code": 0},
    {"step": "APPLY_PARAMETERS",     "cache": "MISS×5", "elapsed_s": 10.2, "exit_code": 0},
    {"step": "RUN_OPENSTUDIO_SIM",   "cache": "MISS×5", "elapsed_s": 30.1, "exit_code": 0},
    {"step": "EXTRACT_KPIS",         "cache": "MISS×5", "elapsed_s": 8.0,  "exit_code": 0},
    {"step": "AGGREGATE_RESULTS",    "cache": "MISS",   "elapsed_s": 0.01, "exit_code": 0},
    {"step": "GENERATE_BASIC_PLOTS", "cache": "MISS",   "elapsed_s": 0.5,  "exit_code": 0}
  ],
  "per_sample": [
    {"sample_id": "0001", "status": "ok",   "elapsed_s": 8.2, "peak_rss_mb": 1200},
    ...
  ]
}
```

This is ~50 lines of code in the Campaign class. It gives you 80% of
what Tower gives for free in the Nextflow case.

**MLflow addition** (optional, 2 hours of work):

```python
import mlflow
mlflow.set_tracking_uri("http://mlflow.local:5000")
with mlflow.start_run(run_name=campaign_id):
    mlflow.log_param("executor", "slurm")
    mlflow.log_param("openstudio_version", "3.4.0")
    mlflow.log_param("n_samples", 500)
    mlflow.log_metric("elapsed_s", 1234.5)
    mlflow.log_artifact("results/aggregated_results.csv")
    for sample in per_sample:
        mlflow.log_metric(f"sample_{sample.id}_eui", sample.eui)
```

This gives you run-comparison (compare two campaigns side-by-side),
parameter-vs-metric scatter plots, and a web UI for browsing past
campaigns. Free, self-hosted, no SaaS dependency.

---

## Decision Matrix

Scored on a 1–5 scale. Total = sum across all rows.

| Criterion | Weight | Tower | BYO (JSON + MLflow) | Notes |
|---|---:|---:|---:|---|
| **MVP must-haves (PRD §1.4, §5.2)** | | | | |
| Per-sample wall-clock + memory | high | 5 | 4 | BYO needs ~20 LoC of the executor layer; Tower has it native. |
| Live progress (X of N running) | high | 5 | 3 | Tower is a real-time web UI; BYO is a log file or terminal progress bar (`tqdm`). |
| Per-step cache hit/miss | high | 5 | 4 | BYO writes cache stats to `run.json` after the campaign. |
| Campaign summary | high | 5 | 5 | Trivial in both. |
| Per-sample stdout/stderr tail | high | 5 | 3 | BYO requires writing logs to a known location and providing a CLI `cat` command. Tower is browser-native. |
| **Total must-have score** | | **25** | **19** | Tower wins on observability, not by a huge margin. |
| **Architecture fit** | | | | |
| Native to Nextflow | n/a | 5 | 1 | Out of scope for this decision. |
| Native to Snakemake | n/a | 5 | 4 | Snakemake's `--report` + `Snakefile.report()` give basic reporting. |
| Native to custom-Python | n/a | 1 | 5 | Tower does NOT have a custom-Python adapter. Custom Python + BYO is the only path that doesn't add glue. |
| **Architecture fit score** | | 11 | 10 | Tied — depends on framework choice. |
| **Operational** | | | | |
| Per-user license cost | high | $$ (Seqera Platform, free for academic, paid for commercial) | 0 (self-hosted MLflow) | Free Tower tier for small teams exists but limited. |
| Data sovereignty (research HPC) | high | 3 | 5 | BYO keeps data on the user's infra. Tower requires shipping metadata to Seqera cloud. |
| Vendor lock-in | medium | 2 | 5 | BYO is plain JSON; you can read it forever. |
| Self-hostable | medium | 2 | 5 | MLflow self-hosts trivially. |
| **Operational score** | | 7 | 15 | BYO wins on cost, sovereignty, lock-in. |
| **Engineering effort** | | | | |
| Setup time (initial) | medium | 1 (1 day) | 4 (4 hours for JSON; 1 day for MLflow) | Tower: configure tower.yml + reports. BYO: 50 LoC. |
| Ongoing maintenance | medium | 4 (Tower handles it) | 4 (BYO is small) | Tied. |
| Debug-ability of the campaign itself | medium | 5 (Tower timeline is excellent) | 3 (need to read `run.json` or `cat outdir/work/sim/0001/stdout.log`) | Tower is better for cross-team debug; BYO is fine for solo / small team. |
| **Engineering effort score** | | 10 | 11 | Tied. |
| **GRAND TOTAL** | | **53** | **55** | **BYO wins by 2 points.** |

---

## Recommendation

**Adopt bring-your-own monitoring (JSON trace + optional MLflow) for
the MVP, with a documented upgrade path to Tower if the team grows.**

Rationale:

1. **Tower is not free for the recommended framework.** The
   `result-architecture.md` recommends custom Python, and Tower has no
   custom-Python adapter. Going Tower + custom Python means either
   wrapping every campaign in a Nextflow skin (high effort) or
   hand-rolling the Tower pipeline schema (also high effort). Both add
   1–2 weeks to the MVP timeline that the PRD does not budget for.

2. **The MVP must-have monitoring surface is small.** Per-sample
   wall-clock, live progress, cache stats, and per-sample log tail.
   50 LoC of BYO covers all of it. The remaining "nice-to-have" features
   (cost reporting, multi-region audit, run comparison, notifications)
   are not in the MVP scope and can be added incrementally.

3. **BYO is open-source-native.** OSimFlow is positioned as a
   community project. A monitoring stack that requires a Seqera
   account creates a barrier to entry. A self-hosted MLflow + JSON
   trace does not.

4. **The 2-point difference is small enough to revisit.** If the team
   grows past ~5 active maintainers, or if cost-reporting on AWS
   becomes a hard requirement (i.e. a funder audit), the upgrade path
   is clear: add a `seqerakit`-based post-hoc reporter to the custom
   Python driver, or migrate to Snakemake (which Tower supports
   natively) for a future version.

### Concrete deliverables for the MVP

- **Always-on**: `run.json` trace per campaign, written to
  `${outdir}/run.json`. Schema documented in
  `docs/monitoring-schema.md`. ~50 LoC in the Campaign class.
- **Always-on**: per-sample stdout/stderr written to
  `${outdir}/work/sim/<sample_id>/stdout.log` and `stderr.log`. ~10
  LoC in the executor wrapper.
- **Always-on**: `tqdm` progress bar in the Campaign orchestrator
  for terminal users. ~5 LoC.
- **Optional**: MLflow integration behind a `--mlflow_tracking_uri`
  flag. ~30 LoC.
- **Optional**: A `streamlit` dashboard (one file, ~100 LoC) for
  browsing past campaigns from `run.json` files.

Total engineering cost: **~100 LoC** for the always-on pieces, plus
optional 130 LoC for MLflow/Streamlit. Less than 1 day of work.

---

## Decision Criteria for the Team

Approve BYO monitoring if:
- The team is comfortable adding ~100 LoC to the Campaign class
  for `run.json` + progress bar.
- The team does not require Tower's full surface area (cost reporting,
  multi-region, audit log) at MVP.

Approve Tower + Snakemake (and reject custom Python) if:
- Tower-native monitoring is a hard requirement (regulatory, funder
  audit, etc.) — in which case the framework choice shifts to
  Snakemake per `result-architecture.md`'s fallback recommendation.
