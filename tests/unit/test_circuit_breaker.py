"""Unit tests for the Redis circuit breaker (issue #1111)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from osimflow.circuit_breaker import CircuitBreaker, CircuitOpenError
from osimflow.distributed_cache import DistributedCache
from osimflow.document_store import DocumentStoreError, RedisDocumentStore


class TestCircuitBreakerStates:
    def test_closed_allows_requests(self) -> None:
        breaker = CircuitBreaker()
        assert breaker.state == "closed"
        assert breaker.allow() is True

    def test_opens_after_threshold_consecutive_failures(self) -> None:
        breaker = CircuitBreaker(failure_threshold=3)
        for _ in range(2):
            breaker.record_failure()
        assert breaker.allow() is True  # still closed below threshold
        assert breaker.state == "closed"
        breaker.record_failure()
        assert breaker.state == "open"
        assert breaker.allow() is False

    def test_success_resets_failure_count(self) -> None:
        breaker = CircuitBreaker(failure_threshold=3)
        breaker.record_failure()
        breaker.record_failure()
        breaker.record_success()
        assert breaker.consecutive_failures == 0
        assert breaker.state == "closed"

    def test_cooldown_elapses_into_half_open_probe(self) -> None:
        breaker = CircuitBreaker(failure_threshold=1, cooldown_s=0.05)
        breaker.record_failure()
        assert breaker.allow() is False
        import time

        time.sleep(0.06)
        assert breaker.state == "half_open"
        assert breaker.allow() is True

    def test_half_open_success_closes_circuit(self) -> None:
        breaker = CircuitBreaker(failure_threshold=1, cooldown_s=0.01)
        breaker.record_failure()
        import time

        time.sleep(0.02)
        assert breaker.allow() is True  # probe admitted
        breaker.record_success()
        assert breaker.state == "closed"

    def test_half_open_failure_reopens_circuit(self) -> None:
        breaker = CircuitBreaker(failure_threshold=5, cooldown_s=0.01)
        for _ in range(5):
            breaker.record_failure()
        import time

        time.sleep(0.02)
        assert breaker.allow() is True  # probe admitted
        breaker.record_failure()  # probe fails -> straight back to open
        assert breaker.state == "open"

    def test_half_open_failure_resets_counter_to_1(self) -> None:
        """Failure in half_open should reset _consecutive_failures to 1 (issue #1188)."""
        import time

        breaker = CircuitBreaker(failure_threshold=3, cooldown_s=0.01)
        for _ in range(3):
            breaker.record_failure()
        assert breaker.consecutive_failures == 3
        assert breaker.state == "open"

        time.sleep(0.02)
        assert breaker.allow() is True  # half-open probe admitted
        assert breaker.consecutive_failures == 3  # not yet incremented

        breaker.record_failure()  # probe fails -> re-open, counter reset to 1
        assert breaker.state == "open"
        assert breaker.consecutive_failures == 1  # reset to 1, not 4

        # Second half-open failure should also have counter=1 before the failure
        time.sleep(0.02)
        breaker.record_failure()
        assert breaker.consecutive_failures == 2

    def test_check_raises_when_open(self) -> None:
        breaker = CircuitBreaker(failure_threshold=1)
        breaker.record_failure()
        with pytest.raises(CircuitOpenError, match="is open"):
            breaker.check()


def _failing_sync_module() -> MagicMock:
    module = MagicMock()
    module.from_url.side_effect = ConnectionError("no redis in tests")
    return module


class TestDistributedCacheBreakerIntegration:
    def test_persistent_outage_fail_fast_skips_client_calls(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """After the failure threshold, the sync client factory stops being called."""
        cache = DistributedCache(
            db_path=tmp_path / "cache.sqlite",
            redis_url="redis://localhost:6379/0",
            campaign_id="cb-test",
        )
        failing = _failing_sync_module()
        with (
            patch("osimflow.distributed_cache._get_redis_sync", return_value=failing),
            caplog.at_level("WARNING"),
        ):
            # Default threshold is 5: each of the first 5 lookups attempts
            # a client construction and logs a degradation warning.
            for _ in range(5):
                assert cache.lookup(_make_key("s")) is None
            warnings_so_far = sum(
                1 for r in caplog.records if "continuing local-only" in r.getMessage()
            )
            assert warnings_so_far == 5

            # Circuit now open: further ops fail fast — no new client
            # construction attempts, no new warnings.
            for _ in range(20):
                assert cache.lookup(_make_key("x")) is None
            warnings_after = sum(
                1 for r in caplog.records if "continuing local-only" in r.getMessage()
            )
            assert warnings_after == 5, "circuit must suppress further Redis attempts"

    def test_recovery_after_cooldown_retries_redis(self, tmp_path: Path) -> None:
        cache = DistributedCache(
            db_path=tmp_path / "cache.sqlite",
            redis_url="redis://localhost:6379/0",
            campaign_id="cb-recover",
        )
        cache._breaker = CircuitBreaker(name="test", failure_threshold=2, cooldown_s=0.01)
        failing = _failing_sync_module()
        with patch("osimflow.distributed_cache._get_redis_sync", return_value=failing):
            key = _make_key("x")
            for _ in range(2):
                assert cache.lookup(key) is None
            assert cache._breaker.state == "open"
            import time

            time.sleep(0.02)
            # Half-open probe goes through (and fails again), re-opening.
            assert cache.lookup(key) is None
            assert cache._breaker.state == "open"


def _make_key(sample_id: str):
    from osimflow.cache import CacheKey

    return CacheKey(
        step="STEP_A",
        sample_id=sample_id,
        openstudio_version="3.11.0",
        inputs_sha256="0" * 64,
        code_sha256="0" * 64,
        container_digest="sha256:" + "0" * 64,
    )


class TestRedisDocumentStoreBreakerIntegration:
    def test_open_circuit_fails_fast_with_document_store_error(self, tmp_path: Path) -> None:
        store = RedisDocumentStore(
            redis_url="redis://localhost:6379/0", namespace="ns-cb", db_path=tmp_path
        )
        store._breaker = CircuitBreaker(name="docs:test", failure_threshold=2, cooldown_s=60.0)
        failing = _failing_sync_module()
        with patch("osimflow.document_store._get_redis_sync", return_value=failing):
            # First two failures propagate raw (ConnectionError) and count.
            with pytest.raises(ConnectionError):
                store.find_one("kpis", {"_id": "doc_1"})
            with pytest.raises(ConnectionError):
                store.find_one("kpis", {"_id": "doc_1"})
            assert store._breaker.state == "open"
            # Third attempt fails fast with DocumentStoreError, no socket wait.
            with pytest.raises(DocumentStoreError, match="[Cc]ircuit|unavailable"):
                store.find_one("kpis", {"_id": "doc_1"})

    def test_successful_ops_keep_circuit_closed(self, tmp_path: Path) -> None:
        store = RedisDocumentStore(
            redis_url="redis://localhost:6379/0", namespace="ns-ok", db_path=tmp_path
        )
        working = MagicMock()
        working.from_url.return_value.hget.return_value = None
        with patch("osimflow.document_store._get_redis_sync", return_value=working):
            for _ in range(10):
                assert store.find_one("kpis", {"_id": "doc_1"}) is None
            assert store._breaker.state == "closed"


class TestCircuitBreakerObservability:
    def test_on_transition_callback_fired_on_state_change(self) -> None:
        events: list[tuple[str, str, str]] = []
        breaker = CircuitBreaker(
            name="test_cb",
            failure_threshold=2,
            cooldown_s=0.001,
            on_transition=lambda n, f, t: events.append((n, f, t)),
        )
        assert breaker.state == "closed"
        # closed → open (2 consecutive failures)
        breaker.record_failure()
        breaker.record_failure()
        assert events == [("test_cb", "closed", "open")]
        # open → half_open (cooldown elapses on allow())
        import time; time.sleep(0.002)
        assert breaker.allow() is True
        assert events == [
            ("test_cb", "closed", "open"),
            ("test_cb", "open", "half_open"),
        ]
        # half_open → open (probe fails)
        breaker.record_failure()
        assert events == [
            ("test_cb", "closed", "open"),
            ("test_cb", "open", "half_open"),
            ("test_cb", "half_open", "open"),
        ]
        # open → half_open again
        import time; time.sleep(0.002)
        breaker.allow()
        # half_open → closed (probe succeeds)
        breaker.record_success()
        assert events[-1] == ("test_cb", "half_open", "closed")

    def test_no_callback_means_no_error(self) -> None:
        breaker = CircuitBreaker(name="no_cb", failure_threshold=1, cooldown_s=0.001)
        breaker.record_failure()
        assert breaker.state == "open"
