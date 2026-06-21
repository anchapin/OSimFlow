"""Safe JSON helpers for OSimFlow.

Wraps json.loads / json.dumps with consistent exception handling so that
corrupted or malformed JSON (from failed writes, disk corruption, or
interrupted campaigns) does not crash a running campaign.

Issue #716
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


def safe_json_loads(
    path: Path,
    default: Any = None,
    *,
    log_warnings: bool = True,
) -> Any:
    """Read and parse a JSON file, returning *default* on failure.

    Handles both JSONDecodeError (malformed content) and OSError (disk
    errors, permissions, etc.).  Callers that need different fallback
    semantics can override *default* or check the return value explicitly.

    Parameters
    ----------
    path
        Path to the JSON file to read.
    default
        Value to return when parsing fails.  Defaults to None.
    log_warnings
        Whether to emit a logger.warning when parsing fails.

    Returns
    -------
    Parsed JSON object, or *default* if the file could not be read or
    parsed.
    """
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        if log_warnings:
            log.warning("Failed to read/parse %s: %s", path, exc)
        return default


def safe_json_dumps(
    obj: Any,
    path: Path,
    *,
    default: str | None = None,
    indent: int | None = None,
    sort_keys: bool = False,
    raise_on_error: bool = False,
) -> bool:
    """Serialize *obj* to JSON and write it to *path*.

    Handles JSONEncodeError and OSError (disk full, permissions, etc.).

    Parameters
    ----------
    obj
        Object to serialize. Must be JSON-serializable or have a
        ``default`` converter.
    path
        Destination file path.
    default
        Passed through to ``json.dumps`` as the *default* kwarg.
        When None, no ``default`` is passed (equivalent to ``default=str``).
    indent
        Passed through to ``json.dumps`` as the *indent* kwarg.
    sort_keys
        Passed through to ``json.dumps`` as the *sort_keys* kwarg.
    raise_on_error
        When False (the default), failures are logged as warnings and
        the function returns False.  When True, the original exception
        is re-raised.

    Returns
    -------
    True if the file was written successfully, False otherwise (unless
    *raise_on_error* is True).
    """
    kwargs: dict[str, Any] = {"default": default} if default is not None else {}
    if indent is not None:
        kwargs["indent"] = indent
    kwargs["sort_keys"] = sort_keys
    try:
        path.write_text(json.dumps(obj, **kwargs))
        return True
    except (TypeError, OSError) as exc:
        log.warning("Failed to write JSON to %s: %s", path, exc)
        if raise_on_error:
            raise
        return False
