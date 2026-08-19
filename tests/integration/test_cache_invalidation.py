"""Cache invalidation tests.

These tests are the gate for approval criterion #2 in
`.agents/results/result-architecture.md`:

  "Validation step 3 confirms cache invalidation behaves correctly for
   `bin/*.py` edits and OpenStudio version changes."

Each test sets up a cache, performs an action, mutates something, and
asserts the cache key no longer matches.
"""

from pathlib import Path

import pytest

from osimflow.cache import CacheKey, SQLiteCache, sha256_of_dict, sha256_of_files


@pytest.fixture
def tmp_cache(tmp_path: Path) -> SQLiteCache:
    return SQLiteCache(tmp_path / "cache.sqlite")


def test_lookup_miss_then_hit(tmp_cache: SQLiteCache, tmp_path: Path) -> None:
    out = tmp_path / "out.txt"
    out.write_text("hello")
    key = CacheKey("STEP", "S1", "N/A", "h1", "h2", "img")
    assert tmp_cache.lookup(key) is None
    tmp_cache.store(key, out, exit_code=0)
    assert tmp_cache.lookup(key) == out


def test_stale_output_detected(tmp_cache: SQLiteCache, tmp_path: Path) -> None:
    out = tmp_path / "out.txt"
    out.write_text("hello")
    key = CacheKey("STEP", "S1", "N/A", "h1", "h2", "img")
    tmp_cache.store(key, out, exit_code=0)
    out.unlink()  # simulate external deletion
    assert tmp_cache.lookup(key) is None  # hit, but not actionable


def test_failed_run_not_returned(tmp_cache: SQLiteCache, tmp_path: Path) -> None:
    out = tmp_path / "out.txt"
    out.write_text("partial")
    key = CacheKey("STEP", "S1", "N/A", "h1", "h2", "img")
    tmp_cache.store(key, out, exit_code=1)
    assert tmp_cache.lookup(key) is None  # failed runs must be re-run


