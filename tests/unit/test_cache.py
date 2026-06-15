"""Unit tests for osimflow/cache.py (issue #211).

Covers:
- CacheKey: construction, hashing, equality, frozen behavior
- sha256_of_files: single file, directory, missing file, determinism
- sha256_of_dict: stable hash, sort_keys, non-dict values
- SQLiteCache: lookup, store, lookup hit/miss, invalidation, stats
- Stale cache: output deleted after caching
- Failed step caching (exit_code != 0)
- WAL mode and busy_timeout for concurrent access (issue #247)
"""

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pytest

from osimflow.cache import CacheKey, CacheStats, SQLiteCache, sha256_of_dict, sha256_of_files


class TestCacheKey:
    def test_construction(self) -> None:
        key = CacheKey(
            step="GENERATE_LHS_SAMPLES",
            sample_id="ALL",
            openstudio_version="N/A",
            inputs_sha256="abc123",
            code_sha256="def456",
            container_digest="py:latest",
        )
        assert key.step == "GENERATE_LHS_SAMPLES"
        assert key.sample_id == "ALL"
        assert key.generation == 0

    def test_frozen(self) -> None:
        key = CacheKey(
            step="S",
            sample_id="ALL",
            openstudio_version="N/A",
            inputs_sha256="a",
            code_sha256="b",
            container_digest="c",
        )
        with pytest.raises(AttributeError):
            key.step = "OTHER"  # type: ignore[misc]

    def test_equality(self) -> None:
        k1 = CacheKey(
            step="S",
            sample_id="ALL",
            openstudio_version="N/A",
            inputs_sha256="a",
            code_sha256="b",
            container_digest="c",
        )
        k2 = CacheKey(
            step="S",
            sample_id="ALL",
            openstudio_version="N/A",
            inputs_sha256="a",
            code_sha256="b",
            container_digest="c",
        )
        assert k1 == k2

    def test_inequality_different_step(self) -> None:
        k1 = CacheKey(
            step="S1",
            sample_id="ALL",
            openstudio_version="N/A",
            inputs_sha256="a",
            code_sha256="b",
            container_digest="c",
        )
        k2 = CacheKey(
            step="S2",
            sample_id="ALL",
            openstudio_version="N/A",
            inputs_sha256="a",
            code_sha256="b",
            container_digest="c",
        )
        assert k1 != k2

    def test_inequality_different_generation(self) -> None:
        k1 = CacheKey(
            step="S",
            sample_id="ALL",
            openstudio_version="N/A",
            inputs_sha256="a",
            code_sha256="b",
            container_digest="c",
            generation=0,
        )
        k2 = CacheKey(
            step="S",
            sample_id="ALL",
            openstudio_version="N/A",
            inputs_sha256="a",
            code_sha256="b",
            container_digest="c",
            generation=1,
        )
        assert k1 != k2

    def test_hashable(self) -> None:
        k1 = CacheKey(
            step="S",
            sample_id="ALL",
            openstudio_version="N/A",
            inputs_sha256="a",
            code_sha256="b",
            container_digest="c",
        )
        k2 = CacheKey(
            step="S",
            sample_id="ALL",
            openstudio_version="N/A",
            inputs_sha256="a",
            code_sha256="b",
            container_digest="c",
        )
        assert hash(k1) == hash(k2)
        s = {k1, k2}
        assert len(s) == 1

    def test_generation_default(self) -> None:
        key = CacheKey(
            step="S",
            sample_id="ALL",
            openstudio_version="N/A",
            inputs_sha256="a",
            code_sha256="b",
            container_digest="c",
        )
        assert key.generation == 0


