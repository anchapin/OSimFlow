"""Unit tests for osimflow/distributed_cache.py (issue #330).

Covers:
- build_cache: returns SQLiteCache when redis_url is None,
  DistributedCache when redis_url is set
- DistributedCache: wraps SQLiteCache interface (lookup, store, stats)
- DistributedCache: invalidate_step broadcasts to Redis and invalidates locally
- DistributedCache: invalidate_sample broadcasts to Redis and invalidates locally
- Subscriber thread: starts lazily, stops on close()
- Message format: correct JSON shape for invalidate_step / invalidate_sample
- Idempotent subscriber start
- Graceful handling of Redis connection failures
- Circuit breaker: repeated Redis failures open the breaker and shared-store
  calls fail fast (issue #1451); half-open success closes it; a failed
  half-open probe re-opens with ``_consecutive_failures`` reset to 0
  (the d1056b8/#1330 behaviour)
"""

import json
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from osimflow.cache import CacheKey, SQLiteCache
from osimflow.circuit_breaker import CircuitBreaker
from osimflow.distributed_cache import DistributedCache, build_cache

# Detect the optional `redis` extra ONCE at import time. The previous
# implementation used a mutable module-level global (``_HAS_REDIS: bool |
# None``) mutated by a ``_check_redis()`` helper — shared state that can
# leak across test modules under pytest-xdist and produce inconsistent
# skip decisions. A plain import-time constant has no mutation surface
# and is the standard pytest pattern (mirrors ``_HAS_KUBERNETES`` in
# test_kubernetes_executor.py). Issue #623.
try:
    import redis.asyncio  # noqa: F401

    _HAS_REDIS = True
except ImportError:
    _HAS_REDIS = False


@pytest.fixture(autouse=True)
def _no_real_sync_redis() -> Any:
    """Keep every test in this module off a real sync Redis connection.

    Since issue #993 the ``store``/``lookup``/``invalidate_*`` paths also
    talk to a *sync* Redis client (the shared entry store). Without this
    fixture, tests that do not patch ``_get_redis_sync`` would attempt a
    real connection to localhost:6379. A failing client factory keeps
    them hermetic: the shared-store helpers catch the error, log a
    warning, and degrade to local-only — which is exactly the behaviour
    under test in the pre-#993 assertions.
    """
    failing_module = MagicMock()
    failing_module.from_url.side_effect = ConnectionError("no real Redis in unit tests")
    with patch("osimflow.distributed_cache._get_redis_sync", return_value=failing_module):
        yield


class TestBuildCache:
    def test_returns_sqlite_cache_when_redis_url_is_none(self, tmp_path: Path) -> None:
        cache = build_cache(
            db_path=tmp_path / "cache.sqlite",
            redis_url=None,
            campaign_id="test-campaign",
        )
        assert isinstance(cache, SQLiteCache)
        assert not isinstance(cache, DistributedCache)

    def test_returns_distributed_cache_when_redis_url_set(self, tmp_path: Path) -> None:
        mock_ra = AsyncMock()
        with patch("osimflow.distributed_cache._get_redis_asyncio", return_value=mock_ra):
            cache = build_cache(
                db_path=tmp_path / "cache.sqlite",
                redis_url="redis://localhost:6379/0",
                campaign_id="test-campaign",
            )
        assert isinstance(cache, DistributedCache)

    def test_build_cache_passes_redis_ssl_context_to_distributed_cache(
        self, tmp_path: Path
    ) -> None:
        import ssl

        ctx = ssl.create_default_context()
        mock_rs = MagicMock()
        mock_rs.from_url.return_value = MagicMock()
        mock_ra = AsyncMock()
        with patch("osimflow.distributed_cache._get_redis_sync", return_value=mock_rs):
            with patch("osimflow.distributed_cache._get_redis_asyncio", return_value=mock_ra):
                cache = build_cache(
                    db_path=tmp_path / "cache.sqlite",
                    redis_url="redis://localhost:6379/0",
                    campaign_id="test-campaign",
                    redis_ssl_context=ctx,
                )
        assert cache._redis_ssl_context is ctx

    def test_distributed_cache_stores_redis_ssl_context(self, tmp_path: Path) -> None:
        import ssl

        ctx = ssl.create_default_context()
        mock_rs = MagicMock()
        mock_rs.from_url.return_value = MagicMock()
        mock_ra = AsyncMock()
        with patch("osimflow.distributed_cache._get_redis_sync", return_value=mock_rs):
            with patch("osimflow.distributed_cache._get_redis_asyncio", return_value=mock_ra):
                cache = DistributedCache(
                    db_path=tmp_path / "dist.sqlite",
                    redis_url="redis://localhost:6379/0",
                    campaign_id="test-campaign",
                    redis_ssl_context=ctx,
                )
        assert cache._redis_ssl_context is ctx

    def test_distributed_cache_sync_client_receives_ssl_context(self, tmp_path: Path) -> None:
        import ssl

        ctx = ssl.create_default_context()
        mock_rs = MagicMock()
        mock_client = MagicMock()
        mock_rs.from_url.return_value = mock_client
        mock_ra = AsyncMock()
        with patch("osimflow.distributed_cache._get_redis_sync", return_value=mock_rs):
            with patch("osimflow.distributed_cache._get_redis_asyncio", return_value=mock_ra):
                cache = DistributedCache(
                    db_path=tmp_path / "dist.sqlite",
                    redis_url="redis://localhost:6379/0",
                    campaign_id="test-campaign",
                    redis_ssl_context=ctx,
                )
                cache._get_sync_client()
        mock_rs.from_url.assert_called_once()
        call_kwargs = mock_rs.from_url.call_args.kwargs
        assert call_kwargs.get("ssl") is ctx

    def test_sqlite_cache_has_correct_db_path(self, tmp_path: Path) -> None:
        db_path = tmp_path / "subdir" / "cache.sqlite"
        cache = build_cache(
            db_path=db_path,
            redis_url=None,
            campaign_id="test-campaign",
        )
        assert cache.db_path == db_path


