"""Tests for osimflow/logging.py — LogAggregator (issue #340)."""

import json
import logging

from osimflow.logging import JSONFormatter, LogAggregator, get_logger, setup_logging


class TestLogAggregator:
    def test_disabled_when_url_is_none(self, tmp_path):
        agg = LogAggregator(log_aggregation_url=None)
        log_file = tmp_path / "stdout.log"
        log_file.write_text("hello world\n")
        agg.add_log_file(log_file)
        agg.publish()
        assert agg._pending == []

    def test_disabled_when_url_is_empty_string(self, tmp_path):
        agg = LogAggregator(log_aggregation_url="")
        log_file = tmp_path / "stdout.log"
        log_file.write_text("hello world\n")
        agg.add_log_file(log_file)
        agg.publish()
        assert agg._pending == []

    def test_add_log_file_noop_for_missing_file(self, tmp_path):
        agg = LogAggregator(log_aggregation_url="https://logs.us-east-1.amazonaws.com/test/group")
        missing = tmp_path / "nonexistent.log"
        agg.add_log_file(missing)
        assert agg._pending == []

    def test_add_log_file_collects_lines(self, tmp_path):
        agg = LogAggregator(log_aggregation_url="https://logs.us-east-1.amazonaws.com/test/group")
        log_file = tmp_path / "stdout.log"
        log_file.write_text("line1\nline2\n")
        agg.add_log_file(log_file, log_stream="my-stream")
        assert len(agg._pending) == 2
        stream, msg, ts = agg._pending[0]
        assert stream == "my-stream"
        assert msg == "line1\n"

    def test_add_log_file_increments_timestamps(self, tmp_path):
        agg = LogAggregator(log_aggregation_url="https://logs.us-east-1.amazonaws.com/test/group")
        log_file = tmp_path / "stdout.log"
        log_file.write_text("line1\nline2\n")
        agg.add_log_file(log_file)
        _, _, ts1 = agg._pending[0]
        _, _, ts2 = agg._pending[1]
        assert ts2 == ts1 + 1

    def test_clear_resets_pending(self, tmp_path):
        agg = LogAggregator(log_aggregation_url="https://logs.us-east-1.amazonaws.com/test/group")
        log_file = tmp_path / "stdout.log"
        log_file.write_text("hello\n")
        agg.add_log_file(log_file)
        agg.clear()
        assert agg._pending == []

    def test_publish_noop_when_disabled(self):
        agg = LogAggregator(log_aggregation_url=None)
        agg.publish()
        assert agg._pending == []

    def test_publish_noop_when_pending_empty(self):
        agg = LogAggregator(log_aggregation_url="https://logs.us-east-1.amazonaws.com/test/group")
        agg.publish()
        assert agg._pending == []

    def test_url_parsing_extracts_region_group_stream(self):
        agg = LogAggregator(
            log_aggregation_url="https://logs.us-west-2.amazonaws.com/my-group/my-stream"
        )
        assert agg._region == "us-west-2"
        assert agg._log_group == "my-group"
        assert agg._log_stream == "my-stream"

    def test_url_parsing_invalid_url(self):
        agg = LogAggregator(log_aggregation_url="not-a-valid-url")
        assert agg._region is None
        assert agg._log_group is None
        assert agg._log_stream is None

    def test_default_batch_size_is_1000(self):
        agg = LogAggregator()
        assert agg._batch_size == 1000

    def test_custom_batch_size(self):
        agg = LogAggregator(batch_size=500)
        assert agg._batch_size == 500

    def test_publish_with_mocked_boto3(self, tmp_path, monkeypatch):
        published_events: list[dict] = []

        class MockClient:
            def put_log_events(self, logGroupName, logStreamName, logEvents):
                published_events.extend(logEvents)

        monkeypatch.setattr("boto3.client", lambda **kwargs: MockClient())

        log_file = tmp_path / "stdout.log"
        log_file.write_text("hello\n")

        agg = LogAggregator(log_aggregation_url="https://logs.us-east-1.amazonaws.com/test/group")
        agg.add_log_file(log_file, log_stream="test-stream")
        agg.publish()

        assert len(published_events) == 1
        assert published_events[0]["message"]["data"] == "hello\n"
        assert agg._pending == []


class TestJSONFormatter:
    def test_format_produces_valid_json(self):
        formatter = JSONFormatter()
        record = logging.LogRecord(
            name="osimflow.test",
            level=logging.INFO,
            pathname="test.py",
            lineno=10,
            msg="test message",
            args=(),
            exc_info=None,
        )
        output = formatter.format(record)
        parsed = json.loads(output)
        assert parsed["message"] == "test message"
        assert parsed["level"] == "INFO"
        assert parsed["logger"] == "osimflow.test"
        assert parsed["module"] == "test"
        assert parsed["line"] == 10
        assert "timestamp" in parsed

    def test_format_includes_timestamp(self):
        formatter = JSONFormatter()
        record = logging.LogRecord(
            name="osimflow.test",
            level=logging.INFO,
            pathname="test.py",
            lineno=1,
            msg="hello",
            args=(),
            exc_info=None,
        )
        output = formatter.format(record)
        parsed = json.loads(output)
        assert "timestamp" in parsed
        assert parsed["timestamp"].endswith("+00:00")

    def test_format_includes_all_required_fields(self):
        formatter = JSONFormatter()
        record = logging.LogRecord(
            name="osimflow.test",
            level=logging.WARNING,
            pathname="test.py",
            lineno=42,
            msg="warning message",
            args=(),
            exc_info=None,
        )
        output = formatter.format(record)
        parsed = json.loads(output)
        assert set(parsed.keys()) >= {
            "timestamp",
            "level",
            "logger",
            "message",
            "module",
            "function",
            "line",
        }


class TestSetupLogging:
    def test_console_handler_added(self, tmp_path):
        setup_logging(log_file=None, console=True)
        logger = logging.getLogger("osimflow")
        assert any(
            isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
            for h in logger.handlers
        )

    def test_file_handler_added(self, tmp_path):
        logger = logging.getLogger("osimflow")
        logger.handlers.clear()
        log_file = tmp_path / "test.log"
        setup_logging(log_file=log_file, console=False)
        assert any(isinstance(h, logging.FileHandler) for h in logger.handlers)

    def test_rotation_handler_used(self, tmp_path):
        from logging.handlers import RotatingFileHandler

        logger = logging.getLogger("osimflow")
        logger.handlers.clear()
        log_file = tmp_path / "test.log"
        setup_logging(log_file=log_file, console=False, max_bytes=1000, backup_count=3)
        assert any(isinstance(h, RotatingFileHandler) for h in logger.handlers)

    def test_multiple_setup_no_duplicate_handlers(self, tmp_path):
        logger = logging.getLogger("osimflow")
        logger.handlers.clear()
        setup_logging(log_file=None, console=True)
        setup_logging(log_file=None, console=True)
        assert len(logger.handlers) == 1


class TestGetLogger:
    def test_returns_osimflow_child_logger(self):
        logger = get_logger("campaign")
        assert logger.name == "osimflow.campaign"

    def test_returns_correct_child(self):
        logger = get_logger("cache")
        assert logger.name == "osimflow.cache"
