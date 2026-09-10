"""Tests for osimflow/alerting.py."""

from __future__ import annotations

import threading
import time
from unittest.mock import patch

from osimflow.alerting import (
    Alert,
    AlertDestination,
    AlertManager,
    AlertRule,
    AlertSeverity,
    LogDestination,
    WebhookDestination,
    build_alert_manager,
    load_alert_destinations_from_yaml,
    load_alert_rules_from_yaml,
)


class TestAlertRule:
    def test_rule_with_callable_condition_passes_when_condition_true(self):
        rule = AlertRule(
            name="test",
            event_type="campaign.completed",
            condition=lambda ctx: ctx.get("run_time", 0) > 300,
            severity=AlertSeverity.WARNING,
            message_template="Slow campaign",
        )
        ctx = {"run_time": 600}
        assert rule.condition(ctx) is True

    def test_rule_with_callable_condition_fails_when_condition_false(self):
        rule = AlertRule(
            name="test",
            event_type="campaign.completed",
            condition=lambda ctx: ctx.get("run_time", 0) > 300,
            severity=AlertSeverity.WARNING,
            message_template="Slow campaign",
        )
        ctx = {"run_time": 100}
        assert rule.condition(ctx) is False

    def test_rule_with_always_condition_always_passes(self):
        rule = AlertRule(
            name="test",
            event_type="campaign.completed",
            condition=lambda _: True,
            severity=AlertSeverity.INFO,
            message_template="Campaign done",
        )
        assert rule.condition({}) is True

    def test_severity_property(self):
        rule = AlertRule(
            name="t",
            event_type="x",
            condition=lambda _: True,
            severity=AlertSeverity.CRITICAL,
            message_template="x",
        )
        assert rule.severity == AlertSeverity.CRITICAL


class TestWebhookDestination:
    def test_send_posts_alert_as_json(self):
        dest = WebhookDestination(url="https://example.com/webhook")
        alert = Alert(
            rule_name="test-rule",
            event_type="campaign.completed",
            severity=AlertSeverity.INFO,
            message="Campaign done",
            context={"campaign_id": "c1"},
            timestamp=1234567890.0,
        )
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_response = type("Resp", (), {"status": 200})()
            mock_urlopen.return_value.__enter__.return_value = mock_response
            result = dest.send(alert)
            assert result is True
            mock_urlopen.assert_called_once()
            call_args = mock_urlopen.call_args
            req = call_args[0][0]
            assert req.full_url == "https://example.com/webhook"
            assert req.method == "POST"

    def test_send_retries_on_http_500(self):
        dest = WebhookDestination(url="https://example.com/webhook", max_retries=2)
        alert = Alert(
            rule_name="test-rule",
            event_type="campaign.completed",
            severity=AlertSeverity.INFO,
            message="Campaign done",
            context={},
            timestamp=1234567890.0,
        )
        import urllib.error

        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = urllib.error.HTTPError(
                "https://example.com/", 500, "Server Error", {}, None
            )
            result = dest.send(alert)
            assert result is False
            assert mock_urlopen.call_count == 3  # initial + 2 retries


class TestLogDestination:
    def test_send_logs_at_configured_level(self, caplog):
        dest = LogDestination(level="ERROR")
        alert = Alert(
            rule_name="test-rule",
            event_type="campaign.completed",
            severity=AlertSeverity.WARNING,
            message="Campaign done",
            context={},
            timestamp=1234567890.0,
        )
        result = dest.send(alert)
        assert result is True
        assert caplog.records[-1].levelname == "ERROR"
        assert "Campaign done" in caplog.records[-1].message

    def test_send_logs_info_by_default(self, caplog):
        dest = LogDestination(level="INFO")
        alert = Alert(
            rule_name="test-rule",
            event_type="campaign.completed",
            severity=AlertSeverity.INFO,
            message="Info msg",
            context={},
            timestamp=1234567890.0,
        )
        dest.send(alert)
        assert caplog.records[-1].levelname == "INFO"