class TestDistributedCacheInterface:
    """Verify DistributedCache is a drop-in replacement for SQLiteCache."""

    @pytest.fixture
    def local_cache(self, tmp_path: Path) -> SQLiteCache:
        return SQLiteCache(tmp_path / "local.sqlite")

    @pytest.fixture
    def dist_cache(self, tmp_path: Path) -> DistributedCache:
        mock_ra = AsyncMock()
        with patch("osimflow.distributed_cache._get_redis_asyncio", return_value=mock_ra):
            return DistributedCache(
                db_path=tmp_path / "dist.sqlite",
                redis_url="redis://localhost:6379/0",
                campaign_id="test-campaign-123",
            )

    def _key(self, **overrides: Any) -> CacheKey:
        defaults: dict[str, Any] = {
            "step": "GENERATE_LHS_SAMPLES",
            "sample_id": "ALL",
            "openstudio_version": "N/A",
            "inputs_sha256": "abc",
            "code_sha256": "def",
            "container_digest": "py",
            "generation": 0,
        }
        defaults.update(overrides)
        return CacheKey(**defaults)

    def test_lookup_miss(self, dist_cache: DistributedCache) -> None:
        key = self._key()
        assert dist_cache.lookup(key) is None

    def test_store_and_lookup_hit(self, dist_cache: DistributedCache, tmp_path: Path) -> None:
        out = tmp_path / "output"
        out.mkdir()
        key = self._key()
        dist_cache.store(key, out, exit_code=0)
        assert dist_cache.lookup(key) == out

    def test_failed_exit_code_is_miss(self, dist_cache: DistributedCache, tmp_path: Path) -> None:
        out = tmp_path / "output"
        out.mkdir()
        key = self._key()
        dist_cache.store(key, out, exit_code=1)
        assert dist_cache.lookup(key) is None

    def test_stats_empty(self, dist_cache: DistributedCache) -> None:
        stats = dist_cache.stats()
        assert stats["total"] == 0
        assert stats["by_step"] == {}

    def test_stats_with_entries(self, dist_cache: DistributedCache, tmp_path: Path) -> None:
        out = tmp_path / "out"
        out.mkdir()
        dist_cache.store(self._key(step="STEP_A"), out, exit_code=0)
        dist_cache.store(self._key(step="STEP_B"), out, exit_code=0)
        stats = dist_cache.stats()
        assert stats["total"] == 2

    def test_store_replaces(self, dist_cache: DistributedCache, tmp_path: Path) -> None:
        out1 = tmp_path / "out1"
        out2 = tmp_path / "out2"
        out1.mkdir()
        out2.mkdir()
        key = self._key()
        dist_cache.store(key, out1, exit_code=0)
        dist_cache.store(key, out2, exit_code=0)
        assert dist_cache.lookup(key) == out2

    def test_context_manager(self, dist_cache: DistributedCache, tmp_path: Path) -> None:
        out = tmp_path / "out"
        out.mkdir()
        key = self._key()
        with dist_cache:
            dist_cache.store(key, out, exit_code=0)
        # After exiting context manager, cache should still be usable.
        assert dist_cache.lookup(key) == out