class TestSha256OfFiles:
    def test_single_file(self, tmp_path: Path) -> None:
        f = tmp_path / "a.py"
        f.write_text("print('hello')")
        h = sha256_of_files([f])
        assert isinstance(h, str)
        assert len(h) == 64

    def test_deterministic(self, tmp_path: Path) -> None:
        f = tmp_path / "a.py"
        f.write_text("x = 1")
        h1 = sha256_of_files([f])
        h2 = sha256_of_files([f])
        assert h1 == h2

    def test_content_change_invalidates(self, tmp_path: Path) -> None:
        f = tmp_path / "a.py"
        f.write_text("x = 1")
        h1 = sha256_of_files([f])
        f.write_text("x = 2")
        h2 = sha256_of_files([f])
        assert h1 != h2

    def test_directory_hashed(self, tmp_path: Path) -> None:
        d = tmp_path / "dir"
        d.mkdir()
        (d / "file.txt").write_text("content")
        h = sha256_of_files([d])
        assert len(h) == 64

    def test_nonexistent_file(self, tmp_path: Path) -> None:
        f = tmp_path / "nonexistent.py"
        h = sha256_of_files([f])
        assert len(h) == 64

    def test_multiple_files_sorted(self, tmp_path: Path) -> None:
        f1 = tmp_path / "b.py"
        f2 = tmp_path / "a.py"
        f1.write_text("b")
        f2.write_text("a")
        h1 = sha256_of_files([f1, f2])
        h2 = sha256_of_files([f2, f1])
        assert h1 == h2

    def test_empty_list(self) -> None:
        h = sha256_of_files([])
        assert len(h) == 64


class TestSha256OfDict:
    def test_basic(self) -> None:
        d = {"a": 1, "b": "two"}
        h = sha256_of_dict(d)
        assert isinstance(h, str)
        assert len(h) == 64

    def test_sort_keys(self) -> None:
        h1 = sha256_of_dict({"a": 1, "b": 2})
        h2 = sha256_of_dict({"b": 2, "a": 1})
        assert h1 == h2

    def test_different_values(self) -> None:
        h1 = sha256_of_dict({"x": 1})
        h2 = sha256_of_dict({"x": 2})
        assert h1 != h2

    def test_nested(self) -> None:
        h = sha256_of_dict({"outer": {"inner": [1, 2, 3]}})
        assert len(h) == 64


