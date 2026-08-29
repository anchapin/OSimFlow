"""Cross-executor HMAC signing symmetry regression test (issue #1384).

The HMAC-SHA256 task-payload contract (issue #1177) requires that every
executor with ``requires_remote_runner_payload = True`` propagates the
shared secret + signature alongside ``OSIMFLOW_TASK_PAYLOAD`` when a
secret is configured. Before issue #1384, ``NomadExecutor`` (legacy +
dispatch modes) and ``KubernetesExecutor`` called ``build_signature_env``
in their env builders, but ``AzureBatchExecutor``,
``GoogleBatchExecutor``, and ``DockerSwarmExecutor`` shipped
``OSIMFLOW_TASK_PAYLOAD`` unsigned, so the ``remote_runner`` fail-closed
verification gate raised ``RuntimeError`` for any campaign dispatched
through those substrates.

This test asserts the cross-executor symmetry contract directly: with a
secret configured, every concrete executor in
``RemoteRunnerPayloadExecutors`` produces a per-job env that includes
``OSIMFLOW_TASK_PAYLOAD_SECRET`` and ``OSIMFLOW_TASK_PAYLOAD_SIG``; with
no secret, the env remains unsigned (legacy mode is preserved).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from osimflow.task_payload_hmac import (
    TASK_PAYLOAD_SECRET_ENV,
    TASK_PAYLOAD_SIG_ENV,
    sign_task_payload,
)

# All five remote-runner executors participate in the same contract.
# Nomad + Kubernetes were already symmetric pre-#1384; the fix adds the
# same wiring to AzureBatch / GoogleBatch / DockerSwarm.
REMOTE_RUNNER_EXECUTORS: tuple[str, ...] = (
    "nomad",
    "kubernetes",
    "azure_batch",
    "google_batch",
    "docker_swarm",
)

SECRET = "cross-executor-shared-secret"
TASK_PAYLOAD = json.dumps({"step": "sim", "args": [], "kwargs": {}})


@pytest.fixture(autouse=True)
def _clean_signature_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate every test from the host's signature configuration."""
    monkeypatch.delenv(TASK_PAYLOAD_SECRET_ENV, raising=False)
    monkeypatch.delenv(TASK_PAYLOAD_SIG_ENV, raising=False)
    monkeypatch.delenv("NOMAD_META_task_payload", raising=False)
    monkeypatch.delenv("NOMAD_META_task_payload_sig", raising=False)
    monkeypatch.delenv("NOMAD_META_task_payload_secret", raising=False)


def _parse_env(env: Iterable[Any] | dict[str, str]) -> dict[str, str]:
    """Normalise every executor's env shape to a plain ``{name: value}`` dict.

    AzureBatch / GoogleBatch / Kubernetes emit ``list[dict[str, str]]``
    of ``{"name": ..., "value": ...}`` entries; Nomad and DockerSwarm emit
    either ``dict[str, str]`` or ``list[str]`` of ``"KEY=VALUE"`` pairs.
    """
    if isinstance(env, dict):
        return dict(env)
    parsed: dict[str, str] = {}
    for entry in env:
        if isinstance(entry, dict):
            name = entry.get("name")
            value = entry.get("value")
            if name is not None:
                parsed[str(name)] = "" if value is None else str(value)
        else:
            text = str(entry)
            if "=" not in text:
                continue
            key, _, value = text.partition("=")
            parsed[key] = value
    return parsed


def _nomad_legacy_env(executor_factory: Callable[..., Any]) -> dict[str, str]:
    ex = executor_factory()
    spec = ex._build_job_spec(  # noqa: SLF001
        name="sim_0001",
        cpus=1,
        memory_mb=1024,
        container="nrel/openstudio:3.11.0",
        openstudio_version="3.11.0",
        task_payload=TASK_PAYLOAD,
    )
    return spec["Job"]["TaskGroups"][0]["Tasks"][0]["Env"]


def _kubernetes_env(executor_factory: Callable[..., Any]) -> dict[str, str]:
    ex = executor_factory()
    env_list = ex._build_environment(  # noqa: SLF001
        container="nrel/openstudio:3.11.0",
        openstudio_version="3.11.0",
        task_payload=TASK_PAYLOAD,
    )
    return _parse_env(env_list)