class TestDistributedCacheInvalidation:
    """Test that invalidate_* calls broadcast to Redis and affect local cache."""

    @pytest.fixture(autouse=True)
    def _isolate_subscriber(self) -> Any:
        """Prevent the real Redis subscriber thread from starting.

        ``invalidate_step`` / ``invalidate_sample`` (production code)
        unconditionally call ``_start_subscriber()`` before publishing.
        That spawns a daemon thread which imports ``redis.asyncio`` and
        runs its own ``asyncio.run`` event loop. The thread outlives the
        test, leaks its event loop across tests, and — when the redis
        extra is absent — raises ``ModuleNotFoundError`` inside the
        thread, surfaced by pytest as a ``PytestUnhandledThreadException
        Warning``. Patching ``_start_subscriber`` here keeps every test
        self-contained (issue #623). The per-test ``_publish`` patches
        below assert the broadcast contract directly.
        """
        with patch.object(DistributedCache, "_start_subscriber"):
            yield

    @pytest.fixture
    def dist_cache(self, tmp_path: Path) -> DistributedCache:
        mock_ra = AsyncMock()
        with patch("osimflow.distributed_cache._get_redis_asyncio", return_value=mock_ra):
            return DistributedCache(
                db_path=tmp_path / "dist.sqlite",
                redis_url="redis://localhost:6379/0",
                campaign_id="test-campaign-123",
            )

    def _key(self, **overrides: Any) -> CacheKey:
        defaults: dict[str, Any] = {
            "step": "GENERATE_LHS_SAMPLES",
            "sample_id": "ALL",
            "openstudio_version": "N/A",
            "inputs_sha256": "abc",
            "code_sha256": "def",
            "container_digest": "py",
            "generation": 0,
        }
        defaults.update(overrides)
        return CacheKey(**defaults)

    def test_invalidate_step_removes_entries(
        self, dist_cache: DistributedCache, tmp_path: Path
    ) -> None:
        out = tmp_path / "out"
        out.mkdir()
        dist_cache.store(self._key(step="STEP_A", sample_id="s1"), out, exit_code=0)
        dist_cache.store(self._key(step="STEP_A", sample_id="s2"), out, exit_code=0)
        dist_cache.store(self._key(step="STEP_B", sample_id="s1"), out, exit_code=0)
        assert dist_cache.stats()["total"] == 3

        with patch.object(dist_cache, "_publish") as mock_publish:
            n = dist_cache.invalidate_step("STEP_A")

        assert n == 2
        assert dist_cache.stats()["total"] == 1
        mock_publish.assert_called_once()
        call_args = mock_publish.call_args[0][0]
        assert call_args["action"] == "invalidate_step"
        assert call_args["step"] == "STEP_A"

    def test_invalidate_sample_removes_single_entry(
        self, dist_cache: DistributedCache, tmp_path: Path
    ) -> None:
        out = tmp_path / "out"
        out.mkdir()
        dist_cache.store(self._key(step="STEP_A", sample_id="s1"), out, exit_code=0)
        dist_cache.store(self._key(step="STEP_A", sample_id="s2"), out, exit_code=0)
        assert dist_cache.stats()["total"] == 2

        with patch.object(dist_cache, "_publish") as mock_publish:
            n = dist_cache.invalidate_sample("STEP_A", "s1")

        assert n == 1
        assert dist_cache.stats()["total"] == 1
        mock_publish.assert_called_once()
        call_args = mock_publish.call_args[0][0]
        assert call_args["action"] == "invalidate_sample"
        assert call_args["step"] == "STEP_A"
        assert call_args["sample_id"] == "s1"

    def test_invalidate_step_returns_zero_when_empty(self, dist_cache: DistributedCache) -> None:
        with patch.object(dist_cache, "_publish"):
            n = dist_cache.invalidate_step("NONEXISTENT")
        assert n == 0

    def test_invalidate_sample_returns_zero_when_empty(self, dist_cache: DistributedCache) -> None:
        with patch.object(dist_cache, "_publish"):
            n = dist_cache.invalidate_sample("NONEXISTENT", "s1")
        assert n == 0


