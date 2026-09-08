"""CLI-reachable HMAC substrate-secret delivery (issue #1535).

`KubernetesExecutor(payload_secret_ref=...)` and
`NomadExecutor(vault_secret_path=..., vault_secret_key=...)` existed since
#1449, but `_build_executor` never passed them, `CampaignConfig` had no
matching fields, and no flags existed — the secure mode was unreachable
from ``osimflow run``.  On the default path the secret shipped as a
literal env value (K8s Job spec) or dispatch-meta entry (Nomad state
store, readable via ``nomad job inspect``).

These tests pin the #1535 contract: the flags exist and reach the
executors, the secret-store path keeps the raw secret out of the Job
spec / dispatch meta, and the literal path emits a loud WARNING.
"""

from __future__ import annotations

import json
import logging
import sys
import types
from typing import Any
from unittest.mock import MagicMock

import pytest

from osimflow.__main__ import _build_executor, _build_parser

_REQUIRED = [
    "--input_variables",
    "variables.yml",
    "--template_sim_package",
    "pkg",
    "--n_samples",
    "1",
    "--outdir",
    "out",
]


def _run_namespace(argv: list[str]) -> Any:
    parser = _build_parser()
    ns = parser.parse_args(["run", *_REQUIRED, *argv])
    return ns


class TestCliFlagsReachExecutors:
    def test_kubernetes_secret_ref_flag(self) -> None:
        ns = _run_namespace(
            [
                "--executor",
                "kubernetes",
                "--kubernetes-payload-secret-ref",
                "osimflow-payload-secret",
            ]
        )
        assert ns.kubernetes_payload_secret_ref == "osimflow-payload-secret"
        executor = _build_executor(ns)
        assert executor.payload_secret_ref == "osimflow-payload-secret"

    def test_nomad_vault_flags(self) -> None:
        ns = _run_namespace(
            [
                "--executor",
                "nomad",
                "--nomad-vault-secret-path",
                "secret/data/osimflow",
                "--nomad-vault-secret-key",
                "hmac_key",
            ]
        )
        assert ns.nomad_vault_secret_path == "secret/data/osimflow"
        assert ns.nomad_vault_secret_key == "hmac_key"
        executor = _build_executor(ns)
        assert executor.vault_secret_path == "secret/data/osimflow"
        assert executor.vault_secret_key == "hmac_key"

    def test_defaults_are_none(self) -> None:
        ns = _run_namespace(["--executor", "kubernetes"])
        assert ns.kubernetes_payload_secret_ref is None
        ns = _run_namespace(["--executor", "nomad"])
        assert ns.nomad_vault_secret_path is None
        assert ns.nomad_vault_secret_key == "payload_secret"

    def test_campaign_config_fields_exist(self) -> None:
        from osimflow.config import CampaignConfig

        cfg = CampaignConfig(
            input_variables="variables.yml",
            template_sim_package="pkg",
            n_samples=1,
            outdir="out",
            openstudio_version="3.11.0",
            kubernetes_payload_secret_ref="s",
            nomad_vault_secret_path="secret/osimflow",
            nomad_vault_secret_key="k",
        )
        assert cfg.kubernetes_payload_secret_ref == "s"
        assert cfg.nomad_vault_secret_path == "secret/osimflow"
        assert cfg.nomad_vault_secret_key == "k"


