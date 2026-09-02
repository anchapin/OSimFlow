"""Transport capability matrix tests (issue #1473).

``osimflow.executors.transport.TRANSPORT_CAPABILITIES`` is the declared
source of truth for which executors support which
``result_transport_mode`` values.  ``BaseExecutor.submit_request`` and
the in-band executors' ``submit`` paths validate against it, raising a
clear ``ValueError`` for unsupported combinations instead of silently
discarding the field.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from osimflow.executors import ExecutorRegistry, LocalExecutor, SlurmExecutor
from osimflow.executors.base import SubmitRequest
from osimflow.executors.dask_jobqueue_executor import DaskJobQueueExecutor
from osimflow.executors.docker_swarm_executor import (
    DockerSwarmExecutor,
    _DockerSwarmHandle,
)
from osimflow.executors.transport import (
    DEFAULT_TRANSPORT_CAPABILITIES,
    IN_BAND_TRANSPORT_MODES,
    TRANSPORT_CAPABILITIES,
    validate_transport_mode,
)

OBJECT_STORAGE_EXECUTORS = sorted(
    name for name, caps in TRANSPORT_CAPABILITIES.items() if "object_storage" in caps
)

IN_BAND_ONLY_EXECUTORS = sorted(
    name for name, caps in TRANSPORT_CAPABILITIES.items() if "object_storage" not in caps
)

TEN_BUILTIN_EXECUTORS = {
    "local",
    "slurm",
    "aws_batch",
    "nomad",
    "azure_batch",
    "google_batch",
    "kubernetes",
    "pbs",
    "dask_jobqueue",
    "docker_swarm",
}


class TestCapabilityMatrixDeclaration:
    def test_all_ten_registered_executors_are_declared(self) -> None:
        assert set(ExecutorRegistry._registry) == TEN_BUILTIN_EXECUTORS
        assert set(TRANSPORT_CAPABILITIES) == TEN_BUILTIN_EXECUTORS

    def test_in_band_modes_are_universal(self) -> None:
        for name, caps in TRANSPORT_CAPABILITIES.items():
            assert IN_BAND_TRANSPORT_MODES <= caps, f"{name} must support auto + shared_fs"

    def test_expected_object_storage_executors(self) -> None:
        assert OBJECT_STORAGE_EXECUTORS == [
            "aws_batch",
            "azure_batch",
            "docker_swarm",
            "google_batch",
            "kubernetes",
            "nomad",
            "pbs",
        ]

    def test_expected_in_band_only_executors(self) -> None:
        assert IN_BAND_ONLY_EXECUTORS == ["dask_jobqueue", "local", "slurm"]


class TestValidateTransportMode:
    @pytest.mark.parametrize("executor_name", IN_BAND_ONLY_EXECUTORS)
    def test_object_storage_raises_for_in_band_only_executors(self, executor_name: str) -> None:
        with pytest.raises(ValueError, match="does not support result_transport_mode"):
            validate_transport_mode(executor_name, "object_storage")

    @pytest.mark.parametrize("executor_name", OBJECT_STORAGE_EXECUTORS)
    def test_object_storage_succeeds_for_object_storage_executors(self, executor_name: str) -> None:
        assert validate_transport_mode(executor_name, "object_storage") == "object_storage"

    @pytest.mark.parametrize("mode", ["auto", "shared_fs"])
    @pytest.mark.parametrize("executor_name", sorted(TRANSPORT_CAPABILITIES))
    def test_in_band_modes_succeed_everywhere(self, executor_name: str, mode: str) -> None:
        assert validate_transport_mode(executor_name, mode) == mode

    def test_none_mode_normalizes_to_auto_everywhere(self) -> None:
        for executor_name in TRANSPORT_CAPABILITIES:
            assert validate_transport_mode(executor_name, None) == "auto"

    def test_unknown_executor_defaults_to_in_band_only(self) -> None:
        assert DEFAULT_TRANSPORT_CAPABILITIES == IN_BAND_TRANSPORT_MODES
        assert validate_transport_mode("some_third_party_executor", "auto") == "auto"
        assert validate_transport_mode("some_third_party_executor", "shared_fs") == "shared_fs"
        with pytest.raises(ValueError, match="some_third_party_executor"):
            validate_transport_mode("some_third_party_executor", "object_storage")

    def test_mode_aliases_are_accepted(self) -> None:
        assert validate_transport_mode("aws_batch", "object-storage") == "object_storage"
        assert validate_transport_mode("local", "shared-fs") == "shared_fs"

    def test_unknown_mode_string_raises_instead_of_coercing_to_auto(self) -> None:
        with pytest.raises(ValueError, match="unknown result_transport_mode"):
            validate_transport_mode("local", "objekt_storage")  # typo'd mode


class TestSubmitRequestValidation:
    def test_local_submit_request_rejects_object_storage(self) -> None:
        executor = LocalExecutor(max_workers=1)
        request = SubmitRequest(
            fn=lambda: None, name="sim_s0", result_transport_mode="object_storage"
        )
        with pytest.raises(ValueError, match="'local' does not support"):
            executor.submit_request(request)
        executor.shutdown()

    def test_local_submit_rejects_object_storage(self) -> None:
        executor = LocalExecutor(max_workers=1)
        with pytest.raises(ValueError, match="'local' does not support"):
            executor.submit(lambda: None, name="sim_s0", result_transport_mode="object_storage")
        executor.shutdown()

    def test_local_submit_accepts_shared_fs(self) -> None:
        executor = LocalExecutor(max_workers=1)
        handle = executor.submit(lambda: "ok", name="sim_s0", result_transport_mode="shared_fs")
        assert handle.result(timeout=5) == "ok"
        executor.shutdown()

    def test_dask_submit_rejects_object_storage(self) -> None:
        # Validation fires before _ensure_cluster(), so no Dask import is
        # required to assert the matrix behaviour.
        executor = DaskJobQueueExecutor()
        with pytest.raises(ValueError, match="'dask_jobqueue' does not support"):
            executor.submit(lambda: None, name="sim_s0", result_transport_mode="object_storage")

    def test_slurm_submit_request_rejects_object_storage(self) -> None:
        # submit_request validates before delegating to submit(), so an
        # uninitialized SlurmExecutor (no submitit dependency) suffices.
        executor = SlurmExecutor.__new__(SlurmExecutor)
        request = SubmitRequest(
            fn=lambda: None, name="sim_s0", result_transport_mode="object_storage"
        )
        with pytest.raises(ValueError, match="'slurm' does not support"):
            executor.submit_request(request)


def _swarm_executor_stub() -> DockerSwarmExecutor:
    executor = DockerSwarmExecutor.__new__(DockerSwarmExecutor)
    executor._check_docker_available = MagicMock(return_value=True)  # type: ignore[method-assign]
    executor._submit_service = MagicMock(return_value="svc-1")  # type: ignore[method-assign]
    return executor


class TestDockerSwarmCompletion:
    """Docker Swarm's previously half-wired object-storage path (issue #1473)."""

    def test_submit_service_receives_result_hint(self) -> None:
        executor = _swarm_executor_stub()
        handle = executor.submit(
            lambda: None,
            name="sim_s0",
            result_transport_mode="object_storage",
            result_storage_backend="s3",
            result_storage_bucket="b",
            result_hint=Path("/tmp/out/s0"),
        )
        assert handle is not None
        params = executor._submit_service.call_args.kwargs
        assert params["result_transport_mode"] == "object_storage"
        assert params["result_hint"] == Path("/tmp/out/s0")

    def test_handle_materializes_object_storage_result(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from osimflow.executors import docker_swarm_executor as swarm_mod

        executor = DockerSwarmExecutor.__new__(DockerSwarmExecutor)
        executor._wait_for_terminal = MagicMock(  # type: ignore[method-assign]
            return_value={"status": {"State": "complete"}}
        )
        calls: list[dict[str, object]] = []

        def _fake_materialize(callback_result: object, **kwargs: object) -> object:
            calls.append(dict(kwargs))
            return callback_result

        monkeypatch.setattr(swarm_mod, "materialize_object_storage_result", _fake_materialize)

        handle = _DockerSwarmHandle(
            service_name="svc-1",
            executor=executor,
            submit_params={
                "result_hint": tmp_path / "s0",
                "result_transport_mode": "object_storage",
                "result_storage_backend": "s3",
                "result_storage_bucket": "b",
                "result_storage_prefix": "out",
                "result_storage_endpoint": None,
            },
        )
        resolved = handle.result()
        assert resolved == tmp_path / "s0"
        assert calls, "materialize_object_storage_result must be invoked"
        assert calls[0]["transport_mode"] == "object_storage"
        assert calls[0]["result_storage_backend"] == "s3"

    def test_handle_returns_none_without_hint(self) -> None:
        executor = DockerSwarmExecutor.__new__(DockerSwarmExecutor)
        executor._wait_for_terminal = MagicMock(  # type: ignore[method-assign]
            return_value={"status": {"State": "complete"}}
        )
        handle = _DockerSwarmHandle(
            service_name="svc-1",
            executor=executor,
            submit_params={},
        )
        assert handle.result() is None