class TestDistributedCacheSubscriberLifecycle:
    """Test subscriber thread start/stop lifecycle."""

    @pytest.fixture
    def dist_cache(self, tmp_path: Path) -> DistributedCache:
        mock_ra = AsyncMock()
        with patch("osimflow.distributed_cache._get_redis_asyncio", return_value=mock_ra):
            return DistributedCache(
                db_path=tmp_path / "dist.sqlite",
                redis_url="redis://localhost:6379/0",
                campaign_id="test-campaign-123",
            )

    def test_subscriber_starts_on_first_invalidate(self, dist_cache: DistributedCache) -> None:
        with patch.object(dist_cache, "_start_subscriber") as mock_start:
            with patch.object(dist_cache, "_publish"):
                dist_cache.invalidate_step("STEP_A")
        mock_start.assert_called_once()

    def test_subscriber_idempotent_start(self, dist_cache: DistributedCache) -> None:
        # Mock _start_subscriber to also set _subscriber_thread (idempotency guard).
        mock_start = MagicMock()
        dist_cache._subscriber_thread = None  # reset for test

        def start_and_set() -> None:
            dist_cache._subscriber_thread = MagicMock()

        mock_start.side_effect = start_and_set

        with patch.object(dist_cache, "_start_subscriber", mock_start):
            with patch.object(dist_cache, "_publish"):
                dist_cache.invalidate_step("STEP_A")
                dist_cache.invalidate_step("STEP_B")
        # Called exactly once (idempotent).
        assert mock_start.call_count == 1

    def test_close_stops_subscriber(self, dist_cache: DistributedCache) -> None:
        mock_thread = MagicMock()
        dist_cache._subscriber_thread = mock_thread
        dist_cache._stop_subscriber = MagicMock()

        dist_cache.close()
        dist_cache._stop_subscriber.set.assert_called_once()
        assert dist_cache._subscriber_thread is None


class TestDistributedCachePublish:
    """Test Redis publish behaviour."""

    @pytest.fixture(autouse=True)
    def _eager_publish_thread(self) -> Any:
        """Run ``_publish``'s background thread eagerly and join it.

        In a sync context ``DistributedCache._publish`` spawns a daemon
        thread that calls ``asyncio.run(_pub())``. If that thread has not
        been scheduled by the time the test asserts on the mock, the
        assertions race the thread and flap; the daemon also leaks its
        event loop across tests. Replacing ``threading.Thread`` with a
        subclass that ``start()``s then ``join()``s forces the publish
        coroutine to complete (and its loop to close) inside the test,
        so every test owns a self-contained event loop and the mock
        assertions are deterministic (issue #623).
        """
        real_thread = threading.Thread

        class _JoinedThread(real_thread):
            def start(self) -> None:
                super().start()
                self.join(timeout=5.0)

        with patch("osimflow.distributed_cache.threading.Thread", _JoinedThread):
            yield

    @pytest.fixture
    def dist_cache(self, tmp_path: Path) -> DistributedCache:
        mock_ra = AsyncMock()
        with patch("osimflow.distributed_cache._get_redis_asyncio", return_value=mock_ra):
            return DistributedCache(
                db_path=tmp_path / "dist.sqlite",
                redis_url="redis://localhost:6379/0",
                campaign_id="test-campaign-123",
            )

    @pytest.mark.skipif(not _HAS_REDIS, reason="redis extra not installed")
    def test_publish_invalidate_step(self, dist_cache: DistributedCache) -> None:
        # Patch _redis_client directly so the asyncio.run() context sees the mock
        mock_client = AsyncMock()
        with patch.object(dist_cache, "_redis_client", mock_client):
            dist_cache._publish({"action": "invalidate_step", "step": "RUN_OPENSTUDIO_SIM"})

            mock_client.publish.assert_called_once()
            channel, message = mock_client.publish.call_args[0]
            assert channel == "osimflow:cache:invalidate:test-campaign-123"
            payload = json.loads(message)
            assert payload["action"] == "invalidate_step"
            assert payload["step"] == "RUN_OPENSTUDIO_SIM"

    @pytest.mark.skipif(not _HAS_REDIS, reason="redis extra not installed")
    def test_publish_invalidate_sample(self, dist_cache: DistributedCache) -> None:
        mock_client = AsyncMock()
        with patch.object(dist_cache, "_redis_client", mock_client):
            dist_cache._publish(
                {
                    "action": "invalidate_sample",
                    "step": "APPLY_PARAMETERS",
                    "sample_id": "s0001",
                }
            )

            mock_client.publish.assert_called_once()
            channel, message = mock_client.publish.call_args[0]
            payload = json.loads(message)
            assert payload["action"] == "invalidate_sample"
            assert payload["step"] == "APPLY_PARAMETERS"
            assert payload["sample_id"] == "s0001"