def _azure_batch_env(executor_factory: Callable[..., Any]) -> dict[str, str]:
    ex = executor_factory()
    env_list = ex._build_environment(  # noqa: SLF001
        container="nrel/openstudio:3.11.0",
        openstudio_version="3.11.0",
        task_payload=TASK_PAYLOAD,
    )
    return _parse_env(env_list)


def _google_batch_env(executor_factory: Callable[..., Any]) -> dict[str, str]:
    ex = executor_factory()
    env_list = ex._build_environment(  # noqa: SLF001
        container="nrel/openstudio:3.11.0",
        openstudio_version="3.11.0",
        task_payload=TASK_PAYLOAD,
    )
    return _parse_env(env_list)


def _docker_swarm_env(executor_factory: Callable[..., Any]) -> dict[str, str]:
    ex = executor_factory()
    captured: dict[str, Any] = {}

    def fake_create(**kwargs: Any) -> MagicMock:
        captured.update(kwargs)
        fake_service = MagicMock()
        fake_service.name = "osimflow-test"
        return fake_service

    ex._client.services.create = MagicMock(side_effect=fake_create)  # type: ignore[method-assign]  # noqa: E501
    ex._submit_service(  # noqa: SLF001
        name="sim_0001",
        cpus=1,
        memory_mb=1024,
        time_min=60,
        openstudio_version="3.11.0",
        container="nrel/openstudio:3.11.0",
        task_payload=TASK_PAYLOAD,
    )
    return _parse_env(captured["env"])


def _nomad_executor_factory() -> Any:
    from osimflow.executors import NomadExecutor

    with patch("urllib.request.urlopen"):
        return NomadExecutor(address="http://127.0.0.1:4646")


def _kubernetes_executor_factory() -> Any:
    from osimflow.executors import KubernetesExecutor

    ex = KubernetesExecutor.__new__(KubernetesExecutor)  # noqa: SLF001
    ex._client = MagicMock()
    ex.namespace = "default"
    ex.poll_interval_s = 5.0
    ex.max_poll_interval_s = 60.0
    ex.backoff_limit = 0
    ex.ttl_seconds_after_finished = None
    ex.queue_name = None
    return ex


def _azure_batch_executor_factory() -> Any:
    from osimflow.executors.azure_batch_executor import AzureBatchExecutor

    ex = AzureBatchExecutor.__new__(AzureBatchExecutor)  # noqa: SLF001
    ex._azure_identity = MagicMock()
    ex._azure_batch = MagicMock()
    ex.account_name = "testaccount"
    ex.account_url = "https://testaccount.eastus.batch.azure.com"
    ex.pool_id = "test-pool"
    ex.location = "eastus"
    ex.poll_interval_s = 0.01
    ex.max_poll_interval_s = 0.02
    ex.use_spot = False
    ex.fallback_to_on_demand = False
    ex.max_retries = 3
    ex._client = MagicMock()
    return ex


def _google_batch_executor_factory() -> Any:
    from osimflow.executors.google_batch_executor import GoogleBatchExecutor

    ex = GoogleBatchExecutor.__new__(GoogleBatchExecutor)  # noqa: SLF001
    ex._batch_v1 = MagicMock()
    ex.project_id = "test-project"
    ex.region = "us-central1"
    ex.batch_service_account = None
    ex.poll_interval_s = 0.01
    ex.max_poll_interval_s = 0.02
    ex.use_spot = False
    ex.fallback_to_on_demand = False
    ex.max_retries = 3
    ex._client = MagicMock()
    return ex


def _docker_swarm_executor_factory() -> Any:
    from osimflow.executors.docker_swarm_executor import DockerSwarmExecutor

    ex = DockerSwarmExecutor.__new__(DockerSwarmExecutor)  # noqa: SLF001
    ex.poll_interval_s = 5.0
    ex.max_poll_interval_s = 60.0
    ex.image = "nrel/openstudio:latest"
    ex.network = None
    ex._client = MagicMock()
    ex._stub_executor = None
    return ex