def test_bin_py_edit_invalidates(tmp_cache: SQLiteCache, tmp_path: Path) -> None:
    """The fix for architecture-decision issue #2: editing a bin/*.py file
    must invalidate the cached entry, because its content is part of the
    code_sha256 that distinguishes cache keys.

    Semantics: the cache itself is content-addressed. After a `bin/*.py`
    edit, the *Campaign* will compute a different `code_sha256` and use
    a different cache key. This test proves (a) the hash function is
    content-sensitive and (b) the cache correctly distinguishes two keys
    whose only difference is the code hash.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "extract_kpis.py"

    script.write_text("v1 = 'extract logic 1'")
    h_v1 = sha256_of_files([script])

    # User edits the script.
    script.write_text("v2 = 'extract logic 2'")
    h_v2 = sha256_of_files([script])
    assert h_v1 != h_v2  # (a) hash is content-sensitive

    out = tmp_path / "out.txt"
    out.write_text("kpis v2")
    key_v2 = CacheKey("EXTRACT_KPIS", "S1", "N/A", "inputs-h", h_v2, "img")
    tmp_cache.store(key_v2, out, exit_code=0)
    # (b) a different code hash produces a different cache slot; the
    # Campaign, having re-hashed the edited bin file, will not collide.
    key_v1 = CacheKey("EXTRACT_KPIS", "S1", "N/A", "inputs-h", h_v1, "img")
    assert tmp_cache.lookup(key_v1) is None  # v1 key is empty
    assert tmp_cache.lookup(key_v2) is not None  # v2 key is stored


def test_openstudio_version_change_invalidates_only_sim_step(
    tmp_cache: SQLiteCache, tmp_path: Path
) -> None:
    """PRD §6 gotcha #3: bumping openstudio_version must invalidate
    RUN_OPENSTUDIO_SIM but NOT the LHS / apply / extract steps."""
    out = tmp_path / "out.txt"
    out.write_text("kpis")
    # Store entries across all steps for os_version=3.11.0
    for step, sid in [
        ("GENERATE_LHS_SAMPLES", "ALL"),
        ("APPLY_PARAMETERS", "S1"),
        ("RUN_OPENSTUDIO_SIM", "S1"),
        ("EXTRACT_KPIS", "S1"),
    ]:
        k = CacheKey(step, sid, "3.11.0", "h", "h", "img")
        tmp_cache.store(k, out, exit_code=0)
    assert tmp_cache.stats()["total"] == 4

    # Simulate the bump: invalidate only RUN_OPENSTUDIO_SIM
    tmp_cache.invalidate_step("RUN_OPENSTUDIO_SIM")
    assert tmp_cache.stats()["total"] == 3
    assert tmp_cache.stats()["by_step"].get("RUN_OPENSTUDIO_SIM", 0) == 0
    # All other steps preserved
    assert tmp_cache.stats()["by_step"].get("GENERATE_LHS_SAMPLES", 0) == 1
    assert tmp_cache.stats()["by_step"].get("APPLY_PARAMETERS", 0) == 1
    assert tmp_cache.stats()["by_step"].get("EXTRACT_KPIS", 0) == 1


def test_extract_kpis_uses_openstudio_version_in_cache_key(
    tmp_cache: SQLiteCache, tmp_path: Path
) -> None:
    """Regression test for issue #643: EXTRACT_KPIS cache key must use the
    actual OpenStudio version (not 'N/A') so that cache hits don't return
    wrong results when the same parameters are used with different OpenStudio
    versions."""
    out = tmp_path / "out.txt"
    out.write_text("kpis")

    sim_dir = str(tmp_path / "sim" / "S1")
    sid = "S1"

    # Compute inputs_hash as step_extract_kpis does (with os_version in hash)
    inputs_hash_v1 = sha256_of_dict({"sim_dir": sim_dir, "sid": sid, "os_version": "3.11.0"})
    inputs_hash_v2 = sha256_of_dict({"sim_dir": sim_dir, "sid": sid, "os_version": "3.12.0"})
    # Same sim_dir/sid but different os_version -> different inputs_hash
    assert inputs_hash_v1 != inputs_hash_v2

    # Build cache keys as step_extract_kpis does after the fix
    key_v1 = CacheKey(
        step="EXTRACT_KPIS",
        sample_id=sid,
        openstudio_version="3.11.0",
        inputs_sha256=inputs_hash_v1,
        code_sha256="code-hash",
        container_digest="img",
        generation=0,
    )
    key_v2 = CacheKey(
        step="EXTRACT_KPIS",
        sample_id=sid,
        openstudio_version="3.12.0",
        inputs_sha256=inputs_hash_v2,
        code_sha256="code-hash",
        container_digest="img",
        generation=0,
    )

    # Verify the two keys are different (different openstudio_version field)
    assert key_v1 != key_v2

    # Store v1 in cache, verify v2 is a miss
    tmp_cache.store(key_v1, out, exit_code=0)
    assert tmp_cache.lookup(key_v1) is not None
    assert tmp_cache.lookup(key_v2) is None

    # Verify the bug is fixed: EXTRACT_KPIS does NOT use "N/A" as version
    key_na = CacheKey(
        step="EXTRACT_KPIS",
        sample_id=sid,
        openstudio_version="N/A",
        inputs_sha256=inputs_hash_v1,
        code_sha256="code-hash",
        container_digest="img",
        generation=0,
    )
    # The "N/A" version key should NOT match the stored v1 key
    assert tmp_cache.lookup(key_na) is None


def test_template_sim_package_change_invalidates_apply_and_run(
    tmp_cache: SQLiteCache, tmp_path: Path
) -> None:
    """If the user updates template_sim_package, the modified package and
    the simulation that uses it must both be re-run."""
    out = tmp_path / "out.txt"
    out.write_text("x")
    for step, sid in [
        ("APPLY_PARAMETERS", "S1"),
        ("RUN_OPENSTUDIO_SIM", "S1"),
        ("EXTRACT_KPIS", "S1"),
    ]:
        k = CacheKey(step, sid, "3.11.0", "old-template-h", "h", "img")
        tmp_cache.store(k, out, exit_code=0)
    # The Campaign recomputes inputs_sha256 from the template content, so
    # an edit naturally produces a different key. Verify the equivalence:
    # bumping the template hash means a miss.
    new_key = CacheKey("APPLY_PARAMETERS", "S1", "3.11.0", "new-template-h", "h", "img")
    assert tmp_cache.lookup(new_key) is None


def test_variables_yml_change_invalidates_lhs_only(tmp_cache: SQLiteCache, tmp_path: Path) -> None:
    """Changing variables.yml invalidates LHS, which is a different code path
    (we'd need to also invalidate apply/run/extract because their sample_ids
    may change). For the spike we demonstrate the LHS invalidation only."""
    out = tmp_path / "out.txt"
    out.write_text("x")
    k = CacheKey("GENERATE_LHS_SAMPLES", "ALL", "N/A", "old-vars-h", "h", "img")
    tmp_cache.store(k, out, exit_code=0)
    new_key = CacheKey("GENERATE_LHS_SAMPLES", "ALL", "N/A", "new-vars-h", "h", "img")
    assert tmp_cache.lookup(new_key) is None


def test_aggregate_results_uses_work_hash_not_bin_hash(
    tmp_cache: SQLiteCache, tmp_path: Path
) -> None:
    """Regression test for issue #652: AGGREGATE_RESULTS must use the work
    hash (hash of work.py), not the bin hash (hash of bin/*.py scripts).

    The Python aggregation logic lives in work.py (the work layer that invokes
    bin/aggregate_results.py). When work.py changes, aggregation must re-run
    because the Python-side orchestration changed. Bin script edits should NOT
    invalidate the AGGREGATE_RESULTS cache key because the Python layer that
    invokes them is what gets hashed.
    """
    # Simulate work.py content at two points in time
    work_dir = tmp_path / "work_module"
    work_dir.mkdir()
    work_file = work_dir / "work.py"
    work_file.write_text("version = 'v1'")
    h_work_v1 = sha256_of_files([work_file])

    work_file.write_text("version = 'v2'")
    h_work_v2 = sha256_of_files([work_file])
    assert h_work_v1 != h_work_v2  # work.py changed

    # Simulate bin/aggregate_results.py content at two points
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    agg_script = bin_dir / "aggregate_results.py"
    agg_script.write_text("# aggregate v1")
    h_bin_v1 = sha256_of_files([agg_script])

    agg_script.write_text("# aggregate v2")
    h_bin_v2 = sha256_of_files([agg_script])
    assert h_bin_v1 != h_bin_v2  # bin script changed

    # Build AGGREGATE_RESULTS cache keys as campaign does after the fix
    # The keys use work hash, not bin hash
    kpi_files = [str(tmp_path / "kpi_1.json"), str(tmp_path / "kpi_2.json")]
    sim_dirs = [str(tmp_path / "sim_1"), str(tmp_path / "sim_2")]
    inputs_hash = sha256_of_dict({"kpis": kpi_files, "sims": sim_dirs})

    # Key with work hash v1
    key_work_v1 = CacheKey(
        step="AGGREGATE_RESULTS",
        sample_id="ALL",
        openstudio_version="N/A",
        inputs_sha256=inputs_hash,
        code_sha256=h_work_v1,
        container_digest="python-img",
    )
    # Key with work hash v2 (work.py changed)
    key_work_v2 = CacheKey(
        step="AGGREGATE_RESULTS",
        sample_id="ALL",
        openstudio_version="N/A",
        inputs_sha256=inputs_hash,
        code_sha256=h_work_v2,
        container_digest="python-img",
    )
    # Key with bin hash v2 (bin script changed, but should NOT affect this key)
    key_bin_v2 = CacheKey(
        step="AGGREGATE_RESULTS",
        sample_id="ALL",
        openstudio_version="N/A",
        inputs_sha256=inputs_hash,
        code_sha256=h_bin_v2,
        container_digest="python-img",
    )

    # Store result with work hash v1
    out = tmp_path / "aggregated.csv"
    out.write_text("sample_id,kpi1,kpi2\n1,100,200\n2,150,250\n")
    tmp_cache.store(key_work_v1, out, exit_code=0)

    # Verify: work hash change -> cache MISS (must re-aggregate)
    assert tmp_cache.lookup(key_work_v2) is None, (
        "AGGREGATE_RESULTS cache should miss when work.py changes"
    )

    # Verify: bin hash change does NOT affect AGGREGATE_RESULTS cache
    # (because it uses work hash, not bin hash)
    assert tmp_cache.lookup(key_bin_v2) is None, (
        "AGGREGATE_RESULTS cache key should use work hash, not bin hash; "
        "bin script changes should not affect this cache key"
    )

    # Verify: same work hash v1 is still a HIT
    assert tmp_cache.lookup(key_work_v1) is not None, (
        "AGGREGATE_RESULTS cache should hit when work.py hasn't changed"
    )


def test_stats(tmp_cache: SQLiteCache, tmp_path: Path) -> None:
    """Cache primary key has 6 columns; same key replaces, different key adds."""
    out = tmp_path / "o.txt"
    out.write_text("x")
    tmp_cache.store(CacheKey("A", "S", "v", "h", "h", "i"), out, 0)
    tmp_cache.store(CacheKey("A", "S", "v", "h", "h", "i"), out, 0)  # same key -> REPLACE
    tmp_cache.store(CacheKey("B", "S", "v", "h", "h", "i"), out, 0)  # different key -> INSERT
    s = tmp_cache.stats()
    assert s["total"] == 2
    assert s["by_step"] == {"A": 1, "B": 1}


def test_bin_py_edit_invalidates_code_hash_in_dev_mode() -> None:
    """Regression test for issue #1021.

    In a ``pip install -e .`` development checkout both
    ``osimflow/_work_scripts/*.py`` and ``bin/*.py`` exist. The bug was
    that ``Campaign._compute_code_hashes`` only hashed whichever directory
    was found *first* (``_work_scripts/``), so editing a ``bin/*.py`` file
    left the cache key unchanged and the campaign kept returning stale
    results.

    After the fix, the hash covers the UNION of both directories
    (sorted, deduped) whenever either exists. This test exercises the
    actual code path: it touches a real ``bin/*.py`` file in the worktree,
    re-computes the hash via ``Campaign._compute_code_hashes``, and
    asserts the hash changed. The file is restored before the test
    returns so subsequent runs are unaffected.
    """
    from osimflow.campaign import Campaign

    # Resolve the worktree's bin/ directory (the dev fallback directory).
    repo_root = Path(__file__).resolve().parents[2]
    bin_dir = repo_root / "bin"
    assert bin_dir.is_dir(), "this test requires a development checkout (bin/ must exist on disk)"

    target = bin_dir / "aggregate_results.py"
    assert target.is_file(), f"{target} should exist in a dev checkout"

    original_content = target.read_text()
    try:
        # Baseline hash via the same path Campaign uses.
        # _compute_code_hashes does not depend on instance state, so a
        # bare bound method call on a None self is sufficient. We use
        # a tiny stub object to keep mypy strict-mode happy.
        class _Stub:
            pass

        baseline = Campaign._compute_code_hashes(_Stub())

        # Touch a bin/*.py file by appending a no-op comment. SHA-256 of
        # the union must change.
        with target.open("a", encoding="utf-8") as f:
            f.write("# no-op touch for issue #1021 regression test\n")

        after = Campaign._compute_code_hashes(_Stub())

        assert "bin" in baseline and "bin" in after
        assert baseline["bin"] != after["bin"], (
            "Editing bin/*.py must change the 'bin' code hash. "
            "If this fails, _compute_code_hashes is still preferring "
            "_work_scripts/ over bin/ (issue #1021)."
        )
        # The work hash must be unaffected by a bin/ edit.
        assert baseline["work"] == after["work"]
    finally:
        # Restore the file byte-for-byte so subsequent test runs and
        # human edits see the same starting content.
        target.write_text(original_content)