class TestDistributedCacheHandleInvalidation:
    """Test _handle_invalidation processing of received messages."""

    @pytest.fixture
    def dist_cache(self, tmp_path: Path) -> DistributedCache:
        mock_ra = AsyncMock()
        with patch("osimflow.distributed_cache._get_redis_asyncio", return_value=mock_ra):
            return DistributedCache(
                db_path=tmp_path / "dist.sqlite",
                redis_url="redis://localhost:6379/0",
                campaign_id="test-campaign-123",
            )

    def _key(self, **overrides: Any) -> CacheKey:
        defaults: dict[str, Any] = {
            "step": "STEP_A",
            "sample_id": "s1",
            "openstudio_version": "N/A",
            "inputs_sha256": "abc",
            "code_sha256": "def",
            "container_digest": "py",
            "generation": 0,
        }
        defaults.update(overrides)
        return CacheKey(**defaults)

    def test_handle_invalidate_step(self, dist_cache: DistributedCache, tmp_path: Path) -> None:
        out = tmp_path / "out"
        out.mkdir()
        dist_cache.store(self._key(step="STEP_A", sample_id="s1"), out, exit_code=0)
        dist_cache.store(self._key(step="STEP_A", sample_id="s2"), out, exit_code=0)
        dist_cache.store(self._key(step="STEP_B", sample_id="s1"), out, exit_code=0)
        assert dist_cache.stats()["total"] == 3

        dist_cache._handle_invalidation({"action": "invalidate_step", "step": "STEP_A"})
        assert dist_cache.stats()["total"] == 1
        assert dist_cache.stats()["by_step"].get("STEP_B", 0) == 1

    def test_handle_invalidate_sample(self, dist_cache: DistributedCache, tmp_path: Path) -> None:
        out = tmp_path / "out"
        out.mkdir()
        dist_cache.store(self._key(step="STEP_A", sample_id="s1"), out, exit_code=0)
        dist_cache.store(self._key(step="STEP_A", sample_id="s2"), out, exit_code=0)
        assert dist_cache.stats()["total"] == 2

        dist_cache._handle_invalidation(
            {
                "action": "invalidate_sample",
                "step": "STEP_A",
                "sample_id": "s1",
            }
        )
        assert dist_cache.stats()["total"] == 1

    def test_handle_unknown_action_is_skipped(self, dist_cache: DistributedCache) -> None:
        # Should not raise.
        dist_cache._handle_invalidation({"action": "unknown_action"})
        dist_cache._handle_invalidation({})


