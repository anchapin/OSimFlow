"""Tests for osimflow/json_utils.py (issue #1763).

The ``safe_json_loads`` / ``safe_json_dumps`` helpers wrap
``json.loads`` / ``json.dumps`` with consistent exception handling so
corrupted or malformed JSON does not crash a running campaign.
Both helpers have non-trivial branching (multiple except clauses,
``log_warnings`` toggle, ``raise_on_error`` toggle, custom ``default``,
``indent`` / ``sort_keys``) that was previously exercised only
indirectly.

These tests cover every documented branch:

- (a) successful load/dump roundtrip
- (b) ``safe_json_loads`` returning the supplied ``default`` on
      ``JSONDecodeError`` and on ``OSError``
- (c) ``safe_json_loads`` ``log_warnings=False`` suppressing the log
- (d) ``safe_json_dumps`` ``indent`` / ``sort_keys`` kwargs visible
      in the output
- (e) ``safe_json_dumps`` ``raise_on_error=True`` re-raising
      ``TypeError`` (and ``OSError``)
- (f) ``default=str`` accepting a non-trivial type (e.g. ``Path``)
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from osimflow.json_utils import safe_json_dumps, safe_json_loads

# ---------------------------------------------------------------------------
# safe_json_loads
# ---------------------------------------------------------------------------


class TestSafeJsonLoads:
    """Coverage for ``safe_json_loads`` (issue #1763 acceptance (a,b,c))."""

    def test_roundtrip(self, tmp_path: Path) -> None:
        """Roundtripping a payload via safe_json_loads / json.dumps."""
        payload = {"a": 1, "b": [1, 2, 3], "c": "hello"}
        path = tmp_path / "doc.json"
        path.write_text(json.dumps(payload))
        assert safe_json_loads(path) == payload

    def test_returns_default_on_json_decode_error(self, tmp_path: Path) -> None:
        """Malformed JSON triggers the except clause and returns *default*."""
        path = tmp_path / "broken.json"
        path.write_text("{this is not valid JSON")
        sentinel = object()
        assert safe_json_loads(path, default=sentinel) is sentinel

    def test_returns_default_on_oserror(self, tmp_path: Path) -> None:
        """A missing file (OSError) also returns *default*."""
        path = tmp_path / "does_not_exist.json"
        sentinel = object()
        assert safe_json_loads(path, default=sentinel) is sentinel

    def test_log_warnings_true_emits_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The default (``log_warnings=True``) logs a WARNING on failure."""
        path = tmp_path / "broken.json"
        path.write_text("not json")
        with caplog.at_level(logging.WARNING, logger="osimflow.json_utils"):
            safe_json_loads(path)
        assert any("Failed to read/parse" in rec.message for rec in caplog.records)

    def test_log_warnings_false_suppresses_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """``log_warnings=False`` silently returns the default."""
        path = tmp_path / "broken.json"
        path.write_text("not json")
        with caplog.at_level(logging.WARNING, logger="osimflow.json_utils"):
            result = safe_json_loads(path, default={}, log_warnings=False)
        assert result == {}
        assert not any("Failed to read/parse" in rec.message for rec in caplog.records), (
            "log_warnings=False must suppress the warning"
        )

    def test_default_default_is_none(self, tmp_path: Path) -> None:
        """The default ``default`` kwarg is ``None`` (per docstring)."""
        path = tmp_path / "broken.json"
        path.write_text("not json")
        # Silent so we don't pollute the test output with the expected
        # warning — we only care about the return value here.
        assert safe_json_loads(path, log_warnings=False) is None


# ---------------------------------------------------------------------------
# safe_json_dumps
# ---------------------------------------------------------------------------


class TestSafeJsonDumps:
    """Coverage for ``safe_json_dumps`` (issue #1763 acceptance (a,d,e,f))."""

    def test_roundtrip(self, tmp_path: Path) -> None:
        """``safe_json_dumps`` writes JSON that ``json.loads`` can read."""
        path = tmp_path / "out.json"
        payload = {"a": 1, "b": [1, 2, 3]}
        assert safe_json_dumps(payload, path) is True
        assert json.loads(path.read_text()) == payload

    def test_returns_true_on_success(self, tmp_path: Path) -> None:
        """Successful write returns ``True``."""
        path = tmp_path / "out.json"
        assert safe_json_dumps({"k": "v"}, path) is True

    def test_indent_kwarg_visible_in_output(self, tmp_path: Path) -> None:
        """``indent=2`` writes pretty-printed JSON (one key per line)."""
        path = tmp_path / "pretty.json"
        safe_json_dumps({"k": "v", "x": 1}, path, indent=2)
        text = path.read_text()
        # Pretty output contains a newline after ``{`` and before ``}``.
        assert "\n" in text
        # The compact form would be ``{"k": "v", "x": 1}`` (no
        # whitespace); with indent=2 the keys appear on their own lines.
        assert "  " in text  # the indent

    def test_sort_keys_kwarg_visible_in_output(self, tmp_path: Path) -> None:
        """``sort_keys=True`` orders keys alphabetically in the output."""
        path = tmp_path / "sorted.json"
        safe_json_dumps({"b": 2, "a": 1, "c": 3}, path, sort_keys=True)
        text = path.read_text()
        # The first key in the JSON output is "a" — earlier in the
        # alphabet than "b" and "c".  Without sort_keys the first key
        # would be "b".
        assert text.index('"a"') < text.index('"b"')
        assert text.index('"b"') < text.index('"c"')

    def test_default_str_accepts_path(self, tmp_path: Path) -> None:
        """``default=str`` lets the helper serialise a ``Path`` payload."""
        path = tmp_path / "with_path.json"
        # Without ``default=str`` this raises TypeError; with it, the
        # Path is rendered via its str() representation.
        assert safe_json_dumps({"file": tmp_path}, path, default=str) is True
        payload = json.loads(path.read_text())
        assert payload["file"] == str(tmp_path)

    def test_no_default_raises_type_error_on_non_serialisable(self, tmp_path: Path) -> None:
        """Without ``default``, a non-serialisable object → TypeError caught."""
        path = tmp_path / "fail.json"
        # Path is not JSON-serialisable without a default handler.
        # ``safe_json_dumps`` catches TypeError and returns False.
        assert safe_json_dumps({"file": tmp_path}, path) is False
        assert not path.exists(), "failed write must not create the file"

    def test_raise_on_error_true_re_raises_typeerror(self, tmp_path: Path) -> None:
        """``raise_on_error=True`` re-raises TypeError instead of returning False."""
        path = tmp_path / "fail.json"
        with pytest.raises(TypeError):
            safe_json_dumps(
                {"file": tmp_path},  # Path → TypeError without default
                path,
                raise_on_error=True,
            )

    def test_raise_on_error_true_re_raises_oserror(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``raise_on_error=True`` re-raises OSError too (e.g. read-only FS)."""

        # Build a payload that's serialisable so we hit the OSError path
        # via ``path.write_text`` rather than the TypeError path.
        def _failing_write_text(self: Path, *args: object, **kwargs: object) -> None:
            raise OSError("read-only filesystem")

        monkeypatch.setattr(Path, "write_text", _failing_write_text)
        with pytest.raises(OSError):
            safe_json_dumps({"k": "v"}, tmp_path / "out.json", raise_on_error=True)

    def test_raise_on_error_false_returns_false(self, tmp_path: Path) -> None:
        """``raise_on_error=False`` (default) returns False on failure."""
        path = tmp_path / "fail.json"
        assert (
            safe_json_dumps(
                {"file": tmp_path},  # TypeError without default
                path,
                raise_on_error=False,
            )
            is False
        )
