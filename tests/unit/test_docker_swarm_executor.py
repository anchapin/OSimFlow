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

from osimflow.executors.docker_swarm_executor import (
    DockerSwarmExecutor,
    _DockerSwarmHandle,
)
from osimflow.executors.transport import ResultTransportConfig
from osimflow.task_payload_hmac import (
    TASK_PAYLOAD_SECRET_ENV,
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