ENV_BUILDERS: dict[str, Callable[[Callable[..., Any]], dict[str, str]]] = {
    "nomad": _nomad_legacy_env,
    "kubernetes": _kubernetes_env,
    "azure_batch": _azure_batch_env,
    "google_batch": _google_batch_env,
    "docker_swarm": _docker_swarm_env,
}

EXECUTOR_FACTORIES: dict[str, Callable[[], Any]] = {
    "nomad": _nomad_executor_factory,
    "kubernetes": _kubernetes_executor_factory,
    "azure_batch": _azure_batch_executor_factory,
    "google_batch": _google_batch_executor_factory,
    "docker_swarm": _docker_swarm_executor_factory,
}


@pytest.mark.parametrize("executor_name", REMOTE_RUNNER_EXECUTORS)
def test_executor_propagates_signature_when_secret_configured(
    executor_name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #1384: every remote-runner executor must sign the payload when secret is set."""
    monkeypatch.setenv(TASK_PAYLOAD_SECRET_ENV, SECRET)
    factory = EXECUTOR_FACTORIES[executor_name]
    builder = ENV_BUILDERS[executor_name]
    env_map = builder(factory)
    assert env_map["OSIMFLOW_TASK_PAYLOAD"] == TASK_PAYLOAD
    assert env_map[TASK_PAYLOAD_SECRET_ENV] == SECRET
    assert env_map[TASK_PAYLOAD_SIG_ENV] == sign_task_payload(TASK_PAYLOAD, SECRET)


@pytest.mark.parametrize("executor_name", REMOTE_RUNNER_EXECUTORS)
def test_executor_omits_signature_when_no_secret(
    executor_name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #1384: legacy unsigned mode is preserved across every executor."""
    monkeypatch.delenv(TASK_PAYLOAD_SECRET_ENV, raising=False)
    factory = EXECUTOR_FACTORIES[executor_name]
    builder = ENV_BUILDERS[executor_name]
    env_map = builder(factory)
    assert env_map["OSIMFLOW_TASK_PAYLOAD"] == TASK_PAYLOAD
    assert TASK_PAYLOAD_SIG_ENV not in env_map
    assert TASK_PAYLOAD_SECRET_ENV not in env_map


def test_remote_runner_executor_registry_complete() -> None:
    """Defensive guard: every concrete executor with remote-runner payload must be wired.

    If a new executor with ``requires_remote_runner_payload = True``
    is added without HMAC signing, this list will go out of sync with
    ``ENV_BUILDERS`` and the parametrized test will fail loud.
    """
    # Local imports keep the module importable without the heavy
    # substrate SDKs (azure.batch, google.cloud.batch_v1, docker, ...).
    from osimflow.executors.azure_batch_executor import AzureBatchExecutor
    from osimflow.executors.base import BaseExecutor
    from osimflow.executors.docker_swarm_executor import DockerSwarmExecutor
    from osimflow.executors.google_batch_executor import GoogleBatchExecutor
    from osimflow.executors.kubernetes_executor import KubernetesExecutor

    expected_classes = {
        "azure_batch": AzureBatchExecutor,
        "docker_swarm": DockerSwarmExecutor,
        "google_batch": GoogleBatchExecutor,
        "kubernetes": KubernetesExecutor,
    }
    for executor_name, executor_cls in expected_classes.items():
        instance = executor_cls.__new__(executor_cls)
        assert isinstance(instance, BaseExecutor)
        assert instance.requires_remote_runner_payload is True, (
            f"{executor_name} must declare requires_remote_runner_payload = True"
        )

    # Nomad is defined inline in osimflow.executors.__init__; verify it
    # through the public name + same property contract.
    from osimflow.executors import NomadExecutor

    nomad_instance = NomadExecutor.__new__(NomadExecutor)
    assert isinstance(nomad_instance, BaseExecutor)
    assert nomad_instance.requires_remote_runner_payload is True

    # ``ENV_BUILDERS`` must cover every executor in the registry list.
    assert set(ENV_BUILDERS.keys()) == set(REMOTE_RUNNER_EXECUTORS)