class TestSQLiteCache:
    @pytest.fixture
    def cache(self, tmp_path: Path) -> SQLiteCache:
        return SQLiteCache(tmp_path / "test.sqlite")

    def _key(self, **overrides: object) -> CacheKey:
        defaults: dict[str, object] = {
            "step": "GENERATE_LHS_SAMPLES",
            "sample_id": "ALL",
            "openstudio_version": "N/A",
            "inputs_sha256": "abc",
            "code_sha256": "def",
            "container_digest": "py",
            "generation": 0,
        }
        defaults.update(overrides)
        return CacheKey(**defaults)  # type: ignore[arg-type]

    def test_creates_db(self, tmp_path: Path) -> None:
        db_path = tmp_path / "subdir" / "cache.sqlite"
        SQLiteCache(db_path)
        assert db_path.exists()

    def test_store_and_lookup(self, cache: SQLiteCache, tmp_path: Path) -> None:
        out = tmp_path / "output"
        out.mkdir()
        key = self._key()
        cache.store(key, out, exit_code=0)
        result = cache.lookup(key)
        assert result == out

    def test_lookup_miss(self, cache: SQLiteCache) -> None:
        key = self._key()
        result = cache.lookup(key)
        assert result is None

    def test_different_step_is_miss(self, cache: SQLiteCache, tmp_path: Path) -> None:
        out = tmp_path / "output"
        out.mkdir()
        key1 = self._key(step="STEP_A")
        cache.store(key1, out, exit_code=0)
        key2 = self._key(step="STEP_B")
        assert cache.lookup(key2) is None

    def test_different_sample_is_miss(self, cache: SQLiteCache, tmp_path: Path) -> None:
        out = tmp_path / "output"
        out.mkdir()
        key1 = self._key(sample_id="s0001")
        cache.store(key1, out, exit_code=0)
        key2 = self._key(sample_id="s0002")
        assert cache.lookup(key2) is None

    def test_different_version_is_miss(self, cache: SQLiteCache, tmp_path: Path) -> None:
        out = tmp_path / "output"
        out.mkdir()
        key1 = self._key(openstudio_version="3.9.0")
        cache.store(key1, out, exit_code=0)
        key2 = self._key(openstudio_version="3.11.0")
        assert cache.lookup(key2) is None

    def test_different_inputs_hash_is_miss(self, cache: SQLiteCache, tmp_path: Path) -> None:
        out = tmp_path / "output"
        out.mkdir()
        key1 = self._key(inputs_sha256="hash_v1")
        cache.store(key1, out, exit_code=0)
        key2 = self._key(inputs_sha256="hash_v2")
        assert cache.lookup(key2) is None

    def test_different_code_hash_is_miss(self, cache: SQLiteCache, tmp_path: Path) -> None:
        out = tmp_path / "output"
        out.mkdir()
        key1 = self._key(code_sha256="code_v1")
        cache.store(key1, out, exit_code=0)
        key2 = self._key(code_sha256="code_v2")
        assert cache.lookup(key2) is None

    def test_different_generation_is_miss(self, cache: SQLiteCache, tmp_path: Path) -> None:
        out = tmp_path / "output"
        out.mkdir()
        key1 = self._key(generation=0)
        cache.store(key1, out, exit_code=0)
        key2 = self._key(generation=1)
        assert cache.lookup(key2) is None

    def test_failed_exit_code_is_miss(self, cache: SQLiteCache, tmp_path: Path) -> None:
        out = tmp_path / "output"
        out.mkdir()
        key = self._key()
        cache.store(key, out, exit_code=1)
        assert cache.lookup(key) is None

    def test_stale_output_is_miss(self, cache: SQLiteCache, tmp_path: Path) -> None:
        out = tmp_path / "output_dir"
        out.mkdir()
        key = self._key()
        cache.store(key, out, exit_code=0)
        assert cache.lookup(key) == out
        import shutil

        shutil.rmtree(out)
        assert cache.lookup(key) is None

    def test_invalidate_step(self, cache: SQLiteCache, tmp_path: Path) -> None:
        out1 = tmp_path / "out1"
        out2 = tmp_path / "out2"
        out1.mkdir()
        out2.mkdir()
        cache.store(self._key(step="STEP_A", sample_id="s1"), out1, exit_code=0)
        cache.store(self._key(step="STEP_A", sample_id="s2"), out2, exit_code=0)
        cache.store(self._key(step="STEP_B", sample_id="s1"), out1, exit_code=0)
        n = cache.invalidate_step("STEP_A")
        assert n == 2
        assert cache.lookup(self._key(step="STEP_A", sample_id="s1")) is None
        assert cache.lookup(self._key(step="STEP_B", sample_id="s1")) is not None

    def test_invalidate_sample(self, cache: SQLiteCache, tmp_path: Path) -> None:
        out = tmp_path / "out"
        out.mkdir()
        cache.store(self._key(step="STEP_A", sample_id="s1"), out, exit_code=0)
        cache.store(self._key(step="STEP_A", sample_id="s2"), out, exit_code=0)
        n = cache.invalidate_sample("STEP_A", "s1")
        assert n == 1
        assert cache.lookup(self._key(step="STEP_A", sample_id="s1")) is None
        assert cache.lookup(self._key(step="STEP_A", sample_id="s2")) is not None

    def test_stats_empty(self, cache: SQLiteCache) -> None:
        stats = cache.stats()
        assert stats["total"] == 0
        assert stats["by_step"] == {}

    def test_stats_with_entries(self, cache: SQLiteCache, tmp_path: Path) -> None:
        out = tmp_path / "out"
        out.mkdir()
        cache.store(self._key(step="STEP_A"), out, exit_code=0)
        cache.store(self._key(step="STEP_B"), out, exit_code=0)
        cache.store(self._key(step="STEP_A", sample_id="s2"), out, exit_code=0)
        stats = cache.stats()
        assert stats["total"] == 3
        assert stats["by_step"]["STEP_A"] == 2
        assert stats["by_step"]["STEP_B"] == 1

    def test_store_replaces(self, cache: SQLiteCache, tmp_path: Path) -> None:
        out1 = tmp_path / "out1"
        out2 = tmp_path / "out2"
        out1.mkdir()
        out2.mkdir()
        key = self._key()
        cache.store(key, out1, exit_code=0)
        cache.store(key, out2, exit_code=0)
        result = cache.lookup(key)
        assert result == out2

    def test_invalidate_step_returns_zero_when_empty(self, cache: SQLiteCache) -> None:
        n = cache.invalidate_step("NONEXISTENT")
        assert n == 0

    def test_invalidate_sample_returns_zero_when_empty(self, cache: SQLiteCache) -> None:
        n = cache.invalidate_sample("NONEXISTENT", "s1")
        assert n == 0

    def test_wal_mode_enabled(self, tmp_path: Path) -> None:
        db_path = tmp_path / "test_wal.sqlite"
        SQLiteCache(db_path)
        conn = sqlite3.connect(db_path)
        try:
            result = conn.execute("PRAGMA journal_mode").fetchone()[0]
            assert result == "wal", f"Expected WAL mode, got {result}"
        finally:
            conn.close()

    def test_busy_timeout_set(self, tmp_path: Path) -> None:
        db_path = tmp_path / "test_busy_timeout.sqlite"
        SQLiteCache(db_path)
        conn = sqlite3.connect(db_path)
        try:
            result = conn.execute("PRAGMA busy_timeout").fetchone()[0]
            assert result == 5000, f"Expected busy_timeout=5000, got {result}"
        finally:
            conn.close()

    def test_concurrent_store_and_lookup(self, tmp_path: Path) -> None:
        db_path = tmp_path / "test_concurrent.sqlite"
        cache = SQLiteCache(db_path)
        num_threads = 10
        num_operations = 20

        errors: list[Exception] = []
        lock = threading.Lock()

        def worker(thread_id: int) -> None:
            try:
                for op_id in range(num_operations):
                    key = CacheKey(
                        step=f"STEP_{thread_id % 3}",
                        sample_id=f"s{thread_id}_{op_id}",
                        openstudio_version="N/A",
                        inputs_sha256=f"hash_{op_id}",
                        code_sha256="code",
                        container_digest="py",
                        generation=0,
                    )
                    out = tmp_path / f"out_{thread_id}_{op_id}"
                    out.mkdir(exist_ok=True)
                    cache.store(key, out, exit_code=0)
                    result = cache.lookup(key)
                    assert result == out
            except Exception as e:
                with lock:
                    errors.append(e)

        with ThreadPoolExecutor(max_workers=num_threads) as executor:
            futures = [executor.submit(worker, i) for i in range(num_threads)]
            for f in as_completed(futures):
                f.result()

        assert not errors, f"Concurrent access errors: {errors}"

    def test_get_stats_empty(self, cache: SQLiteCache) -> None:
        stats = cache.get_stats()
        assert stats.hits == 0
        assert stats.misses == 0
        assert stats.invalidations == 0
        assert stats.total_keys == 0

    def test_get_stats_after_miss(self, cache: SQLiteCache) -> None:
        key = self._key()
        cache.lookup(key)
        stats = cache.get_stats()
        assert stats.hits == 0
        assert stats.misses == 1
        assert stats.total_keys == 0

    def test_get_stats_after_hit(self, cache: SQLiteCache, tmp_path: Path) -> None:
        out = tmp_path / "out"
        out.mkdir()
        key = self._key()
        cache.store(key, out, exit_code=0)
        cache.lookup(key)
        stats = cache.get_stats()
        assert stats.hits == 1
        assert stats.misses == 0
        assert stats.total_keys == 1

    def test_get_stats_after_invalidate(self, cache: SQLiteCache, tmp_path: Path) -> None:
        out = tmp_path / "out"
        out.mkdir()
        cache.store(self._key(step="STEP_A"), out, exit_code=0)
        cache.store(self._key(step="STEP_A", sample_id="s2"), out, exit_code=0)
        cache.invalidate_step("STEP_A")
        stats = cache.get_stats()
        assert stats.invalidations == 2
        assert stats.total_keys == 0

    def test_get_cache_hit_rate_no_lookups(self, cache: SQLiteCache) -> None:
        rate = cache.get_cache_hit_rate()
        assert rate == 0.0

    def test_get_cache_hit_rate_all_misses(self, cache: SQLiteCache) -> None:
        key = self._key()
        cache.lookup(key)
        cache.lookup(key)
        rate = cache.get_cache_hit_rate()
        assert rate == 0.0

    def test_get_cache_hit_rate_all_hits(self, cache: SQLiteCache, tmp_path: Path) -> None:
        out = tmp_path / "out"
        out.mkdir()
        key = self._key()
        cache.store(key, out, exit_code=0)
        cache.lookup(key)
        cache.lookup(key)
        rate = cache.get_cache_hit_rate()
        assert rate == 1.0

    def test_get_cache_hit_rate_mixed(self, cache: SQLiteCache, tmp_path: Path) -> None:
        out = tmp_path / "out"
        out.mkdir()
        key = self._key()
        cache.store(key, out, exit_code=0)
        cache.lookup(key)
        cache.lookup(self._key(sample_id="other"))
        rate = cache.get_cache_hit_rate()
        assert rate == 0.5

    def test_cache_stats_dataclass(self) -> None:
        stats = CacheStats(hits=10, misses=5, invalidations=3, total_keys=20)
        assert stats.hits == 10
        assert stats.misses == 5
        assert stats.invalidations == 3
        assert stats.total_keys == 20