class TestDistributedCacheBreaker:
    """Circuit-breaker integration over the shared Redis paths (issue #1451).

    Mirrors ``test_document_store.py::TestRedisDocumentStoreErrorPaths``:
    ``DistributedCache`` wires a ``CircuitBreaker`` into the shared-store
    read/write paths (``_shared_lookup`` / ``_shared_store`` /
    ``_shared_invalidate``), and a refactor that bypasses it must fail
    these tests.

    The cooldown clock is controlled by monkeypatching
    ``osimflow.circuit_breaker.time.monotonic`` so the open → half_open
    transition happens deterministically without any real ``time.sleep``
    (the sibling ``test_circuit_breaker.py`` sleeps; here the clock is
    injected instead).
    """

    @pytest.fixture
    def dist_cache(self, tmp_path: Path) -> DistributedCache:
        mock_ra = AsyncMock()
        with patch("osimflow.distributed_cache._get_redis_asyncio", return_value=mock_ra):
            return DistributedCache(
                db_path=tmp_path / "dist_breaker.sqlite",
                redis_url="redis://localhost:6379/0",
                campaign_id="test-breaker",
            )

    @pytest.fixture
    def clock(self, monkeypatch: pytest.MonkeyPatch) -> list[float]:
        """Controllable ``time.monotonic`` for the breaker's cooldown math."""
        now = [1000.0]
        monkeypatch.setattr("osimflow.circuit_breaker.time.monotonic", lambda: now[0])
        return now

    def _key(self, **overrides: Any) -> CacheKey:
        defaults: dict[str, Any] = {
            "step": "STEP_A",
            "sample_id": "s1",
            "openstudio_version": "N/A",
            "inputs_sha256": "abc",
            "code_sha256": "def",
            "container_digest": "py",
            "generation": 0,
        }
        defaults.update(overrides)
        return CacheKey(**defaults)

    @staticmethod
    def _failing_module() -> MagicMock:
        """Sync-redis module whose ``from_url`` simulates a Redis outage."""
        module = MagicMock()
        module.from_url.side_effect = ConnectionError("no redis in tests")
        return module

    # ------------------------------------------------------------------
    # (a) repeated failures open the breaker; subsequent calls fail fast
    # ------------------------------------------------------------------
    def test_repeated_write_failures_open_breaker_and_fail_fast(
        self, dist_cache: DistributedCache, tmp_path: Path
    ) -> None:
        dist_cache._breaker = CircuitBreaker(
            name="cache:fail-fast", failure_threshold=2, cooldown_s=60.0
        )
        failing = self._failing_module()
        out = tmp_path / "out"
        out.mkdir()

        with patch("osimflow.distributed_cache._get_redis_sync", return_value=failing):
            # Each of the first two writes attempts (and fails) a Redis call.
            dist_cache.store(self._key(), out, exit_code=0)
            dist_cache.store(self._key(), out, exit_code=0)
            assert dist_cache._breaker.state == "open"

            # Circuit open: writes/lookups skip the shared data plane
            # entirely — no further client-construction attempts.
            for i in range(5):
                dist_cache.store(self._key(sample_id=f"late{i}"), out, exit_code=0)
                assert dist_cache.lookup(self._key(sample_id=f"late{i}")) == out
            assert failing.from_url.call_count == 2

        # The local layer kept working while the circuit was open
        # (1 original key + 5 late keys, each a local hit above).
        assert dist_cache.stats()["total"] == 6

    def test_read_and_invalidate_fail_fast_when_circuit_open(
        self, dist_cache: DistributedCache
    ) -> None:
        dist_cache._breaker = CircuitBreaker(
            name="cache:fail-fast-ro", failure_threshold=2, cooldown_s=60.0
        )
        failing = self._failing_module()

        with (
            patch("osimflow.distributed_cache._get_redis_sync", return_value=failing),
            patch.object(dist_cache, "_start_subscriber"),
            patch.object(dist_cache, "_publish"),
        ):
            for _ in range(2):
                assert dist_cache.lookup(self._key()) is None
            assert dist_cache._breaker.state == "open"
            assert failing.from_url.call_count == 2

            # Read path: misses stay misses with no Redis contact.
            for _ in range(5):
                assert dist_cache.lookup(self._key(sample_id="miss")) is None
            # Invalidate path: the shared HDEL is skipped too.
            assert dist_cache.invalidate_step("STEP_A") == 0
            assert failing.from_url.call_count == 2

    # ------------------------------------------------------------------
    # (b) a success in half-open closes the circuit
    # ------------------------------------------------------------------
    def test_half_open_success_closes_breaker(
        self,
        dist_cache: DistributedCache,
        tmp_path: Path,
        clock: list[float],
    ) -> None:
        dist_cache._breaker = CircuitBreaker(
            name="cache:half-open-ok", failure_threshold=2, cooldown_s=30.0
        )
        failing = self._failing_module()
        out = tmp_path / "out"
        out.mkdir()

        with patch("osimflow.distributed_cache._get_redis_sync", return_value=failing):
            for _ in range(2):
                assert dist_cache.lookup(self._key()) is None
            assert dist_cache._breaker.state == "open"
            assert dist_cache._breaker.consecutive_failures == 2

        # Advance past the cooldown — no sleep — then let the probe read
        # a real shared entry through a working client.
        clock[0] += 31.0
        assert dist_cache._breaker.state == "half_open"

        working_client = MagicMock()
        working_client.hget.return_value = json.dumps(
            {"output_path": str(out), "exit_code": 0, "finished_at": 0.0}
        )
        working = MagicMock()
        working.from_url.return_value = working_client
        with patch("osimflow.distributed_cache._get_redis_sync", return_value=working):
            assert dist_cache.lookup(self._key()) == out  # probe: shared hit
            assert dist_cache._breaker.state == "closed"
            assert dist_cache._breaker.consecutive_failures == 0

            # Circuit closed: normal shared ops resume on the same client.
            dist_cache.store(self._key(sample_id="s2"), out, exit_code=0)
            assert dist_cache._breaker.state == "closed"
            assert working.from_url.call_count == 1  # client was cached

    # ------------------------------------------------------------------
    # (c) d1056b8: a failed half-open probe re-opens with counter at 0
    # ------------------------------------------------------------------
    def test_half_open_failure_reopens_and_resets_counter(
        self,
        dist_cache: DistributedCache,
        clock: list[float],
    ) -> None:
        """Failed half_open probe → open with ``_consecutive_failures`` == 0.

        Regression guard for d1056b8 (issue #1330/#1379): the counter is
        preserved across open → half_open, then reset to 0 — not 1 — when
        the failed probe re-opens the circuit.
        """
        dist_cache._breaker = CircuitBreaker(
            name="cache:half-open-fail", failure_threshold=2, cooldown_s=30.0
        )
        failing = self._failing_module()

        with patch("osimflow.distributed_cache._get_redis_sync", return_value=failing):
            for _ in range(2):
                assert dist_cache.lookup(self._key()) is None
            assert dist_cache._breaker.state == "open"
            assert dist_cache._breaker.consecutive_failures == 2

            clock[0] += 31.0
            assert dist_cache._breaker.state == "half_open"
            # Counter preserved across open → half_open (no premature reset).
            assert dist_cache._breaker.consecutive_failures == 2

            # The single half-open probe goes through and fails.
            assert dist_cache.lookup(self._key()) is None
            assert dist_cache._breaker.state == "open"  # fresh cooldown re-armed
            # d1056b8: reset to 0, not 1.
            assert dist_cache._breaker.consecutive_failures == 0

            # Still inside the re-armed cooldown: fail fast, no Redis contact.
            calls_before = failing.from_url.call_count
            clock[0] += 1.0
            assert dist_cache.lookup(self._key()) is None
            assert failing.from_url.call_count == calls_before


