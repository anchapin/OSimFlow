"""Cross-executor HMAC signing symmetry regression test (issues #1384, #1445).

The HMAC-SHA256 task-payload contract (issue #1177) requires that every
executor with ``requires_remote_runner_payload = True`` propagates the
shared secret + signature alongside ``OSIMFLOW_TASK_PAYLOAD`` when a
secret is configured. Before issue #1384, ``NomadExecutor`` (legacy +
dispatch modes) and ``KubernetesExecutor`` called ``build_signature_env``
in their env builders, but ``AzureBatchExecutor``,
``GoogleBatchExecutor``, and ``DockerSwarmExecutor`` shipped
``OSIMFLOW_TASK_PAYLOAD`` unsigned, so the ``remote_runner`` fail-closed
verification gate raised ``RuntimeError`` for any campaign dispatched
through those substrates. Issue #1445 found the same drift in
``AWSBatchExecutor`` — the one executor the hardcoded executor list in
this module had left out.

The parametrized executor list is therefore derived from the live
``ExecutorRegistry`` via the ``requires_remote_runner_payload`` property
so any future executor that adopts the remote-runner payload contract is
automatically pulled into the symmetry test (and fails CI if its env
builder does not sign).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from osimflow.task_payload_hmac import (
    TASK_PAYLOAD_SECRET_ENV,
    TASK_PAYLOAD_SECRET_META_KEY,
    TASK_PAYLOAD_SIG_ENV,
    TASK_PAYLOAD_SIG_META_KEY,
    sign_task_payload,
)


def _remote_runner_executor_names() -> tuple[str, ...]:
    """Derive the remote-runner executor set from the live registry.

    Every registered executor whose ``requires_remote_runner_payload``
    property is True participates in the HMAC signing contract. Deriving
    the list here (instead of hardcoding names) keeps the symmetry test
    in lockstep with the registry: a new remote-runner executor is
    parametrized automatically, and the completeness guard below fails
    CI if its signing env builder is missing (issue #1445).
    """
    from osimflow.executors import ExecutorRegistry

    names: list[str] = []
    for name in ExecutorRegistry.list_available():
        executor_cls = ExecutorRegistry.get(name)
        instance = executor_cls.__new__(executor_cls)  # noqa: SLF001
        if instance.requires_remote_runner_payload:
            names.append(name)
    return tuple(sorted(names))


REMOTE_RUNNER_EXECUTORS: tuple[str, ...] = _remote_runner_executor_names()

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


def _aws_batch_env(executor_factory: Callable[..., Any]) -> dict[str, str]:
    ex = executor_factory()
    env_list = ex._build_environment(  # noqa: SLF001
        container="nrel/openstudio:3.11.0",
        openstudio_version="3.11.0",
        task_payload=TASK_PAYLOAD,
    )
    return _parse_env(env_list)


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


def _aws_batch_executor_factory() -> Any:
    from osimflow.executors import AWSBatchExecutor

    ex = AWSBatchExecutor.__new__(AWSBatchExecutor)  # noqa: SLF001
    ex.ecr_repository = None
    return ex


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
    ex.security_context_strict = True
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
    "aws_batch": _aws_batch_env,
    "nomad": _nomad_legacy_env,
    "kubernetes": _kubernetes_env,
    "azure_batch": _azure_batch_env,
    "google_batch": _google_batch_env,
    "docker_swarm": _docker_swarm_env,
}

EXECUTOR_FACTORIES: dict[str, Callable[[], Any]] = {
    "aws_batch": _aws_batch_executor_factory,
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
    """Defensive guard: the registry-derived set must match this module's wiring.

    ``REMOTE_RUNNER_EXECUTORS`` is derived from the live
    ``ExecutorRegistry`` via the ``requires_remote_runner_payload``
    property (issue #1445 — the hardcoded list had left out AWS Batch).
    This guard pins the derivation to an explicit class map so a broken
    derivation (empty / over-matching) fails loud, and asserts
    ``ENV_BUILDERS`` covers every derived executor — a new remote-runner
    executor without HMAC signing in its env builder fails CI here and
    in the parametrized symmetry tests above.
    """
    # Local imports keep the module importable without the heavy
    # substrate SDKs (azure.batch, google.cloud.batch_v1, docker, ...).
    from osimflow.executors import AWSBatchExecutor, NomadExecutor
    from osimflow.executors.azure_batch_executor import AzureBatchExecutor
    from osimflow.executors.base import BaseExecutor
    from osimflow.executors.docker_swarm_executor import DockerSwarmExecutor
    from osimflow.executors.google_batch_executor import GoogleBatchExecutor
    from osimflow.executors.kubernetes_executor import KubernetesExecutor

    expected_classes: dict[str, type[Any]] = {
        "aws_batch": AWSBatchExecutor,
        "azure_batch": AzureBatchExecutor,
        "docker_swarm": DockerSwarmExecutor,
        "google_batch": GoogleBatchExecutor,
        "kubernetes": KubernetesExecutor,
        "nomad": NomadExecutor,
    }
    assert REMOTE_RUNNER_EXECUTORS == tuple(sorted(expected_classes)), (
        f"registry-derived remote-runner set {REMOTE_RUNNER_EXECUTORS} "
        f"diverged from the expected class map {tuple(sorted(expected_classes))}"
    )
    for executor_name, executor_cls in expected_classes.items():
        instance = executor_cls.__new__(executor_cls)  # noqa: SLF001
        assert isinstance(instance, BaseExecutor)
        assert instance.requires_remote_runner_payload is True, (
            f"{executor_name} must declare requires_remote_runner_payload = True"
        )

    # ``ENV_BUILDERS`` must cover every executor in the registry-derived list.
    assert set(ENV_BUILDERS.keys()) == set(REMOTE_RUNNER_EXECUTORS)
    assert set(EXECUTOR_FACTORIES.keys()) == set(REMOTE_RUNNER_EXECUTORS)


# --- Issue #1449: native secret-store delivery -------------------------
#
# The literal-secret modes asserted above remain the default for
# backward compat. The tests below opt into the per-substrate native
# secret-reference mechanisms and assert the acceptance criterion for
# issue #1449: the secret is NOT embedded verbatim in the job spec
# where a native secret-reference mechanism is used.


def _nomad_vault_executor_factory(**kwargs: Any) -> Any:
    """Build a NomadExecutor with Vault-template delivery enabled."""
    from osimflow.executors import NomadExecutor

    kwargs.setdefault("vault_secret_path", "secret/data/osimflow/hmac")
    with patch("urllib.request.urlopen"):
        return NomadExecutor(address="http://127.0.0.1:4646", **kwargs)


def test_kubernetes_secret_key_ref_replaces_literal_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue #1449: with ``payload_secret_ref`` the secret ships as a secretKeyRef."""
    monkeypatch.setenv(TASK_PAYLOAD_SECRET_ENV, SECRET)
    ex = _kubernetes_executor_factory()
    ex.payload_secret_ref = "osimflow-payload-secret"
    env_list = ex._build_environment(  # noqa: SLF001
        container="nrel/openstudio:3.11.0",
        openstudio_version="3.11.0",
        task_payload=TASK_PAYLOAD,
    )
    # The raw secret must not appear anywhere in the serialized env.
    assert SECRET not in json.dumps(env_list)
    secret_entry = next(e for e in env_list if e["name"] == TASK_PAYLOAD_SECRET_ENV)
    assert secret_entry["valueFrom"]["secretKeyRef"] == {
        "name": "osimflow-payload-secret",
        "key": TASK_PAYLOAD_SECRET_ENV,
    }
    # The signature is public by design and still ships as a literal.
    sig_entry = next(e for e in env_list if e["name"] == TASK_PAYLOAD_SIG_ENV)
    assert sig_entry["value"] == sign_task_payload(TASK_PAYLOAD, SECRET)


def test_kubernetes_submit_job_emits_secret_key_ref(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue #1449: the submitted V1Job carries a secretKeyRef source, not a literal."""
    monkeypatch.setenv(TASK_PAYLOAD_SECRET_ENV, SECRET)
    ex = _kubernetes_executor_factory()
    ex.payload_secret_ref = "osimflow-payload-secret"
    environment = ex._build_environment(  # noqa: SLF001
        container="nrel/openstudio:3.11.0",
        openstudio_version="3.11.0",
        task_payload=TASK_PAYLOAD,
    )
    ex._submit_job(  # noqa: SLF001
        name="sim_0001",
        cpus=1,
        memory_mb=1024,
        time_min=60,
        environment=environment,
    )
    body = ex._client.create_namespaced_job.call_args.kwargs["body"]  # type: ignore[union-attr]  # noqa: E501
    body_dict = body.to_dict()
    assert SECRET not in json.dumps(body_dict, default=str)
    env_vars = body.spec.template.spec.containers[0].env
    secret_var = next(v for v in env_vars if v.name == TASK_PAYLOAD_SECRET_ENV)
    assert secret_var.value is None
    assert secret_var.value_from.secret_key_ref.name == "osimflow-payload-secret"
    assert secret_var.value_from.secret_key_ref.key == TASK_PAYLOAD_SECRET_ENV
    literal_env_names = {v.name for v in env_vars if v.name != TASK_PAYLOAD_SECRET_ENV}
    assert TASK_PAYLOAD_SIG_ENV in literal_env_names


def test_kubernetes_secret_key_ref_warns_when_unsigned(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Issue #1449: a configured ref without an orchestrator secret warns loudly."""
    monkeypatch.delenv(TASK_PAYLOAD_SECRET_ENV, raising=False)
    ex = _kubernetes_executor_factory()
    ex.payload_secret_ref = "osimflow-payload-secret"
    with caplog.at_level("WARNING", logger="osimflow.executors.kubernetes"):
        env_list = ex._build_environment(  # noqa: SLF001
            container="nrel/openstudio:3.11.0",
            openstudio_version="3.11.0",
            task_payload=TASK_PAYLOAD,
        )
    names = {e["name"] for e in env_list}
    assert TASK_PAYLOAD_SECRET_ENV not in names
    assert TASK_PAYLOAD_SIG_ENV not in names
    assert any("cannot be signed" in record.message for record in caplog.records)


def test_nomad_vault_template_replaces_literal_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue #1449: Vault mode renders the secret from a template stanza, not env."""
    monkeypatch.setenv(TASK_PAYLOAD_SECRET_ENV, SECRET)
    ex = _nomad_vault_executor_factory()
    spec = ex._build_job_spec(  # noqa: SLF001
        name="sim_0001",
        cpus=1,
        memory_mb=1024,
        container="nrel/openstudio:3.11.0",
        openstudio_version="3.11.0",
        task_payload=TASK_PAYLOAD,
    )
    # The raw secret must not appear anywhere in the serialized job spec.
    assert SECRET not in json.dumps(spec)
    task = spec["Job"]["TaskGroups"][0]["Tasks"][0]
    assert TASK_PAYLOAD_SECRET_ENV not in task["Env"]
    # The signature is public by design and still ships as a literal.
    assert task["Env"][TASK_PAYLOAD_SIG_ENV] == sign_task_payload(TASK_PAYLOAD, SECRET)
    templates = task["Templates"]
    assert len(templates) == 1
    assert templates[0]["Env"] is True
    assert templates[0]["EmbeddedTmpl"] == (
        f"{TASK_PAYLOAD_SECRET_ENV}="
        '{{ with secret "secret/data/osimflow/hmac" }}'
        "{{ .Data.data.payload_secret }}{{ end }}"
    )


def test_nomad_vault_template_kv_v1_field() -> None:
    """Issue #1449: non-``/data/`` paths read the KV v1 field layout."""
    ex = _nomad_vault_executor_factory(vault_secret_path="kv/osimflow")
    assert ex._vault_secret_template() == (  # noqa: SLF001
        f"{TASK_PAYLOAD_SECRET_ENV}="
        '{{ with secret "kv/osimflow" }}{{ .Data.payload_secret }}{{ end }}'
    )


def test_nomad_dispatch_meta_omits_secret_in_vault_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue #1449: dispatch meta carries the signature but not the secret."""
    monkeypatch.setenv(TASK_PAYLOAD_SECRET_ENV, SECRET)
    ex = _nomad_vault_executor_factory(
        use_dispatch=True,
        dispatch_job_id="osimflow-worker-hmac-test",
    )
    ex._client = MagicMock()
    ex._client.register_job.return_value = {}
    ex._client.dispatch_job.return_value = {"JobID": "child-1", "EvalID": "eval-1"}
    ex.submit(
        lambda: None,
        name="sim_0001",
        container="nrel/openstudio:3.11.0",
        openstudio_version="3.11.0",
    )
    # The registered parameterized job renders the secret from Vault.
    registered_spec = ex._client.register_job.call_args.args[0]
    assert SECRET not in json.dumps(registered_spec)
    dispatch_task = registered_spec["Job"]["TaskGroups"][0]["Tasks"][0]
    assert dispatch_task["Templates"][0]["Env"] is True
    assert "{{ with secret " in dispatch_task["Templates"][0]["EmbeddedTmpl"]
    # The per-dispatch meta (visible via nomad job inspect / alloc status)
    # carries the signature but never the raw secret.
    meta = ex._client.dispatch_job.call_args.kwargs["meta"]
    assert SECRET not in json.dumps(meta)
    assert TASK_PAYLOAD_SECRET_META_KEY not in meta
    assert meta[TASK_PAYLOAD_SIG_META_KEY] == sign_task_payload(meta["task_payload"], SECRET)