class TestKubernetesSecretDelivery:
    def test_secret_key_ref_and_no_raw_secret_in_job_env(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        from osimflow.executors.kubernetes_executor import KubernetesExecutor
        from osimflow.task_payload_hmac import TASK_PAYLOAD_SECRET_ENV

        monkeypatch.setenv(TASK_PAYLOAD_SECRET_ENV, "super-secret")
        ex = KubernetesExecutor(payload_secret_ref="osimflow-payload-secret")
        entries = ex._signature_env_entries('{"step": "RUN_OPENSTUDIO_SIM"}')
        secret_entries = [e for e in entries if e["name"] == TASK_PAYLOAD_SECRET_ENV]
        assert len(secret_entries) == 1
        entry = secret_entries[0]
        # secretKeyRef, not a literal value
        assert "valueFrom" in entry
        assert entry["valueFrom"]["secretKeyRef"]["name"] == "osimflow-payload-secret"
        assert entry["valueFrom"]["secretKeyRef"]["key"] == TASK_PAYLOAD_SECRET_ENV
        assert "value" not in entry
        # No literal warning when the secret-store path is configured
        with caplog.at_level(logging.WARNING, logger="osimflow.executors"):
            ex._signature_env_entries('{"step": "RUN_OPENSTUDIO_SIM"}')
        assert not any("SECURITY (issue #1535)" in r.message for r in caplog.records)

    def test_literal_secret_ships_with_loud_warning(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        from osimflow.executors.kubernetes_executor import KubernetesExecutor
        from osimflow.task_payload_hmac import TASK_PAYLOAD_SECRET_ENV

        monkeypatch.setenv(TASK_PAYLOAD_SECRET_ENV, "super-secret")
        ex = KubernetesExecutor()  # no payload_secret_ref
        with caplog.at_level(logging.WARNING, logger="osimflow.executors"):
            entries = ex._signature_env_entries('{"step": "RUN_OPENSTUDIO_SIM"}')
        secret_entries = [e for e in entries if e["name"] == TASK_PAYLOAD_SECRET_ENV]
        assert len(secret_entries) == 1
        assert secret_entries[0]["value"] == "super-secret"  # literal, warned
        assert any("SECURITY (issue #1535)" in r.message for r in caplog.records)


class TestNomadSecretDelivery:
    def _executor(self, **kw: Any):
        from osimflow.executors.nomad_executor import NomadExecutor

        return NomadExecutor(address="http://127.0.0.1:4646", **kw)

    def test_vault_template_keeps_secret_out_of_env(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        from osimflow.task_payload_hmac import TASK_PAYLOAD_SECRET_ENV

        monkeypatch.setenv(TASK_PAYLOAD_SECRET_ENV, "super-secret")
        ex = self._executor(vault_secret_path="secret/data/osimflow")
        spec = ex._build_job_spec(
            name="sim_0001",
            cpus=2,
            memory_mb=2048,
            container="nrel/openstudio:3.11.0",
            openstudio_version="3.11.0",
            task_payload='{"step": "RUN_OPENSTUDIO_SIM"}',
        )
        task = spec["Job"]["TaskGroups"][0]["Tasks"][0]
        env = task.get("Env", {})
        # The Vault template renders the secret at allocation time; the
        # literal env block must not carry it (issue #1535).
        assert TASK_PAYLOAD_SECRET_ENV not in env
        templates = task.get("Templates", [])
        assert any(TASK_PAYLOAD_SECRET_ENV in (t.get("EmbeddedTmpl") or "") for t in templates)
        with caplog.at_level(logging.WARNING, logger="osimflow.executors"):
            ex._build_job_spec(
                name="sim_0001",
                cpus=2,
                memory_mb=2048,
                container="nrel/openstudio:3.11.0",
                openstudio_version="3.11.0",
                task_payload='{"step": "RUN_OPENSTUDIO_SIM"}',
            )
        assert not any("SECURITY (issue #1535)" in r.message for r in caplog.records)

    def test_literal_secret_ships_with_loud_warning(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        from osimflow.task_payload_hmac import TASK_PAYLOAD_SECRET_ENV

        monkeypatch.setenv(TASK_PAYLOAD_SECRET_ENV, "super-secret")
        ex = self._executor()  # no vault_secret_path
        with caplog.at_level(logging.WARNING, logger="osimflow.executors"):
            spec = ex._build_job_spec(
                name="sim_0001",
                cpus=2,
                memory_mb=2048,
                container="nrel/openstudio:3.11.0",
                openstudio_version="3.11.0",
                task_payload='{"step": "RUN_OPENSTUDIO_SIM"}',
            )
        task = spec["Job"]["TaskGroups"][0]["Tasks"][0]
        assert task["Env"][TASK_PAYLOAD_SECRET_ENV] == "super-secret"
        assert any("SECURITY (issue #1535)" in r.message for r in caplog.records)


# --- Issue #1633: out-of-band HMAC secret delivery for the remaining --
# substrates (AWS / Azure / Google Batch + Docker Swarm). Mirrors the
# #1449/#1535 K8s+Nomad tests above: default mode keeps the legacy
# literal env pair; secret-ref mode keeps the signature (public by
# design) but must keep the raw secret out of the job spec, wiring the
# substrate's native secret channel instead.

TASK_PAYLOAD = '{"step": "RUN_OPENSTUDIO_SIM"}'
AWS_SECRET_ARN = "arn:aws:secretsmanager:us-east-1:123456789012:secret:osimflow/hmac-AbCdEf"
AZURE_SECRET_ID = "https://osimflow-kv.vault.azure.net/secrets/payload-hmac/0d1e2f"
GOOGLE_SECRET_NAME = "osimflow-payload-hmac"
SWARM_SECRET_NAME = "osimflow-payload-hmac"


def _parse_env_list(entries: list[Any]) -> dict[str, str]:
    """Normalise ``{"name": ..., "value": ...}`` lists to a plain dict."""
    return {e["name"]: e["value"] for e in entries}


def _parse_env_kv(pairs: list[str]) -> dict[str, str]:
    """Normalise ``KEY=VALUE`` string lists to a plain dict."""
    parsed: dict[str, str] = {}
    for text in pairs:
        key, _, value = text.partition("=")
        parsed[key] = value
    return parsed


class TestAwsBatchSecretDelivery:
    def test_cli_flag_reaches_executor(self) -> None:
        ns = _run_namespace(
            ["--executor", "aws_batch", "--aws-batch-payload-secret-arn", AWS_SECRET_ARN]
        )
        assert ns.aws_batch_payload_secret_arn == AWS_SECRET_ARN
        executor = _build_executor(ns)
        assert executor.payload_secret_arn == AWS_SECRET_ARN

    def test_default_mode_ships_literal_secret(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        from osimflow.executors import AWSBatchExecutor
        from osimflow.task_payload_hmac import (
            TASK_PAYLOAD_SECRET_ENV,
            TASK_PAYLOAD_SIG_ENV,
            sign_task_payload,
        )

        monkeypatch.setenv(TASK_PAYLOAD_SECRET_ENV, "super-secret")
        ex = AWSBatchExecutor()
        with caplog.at_level(logging.WARNING):
            env = ex._build_environment(  # noqa: SLF001
                container="nrel/openstudio:3.11.0",
                openstudio_version="3.11.0",
                task_payload=TASK_PAYLOAD,
            )
        env_map = _parse_env_list(env)
        assert env_map[TASK_PAYLOAD_SECRET_ENV] == "super-secret"  # literal, warned
        assert env_map[TASK_PAYLOAD_SIG_ENV] == sign_task_payload(TASK_PAYLOAD, "super-secret")
        assert any("SECURITY (issue #1633)" in r.message for r in caplog.records)
        # No secrets channel in legacy mode.
        assert ex._payload_secret_overrides() == []  # noqa: SLF001
        overrides = ex._build_container_overrides(  # noqa: SLF001
            cpus=1, memory_mb=1024, environment=env
        )
        assert "secrets" not in overrides

    def test_secret_ref_mode_emits_secrets_not_literal(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        from osimflow.executors import AWSBatchExecutor
        from osimflow.task_payload_hmac import (
            TASK_PAYLOAD_SECRET_ENV,
            TASK_PAYLOAD_SIG_ENV,
            sign_task_payload,
        )

        monkeypatch.setenv(TASK_PAYLOAD_SECRET_ENV, "super-secret")
        ex = AWSBatchExecutor(payload_secret_arn=AWS_SECRET_ARN)
        with caplog.at_level(logging.WARNING):
            env = ex._build_environment(  # noqa: SLF001
                container="nrel/openstudio:3.11.0",
                openstudio_version="3.11.0",
                task_payload=TASK_PAYLOAD,
            )
        env_map = _parse_env_list(env)
        # Signature present (public by design); raw secret ABSENT.
        assert env_map[TASK_PAYLOAD_SIG_ENV] == sign_task_payload(TASK_PAYLOAD, "super-secret")
        assert TASK_PAYLOAD_SECRET_ENV not in env_map
        assert "super-secret" not in json.dumps(env)
        # Substrate-specific secret reference present.
        assert ex._payload_secret_overrides() == [  # noqa: SLF001
            {"name": TASK_PAYLOAD_SECRET_ENV, "valueFrom": AWS_SECRET_ARN}
        ]
        assert not any("SECURITY (issue #1633)" in r.message for r in caplog.records)

    def test_submit_job_secrets_in_container_overrides(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from osimflow.executors import AWSBatchExecutor
        from osimflow.task_payload_hmac import TASK_PAYLOAD_SECRET_ENV, TASK_PAYLOAD_SIG_ENV

        monkeypatch.setenv(TASK_PAYLOAD_SECRET_ENV, "super-secret")
        ex = AWSBatchExecutor(payload_secret_arn=AWS_SECRET_ARN)
        captured: dict[str, Any] = {}

        def fake_submit(kwargs: dict[str, Any]) -> dict[str, Any]:
            captured.update(kwargs)
            return {"jobId": "j-1"}

        ex._submit_job_with_retry = fake_submit  # type: ignore[method-assign]  # noqa: SLF001
        environment = ex._build_environment(  # noqa: SLF001
            container="nrel/openstudio:3.11.0",
            openstudio_version="3.11.0",
            task_payload=TASK_PAYLOAD,
        )
        ex._submit_job(  # noqa: SLF001
            name="sim_0001",
            cpus=1,
            memory_mb=1024,
            time_min=1,
            environment=environment,
        )
        overrides = captured["containerOverrides"]
        assert overrides["secrets"] == [
            {"name": TASK_PAYLOAD_SECRET_ENV, "valueFrom": AWS_SECRET_ARN}
        ]
        assert any(e["name"] == TASK_PAYLOAD_SIG_ENV for e in overrides["environment"])
        assert "super-secret" not in json.dumps(captured)

    def test_ref_without_secret_warns_unsigned(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        from osimflow.executors import AWSBatchExecutor
        from osimflow.task_payload_hmac import (
            TASK_PAYLOAD_SECRET_ENV,
            TASK_PAYLOAD_SIG_ENV,
        )

        monkeypatch.delenv(TASK_PAYLOAD_SECRET_ENV, raising=False)
        ex = AWSBatchExecutor(payload_secret_arn=AWS_SECRET_ARN)
        with caplog.at_level(logging.WARNING):
            env = ex._build_environment(  # noqa: SLF001
                container="nrel/openstudio:3.11.0",
                openstudio_version="3.11.0",
                task_payload=TASK_PAYLOAD,
            )
        names = {e["name"] for e in env}
        assert TASK_PAYLOAD_SECRET_ENV not in names
        assert TASK_PAYLOAD_SIG_ENV not in names
        assert any("cannot be signed" in r.message for r in caplog.records)


class TestAzureBatchSecretDelivery:
    def _executor(self, **kw: Any):
        from osimflow.executors.azure_batch_executor import AzureBatchExecutor

        return AzureBatchExecutor(
            account_name="testaccount",
            account_url="https://testaccount.eastus.batch.azure.com",
            pool_id="test-pool",
            **kw,
        )

    def test_cli_flag_parses(self) -> None:
        ns = _run_namespace(
            ["--executor", "azure_batch", "--azure-batch-payload-secret-id", AZURE_SECRET_ID]
        )
        assert ns.azure_batch_payload_secret_id == AZURE_SECRET_ID

    def test_config_fields_exist(self) -> None:
        from osimflow.config import CampaignConfig
        from osimflow.executor_configs import AzureBatchConfig

        cfg = AzureBatchConfig(payload_secret_id=AZURE_SECRET_ID)
        assert cfg.payload_secret_id == AZURE_SECRET_ID
        cc = CampaignConfig(
            input_variables="variables.yml",
            template_sim_package="pkg",
            n_samples=1,
            outdir="out",
            openstudio_version="3.11.0",
            azure_batch_payload_secret_id=AZURE_SECRET_ID,
        )
        assert cc.azure_batch_payload_secret_id == AZURE_SECRET_ID

    def test_secret_ref_mode_refused_loud(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Issue #1633: no task-level secret channel exists in Azure Batch.

        azure-batch 15.x ``EnvironmentSetting`` exposes only ``{name,
        value}`` (verified against SDK 15.1.0 and the data-plane API
        2025-06-01), so the executor refuses the flag instead of
        shipping the raw secret as a literal task environment setting.
        """
        from osimflow.task_payload_hmac import TASK_PAYLOAD_SECRET_ENV

        monkeypatch.setenv(TASK_PAYLOAD_SECRET_ENV, "super-secret")
        ex = self._executor(payload_secret_id=AZURE_SECRET_ID)
        with pytest.raises(ValueError, match="no task-level secret-injection"):
            ex._build_environment(  # noqa: SLF001
                container="nrel/openstudio:3.11.0",
                openstudio_version="3.11.0",
                task_payload=TASK_PAYLOAD,
            )

    def test_default_mode_ships_literal_secret(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        from osimflow.task_payload_hmac import (
            TASK_PAYLOAD_SECRET_ENV,
            TASK_PAYLOAD_SIG_ENV,
            sign_task_payload,
        )

        monkeypatch.setenv(TASK_PAYLOAD_SECRET_ENV, "super-secret")
        ex = self._executor()
        with caplog.at_level(logging.WARNING):
            env = ex._build_environment(  # noqa: SLF001
                container="nrel/openstudio:3.11.0",
                openstudio_version="3.11.0",
                task_payload=TASK_PAYLOAD,
            )
        env_map = _parse_env_list(env)
        assert env_map[TASK_PAYLOAD_SECRET_ENV] == "super-secret"  # literal legacy mode
        assert env_map[TASK_PAYLOAD_SIG_ENV] == sign_task_payload(TASK_PAYLOAD, "super-secret")


class TestGoogleBatchSecretDelivery:
    def _executor(self, **kw: Any):
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
        ex.payload_secret_name = kw.get("payload_secret_name")
        ex._client = MagicMock()
        return ex

    def test_cli_flag_reaches_executor(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The real constructor hard-imports google.cloud.batch_v1; the google
        # extra is not part of the default dev install, so stub the module the
        # same way the SDK-less sibling executor tests do.
        google_cloud = types.ModuleType("google.cloud")
        batch_v1 = MagicMock()
        google_cloud.batch_v1 = batch_v1
        monkeypatch.setitem(sys.modules, "google.cloud", google_cloud)
        monkeypatch.setitem(sys.modules, "google.cloud.batch_v1", batch_v1)
        ns = _run_namespace(
            ["--executor", "google_batch", "--google-batch-payload-secret-name", GOOGLE_SECRET_NAME]
        )
        assert ns.google_batch_payload_secret_name == GOOGLE_SECRET_NAME
        executor = _build_executor(ns)
        assert executor.payload_secret_name == GOOGLE_SECRET_NAME

    def test_default_mode_ships_literal_secret(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from osimflow.task_payload_hmac import (
            TASK_PAYLOAD_SECRET_ENV,
            TASK_PAYLOAD_SIG_ENV,
            sign_task_payload,
        )

        monkeypatch.setenv(TASK_PAYLOAD_SECRET_ENV, "super-secret")
        ex = self._executor()
        env = ex._build_environment(  # noqa: SLF001
            container="nrel/openstudio:3.11.0",
            openstudio_version="3.11.0",
            task_payload=TASK_PAYLOAD,
        )
        env_map = _parse_env_list(env)
        assert env_map[TASK_PAYLOAD_SECRET_ENV] == "super-secret"  # literal legacy mode
        assert env_map[TASK_PAYLOAD_SIG_ENV] == sign_task_payload(TASK_PAYLOAD, "super-secret")

    def test_secret_ref_mode_emits_secret_variables_not_literal(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        from osimflow.task_payload_hmac import (
            TASK_PAYLOAD_SECRET_ENV,
            TASK_PAYLOAD_SIG_ENV,
            sign_task_payload,
        )

        monkeypatch.setenv(TASK_PAYLOAD_SECRET_ENV, "super-secret")
        ex = self._executor(payload_secret_name=GOOGLE_SECRET_NAME)
        with caplog.at_level(logging.WARNING):
            env = ex._build_environment(  # noqa: SLF001
                container="nrel/openstudio:3.11.0",
                openstudio_version="3.11.0",
                task_payload=TASK_PAYLOAD,
            )
        env_map = _parse_env_list(env)
        assert env_map[TASK_PAYLOAD_SIG_ENV] == sign_task_payload(TASK_PAYLOAD, "super-secret")
        assert TASK_PAYLOAD_SECRET_ENV not in env_map
        assert "super-secret" not in json.dumps(env)
        assert not any("SECURITY (issue #1633)" in r.message for r in caplog.records)

        # The submitted job carries the substrate secret reference.
        ex._submit_job(  # noqa: SLF001
            name="sim_0001",
            cpus=1,
            memory_mb=1024,
            time_min=1,
            environment=env,
        )
        env_arg = ex._batch_v1.TaskSpec.call_args.kwargs["environment"]  # type: ignore[union-attr]  # noqa: E501, SLF001
        assert env_arg["secret_variables"] == {TASK_PAYLOAD_SECRET_ENV: GOOGLE_SECRET_NAME}
        assert "super-secret" not in json.dumps(env_arg)
        assert env_arg["variables"][TASK_PAYLOAD_SIG_ENV] == sign_task_payload(
            TASK_PAYLOAD, "super-secret"
        )

    def test_legacy_mode_omits_secret_variables(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from osimflow.task_payload_hmac import TASK_PAYLOAD_SECRET_ENV

        monkeypatch.setenv(TASK_PAYLOAD_SECRET_ENV, "super-secret")
        ex = self._executor()
        env = ex._build_environment(  # noqa: SLF001
            container="nrel/openstudio:3.11.0",
            openstudio_version="3.11.0",
            task_payload=TASK_PAYLOAD,
        )
        ex._submit_job(  # noqa: SLF001
            name="sim_0001",
            cpus=1,
            memory_mb=1024,
            time_min=1,
            environment=env,
        )
        env_arg = ex._batch_v1.TaskSpec.call_args.kwargs["environment"]  # type: ignore[union-attr]  # noqa: E501, SLF001
        assert "secret_variables" not in env_arg
        assert env_arg["variables"][TASK_PAYLOAD_SECRET_ENV] == "super-secret"


class TestDockerSwarmSecretDelivery:
    def _submit_and_capture(self, ex: Any) -> dict[str, Any]:
        captured: dict[str, Any] = {}

        def fake_create(**kwargs: Any) -> MagicMock:
            captured.update(kwargs)
            fake_service = MagicMock()
            fake_service.name = "osimflow-test"
            return fake_service

        ex._client = MagicMock()
        ex._client.services.create = MagicMock(side_effect=fake_create)  # type: ignore[method-assign]  # noqa: E501, SLF001
        ex._submit_service(  # noqa: SLF001
            name="sim_0001",
            cpus=1,
            memory_mb=1024,
            time_min=60,
            openstudio_version="3.11.0",
            container="nrel/openstudio:3.11.0",
            task_payload=TASK_PAYLOAD,
        )
        return captured

    def test_cli_flag_reaches_executor(self) -> None:
        ns = _run_namespace(
            ["--executor", "docker_swarm", "--docker-swarm-payload-secret", SWARM_SECRET_NAME]
        )
        assert ns.docker_swarm_payload_secret == SWARM_SECRET_NAME
        executor = _build_executor(ns)
        assert executor.payload_secret == SWARM_SECRET_NAME

    def test_default_mode_ships_literal_secret(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from osimflow.executors.docker_swarm_executor import DockerSwarmExecutor
        from osimflow.task_payload_hmac import (
            TASK_PAYLOAD_SECRET_ENV,
            TASK_PAYLOAD_SIG_ENV,
            sign_task_payload,
        )

        monkeypatch.setenv(TASK_PAYLOAD_SECRET_ENV, "super-secret")
        ex = DockerSwarmExecutor()
        captured = self._submit_and_capture(ex)
        env_map = _parse_env_kv(captured["env"])
        assert env_map[TASK_PAYLOAD_SECRET_ENV] == "super-secret"  # literal legacy mode
        assert env_map[TASK_PAYLOAD_SIG_ENV] == sign_task_payload(TASK_PAYLOAD, "super-secret")
        assert captured["secrets"] is None
        assert "OSIMFLOW_TASK_PAYLOAD_SECRET_FILE" not in env_map

    def test_secret_ref_mode_mounts_secret_file_not_literal(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        from osimflow.executors.docker_swarm_executor import DockerSwarmExecutor
        from osimflow.task_payload_hmac import (
            TASK_PAYLOAD_SECRET_ENV,
            TASK_PAYLOAD_SECRET_FILE_ENV,
            TASK_PAYLOAD_SIG_ENV,
            sign_task_payload,
        )

        monkeypatch.setenv(TASK_PAYLOAD_SECRET_ENV, "super-secret")
        ex = DockerSwarmExecutor(payload_secret=SWARM_SECRET_NAME)
        with caplog.at_level(logging.WARNING):
            captured = self._submit_and_capture(ex)
        env_map = _parse_env_kv(captured["env"])
        assert env_map[TASK_PAYLOAD_SIG_ENV] == sign_task_payload(TASK_PAYLOAD, "super-secret")
        assert TASK_PAYLOAD_SECRET_ENV not in env_map
        assert env_map[TASK_PAYLOAD_SECRET_FILE_ENV] == f"/run/secrets/{SWARM_SECRET_NAME}"
        assert "super-secret" not in json.dumps(captured["env"])
        # Substrate-specific secret reference present (Docker secret mount).
        assert captured["secrets"] == [{"Name": SWARM_SECRET_NAME}]
        assert not any("SECURITY (issue #1633)" in r.message for r in caplog.records)

    def test_ref_without_secret_warns_unsigned(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        from osimflow.executors.docker_swarm_executor import DockerSwarmExecutor
        from osimflow.task_payload_hmac import (
            TASK_PAYLOAD_SECRET_ENV,
            TASK_PAYLOAD_SIG_ENV,
        )

        monkeypatch.delenv(TASK_PAYLOAD_SECRET_ENV, raising=False)
        ex = DockerSwarmExecutor(payload_secret=SWARM_SECRET_NAME)
        with caplog.at_level(logging.WARNING):
            captured = self._submit_and_capture(ex)
        env_map = _parse_env_kv(captured["env"])
        assert TASK_PAYLOAD_SIG_ENV not in env_map
        assert TASK_PAYLOAD_SECRET_ENV not in env_map
        assert any("cannot be signed" in r.message for r in caplog.records)