class TestDistributedCacheAutoRecovery:
    """Test auto-recovery of the Redis subscriber thread (issue #443)."""

    @pytest.fixture
    def dist_cache(self, tmp_path: Path) -> DistributedCache:
        mock_ra = AsyncMock()
        with patch("osimflow.distributed_cache._get_redis_asyncio", return_value=mock_ra):
            return DistributedCache(
                db_path=tmp_path / "dist_recovery.sqlite",
                redis_url="redis://localhost:6379/0",
                campaign_id="test-auto-recovery",
            )

    def test_subscriber_reconnect_delay_doubles_on_error(
        self, dist_cache: DistributedCache, tmp_path: Path
    ) -> None:
        """Reconnect delay doubles after each error, capped at 60s."""

        async def mock_get_message(timeout: float = 1.0, ignore_subscribe_messages: bool = True):
            raise ConnectionError("Redis connection lost")

        mock_pubsub = AsyncMock()
        mock_pubsub.get_message = mock_get_message
        mock_pubsub.subscribe = AsyncMock()

        mock_client_instance = AsyncMock()
        mock_client_instance.pubsub = MagicMock(return_value=mock_pubsub)
        mock_pubsub.__aenter__ = AsyncMock(return_value=mock_pubsub)
        mock_pubsub.__aexit__ = AsyncMock(return_value=None)
        mock_client_instance.aclose = AsyncMock()

        sleep_delays: list[float] = []

        async def mock_sleep(delay: float) -> None:
            sleep_delays.append(delay)

        mock_ra = MagicMock()
        mock_ra.from_url.return_value = mock_client_instance

        with patch("osimflow.distributed_cache._get_redis_asyncio", return_value=mock_ra):
            with patch("asyncio.sleep", mock_sleep):
                dist_cache.invalidate_step("STEP_A")
                import time

                time.sleep(0.3)
                dist_cache.close()

        assert len(sleep_delays) >= 2
        assert sleep_delays[0] <= 2.0
        assert sleep_delays[1] <= 4.0
        assert sleep_delays[1] >= sleep_delays[0]


