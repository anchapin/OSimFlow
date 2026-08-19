"""Unit tests for osimflow/cache.py (issue #211).

Covers:
- CacheKey: construction, hashing, equality, frozen behavior
- sha256_of_files: single file, directory, missing file, determinism
- sha256_of_dict: stable hash, sort_keys, non-dict values
- SQLiteCache: lookup, store, lookup hit/miss, invalidation, stats
- Stale cache: output deleted after caching
- Failed step caching (exit_code != 0)
- WAL mode and busy_timeout for concurrent access (issue #247)
- Race conditions on close() during cancellation (issue #620)
"""

import multiprocessing as mp
import os
import sqlite3
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pytest

from osimflow.cache import CacheKey, CacheStats, SQLiteCache, sha256_of_dict, sha256_of_files

# --------------------------------------------------------------------------- #
# Issue #620 regression: multi-process / multi-thread race on cache.close().
#
# The worker function below MUST live at module scope so the spawn
# multiprocessing context can pickle it by reference into child processes.
# --------------------------------------------------------------------------- #


def _issue620_multiproc_worker(
    db_path_str: str,
    worker_id: int,
    n_ops: int,
    close_after: int | None,
) -> int:
    """Module-level worker for the issue #620 cross-process regression test.

    Each child process opens its OWN ``SQLiteCache`` instance pointing at
    the SAME ``db_path`` — exactly the multi-worker campaign shape that
    triggered ``FileNotFoundError: cache.sqlite-shm`` when one peer ran
    ``PRAGMA wal_checkpoint(TRUNCATE)`` on close during cancellation.

    Returns 0 on clean exit, 1 on any exception (with a traceback dumped
    to stderr so CI failure logs point straight at the offending line).
    """
    try:
        db_path = Path(db_path_str)
        cache = SQLiteCache(db_path)
        for i in range(n_ops):
            key = CacheKey(
                step=f"STEP_{worker_id}",
                sample_id=f"s{i}",
                openstudio_version="N/A",
                inputs_sha256=f"h_{i}",
                code_sha256="c",
                container_digest="py",
                generation=0,
            )
            out = db_path.parent / f"out_{worker_id}_{i}"
            out.mkdir(exist_ok=True)
            cache.store(key, out, exit_code=0)
            cache.lookup(key)
            if close_after is not None and i == close_after:
                # Simulate cancellation teardown mid-flight. With the old
                # wal_checkpoint(TRUNCATE) this removed the -wal/-shm
                # aux files out from under the still-writing peer
                # processes -> FileNotFoundError: cache.sqlite-shm.
                cache.close()
        if close_after is None:
            cache.close()
        return 0
    except Exception:
        traceback.print_exc()
        return 1


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

    def test_pragma_cache_size_mmap_size_temp_store(self, tmp_path: Path) -> None:
        """Regression test for issue #1016: PRAGMA cache_size, mmap_size,
        and temp_store must be configured for multi-GB caches."""
        db_path = tmp_path / "test_pragma_cache_size.sqlite"
        cache = SQLiteCache(db_path)
        # Populate with 10K synthetic entries so the cache has nontrivial state.
        for i in range(10_000):
            cache.store(
                CacheKey(
                    step="GENERATE_LHS_SAMPLES",
                    sample_id=f"s{i:08d}",
                    openstudio_version="3.11.0",
                    inputs_sha256="0" * 64,
                    code_sha256="0" * 64,
                    container_digest="",
                    generation=0,
                ),
                db_path,
                exit_code=0,
            )
        # PRAGMA cache_size / temp_store are per-connection; query the
        # cache's own live connection rather than opening a new one.
        conn = cache._ensure_conn()
        assert conn.execute("PRAGMA cache_size").fetchone()[0] == -65536, (
            "Expected cache_size=-65536 (64 MiB working set)"
        )
        assert conn.execute("PRAGMA mmap_size").fetchone()[0] == 268435456, (
            "Expected mmap_size=268435456 (256 MiB memory-mapped I/O)"
        )
        assert conn.execute("PRAGMA temp_store").fetchone()[0] == 2, (
            "Expected temp_store=2 (MEMORY)"
        )

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