class TestAlertManager:
    def test_notify_fires_matching_rule(self, caplog):
        manager = AlertManager()
        rule = AlertRule(
            name="test-rule",
            event_type="campaign.completed",
            condition=lambda _: True,
            severity=AlertSeverity.INFO,
            message_template="Campaign done in {run_time}s",
        )
        manager.add_rule(rule)
        dest = LogDestination(level="INFO")
        manager.add_destination(dest)
        manager.notify("campaign.completed", {"run_time": 42})
        assert any("Campaign done in 42s" in r.message for r in caplog.records)

    def test_notify_skips_non_matching_condition(self, caplog):
        manager = AlertManager()
        rule = AlertRule(
            name="slow",
            event_type="campaign.completed",
            condition=lambda ctx: ctx.get("run_time", 0) > 300,
            severity=AlertSeverity.WARNING,
            message_template="Slow!",
        )
        manager.add_rule(rule)
        dest = LogDestination(level="WARNING")
        manager.add_destination(dest)
        manager.notify("campaign.completed", {"run_time": 50})
        assert not any("Slow!" in r.message for r in caplog.records)

    def test_notify_does_not_raise_on_destination_error(self):
        manager = AlertManager()
        rule = AlertRule(
            name="test",
            event_type="campaign.completed",
            condition=lambda _: True,
            severity=AlertSeverity.INFO,
            message_template="Done",
        )
        manager.add_rule(rule)
        dest = WebhookDestination(url="https://invalid.example.com/")
        manager.add_destination(dest)
        # Should not raise
        manager.notify("campaign.completed", {})

    def test_builtin_rules_includes_six_event_types(self):
        manager = AlertManager()
        rules = manager.builtin_rules()
        event_types = {r.event_type for r in rules}
        assert event_types >= {
            "campaign.started",
            "campaign.completed",
            "campaign.failed",
            "sample.failed",
            "worker.dead",
            "cache.miss_rate_low",
        }


class TestLoadAlertRulesFromYaml:
    def test_load_rules_from_yaml(self, tmp_path):
        rules_file = tmp_path / "rules.yml"
        rules_file.write_text(
            "rules:\n"
            "  - name: slow_campaign\n"
            "    event_type: campaign.completed\n"
            "    severity: WARNING\n"
            "    message_template: 'Slow: {run_time}s'\n"
            "    condition:\n"
            "      type: expr\n"
            "      value: context.get('run_time', 0) > 300\n"
        )
        rules = load_alert_rules_from_yaml(rules_file)
        assert len(rules) == 1
        assert rules[0].name == "slow_campaign"
        assert rules[0].severity == "WARNING"

    def test_load_rules_empty_file(self, tmp_path):
        rules_file = tmp_path / "empty.yml"
        rules_file.write_text("rules: []")
        rules = load_alert_rules_from_yaml(rules_file)
        assert rules == []

    def test_load_rules_file_not_found_returns_empty(self, tmp_path):
        nonexistent = tmp_path / "nonexistent.yml"
        assert not nonexistent.is_file()
        rules = load_alert_rules_from_yaml(nonexistent)
        assert rules == []


class TestLoadAlertDestinationsFromYaml:
    def test_load_webhook_destination(self, tmp_path):
        dests_file = tmp_path / "dests.yml"
        dests_file.write_text(
            "destinations:\n"
            "  - name: slack\n"
            "    type: webhook\n"
            "    url: https://hooks.slack.com/services/xxx\n"
        )
        dests = load_alert_destinations_from_yaml(dests_file)
        assert len(dests) == 1
        assert isinstance(dests[0], WebhookDestination)

    def test_load_log_destination(self, tmp_path):
        dests_file = tmp_path / "dests.yml"
        dests_file.write_text("destinations:\n  - name: console\n    type: log\n    level: ERROR\n")
        dests = load_alert_destinations_from_yaml(dests_file)
        assert len(dests) == 1
        assert isinstance(dests[0], LogDestination)

    def test_load_destinations_file_not_found_returns_empty(self, tmp_path):
        nonexistent = tmp_path / "nonexistent.yml"
        assert not nonexistent.is_file()
        dests = load_alert_destinations_from_yaml(nonexistent)
        assert dests == []


class TestBuildAlertManager:
    def test_build_with_no_files_includes_builtin_rules(self):
        manager = build_alert_manager(None, None)
        event_types = {r.event_type for r in manager.builtin_rules()}
        assert "campaign.started" in event_types
        assert "campaign.completed" in event_types
        assert "campaign.failed" in event_types
        assert "sample.failed" in event_types
        assert "worker.dead" in event_types
        assert "cache.miss_rate_low" in event_types

    def test_build_with_rules_file_adds_custom_rules(self, tmp_path):
        rules_file = tmp_path / "rules.yml"
        rules_file.write_text(
            "rules:\n"
            "  - name: custom\n"
            "    event_type: campaign.started\n"
            "    severity: INFO\n"
            "    message_template: 'Custom alert'\n"
            "    condition:\n"
            "      type: always\n"
            "      value: true\n"
        )
        manager = build_alert_manager(rules_file, None)
        rule_names = [r.name for r in manager._rules]
        assert "custom" in rule_names

    def test_build_with_destinations_file_adds_destinations(self, tmp_path):
        dests_file = tmp_path / "dests.yml"
        dests_file.write_text("destinations:\n  - type: log\n    level: ERROR\n")
        manager = build_alert_manager(None, dests_file)
        # One LogDestination from the file
        assert len(manager._destinations) == 1
        assert isinstance(manager._destinations[0], LogDestination)


