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

        time.sleep(0.01)
        manager.notify("campaign.completed", {"i": 2})

        # Both the original and the new failure remain queued.
        assert len(manager._alert_history) == 2
        retained = [e for e in manager._alert_history if e.alert.message == "alert 1"][0]
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