class TestSQLiteCacheRaceOnClose:
    """Regression tests for issue #620: SQLite race conditions and crashes
    during campaign cancellation.

    Root cause: ``SQLiteCache.close()`` ran ``PRAGMA wal_checkpoint(TRUNCATE)``.
    When one worker process tore down its cache during cancellation while
    peer processes still had the cache open, the peers' next write hit a
    now-deleted ``-shm``/``-wal`` aux file and crashed with
    ``FileNotFoundError: cache.sqlite-shm``. ``close()`` was also not
    thread-safe (it ignored ``_lock``) and hid every error behind a
    blanket ``except Exception: pass``.

    Fix: ``close()`` uses ``wal_checkpoint(PASSIVE)`` (never removes the
    aux files), acquires ``_lock``, is idempotent, and logs/swallows
    ``sqlite3.OperationalError`` and ``FileNotFoundError`` specifically.
    """

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

    @pytest.mark.timeout(30)
    def test_close_is_thread_safe_and_idempotent(self, tmp_path: Path) -> None:
        """N threads concurrently operate on and ``close()`` one instance.

        Before the fix, ``close()`` did not acquire ``_lock`` and so a
        concurrent ``store()`` could observe ``_conn = None`` mid-write
        and raise ``ProgrammingError: Cannot operate on a closed
        database``. With the fix, ``close()`` is serialized against
        every operation and the next op re-opens lazily.

        The ``timeout`` marker turns a future re-entrancy regression
        (e.g. someone switching ``RLock`` back to ``Lock``) into a
        clean failure instead of a silent hang.
        """
        db_path = tmp_path / "test_close_threadsafe.sqlite"
        cache = SQLiteCache(db_path)
        n_threads = 8
        n_ops_per_thread = 50
        errors: list[Exception] = []
        err_lock = threading.Lock()

        def worker(tid: int) -> None:
            try:
                for i in range(n_ops_per_thread):
                    key = self._key(
                        step=f"S_{tid}",
                        sample_id=f"s{i}",
                        inputs_sha256=f"h{i}",
                    )
                    out = tmp_path / f"o_{tid}_{i}"
                    out.mkdir(exist_ok=True)
                    cache.store(key, out, exit_code=0)
                    cache.lookup(key)
                    # Interleave closes with operations to maximise the
                    # chance of catching a close/operate race. Lazy
                    # re-open on the next op must work seamlessly.
                    if i == n_ops_per_thread // 2:
                        cache.close()
                cache.close()
                cache.close()  # idempotent: double-close must not raise
            except Exception as e:  # noqa: BLE001 - we collect, not swallow
                with err_lock:
                    errors.append(e)

        with ThreadPoolExecutor(max_workers=n_threads) as ex:
            futures = [ex.submit(worker, i) for i in range(n_threads)]
            for f in as_completed(futures):
                f.result()

        assert not errors, f"concurrent close/operate errors: {errors}"

        # Cache must still open cleanly and report its data after the storm.
        cache_after = SQLiteCache(db_path)
        try:
            assert cache_after.stats()["total"] > 0
        finally:
            cache_after.close()

    @pytest.mark.timeout(30)
    def test_close_does_not_use_truncate_checkpoint(self, tmp_path: Path) -> None:
        """Issue #620 invariant: ``close()`` MUST NOT execute TRUNCATE.

        A TRUNCATE checkpoint is what removed the aux files and crashed
        peer workers. This test inspects the source to prevent a silent
        regression: it fails only on an executable ``execute(...)`` call
        that issues ``wal_checkpoint(TRUNCATE)``, not on prose mentions
        in docstrings/comments (which explain why we avoid it).
        """
        source = (Path(__file__).resolve().parents[2] / "osimflow" / "cache.py").read_text(
            encoding="utf-8"
        )
        # The actual invariant: no executable statement may issue a
        # TRUNCATE checkpoint. We match the two realistic call shapes
        # (``.execute("PRAGMA wal_checkpoint(TRUNCATE)")`` and the
        # bare ``execute("wal_checkpoint(TRUNCATE)"`` variant) while
        # allowing explanatory prose in docstrings/comments.
        import re

        executable_truncate = re.findall(
            r'\.execute\(\s*["\']PRAGMA\s*wal_checkpoint\(TRUNCATE\)["\']',
            source,
            flags=re.IGNORECASE,
        )
        assert not executable_truncate, (
            "SQLiteCache must not execute wal_checkpoint(TRUNCATE) — it "
            "removes the -wal/-shm aux files and crashes peer worker "
            "processes with FileNotFoundError (issue #620). Use PASSIVE."
        )
        # The PASSIVE checkpoint must be the documented teardown mode.
        assert "wal_checkpoint(PASSIVE)" in source

    @pytest.mark.timeout(90)
    def test_multiprocess_concurrent_close_no_aux_file_race(self, tmp_path: Path) -> None:
        """Cross-process regression: N processes share one DB path; one
        calls ``close()`` mid-flight while peers keep writing.

        This is the faithful reproduction of issue #620 (worker nodes are
        separate processes, not threads). Before the fix, worker 0's
        ``close()`` -> ``wal_checkpoint(TRUNCATE)`` removed the
        ``-wal``/``-shm`` files mid-write by the peers, crashing them
        with ``FileNotFoundError: cache.sqlite-shm``. After the fix,
        PASSIVE checkpoint + per-connection ``busy_timeout`` let every
        peer complete cleanly.
        """
        if os.environ.get("OSIMFLOW_SKIP_MULTIPROC_CACHE_TEST"):
            pytest.skip("OSIMFLOW_SKIP_MULTIPROC_CACHE_TEST set")

        db_path = tmp_path / "test_multiproc_close.sqlite"
        # Establish the schema + WAL mode once so all workers see a
        # consistent initial state, then close cleanly.
        SQLiteCache(db_path).close()

        n_workers = 4
        n_ops = 30
        ctx = mp.get_context("spawn")
        procs: list[mp.Process] = []
        for wid in range(n_workers):
            # Worker 0 closes its cache mid-flight (cancellation teardown
            # simulation); workers 1..N-1 keep writing through it.
            close_after = 10 if wid == 0 else None
            procs.append(
                ctx.Process(
                    target=_issue620_multiproc_worker,
                    args=(str(db_path), wid, n_ops, close_after),
                    name=f"issue620-worker-{wid}",
                )
            )

        for p in procs:
            p.start()
        try:
            for p in procs:
                p.join(timeout=60)
                if p.is_alive():
                    p.terminate()
                    p.join(timeout=5)
                    pytest.fail(
                        f"multiprocess cache worker {p.name!r} hung — "
                        "possible WAL/checkpoint deadlock (issue #620 regression)"
                    )
                assert p.exitcode == 0, (
                    f"worker {p.name!r} exited with code {p.exitcode}; "
                    "see the traceback on stderr above — this reproduces "
                    "issue #620 (FileNotFoundError on cache.sqlite-shm / "
                    "cache.sqlite-wal during cancellation)"
                )
        finally:
            for p in procs:
                if p.is_alive():
                    p.kill()
                    p.join(timeout=2)

        # Cache must still open cleanly after the cross-process storm.
        cache_after = SQLiteCache(db_path)
        try:
            total = cache_after.stats()["total"]
            assert total > 0, "cache lost all rows across the multiprocess run"
        finally:
            cache_after.close()

    def test_close_is_idempotent_on_fresh_instance(self, tmp_path: Path) -> None:
        """``close()`` on a fresh instance, then double-close, must be no-ops."""
        db_path = tmp_path / "test_idempotent_close.sqlite"
        cache = SQLiteCache(db_path)
        cache.close()
        cache.close()  # idempotent
        cache.close()  # idempotent
        # The instance must remain usable after explicit close (lazy reopen).
        out = tmp_path / "out"
        out.mkdir()
        cache.store(self._key(), out, exit_code=0)
        assert cache.lookup(self._key()) == out
        cache.close()