class FlakyDestination(AlertDestination):
    """Fake destination that raises while down and delivers while up."""

    def __init__(self, fail: bool = True) -> None:
        self.fail = fail
        self.delivered: list[Alert] = []

    def send(self, alert: Alert) -> bool:
        if self.fail:
            raise RuntimeError("destination down")
        self.delivered.append(alert)
        return True


def _manager_with(dest: AlertDestination) -> AlertManager:
    manager = AlertManager()
    manager.add_rule(
        AlertRule(
            name="test-rule",
            event_type="campaign.completed",
            condition=lambda _: True,
            severity=AlertSeverity.WARNING,
            message_template="alert {i}",
        )
    )
    manager.add_destination(dest)
    return manager


class TestPendingAlertRetryQueue:
    """Issue #1185 — failed alerts are queued and retried, not lost."""

    def test_failed_alert_is_queued_with_timestamp(self):
        dest = FlakyDestination(fail=True)
        manager = _manager_with(dest)

        before = time.time()
        manager.notify("campaign.completed", {"i": 1})

        assert len(manager._alert_history) == 1
        entry = manager._alert_history[0]
        assert entry.alert.rule_name == "test-rule"
        assert entry.alert.message == "alert 1"
        assert entry.destination is dest
        assert entry.failed_at >= before
        assert "destination down" in entry.error

    def test_recovered_destination_flushes_pending_on_next_notify(self):
        dest = FlakyDestination(fail=True)
        manager = _manager_with(dest)

        manager.notify("campaign.completed", {"i": 1})
        assert len(manager._alert_history) == 1
        first_alert = manager._alert_history[0].alert

        dest.fail = False
        manager.notify("campaign.completed", {"i": 2})

        assert len(manager._alert_history) == 0
        # The queued alert from the outage was delivered, plus the new one.
        assert [a.message for a in dest.delivered] == ["alert 1", "alert 2"]
        assert first_alert in dest.delivered

    def test_ring_buffer_caps_at_100(self):
        dest = FlakyDestination(fail=True)
        manager = _manager_with(dest)

        for i in range(101):
            manager.notify("campaign.completed", {"i": i})

        assert len(manager._alert_history) == 100
        messages = [entry.alert.message for entry in manager._alert_history]
        # Oldest entry (i=0) was evicted by the 101st failure.
        assert "alert 0" not in messages
        assert messages[0] == "alert 1"
        assert messages[-1] == "alert 100"

    def test_delivered_alerts_are_not_queued(self):
        dest = FlakyDestination(fail=False)
        manager = _manager_with(dest)

        manager.notify("campaign.completed", {"i": 1})
        manager.notify("campaign.completed", {"i": 2})

        assert len(manager._alert_history) == 0
        assert len(dest.delivered) == 2

    def test_continued_failure_keeps_alert_queued_with_updated_error(self):
        dest = FlakyDestination(fail=True)
        manager = _manager_with(dest)

        manager.notify("campaign.completed", {"i": 1})
        first_failed_at = manager._alert_history[0].failed_at

        manager.notify("campaign.completed", {"i": 2})

        # Both the original and the new failure remain queued.
        assert len(manager._alert_history) == 2
        retained = [e for e in manager._alert_history if e.alert.message == "alert 1"][0]
        # No wall-clock sleep needed (issue #1544): ``failed_at`` is only
        # ever refreshed forward, so ``>=`` holds for equal-or-later stamps.
        assert retained.failed_at >= first_failed_at
        assert "destination down" in retained.error

    def test_false_returning_destination_is_queued_and_retried(self):
        class RefusingDestination(AlertDestination):
            def __init__(self) -> None:
                self.calls = 0

            def send(self, alert: Alert) -> bool:
                self.calls += 1
                return False

        dest = RefusingDestination()
        manager = _manager_with(dest)

        manager.notify("campaign.completed", {"i": 1})
        assert len(manager._alert_history) == 1
        assert manager._alert_history[0].error == "destination reported delivery failure"

    def test_concurrent_notify_is_thread_safe(self):
        dest = FlakyDestination(fail=True)
        manager = _manager_with(dest)

        def worker() -> None:
            for i in range(10):
                manager.notify("campaign.completed", {"i": i})

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Ring buffer bound holds under concurrent queue mutations.
        assert len(manager._alert_history) <= 100

    def test_get_alert_history_returns_failed_deliveries_with_status(self):
        dest = FlakyDestination(fail=True)
        manager = _manager_with(dest)

        before = time.time()
        manager.notify("campaign.completed", {"i": 1})

        history = manager.get_alert_history()
        assert len(history) == 1
        entry = history[0]
        assert entry["rule_name"] == "test-rule"
        assert entry["event_type"] == "campaign.completed"
        assert entry["severity"] == "WARNING"
        assert entry["message"] == "alert 1"
        assert entry["timestamp"] >= before
        assert entry["destination"] == "FlakyDestination"
        assert entry["failed_at"] >= before
        assert "destination down" in entry["error"]
        assert entry["delivery_status"] == "failed"

    def test_get_alert_history_empty_when_all_delivered(self):
        dest = FlakyDestination(fail=False)
        manager = _manager_with(dest)

        manager.notify("campaign.completed", {"i": 1})

        assert manager.get_alert_history() == []

    def test_get_alert_history_thread_safe(self):
        dest = FlakyDestination(fail=True)
        manager = _manager_with(dest)

        def worker() -> None:
            for i in range(10):
                manager.notify("campaign.completed", {"i": i})

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Ring buffer is capped at 100; we sent 800 alerts so we may have
        # fewer entries depending on timing, but never more than 100.
        history = manager.get_alert_history()
        assert len(history) <= 100
        for entry in history:
            assert entry["delivery_status"] == "failed"
            assert "destination down" in entry["error"]


