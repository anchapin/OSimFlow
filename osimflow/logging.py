"""Structured logging for OSimFlow.

Provides JSON-formatted logging with log rotation via RotatingFileHandler.
"""

import logging
import json
import sys
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any


class JSONFormatter(logging.Formatter):
    """Formatter that outputs log records as JSON objects."""

    def __init__(self, include_extra: bool = True) -> None:
        super().__init__()
        self.include_extra = include_extra

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
        }
        if self.include_extra and record.exc_info:
            entry["exc_info"] = self.formatException(record.exc_info)
        if self.include_extra:
            for key, value in record.__dict__.items():
                if key not in (
                    "name",
                    "msg",
                    "args",
                    "created",
                    "filename",
                    "funcName",
                    "levelname",
                    "levelno",
                    "lineno",
                    "module",
                    "msecs",
                    "pathname",
                    "process",
                    "processName",
                    "relativeCreated",
                    "thread",
                    "threadName",
                    "exc_info",
                    "exc_text",
                    "stack_info",
                    "message",
                    "taskName",
                ):
                    entry[key] = value
        return json.dumps(entry)


def setup_logging(
    log_file: Path | str | None = None,
    level: int = logging.INFO,
    max_bytes: int = 10_000_000,
    backup_count: int = 5,
    console: bool = True,
) -> None:
    """Configure OSimFlow structured logging.

    Args:
        log_file: Path to log file. If None, no file handler is created.
        level: Logging level for all handlers.
        max_bytes: Max size of each log file before rotation.
        backup_count: Number of backup files to keep.
        console: Whether to add a console handler.
    """
    root_logger = logging.getLogger("osimflow")
    root_logger.setLevel(level)

    if not root_logger.handlers:
        json_formatter = JSONFormatter()

        if log_file is not None and log_file != "":
            file_handler = RotatingFileHandler(
                log_file,
                maxBytes=max_bytes,
                backupCount=backup_count,
            )
            file_handler.setLevel(level)
            file_handler.setFormatter(json_formatter)
            root_logger.addHandler(file_handler)

        if console:
            console_handler = logging.StreamHandler(sys.stdout)
            console_handler.setLevel(level)
            console_handler.setFormatter(json_formatter)
            root_logger.addHandler(console_handler)


def get_logger(name: str) -> logging.Logger:
    """Get a logger for the given name under the osimflow hierarchy."""
    return logging.getLogger(f"osimflow.{name}")
