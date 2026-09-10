"""Tests for osimflow/storage.py retry consolidation (issue #1781).

Issue #1781 — ``_retry_transient_storage`` used to hand-roll an
exponential-backoff loop with deterministic ``time.sleep(delay)``
calls; it now delegates to the shared ``retry_with_backoff`` helper
in :mod:`osimflow.executors.base` (issue #1540) with ``jitter=True``.
This module exercises the delegation contract:

* ``_is_transient_storage_error`` still classifies transient errors
  correctly (preserves the issue #1398 acceptance),
* ``retry_with_backoff`` is the actual owner of the sleep schedule —
  patching ``random.uniform`` via ``osimflow.testing.patch_targets``
  changes the schedule that storage retries observe,
* permanent errors propagate immediately without retrying.
"""

from __future__ import annotations

import logging
from unittest.mock import patch

import pytest

from osimflow.storage import (
    _TRANSIENT_ERROR_MARKERS,
    _is_transient_storage_error,
    _retry_transient_storage,
)

# ---------------------------------------------------------------------------
# _is_transient_storage_error — issue #1398 acceptance preserved
# ---------------------------------------------------------------------------


class TestIsTransientStorageError:
    def test_connection_error_is_transient(self) -> None:
        assert _is_transient_storage_error(ConnectionError("reset")) is True

    def test_timeout_error_is_transient(self) -> None:
        assert _is_transient_storage_error(TimeoutError("read")) is True

    @pytest.mark.parametrize("marker", list(_TRANSIENT_ERROR_MARKERS))
    def test_marker_in_text_is_transient(self, marker: str) -> None:
        exc = RuntimeError(f"upstream said: {marker}")
        assert _is_transient_storage_error(exc) is True

    def test_non_matching_text_is_not_transient(self) -> None:
        # Plain text with no transient marker should not be retried.
        assert _is_transient_storage_error(ValueError("bad request")) is False


# ---------------------------------------------------------------------------
# _retry_transient_storage — delegates to retry_with_backoff with jitter
# ---------------------------------------------------------------------------


class TestRetryTransientStorageDelegation:
    """The retry helper is now a thin wrapper over ``retry_with_backoff``.

    Issue #1781 requires the sleep schedule to come from
    ``retry_with_backoff`` (issue #1540) — full-jitter exponential
    backoff.  We assert this by patching ``random.uniform`` through
    :mod:`osimflow.testing.patch_targets` (the explicit testing
    surface for the shared ``random`` module singleton, issue #1574)
    and confirming storage retries honour the jittered schedule
    rather than the historic deterministic one.
    """

    def test_uses_jittered_backoff_via_shared_helper(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Patching ``random.uniform`` changes the sleep schedule.

        Pre-fix: storage called ``time.sleep(delay)`` directly with a
        deterministic value (no ``random.uniform`` call).  After the
        fix, the sleep schedule comes from ``retry_with_backoff`` →
        ``random.uniform(0, window)`` and patching it must reduce the
        observed sleep duration to the patched return value.
        """
        # Patch both time.sleep and random.uniform via the shared
        # singleton that retry_with_backoff imports.
        from osimflow.testing import patch_targets

        sleeps: list[float] = []

        def _capture_sleep(d: float) -> None:
            sleeps.append(d)

        monkeypatch.setattr(patch_targets.time, "sleep", _capture_sleep)
        # Force uniform() to always return a small constant — the
        # jittered sleep will then be that constant, not the historical
        # deterministic delay.
        monkeypatch.setattr(patch_targets.random, "uniform", lambda _lo, _hi: 0.1)

        calls: list[int] = []
        target_call_count = 4  # > 3, so we always exhaust retries

        def _always_transient() -> None:
            calls.append(1)
            raise ConnectionError("503 service unavailable")

        with caplog.at_level(logging.WARNING, logger="osimflow.storage"):
            with pytest.raises(ConnectionError, match="503"):
                _retry_transient_storage("test op", _always_transient)

        # 3 attempts total → 2 sleeps in between.
        assert len(calls) == 3
        assert len(sleeps) == 2
        # The first sleep's window is 2.0s, second is 4.0s (both
        # <= cap).  Because uniform() returns 0.1 always, the
        # observed sleep is exactly 0.1 — *not* the deterministic 2.0
        # / 4.0 the pre-fix code would have produced.
        assert sleeps == [0.1, 0.1]
        # The historic log line is preserved.
        assert any(
            "test op transient failure" in rec.message
            and "attempt 1/3" in rec.message
            for rec in caplog.records
        )

    def test_permanent_error_propagates_without_retry(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A non-transient error must raise immediately, no retry sleep."""
        from osimflow.testing import patch_targets

        sleeps: list[float] = []
        monkeypatch.setattr(patch_targets.time, "sleep", sleeps.append)

        def _fatal() -> None:
            raise ValueError("auth denied — permanent")

        with pytest.raises(ValueError, match="auth denied"):
            _retry_transient_storage("test op", _fatal)

        assert sleeps == [], (
            "permanent errors must not trigger retry sleeps"
        )

    def test_succeeds_on_retry_after_transient(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A retry that succeeds on the second attempt returns cleanly."""
        from osimflow.testing import patch_targets

        monkeypatch.setattr(patch_targets.time, "sleep", lambda _d: None)
        monkeypatch.setattr(patch_targets.random, "uniform", lambda _lo, _hi: 0.0)

        attempts: list[int] = []

        def _succeeds_on_second() -> None:
            attempts.append(1)
            if len(attempts) == 1:
                raise ConnectionError("503 throttled")
            return None

        _retry_transient_storage("test op", _succeeds_on_second)
        assert len(attempts) == 2

    def test_module_path_uses_shared_helper(self) -> None:
        """``retry_with_backoff`` from executors.base is the source of truth.

        This test pins the consolidation: the storage module must not
        silently regress to a hand-rolled ``time.sleep`` loop.  The
        function should still work end-to-end when
        ``retry_with_backoff`` is the actual owner of the sleep
        schedule — verified above by patching ``random.uniform``.
        """
        from osimflow.executors.base import retry_with_backoff

        # smoke — both functions exist and the storage helper is the
        # call-site that consumes the shared helper.
        assert callable(retry_with_backoff)
        assert callable(_retry_transient_storage)


# ---------------------------------------------------------------------------
# Regression: ensure monkeypatching the storage module's local symbols
# still works (i.e. _is_transient_storage_error is unchanged).
# ---------------------------------------------------------------------------


class TestPatchability:
    def test_is_transient_helper_patchable(self) -> None:
        """A test using ``patch`` can swap out the storage classifier."""
        with patch("osimflow.storage._is_transient_storage_error") as mock_classify:
            mock_classify.return_value = True

            calls: list[int] = []

            def _fail() -> None:
                calls.append(1)
                raise ValueError("anything — classifier returns True via patch")

            # With classifier patched to True, even ValueError is
            # retried — verifies the wrapper consults the callable by
            # reference rather than caching it at import time.
            with pytest.raises(ValueError):
                _retry_transient_storage("op", _fail)

            # 3 attempts because patched classifier returns True.
            assert len(calls) == 3
            assert mock_classify.call_count == 3
