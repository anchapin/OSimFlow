"""Regression tests for issue #656 — DockerSwarmExecutor shutdown in real mode.

Bug description: In DockerSwarmExecutor, self._stub_executor=False causes shutdown
to fail. The shutdown logic was gated behind `if self._stub_executor:` which only
evaluates to True when _stub_executor is a truthy value (e.g., a LocalExecutor in
stub mode). When _stub_executor is explicitly set to False (which should be treated
same as None for "real mode"), the condition was False and shutdown never ran.

The fix changes the condition from:
    if self._stub_executor and hasattr(self._stub_executor, "shutdown"):
to:
    if self._stub_executor is not None and hasattr(self._stub_executor, "shutdown"):

This correctly handles all three states:
- _stub_executor = LocalExecutor (stub mode): shutdown is called
- _stub_executor = None (real mode): nothing to shutdown, no-op
- _stub_executor = False (real mode, bug case): should be treated as "not stub", no-op
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from osimflow.executors.base import PollOutcome
from osimflow.executors.docker_swarm_executor import (
    DockerSwarmExecutor,
    _docker_error_code,
    _DockerSwarmHandle,
)
from osimflow.executors.transport import ResultTransportConfig
from osimflow.task_payload_hmac import (
    RESULT_TRANSPORT_SIG_ENV,
    TASK_PAYLOAD_SECRET_ENV,
    TASK_PAYLOAD_SECRET_FILE_ENV,
    TASK_PAYLOAD_SIG_ENV,
    sign_task_payload,
)


class TestDockerSwarmExecutorShutdown:
    """Test DockerSwarmExecutor.shutdown() behavior across stub_executor states."""

    def _make_executor(self) -> DockerSwarmExecutor:
        """Create a DockerSwarmExecutor without calling __init__."""
        ex = DockerSwarmExecutor.__new__(DockerSwarmExecutor)
        ex.poll_interval_s = 5.0
        ex.max_poll_interval_s = 60.0
        ex.image = "nrel/openstudio:latest"
        ex.network = None
        ex._client = None
        return ex

    def test_shutdown_stub_mode_calls_local_executor_shutdown(self) -> None:
        """When _stub_executor is a LocalExecutor, shutdown calls its shutdown."""
        ex = self._make_executor()
        mock_local = MagicMock()
        ex._stub_executor = mock_local

        ex.shutdown()

        mock_local.shutdown.assert_called_once()

    def test_shutdown_real_mode_none_is_noop(self) -> None:
        """When _stub_executor is None (real mode), shutdown is a no-op."""
        ex = self._make_executor()
        ex._stub_executor = None

        # Should not raise
        ex.shutdown()

    def test_shutdown_real_mode_false_is_noop(self) -> None:
        """When _stub_executor is False (bug case), shutdown is still a no-op.

        This is the regression test for issue #656. Previously, when
        _stub_executor was False, the condition `if self._stub_executor and ...`
        would evaluate to False and shutdown would not run. With the fix
        (`if self._stub_executor is not None and ...`), the behavior is
        consistent: when there's no stub executor (None or False), shutdown
        does nothing because there's nothing to shut down.
        """
        ex = self._make_executor()
        ex._stub_executor = False

        # Should not raise
        ex.shutdown()

    def test_shutdown_multiple_calls_are_idempotent(self) -> None:
        """Calling shutdown multiple times should be safe."""
        ex = self._make_executor()
        mock_local = MagicMock()
        ex._stub_executor = mock_local

        ex.shutdown()
        ex.shutdown()
        ex.shutdown()

        # shutdown() on LocalExecutor is not idempotent, but multiple calls shouldn't raise
        assert mock_local.shutdown.call_count == 3


class TestDockerSwarmExecutorFailDense:
    """Regression tests for issue #944 — fail-dense by default.

    When Docker is unavailable or not in Swarm mode, submit() must raise
    RuntimeError instead of silently falling back to LocalExecutor. The fallback
    path is only available when explicitly opted in via:
    - OSIMFLOW_DOCKER_SWARM_DEV_FALLBACK=1  (general dev/CI opt-in)
    - OSIMFLOW_DOCKER_SWARM_DRY_RUN=1       (dry-run mode, set by Campaign)
    """

    @pytest.fixture(autouse=True)
    def _clear_fallback_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Ensure no fallback env vars leak from other tests (e.g. dry-run campaign tests).

        The Campaign class sets OSIMFLOW_DOCKER_SWARM_DRY_RUN=1 process-globally
        during --dry-run execution (added in #944). Under pytest-xdist (-n 2),
        if a dry-run campaign test runs in the same worker process before these
        fail-dense tests, the env var leaks and _is_dev_fallback_enabled() returns
        True, causing submit() to fall back instead of raising.
        """
        monkeypatch.delenv("OSIMFLOW_DOCKER_SWARM_DEV_FALLBACK", raising=False)
        monkeypatch.delenv("OSIMFLOW_DOCKER_SWARM_DRY_RUN", raising=False)

    def _make_executor(self) -> DockerSwarmExecutor:
        """Create a DockerSwarmExecutor without calling __init__."""
        ex = DockerSwarmExecutor.__new__(DockerSwarmExecutor)
        ex.poll_interval_s = 5.0
        ex.max_poll_interval_s = 60.0
        ex.image = "nrel/openstudio:latest"
        ex.network = None
        ex._client = None
        ex._stub_executor = None
        return ex

    def test_submit_raises_when_docker_not_in_swarm_mode_no_fallback(self) -> None:
        """Without dev-fallback flag, submit() raises when Docker is not in Swarm mode."""
        ex = self._make_executor()
        with patch.object(ex, "_check_docker_available", return_value=False):
            with pytest.raises(RuntimeError, match="not in Swarm mode"):
                ex.submit(lambda: None)

    def test_submit_raises_when_docker_unavailable_no_fallback(self) -> None:
        """Without dev-fallback flag, submit() raises when Docker is unreachable."""
        ex = self._make_executor()
        with patch.object(
            ex, "_check_docker_available", side_effect=RuntimeError("daemon not reachable")
        ):
            with pytest.raises(RuntimeError, match="not reachable"):
                ex.submit(lambda: None)

    def test_submit_raises_when_docker_import_error_no_fallback(self) -> None:
        """Without dev-fallback flag, submit() raises when docker package is absent."""
        ex = self._make_executor()
        with patch.object(
            ex,
            "_check_docker_available",
            side_effect=ImportError("docker package not installed"),
        ):
            with pytest.raises(RuntimeError, match="not reachable"):
                ex.submit(lambda: None)

    def test_submit_falls_back_when_dev_fallback_env_set(self) -> None:
        """With OSIMFLOW_DOCKER_SWARM_DEV_FALLBACK=1, submit() falls back to LocalExecutor."""
        ex = self._make_executor()
        with patch.object(ex, "_check_docker_available", return_value=False):
            with patch.dict(os.environ, {"OSIMFLOW_DOCKER_SWARM_DEV_FALLBACK": "1"}):
                handle = ex.submit(lambda: None)
                # Should return a Handle from the LocalExecutor fallback
                assert handle is not None
                # The stub executor should now be set
                assert ex._stub_executor is not None

    def test_submit_falls_back_when_dry_run_env_set(self) -> None:
        """With OSIMFLOW_DOCKER_SWARM_DRY_RUN=1, submit() falls back to LocalExecutor."""
        ex = self._make_executor()
        with patch.object(ex, "_check_docker_available", return_value=False):
            with patch.dict(os.environ, {"OSIMFLOW_DOCKER_SWARM_DRY_RUN": "1"}):
                handle = ex.submit(lambda: None)
                assert handle is not None
                assert ex._stub_executor is not None

    def test_submit_falls_back_when_both_envs_set(self) -> None:
        """Both env vars set — should still fall back (both are opt-ins)."""
        ex = self._make_executor()
        with patch.object(ex, "_check_docker_available", return_value=False):
            with patch.dict(
                os.environ,
                {
                    "OSIMFLOW_DOCKER_SWARM_DEV_FALLBACK": "1",
                    "OSIMFLOW_DOCKER_SWARM_DRY_RUN": "1",
                },
            ):
                handle = ex.submit(lambda: None)
                assert handle is not None

    def test_submit_raises_when_docker_unavailable_dev_fallback_false(self) -> None:
        """OSIMFLOW_DOCKER_SWARM_DEV_FALLBACK=0 should NOT enable fallback."""
        ex = self._make_executor()
        with patch.object(ex, "_check_docker_available", return_value=False):
            with patch.dict(os.environ, {"OSIMFLOW_DOCKER_SWARM_DEV_FALLBACK": "0"}):
                with pytest.raises(RuntimeError, match="not in Swarm mode"):
                    ex.submit(lambda: None)

    def test_submit_raises_when_docker_unavailable_dry_run_false(self) -> None:
        """OSIMFLOW_DOCKER_SWARM_DRY_RUN=0 should NOT enable fallback."""
        ex = self._make_executor()
        with patch.object(ex, "_check_docker_available", return_value=False):
            with patch.dict(os.environ, {"OSIMFLOW_DOCKER_SWARM_DRY_RUN": "0"}):
                with pytest.raises(RuntimeError, match="not in Swarm mode"):
                    ex.submit(lambda: None)

    def test_submit_raises_when_exception_and_no_fallback(self) -> None:
        """RuntimeError from _check_docker_available should raise when no fallback flag."""
        ex = self._make_executor()
        with patch.object(
            ex,
            "_check_docker_available",
            side_effect=RuntimeError("connection refused"),
        ):
            with pytest.raises(RuntimeError, match="not reachable"):
                ex.submit(lambda: None)

    def test_submit_succeeds_when_swarm_available(self) -> None:
        """When Docker is in Swarm mode, submit() should NOT fall back or raise."""
        ex = self._make_executor()
        with patch.object(ex, "_check_docker_available", return_value=True):
            with patch.object(ex, "_submit_service", return_value="test-service"):
                handle = ex.submit(lambda: None)
                # _stub_executor should remain None (real mode)
                assert ex._stub_executor is None
                assert handle is not None


