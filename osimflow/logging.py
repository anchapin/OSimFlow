"""Structured logging for OSimFlow.

Provides JSON-formatted logging with log rotation via RotatingFileHandler.
"""

import json
import logging
import re
import sys
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

log = logging.getLogger("osimflow.logging")


class JSONFormatter(logging.Formatter):
    """Formatter that outputs log records as JSON objects."""

    def __init__(self, include_extra: bool = True) -> None:
        super().__init__()
        self.include_extra = include_extra

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
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


class LogAggregator:
    """Aggregates per-sample log files and publishes them to CloudWatch Logs.

    Collects stdout/stderr log files from completed simulation samples and
    publishes them in batches to CloudWatch Logs via the ``put_log_events``
    API. This enables distributed log aggregation across all campaign workers
    in a single queryable location.

    Parameters
    ----------
    log_aggregation_url
        CloudWatch Logs URL in the format
        ``https://logs.<region>.amazonaws.com/<log-group>/<log-stream>``.
        When ``None``, publishing is a no-op (disabled).
    batch_size
        Maximum number of log events to send per ``put_log_events`` call.
        CloudWatch Logs has a limit of 10,000 events per batch.
    """

    def __init__(
        self,
        log_aggregation_url: str | None = None,
        batch_size: int = 1000,
    ) -> None:
        self._url = log_aggregation_url
        self._batch_size = batch_size
        self._pending: list[tuple[str, str, int]] = []  # (log_stream, message, timestamp)
        self._client: Any = None
        self._log_group: str | None = None
        self._log_stream: str | None = None
        self._region: str | None = None
        if self._url:
            self._parse_url()

    def _parse_url(self) -> None:
        """Parse log_aggregation_url into group, stream, and region components."""
        assert self._url is not None
        m = re.match(
            r"https://logs\.(\w+-\w+-\d+)\.amazonaws\.com/([^/]+)/(.+)",
            self._url,
        )
        if not m:
            log.warning("could not parse log_aggregation_url: %s", self._url)
            return
        self._region = m.group(1)
        self._log_group = m.group(2)
        self._log_stream = m.group(3)

    def _get_client(self) -> Any:
        """Lazily create a boto3 logs client."""
        if self._client is None:
            try:
                import boto3  # noqa: PLC0415
            except ImportError:
                raise ImportError(
                    "LogAggregator requires boto3. Install with: pip install osimflow[aws]"
                ) from None
            kwargs: dict[str, Any] = {"service_name": "logs"}
            if self._region:
                kwargs["region_name"] = self._region
            self._client = boto3.client(**kwargs)
        return self._client

    def add_log_file(self, log_path: Path, log_stream: str | None = None) -> None:
        """Register a log file for aggregation.

        Parameters
        ----------
        log_path
            Path to the stdout or stderr log file.
        log_stream
            CloudWatch Logs stream name. Defaults to the log file's filename
            within the campaign's log stream prefix.
        """
        if not self._url or not log_path.is_file():
            return
        stream = log_stream or f"{self._log_stream}/{log_path.name}"
        timestamp = int(log_path.stat().st_mtime * 1000)
        content = log_path.read_text(errors="replace")
        for line in content.splitlines(keepends=True):
            self._pending.append((stream, line, timestamp))
            timestamp += 1

    def publish(self) -> None:
        """Publish all pending log events to CloudWatch Logs.

        Events are sent in batches of up to ``self._batch_size`` via the
        ``put_log_events`` API. Logs are grouped by stream name and
        sent in chronological order.

        This method is idempotent — calling it multiple times re-sends
        the same events unless ``clear()`` is called between calls.
        """
        if not self._pending or not self._url:
            return

        client = self._get_client()
        by_stream: dict[str, list[dict[str, Any]]] = {}
        for stream, message, timestamp in self._pending:
            by_stream.setdefault(stream, []).append(
                {"timestamp": timestamp, "message": {"data": message}}
            )

        for stream, events in by_stream.items():
            for i in range(0, len(events), self._batch_size):
                batch = events[i : i + self._batch_size]
                try:
                    client.put_log_events(
                        logGroupName=self._log_group,
                        logStreamName=stream,
                        logEvents=batch,
                    )
                except Exception as exc:
                    log.warning("failed to publish logs to stream %s: %s", stream, exc)

        log.debug("published %d log events to CloudWatch Logs", len(self._pending))
        self._pending.clear()

    def clear(self) -> None:
        """Clear all pending log events without publishing."""
        self._pending.clear()