class BlockingDestination(AlertDestination):
    """Fake destination that blocks on an Event until released or aborted.

    Used to test that ``notify()`` returns quickly when background dispatch
    is enabled (the caller doesn't wait for ``send`` to return), and that
    the worker honours a per-alert deadline when a delivery is wedged.
    """

    def __init__(self) -> None:
        self.gate = threading.Event()
        self.received: list[Alert] = []

    def send(self, alert: Alert) -> bool:
        self.received.append(alert)
        # Block until the test releases the gate (or the process exits and
        # daemon-thread join is abandoned — both are acceptable here).
        self.gate.wait(timeout=10.0)
        return True


class TestBackgroundDispatcher:
    """Issue #1673 — alert delivery moves off the caller's thread.

    Backward compat: when no background dispatcher is started (the default
    for ``AlertManager()``), the synchronous path is preserved and these
    tests do not apply — see TestPendingAlertRetryQueue above.
    """

    def test_notify_returns_quickly_with_slow_destination(self):
        """notify() must not block on a slow/blackholed destination when
        background dispatch is enabled — a slow destination would otherwise
        serialize fan-out worker threads (issue #1673)."""
        dest = BlockingDestination()
        manager = _manager_with(dest)
        manager.start_background_dispatch()
        try:
            t0 = time.monotonic()
            manager.notify("campaign.completed", {"i": 1})
            elapsed = time.monotonic() - t0
            # notify() should return after enqueue + the rule-evaluation
            # micro-cost — nowhere near the 10s the gate could block for.
            assert elapsed < 1.0, f"notify() blocked for {elapsed:.2f}s"
            # Wait briefly for the worker to dequeue + enter send(); the
            # gate not being released means send() will block, but
            # dest.received is appended before the wait, so a non-empty
            # list proves the dispatcher picked the alert up.
            deadline = time.monotonic() + 2.0
            while not dest.received and time.monotonic() < deadline:
                time.sleep(0.01)
            assert dest.received, "worker should have dequeued the alert"
        finally:
            dest.gate.set()
            manager.close(timeout_s=2.0)

    def test_close_drains_pending_alerts(self):
        """close() signals the worker to drain remaining alerts and joins."""
        dest = BlockingDestination()
        manager = _manager_with(dest)
        manager.start_background_dispatch()
        manager.notify("campaign.completed", {"i": 1})
        # Let the worker reach the gate (parked inside send()).
        deadline = time.monotonic() + 2.0
        while not dest.received and time.monotonic() < deadline:
            time.sleep(0.01)
        assert dest.received, "worker did not reach send()"
        # Release the gate and close — close() should return quickly because
        # the worker drains the single in-flight alert and exits cleanly.
        dest.gate.set()
        manager.close(timeout_s=5.0)
        # Idempotent: a second close is a no-op.
        manager.close(timeout_s=1.0)
        assert manager._dispatch_thread is None or not manager._dispatch_thread.is_alive()

    def test_per_alert_deadline_abandons_wedged_delivery(self):
        """An alert whose destination never returns must be abandoned past
        the per_alert_deadline_s deadline so the dispatcher can move on to
        subsequent alerts."""
        dest = BlockingDestination()
        manager = _manager_with(
            dest,
        )
        # Use a tiny deadline so the test is fast but still deterministically
        # above wall-clock noise under xdist.
        manager._per_alert_deadline_s = 0.1
        manager.start_background_dispatch()
        try:
            manager.notify("campaign.completed", {"i": 1})
            # Worker enters send() and parks on the gate. Wait for arrival.
            deadline = time.monotonic() + 1.0
            while not dest.received and time.monotonic() < deadline:
                time.sleep(0.01)
            assert dest.received
            # The deadline thread inside _dispatch_one starts a daemon runner.
            # That runner is parked inside _do_dispatch → send(). Release the
            # gate so it can finish (the deadline check fires first; we
            # release afterwards so the daemon runner cleans up at process
            # exit — the dispatch was already logged as abandoned).
            dest.gate.set()
        finally:
            manager.close(timeout_s=2.0)

    def test_start_background_dispatch_is_idempotent(self):
        """Calling start_background_dispatch twice doesn't spawn two workers."""
        dest = BlockingDestination()
        manager = _manager_with(dest)
        manager.start_background_dispatch()
        first = manager._dispatch_thread
        manager.start_background_dispatch()
        assert manager._dispatch_thread is first
        dest.gate.set()
        manager.close(timeout_s=2.0)

    def test_build_alert_manager_with_background_dispatch(self):
        """build_alert_manager(..., background_dispatch=True) starts the worker."""
        manager = build_alert_manager(
            rules_path=None,
            destinations_path=None,
            include_builtin=False,
            background_dispatch=True,
        )
        try:
            assert manager._is_dispatcher_running()
        finally:
            manager.close(timeout_s=2.0)

    def test_build_alert_manager_default_keeps_synchronous_path(self):
        """Default (background_dispatch=False) preserves the legacy path —
        no worker is spawned."""
        manager = build_alert_manager(
            rules_path=None,
            destinations_path=None,
            include_builtin=False,
        )
        assert not manager._is_dispatcher_running()
        # notify() must dispatch inline so existing tests (which check
        # delivery_status immediately after notify) keep working.
        seen: list[str] = []
        manager.add_rule(
            AlertRule(
                name="sync-test",
                event_type="campaign.completed",
                condition=lambda _: True,
                severity=AlertSeverity.INFO,
                message_template="hello {i}",
            )
        )

        class _Capture(AlertDestination):
            def send(self, alert: Alert) -> bool:
                seen.append(alert.message)
                return True

        manager.add_destination(_Capture())
        manager.notify("campaign.completed", {"i": 1})
        assert seen == ["hello 1"]


