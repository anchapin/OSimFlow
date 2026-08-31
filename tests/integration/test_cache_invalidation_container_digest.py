"""End-to-end integration test: cache invalidation when ``--container-digest`` changes.

Issue #1401 acceptance criterion:

    "integration test proves that changing ``--container-digest`` invalidates
     the cache, and that re-running with the same digest hits cache."

When the user pins the OpenStudio / Python container image by SHA-256
digest (``--container-digest sha256:<hex>``, issue #1081), the Campaign
must treat that digest as part of the cache key because the per-step
container image drives simulation results. This test exercises the
full Campaign end-to-end through ``LocalExecutor`` (stub simulation):

1. **Cold run** with ``--container-digest sha256:1111...`` writes 11
   cache entries (1 LHS + 3 APPLY + 3 SIM + 3 EXTRACT + 1 AGGREGATE).
   All four canonical artifacts are produced.
2. **Warm re-run** with the SAME digest is fully cache-served — no new
   per-step entries appear in ``cache.stats().by_step`` and the
   per-sample misses counter is zero for per-sample steps.
3. **Switch digest** to ``--container-digest sha256:2222...`` —
   every per-step entry stores under a NEW cache key because the
   ``container_digest`` column of the primary key differs. ``by_step``
   counts double and misses are non-zero.
4. **Warm re-run** with the NEW digest is again fully cache-served.
5. **Remove ``--container-digest``** (mutable tag). With docker
   unavailable the campaign falls back to ``<label>@unresolved``
   digests, which differ from both pinned forms, so every per-step
   entry is invalidated and re-stored.

Hard constraints (per issue body + ``AGENTS.md`` §6 / §10)
---------------------------------------------------------
* **No changes to ``osimflow/cache.py`` or ``CacheKey`` schema.**
  The cache-key behaviour is already correct — this test proves it.
* **No changes to ``_compute_code_hashes`` semantics.** The
  ``container_digest`` flows into the cache key via the per-step
  ``CacheKey(..., container_digest=...)`` constructor calls, not via
  ``_compute_code_hashes``.
* ``Campaign.cache`` (``SQLiteCache`` instance) and
  :py:meth:`Campaign.run` are the only public surfaces touched.
* ``OSIMFLOW_STUB_SIM=1`` is set by ``conftest.py`` so the test runs
  without a real ``openstudio.cli`` on PATH. ``skip_preflight=True``
  avoids the preflight validation against the stub
  ``example_package/model.osm``.

Determinism
-----------
All assertions are structural (``cache.stats().by_step``,
``cache.get_stats()``), never wall-clock timing. The CacheStats
hit/miss counters are recorded by ``SQLiteCache.lookup_many`` (the
batch path used in ``step_*``) and are the authoritative signal
for "did the work re-run?".

Test scheduling
---------------
Marked ``@pytest.mark.slow`` so it runs in the dedicated ``slow`` CI
job rather than the fast PR-CI lane (the test executes four stub
campaigns, ~25 seconds end-to-end). Also pinned to the
``xdist_group(name="cache_resume_solo")`` group from
:mod:`tests.integration.test_cache_resume` so it does not run
concurrently with that test (they share the campaign registry DB
path and the SQLite cache).
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from osimflow import Campaign, CampaignConfig
from osimflow.executors import LocalExecutor

pytestmark = pytest.mark.slow

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_PKG = REPO_ROOT / "example_package"

# Two distinct synthetic SHA-256 digests. Both are clearly invalid in the
# real world (no registry ever published these) but they are valid *strings*
# and are the only thing the cache key consumes, so they exercise the
# invalidation path completely. See issue #1401 / #1081.
DIGEST_A = "sha256:" + "1111111111111111111111111111111111111111111111111111111111111111"
DIGEST_B = "sha256:" + "2222222222222222222222222222222222222222222222222222222222222222"


# ---------------------------------------------------------------------------
# Fixtures (same shape as the other executor test files)
# ---------------------------------------------------------------------------


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


def _require_four_artifacts(outdir: Path) -> None:
    """Assert the four canonical per-campaign artifacts exist."""
    csv_path = outdir / "aggregated_results.csv"
    failed_path = outdir / "failed_simulations.csv"
    assert csv_path.is_file(), f"missing artifact: {csv_path}"
    csv_text = csv_path.read_text()
    assert csv_text.startswith("sample_id"), (
        f"aggregated_results.csv missing header; got: {csv_text[:200]!r}"
    )
    assert failed_path.is_file(), f"missing artifact: {failed_path}"
    kpi_dir = outdir / "work" / "kpis"
    assert kpi_dir.is_dir(), f"missing KPI directory: {kpi_dir}"
    kpi_files = list(kpi_dir.glob("kpi_*.json"))
    assert kpi_files, f"no KPI JSONs under {kpi_dir}"
    for kpi in kpi_files:
        data = json.loads(kpi.read_text())
        assert "sample_id" in data
        assert "kpis" in data
    plots_dir = outdir / "plots"
    assert plots_dir.is_dir(), f"missing plot directory: {plots_dir}"
    assert any(plots_dir.iterdir()), f"plot directory is empty: {plots_dir}"


def _build_cfg(
    workdir: Path,
    template_pkg: Path,
    outdir: Path,
    container_digest: str | None,
) -> CampaignConfig:
    """Build a CampaignConfig for the test.

    ``container_digest`` is ``None`` for the "mutable tag" baseline run
    and a literal ``"sha256:<hex>"`` string when the user pins it via
    ``--container-digest``. The Campaign's ``__init__`` overrides both
    ``self._python_container_digest`` and ``self._os_container_digest``
    to that literal value when set, so every per-step ``CacheKey``
    carries the same digest string.
    """
    kwargs: dict[str, object] = {
        "input_variables": workdir / "variables.yml",
        "template_sim_package": template_pkg,
        "n_samples": 3,
        "outdir": outdir,
        "openstudio_version": "3.11.0",
        "archive_intermediates": False,
        "skip_preflight": True,
    }
    if container_digest is not None:
        kwargs["container_digest"] = container_digest
    return CampaignConfig(**kwargs)  # type: ignore[arg-type]


def _run_campaign(
    workdir: Path,
    template_pkg: Path,
    outdir: Path,
    container_digest: str | None,
) -> Campaign:
    """Build + run a Campaign, returning the instance for stats inspection.

    The instance is returned BEFORE ``executor.shutdown()`` so the caller
    can read ``campaign.cache.stats()`` and ``campaign.cache.get_stats()``
    deterministically (issue #1389 acceptance: assert against the public
    cache surface, not log lines).
    """
    cfg = _build_cfg(workdir, template_pkg, outdir, container_digest)
    campaign = Campaign(cfg=cfg, executor=LocalExecutor(max_workers=3))
    campaign.run()
    return campaign


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.xdist_group(name="cache_resume_solo")
def test_container_digest_change_invalidates_and_redo_invalidates_cache(
    workdir: Path,
    template_pkg: Path,
    outdir: Path,
) -> None:
    """End-to-end: changing ``--container-digest`` invalidates every
    per-step cache entry; re-running with the same digest hits cache;
    removing the digest pin again invalidates (issue #1401 acceptance).

    Sequence (all five campaigns share one ``outdir`` and therefore one
    on-disk SQLite cache, just like a real user re-running with
    ``--outdir <same>`` would):

    1. ``--container-digest DIGEST_A`` (cold): 11 entries stored, 4 artifacts.
    2. ``--container-digest DIGEST_A`` (warm): 0 new entries stored.
    3. ``--container-digest DIGEST_B`` (cold): 11 new entries stored.
    4. ``--container-digest DIGEST_B`` (warm): 0 new entries stored.
    5. no ``--container_digest`` (mutable tag): 11 new entries stored.

    Each cold step raises the per-step ``stats().by_step`` count to the
    canonical 11-entry baseline; each warm step leaves it flat. ``hits``
    and ``misses`` rise accordingly. Together these prove the
    ``container_digest`` column of the primary key is doing its job.
    """
    # ---- 1. Cold run with DIGEST_A ---------------------------------------
    cold_a = _run_campaign(workdir, template_pkg, outdir, DIGEST_A)
    cold_a_total = int(str(cold_a.cache.stats()["total"]))
    assert cold_a_total == 11, (
        f"cold DIGEST_A run expected 11 cache entries "
        f"(1 LHS + 3 APPLY + 3 SIM + 3 EXTRACT + 1 AGGREGATE); got {cold_a_total}"
    )
    _require_four_artifacts(outdir)

    # ---- 2. Warm re-run with the SAME DIGEST_A --------------------------
    warm_a = _run_campaign(workdir, template_pkg, outdir, DIGEST_A)
    warm_a_total = int(str(warm_a.cache.stats()["total"]))
    warm_a_misses = warm_a.cache.get_stats().misses
    assert warm_a_total == cold_a_total, (
        f"warm DIGEST_A run must NOT add new cache entries; cold={cold_a_total} warm={warm_a_total}"
    )
    # Warm re-run against a fully-warm cache records 0 misses.
    assert warm_a_misses == 0, (
        f"warm DIGEST_A run must record 0 misses (every per-sample step "
        f"is a HIT against the same digest); got misses={warm_a_misses}"
    )

    # ---- 3. Switch digest to DIGEST_B ------------------------------------
    cold_b = _run_campaign(workdir, template_pkg, outdir, DIGEST_B)
    cold_b_total = int(str(cold_b.cache.stats()["total"]))
    cold_b_misses = cold_b.cache.get_stats().misses
    assert cold_b_total == cold_a_total * 2, (
        f"changing --container-digest from DIGEST_A to DIGEST_B must store "
        f"a fresh set of 11 entries under the new cache key (issue #1401). "
        f"Expected {cold_a_total * 2} entries, got {cold_b_total}"
    )
    assert cold_b_misses > 0, (
        f"changing --container-digest must produce per-sample misses "
        f"(every per-sample step is invalidated by the new digest); "
        f"got misses={cold_b_misses}"
    )
    # Per-step counts must have doubled for the per-sample steps and
    # the LHS / AGGREGATE singletons.
    by_step_b = dict(cold_b.cache.stats()["by_step"])  # type: ignore[arg-type]
    for step, expected_per_digest in (
        ("GENERATE_LHS_SAMPLES", 1),
        ("APPLY_PARAMETERS", 3),
        ("RUN_OPENSTUDIO_SIM", 3),
        ("EXTRACT_KPIS", 3),
        ("AGGREGATE_RESULTS", 1),
    ):
        # Two digests stored means count is 2 * per-digest count.
        assert by_step_b.get(step, 0) == expected_per_digest * 2, (
            f"by_step[{step}] after the digest switch should be "
            f"{expected_per_digest * 2} (DIGEST_A + DIGEST_B entries); "
            f"got by_step={by_step_b}"
        )

    # ---- 4. Warm re-run with the SAME DIGEST_B --------------------------
    warm_b = _run_campaign(workdir, template_pkg, outdir, DIGEST_B)
    warm_b_total = int(str(warm_b.cache.stats()["total"]))
    warm_b_misses = warm_b.cache.get_stats().misses
    assert warm_b_total == cold_b_total, (
        f"warm DIGEST_B run must NOT add new entries; cold={cold_b_total} warm={warm_b_total}"
    )
    assert warm_b_misses == 0, f"warm DIGEST_B run must record 0 misses; got misses={warm_b_misses}"

    # ---- 5. Remove --container-digest entirely --------------------------
    cold_none = _run_campaign(workdir, template_pkg, outdir, None)
    cold_none_total = int(str(cold_none.cache.stats()["total"]))
    cold_none_misses = cold_none.cache.get_stats().misses
    assert cold_none_total == cold_b_total + 11, (
        f"removing --container-digest must store a fresh 11 entries "
        f"(mutable tag digests differ from both pinned digests); "
        f"expected {cold_b_total + 11}, got {cold_none_total}"
    )
    assert cold_none_misses > 0, (
        f"removing --container-digest must produce per-sample misses; got misses={cold_none_misses}"
    )
    _require_four_artifacts(outdir)

    # ---- Sanity: the three digests really produced three distinct cache keys
    # by directly inspecting the underlying SQLite table. The aggregation
    # above proves behaviour; this proves that the diverging "total" count
    # is the *container_digest* column doing the invalidation rather than
    # some other key drift (e.g. openstudio_version, byos hashes, code
    # hashes).
    import sqlite3

    con = sqlite3.connect(str(outdir / "work" / "cache.sqlite"))
    con.row_factory = sqlite3.Row
    rows = con.execute("SELECT DISTINCT container_digest FROM cache_entries").fetchall()
    digests_seen = {r["container_digest"] for r in rows}
    con.close()
    assert DIGEST_A in digests_seen, (
        f"DIGEST_A missing from cache_entries.container_digest; got {digests_seen!r}"
    )
    assert DIGEST_B in digests_seen, (
        f"DIGEST_B missing from cache_entries.container_digest; got {digests_seen!r}"
    )
    assert any(d != DIGEST_A and d != DIGEST_B for d in digests_seen), (
        f"mutable-tag digest (<label>@unresolved) missing from "
        f"cache_entries.container_digest; got {digests_seen!r}"
    )


@pytest.mark.xdist_group(name="cache_resume_solo")
def test_container_digest_pin_landed_in_cache_key_row(tmp_path: Path) -> None:
    """Unit-style proof: building a ``CacheKey`` under two distinct
    ``--container-digest`` values with every other column equal MUST
    yield two distinct keys, and writing one must not surface the
    other.

    This is the structural counterpart to the end-to-end test above:
    if a future refactor accidentally folds ``container_digest`` into
    ``code_sha256`` (or drops the column), the SQL primary key would
    collapse to <8 columns and this test would FAIL with a clear
    inequality rather than the end-to-end test silently producing a
    3× doubled cache for unrelated reasons.
    """
    from osimflow.cache import CacheKey, SQLiteCache

    cache_path = tmp_path / "cache.sqlite"
    with SQLiteCache(cache_path) as cache:
        output_a = tmp_path / "out_a"
        output_a.write_text("digest A result")
        output_b = tmp_path / "out_b"
        output_b.write_text("digest B result")

        key_a = CacheKey(
            step="RUN_OPENSTUDIO_SIM",
            sample_id="0001",
            openstudio_version="3.11.0",
            inputs_sha256="i",
            code_sha256="c",
            container_digest=DIGEST_A,
        )
        key_b = CacheKey(
            step="RUN_OPENSTUDIO_SIM",
            sample_id="0001",
            openstudio_version="3.11.0",
            inputs_sha256="i",
            code_sha256="c",
            container_digest=DIGEST_B,
        )

        assert key_a != key_b, (
            "CacheKey keys with identical columns except container_digest "
            "must be unequal (issue #1401). If equal, the container_digest "
            "column is not being used in the primary key."
        )

        cache.store(key_a, output_a, exit_code=0)
        assert cache.lookup(key_a) == output_a
        # Storing under DIGEST_A must not surface DIGEST_B (the other
        # row does not exist yet).
        assert cache.lookup(key_b) is None, (
            "CacheLookup under a different container_digest must MISS even "
            "if the other columns match (issue #1401)."
        )
        cache.store(key_b, output_b, exit_code=0)
        # Cross-check still holds after both inserts: each side sees
        # only its own output.
        assert cache.lookup(key_a) == output_a
        assert cache.lookup(key_b) == output_b
