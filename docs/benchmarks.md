# OSimFlow — Performance Benchmarks

> **Audience:** maintainers triaging CI bench failures, contributors
> writing a PR that touches the orchestrator, and reviewers trying to
> interpret a `benchmarks.json` artifact.

The performance benchmark is a small, deterministic regression test
for **orchestrator overhead** — the glue code in `osimflow/campaign.py`,
`osimflow/executors/`, and `osimflow/cache.py` that surrounds the
per-sample work functions. It is the implementation of PRD §5.2
*"Initial 'Performance Benchmarking' workflow within CI/CD to track
execution time/resource use for a small sample against different
environments."*

## How it works

The bench lives in `tests/benchmarks/bench_campaign.py`. On every
PR and push to `main`, the `bench` GitHub Actions job runs:

1. **Cold run** — runs a 3-sample campaign against
   `example_package/` with a fresh `${outdir}/work` (so the SQLite
   cache DB is empty and every step is a `MISS`).
2. **Warm run** — runs the same campaign against the same `${outdir}`
   (so every step is a `HIT`).
3. Persists the metrics to `${outdir}/benchmarks.json` and uploads
   the artifact to the `benchmarks-<python-version>` GitHub Actions
   artifact. The job fails if the cold-cache wall-clock exceeds the
   configured threshold.

The local equivalent is:

```bash
python -m tests.benchmarks.bench_campaign \
  --input_variables variables.yml \
  --template_sim_package ./example_package \
  --outdir ./bench-out \
  --openstudio_version 3.11.0 \
  --n_samples 3
```

A `pytest` mirror of the same checks lives in
`tests/benchmarks/test_bench_regression.py`; it runs in under 30s
locally and is the primary developer feedback loop. The CI `bench`
job is the cross-machine artifact collector.

## Interpreting the results

The `benchmarks.json` artifact has the following schema
(`schema_version` is `1`):

| Key | Meaning |
| --- | --- |
| `schema_version` | The artifact schema. Bump on backward-incompatible changes. |
| `campaign_id` | Timestamp-style identifier of the cold run. |
| `executor` | Executor name (`local` for the bench; future envs add `slurm` / `aws_batch`). |
| `openstudio_version` | The `--openstudio_version` the bench was run against. |
| `n_samples` | Number of samples (default 3). |
| `cold_wall_s` | **Total wall-clock for the cold run** — the headline number. A regression here is the primary signal. |
| `warm_wall_s` | Total wall-clock for the warm run. Should be ≪ `cold_wall_s` (the cache is doing its job). |
| `cold_per_step_s` | Per-step wall-clock parsed from the cold run's `run.json`. |
| `warm_per_step_s` | Per-step wall-clock parsed from the warm run's `run.json`. |
| `peak_rss` | Peak resident-set size of the bench process in bytes (POSIX only; `null` on Windows). |
| `threshold_cold_s` | The cold-cache wall-clock threshold the bench compared against. |
| `passed` | `true` iff `cold_wall_s < threshold_cold_s`. The job's exit code mirrors this flag. |

### What a healthy artifact looks like

A healthy artifact shows a `cold_wall_s` well below the threshold
(typically 10–15s on a small CI runner), and a `warm_wall_s` in the
sub-second range. The ratio of the two is the cache speedup; in the
spike we measured a 288x speedup (see
[`.agents/results/decision-verdict.md` §1](../../.agents/results/decision-verdict.md)).

### What a failing artifact looks like

A failing artifact shows `cold_wall_s ≥ threshold_cold_s` and
`passed: false`. Common causes:

- A `bin/*.py` edit that adds a slow import (e.g. a heavy ML
  library) without a `try/except` lazy import. The cache key in
  `osimflow/campaign.py:_compute_code_hashes` includes the
  `bin/*.py` hash, so an edit invalidates every step — the cold
  run re-runs the whole campaign from scratch, which is exactly
  what we want for a regression test.
- A new feature that synchronously loads a large dataset on the
  orchestrator path. Move it behind a lazy import in
  `osimflow/__init__.py` or the relevant submodule.
- A `osimflow/cache.py` change that drops the cache hit rate. The
  `warm_wall_s` will jump to be close to `cold_wall_s`; the
  `tests/benchmarks/test_bench_regression.py::test_bench_warm_is_faster_than_cold`
  test is the guard.

### Overriding the threshold

The threshold defaults to **30s** (generous for the stub work, tight
enough to catch real regressions). CI uses 60s to absorb runner
slowness; the local dev loop uses 30s. Override via env var:

```bash
OSIMFLOW_BENCH_THRESHOLD_S=120 \
  python -m tests.benchmarks.bench_campaign \
    --input_variables variables.yml \
    --template_sim_package ./example_package \
    --outdir ./bench-out
```

The threshold is recorded in the artifact so a future reader can
re-interpret the verdict without re-running.

## Adding a new environment to the bench

The current bench only exercises the `local` executor. Adding
`slurm` or `aws_batch` is a future wire-up (issue #10 §*Out of
scope*): the bench function takes an `executor` argument, so the
work function needs no changes — only the CI matrix and a small
fixture in `tests/benchmarks/test_bench_regression.py`. See the issue for the
design notes.