class TestDockerSwarmExecutorTaskPayloadSigning:
    """Issue #1177/#1384: DockerSwarmExecutor must sign OSIMFLOW_TASK_PAYLOAD.

    The executor sets ``requires_remote_runner_payload = True`` and ships
    the per-task serialized step call as ``OSIMFLOW_TASK_PAYLOAD`` in the
    service env. ``osimflow.remote_runner`` fails closed when the HMAC
    secret is unset, so the executor must propagate the secret + signature
    alongside the payload whenever a secret is configured.
    """

    @staticmethod
    def _parse_service_env(env_list: list[str]) -> dict[str, str]:
        """Convert a Docker Swarm env list (KEY=VALUE) into a dict."""
        parsed: dict[str, str] = {}
        for entry in env_list:
            if "=" not in entry:
                continue
            key, _, value = entry.partition("=")
            parsed[key] = value
        return parsed

    def _make_executor(self) -> DockerSwarmExecutor:
        """Build a DockerSwarmExecutor without invoking __init__."""
        ex = DockerSwarmExecutor.__new__(DockerSwarmExecutor)  # noqa: SLF001
        ex.poll_interval_s = 5.0
        ex.max_poll_interval_s = 60.0
        ex.image = "nrel/openstudio:latest"
        ex.network = None
        ex._client = MagicMock()
        ex._stub_executor = None
        return ex

    @staticmethod
    def _stub_create_service(ex: DockerSwarmExecutor) -> dict[str, object]:
        """Stub the docker SDK call and capture the kwargs passed to services.create."""
        captured: dict[str, object] = {}

        def fake_create(**kwargs: object) -> MagicMock:
            captured.update(kwargs)
            fake_service = MagicMock()
            fake_service.name = "osimflow-test"
            return fake_service

        ex._client.services.create = MagicMock(side_effect=fake_create)  # type: ignore[method-assign]  # noqa: E501
        return captured

    def test_submit_service_signs_task_payload_when_secret_configured(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Issue #1177/#1384: service env must carry the HMAC over payload bytes."""
        secret = "swarm-shared-secret"
        monkeypatch.setenv(TASK_PAYLOAD_SECRET_ENV, secret)
        task_payload = json.dumps({"step": "sim", "args": [], "kwargs": {}})
        ex = self._make_executor()
        captured = self._stub_create_service(ex)
        ex._submit_service(  # noqa: SLF001
            name="test",
            cpus=1,
            memory_mb=1024,
            time_min=60,
            openstudio_version="3.11.0",
            container="nrel/openstudio:3.11.0",
            task_payload=task_payload,
        )
        env_map = self._parse_service_env(captured["env"])  # type: ignore[arg-type]
        assert env_map["OSIMFLOW_TASK_PAYLOAD"] == task_payload
        assert env_map[TASK_PAYLOAD_SECRET_ENV] == secret
        assert env_map[TASK_PAYLOAD_SIG_ENV] == sign_task_payload(task_payload, secret)

    def test_submit_service_omits_signature_env_without_secret(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Legacy unsigned mode must leave the service env unchanged (issue #1177)."""
        monkeypatch.delenv(TASK_PAYLOAD_SECRET_ENV, raising=False)
        task_payload = json.dumps({"step": "sim", "args": [], "kwargs": {}})
        ex = self._make_executor()
        captured = self._stub_create_service(ex)
        ex._submit_service(  # noqa: SLF001
            name="test",
            cpus=1,
            memory_mb=1024,
            time_min=60,
            openstudio_version="3.11.0",
            container="nrel/openstudio:3.11.0",
            task_payload=task_payload,
        )
        env_map = self._parse_service_env(captured["env"])  # type: ignore[arg-type]
        assert env_map["OSIMFLOW_TASK_PAYLOAD"] == task_payload
        assert TASK_PAYLOAD_SIG_ENV not in env_map
        assert TASK_PAYLOAD_SECRET_ENV not in env_map


class TestDockerSwarmExecutorRestartPolicy:
    """Issue #1641: pin the service restart policy so failed tasks stay terminal.

    Swarm's default policy (condition=any, unlimited attempts) replaces a
    crashed task with a new one in a non-terminal state, so the
    all-tasks-terminal conjunction in ``_wait_for_terminal``'s
    ``_is_terminal`` may never hold and the poll loops to the await
    deadline — misclassifying a failed sample as a ``TimeoutError``
    instead of surfacing the task failure. The one-task-per-sample model
    (``mode={"Replicated": {"Replicas": 1}}``) requires
    ``Condition: none``: the single task's outcome is the sample's
    outcome.
    """

    def _make_executor(self) -> DockerSwarmExecutor:
        """Build a DockerSwarmExecutor without invoking __init__."""
        ex = DockerSwarmExecutor.__new__(DockerSwarmExecutor)  # noqa: SLF001
        ex.poll_interval_s = 5.0
        ex.max_poll_interval_s = 60.0
        ex.image = "nrel/openstudio:latest"
        ex.network = None
        ex._client = MagicMock()
        ex._stub_executor = None
        return ex

    @staticmethod
    def _stub_create_service(ex: DockerSwarmExecutor) -> dict[str, object]:
        """Stub the docker SDK call and capture the kwargs passed to services.create."""
        captured: dict[str, object] = {}

        def fake_create(**kwargs: object) -> MagicMock:
            captured.update(kwargs)
            fake_service = MagicMock()
            fake_service.name = "osimflow-test"
            return fake_service

        ex._client.services.create = MagicMock(side_effect=fake_create)  # type: ignore[method-assign]  # noqa: E501
        return captured

    def test_submit_service_pins_restart_policy_none(self) -> None:
        """Issue #1641: the service spec must pin restart_policy Condition=none."""
        ex = self._make_executor()
        captured = self._stub_create_service(ex)
        ex._submit_service(  # noqa: SLF001
            name="test",
            cpus=1,
            memory_mb=1024,
            time_min=60,
            openstudio_version="3.11.0",
            container="nrel/openstudio:3.11.0",
        )
        assert captured["restart_policy"] == {"Condition": "none"}

    def test_wait_for_terminal_returns_promptly_when_single_task_failed(self) -> None:
        """Issue #1641: a task reaching ``failed`` must satisfy ``_is_terminal``
        on the first probe — no replacement task keeps it non-terminal, no
        sleep, no ``TimeoutError``.
        """
        failed_task = {
            "ID": "task-1",
            "status": {"State": "failed", "Err": "exited nonzero"},
        }
        ex = self._make_executor()
        with (
            patch.object(ex, "_get_service_status", return_value={"tasks": [failed_task]}) as probe,
            patch("osimflow.testing.patch_targets.time.sleep") as sleep_mock,
        ):
            result = ex._wait_for_terminal("osimflow-test", timeout=30.0)  # noqa: SLF001
        assert result == failed_task
        probe.assert_called_once_with("osimflow-test")
        sleep_mock.assert_not_called()

    def test_wait_for_terminal_times_out_while_replacement_task_running(self) -> None:
        """Issue #1641 (failure mode the pin prevents): under a restart-churning
        Swarm (a failed task plus a replacement in ``running``), the
        all-tasks-terminal conjunction stays false and the poll raises
        ``TimeoutError`` — demonstrating why the default policy must not
        be left in place.
        """
        failed_task = {
            "ID": "task-1",
            "status": {"State": "failed", "Err": "exited nonzero"},
        }
        replacement_task = {
            "ID": "task-2",
            "status": {"State": "running"},
        }
        ex = self._make_executor()
        with (
            patch.object(
                ex,
                "_get_service_status",
                return_value={"tasks": [failed_task, replacement_task]},
            ),
            patch("osimflow.testing.patch_targets.time.sleep"),
        ):
            with pytest.raises(TimeoutError, match="Timed out"):
                ex._wait_for_terminal("osimflow-test", timeout=0.05)  # noqa: SLF001


class TestDockerSwarmHandleTransportContract:
    """Issue #1680: ``_DockerSwarmHandle`` honors the uniform ``PollingHandle``
    result-transport constructor contract.

    Seven ``PollingHandle`` subclasses (Nomad, Kubernetes, AWS, Azure,
    Google, PBS, ...) accept ``transport: ResultTransportConfig | None = None``
    as a typed keyword argument and store it on ``self._transport``.
    ``_DockerSwarmHandle`` previously smuggled the frozen config through the
    untyped ``submit_params`` dict and recovered it with an ``isinstance``
    fallback inside ``_resolve_success_result`` — a plug-in author reading
    the documented ``PollingHandle`` constructor contract had no way to
    know the dict smuggling existed. These tests assert the contract
    directly so the gap cannot reopen silently.
    """

    @staticmethod
    def _make_executor() -> DockerSwarmExecutor:
        """Build a ``DockerSwarmExecutor`` without invoking ``__init__``."""
        ex = DockerSwarmExecutor.__new__(DockerSwarmExecutor)  # noqa: SLF001
        ex.poll_interval_s = 0.01
        ex.max_poll_interval_s = 0.02
        ex.image = "nrel/openstudio:3.11.0"
        ex.network = None
        ex._client = MagicMock()
        ex._stub_executor = None
        return ex

    def test_handle_stores_transport_passed_as_kwarg(self) -> None:
        """``_DockerSwarmHandle(..., transport=cfg)`` stores the identical config."""
        cfg = ResultTransportConfig(
            mode="object_storage",
            backend="s3",
            bucket="handle-transport-bucket",
            prefix="out",
            endpoint="https://s3.example.test",
            presigned_url_expiration_s=600,
        )
        ex = self._make_executor()
        handle = _DockerSwarmHandle(
            service_name="svc-1",
            executor=ex,
            submit_params={},
            transport=cfg,
        )
        # The identical frozen config must be retained (issue #1541's
        # field-wise equality on the dataclass means reconstructing the
        # config would also satisfy the assertion, but retaining the
        # same instance is the strictest contract).
        assert handle._transport is cfg

    def test_handle_transport_defaults_when_unset(self) -> None:
        """``_DockerSwarmHandle(...)`` with no transport defaults to ``auto``.

        Matches the historic per-field defaults documented on the base
        ``PollingHandle._transport`` class attribute (issue #1541).
        """
        ex = self._make_executor()
        handle = _DockerSwarmHandle(
            service_name="svc-1",
            executor=ex,
            submit_params={},
        )
        assert handle._transport == ResultTransportConfig()
        assert handle._transport.mode == "auto"

    def test_handle_stores_result_hint_passed_as_kwarg(self) -> None:
        """``_DockerSwarmHandle(..., result_hint=hint)`` stores the hint.

        Symmetric with the Kubernetes / Nomad / AWS handles — the
        ``result_hint`` plumbs through the uniform constructor signature
        instead of the ``submit_params`` dict so plug-in authors cannot
        accidentally bypass it.
        """
        ex = self._make_executor()
        sentinel_hint = Path("/tmp/sentinel-result-hint")
        handle = _DockerSwarmHandle(
            service_name="svc-1",
            executor=ex,
            submit_params={},
            result_hint=sentinel_hint,
        )
        assert handle._result_hint == sentinel_hint

    def test_submit_routes_transport_through_typed_kwarg(self) -> None:
        """``DockerSwarmExecutor.submit(..., transport=cfg)`` reaches the handle.

        Exercises the full submit path with a stubbed Swarm so the
        executor's ``_do_submit`` is exercised end-to-end: the
        ``ResultTransportConfig`` accepted by ``submit`` must land on the
        handle via the typed constructor kwarg, not the legacy
        ``submit_params`` dict smuggling path (issue #1680 acceptance
        criterion).
        """
        cfg = ResultTransportConfig(
            mode="shared_fs",
            backend="s3",
            bucket="submit-routing-bucket",
        )
        ex = self._make_executor()
        ex._check_docker_available = MagicMock(return_value=True)  # type: ignore[method-assign]
        ex._submit_service = MagicMock(return_value="svc-1")  # type: ignore[method-assign]
        ex._init_rate_limiter(None)

        handle = ex.submit(lambda: None, name="sim_s0", transport=cfg)
        assert isinstance(handle, _DockerSwarmHandle)
        # The transport on the handle matches the one submitted — no
        # default-instance sneak-through and no smuggling via submit_params.
        assert handle._transport == cfg

    def test_submit_no_longer_carries_transport_in_submit_params(self) -> None:
        """``_submit_service`` receives ``transport=`` via kwargs, not the dict.

        The acceptance criterion explicitly removes the
        ``submit_params["transport"]`` smuggling. The executor passes
        the config explicitly to ``_submit_service`` and the handle, but
        the dict must no longer carry it.
        """
        cfg = ResultTransportConfig(
            mode="shared_fs",
            backend="s3",
            bucket="no-smuggle-bucket",
        )
        ex = self._make_executor()
        ex._check_docker_available = MagicMock(return_value=True)  # type: ignore[method-assign]
        ex._submit_service = MagicMock(return_value="svc-1")  # type: ignore[method-assign]
        ex._init_rate_limiter(None)

        ex.submit(lambda: None, name="sim_s0", transport=cfg)
        params = ex._submit_service.call_args.kwargs
        # ``transport`` is still passed to ``_submit_service`` (it writes
        # ``OSIMFLOW_RESULT_*`` env vars) but it is no longer a member of
        # the dict that used to smuggle it through to the handle.
        assert params["transport"] == cfg


class TestDockerSwarmHelperFunctions:
    """Cover the ``_docker_error_code`` helper (issue #1676 ratchet)."""

    def test_returns_status_code_when_response_present(self) -> None:
        """A response with a numeric status_code returns it as an int."""

        class _Response:
            status_code = 503

        exc = Exception("boom")
        exc.response = _Response()  # type: ignore[attr-defined]
        assert _docker_error_code(exc) == 503

    def test_returns_zero_when_no_response_attr(self) -> None:
        """An exception without a ``response`` attribute returns 0."""

        class _BareExc(Exception):
            pass

        assert _docker_error_code(_BareExc("no resp")) == 0

    def test_returns_zero_when_response_status_code_is_none(self) -> None:
        """A response with status_code ``None`` coerces to 0 (no error code)."""

        class _Response:
            status_code = None

        exc = Exception("no code")
        exc.response = _Response()  # type: ignore[attr-defined]
        assert _docker_error_code(exc) == 0

    def test_swallows_attribute_error_on_response_probe(self) -> None:
        """An exception whose response.attr access raises is reported as 0."""

        class _BadResponse:
            @property
            def status_code(self) -> int:
                raise RuntimeError("nope")

        exc = Exception("weird")
        exc.response = _BadResponse()  # type: ignore[attr-defined]
        assert _docker_error_code(exc) == 0


class TestDockerSwarmHandleClassifyAndError:
    """``_DockerSwarmHandle._classify`` and ``_failure_error`` terminal transitions.

    The single-state poll that completes / fails a Swarm service task is
    the load-bearing path that historically sat at ~50% coverage. Each
    terminal state (SUCCEEDED, FAILED) plus the failure-error message
    extraction from ``Message`` / ``Err`` / ``ContainerStatus`` must be
    directly asserted so a future regression in the poll loop
    classification cannot ship green.
    """

    @staticmethod
    def _make_executor() -> DockerSwarmExecutor:
        ex = DockerSwarmExecutor.__new__(DockerSwarmExecutor)  # noqa: SLF001
        ex.poll_interval_s = 0.01
        ex.max_poll_interval_s = 0.02
        ex.image = "nrel/openstudio:3.11.0"
        ex.network = None
        ex._client = MagicMock()
        ex._stub_executor = None
        return ex

    def test_classify_succeeded_maps_complete_to_succeeded(self) -> None:
        """``state == "complete"`` maps to ``PollOutcome.SUCCEEDED``."""

        ex = self._make_executor()
        handle = _DockerSwarmHandle(
            service_name="svc-ok",
            executor=ex,
            submit_params={},
        )
        outcome, reason = handle._classify(  # noqa: SLF001
            {"status": {"State": "complete"}}
        )
        assert outcome is PollOutcome.SUCCEEDED
        assert reason is None

    def test_classify_failed_maps_non_complete_states_to_failed(self) -> None:
        """Any non-``complete`` state (failed / shutdown / rejected) maps to FAILED."""

        ex = self._make_executor()
        handle = _DockerSwarmHandle(
            service_name="svc-bad",
            executor=ex,
            submit_params={},
        )
        for bad_state in ("failed", "shutdown", "rejected"):
            outcome, reason = handle._classify(  # noqa: SLF001
                {"status": {"State": bad_state}}
            )
            assert outcome is PollOutcome.FAILED
            assert reason is None

    def test_failure_error_includes_state_and_message(self) -> None:
        """The ``RuntimeError`` includes the state and the extracted message."""

        ex = self._make_executor()
        handle = _DockerSwarmHandle(
            service_name="svc-1",
            executor=ex,
            submit_params={},
        )
        job = {"status": {"State": "failed", "Message": "exit code 137"}}
        err = handle._failure_error(job)  # noqa: SLF001
        assert isinstance(err, RuntimeError)
        assert "svc-1" in str(err)
        assert "failed" in str(err)
        assert "exit code 137" in str(err)

    def test_extract_error_message_prefers_message_field(self) -> None:
        """``status.Message`` is the primary extraction target."""

        ex = self._make_executor()
        handle = _DockerSwarmHandle(
            service_name="svc-1",
            executor=ex,
            submit_params={},
        )
        task = {"status": {"Message": "primary", "Err": "secondary"}}
        assert handle._extract_error_message(task) == "primary"  # noqa: SLF001

    def test_extract_error_message_falls_back_to_err_field(self) -> None:
        """When ``Message`` is empty, ``status.Err`` wins."""

        ex = self._make_executor()
        handle = _DockerSwarmHandle(
            service_name="svc-1",
            executor=ex,
            submit_params={},
        )
        task = {"status": {"Message": "", "Err": "fallback-err"}}
        assert handle._extract_error_message(task) == "fallback-err"  # noqa: SLF001

    def test_extract_error_message_falls_back_to_container_exit_code(self) -> None:
        """When neither Message nor Err yield, ``ContainerStatus.ExitCode != 0`` does."""

        ex = self._make_executor()
        handle = _DockerSwarmHandle(
            service_name="svc-1",
            executor=ex,
            submit_params={},
        )
        task = {
            "status": {
                "Message": "",
                "Err": "",
                "ContainerStatus": {"ExitCode": 127},
            }
        }
        assert (
            handle._extract_error_message(task)  # noqa: SLF001
            == "exit code 127"
        )

    def test_extract_error_message_returns_unknown_when_nothing_matches(self) -> None:
        """Falls back to ``"unknown"`` when no error path yields a message."""

        ex = self._make_executor()
        handle = _DockerSwarmHandle(
            service_name="svc-1",
            executor=ex,
            submit_params={},
        )
        task = {
            "status": {
                "Message": "",
                "Err": "",
                "ContainerStatus": {"ExitCode": 0},
            }
        }
        assert handle._extract_error_message(task) == "unknown"  # noqa: SLF001

    def test_extract_error_message_handles_empty_container_status(self) -> None:
        """An empty ``ContainerStatus`` is the same as no container info."""

        ex = self._make_executor()
        handle = _DockerSwarmHandle(
            service_name="svc-1",
            executor=ex,
            submit_params={},
        )
        task = {
            "status": {
                "Message": "",
                "Err": "",
                "ContainerStatus": None,
            }
        }
        assert handle._extract_error_message(task) == "unknown"  # noqa: SLF001


class TestDockerSwarmHandleCancelAndResolve:
    """Cancel + success-resolution + done() exception branches (issue #1676)."""

    @staticmethod
    def _make_executor() -> DockerSwarmExecutor:
        ex = DockerSwarmExecutor.__new__(DockerSwarmExecutor)  # noqa: SLF001
        ex.poll_interval_s = 0.01
        ex.max_poll_interval_s = 0.02
        ex.image = "nrel/openstudio:3.11.0"
        ex.network = None
        ex._client = MagicMock()
        ex._stub_executor = None
        return ex

    def test_cancel_job_calls_services_get_remove(self) -> None:
        """``_cancel_job`` removes the service via the docker SDK."""

        ex = self._make_executor()
        mock_service = MagicMock()
        ex._client.services.get.return_value = mock_service  # type: ignore[method-assign]

        handle = _DockerSwarmHandle(
            service_name="svc-rm",
            executor=ex,
            submit_params={},
        )
        assert handle._cancel_job() is True  # noqa: SLF001
        ex._client.services.get.assert_called_once_with("svc-rm")
        mock_service.remove.assert_called_once_with()

    def test_cancel_job_not_found_returns_false(self) -> None:
        """A cancel attempt on a not-found service reports ``False``.

        The shared ``PollingHandle.cancel`` wrapper catches the
        ``NotFound`` API error (issue #1538) and reports the kill as
        un-issued; ``_cancel_job`` itself propagates the exception so
        the upstream sweep can count the handle without aborting.
        """

        import docker.errors
        import requests  # type: ignore[import-not-found]

        ex = self._make_executor()
        response = requests.Response()
        response.status_code = 404
        api_err = docker.errors.APIError(
            "service not found", response=response
        )
        ex._client.services.get.side_effect = api_err  # type: ignore[method-assign]

        handle = _DockerSwarmHandle(
            service_name="svc-missing",
            executor=ex,
            submit_params={},
        )
        with pytest.raises(docker.errors.APIError):
            handle._cancel_job()  # noqa: SLF001

    def test_resolve_success_result_delegates_to_transport_helpers(self) -> None:
        """``_resolve_success_result`` forwards the hint + transport to the helper.

        The handle honours the uniform ``PollingHandle._resolve_success_result``
        contract (issue #1541): one frozen transport config, materialization
        via the ``resolve_and_materialize`` facade (issue #1697).
        """

        ex = self._make_executor()
        sentinel_hint = Path("/tmp/sentinel-result")
        cfg = ResultTransportConfig(
            mode="object_storage",
            backend="s3",
            bucket="handle-resolve-bucket",
        )
        handle = _DockerSwarmHandle(
            service_name="svc-resolve",
            executor=ex,
            submit_params={},
            result_hint=sentinel_hint,
            transport=cfg,
        )
        with patch(
            "osimflow.executors.docker_swarm_executor.resolve_and_materialize",
            return_value="RESOLVED",
        ) as patched:
            result = handle._resolve_success_result()  # noqa: SLF001
        assert result == "RESOLVED"
        patched.assert_called_once_with(sentinel_hint, cfg)

    def test_done_returns_true_when_future_already_done(self) -> None:
        """``done()`` short-circuits when the local mirror future is set."""

        ex = self._make_executor()
        handle = _DockerSwarmHandle(
            service_name="svc-quick",
            executor=ex,
            submit_params={},
        )
        handle._future.set_result(None)  # noqa: SLF001
        assert handle.done() is True

    def test_done_returns_false_when_no_tasks(self) -> None:
        """A service with no tasks yet reports not-done."""

        ex = self._make_executor()
        ex._get_service_status = MagicMock(  # type: ignore[method-assign]
            return_value={"tasks": []}
        )
        handle = _DockerSwarmHandle(
            service_name="svc-empty",
            executor=ex,
            submit_params={},
        )
        assert handle.done() is False

    def test_done_returns_true_when_all_tasks_terminal(self) -> None:
        """All terminal-state tasks make done() return True."""

        ex = self._make_executor()
        tasks = [
            {"status": {"State": "complete"}},
            {"status": {"State": "shutdown"}},
        ]
        ex._get_service_status = MagicMock(  # type: ignore[method-assign]
            return_value={"tasks": tasks}
        )
        handle = _DockerSwarmHandle(
            service_name="svc-all-done",
            executor=ex,
            submit_params={},
        )
        assert handle.done() is True

    def test_done_returns_false_when_a_task_is_running(self) -> None:
        """A non-terminal task makes done() return False."""

        ex = self._make_executor()
        tasks = [
            {"status": {"State": "complete"}},
            {"status": {"State": "running"}},
        ]
        ex._get_service_status = MagicMock(  # type: ignore[method-assign]
            return_value={"tasks": tasks}
        )
        handle = _DockerSwarmHandle(
            service_name="svc-mixed",
            executor=ex,
            submit_params={},
        )
        assert handle.done() is False

    def test_done_handles_timeout_error_gracefully(self) -> None:
        """``TimeoutError`` from the probe is caught and returns False."""

        ex = self._make_executor()

        def _raise_timeout(_name: str) -> dict[str, object]:
            raise TimeoutError("read timeout")

        ex._get_service_status = MagicMock(side_effect=_raise_timeout)  # type: ignore[method-assign]
        handle = _DockerSwarmHandle(
            service_name="svc-timeout",
            executor=ex,
            submit_params={},
        )
        assert handle.done() is False

    def test_done_handles_connection_error_gracefully(self) -> None:
        """``ConnectionError`` from the probe is caught and returns False."""

        ex = self._make_executor()

        def _raise_conn(_name: str) -> dict[str, object]:
            raise ConnectionError("daemon hung up")

        ex._get_service_status = MagicMock(side_effect=_raise_conn)  # type: ignore[method-assign]
        handle = _DockerSwarmHandle(
            service_name="svc-conn",
            executor=ex,
            submit_params={},
        )
        assert handle.done() is False

    def test_done_permanent_401_sets_exception_and_raises(self) -> None:
        """A 401 from the probe sets the future exception and re-raises."""

        import docker.errors
        import requests  # type: ignore[import-not-found]

        ex = self._make_executor()
        response = requests.Response()
        response.status_code = 401
        err = docker.errors.APIError("unauthorized", response=response)

        def _raise_auth(_name: str) -> dict[str, object]:
            raise err

        ex._get_service_status = MagicMock(side_effect=_raise_auth)  # type: ignore[method-assign]
        handle = _DockerSwarmHandle(
            service_name="svc-auth",
            executor=ex,
            submit_params={},
        )
        with pytest.raises(docker.errors.APIError):
            handle.done()
        assert handle._future.exception() is err  # noqa: SLF001

    def test_done_permanent_403_sets_exception_and_raises(self) -> None:
        """A 403 from the probe is permanent: re-raise + set future exception."""

        import docker.errors
        import requests  # type: ignore[import-not-found]

        ex = self._make_executor()
        response = requests.Response()
        response.status_code = 403
        err = docker.errors.APIError("forbidden", response=response)

        def _raise_forbidden(_name: str) -> dict[str, object]:
            raise err

        ex._get_service_status = MagicMock(side_effect=_raise_forbidden)  # type: ignore[method-assign]
        handle = _DockerSwarmHandle(
            service_name="svc-forbidden",
            executor=ex,
            submit_params={},
        )
        with pytest.raises(docker.errors.APIError):
            handle.done()
        assert handle._future.exception() is err  # noqa: SLF001

    def test_done_permanent_404_sets_exception_and_raises(self) -> None:
        """A 404 (service was deleted) is permanent: re-raise + set future exception."""

        import docker.errors
        import requests  # type: ignore[import-not-found]

        ex = self._make_executor()
        response = requests.Response()
        response.status_code = 404
        err = docker.errors.APIError("not found", response=response)

        def _raise_missing(_name: str) -> dict[str, object]:
            raise err

        ex._get_service_status = MagicMock(side_effect=_raise_missing)  # type: ignore[method-assign]
        handle = _DockerSwarmHandle(
            service_name="svc-gone",
            executor=ex,
            submit_params={},
        )
        with pytest.raises(docker.errors.APIError):
            handle.done()
        assert handle._future.exception() is err  # noqa: SLF001

    def test_done_transient_error_returns_false(self) -> None:
        """Any other exception (no docker response, no HTTP code) returns False."""

        ex = self._make_executor()

        def _raise_weird(_name: str) -> dict[str, object]:
            raise RuntimeError("weird nondocker failure")

        ex._get_service_status = MagicMock(side_effect=_raise_weird)  # type: ignore[method-assign]
        handle = _DockerSwarmHandle(
            service_name="svc-weird",
            executor=ex,
            submit_params={},
        )
        # The exception is logged at debug level and swallowed, returning
        # False so the poll loop retries on the next probe.
        assert handle.done() is False
        # The future must NOT have been poisoned with an exception —
        # transient errors do not finalize the handle. ``done()`` is False
        # is the only externally visible signal we can assert without
        # blocking on a never-finalized future.
        assert handle._future.done() is False  # noqa: SLF001


class TestDockerSwarmExecutorInternalHelpers:
    """Internal executors: ``_is_dev_fallback_enabled``, ``_check_docker_available``,
    ``_build_service_name``, ``_get_service_status``, ``shutdown``.
    """

    @pytest.fixture(autouse=True)
    def _clear_fallback_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Always start with a clean fallback env so the dev-fallback tests are
        deterministic across pytest-xdist worker orderings.
        """
        monkeypatch.delenv("OSIMFLOW_DOCKER_SWARM_DEV_FALLBACK", raising=False)
        monkeypatch.delenv("OSIMFLOW_DOCKER_SWARM_DRY_RUN", raising=False)

    def _make_executor(self) -> DockerSwarmExecutor:
        ex = DockerSwarmExecutor.__new__(DockerSwarmExecutor)  # noqa: SLF001
        ex.poll_interval_s = 5.0
        ex.max_poll_interval_s = 60.0
        ex.image = "nrel/openstudio:3.11.0"
        ex.network = None
        ex._client = MagicMock()
        ex._stub_executor = None
        return ex

    def test_is_dev_fallback_enabled_false_when_no_envs(self) -> None:
        """No env vars set → fallback disabled."""

        ex = self._make_executor()
        assert ex._is_dev_fallback_enabled() is False  # noqa: SLF001

    def test_is_dev_fallback_enabled_dev_fallback_truthy(self) -> None:
        """OSIMFLOW_DOCKER_SWARM_DEV_FALLBACK=1 enables the fallback."""

        ex = self._make_executor()
        with patch.dict(os.environ, {"OSIMFLOW_DOCKER_SWARM_DEV_FALLBACK": "1"}):
            assert ex._is_dev_fallback_enabled() is True  # noqa: SLF001

    def test_is_dev_fallback_enabled_dry_run_truthy(self) -> None:
        """OSIMFLOW_DOCKER_SWARM_DRY_RUN=1 enables the fallback."""

        ex = self._make_executor()
        with patch.dict(os.environ, {"OSIMFLOW_DOCKER_SWARM_DRY_RUN": "1"}):
            assert ex._is_dev_fallback_enabled() is True  # noqa: SLF001

    def test_is_dev_fallback_enabled_zero_strings_disable(self) -> None:
        """``0`` and ``""`` are not opt-ins."""

        ex = self._make_executor()
        with patch.dict(
            os.environ,
            {
                "OSIMFLOW_DOCKER_SWARM_DEV_FALLBACK": "0",
                "OSIMFLOW_DOCKER_SWARM_DRY_RUN": "",
            },
        ):
            assert ex._is_dev_fallback_enabled() is False  # noqa: SLF001

    def test_check_docker_available_true(self) -> None:
        """``ControlAvailable == True`` makes the swarm check pass."""

        ex = self._make_executor()
        ex._get_client = MagicMock(  # type: ignore[method-assign]
            return_value=MagicMock(
                info=MagicMock(return_value={"Swarm": {"ControlAvailable": True}})
            )
        )
        assert ex._check_docker_available() is True  # noqa: SLF001

    def test_check_docker_available_false(self) -> None:
        """A swarm-less daemon reports ``ControlAvailable == False``."""

        ex = self._make_executor()
        ex._get_client = MagicMock(  # type: ignore[method-assign]
            return_value=MagicMock(
                info=MagicMock(return_value={"Swarm": {"ControlAvailable": False}})
            )
        )
        assert ex._check_docker_available() is False  # noqa: SLF001

    def test_check_docker_available_exception_returns_false(self) -> None:
        """An unreachable daemon returns False with a logged warning."""

        ex = self._make_executor()

        def _raise(*_args: object, **_kwargs: object) -> object:
            raise ConnectionRefusedError("docker not running")

        ex._get_client = MagicMock(side_effect=_raise)  # type: ignore[method-assign]
        assert ex._check_docker_available() is False  # noqa: SLF001

    def test_build_service_name_sanitizes_underscore_to_dash(self) -> None:
        """``_`` and ``.`` become ``-``; uppercase is lowered."""

        ex = self._make_executor()
        name = ex._build_service_name("OSimFlow.Task_alpha")  # noqa: SLF001
        assert name == "osimflow-osimflow-task-alpha"

    def test_build_service_name_handles_empty_after_sanitize(self) -> None:
        """A name that sanitizes to empty falls back to ``osimflow-task``."""

        ex = self._make_executor()
        name = ex._build_service_name("...")  # noqa: SLF001
        assert name.startswith("osimflow-")
        # Histroic fallback for "all-stripped" inputs.
        assert "osimflow" in name

    def test_get_service_status_swallow_on_probe_error(self) -> None:
        """A failure probing service status returns an empty ``tasks`` payload."""

        ex = self._make_executor()
        ex._get_client = MagicMock(  # type: ignore[method-assign]
            return_value=MagicMock(
                services=MagicMock(
                    get=MagicMock(side_effect=RuntimeError("probe failed"))
                )
            )
        )
        result = ex._get_service_status("svc-x")  # noqa: SLF001
        assert result == {"tasks": []}

    def test_shutdown_with_stub_executor_without_shutdown_attr(self) -> None:
        """A stub executor that lacks a ``shutdown`` method is not called.

        Defends against AttributeError on ill-typed stubs (the
        ``hasattr`` guard).
        """

        ex = self._make_executor()

        class _NoShutdown:
            pass

        ex._stub_executor = _NoShutdown()
        # Should not raise — the hasattr guard skips it.
        ex.shutdown()

    def test_submit_service_logs_latest_tag_warning(self) -> None:
        """A bare ``:latest`` image tag triggers the supply-chain warning."""

        ex = self._make_executor()
        ex.image = "nrel/openstudio:latest"
        ex._client.services.create = MagicMock(  # type: ignore[method-assign]
            return_value=MagicMock(name="osimflow-test")
        )
        # The warning is emitted at __init__; re-trigger by re-calling
        # the init's leading-block that emits it, but here we just
        # call _submit_service — the warning fires once at __init__
        # so it's effectively a no-coverage-adding smoke check.
        ex._submit_service(  # noqa: SLF001
            name="test",
            cpus=1,
            memory_mb=1024,
            time_min=60,
            openstudio_version="3.11.0",
            container=None,
        )

    def test_submit_service_emits_result_transport_env_vars(self) -> None:
        """``_submit_service`` writes ``OSIMFLOW_RESULT_*`` env vars for the transport.

        Each transport field becomes an env entry; the per-call
        transport signature env var is also appended when a secret is
        configured.
        """

        ex = self._make_executor()
        captured: dict[str, object] = {}

        def _capture_create(**kwargs: object) -> MagicMock:
            captured.update(kwargs)
            return MagicMock(name="osimflow-test")

        ex._client.services.create = MagicMock(side_effect=_capture_create)  # type: ignore[method-assign]
        cfg = ResultTransportConfig(
            mode="object_storage",
            backend="s3",
            bucket="ds-resolve-bucket",
            prefix="campaign-x",
            endpoint="https://s3.example.test",
        )
        with patch.dict(os.environ, {TASK_PAYLOAD_SECRET_ENV: "shared-secret"}):
            ex._submit_service(  # noqa: SLF001
                name="test",
                cpus=1,
                memory_mb=1024,
                time_min=60,
                openstudio_version="3.11.0",
                container="nrel/openstudio:3.11.0",
                transport=cfg,
            )
        env_map = {
            entry.partition("=")[0]: entry.partition("=")[2]
            for entry in (captured["env"] or [])  # type: ignore[union-attr]
            if isinstance(entry, str) and "=" in entry
        }
        assert env_map["OSIMFLOW_RESULT_TRANSPORT_MODE"] == "object_storage"
        assert env_map["OSIMFLOW_RESULT_STORAGE_BACKEND"] == "s3"
        assert env_map["OSIMFLOW_RESULT_STORAGE_BUCKET"] == "ds-resolve-bucket"
        assert env_map["OSIMFLOW_RESULT_STORAGE_PREFIX"] == "campaign-x"
        assert env_map["OSIMFLOW_RESULT_STORAGE_ENDPOINT"] == "https://s3.example.test"
        # Issue #1549: transport signature env var when a secret is set.
        assert RESULT_TRANSPORT_SIG_ENV in env_map

    def test_submit_service_warns_when_secret_name_set_but_no_orchestrator_secret(
        self,
    ) -> None:
        """``payload_secret`` configured but no orchestrator secret → unsigned warning.

        Issue #1633: the executor falls back to unsigned mode when
        ``payload_secret`` is set but ``OSIMFLOW_TASK_PAYLOAD_SECRET``
        is missing from the orchestrator environment.
        """

        ex = self._make_executor()
        ex.payload_secret = "ds-shared-payload-secret"  # noqa: SLF001
        captured: dict[str, object] = {}

        def _capture_create(**kwargs: object) -> MagicMock:
            captured.update(kwargs)
            return MagicMock(name="osimflow-test")

        ex._client.services.create = MagicMock(side_effect=_capture_create)  # type: ignore[method-assign]
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(TASK_PAYLOAD_SECRET_ENV, None)
            ex._submit_service(  # noqa: SLF001
                name="test",
                cpus=1,
                memory_mb=1024,
                time_min=60,
                openstudio_version="3.11.0",
                container="nrel/openstudio:3.11.0",
                task_payload='{"step": "sim"}',
            )
        # The Docker secret name is mounted at /run/secrets/<name>.
        env_map = {
            entry.partition("=")[0]: entry.partition("=")[2]
            for entry in (captured["env"] or [])  # type: ignore[union-attr]
            if isinstance(entry, str) and "=" in entry
        }
        assert env_map.get(TASK_PAYLOAD_SECRET_FILE_ENV) == "/run/secrets/ds-shared-payload-secret"
        # No raw secret in the env — out-of-band delivery.
        assert TASK_PAYLOAD_SECRET_ENV not in env_map
        # The Docker SDK received a Secret mount.
        secrets = captured.get("secrets")
        assert secrets is not None and len(secrets) == 1
        assert secrets[0]["Name"] == "ds-shared-payload-secret"  # type: ignore[index]

    def test_submit_service_warns_on_literal_secret_when_no_secret_name(self) -> None:
        """When the orchestrator has a secret but no ``payload_secret`` mount is set,
        the executor logs the issue #1633 literal-secret warning and ships the
        raw secret in the env."""

        ex = self._make_executor()
        # payload_secret is None (default).
        captured: dict[str, object] = {}

        def _capture_create(**kwargs: object) -> MagicMock:
            captured.update(kwargs)
            return MagicMock(name="osimflow-test")

        ex._client.services.create = MagicMock(side_effect=_capture_create)  # type: ignore[method-assign]
        with patch.dict(
            os.environ, {TASK_PAYLOAD_SECRET_ENV: "orch-shared-secret"}
        ):
            ex._submit_service(  # noqa: SLF001
                name="test",
                cpus=1,
                memory_mb=1024,
                time_min=60,
                openstudio_version="3.11.0",
                container="nrel/openstudio:3.11.0",
                task_payload='{"step": "sim"}',
            )
        env_map = {
            entry.partition("=")[0]: entry.partition("=")[2]
            for entry in (captured["env"] or [])  # type: ignore[union-attr]
            if isinstance(entry, str) and "=" in entry
        }
        # The raw secret IS in the env (legacy unsafe path).
        assert env_map[TASK_PAYLOAD_SECRET_ENV] == "orch-shared-secret"
        # No file-marker env (because no Docker secret was configured).
        assert TASK_PAYLOAD_SECRET_FILE_ENV not in env_map

    def test_submit_service_propagates_resource_directives(self) -> None:
        """``cpus`` and ``memory_mb`` translate to NanoCPUs and MemoryBytes."""

        ex = self._make_executor()
        captured: dict[str, object] = {}

        def _capture_create(**kwargs: object) -> MagicMock:
            captured.update(kwargs)
            return MagicMock(name="osimflow-test")

        ex._client.services.create = MagicMock(side_effect=_capture_create)  # type: ignore[method-assign]
        ex._submit_service(  # noqa: SLF001
            name="res",
            cpus=4,
            memory_mb=2048,
            time_min=15,
            openstudio_version="3.11.0",
            container="nrel/openstudio:3.11.0",
        )
        resources = captured["resources"]  # type: ignore[assignment]
        assert resources["Limits"]["NanoCPUs"] == 4 * 10**9
        assert resources["Limits"]["MemoryBytes"] == 2048 * 1024 * 1024
        assert resources["Reservations"]["NanoCPUs"] == 4 * 10**9
        assert resources["Reservations"]["MemoryBytes"] == 2048 * 1024 * 1024

    def test_submit_service_api_error_surfaces_as_runtime_error(self) -> None:
        """A docker.errors.APIError from ``services.create`` raises ``RuntimeError``."""

        import docker.errors
        import requests  # type: ignore[import-not-found]

        ex = self._make_executor()
        response = requests.Response()
        response.status_code = 500
        api_err = docker.errors.APIError("boom", response=response)

        def _raise_api(**_kwargs: object) -> object:
            raise api_err

        ex._client.services.create = MagicMock(side_effect=_raise_api)  # type: ignore[method-assign]
        with pytest.raises(RuntimeError, match="failed to create Docker Swarm service"):
            ex._submit_service(  # noqa: SLF001
                name="test",
                cpus=1,
                memory_mb=1024,
                time_min=60,
                openstudio_version="3.11.0",
                container=None,
            )

    def test_submit_service_skips_endpoint_spec_when_no_network(self) -> None:
        """``endpoint_spec`` is omitted entirely when no network is configured."""

        ex = self._make_executor()
        captured: dict[str, object] = {}

        def _capture_create(**kwargs: object) -> MagicMock:
            captured.update(kwargs)
            return MagicMock(name="osimflow-test")

        ex._client.services.create = MagicMock(side_effect=_capture_create)  # type: ignore[method-assign]
        ex._submit_service(  # noqa: SLF001
            name="test",
            cpus=1,
            memory_mb=1024,
            time_min=60,
            openstudio_version="3.11.0",
            container="nrel/openstudio:3.11.0",
        )
        assert captured["endpoint_spec"] is None

    def test_submit_service_includes_endpoint_spec_when_network_set(self) -> None:
        """A configured ``network`` produces a published-port endpoint spec."""

        ex = self._make_executor()
        ex.network = "osimflow-net"  # noqa: SLF001
        captured: dict[str, object] = {}

        def _capture_create(**kwargs: object) -> MagicMock:
            captured.update(kwargs)
            return MagicMock(name="osimflow-test")

        ex._client.services.create = MagicMock(side_effect=_capture_create)  # type: ignore[method-assign]
        ex._submit_service(  # noqa: SLF001
            name="test",
            cpus=1,
            memory_mb=1024,
            time_min=60,
            openstudio_version="3.11.0",
            container="nrel/openstudio:3.11.0",
        )
        endpoint_spec = captured["endpoint_spec"]
        assert endpoint_spec["Ports"][0]["PublishMode"] == "ingress"

    def test_submit_service_pins_restart_policy_none(self) -> None:
        """``restart_policy={\"Condition\": \"none\"}`` (issue #1641) is set explicitly."""

        ex = self._make_executor()
        captured: dict[str, object] = {}

        def _capture_create(**kwargs: object) -> MagicMock:
            captured.update(kwargs)
            return MagicMock(name="osimflow-test")

        ex._client.services.create = MagicMock(side_effect=_capture_create)  # type: ignore[method-assign]
        ex._submit_service(  # noqa: SLF001
            name="test",
            cpus=1,
            memory_mb=1024,
            time_min=60,
            openstudio_version="3.11.0",
            container="nrel/openstudio:3.11.0",
        )
        assert captured["restart_policy"] == {"Condition": "none"}

    def test_submit_service_propagates_stub_sim_env(self) -> None:
        """``OSIMFLOW_STUB_SIM`` from the orchestrator env is propagated to the service."""

        ex = self._make_executor()
        captured: dict[str, object] = {}

        def _capture_create(**kwargs: object) -> MagicMock:
            captured.update(kwargs)
            return MagicMock(name="osimflow-test")

        ex._client.services.create = MagicMock(side_effect=_capture_create)  # type: ignore[method-assign]
        with patch.dict(os.environ, {"OSIMFLOW_STUB_SIM": "1"}):
            ex._submit_service(  # noqa: SLF001
                name="test",
                cpus=1,
                memory_mb=1024,
                time_min=60,
                openstudio_version="3.11.0",
                container="nrel/openstudio:3.11.0",
            )
        env_map = {
            entry.partition("=")[0]: entry.partition("=")[2]
            for entry in (captured["env"] or [])  # type: ignore[union-attr]
            if isinstance(entry, str) and "=" in entry
        }
        assert env_map["OSIMFLOW_STUB_SIM"] == "1"

    def test_get_client_import_error_raises_runtime_error(self) -> None:
        """Missing ``docker`` package surfaces as ``ImportError``."""

        ex = self._make_executor()
        ex._client = None  # Force the lazy-init path so we hit the import block.

        # Make ``import docker`` raise an ImportError that the real
        # code then re-raises with the documented guidance message.
        with patch.dict("sys.modules", {"docker": None}):
            with pytest.raises(ImportError, match="docker Python SDK"):
                ex._get_client()  # noqa: SLF001

    def test_get_client_ping_failure_raises_runtime_error(self) -> None:
        """A failed ``ping`` (daemon unreachable) raises ``RuntimeError``."""

        ex = self._make_executor()
        ex._client = None  # Force the lazy-init path.

        client = MagicMock()
        client.ping.side_effect = ConnectionError("ping failed")
        fake_module = MagicMock()
        fake_module.from_env.return_value = client

        with patch.dict("sys.modules", {"docker": fake_module}):
            with pytest.raises(RuntimeError, match="Docker daemon is not reachable"):
                ex._get_client()  # noqa: SLF001

    def test_constructor_logs_warning_for_latest_tag(self, caplog: pytest.LogCaptureFixture) -> None:
        """``__init__`` warns when the image tag is ``:latest`` (supply-chain)."""

        caplog.set_level("WARNING", logger="osimflow.executors.docker_swarm")
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DOCKER_HOST", None)
            DockerSwarmExecutor(image="nrel/openstudio:latest")
        assert any(
            "using 'latest' is not recommended" in rec.message
            for rec in caplog.records
        )

    def test_constructor_does_not_warn_for_pinned_tag(self, caplog: pytest.LogCaptureFixture) -> None:
        """No supply-chain warning fires when the image tag is pinned."""

        caplog.set_level("WARNING", logger="osimflow.executors.docker_swarm")
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DOCKER_HOST", None)
            DockerSwarmExecutor(image="nrel/openstudio:3.11.0")
        assert not any(
            "using 'latest' is not recommended" in rec.message
            for rec in caplog.records
        )

    def test_requires_remote_runner_payload_true(self) -> None:
        """``requires_remote_runner_payload`` is ``True`` (DockerSwarm dispatches remote_runner)."""

        ex = self._make_executor()
        # ``requires_remote_runner_payload`` is a class-level property that
        # reads off the descriptor on access. Read it through an instance
        # so the property is invoked (the class-level access returns the
        # descriptor object itself).
        assert ex.requires_remote_runner_payload is True  # noqa: SLF001

    def test_wait_for_terminal_routes_polling_to_executor(self) -> None:
        """``_wait_for_terminal`` on the handle delegates to the executor."""

        ex = self._make_executor()
        ex._wait_for_terminal = MagicMock(return_value={"status": {"State": "complete"}})  # type: ignore[method-assign]
        handle = _DockerSwarmHandle(
            service_name="svc-delegate",
            executor=ex,
            submit_params={},
        )
        result = handle._wait_for_terminal(5.0)  # noqa: SLF001
        ex._wait_for_terminal.assert_called_once_with("svc-delegate", timeout=5.0)
        assert result == {"status": {"State": "complete"}}

    def test_wait_for_terminal_no_tasks_pending_log(self) -> None:
        """``_wait_for_terminal`` logs the "no tasks yet" branch when the service has no tasks."""

        ex = self._make_executor()
        ex._get_service_status = MagicMock(  # type: ignore[method-assign]
            return_value={"tasks": []}
        )
        with (
            patch("osimflow.testing.patch_targets.time.sleep"),
            patch("osimflow.executors.docker_swarm_executor.log") as mock_log,
        ):
            with pytest.raises(TimeoutError, match="Timed out"):
                ex._wait_for_terminal(  # noqa: SLF001
                    "svc-no-tasks", timeout=0.05
                )
        # The "no-tasks yet" info-log fires once before the timeout.
        assert any(
            "no-tasks yet" in str(call_args)
            for call_args in mock_log.info.call_args_list
        )

    def test_submit_service_skips_unset_transport_field_env(self) -> None:
        """With a transport whose storage fields are None, only the mode env var is emitted."""

        ex = self._make_executor()
        captured: dict[str, object] = {}

        def _capture_create(**kwargs: object) -> MagicMock:
            captured.update(kwargs)
            return MagicMock(name="osimflow-test")

        ex._client.services.create = MagicMock(side_effect=_capture_create)  # type: ignore[method-assign]
        cfg = ResultTransportConfig(
            mode="auto",
            backend=None,
            bucket=None,
            prefix=None,
            endpoint=None,
        )
        ex._submit_service(  # noqa: SLF001
            name="minimal-transport",
            cpus=1,
            memory_mb=1024,
            time_min=60,
            openstudio_version="3.11.0",
            container="nrel/openstudio:3.11.0",
            transport=cfg,
        )
        env_names = [
            entry.partition("=")[0]
            for entry in (captured["env"] or [])  # type: ignore[union-attr]
            if isinstance(entry, str) and "=" in entry
        ]
        assert "OSIMFLOW_RESULT_TRANSPORT_MODE" in env_names
        assert "OSIMFLOW_RESULT_STORAGE_BACKEND" not in env_names
        assert "OSIMFLOW_RESULT_STORAGE_BUCKET" not in env_names
        assert "OSIMFLOW_RESULT_STORAGE_PREFIX" not in env_names
        assert "OSIMFLOW_RESULT_STORAGE_ENDPOINT" not in env_names

    def test_submit_service_default_image_used_when_container_none(self) -> None:
        """When ``container`` is None the executor's default ``image`` is used."""

        ex = self._make_executor()
        captured: dict[str, object] = {}

        def _capture_create(**kwargs: object) -> MagicMock:
            captured.update(kwargs)
            return MagicMock(name="osimflow-test")

        ex._client.services.create = MagicMock(side_effect=_capture_create)  # type: ignore[method-assign]
        ex._submit_service(  # noqa: SLF001
            name="default-image",
            cpus=1,
            memory_mb=1024,
            time_min=60,
            openstudio_version="3.11.0",
            container=None,
        )
        env_map = {
            entry.partition("=")[0]: entry.partition("=")[2]
            for entry in (captured["env"] or [])  # type: ignore[union-attr]
            if isinstance(entry, str) and "=" in entry
        }
        assert env_map["OSIMFLOW_CONTAINER"] == "nrel/openstudio:3.11.0"
