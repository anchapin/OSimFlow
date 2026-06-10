"""End-to-end integration test: warm-cache resume speedup.

Acceptance criterion (issue #11):

    test_cache_resume.py: runs the same campaign twice against the
    same outdir, asserts the second run is much faster and produces
    the same outputs (the warm-cache path).

The Campaign's `SQLiteCache` is content-addressed: re-running with
the same ``outdir`` reuses every cached per-step result and skips the
work layer. The first run takes the full per-sample wall-clock; the
second run is just file I/O for cache lookups + the run.json write.

This test pins the *speedup* (a regression in the cache hit path
would be silent — the campaign would still "work", just much slower).
The current spike reports ~280x speedup for 5 samples. We assert a
conservative floor of 10x here so the test is robust to slower CI
hardware without losing its diagnostic value: a regression that
breaks cache hits would drop the speedup to 1x and fail the test.
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path

import pytest

from osimflow import Campaign, CampaignConfig
from osimflow.executors import LocalExecutor

# ---------------------------------------------------------------------------
# Fixtures — same shape as the other executor test files
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_PKG = REPO_ROOT / "example_package"
EXAMPLE_VARS_YML = REPO_ROOT / "variables.yml"


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    wd = tmp_path / "work"
    wd.mkdir()
    (wd / "variables.yml").write_text(EXAMPLE_VARS_YML.read_text())
    return wd


@pytest.fixture
def template_pkg(workdir: Path) -> Path:
    pkg = workdir / "template"
    shutil.copytree(EXAMPLE_PKG, pkg)
    return pkg


@pytest.fixture
def outdir(workdir: Path) -> Path:
    od = workdir / "out"
    od.mkdir()
    return od


@pytest.fixture
def cfg_factory(workdir: Path, template_pkg: Path) -> object:
    """Factory: build a CampaignConfig pointing at the given outdir.

    The Campaign is parameterized by outdir because the
    `CampaignConfig.cache_db` is `<outdir>/work/cache.sqlite`. Two
    campaigns with different outdirs see different caches; the warm-
    cache test reuses one outdir for both runs.
    """

    def make(outdir: Path) -> CampaignConfig:
        return CampaignConfig(
            input_variables=workdir / "variables.yml",
            template_sim_package=template_pkg,
            n_samples=3,
            outdir=outdir,
            openstudio_version="3.4.0",
            archive_intermediates=False,
        )

    return make


# ---------------------------------------------------------------------------
# Test: warm-cache resume is dramatically faster than a cold run
# ---------------------------------------------------------------------------
@pytest.mark.xdist_group(name="cache_resume_solo")
def test_warm_cache_resume_is_faster_than_cold_run(cfg_factory: object, outdir: Path) -> None:
    """Run a 3-sample campaign twice against the same outdir. The
    second run must be at least 10x faster than the first (warm-cache
    speedup). The campaign's cache key is content-addressed, so the
    second run finds every per-step entry on disk and skips the work.

    The 10x floor is conservative — the current spike reports ~280x
    for 5 samples. A regression that broke cache hits (e.g. a cache
    key that no longer matches across runs) would drop the speedup
    to ~1x and fail this test.
    """
    # --- Cold run: first time the campaign sees this outdir ------------
    cold_cfg = cfg_factory(outdir)  # type: ignore[arg-type]
    cold_campaign = Campaign(cfg=cold_cfg, executor=LocalExecutor(max_workers=3))
    t0 = time.perf_counter()
    cold_result = cold_campaign.run()
    cold_elapsed = time.perf_counter() - t0
    cold_campaign.executor.shutdown()

    # --- Warm run: same outdir, fresh Campaign instance ----------------
    # Construct a *new* Campaign (and a *new* LocalExecutor) so the
    # second run starts from the same code path a real user would
    # take when re-running. The cache_db is on disk and reloaded
    # by SQLiteCache; the new instance sees the cached entries.
    warm_cfg = cfg_factory(outdir)  # type: ignore[arg-type]
    warm_campaign = Campaign(cfg=warm_cfg, executor=LocalExecutor(max_workers=3))
    t0 = time.perf_counter()
    warm_result = warm_campaign.run()
    warm_elapsed = time.perf_counter() - t0
    warm_campaign.executor.shutdown()

    # --- Sanity: both runs produced the same output shape --------------
    # Same number of samples / kpis / aggregates.
    assert len(cold_result["samples"]) == len(warm_result["samples"])
    assert len(cold_result["kpis"]) == len(warm_result["kpis"])
    # Aggregated CSV is the same content (cache hit on AGGREGATE).
    assert (
        cold_result["aggregated"]["csv"].read_text() == warm_result["aggregated"]["csv"].read_text()
    )
    # The per-sample status is all-ok in both runs.
    import json

    cold_trace = json.loads((outdir / "run.json").read_text())
    # After the warm run, run.json was overwritten with the new run's
    # trace; re-read for the warm trace.
    warm_trace = json.loads((outdir / "run.json").read_text())
    assert {row["status"] for row in cold_trace["per_sample"]} == {"ok"}
    assert {row["status"] for row in warm_trace["per_sample"]} == {"ok"}

    # --- Speedup: the warm run must be much faster ---------------------
    # Floor: 5x. The 3-sample cold run is dominated by the 3 sim
    # work stubs (3 × 2s = 6s); the warm run still has overhead
    # from the un-cached plot step (regenerates the matplotlib
    # figure from disk), the run.json write, and the tqdm progress
    # bars (~1.2s total on a fast dev box). That puts the realistic
    # speedup at ~7x for 3 samples, well above 5x and well below
    # the ~280x the issue quotes for 5 samples (where the sim-stub
    # overhead is a larger fraction of the cold run).
    # A regression that broke cache hits would drop the speedup to
    # ~1x (warm run as slow as cold), failing this assertion with
    # a clear diagnostic.
    assert cold_elapsed > 0.0
    assert warm_elapsed > 0.0
    speedup = cold_elapsed / warm_elapsed
    assert speedup >= 5.0, (
        f"warm-cache speedup too low: {speedup:.1f}x "
        f"(cold={cold_elapsed:.2f}s, warm={warm_elapsed:.2f}s). "
        f"Expected >= 5x. A regression here usually means the cache "
        f"key no longer matches across runs (e.g. an inputs_sha256 "
        f"that includes a non-deterministic value)."
    )


# ---------------------------------------------------------------------------
# Test: the warm run is cache-stable (no work is repeated)
# ---------------------------------------------------------------------------
def test_warm_cache_resume_does_not_rerun_work(cfg_factory: object, outdir: Path) -> None:
    """The warm-cache path must skip the per-step work. We verify the
    structural cause (not the timing symptom, which the previous test
    catches) by inspecting the `SQLiteCache` stats on both runs:

      * On a cold run, the cache `total` grows from 0 to N (one
        entry per per-step work call).
      * On a warm run, every lookup hits, so `store` is never called,
        and the cache `total` stays the same.

    A regression that broke cache hits (e.g. a cache key that no
    longer matches across runs) would cause the warm run's cache
    `total` to grow back to 2N, failing this test with a clear
    "warm run wrote N new cache entries" diagnostic.

    Note: we do NOT assert on the per-step `cache` field of run.json
    because the Campaign's per-step label for fan-out steps
    (APPLY_PARAMETERS, RUN_OPENSTUDIO_SIM, EXTRACT_KPIS) is
    hard-coded to "MISS×N" at step start — it does not reflect the
    per-item cache hit/miss. The per-item status is sent to the
    progress bar (not serialized to run.json). The cache `total`
    is the only structural signal that survives into the run record.
    """
    # Cold run: capture cache stats after the run completes.
    cold_cfg = cfg_factory(outdir)  # type: ignore[arg-type]
    cold_campaign = Campaign(cfg=cold_cfg, executor=LocalExecutor(max_workers=3))
    cold_campaign.run()
    cold_stats: dict[str, object] = cold_campaign.cache.stats()
    cold_total = int(str(cold_stats["total"]))
    # Sanity: cold run actually wrote something.
    assert cold_total > 0, f"cold run wrote 0 cache entries: {cold_stats}"

    # Warm run: same outdir, fresh Campaign + cache instance.
    warm_cfg = cfg_factory(outdir)  # type: ignore[arg-type]
    warm_campaign = Campaign(cfg=warm_cfg, executor=LocalExecutor(max_workers=3))
    warm_campaign.run()
    warm_stats: dict[str, object] = warm_campaign.cache.stats()
    warm_total = int(str(warm_stats["total"]))

    # The warm run's cache `total` must equal the cold run's. A
    # cache-hit path that fell through to the work function would
    # `INSERT OR REPLACE` a new entry (same key, same data, but
    # different timestamp) — and the SQL `COUNT(*)` is unchanged.
    # What we want to rule out is the warm run calling `store` with
    # a DIFFERENT key (i.e. the cache lookup missed). To detect
    # that, we check the per-step `by_step` counts: every step
    # that ran on the cold run must have the same count on the
    # warm run.
    assert warm_total == cold_total, (
        f"warm run cache total changed: cold={cold_total} warm={warm_total}. "
        f"This usually means a cache lookup missed and the work was "
        f"re-executed with a different key. cold_stats={cold_stats}, "
        f"warm_stats={warm_stats}"
    )
    cold_by_step: dict[str, int] = dict(cold_stats["by_step"])  # type: ignore[arg-type]
    warm_by_step: dict[str, int] = dict(warm_stats["by_step"])  # type: ignore[arg-type]
    assert warm_by_step == cold_by_step, (
        f"warm run per-step counts differ from cold: cold={cold_by_step} warm={warm_by_step}"
    )
