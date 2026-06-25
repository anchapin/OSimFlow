"""Real AWS Batch cache-warm/resume E2E test. Only runs when opted in.

This is the cloud counterpart of ``tests/integration/test_cache_resume.py``:
it asserts that the cache-warm resume path still works when a campaign is
backed by **real AWS Batch** plus the **S3 result storage backend**
(issue #960). The local test proves the speedup when results live on a
local filesystem; this test proves the same speedup when per-sample
results are additionally uploaded to S3 during the cold run.

How cross-run resume survives on Batch
--------------------------------------
The campaign's ``SQLiteCache`` lives at ``<outdir>/work/cache.sqlite`` —
it is **local** to the orchestrator process that drives the campaign. A
cache entry's ``output_path`` is likewise a *local* path under ``outdir``.
On a cold run the ``AWSBatchExecutor`` submits real Batch jobs, their
results land back in the local ``outdir``, and (when
``result_storage_backend="s3"`` is set) the KPIs / ``eplusout.sql`` are
*also* uploaded to S3. The cache row is written pointing at the local
path.

On a warm run against the **same** ``outdir``:

* the local ``cache.sqlite`` is re-opened by a fresh ``SQLiteCache``,
* every per-step ``cache.lookup`` hits (the local outputs never moved),
* the work layer is skipped entirely, so **zero** Batch jobs are
  submitted, and
* the warm run is bounded only by cache-lookup + ``run.json`` I/O.

The S3 backend is exercised during the cold run (uploads happen), so a
regression in ``ResultStorageUploader`` that broke the cold-run write
path — or a regression in the cache key that broke cross-run hit
detection — would surface here.

Skip gate
---------
Identical to ``test_aws_batch_real.py`` (``OSIMFLOW_AWS_BATCH_E2E=1`` plus
the ``OSIMFLOW_AWS_BATCH_QUEUE`` / ``OSIMFLOW_AWS_BATCH_JOB_DEFINITION`` /
``OSIMFLOW_AWS_REGION`` vars), with the additional
``OSIMFLOW_AWS_BATCH_RESULT_BUCKET`` var that names the S3 bucket the cold
run uploads to. The test is inert in normal CI; it is intended for the
nightly ``aws-batch-e2e`` workflow (or a manual local run against a real
Batch compute environment).

To run locally::

    export OSIMFLOW_AWS_BATCH_E2E=1
    export OSIMFLOW_AWS_BATCH_QUEUE=my-queue
    export OSIMFLOW_AWS_BATCH_JOB_DEFINITION=my-job-def
    export OSIMFLOW_AWS_REGION=us-east-1
    export OSIMFLOW_AWS_BATCH_RESULT_BUCKET=my-result-bucket
    .venv/bin/pytest tests/integration/test_aws_batch_cache_resume.py -v --timeout=3600

Cost note: bounded to ``n_samples=2`` per the issue acceptance criteria
("Bounded to <=2 samples to control cost").
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Skip gate — OSIMFLOW_AWS_BATCH_E2E + the four required env vars.
# ---------------------------------------------------------------------------
_REQUIRED_ENV = (
    "OSIMFLOW_AWS_BATCH_E2E",
    "OSIMFLOW_AWS_BATCH_QUEUE",
    "OSIMFLOW_AWS_BATCH_JOB_DEFINITION",
    "OSIMFLOW_AWS_REGION",
    "OSIMFLOW_AWS_BATCH_RESULT_BUCKET",
)

_MISSING = [v for v in _REQUIRED_ENV if os.environ.get(v) in (None, "")]

pytestmark = pytest.mark.skipif(
    bool(_MISSING),
    reason=(
        "Set OSIMFLOW_AWS_BATCH_E2E=1 plus OSIMFLOW_AWS_BATCH_QUEUE, "
        "OSIMFLOW_AWS_BATCH_JOB_DEFINITION, OSIMFLOW_AWS_REGION, and "
        "OSIMFLOW_AWS_BATCH_RESULT_BUCKET to run the real AWS Batch "
        f"cache-resume test (missing: {_MISSING})"
    ),
)

REPO_ROOT = Path(__file__).resolve().parents[2]

# Bounded to 2 samples to control real AWS spend (issue #960 acceptance).
N_SAMPLES = 2

# Warm-run must be at least this many times faster than the cold run.
# See ``_SPEEDUP_FLOOR`` rationale below.
_SPEEDUP_FLOOR = 5.0


def test_real_aws_batch_cache_warm_resume(tmp_path: Path) -> None:
    """A second campaign run against the same ``outdir`` on real AWS Batch
    must complete >=5x faster than the cold run and be fully cache-served.

    Two structural signals are asserted (timing alone is flaky on Batch):

      1. **Timing** — ``warm_elapsed < cold_elapsed / 5`` (>=5x speedup).
      2. **Cache stats** — the warm run's ``SQLiteCache`` ``total`` and
         per-step ``by_step`` counts equal the cold run's, proving no new
         cache entries were written and therefore **zero** Batch jobs were
         submitted on the warm run (a cache miss would ``INSERT OR REPLACE``
         with a fresh timestamp and, more importantly, would have had to
         submit a Batch job to produce the output).

    The ``>=5x`` floor matches the local ``test_cache_resume.py``
    expectation and the issue title. The cold run's wall-clock is
    dominated by Batch job startup + polling (minutes); the warm run is
    seconds of local I/O, so a genuine cache hit comfortably exceeds 5x.
    A regression that broke cross-run cache detection would collapse the
    speedup toward 1x (warm run re-submits every sample) and fail both
    the timing and the cache-stats assertions.
    """
    import shutil

    from osimflow import Campaign, CampaignConfig
    from osimflow.executors import AWSBatchExecutor

    queue = os.environ["OSIMFLOW_AWS_BATCH_QUEUE"]
    job_def = os.environ["OSIMFLOW_AWS_BATCH_JOB_DEFINITION"]
    region = os.environ["OSIMFLOW_AWS_REGION"]
    bucket = os.environ["OSIMFLOW_AWS_BATCH_RESULT_BUCKET"]

    # --- Hermetic fixtures (same pattern as test_aws_batch_real.py) ---
    example_pkg = REPO_ROOT / "example_package"
    example_vars = REPO_ROOT / "variables.yml"

    workdir = tmp_path / "work"
    workdir.mkdir()
    (workdir / "variables.yml").write_text(example_vars.read_text())

    template_pkg = workdir / "template"
    shutil.copytree(example_pkg, template_pkg)

    outdir = tmp_path / "out"
    outdir.mkdir()

    def make_cfg() -> CampaignConfig:
        # result_storage_backend="s3" exercises the S3 upload path on the
        # cold run (the feature under test). The cache.db remains local at
        # <outdir>/work/cache.sqlite, so the warm run hits locally.
        return CampaignConfig(
            input_variables=workdir / "variables.yml",
            template_sim_package=template_pkg,
            n_samples=N_SAMPLES,
            outdir=outdir,
            openstudio_version="3.11.0",
            archive_intermediates=False,
            result_storage_backend="s3",
            result_storage_bucket=bucket,
        )

    def make_executor() -> AWSBatchExecutor:
        return AWSBatchExecutor(
            job_queue=queue,
            job_definition=job_def,
            region_name=region,
        )

    # --- Cold run: first time the campaign sees this outdir ------------
    cold_campaign = Campaign(cfg=make_cfg(), executor=make_executor())
    t0 = time.perf_counter()
    cold_result = cold_campaign.run()
    cold_elapsed = time.perf_counter() - t0
    cold_campaign.executor.shutdown()
    cold_stats: dict[str, object] = cold_campaign.cache.stats()
    cold_total = int(str(cold_stats["total"]))  # type: ignore[arg-type]

    # Sanity: the cold run actually wrote cache entries.
    assert cold_total > 0, f"cold run wrote 0 cache entries: {cold_stats}"

    # --- Warm run: same outdir, fresh Campaign + executor --------------
    # A brand-new Campaign + AWSBatchExecutor mirrors what a real user
    # does when re-running. The local cache.sqlite is reloaded and every
    # per-step lookup hits.
    warm_campaign = Campaign(cfg=make_cfg(), executor=make_executor())
    t0 = time.perf_counter()
    warm_result = warm_campaign.run()
    warm_elapsed = time.perf_counter() - t0
    warm_campaign.executor.shutdown()
    warm_stats: dict[str, object] = warm_campaign.cache.stats()
    warm_total = int(str(warm_stats["total"]))  # type: ignore[arg-type]

    # --- Signal 1: cache stats prove zero Batch submits on warm run ----
    # Equal totals + equal per-step counts mean the warm run never called
    # ``store`` with a new key, which means every ``lookup`` hit, which
    # means no work function ran and no Batch job was submitted.
    assert warm_total == cold_total, (
        f"warm run cache total changed: cold={cold_total} warm={warm_total}. "
        f"This means a cache lookup missed and a Batch job was re-submitted. "
        f"cold_stats={cold_stats}, warm_stats={warm_stats}"
    )
    cold_by_step: dict[str, int] = dict(cold_stats["by_step"])  # type: ignore[arg-type]
    warm_by_step: dict[str, int] = dict(warm_stats["by_step"])  # type: ignore[arg-type]
    assert warm_by_step == cold_by_step, (
        f"warm run per-step cache counts differ from cold: cold={cold_by_step} warm={warm_by_step}"
    )

    # --- Signal 2: run.json shows all samples served from cache --------
    run_json = outdir / "run.json"
    assert run_json.is_file(), f"missing run.json: {run_json}"
    trace = json.loads(run_json.read_text())
    assert trace["schema_version"] == 1
    assert trace["config"]["executor"] == "aws_batch"
    assert trace["config"]["n_samples"] == N_SAMPLES
    # Every campaign step recorded on the warm run.
    step_names = {s["step"] for s in trace["steps"]}
    for required in (
        "GENERATE_LHS_SAMPLES",
        "APPLY_PARAMETERS",
        "RUN_OPENSTUDIO_SIM",
        "EXTRACT_KPIS",
        "AGGREGATE_RESULTS",
        "GENERATE_BASIC_PLOTS",
    ):
        assert required in step_names, f"step {required} missing from warm run.json"
    # Every sample completed ok (cached samples are recorded as "ok").
    per_sample = trace.get("per_sample", [])
    assert len(per_sample) == N_SAMPLES, (
        f"warm run.json per_sample has {len(per_sample)} rows, expected {N_SAMPLES}"
    )
    statuses = {row["status"] for row in per_sample}
    assert statuses <= {"ok"}, (
        f"warm run produced non-ok samples: {statuses}. A status other than "
        f"'ok' on a warm run indicates a cache miss re-ran (and possibly "
        f"failed) a sample."
    )

    # --- Signal 3: output equivalence (warm reproduces cold) -----------
    assert len(cold_result["samples"]) == len(warm_result["samples"]) == N_SAMPLES
    assert len(cold_result["kpis"]) == len(warm_result["kpis"]) == N_SAMPLES
    assert (
        cold_result["aggregated"]["csv"].read_text() == warm_result["aggregated"]["csv"].read_text()
    ), "warm run aggregated CSV differs from cold — cache hit returned wrong output"

    # --- Signal 4: timing — warm run must be >=5x faster ---------------
    # The cold run is dominated by Batch job startup + polling (minutes);
    # the warm run is local cache lookups + run.json I/O (seconds). A real
    # cache hit comfortably exceeds 5x. If this is flaky on a particularly
    # slow Batch day, the cache-stats assertion (Signal 1) is the
    # authoritative structural check — it cannot false-pass.
    assert cold_elapsed > 0.0
    assert warm_elapsed > 0.0
    speedup = cold_elapsed / warm_elapsed
    assert speedup >= _SPEEDUP_FLOOR, (
        f"warm-cache speedup too low on real AWS Batch: {speedup:.1f}x "
        f"(cold={cold_elapsed:.2f}s, warm={warm_elapsed:.2f}s). "
        f"Expected >= {_SPEEDUP_FLOOR}x. The cache-stats check above already "
        f"confirmed no Batch jobs were re-submitted, so a sub-threshold "
        f"speedup here points to local I/O / cache-lookup slowdown rather "
        f"than a cache regression."
    )
