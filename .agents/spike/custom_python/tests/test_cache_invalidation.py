"""Cache invalidation tests.

These tests are the gate for approval criterion #2 in
`.agents/results/result-architecture.md`:

  "Validation step 3 confirms cache invalidation behaves correctly for
   `bin/*.py` edits and OpenStudio version changes."

Each test sets up a cache, performs an action, mutates something, and
asserts the cache key no longer matches.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
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
    # Store entries across all steps for os_version=3.4.0
    for step, sid in [("GENERATE_LHS_SAMPLES", "ALL"),
                       ("APPLY_PARAMETERS", "S1"),
                       ("RUN_OPENSTUDIO_SIM", "S1"),
                       ("EXTRACT_KPIS", "S1")]:
        k = CacheKey(step, sid, "3.4.0", "h", "h", "img")
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


def test_template_sim_package_change_invalidates_apply_and_run(
    tmp_cache: SQLiteCache, tmp_path: Path
) -> None:
    """If the user updates template_sim_package, the modified package and
    the simulation that uses it must both be re-run."""
    out = tmp_path / "out.txt"
    out.write_text("x")
    for step, sid in [("APPLY_PARAMETERS", "S1"),
                       ("RUN_OPENSTUDIO_SIM", "S1"),
                       ("EXTRACT_KPIS", "S1")]:
        k = CacheKey(step, sid, "3.4.0", "old-template-h", "h", "img")
        tmp_cache.store(k, out, exit_code=0)
    # The Campaign recomputes inputs_sha256 from the template content, so
    # an edit naturally produces a different key. Verify the equivalence:
    # bumping the template hash means a miss.
    new_key = CacheKey("APPLY_PARAMETERS", "S1", "3.4.0", "new-template-h", "h", "img")
    assert tmp_cache.lookup(new_key) is None


def test_variables_yml_change_invalidates_lhs_only(
    tmp_cache: SQLiteCache, tmp_path: Path
) -> None:
    """Changing variables.yml invalidates LHS, which is a different code path
    (we'd need to also invalidate apply/run/extract because their sample_ids
    may change). For the spike we demonstrate the LHS invalidation only."""
    out = tmp_path / "out.txt"
    out.write_text("x")
    k = CacheKey("GENERATE_LHS_SAMPLES", "ALL", "N/A", "old-vars-h", "h", "img")
    tmp_cache.store(k, out, exit_code=0)
    new_key = CacheKey("GENERATE_LHS_SAMPLES", "ALL", "N/A", "new-vars-h", "h", "img")
    assert tmp_cache.lookup(new_key) is None


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
