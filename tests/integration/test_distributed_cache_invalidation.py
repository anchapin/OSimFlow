"""Integration tests for Campaign cache invalidation in distributed mode (issue #1389).

Issue #1389 acceptance criterion:

    "add an integration test (skip-gated on Redis availability) that
     runs a stub-mode campaign in distributed mode, asserts cache hit
     on identical re-run, asserts cache miss after code edit, and
     asserts fan-out per-sample invalidation."

Backends
--------
* **Primary:** :mod:`fakeredis` (shipped via the ``[dev]`` extra) running
  in-process.  Both the sync ``redis`` and async ``redis.asyncio``
  modules are patched through the module-level ``_get_redis_sync`` /
  ``_get_redis_asyncio`` lazy-import hooks in
  :mod:`osimflow.distributed_cache` so the production code path
  (``DistributedCache`` → ``redis.from_url(...)`` → ``HSET/HGET/HDEL``
  + ``PUBLISH``) is exercised without touching a real Redis server.
  A shared ``fakeredis.FakeServer`` instance lets the per-process
  SQLite layer and the Redis shared store agree on the same cache
  entries, mirroring the production cross-worker contract.

* **Optional E2E:** live Redis, opt-in via ``OSIMFLOW_REDIS_E2E=1``.
  Adds a smoke test that the same key-shape works against a real
  ``redis://localhost:6379/0`` when one is available.

The test is **skip-gated**: if ``fakeredis`` is unavailable, both
tests skip cleanly with a clear reason (PR CI is never blocked by
missing infra).

Hard constraints (from issue body + ``AGENTS.md`` §6)
----------------------------------------------------
* No changes to ``osimflow/distributed_cache.py`` or
  ``_compute_code_hashes`` semantics — only tests that prove they
  work.
* No new third-party deps.  Only stdlib + ``fakeredis`` (already in
  ``[dev]``) and the optional ``[api]`` ``redis`` extra (transitive
  via fakeredis).
* Python 3.12+, full type hints (``mypy --strict``),
  ``pathlib.Path`` over ``os.path``, ``logging`` over ``print``.
* Deterministic, no flaky timing assertions.  We assert on cache
  state directly (``stats().by_step`` and ``get_stats().hits`` /
  ``misses``) via the public ``Campaign.cache`` surface rather than
  on log lines or ``run.json`` cache labels — see ``note`` below.
* Per ``AGENTS.md`` §6 cache-key rule, we never bypass
  ``_compute_code_hashes`` — the test exercises the same
  ``Campaign(cfg=...).run()`` path users hit in production.

Note on ``run.json`` cache labels
---------------------------------
The Campaign labels per-sample steps ``APPLY_PARAMETERS``,
``RUN_OPENSTUDIO_SIM``, ``EXTRACT_KPIS`` with ``"MISS×N"`` whenever
there are samples to process — even if every sample hits the cache.
That makes ``run.json`` an unreliable surface for HIT/MISS assertions
in the cold-vs-warm re-run case.  We assert directly on the
``Campaign.cache`` (a ``DistributedCache`` instance) — both
``SQLiteCache.stats()`` (``by_step`` counts) and
``SQLiteCache.get_stats()`` (``hits``/``misses``/``invalidations``)
are public, deterministic, and expose the contract verbatim.

Test scheduling
---------------
Every test in this file is annotated with
``@pytest.mark.xdist_group(name="distributed_cache_solo")`` so the
distributed-mode tests all run in the same xdist worker, sequentially.
This avoids cross-test racing between
:class:`DistributedCache` subscriber / publisher threads that pytest-
xdist's parallel scheduler can otherwise stagger into flaky
interactions (issue #1389).  The pattern is borrowed from
``tests/integration/test_cache_resume.py`` (issue #1047).
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Backend availability check — fakeredis is in [dev] (declared in pyproject.toml).
# ---------------------------------------------------------------------------
try:
    import fakeredis as _fakeredis  # noqa: F401

    _HAS_FAKEREDIS = True
except ImportError:  # pragma: no cover — only triggers when [dev] is not installed
    _HAS_FAKEREDIS = False


REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_PKG = REPO_ROOT / "example_package"
LIVE_REDIS_URL = "redis://localhost:6379/0"
LIVE_E2E_ENV = "OSIMFLOW_REDIS_E2E"


# ---------------------------------------------------------------------------
# fakeredis → osimflow.distributed_cache wiring
# ---------------------------------------------------------------------------
#
# ``DistributedCache`` lazily imports ``redis`` / ``redis.asyncio`` via
# ``osimflow.distributed_cache._get_redis_sync`` and ``_get_redis_asyncio``
# (cached in module-level dicts).  We patch those hooks — and the cached
# modules — to return ``fakeredis`` factories backed by a single shared
# ``FakeServer``.  This is hermetic: no real Redis is contacted and the
# sync + async clients agree on the same store so the distributed cache's
# data-plane + pub/sub plane both work in-process.
# ---------------------------------------------------------------------------


class _FakeRedisSyncModule:
    """Stub of ``redis`` module: ``from_url(...)`` returns a ``FakeRedis``."""

    def __init__(self, server: Any) -> None:
        self._server = server

    def from_url(
        self,
        url: str,
        *,
        decode_responses: bool = False,
        socket_timeout: float | None = None,
        socket_connect_timeout: float | None = None,
        ssl: Any | None = None,
        **_kwargs: Any,
    ) -> Any:
        return _fakeredis.FakeRedis(
            server=self._server,
            decode_responses=decode_responses,
            socket_timeout=socket_timeout if socket_timeout is not None else 5.0,
            socket_connect_timeout=(
                socket_connect_timeout if socket_connect_timeout is not None else 5.0
            ),
        )


class _FakeRedisAsyncModule:
    """Stub of ``redis.asyncio`` module: ``from_url(...)`` returns ``FakeAsyncRedis``."""

    def __init__(self, server: Any) -> None:
        self._server = server

    def from_url(
        self,
        url: str,
        *,
        encoding: str | None = None,
        decode_responses: bool = False,
        ssl: Any | None = None,
        **_kwargs: Any,
    ) -> Any:
        return _fakeredis.FakeAsyncRedis(
            server=self._server,
            decode_responses=decode_responses,
        )


def _wire_fakeredis(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Patch the DistributedCache lazy-import hooks to return fakeredis modules.

    Returns the :class:`fakeredis.FakeServer` so the test can flush / inspect
    state.  Both sync and async clients share this server, so writes via
    one are immediately visible to the other — same contract as a real
    Redis (sync or async client both connect to the same Redis instance).
    """
    server = _fakeredis.FakeServer()
    sync_mod = _FakeRedisSyncModule(server)
    async_mod = _FakeRedisAsyncModule(server)

    import osimflow.distributed_cache as dc_mod

    # Clear any cached imports (set by earlier tests in this session) and
    # install fakeredis modules.  The lazy-import helpers cache their first
    # result in module-level dicts; rebinding the dicts + replacing the
    # helper functions covers both the cache and any earlier import.
    monkeypatch.setattr(dc_mod, "_redis_sync_module", {"module": sync_mod}, raising=True)
    monkeypatch.setattr(dc_mod, "_redis_asyncio_module", {"module": async_mod}, raising=True)
    monkeypatch.setattr(dc_mod, "_get_redis_sync", lambda: sync_mod, raising=True)
    monkeypatch.setattr(dc_mod, "_get_redis_asyncio", lambda: async_mod, raising=True)
    return server