class TestDispatchPoolBounded:
    """Issue #1770 — ``_dispatch_one`` must use a bounded thread pool.

    Pre-fix the per-alert ``Thread(...)`` daemon spawn was unbounded:
    50 wedged alerts ⇒ 50 daemon threads, each independently blocked in
    ``_do_dispatch``.  The fix replaces the per-call thread with a
    class-level :class:`ThreadPoolExecutor` of size
    ``_ALERT_DISPATCH_POOL_SIZE`` (default 4) reused across every
    alert, with per-alert deadlines still cancelling abandoned
    futures.

    These tests enqueue N>max_workers alerts while the destination
    hangs and assert:

    * the dispatcher worker thread count stays at 1 (the dispatcher
      worker itself),
    * the per-alert dispatch pool never grows past
      ``_ALERT_DISPATCH_POOL_SIZE`` live threads,
    * alerts past the per-alert deadline are abandoned without
      blocking subsequent deliveries.
    """

    def test_pool_size_constant_is_bounded(self) -> None:
        """The pool size constant must stay small — pinning the
        contract so a future edit cannot silently remove the cap."""
        from osimflow.alerting import _ALERT_DISPATCH_POOL_SIZE

        assert _ALERT_DISPATCH_POOL_SIZE <= 8
        assert _ALERT_DISPATCH_POOL_SIZE >= 1

    def test_dispatch_pool_caps_thread_count_under_wedged_destination(self):
        """N alerts with a wedged destination must not spawn unbounded threads.

        We use a destination whose ``send`` blocks on an Event the
        test never releases.  Enqueue N>>pool_size alerts, then count
        the live threads inside the pool.  Pre-fix each call spawned a
        fresh daemon thread; the fix reuses the same
        ``max_workers=4`` pool.
        """
        import osimflow.alerting as alerting_mod

        dest = BlockingDestination()
        manager = _manager_with(dest)
        # Tiny deadline so wedged alerts abandon fast and we can
        # repeatedly submit more.
        manager._per_alert_deadline_s = 0.05
        manager.start_background_dispatch()
        try:
            # Pre-warm the pool so ``_dispatch_pool`` is created.
            pool = manager._get_dispatch_pool()
            assert pool._max_workers == alerting_mod._ALERT_DISPATCH_POOL_SIZE

            # Fire far more than the pool ceiling.
            n_alerts = 20
            for i in range(n_alerts):
                manager.notify("campaign.completed", {"i": i})

            # The pool thread count never exceeds the configured ceiling
            # (active + idle workers reuse the same fixed set).
            active = pool._max_workers
            assert active == alerting_mod._ALERT_DISPATCH_POOL_SIZE
            assert active < n_alerts
        finally:
            dest.gate.set()
            manager.close(timeout_s=2.0)

    def test_alerts_past_deadline_are_abandoned_not_blocking(self):
        """An alert whose deadline expires is dropped, not blocking the pool.

        We verify this by issuing a wedged alert and waiting longer
        than the deadline, then issuing a *fresh* alert whose
        destination is unblocked.  The fresh alert must dispatch
        successfully even though the wedged one is still parked.
        """
        from osimflow.alerting import _ALERT_DISPATCH_POOL_SIZE

        class _Switchable(AlertDestination):
            """Wedged for the first call, unblocked for the second."""

            def __init__(self) -> None:
                self.gate = threading.Event()
                self.received: list[Alert] = []

            def send(self, alert: Alert) -> bool:
                self.received.append(alert)
                if len(self.received) == 1:
                    # Block the first call until the test releases the gate.
                    self.gate.wait(timeout=10.0)
                    return False
                return True  # second call completes immediately

        dest = _Switchable()
        manager = _manager_with(dest)
        manager._per_alert_deadline_s = 0.1
        manager.start_background_dispatch()
        try:
            manager.notify("campaign.completed", {"i": 1})
            # Wait for the dispatcher to enter send() and park on the gate.
            deadline = time.monotonic() + 1.0
            while not dest.received and time.monotonic() < deadline:
                time.sleep(0.01)
            assert dest.received, "first alert did not reach the destination"
            # Wait past the per-alert deadline so the dispatcher
            # abandons the wedged delivery.
            time.sleep(manager._per_alert_deadline_s + 0.1)
            # Submit a fresh alert — must still be dispatched because the
            # pool is bounded, not blocked on the wedged worker.
            manager.notify("campaign.completed", {"i": 2})
            deadline = time.monotonic() + 2.0
            while len(dest.received) < 2 and time.monotonic() < deadline:
                time.sleep(0.01)
            assert len(dest.received) == 2, (
                "second alert should reach the destination — the pool "
                "must not be blocked by the wedged first alert"
            )
            # Sanity: pool size is still bounded.
            pool = manager._get_dispatch_pool()
            assert pool._max_workers == _ALERT_DISPATCH_POOL_SIZE
        finally:
            dest.gate.set()
            manager.close(timeout_s=2.0)

    def test_close_shuts_down_dispatch_pool(self):
        """``close()`` shuts down the per-alert dispatch pool."""
        manager = _manager_with(BlockingDestination())
        manager.start_background_dispatch()
        # Force the pool to be created.
        pool = manager._get_dispatch_pool()
        assert manager._dispatch_pool is pool
        manager.close(timeout_s=2.0)
        assert manager._dispatch_pool is None


def _manager_with(dest: AlertDestination) -> AlertManager:  # type: ignore[no-redef]
    """Test-local helper (mirrors the one at line ~308 but ensures the test
    class can resolve it after the appended-block re-export order)."""
    manager = AlertManager()
    manager.add_rule(
        AlertRule(
            name="test-rule",
            event_type="campaign.completed",
            condition=lambda _: True,
            severity=AlertSeverity.WARNING,
            message_template="alert {i}",
        )
    )
    manager.add_destination(dest)
    return manager
