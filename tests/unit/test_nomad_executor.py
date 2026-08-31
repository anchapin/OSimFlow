"""Unit tests for Nomad HTTP retry on transient 5xx and URLError (issue #1395).

Tests cover:
  - ``_retry_nomad_request``: exponential backoff on HTTPError
    {500, 502, 503, 504} and URLError, matching the AWS Batch 5-attempt/
    30s cap pattern.
  - ``_NomadClient._request``: end-to-end retry on transient 502 with a
    fake response that succeeds on the second attempt (acceptance
    criterion from issue #1395 — "a regression test injects a transient
    502 and asserts the job eventually completes").
  - ``_NomadHandle.result()``: the allocation poll path goes through the
    same client, so a transient blip is absorbed by the retry helper
    and the allocation eventually reports ``complete``.

The tests mock ``urllib.request.urlopen`` (or ``_NomadClient.urlopen``)
so no real Nomad cluster is required; they are portable to any Python
3.12+ environment.
"""

from __future__ import annotations

import json
import urllib.error
from collections.abc import Callable
from io import BytesIO
from unittest.mock import MagicMock, patch

import pytest

from osimflow.executors import (
    _NOMAD_RETRY_CAP_S,
    _NOMAD_RETRY_INITIAL_DELAY_S,
    _NOMAD_RETRY_MAX_ATTEMPTS,
    _NOMAD_RETRYABLE_HTTP_CODES,
    NomadExecutor,
    _retry_nomad_request,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _make_http_error(code: int) -> urllib.error.HTTPError:
    """Construct an HTTPError with a readable, UTF-8 body for retry tests."""
    return urllib.error.HTTPError(
        url="http://nomad.local:4646/v1/jobs",
        code=code,
        msg=f"HTTP Error {code}",
        hdrs={},
        fp=BytesIO(b'{"error":"transient"}'),
    )


def _make_url_response(body: dict[str, object]) -> MagicMock:
    """Build a MagicMock that mimics ``urllib.request.urlopen``'s context
    manager protocol, returning *body* on ``.read()``.
    """
    resp = MagicMock()
    resp.read.return_value = json.dumps(body).encode("utf-8")
    resp.__enter__ = lambda s: s
    resp.__exit__ = lambda s, *a: None
    return resp


# ---------------------------------------------------------------------------
# _NOMAD_RETRYABLE_HTTP_CODES — make sure the constants stay aligned with
# the issue acceptance criterion (issue #1395).
# ---------------------------------------------------------------------------
class TestNomadRetryConstants:
    def test_retryable_codes_match_acceptance_criterion(self) -> None:
        """{500, 502, 503, 504} per issue #1395 acceptance criterion."""
        assert _NOMAD_RETRYABLE_HTTP_CODES == frozenset({500, 502, 503, 504})

    def test_default_attempts_match_aws_batch(self) -> None:
        """5 attempts / 30s cap, mirroring ``_submit_job_with_retry``."""
        assert _NOMAD_RETRY_MAX_ATTEMPTS == 5
        assert _NOMAD_RETRY_CAP_S == pytest.approx(30.0)
        assert _NOMAD_RETRY_INITIAL_DELAY_S == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# _retry_nomad_request — direct tests of the retry helper
# ---------------------------------------------------------------------------
class TestRetryNomadRequest:
    def test_returns_success_on_first_attempt(self) -> None:
        """A successful call returns immediately with no sleep."""
        call_count = 0

        def _ok() -> str:
            nonlocal call_count
            call_count += 1
            return "ok"

        with patch("osimflow.executors.time.sleep") as sleep_mock:
            result = _retry_nomad_request(_ok)

        assert result == "ok"
        assert call_count == 1
        sleep_mock.assert_not_called()

    def test_retries_on_http_503_then_succeeds(self) -> None:
        """Two HTTPError(503) failures followed by a success must
        propagate the success value (acceptance criterion #1)."""
        call_count = 0

        def _two_blips_then_ok() -> str:
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise _make_http_error(503)
            return "blip-survived"

        with patch("osimflow.executors.time.sleep") as sleep_mock:
            result = _retry_nomad_request(_two_blips_then_ok)

        assert result == "blip-survived"
        assert call_count == 3
        # Two retries → two jittered sleeps.
        assert sleep_mock.call_count == 2

    def test_retries_on_url_error_then_succeeds(self) -> None:
        """A single URLError followed by a success must propagate the
        success value (acceptance criterion #2)."""
        call_count = 0

        def _dns_blip_then_ok() -> str:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise urllib.error.URLError("[Errno -2] Name or service not known")
            return "url-survived"

        with patch("osimflow.executors.time.sleep") as sleep_mock:
            result = _retry_nomad_request(_dns_blip_then_ok)

        assert result == "url-survived"
        assert call_count == 2
        sleep_mock.assert_called_once()

    def test_retries_on_all_five_codes(self) -> None:
        """Each transient 5xx code in the retryable set must trigger a retry."""

        def _first_blip_then_ok(target_code: int) -> Callable[[], str]:
            state = {"count": 0}

            def _call() -> str:
                state["count"] += 1
                if state["count"] == 1:
                    raise _make_http_error(target_code)
                return f"survived-{target_code}"

            return _call

        for code in sorted(_NOMAD_RETRYABLE_HTTP_CODES):
            with patch("osimflow.executors.time.sleep"):
                result = _retry_nomad_request(_first_blip_then_ok(code))
            assert result == f"survived-{code}"

    def test_propagates_final_exception_when_all_attempts_fail(self) -> None:
        """Five consecutive HTTPError(502) must raise on the last attempt
        (acceptance criterion #3)."""
        call_count = 0

        def _always_502() -> str:
            nonlocal call_count
            call_count += 1
            raise _make_http_error(502)

        with patch("osimflow.executors.time.sleep") as sleep_mock:
            with pytest.raises(urllib.error.HTTPError) as exc_info:
                _retry_nomad_request(_always_502)

        assert exc_info.value.code == 502
        assert call_count == _NOMAD_RETRY_MAX_ATTEMPTS
        # 4 sleeps between 5 attempts.
        assert sleep_mock.call_count == _NOMAD_RETRY_MAX_ATTEMPTS - 1

    def test_propagates_final_url_error_when_all_attempts_fail(self) -> None:
        """Five consecutive URLError must raise on the last attempt."""
        call_count = 0

        def _always_urlerr() -> str:
            nonlocal call_count
            call_count += 1
            raise urllib.error.URLError("connection refused")

        with patch("osimflow.executors.time.sleep") as sleep_mock:
            with pytest.raises(urllib.error.URLError, match="connection refused"):
                _retry_nomad_request(_always_urlerr)

        assert call_count == _NOMAD_RETRY_MAX_ATTEMPTS
        assert sleep_mock.call_count == _NOMAD_RETRY_MAX_ATTEMPTS - 1

    def test_does_not_retry_on_4xx(self) -> None:
        """4xx HTTPError must propagate immediately — only 5xx are
        retryable per the acceptance criterion."""
        for code in (400, 401, 403, 404, 409):
            call_count = 0

            def _call(target: int = code) -> str:
                def _inner() -> str:
                    nonlocal call_count
                    call_count += 1
                    raise _make_http_error(target)

                return _inner()

            with patch("osimflow.executors.time.sleep") as sleep_mock:
                with pytest.raises(urllib.error.HTTPError) as exc_info:
                    _retry_nomad_request(_call())
            assert exc_info.value.code == code
            assert call_count == 1, f"4xx {code} should not be retried"
            sleep_mock.assert_not_called()

    def test_propagates_unrelated_exceptions(self) -> None:
        """Non-urllib exceptions must propagate immediately without retry."""
        call_count = 0

        def _boom() -> str:
            nonlocal call_count
            call_count += 1
            raise ValueError("not a network error")

        with patch("osimflow.executors.time.sleep") as sleep_mock:
            with pytest.raises(ValueError, match="not a network error"):
                _retry_nomad_request(_boom)

        assert call_count == 1
        sleep_mock.assert_not_called()

    def test_sleep_delay_doubles_and_caps(self) -> None:
        """The sleep delay must grow exponentially and cap at
        ``total_cap_seconds``."""
        delays: list[float] = []

        def _capture_sleep(seconds: float) -> None:
            delays.append(seconds)

        def _always_502() -> str:
            raise _make_http_error(502)

        with patch("osimflow.executors.time.sleep", side_effect=_capture_sleep):
            with patch(
                "osimflow.executors.random.uniform",
                side_effect=lambda lo, hi: hi,
            ):
                with pytest.raises(urllib.error.HTTPError):
                    _retry_nomad_request(_always_502)

        # With random.uniform mocked to return `hi`, the sleeps are the
        # full backoff: 0.5, 1.0, 2.0, 4.0 (4 sleeps between 5 attempts).
        assert delays == pytest.approx([0.5, 1.0, 2.0, 4.0])

    def test_custom_attempts_and_cap(self) -> None:
        """``max_attempts`` and ``total_cap_seconds`` kwargs override defaults."""
        call_count = 0

        def _always_502() -> str:
            nonlocal call_count
            call_count += 1
            raise _make_http_error(502)

        with patch("osimflow.executors.time.sleep") as sleep_mock:
            with pytest.raises(urllib.error.HTTPError):
                _retry_nomad_request(
                    _always_502,
                    max_attempts=3,
                    total_cap_seconds=1.0,
                )

        assert call_count == 3
        # Two sleeps between three attempts.
        assert sleep_mock.call_count == 2


# ---------------------------------------------------------------------------
# _NomadClient._request — end-to-end retry wiring through the public API
# ---------------------------------------------------------------------------
class TestNomadClientRequestRetry:
    def test_transient_502_then_success_returns_parsed_body(self) -> None:
        """Issue #1395 acceptance criterion: a regression test injects a
        transient 502 and asserts the job eventually completes. End-to-end
        version: ``_NomadClient._request`` must absorb a transient 502 and
        return the parsed body from the next attempt."""

        queue: list[object] = [
            _make_http_error(502),
            _make_url_response({"JobID": "osimflow/sample-1", "EvalID": "e1", "Index": 0}),
        ]
        calls: list[object] = []

        def _fake_urlopen(request: object, *args: object, **kwargs: object) -> object:
            if not queue:
                raise AssertionError("executor issued more HTTP calls than expected")
            item = queue.pop(0)
            calls.append(request)
            if isinstance(item, Exception):
                raise item
            return item

        with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
            ex = NomadExecutor(address="http://nomad.local:4646", datacentre="dc1")
            try:
                body = ex._client.submit_job(  # noqa: SLF001
                    {
                        "Job": {
                            "ID": "osimflow/sample-1",
                            "Name": "osimflow/sample-1",
                            "Type": "batch",
                            "Datacenters": ["dc1"],
                            "TaskGroups": [],
                        }
                    }
                )
            finally:
                ex.shutdown()

        assert body == {"JobID": "osimflow/sample-1", "EvalID": "e1", "Index": 0}
        # Two urlopen calls — one 502, one success.
        assert len(calls) == 2

    def test_retry_exhaustion_converts_to_runtime_error(self) -> None:
        """After 5 consecutive 502s, ``_request`` must convert the final
        HTTPError to a RuntimeError (matches the pre-#1395 caller-facing
        contract for HTTP failures)."""

        def _always_502(request: object, *args: object, **kwargs: object) -> object:
            raise _make_http_error(502)

        with patch("urllib.request.urlopen", side_effect=_always_502):
            ex = NomadExecutor(address="http://nomad.local:4646", datacentre="dc1")
            try:
                with pytest.raises(RuntimeError, match="HTTP 502"):
                    ex._client.submit_job(  # noqa: SLF001
                        {
                            "Job": {
                                "ID": "x",
                                "Name": "x",
                                "Type": "batch",
                                "Datacenters": ["dc1"],
                                "TaskGroups": [],
                            }
                        }
                    )
            finally:
                ex.shutdown()

    def test_url_error_converts_to_runtime_error(self) -> None:
        """After 5 consecutive URLErrors, ``_request`` must convert the
        final URLError to a RuntimeError so the caller-facing contract is
        preserved."""

        def _always_urlerr(request: object, *args: object, **kwargs: object) -> object:
            raise urllib.error.URLError("[Errno 111] Connection refused")

        with patch("urllib.request.urlopen", side_effect=_always_urlerr):
            ex = NomadExecutor(address="http://nomad.local:4646", datacentre="dc1")
            try:
                with pytest.raises(RuntimeError, match="Connection refused"):
                    ex._client.submit_job(  # noqa: SLF001
                        {
                            "Job": {
                                "ID": "x",
                                "Name": "x",
                                "Type": "batch",
                                "Datacenters": ["dc1"],
                                "TaskGroups": [],
                            }
                        }
                    )
            finally:
                ex.shutdown()

    def test_4xx_is_not_retried(self) -> None:
        """A 4xx error must propagate after exactly one attempt."""

        call_count = 0

        def _fake_urlopen(request: object, *args: object, **kwargs: object) -> object:
            nonlocal call_count
            call_count += 1
            raise _make_http_error(403)

        with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
            ex = NomadExecutor(address="http://nomad.local:4646", datacentre="dc1")
            try:
                with pytest.raises(RuntimeError, match="HTTP 403"):
                    ex._client.submit_job(  # noqa: SLF001
                        {
                            "Job": {
                                "ID": "x",
                                "Name": "x",
                                "Type": "batch",
                                "Datacenters": ["dc1"],
                                "TaskGroups": [],
                            }
                        }
                    )
            finally:
                ex.shutdown()

        assert call_count == 1


# ---------------------------------------------------------------------------
# Full-flow regression: NomadExecutor.submit → _NomadHandle.result()
# completes despite a transient 502 during allocation polling.
# ---------------------------------------------------------------------------
class TestNomadExecutorEndToEndTransientFailure:
    def test_job_completes_despite_transient_502(self) -> None:
        """Issue #1395 acceptance criterion: a regression test injects a
        transient 502 and asserts the job eventually completes. This test
        drives the full ``submit()`` → ``result()`` path with one 502 on
        the first allocation poll, then a successful ``complete``
        allocation response on the next poll."""

        responses: list[object] = [
            # submit_job → POST /v1/jobs
            {"JobID": "osimflow/sample-2", "EvalID": "e2", "Index": 0},
            # _ensure_allocation_id → GET /v1/evaluation/<id>/allocations
            [{"ID": "alloc-2", "ClientStatus": "pending"}],
            # _wait_for_terminal → first poll: transient 502
            _make_http_error(502),
            # _wait_for_terminal → second poll: complete
            {"ID": "alloc-2", "ClientStatus": "complete", "JobID": "osimflow/sample-2"},
        ]

        def _fake_urlopen(request: object, *args: object, **kwargs: object) -> object:
            if not responses:
                raise AssertionError("executor issued more HTTP calls than expected")
            item = responses.pop(0)
            if isinstance(item, Exception):
                raise item
            return _make_url_response(item)

        with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
            ex = NomadExecutor(
                poll_interval_s=0.01,
                max_poll_interval_s=0.02,
            )
            try:
                handle = ex.submit(lambda: None, name="sample-2")
                # Must not raise despite the transient 502 on poll.
                result = handle.result(timeout=5)
            finally:
                ex.shutdown()

        assert result is None
        assert handle.done() is True
        # 4 wire calls: submit, eval allocs, alloc poll (502→retried),
        # alloc poll (complete).
        assert len(responses) == 0


# ---------------------------------------------------------------------------
# Nomad-server-mode dispatch hardening regression (issue #1387)
# ---------------------------------------------------------------------------
# Mirrors the Kubernetes ``security_context_strict`` hardening from issue
# #1383 / PR #1407: the Nomad dispatch Docker task spec must drop the full
# default Linux capability set, mark the rootfs read-only, and provide
# tmpfs mounts for the per-sample ``remote_runner`` to write into.
# Acceptance criterion from issue #1387: a Nomad-``nominal-server-mode``
# regression test asserts all three keys appear in the dispatched spec.
class TestNomadDispatchSpecHardening:
    """Issue #1387 — Nomad dispatch container hardening.

    Verifies that ``_build_dispatch_job_spec`` produces a Docker task
    config with ``cap_drop = ["ALL"]``, ``read_only = True``, and
    ``tmpfs`` mounts for ``/tmp`` and ``/work``. This is the
    Nomad-substrate analogue of the Kubernetes
    ``security_context_strict=True`` flag (issue #1383, PR #1407).
    """

    def _dispatch_task_config(self) -> dict[str, object]:
        """Build a Nomad dispatch spec and return the Docker task Config block."""
        ex = NomadExecutor(use_dispatch=True)
        try:
            spec = ex._build_dispatch_job_spec()  # noqa: SLF001
        finally:
            ex.shutdown()
        tasks = spec["Job"]["TaskGroups"][0]["Tasks"]
        assert len(tasks) == 1, f"expected one task, got {len(tasks)}"
        return tasks[0]["Config"]

    def test_dispatch_spec_drops_all_capabilities(self) -> None:
        """The dispatch Docker config must set ``cap_drop = ["ALL"]``."""
        config = self._dispatch_task_config()
        cap_drop = config.get("cap_drop")
        assert cap_drop == ["ALL"], (
            f"cap_drop must equal ['ALL'] to drop the full default Linux "
            f"capability set (issue #1387), got {cap_drop!r}"
        )

    def test_dispatch_spec_marks_rootfs_read_only(self) -> None:
        """The dispatch Docker config must set ``read_only = True`` so the
        container rootfs is immutable and the only writable paths live on
        the explicit tmpfs mounts."""
        config = self._dispatch_task_config()
        read_only = config.get("read_only")
        assert read_only is True, f"read_only must be True (issue #1387), got {read_only!r}"

    def test_dispatch_spec_has_tmpfs_mount_for_tmp(self) -> None:
        """The dispatch Docker config must declare a tmpfs mount at ``/tmp``
        so the in-container ``remote_runner`` can stage files."""
        config = self._dispatch_task_config()
        mounts = config.get("mount", [])
        assert isinstance(mounts, list), f"mount must be a list, got {type(mounts).__name__}"
        tmp_mounts = [m for m in mounts if isinstance(m, dict) and m.get("target") == "/tmp"]
        assert tmp_mounts, f"expected a tmpfs mount at /tmp (issue #1387), got mounts={mounts!r}"
        tmp_mount = tmp_mounts[0]
        assert tmp_mount.get("type") == "tmpfs", (
            f"/tmp mount must be tmpfs, got type={tmp_mount.get('type')!r}"
        )
        assert tmp_mount.get("read_only") is False, (
            f"/tmp mount must be writable (read_only=False), got "
            f"read_only={tmp_mount.get('read_only')!r}"
        )
        size = (tmp_mount.get("tmpfs_options") or {}).get("size")
        assert isinstance(size, int) and size > 0, (
            f"/tmp tmpfs_options.size must be a positive int, got {size!r}"
        )

    def test_dispatch_spec_has_tmpfs_mount_for_work_dir(self) -> None:
        """The dispatch Docker config must declare a tmpfs mount at the
        per-sample working directory (``/work``) so the ``remote_runner``
        has a writable scratch path on the read-only rootfs."""
        config = self._dispatch_task_config()
        mounts = config.get("mount", [])
        assert isinstance(mounts, list), f"mount must be a list, got {type(mounts).__name__}"
        work_mounts = [m for m in mounts if isinstance(m, dict) and m.get("target") == "/work"]
        assert work_mounts, f"expected a tmpfs mount at /work (issue #1387), got mounts={mounts!r}"
        work_mount = work_mounts[0]
        assert work_mount.get("type") == "tmpfs", (
            f"/work mount must be tmpfs, got type={work_mount.get('type')!r}"
        )
        assert work_mount.get("read_only") is False, (
            f"/work mount must be writable (read_only=False), got "
            f"read_only={work_mount.get('read_only')!r}"
        )
        size = (work_mount.get("tmpfs_options") or {}).get("size")
        assert isinstance(size, int) and size > 0, (
            f"/work tmpfs_options.size must be a positive int, got {size!r}"
        )

    def test_dispatch_spec_privileged_remains_false(self) -> None:
        """The pre-existing ``privileged = False`` invariant must hold
        after the hardening change (regression guard)."""
        config = self._dispatch_task_config()
        assert config.get("privileged") is False, (
            f"privileged must remain False, got {config.get('privileged')!r}"
        )

    def test_dispatch_spec_hardening_keys_all_present(self) -> None:
        """Aggregated acceptance check: ``cap_drop``, ``read_only``, and a
        non-empty ``mount`` list covering ``/tmp`` and ``/work`` must all
        appear in the same Docker task config."""
        config = self._dispatch_task_config()
        assert config.get("cap_drop") == ["ALL"]
        assert config.get("read_only") is True
        mounts = config.get("mount") or []
        targets = {m.get("target") for m in mounts if isinstance(m, dict)}
        assert {"/tmp", "/work"} <= targets, (
            f"mount targets must include /tmp and /work, got {targets!r}"
        )