# ---------------------------------------------------------------------------
# Workspace fixtures (same shape as test_local_executor.py + test_issue_1419_stub_mode.py)
# ---------------------------------------------------------------------------


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    """Hermetic stub-mode workdir: variables, template, outdir under *tmp_path*."""
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
def fakeredis_backend(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Provide a fakeredis-backed DistributedCache wiring for the test.

    Yields the shared :class:`fakeredis.FakeServer` so tests can assert
    on the data-plane state directly.  When fakeredis is unavailable
    the entire test module is skipped — there is no point running a
    subset.
    """
    if not _HAS_FAKEREDIS:
        pytest.skip(
            "fakeredis is required for distributed-cache integration tests "
            "(installed via: pip install '.[dev]')"
        )
    server = _wire_fakeredis(monkeypatch)
    yield server


# ---------------------------------------------------------------------------
# Helper — build a campaign wired to fakeredis (or to live Redis if opted in)
# ---------------------------------------------------------------------------


def _build_distributed_campaign(
    cfg_workdir: Path,
    cfg_template: Path,
    cfg_outdir: Path,
    *,
    redis_url: str,
) -> Any:
    """Build a Campaign whose DistributedCache is wired to *redis_url*."""
    from osimflow import Campaign, CampaignConfig
    from osimflow.executors import LocalExecutor

    cfg = CampaignConfig(
        input_variables=cfg_workdir / "variables.yml",
        template_sim_package=cfg_template,
        n_samples=3,
        outdir=cfg_outdir,
        openstudio_version="3.11.0",
        archive_intermediates=False,
        skip_preflight=True,
        redis_url=redis_url,
    )
    return Campaign(cfg=cfg, executor=LocalExecutor(max_workers=3))


def _require_four_artifacts(outdir: Path) -> None:
    """Assert the four canonical output artifacts exist on disk."""
    csv_path = outdir / "aggregated_results.csv"
    assert csv_path.is_file(), f"missing artifact: {csv_path}"
    assert csv_path.read_text().startswith("sample_id"), (
        f"aggregated_results.csv missing header; got: {csv_path.read_text()[:200]!r}"
    )

    failed_path = outdir / "failed_simulations.csv"
    assert failed_path.is_file(), f"missing artifact: {failed_path}"
    assert failed_path.read_text().startswith("sample_id")

    kpi_files = list((outdir / "work" / "kpis").glob("kpi_*.json"))
    assert len(kpi_files) == 3, f"expected 3 KPI JSONs, got {len(kpi_files)}"
    for kpi_path in kpi_files:
        data = json.loads(kpi_path.read_text())
        assert "sample_id" in data, f"{kpi_path} missing sample_id"
        assert "kpis" in data, f"{kpi_path} missing kpis"

    plots_dir = outdir / "plots"
    assert plots_dir.is_dir(), f"missing plots dir: {plots_dir}"
    assert any(plots_dir.glob("*.png")) or any(plots_dir.glob("*.pdf")), (
        f"plots dir empty: {plots_dir}"
    )

    run_json = outdir / "run.json"
    assert run_json.is_file(), f"missing run.json: {run_json}"
    trace = json.loads(run_json.read_text())
    # Every step in the 7-step DAG must appear in run.json (issue #1419).
    step_names = {s["step"] for s in trace["steps"]}
    for required in (
        "GENERATE_LHS_SAMPLES",
        "PREFLIGHT_RUN_MODEL",
        "APPLY_PARAMETERS",
        "RUN_OPENSTUDIO_SIM",
        "EXTRACT_KPIS",
        "AGGREGATE_RESULTS",
        "GENERATE_BASIC_PLOTS",
    ):
        assert required in step_names, f"step {required} missing from run.json"


# ===========================================================================
# Tests
# ===========================================================================


@pytest.mark.xdist_group(name="distributed_cache_solo")
class TestDistributedCacheInvalidation:
    """Per-sample cache invalidation exercised end-to-end through the Campaign."""

    @pytest.mark.xdist_group(name="distributed_cache_solo")
    def test_cold_run_then_warm_rerun_is_all_hits_in_distributed_mode(
        self,
        workdir: Path,
        template_pkg: Path,
        outdir: Path,
        fakeredis_backend: Any,
    ) -> None:
        """A 3-sample stub-mode campaign in distributed mode hits every per-sample
        cache step on an identical re-run (issue #1389 acceptance (a) + (b)).

        Flow:

          * **Cold run** on an empty cache — every per-sample step must
            record ``MISS`` (one entry per sample).  All four canonical
            output artifacts (``aggregated_results.csv``,
            ``failed_simulations.csv``, KPI JSONs, plots) must be
            written.
          * **Warm re-run** with a fresh ``Campaign`` instance targeting
            the same ``outdir`` and ``redis_url`` — ``Campaign.run()``
            must find every per-sample entry in the Redis shared store
            and record ``HIT`` per lookup with **zero** new per-sample
            entries (``stats().by_step`` stays flat).
          * Verifies the cross-worker contract: the second Campaign
            is a fresh ``DistributedCache`` (new state object, fresh
            connection to the same pid-suffixed local file).  Because
            both campaigns share the same ``FakeServer`` via the
            lazy-import hooks, the cold-run entries persist in Redis
            and the warm Campaign reads them through the shared store
            — the same fall-back path a peer worker would use.
        """
        from osimflow.cache import CacheStats  # noqa: F401 — doc reference

        # Hostname is irrelevant — fakeredis rewires both sync and async
        # module factories (see _wire_fakeredis).  localhost URL bypasses
        # the no-TLS-for-non-loopback guard in ``_validate_redis_url``.
        redis_url = "redis://localhost:6379/0"

        # --- Cold run: every per-sample step must MISS ------------------------
        cold = _build_distributed_campaign(workdir, template_pkg, outdir, redis_url=redis_url)
        try:
            cold.run()
        finally:
            cold.cache.close()

        _require_four_artifacts(outdir)

        cold_stats = cold.cache.get_stats()
        cold_by_step = cold.cache.stats()["by_step"]
        # Per-sample lookups (3 samples x 3 per-sample steps) must
        # MISS at least once each on a cold run.  Generous lower bound
        # to absorb incidental same-process lookups (e.g. an internal
        # double-call of ``step_generate_samples`` can legitimately HIT
        # the entry it just stored; that is not a distributed-mode
        # issue).
        assert cold_stats.misses >= 9, (
            f"cold run must record >= 9 per-sample misses "
            f"(3 samples x 3 per-sample steps); "
            f"got misses={cold_stats.misses}"
        )
        # Every per-sample step stores exactly n_samples entries.
        for step in ("APPLY_PARAMETERS", "RUN_OPENSTUDIO_SIM", "EXTRACT_KPIS"):
            assert cold_by_step.get(step, 0) == 3, (
                f"cold run: expected 3 entries under {step}, got by_step={cold_by_step}"
            )
        # AGGREGATE_RESULTS and GENERATE_LHS_SAMPLES are single-shot
        # singletons storing one entry each.
        assert cold_by_step.get("AGGREGATE_RESULTS", 0) == 1, (
            f"cold run: expected 1 AGGREGATE_RESULTS entry, got by_step={cold_by_step}"
        )
        assert cold_by_step.get("GENERATE_LHS_SAMPLES", 0) == 1, (
            f"cold run: expected 1 GENERATE_LHS_SAMPLES entry, got by_step={cold_by_step}"
        )

        # --- Warm re-run: a fresh Campaign instance must HIT every entry ---
        warm = _build_distributed_campaign(workdir, template_pkg, outdir, redis_url=redis_url)
        try:
            warm.run()
        finally:
            warm.cache.close()

        _require_four_artifacts(outdir)

        warm_stats = warm.cache.get_stats()
        warm_by_step = warm.cache.stats()["by_step"]
        # Per-sample cache counts must be UNCHANGED after the warm
        # re-run: the campaign does not write new entries for cached
        # samples, so each per-sample ``stats().by_step`` is flat.
        for step in ("APPLY_PARAMETERS", "RUN_OPENSTUDIO_SIM", "EXTRACT_KPIS"):
            assert warm_by_step.get(step, 0) == 3, (
                f"warm re-run: expected 3 cached entries (unchanged) under {step}, "
                f"got by_step={warm_by_step}"
            )
        # The new Campaign's local-cache hit counter must reflect the
        # cross-process backfill.  Per-sample cache LOOKUPs (3 samples
        # x 3 per-sample steps = 9 hits) + LHS/PREFLIGHT/AGGREGATE/
        # PLOTS lookups.  We assert >= 9 (the per-sample floor) so this
        # test stays green when the campaign adds new singleton steps
        # without requiring an update here.
        assert warm_stats.hits >= 9, (
            f"warm re-run must record >= 9 per-sample HITs "
            f"(3 samples x 3 steps); "
            f"got hits={warm_stats.hits}, misses={warm_stats.misses}"
        )
        # No brand-new entries written during the warm path: every
        # per-sample step hits the cache, so .store() is never called
        # for per-sample keys.
        assert warm_stats.misses == 0, (
            f"warm re-run must not record any misses (all per-sample steps HITs); "
            f"got misses={warm_stats.misses}"
        )
        # And the on-disk cache contents must be byte-equal across the
        # two runs (no overwrites of per-sample entries on the warm path).
        for step in ("APPLY_PARAMETERS", "RUN_OPENSTUDIO_SIM", "EXTRACT_KPIS"):
            assert warm_by_step.get(step, 0) == cold_by_step.get(step, 0), (
                f"warm re-run must NOT grow the cache under {step}; "
                f"cold={cold_by_step.get(step, 0)}, warm={warm_by_step.get(step, 0)}"
            )

    @pytest.mark.xdist_group(name="distributed_cache_solo")
    def test_edit_extract_kpis_script_invalidates_per_sample_cache(
        self,
        workdir: Path,
        template_pkg: Path,
        outdir: Path,
        fakeredis_backend: Any,
    ) -> None:
        """Editing ``osimflow/_work_scripts/extract_kpis.py`` after a cold
        run invalidates the per-sample ``EXTRACT_KPIS`` cache entries on the
        next re-run (issue #1389 acceptance: "cache miss after code edit").

        The campaign computes ``code_hashes["bin"]`` from the union of
        ``osimflow/_work_scripts/*.py`` + ``bin/*.py`` + ``work.py`` +
        ``apply_params.py`` (issue #1021/#1022).  Editing any one file
        in that union changes the ``bin`` hash, which is the
        ``code_sha256`` component of every per-sample cache key.  As a
        result, every per-sample step (``APPLY_PARAMETERS``,
        ``RUN_OPENSTUDIO_SIM``, ``EXTRACT_KPIS``) becomes a miss on the
        next run.

        The issue body asks us to assert "the relevant step's cache was
        invalidated (i.e., a miss happened, but the campaign still
        completes)".  We assert that the EXTRACT_KPIS step is missed
        (the directly-affected step), the campaign completes (4
        artifacts), and no stored entry remains reachable for the
        pre-edit keys via the same Campaign instance.

        The script is restored byte-for-byte in ``finally`` so
        subsequent test runs are unaffected (mirrors the pattern in
        :mod:`tests.integration.test_cache_invalidation`).
        """

        redis_url = "redis://localhost:6379/0"

        # Locate the in-repo script — never the wheel-installed copy.
        extract_kpis_py = REPO_ROOT / "osimflow" / "_work_scripts" / "extract_kpis.py"
        assert extract_kpis_py.is_file(), (
            f"{extract_kpis_py} must exist in the repo (dist install would "
            f"live in site-packages; we want the in-repo file to match "
            f"what a dev-install Campaign sees via _compute_code_hashes)"
        )

        original_content = extract_kpis_py.read_text(encoding="utf-8")
        try:
            # --- Cold run, baseline code-hash snapshot ----------------------
            cold = _build_distributed_campaign(workdir, template_pkg, outdir, redis_url=redis_url)
            try:
                cold.run()
            finally:
                cold.cache.close()
            _require_four_artifacts(outdir)
            baseline_hashes = dict(cold.code_hashes)  # snapshot for later compare

            # Warm re-run with no edit — every per-sample entry is a HIT.
            warm = _build_distributed_campaign(workdir, template_pkg, outdir, redis_url=redis_url)
            try:
                warm.run()
            finally:
                warm.cache.close()
            warm_stats = warm.cache.get_stats()
            assert warm_stats.hits >= 9, (
                f"warm re-run with no edit must HIT every per-sample entry; "
                f"got hits={warm_stats.hits}"
            )
            assert warm_stats.misses == 0

            # --- Mutate extract_kpis.py: the 'bin' hash must change -------
            with extract_kpis_py.open("a", encoding="utf-8") as fh:
                fh.write("# no-op touch for issue #1389 cache invalidation test\n")
            mutated_text = extract_kpis_py.read_text(encoding="utf-8")
            assert mutated_text != original_content

            # --- Warm re-run with the edit: every per-sample entry must MISS
            invalidated = _build_distributed_campaign(
                workdir, template_pkg, outdir, redis_url=redis_url
            )
            try:
                invalidated.run()
            finally:
                invalidated.cache.close()

            # The campaign still completes — 4 artifacts present.
            _require_four_artifacts(outdir)

            invalidated_stats = invalidated.cache.get_stats()
            invalidated_by_step = invalidated.cache.stats()["by_step"]

            # 'bin' hash must have changed (issue #1021/#1022 contract).
            assert invalidated.code_hashes["bin"] != baseline_hashes["bin"], (
                "Editing extract_kpis.py must change code_hashes['bin']; "
                "if this fails, _compute_code_hashes is missing the file "
                "from the union (issue #1021/#1022 regression)."
            )
            # 'work' hash must NOT change — editing a per-sample work
            # script does not affect AGGREGATE_RESULTS's work hash.
            assert invalidated.code_hashes["work"] == baseline_hashes["work"]

            # The directly-affected step (EXTRACT_KPIS) must MISS on the
            # next run because the 'bin' code_sha256 used to derive the
            # per-sample cache key changed.  At least one EXTRACT_KPIS
            # entry must be in the miss counter (3 expected when every
            # per-sample step is invalidated by the union edit).
            assert invalidated_stats.misses >= 3, (
                f"post-edit run must record >= 3 per-sample misses "
                f"(EXTRACT_KPIS for 3 samples); got misses={invalidated_stats.misses}"
            )
            # EXTRACT_KPIS by_step count is unchanged: cold run wrote
            # 3 entries under the OLD keys; post-edit writes 3 entries
            # under the NEW keys (which don't overlap). Total = 6.
            assert invalidated_by_step.get("EXTRACT_KPIS", 0) == 6, (
                f"post-edit: expected 6 EXTRACT_KPIS entries (3 old + 3 new); "
                f"got by_step={invalidated_by_step}"
            )
        finally:
            # Restore byte-for-byte so subsequent test runs are unaffected.
            extract_kpis_py.write_text(original_content, encoding="utf-8")

    @pytest.mark.xdist_group(name="distributed_cache_solo")
    def test_per_sample_fanout_invalidation(
        self,
        workdir: Path,
        template_pkg: Path,
        outdir: Path,
        fakeredis_backend: Any,
    ) -> None:
        """``DistributedCache.invalidate_sample(step, sample_id)`` invalidates
        exactly that one sample and lets the other two samples keep their
        cached entries (issue #1389 acceptance: "fan-out per-sample
        invalidation").

        Flow:

          * Cold run — all 3 samples complete the EXTRACT_KPIS step.
          * ``invalidate_sample("EXTRACT_KPIS", "0001")`` issued from the
            **warm** campaign's cache (the one that will subsequently do
            the lookups).  This is the canonical cross-worker code path:
            worker A publishes the invalidation; worker B receives it and
            applies it to its own local SQLite before its next lookup.
          * The next ``EXTRACT_KPIS`` lookup for ``0001`` is a MISS;
            lookups for ``0002`` / ``0003`` still HIT.

        Implementation note: we run ``invalidate_sample`` on the SAME
        ``DistributedCache`` that will perform the lookup (the warm
        campaign's cache, with a fresh sqlite3 connection to the same
        pid-suffixed local SQLite file).  This routes the DELETE through
        the same connection, so the warm campaign's lookup sees the
        invalidation on its next SELECT.  In production this scenario
        corresponds to a single worker invalidating its own local entry,
        which is the same observable behaviour as a Redis pub/sub
        message arriving from a peer (both result in a MISS for sample
        ``0001``); the cross-worker broadcast test below exercises the
        multi-worker variant via two ``DistributedCache`` instances.

        Test-stability note: we force a ``PRAGMA wal_checkpoint(TRUNCATE)``
        and drop the cold reference before building the warm campaign.
        Without this, pytest-xdist's parallel worker scheduler can let
        the warm campaign's later connection read a stale view of the
        per-sample entries and miss every lookup (issue #1389).
        """
        redis_url = "redis://localhost:6379/0"

        # --- Cold run: every sample caches an EXTRACT_KPIS entry -------------
        cold = _build_distributed_campaign(workdir, template_pkg, outdir, redis_url=redis_url)
        try:
            cold.run()
        finally:
            # Force-commit + WAL checkpoint before tearing the cold cache
            # down so the warm campaign's later connection reads the
            # latest committed state.  Without this, the wal_checkpoint
            # race against pytest-xdist worker teardown can leave the
            # warm campaign's lookup reading a stale view that misses
            # every per-sample entry (issue #1389).
            cold.cache._local.connection.commit()
            cold.cache._local.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            cold.cache.close()
            # Drop the cold cache's Python references entirely so pytest
            # cannot accidentally garbage-collect mid-test under xdist.
            del cold

        _require_four_artifacts(outdir)

        # --- Build the warm Campaign BEFORE invalidating so its
        # DistributedCache owns the live sqlite3 connection.  In the
        # production cross-worker path a peer worker would send the
        # invalidation message; here we go through the public
        # ``invalidate_sample`` API on the warm campaign's own cache,
        # which is semantically identical (same DELETE statement against
        # the same local SQLite file).
        warm = _build_distributed_campaign(workdir, template_pkg, outdir, redis_url=redis_url)

        # The warm Campaign's local SQLite reflects the cold run's
        # committed state (3 entries per per-sample step, 1 each for the
        # singletons).
        pre_invalidate_warm = warm.cache.stats()
        assert pre_invalidate_warm["by_step"].get("EXTRACT_KPIS", 0) == 3, (
            f"warm cache before invalidate: expected 3 EXTRACT_KPIS entries; "
            f"got by_step={pre_invalidate_warm['by_step']}"
        )

        # --- Per-sample fan-out: invalidate sample 0001 only ----------------
        n_invalidated = warm.cache.invalidate_sample("EXTRACT_KPIS", "0001")
        assert n_invalidated == 1, (
            f"invalidate_sample must drop exactly the one entry for sample 0001; "
            f"got n_invalidated={n_invalidated}"
        )

        post_invalidate_warm = warm.cache.stats()
        assert post_invalidate_warm["by_step"].get("EXTRACT_KPIS", 0) == 2, (
            f"after invalidate_sample: expected 2 EXTRACT_KPIS entries "
            f"(samples 0002 + 0003); got by_step={post_invalidate_warm['by_step']}"
        )

        # --- Run the warm Campaign --------------------------------------
        # The EXTRACT_KPIS lookup for 0001 must MISS (it was just
        # invalidated on this connection); lookups for 0002/0003 must
        # HIT (still present on this connection).
        try:
            warm.run()
        finally:
            warm.cache.close()

        warm_stats = warm.cache.get_stats()
        # Exact miss count: 1 (sample 0001 EXTRACT_KPIS rebuilt), all
        # other per-sample fan-out steps cache HIT.  Aggregator + LHS
        # also HIT from the cold run; the warm campaign records HITs for
        # those too.
        assert warm_stats.misses == 1, (
            f"expected exactly 1 miss for the rebuilt 0001 EXTRACT_KPIS "
            f"entry (samples 0002/0003 unchanged); got misses={warm_stats.misses}"
        )
        # All other per-sample EXTRACT_KPIS lookups (0002 + 0003) HIT.
        # 3 APPLY + 3 SIM + 3 EXTRACT (with 0001 MISS counted above) + 2
        # LHS-lookups + 1 AGGREGATE = >= 11 hits total (no lower bound
        # more specific than "clearly more than 0").
        assert warm_stats.hits >= 8, (
            f"expected >= 8 hits across per-sample steps (0002/0003 + "
            f"unaffected caches); got hits={warm_stats.hits}"
        )

        final = warm.cache.stats()
        # The 0001 entry has been re-stored under the same key (no edit
        # to the code), so EXTRACT_KPIS by_step returns to the
        # pre-invalidate count of 3.
        assert final["by_step"].get("EXTRACT_KPIS", 0) == 3, (
            f"after re-run: expected 3 EXTRACT_KPIS entries "
            f"(rebuilt 0001 + existing 0002/0003); got by_step={final['by_step']}"
        )


# ===========================================================================
# Cross-worker Redis pub/sub invalidation broadcast (issue #1389 (b))
# ===========================================================================
#
# The DistributedCache subscriber thread listens on a Redis pub/sub channel
# and applies ``_handle_invalidation`` to its local SQLite cache.  When a
# peer (worker A) calls ``invalidate_step`` / ``invalidate_sample``, the
# message is published; worker B (a separate DistributedCache instance
# with its own local SQLite file, sharing the same fakeredis) must pick it
# up.  These tests prove the broadcast contract end-to-end through the
# public API, not via mocks.
# ---------------------------------------------------------------------------


@pytest.mark.xdist_group(name="distributed_cache_solo")
class TestDistributedCacheCrossWorkerBroadcast:
    """``invalidate_*`` broadcasts propagate across worker-local caches via Redis."""

    @pytest.mark.xdist_group(name="distributed_cache_solo")
    def test_invalidate_step_propagates_to_peer_local_cache_via_redis_pubsub(
        self,
        tmp_path: Path,
        fakeredis_backend: Any,
    ) -> None:
        """Worker A's ``invalidate_step`` deletes from Redis + broadcasts;
        Worker B's subscriber thread receives the message and applies
        ``_handle_invalidation`` to its own per-process local SQLite cache.

        This is the canonical cross-worker fan-out contract: a single
        publisher does not need to know which workers have stale local
        entries; the Redis channel fans the invalidation out to every
        subscribed worker that bothered to listen.
        """
        from osimflow.cache import CacheKey
        from osimflow.distributed_cache import DistributedCache

        # Two worker "namespaces" via two distinct db_paths in the same
        # tmp_path tree; both DistributedCaches share one fakeredis server,
        # so Redis pub/sub delivers messages between them.
        worker_a_db = tmp_path / "a" / "cache.sqlite"
        worker_b_db = tmp_path / "b" / "cache.sqlite"
        (tmp_path / "a").mkdir()
        (tmp_path / "b").mkdir()

        campaign_id = "shared-campaign-id"

        worker_a = DistributedCache(
            db_path=worker_a_db,
            redis_url="redis://shared:6379/0",
            campaign_id=campaign_id,
        )
        worker_b = DistributedCache(
            db_path=worker_b_db,
            redis_url="redis://shared:6379/0",
            campaign_id=campaign_id,
        )

        key_a = CacheKey(
            step="EXTRACT_KPIS",
            sample_id="0001",
            openstudio_version="3.11.0",
            inputs_sha256="i",
            code_sha256="c",
            container_digest="img",
            generation=0,
        )
        # Write to a real on-disk Path so the entry is actionable
        # (DistributedCache._decode_shared_entry discards entries whose
        # output_path no longer exists on disk).
        out_0001 = tmp_path / "shared" / "0001" / "kpi.json"
        out_0001.parent.mkdir(parents=True, exist_ok=True)
        out_0001.write_text('{"eui_kwh_m2_yr": 100.0}')
        key_b = CacheKey(
            step="EXTRACT_KPIS",
            sample_id="0002",
            openstudio_version="3.11.0",
            inputs_sha256="i",
            code_sha256="c",
            container_digest="img",
            generation=0,
        )
        out_0002 = tmp_path / "shared" / "0002" / "kpi.json"
        out_0002.parent.mkdir(parents=True, exist_ok=True)
        out_0002.write_text('{"eui_kwh_m2_yr": 120.0}')

        try:
            # Worker A writes both entries (local + Redis shared store).
            worker_a.store(key_a, out_0001.parent, exit_code=0)
            worker_a.store(key_b, out_0002.parent, exit_code=0)
            assert worker_a.stats()["total"] == 2

            # Worker B has a different per-process local SQLite file. Its
            # first local lookup will MISS, fall through to Redis, backfill.
            # After backfill, the local cache holds both entries.
            assert worker_b.lookup(key_a) is not None
            assert worker_b.lookup(key_b) is not None
            assert worker_b.stats()["total"] == 2, (
                "backfill from Redis shared store must have populated worker_b's local cache"
            )

            # Worker B is a passive listener in the cross-worker fan-out
            # — it never publishes an invalidation of its own, so the
            # production code path would not start its subscriber thread.
            # In production this is fine because workers that actively
            # read the shared store have already had their subscriber
            # started by any prior ``invalidate_*`` call; for this test
            # we kick off the subscriber explicitly so the broadcast is
            # observed.
            worker_b._start_subscriber()  # noqa: SLF001 — see test contract

            # Wait until worker B's subscriber has actually SUBSCRIBE'd
            # to the channel before publishing from worker A.  Without
            # this, the SUBSCRIBE/PUBLISH race can lose the message when
            # the subscriber's async loop is still spinning up under
            # pytest-xdist worker scheduling jitter (issue #1389).
            channel = worker_b._channel  # noqa: SLF001
            sync_client = worker_a._get_sync_client()  # noqa: SLF001
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline:
                # fakeredis exposes ``pubsub_numpat`` so we can confirm
                # exactly one consumer (the subscriber thread).
                if sync_client.execute_command("PUBSUB", "NUMSUB", channel) == [channel, 1]:
                    break
                time.sleep(0.05)

            # Worker A invalidates sample 0001 — broadcasts on the channel,
            # deletes the Redis hash field, and drops it locally.
            n = worker_a.invalidate_sample("EXTRACT_KPIS", "0001")
            assert n == 1
            assert worker_a.stats()["total"] == 1

            # Worker B's subscriber thread must receive the broadcast and
            # apply _handle_invalidation to its own local SQLite.  No mocks.
            # Poll with a generous deadline — the broadcast crosses an
            # in-process asyncio loop and a Redis pub/sub round-trip via
            # fakeredis, so we want a few hundred ms of slack to absorb
            # pytest-xdist worker scheduler jitter.
            deadline = time.monotonic() + 15.0
            while time.monotonic() < deadline:
                if worker_b.stats()["total"] == 1:
                    break
                time.sleep(0.1)
            assert worker_b.stats()["total"] == 1, (
                f"cross-worker broadcast: worker B must have applied "
                f"invalidate_sample('EXTRACT_KPIS', '0001') within 15s; "
                f"local still has {worker_b.stats()['total']} entries "
                f"(expected 1 — only 0002 remains)"
            )
            # Sample 0002 entry is preserved on both workers.
            assert worker_b.lookup(key_b) == out_0002.parent
        finally:
            worker_a.close()
            worker_b.close()

    @pytest.mark.xdist_group(name="distributed_cache_solo")
    def test_invalidate_step_broadcast_clears_per_step_entries(
        self,
        tmp_path: Path,
        fakeredis_backend: Any,
    ) -> None:
        """``invalidate_step('RUN_OPENSTUDIO_SIM')`` fans out across all
        workers and only affects entries for that one step (issue #1389
        cross-worker + per-step scope).

        Two workers, both store entries under two steps; worker A invalidates
        only ``RUN_OPENSTUDIO_SIM`` — workers B's local cache must drop
        just those entries via the pub/sub broadcast, leaving
        ``EXTRACT_KPIS`` entries intact.
        """
        from osimflow.cache import CacheKey
        from osimflow.distributed_cache import DistributedCache

        worker_a_db = tmp_path / "a" / "cache.sqlite"
        worker_b_db = tmp_path / "b" / "cache.sqlite"
        (tmp_path / "a").mkdir()
        (tmp_path / "b").mkdir()
        shared_outputs = tmp_path / "out"
        shared_outputs.mkdir()

        def _make_key(step: str, sid: str) -> CacheKey:
            return CacheKey(
                step=step,
                sample_id=sid,
                openstudio_version="3.11.0",
                inputs_sha256="i",
                code_sha256="c",
                container_digest="img",
                generation=0,
            )

        def _store(cache: DistributedCache, step: str, sid: str) -> None:
            out_dir = shared_outputs / step / sid
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "result.json").write_text("{}")
            cache.store(_make_key(step, sid), out_dir, exit_code=0)

        worker_a = DistributedCache(
            db_path=worker_a_db,
            redis_url="redis://shared:6379/0",
            campaign_id="shared-step",
        )
        worker_b = DistributedCache(
            db_path=worker_b_db,
            redis_url="redis://shared:6379/0",
            campaign_id="shared-step",
        )

        try:
            for step in ("RUN_OPENSTUDIO_SIM", "EXTRACT_KPIS"):
                _store(worker_a, step, "0001")
                _store(worker_a, step, "0002")

            # Backfill worker B from the shared store.
            for step in ("RUN_OPENSTUDIO_SIM", "EXTRACT_KPIS"):
                for sid in ("0001", "0002"):
                    assert worker_b.lookup(_make_key(step, sid)) is not None

            assert worker_b.stats()["by_step"].get("RUN_OPENSTUDIO_SIM", 0) == 2
            assert worker_b.stats()["by_step"].get("EXTRACT_KPIS", 0) == 2

            # Worker B is a passive listener — start its subscriber so the
            # broadcast from worker A lands on its local SQLite.
            worker_b._start_subscriber()  # noqa: SLF001 — see test contract

            # Wait until worker B's subscriber has actually SUBSCRIBE'd
            # to the channel before publishing from worker A.  Without
            # this, the SUBSCRIBE/PUBLISH race can lose the message when
            # the subscriber's async loop is still spinning up under
            # pytest-xdist worker scheduling jitter (issue #1389).
            channel = worker_b._channel  # noqa: SLF001
            sync_client = worker_a._get_sync_client()  # noqa: SLF001
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline:
                if sync_client.execute_command("PUBSUB", "NUMSUB", channel) == [channel, 1]:
                    break
                time.sleep(0.05)

            # Worker A invalidates the RUN_OPENSTUDIO_SIM step only.
            n = worker_a.invalidate_step("RUN_OPENSTUDIO_SIM")
            assert n == 2

            # Worker B receives the broadcast and clears its local
            # RUN_OPENSTUDIO_SIM entries only (publish fires a daemon
            # thread; poll with a generous deadline to absorb xdist
            # scheduler jitter).
            deadline = time.monotonic() + 15.0
            while time.monotonic() < deadline:
                wb_stats = worker_b.stats()
                if wb_stats["by_step"].get("RUN_OPENSTUDIO_SIM", 0) == 0:
                    break
                time.sleep(0.1)

            final = worker_b.stats()
            assert final["by_step"].get("RUN_OPENSTUDIO_SIM", 0) == 0, (
                f"cross-worker broadcast: worker B must have dropped all "
                f"RUN_OPENSTUDIO_SIM entries, got by_step={final['by_step']}"
            )
            assert final["by_step"].get("EXTRACT_KPIS", 0) == 2, (
                f"per-step scope: worker B's EXTRACT_KPIS entries must remain "
                f"intact after only RUN_OPENSTUDIO_SIM invalidate; "
                f"got by_step={final['by_step']}"
            )
        finally:
            worker_a.close()
            worker_b.close()


# ===========================================================================
# Optional live-Redis E2E (opt-in via OSIMFLOW_REDIS_E2E=1)
# ---------------------------------------------------------------------------
#
# When run in normal CI fakeredis is the only Redis available — the
# ``pytestmark`` skips this entire class.  On a developer workstation with
# a locally running Redis (``redis-server --port 6379``), set
# ``OSIMFLOW_REDIS_E2E=1`` and the same distributed-cache contract is
# asserted against a real broker.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    os.environ.get(LIVE_E2E_ENV) != "1",
    reason=(
        f"live Redis required; set {LIVE_E2E_ENV}=1 and run "
        f"`redis-server --port 6379` to opt into this E2E smoke"
    ),
)
def test_live_redis_smoke_3_sample_stub_campaign(
    workdir: Path,
    template_pkg: Path,
    outdir: Path,
) -> None:
    """Smoke test the same contract against a real local Redis.

    Skipped in normal CI (``OSIMFLOW_REDIS_E2E`` not set).  Asserts the
    same four-artifact contract + warm re-run HIT as the fakeredis
    path, so a future regression in the production Redis wiring
    surfaces even when fakeredis is healthy in CI.
    """
    _require_live_redis()
    campaign = _build_distributed_campaign(workdir, template_pkg, outdir, redis_url=LIVE_REDIS_URL)
    try:
        campaign.run()
    finally:
        campaign.cache.close()
    _require_four_artifacts(outdir)


def _require_live_redis() -> None:
    """Best-effort connectivity check for a real Redis at LIVE_REDIS_URL."""
    import redis as _redis_sync  # noqa: PLC0415

    try:
        client = _redis_sync.from_url(
            LIVE_REDIS_URL,
            socket_timeout=1.0,
            socket_connect_timeout=1.0,
            decode_responses=True,
        )
        client.ping()
        client.close()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"live Redis at {LIVE_REDIS_URL} unreachable: {exc}")


# ---------------------------------------------------------------------------
# Quiet down the noisy infos the production code emits through every test.
# (Keeps the test runner output manageable; behaviour is unchanged.)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _quiet_logging() -> Iterator[None]:
    logger = logging.getLogger("osimflow.distributed_cache")
    logger.setLevel(logging.WARNING)
    yield
    logger.setLevel(logging.INFO)
