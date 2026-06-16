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
"""

import json
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from osimflow.cache import CacheKey, SQLiteCache
from osimflow.distributed_cache import DistributedCache, build_cache


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


_HAS_REDIS: bool | None = None


def _check_redis() -> bool:
    global _HAS_REDIS
    if _HAS_REDIS is None:
        try:
            import redis.asyncio  # noqa: F401

            _HAS_REDIS = True
        except Exception:
            _HAS_REDIS = False
    return _HAS_REDIS


class TestDistributedCachePublish:
    """Test Redis publish behaviour."""

    @pytest.fixture
    def dist_cache(self, tmp_path: Path) -> DistributedCache:
        mock_ra = AsyncMock()
        with patch("osimflow.distributed_cache._get_redis_asyncio", return_value=mock_ra):
            return DistributedCache(
                db_path=tmp_path / "dist.sqlite",
                redis_url="redis://localhost:6379/0",
                campaign_id="test-campaign-123",
            )

    @pytest.mark.skipif(not _check_redis(), reason="redis extra not installed")
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

    @pytest.mark.skipif(not _check_redis(), reason="redis extra not installed")
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

        async def mock_pubsub_context():
            return mock_pubsub

        mock_client_instance = AsyncMock()
        mock_client_instance.pubsub = mock_pubsub_context
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