class TestDistributedCacheConcurrent:
    """Test concurrent access to DistributedCache."""

    @pytest.fixture
    def dist_cache(self, tmp_path: Path) -> DistributedCache:
        # No Redis mocking needed: this test only exercises local SQLite operations.
        # _get_redis_asyncio is only called when _publish is called, which doesn't
        # happen in this test.
        return DistributedCache(
            db_path=tmp_path / "dist_concurrent.sqlite",
            redis_url="redis://localhost:6379/0",
            campaign_id="test-concurrent",
        )

    def _key(self, step: str, sample_id: str) -> CacheKey:
        return CacheKey(
            step=step,
            sample_id=sample_id,
            openstudio_version="N/A",
            inputs_sha256="abc",
            code_sha256="def",
            container_digest="py",
            generation=0,
        )

    def test_concurrent_store_and_lookup(
        self, dist_cache: DistributedCache, tmp_path: Path
    ) -> None:
        num_threads = 10
        num_operations = 20
        errors: list[Exception] = []
        lock = threading.Lock()

        def worker(thread_id: int) -> None:
            try:
                for op_id in range(num_operations):
                    key = self._key(
                        step=f"STEP_{thread_id % 3}",
                        sample_id=f"s{thread_id}_{op_id}",
                    )
                    out = tmp_path / f"out_{thread_id}_{op_id}"
                    out.mkdir(exist_ok=True)
                    dist_cache.store(key, out, exit_code=0)
                    result = dist_cache.lookup(key)
                    assert result == out
            except Exception as e:
                with lock:
                    errors.append(e)

        with ThreadPoolExecutor(max_workers=num_threads) as executor:
            futures = [executor.submit(worker, i) for i in range(num_threads)]
            for f in as_completed(futures):
                f.result()

        assert not errors, f"Concurrent access errors: {errors}"


class TestValidateRedisUrl:
    """Regression tests for _validate_redis_url (issue #1321)."""

    def test_localhost_passthrough(self) -> None:
        from osimflow.distributed_cache import _validate_redis_url

        _validate_redis_url("redis://localhost:6379")
        _validate_redis_url("redis://127.0.0.1:6379")
        _validate_redis_url("redis://[::1]:6379")
        _validate_redis_url("redis://0.0.0.0:6379")
        _validate_redis_url("rediss://localhost:6379")
        _validate_redis_url("redis://localhost:6379", require_auth=True)

    def test_nonlocalhost_rediss_with_creds_ok(self) -> None:
        from osimflow.distributed_cache import _validate_redis_url

        _validate_redis_url("rediss://user:pass@redis.example.com:6379")
        _validate_redis_url("rediss://user:pass@redis.example.com:6379", require_auth=True)

    def test_nonlocalhost_rediss_no_creds_require_auth_ok(self) -> None:
        from osimflow.distributed_cache import _validate_redis_url

        _validate_redis_url("rediss://redis.example.com:6379", require_auth=True)

    def test_nonlocalhost_rediss_no_creds_no_require_auth_fails(self) -> None:
        from osimflow.distributed_cache import _validate_redis_url

        with pytest.raises(ValueError, match="issue #1277"):
            _validate_redis_url("rediss://redis.example.com:6379")

    def test_nonlocalhost_redis_no_tls_fails_even_with_require_auth(self) -> None:
        from osimflow.distributed_cache import _validate_redis_url

        with pytest.raises(ValueError, match="issue #1321"):
            _validate_redis_url("redis://redis.example.com:6379", require_auth=True)

    def test_nonlocalhost_redis_no_tls_no_creds_fails(self) -> None:
        from osimflow.distributed_cache import _validate_redis_url

        with pytest.raises(ValueError, match="issue #1321"):
            _validate_redis_url("redis://redis.example.com:6379")

    def test_nonlocalhost_redis_no_tls_with_creds_fails(self) -> None:
        from osimflow.distributed_cache import _validate_redis_url

        with pytest.raises(ValueError, match="issue #1321"):
            _validate_redis_url("redis://user:pass@redis.example.com:6379")
