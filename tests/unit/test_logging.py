"""Tests for osimflow/logging.py."""

import json
import logging

from osimflow.logging import JSONFormatter, get_logger, setup_logging


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
