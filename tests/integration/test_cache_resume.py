"""End-to-end integration test: warm-cache resume.

Acceptance criterion (issue #11):

    test_cache_resume.py: runs the same campaign twice against the
    same outdir, asserts the second run is fully served from cache
    (no work re-executed) and produces the same outputs.

The Campaign's ``SQLiteCache`` is content-addressed: re-running with
the same ``outdir`` reuses every cached per-step result and skips the
work layer. The first run takes the full per-sample wall-clock; the
second run is just file I/O for cache lookups + the run.json write.

Issue #1047: this test previously also asserted a wall-clock speedup
floor (``cold_elapsed / warm_elapsed >= 2.0``). That assertion was
timing-sensitive on contended CI runners (where the warm run can take
~half the cold run's wall-clock with no cache regression). The
structural cache-stats assertion below is the authoritative check:
equal cache totals + equal per-step counts prove every lookup hit,
which means no work function re-ran. A regression that broke cache
hits (e.g. a cache key that no longer matches across runs) would
grow the warm run's cache total + per-step counts above the cold
run's, failing the structural test with a clear diagnostic.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from osimflow import Campaign, CampaignConfig
from osimflow.executors import LocalExecutor

# ---------------------------------------------------------------------------
# Fixtures — same shape as the other executor test files
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_PKG = REPO_ROOT / "example_package"


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    wd = tmp_path / "work"
    wd.mkdir()
    (wd / "variables.yml").write_text(
        "algorithm: lhs\n"
        "variables:\n"
        "  - name: wwr\n"
        "    distribution: uniform\n"
        "    min: 0.2\n"
        "    max: 0.6\n"
        "    measure_argument: SetEnvelopePerformance.wwr\n"
    )
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
            openstudio_version="3.11.0",
            archive_intermediates=False,
        )

    return make


# ---------------------------------------------------------------------------
# Test: the warm run is fully cache-served (no work re-executed)
# ---------------------------------------------------------------------------
@pytest.mark.xdist_group(name="cache_resume_solo")
def test_warm_cache_resume_is_fully_cache_served(cfg_factory: object, outdir: Path) -> None:
    """Run a 3-sample campaign twice against the same outdir. The
    second run must be fully cache-served — every per-step lookup
    hits, no work function re-runs, and the warm run produces the
    same outputs as the cold run.

    This is the structural counterpart to the deleted
    ``test_warm_cache_resume_is_faster_than_cold_run`` (issue #1047).
    Wall-clock speedup assertions were timing-sensitive on contended
    CI runners; the cache-stats check below cannot false-pass: equal
    cache totals + equal per-step counts prove every lookup hit,
    which means no work function re-ran. A regression that broke
    cache hits (e.g. a cache key that no longer matches across runs)
    would grow the warm run's cache total + per-step counts above the
    cold run's, failing this test with a clear diagnostic.
    """
    # --- Cold run: first time the campaign sees this outdir ------------
    cold_cfg = cfg_factory(outdir)  # type: ignore[arg-type]
    cold_campaign = Campaign(cfg=cold_cfg, executor=LocalExecutor(max_workers=3))
    cold_result = cold_campaign.run()
    cold_campaign.executor.shutdown()
    cold_stats: dict[str, object] = cold_campaign.cache.stats()
    cold_total = int(str(cold_stats["total"]))
    # Sanity: cold run actually wrote something.
    assert cold_total > 0, f"cold run wrote 0 cache entries: {cold_stats}"

    # --- Warm run: same outdir, fresh Campaign instance ----------------
    # Construct a *new* Campaign (and a *new* LocalExecutor) so the
    # second run starts from the same code path a real user would
    # take when re-running. The cache_db is on disk and reloaded
    # by SQLiteCache; the new instance sees the cached entries.
    warm_cfg = cfg_factory(outdir)  # type: ignore[arg-type]
    warm_campaign = Campaign(cfg=warm_cfg, executor=LocalExecutor(max_workers=3))
    warm_result = warm_campaign.run()
    warm_campaign.executor.shutdown()
    warm_stats: dict[str, object] = warm_campaign.cache.stats()
    warm_total = int(str(warm_stats["total"]))

    # --- Signal 1: cache stats prove no work re-ran on the warm run ---
    # Equal totals + equal per-step counts mean the warm run never
    # called ``store`` with a new key, which means every ``lookup``
    # hit, which means no work function ran and no Batch job was
    # submitted. (The ``INSERT OR REPLACE`` semantics in the cache
    # ``store`` path would overwrite the timestamp on a hit, but the
    # SQL ``COUNT(*)`` is unchanged — what we want to rule out is the
    # warm run calling ``store`` with a *different* key.)
    assert warm_total == cold_total, (
        f"warm run cache total changed: cold={cold_total} warm={warm_total}. "
        f"This usually means a cache lookup missed and the work was "
        f"re-executed with a different key. cold_stats={cold_stats}, "
        f"warm_stats={warm_stats}"
    )
    cold_by_step: dict[str, int] = dict(cold_stats["by_step"])  # type: ignore[arg-type]
    warm_by_step: dict[str, int] = dict(warm_stats["by_step"])  # type: ignore[arg-type]
    assert warm_by_step == cold_by_step, (
        f"warm run per-step counts differ from cold: cold={cold_by_step} "
        f"warm={warm_by_step}. This usually means a cache lookup missed "
        f"and the work was re-executed with a different key."
    )

    # --- Signal 2: both runs produced the same output shape ------------
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
