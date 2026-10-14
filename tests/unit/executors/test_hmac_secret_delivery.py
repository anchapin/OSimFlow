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
        assert not any("SECURITY (issue #1633)" in r.message for r in caplog.records)

    def _submit(
        self, monkeypatch: pytest.MonkeyPatch, job_def_secrets: list[dict[str, str]] | None
    ) -> dict[str, Any]:
        from osimflow.executors import AWSBatchExecutor
        from osimflow.task_payload_hmac import TASK_PAYLOAD_SECRET_ENV

        monkeypatch.setenv(TASK_PAYLOAD_SECRET_ENV, "super-secret")
        ex = AWSBatchExecutor(payload_secret_arn=AWS_SECRET_ARN)
        client = MagicMock()
        props: dict[str, Any] = {}
        if job_def_secrets is not None:
            props["secrets"] = job_def_secrets
        client.describe_job_definitions.return_value = {
            "jobDefinitions": [{"revision": 1, "containerProperties": props}]
        }
        client.submit_job.return_value = {"jobId": "j-1"}
        ex._client = client  # noqa: SLF001
        environment = ex._build_environment(  # noqa: SLF001
            container="nrel/openstudio:3.11.0",
            openstudio_version="3.11.0",
            task_payload=TASK_PAYLOAD,
        )
        ex._submit_job(  # noqa: SLF001
            name="sim_0001", cpus=1, memory_mb=1024, time_min=1, environment=environment
        )
        return dict(client.submit_job.call_args.kwargs)

    def test_submit_job_has_no_secrets_override_and_passes_botocore_schema(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import botocore.session
        from botocore.validate import validate_parameters

        from osimflow.task_payload_hmac import TASK_PAYLOAD_SECRET_ENV, TASK_PAYLOAD_SIG_ENV

        kwargs = self._submit(
            monkeypatch, [{"name": TASK_PAYLOAD_SECRET_ENV, "valueFrom": AWS_SECRET_ARN}]
        )
        overrides = kwargs["containerOverrides"]
        assert "secrets" not in overrides
        assert any(e["name"] == TASK_PAYLOAD_SIG_ENV for e in overrides["environment"])
        assert "super-secret" not in json.dumps(kwargs)
        model = botocore.session.get_session().get_service_model("batch")
        validate_parameters(kwargs, model.operation_model("SubmitJob").input_shape)

    def test_botocore_schema_rejects_old_overrides_secrets_shape(self) -> None:
        import botocore.session
        from botocore.exceptions import ParamValidationError
        from botocore.validate import validate_parameters

        from osimflow.task_payload_hmac import TASK_PAYLOAD_SECRET_ENV

        old = {
            "jobName": "j",
            "jobQueue": "q",
            "jobDefinition": "d",
            "containerOverrides": {
                "secrets": [{"name": TASK_PAYLOAD_SECRET_ENV, "valueFrom": AWS_SECRET_ARN}]
            },
        }
        model = botocore.session.get_session().get_service_model("batch")
        with pytest.raises(ParamValidationError, match="secrets"):
            validate_parameters(old, model.operation_model("SubmitJob").input_shape)

    def test_describe_job_definitions_request_matches_schema(self) -> None:
        import botocore.session
        from botocore.validate import validate_parameters

        model = botocore.session.get_session().get_service_model("batch")
        shape = model.operation_model("DescribeJobDefinitions").input_shape
        validate_parameters({"jobDefinitionName": "d", "status": "ACTIVE"}, shape)
        validate_parameters({"jobDefinitions": ["d:1"]}, shape)

    @pytest.mark.parametrize(
        "secrets",
        [
            None,
            [],
            [{"name": "OSIMFLOW_TASK_PAYLOAD_SECRET", "valueFrom": "arn:other"}],
        ],
    )
    def test_missing_or_mismatched_job_def_secret_fails_before_submit(
        self, monkeypatch: pytest.MonkeyPatch, secrets: list[dict[str, str]] | None
    ) -> None:
        from osimflow.executors import AWSBatchExecutor

        ex = AWSBatchExecutor(payload_secret_arn=AWS_SECRET_ARN)
        client = MagicMock()
        props: dict[str, Any] = {} if secrets is None else {"secrets": secrets}
        client.describe_job_definitions.return_value = {
            "jobDefinitions": [{"revision": 2, "containerProperties": props}]
        }
        ex._client = client  # noqa: SLF001
        with pytest.raises(RuntimeError, match="containerProperties.secrets"):
            ex._submit_job(  # noqa: SLF001
                name="n", cpus=1, memory_mb=1, time_min=1, environment=[]
            )
        client.submit_job.assert_not_called()

    def test_job_def_validation_variants(self) -> None:
        from osimflow.executors import AWSBatchExecutor

        legacy = AWSBatchExecutor()
        legacy._get_client = MagicMock(side_effect=AssertionError)  # type: ignore[method-assign]  # noqa: SLF001
        legacy._validate_job_definition_secret("d")  # noqa: SLF001  # no ARN: no AWS call

        ex = AWSBatchExecutor(payload_secret_arn=AWS_SECRET_ARN)
        client = MagicMock()
        ex._client = client  # noqa: SLF001
        client.describe_job_definitions.return_value = {"jobDefinitions": []}
        with pytest.raises(RuntimeError, match="not found"):
            ex._validate_job_definition_secret("d")  # noqa: SLF001

        good = {
            "revision": 3,
            "containerProperties": {
                "secrets": [{"name": "OSIMFLOW_TASK_PAYLOAD_SECRET", "valueFrom": AWS_SECRET_ARN}]
            },
        }
        client.describe_job_definitions.return_value = {"jobDefinitions": [good]}
        ex._validate_job_definition_secret("d:3")  # noqa: SLF001
        client.describe_job_definitions.assert_called_with(jobDefinitions=["d:3"])
        ex._validate_job_definition_secret("d:3")  # noqa: SLF001  # cached
        assert client.describe_job_definitions.call_count == 2

    def test_ref_without_secret_warns_unsigned(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Issue #2025: payload_secret_arn without a signing secret fails fast."""
        from osimflow.executors import AWSBatchExecutor
        from osimflow.task_payload_hmac import TASK_PAYLOAD_SECRET_ENV

        monkeypatch.delenv(TASK_PAYLOAD_SECRET_ENV, raising=False)
        ex = AWSBatchExecutor(payload_secret_arn=AWS_SECRET_ARN)
        with pytest.raises(RuntimeError, match="aws-batch-payload-secret-arn"):
            ex._build_environment(  # noqa: SLF001
                container="nrel/openstudio:3.11.0",
                openstudio_version="3.11.0",
                task_payload=TASK_PAYLOAD,
            )

    def test_missing_secret_without_arn_fails_before_submit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Issue #2025: missing secret fails fast before submit_job is called."""
        from osimflow.executors import AWSBatchExecutor
        from osimflow.task_payload_hmac import TASK_PAYLOAD_SECRET_ENV

        monkeypatch.delenv(TASK_PAYLOAD_SECRET_ENV, raising=False)
        ex = AWSBatchExecutor()
        client = MagicMock()
        ex._client = client  # noqa: SLF001
        with pytest.raises(RuntimeError, match=TASK_PAYLOAD_SECRET_ENV):
            ex.submit(lambda: None, name="sim_0001")
        client.submit_job.assert_not_called()

    def test_secret_ref_resolves_via_secrets_client_when_env_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Issue #2025 / #2042: when _secrets_client is provided, ARN resolves secret for signing."""
        from osimflow.executors import AWSBatchExecutor
        from osimflow.task_payload_hmac import (
            TASK_PAYLOAD_SECRET_ENV,
            TASK_PAYLOAD_SIG_ENV,
            sign_task_payload,
        )

        monkeypatch.delenv(TASK_PAYLOAD_SECRET_ENV, raising=False)
        ex = AWSBatchExecutor(payload_secret_arn=AWS_SECRET_ARN)
        secret_val = "arn-fetched-secret-" + "x" * 32
        secrets_client = MagicMock()
        secrets_client.get_secret_value.return_value = {"SecretString": f"{secret_val}\n"}
        ex._secrets_client = secrets_client  # type: ignore[attr-defined]  # noqa: SLF001
        env = ex._build_environment(  # noqa: SLF001
            container="nrel/openstudio:3.11.0",
            openstudio_version="3.11.0",
            task_payload=TASK_PAYLOAD,
        )
        env_map = _parse_env_list(env)
        assert env_map[TASK_PAYLOAD_SIG_ENV] == sign_task_payload(TASK_PAYLOAD, secret_val)
        assert TASK_PAYLOAD_SECRET_ENV not in env_map

    def test_autodiscovers_secret_arn_from_job_def_and_fetches_secretsmanager(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Issue #2042: auto-discovers secret ARN from highest active job def revision and fetches from Secrets Manager."""
        from osimflow.executors import AWSBatchExecutor
        from osimflow.task_payload_hmac import (
            TASK_PAYLOAD_SECRET_ENV,
            TASK_PAYLOAD_SIG_ENV,
            sign_task_payload,
        )

        monkeypatch.delenv(TASK_PAYLOAD_SECRET_ENV, raising=False)
        ex = AWSBatchExecutor(job_definition="osimflow-openstudio-job-def")
        batch_client = MagicMock()
        batch_client.describe_job_definitions.return_value = {
            "jobDefinitions": [
                {
                    "jobDefinitionName": "osimflow-openstudio-job-def",
                    "revision": 1,
                    "status": "ACTIVE",
                    "containerProperties": {"secrets": []},
                },
                {
                    "jobDefinitionName": "osimflow-openstudio-job-def",
                    "revision": 3,
                    "status": "ACTIVE",
                    "containerProperties": {
                        "secrets": [
                            {
                                "name": TASK_PAYLOAD_SECRET_ENV,
                                "valueFrom": AWS_SECRET_ARN,
                            }
                        ]
                    },
                },
            ]
        }
        secret_val = "sm-secret-value-at-least-32-bytes-long!"
        secrets_client = MagicMock()
        secrets_client.get_secret_value.return_value = {"SecretString": f"{secret_val}\r\n"}
        ex._client = batch_client  # noqa: SLF001
        ex._secrets_client = secrets_client  # noqa: SLF001

        env1 = ex._build_environment(  # noqa: SLF001
            container="nrel/openstudio:3.11.0",
            openstudio_version="3.11.0",
            task_payload=TASK_PAYLOAD,
        )
        env2 = ex._build_environment(  # noqa: SLF001
            container="nrel/openstudio:3.11.0",
            openstudio_version="3.11.0",
            task_payload=TASK_PAYLOAD,
        )
        env_map = _parse_env_list(env1)
        assert ex.payload_secret_arn == AWS_SECRET_ARN
        assert env_map[TASK_PAYLOAD_SIG_ENV] == sign_task_payload(TASK_PAYLOAD, secret_val)
        assert TASK_PAYLOAD_SECRET_ENV not in env_map
        assert _parse_env_list(env2)[TASK_PAYLOAD_SIG_ENV] == env_map[TASK_PAYLOAD_SIG_ENV]
        # Cached on executor instance across submissions
        batch_client.describe_job_definitions.assert_called_once_with(
            jobDefinitionName="osimflow-openstudio-job-def", status="ACTIVE"
        )
        secrets_client.get_secret_value.assert_called_once_with(SecretId=AWS_SECRET_ARN)

    def test_autodiscovers_ssm_parameter_arn_and_fetches_with_decryption(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Issue #2042: auto-discovers SSM parameter ARN (with region inferred from ARN) and fetches WithDecryption=True."""
        from osimflow.executors import AWSBatchExecutor
        from osimflow.task_payload_hmac import (
            TASK_PAYLOAD_SECRET_ENV,
            TASK_PAYLOAD_SIG_ENV,
            sign_task_payload,
        )

        ssm_arn = "arn:aws:ssm:us-west-2:123456789012:parameter/osimflow/task_payload_secret"
        monkeypatch.delenv(TASK_PAYLOAD_SECRET_ENV, raising=False)
        ex = AWSBatchExecutor(
            job_definition="arn:aws:batch:us-west-2:123456789012:job-definition/my-jd:4"
        )
        batch_client = MagicMock()
        batch_client.describe_job_definitions.return_value = {
            "jobDefinitions": [
                {
                    "jobDefinitionName": "my-jd",
                    "revision": 4,
                    "status": "ACTIVE",
                    "containerProperties": {
                        "secrets": [
                            {
                                "name": TASK_PAYLOAD_SECRET_ENV,
                                "valueFrom": ssm_arn,
                            }
                        ]
                    },
                }
            ]
        }
        secret_val = "ssm-secret-value-at-least-32-bytes-long!"
        ssm_client = MagicMock()
        ssm_client.get_parameter.return_value = {"Parameter": {"Value": f"{secret_val}\n"}}
        ex._client = batch_client  # noqa: SLF001
        ex._ssm_client = ssm_client  # noqa: SLF001

        env = ex._build_environment(  # noqa: SLF001
            container="nrel/openstudio:3.11.0",
            openstudio_version="3.11.0",
            task_payload=TASK_PAYLOAD,
        )
        env_map = _parse_env_list(env)
        assert ex.payload_secret_arn == ssm_arn
        assert env_map[TASK_PAYLOAD_SIG_ENV] == sign_task_payload(TASK_PAYLOAD, secret_val)
        assert TASK_PAYLOAD_SECRET_ENV not in env_map
        batch_client.describe_job_definitions.assert_called_once_with(
            jobDefinitions=["arn:aws:batch:us-west-2:123456789012:job-definition/my-jd:4"]
        )
        ssm_client.get_parameter.assert_called_once_with(Name=ssm_arn, WithDecryption=True)

    def test_autodiscovery_omits_plaintext_secret_even_when_local_env_is_set(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Issue #2042: if OSIMFLOW_TASK_PAYLOAD_SECRET is set locally AND job def has secrets entry, never leak plaintext in containerOverrides.environment."""
        from osimflow.executors import AWSBatchExecutor
        from osimflow.task_payload_hmac import (
            TASK_PAYLOAD_SECRET_ENV,
            TASK_PAYLOAD_SIG_ENV,
            sign_task_payload,
        )

        local_secret = "local-secret-at-least-32-bytes-long-12345"
        monkeypatch.setenv(TASK_PAYLOAD_SECRET_ENV, local_secret)
        ex = AWSBatchExecutor(job_definition="osimflow-openstudio-job-def")
        batch_client = MagicMock()
        batch_client.describe_job_definitions.return_value = {
            "jobDefinitions": [
                {
                    "jobDefinitionName": "osimflow-openstudio-job-def",
                    "revision": 2,
                    "status": "ACTIVE",
                    "containerProperties": {
                        "secrets": [
                            {
                                "name": TASK_PAYLOAD_SECRET_ENV,
                                "valueFrom": AWS_SECRET_ARN,
                            }
                        ]
                    },
                }
            ]
        }
        ex._client = batch_client  # noqa: SLF001

        env = ex._build_environment(  # noqa: SLF001
            container="nrel/openstudio:3.11.0",
            openstudio_version="3.11.0",
            task_payload=TASK_PAYLOAD,
        )
        env_map = _parse_env_list(env)
        assert ex.payload_secret_arn == AWS_SECRET_ARN
        assert TASK_PAYLOAD_SECRET_ENV not in env_map
        assert env_map[TASK_PAYLOAD_SIG_ENV] == sign_task_payload(TASK_PAYLOAD, local_secret)

    def test_describe_job_definitions_failure_fails_closed_against_plaintext_downgrade(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Issue #2042: describe_job_definitions failure raises RuntimeError instead of silently downgrading local secret to containerOverrides.environment."""
        from osimflow.executors import AWSBatchExecutor
        from osimflow.task_payload_hmac import TASK_PAYLOAD_SECRET_ENV

        monkeypatch.setenv(TASK_PAYLOAD_SECRET_ENV, "local-secret-at-least-32-bytes-long-12345")
        ex = AWSBatchExecutor(job_definition="osimflow-openstudio-job-def")
        batch_client = MagicMock()
        batch_client.describe_job_definitions.side_effect = RuntimeError("AccessDeniedException")
        ex._client = batch_client  # noqa: SLF001

        with pytest.raises(RuntimeError, match="batch:DescribeJobDefinitions"):
            ex._build_environment(  # noqa: SLF001
                container="nrel/openstudio:3.11.0",
                openstudio_version="3.11.0",
                task_payload=TASK_PAYLOAD,
            )

    @pytest.mark.parametrize(
        ("secret_payload", "match_pattern"),
        [
            ({"SecretBinary": b"binary-secret-value-at-least-32-bytes"}, "SecretBinary"),
            ({"SecretString": "   \n"}, "empty or whitespace-only"),
            ({"SecretString": "too-short-secret\n"}, "at least 32 UTF-8 bytes"),
        ],
    )
    def test_fetched_secret_validation_rejects_binary_empty_or_short(
        self,
        monkeypatch: pytest.MonkeyPatch,
        secret_payload: dict[str, Any],
        match_pattern: str,
    ) -> None:
        """Issue #2042: SecretBinary, empty/whitespace, and <32-byte secrets raise ValueError."""
        from osimflow.executors import AWSBatchExecutor
        from osimflow.task_payload_hmac import TASK_PAYLOAD_SECRET_ENV

        monkeypatch.delenv(TASK_PAYLOAD_SECRET_ENV, raising=False)
        ex = AWSBatchExecutor(payload_secret_arn=AWS_SECRET_ARN)
        secrets_client = MagicMock()
        secrets_client.get_secret_value.return_value = secret_payload
        ex._secrets_client = secrets_client  # noqa: SLF001

        with pytest.raises(ValueError, match=match_pattern):
            ex._resolve_signing_secret()  # noqa: SLF001

    def test_arn_region_inferred_when_constructing_secrets_and_ssm_clients(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Issue #2042: region is parsed from the ARN when building Secrets Manager / SSM clients."""
        from osimflow.executors import AWSBatchExecutor

        ex = AWSBatchExecutor(region_name="us-east-1")
        captured_calls: list[tuple[str, str | None]] = []

        def fake_make_client(service: str, *, region_name: str | None = None) -> Any:
            captured_calls.append((service, region_name))
            mock_c = MagicMock()
            if service == "secretsmanager":
                mock_c.get_secret_value.return_value = {"SecretString": "a" * 32}
            else:
                mock_c.get_parameter.return_value = {"Parameter": {"Value": "b" * 32}}
            return mock_c

        monkeypatch.setattr(ex, "_make_client", fake_make_client)
        sm_arn = "arn:aws:secretsmanager:eu-west-1:123456789012:secret:osimflow/hmac-AbCdEf"
        ssm_arn = "arn:aws:ssm:ap-northeast-1:123456789012:parameter/osimflow/hmac"

        assert ex._fetch_secret_from_arn(sm_arn) == "a" * 32  # noqa: SLF001
        assert ex._fetch_secret_from_arn(ssm_arn) == "b" * 32  # noqa: SLF001
        assert captured_calls == [
            ("secretsmanager", "eu-west-1"),
            ("ssm", "ap-northeast-1"),
        ]

    def test_secretsmanager_selector_json_key_version_stage_and_version_id(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Issue #2042: ECS/Batch valueFrom selector arn:...:secret:name-xxx:json-key:version-stage:version-id is parsed and extracted."""
        import json as _json

        from osimflow.executors import AWSBatchExecutor
        from osimflow.task_payload_hmac import (
            TASK_PAYLOAD_SECRET_ENV,
            TASK_PAYLOAD_SIG_ENV,
            sign_task_payload,
        )

        monkeypatch.delenv(TASK_PAYLOAD_SECRET_ENV, raising=False)
        base_arn = "arn:aws:secretsmanager:us-east-1:123456789012:secret:osimflow/hmac-AbCdEf"
        full_value_from = f"{base_arn}:hmac_key:AWSCURRENT:ver-uuid-1234"
        ex = AWSBatchExecutor(job_definition="osimflow-openstudio-job-def")
        batch_client = MagicMock()
        batch_client.describe_job_definitions.return_value = {
            "jobDefinitions": [
                {
                    "jobDefinitionName": "osimflow-openstudio-job-def",
                    "revision": 5,
                    "status": "ACTIVE",
                    "containerProperties": {
                        "secrets": [
                            {
                                "name": TASK_PAYLOAD_SECRET_ENV,
                                "valueFrom": full_value_from,
                            }
                        ]
                    },
                }
            ]
        }
        extracted_secret = "json-extracted-secret-at-least-32-bytes!"
        secrets_client = MagicMock()
        secrets_client.get_secret_value.return_value = {
            "SecretString": _json.dumps({"hmac_key": extracted_secret, "other": "val"})
        }
        ex._client = batch_client  # noqa: SLF001
        ex._secrets_client = secrets_client  # noqa: SLF001

        env = ex._build_environment(  # noqa: SLF001
            container="nrel/openstudio:3.11.0",
            openstudio_version="3.11.0",
            task_payload=TASK_PAYLOAD,
        )
        env_map = _parse_env_list(env)
        assert ex.payload_secret_arn == full_value_from
        assert env_map[TASK_PAYLOAD_SIG_ENV] == sign_task_payload(TASK_PAYLOAD, extracted_secret)
        secrets_client.get_secret_value.assert_called_once_with(
            SecretId=base_arn,
            VersionStage="AWSCURRENT",
            VersionId="ver-uuid-1234",
        )

    @pytest.mark.parametrize(
        ("arn", "secret_string", "match_pattern"),
        [
            (
                "arn:aws:s3:::my-bucket/secret",
                "a" * 32,
                "Unsupported ARN service",
            ),
            (
                f"{AWS_SECRET_ARN}:hmac_key",
                "not-valid-json",
                "not valid JSON",
            ),
            (
                f"{AWS_SECRET_ARN}:missing_key",
                '{"other_key": "' + "a" * 32 + '"}',
                "does not contain key",
            ),
            (
                f"{AWS_SECRET_ARN}:null_key",
                '{"null_key": null}',
                "is null",
            ),
        ],
    )
    def test_fetch_secret_from_arn_error_branches(
        self,
        arn: str,
        secret_string: str,
        match_pattern: str,
    ) -> None:
        """Issue #2042: unsupported ARN service and malformed/missing JSON selector keys raise ValueError."""
        from osimflow.executors import AWSBatchExecutor

        ex = AWSBatchExecutor(payload_secret_arn=arn)
        secrets_client = MagicMock()
        secrets_client.get_secret_value.return_value = {"SecretString": secret_string}
        ex._secrets_client = secrets_client  # noqa: SLF001

        with pytest.raises(ValueError, match=match_pattern):
            ex._fetch_secret_from_arn(arn)  # noqa: SLF001

    def test_fetch_secret_from_arn_missing_secret_string_and_ssm_missing_value(
        self,
    ) -> None:
        """Issue #2042: missing SecretString or missing SSM Parameter.Value raise clear errors."""
        from osimflow.executors import AWSBatchExecutor

        ex = AWSBatchExecutor()
        secrets_client = MagicMock()
        secrets_client.get_secret_value.return_value = {}
        ex._secrets_client = secrets_client  # noqa: SLF001
        with pytest.raises(ValueError, match="returned no SecretString"):
            ex._fetch_secret_from_arn(AWS_SECRET_ARN)  # noqa: SLF001

        ssm_arn = "arn:aws:ssm:us-east-1:123456789012:parameter/osimflow/hmac"
        ssm_client = MagicMock()
        ssm_client.get_parameter.return_value = {"Parameter": {}}
        ex._ssm_client = ssm_client  # noqa: SLF001
        with pytest.raises(RuntimeError, match=r"returned no Parameter\.Value"):
            ex._fetch_secret_from_arn(ssm_arn)  # noqa: SLF001

    def test_autodiscovery_handles_empty_or_unconfigured_job_definitions(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Issue #2042: job definition without secrets entry caches None and leaves payload_secret_arn unset."""
        from osimflow.executors import AWSBatchExecutor
        from osimflow.task_payload_hmac import TASK_PAYLOAD_SECRET_ENV

        monkeypatch.setenv(TASK_PAYLOAD_SECRET_ENV, "local-secret-at-least-32-bytes-long-12345")
        ex = AWSBatchExecutor(job_definition="osimflow-openstudio-job-def")
        batch_client = MagicMock()
        batch_client.describe_job_definitions.return_value = {
            "jobDefinitions": [
                {
                    "jobDefinitionName": "osimflow-openstudio-job-def",
                    "revision": 1,
                    "status": "ACTIVE",
                    "containerProperties": {"secrets": [{"name": "OTHER", "valueFrom": "arn:x"}]},
                }
            ]
        }
        ex._client = batch_client  # noqa: SLF001
        assert ex._resolve_effective_payload_secret_arn() is None  # noqa: SLF001
        # Second call hits cache without re-querying
        assert ex._resolve_effective_payload_secret_arn() is None  # noqa: SLF001
        batch_client.describe_job_definitions.assert_called_once()

        # Empty jobDefinitions list
        ex2 = AWSBatchExecutor(job_definition="missing-jd")
        batch_client2 = MagicMock()
        batch_client2.describe_job_definitions.return_value = {"jobDefinitions": []}
        ex2._client = batch_client2  # noqa: SLF001
        assert ex2._resolve_effective_payload_secret_arn() is None  # noqa: SLF001

        # Job definition ARN infers region when _client is None
        ex3 = AWSBatchExecutor(
            job_definition="arn:aws:batch:eu-central-1:123456789012:job-definition/my-jd:1"
        )
        regions_seen: list[str | None] = []
        batch_client3 = MagicMock()
        batch_client3.describe_job_definitions.return_value = {
            "jobDefinitions": [
                {
                    "jobDefinitionName": "my-jd",
                    "revision": 1,
                    "status": "ACTIVE",
                    "containerProperties": {
                        "secrets": [
                            {"name": TASK_PAYLOAD_SECRET_ENV, "valueFrom": "   "},
                            {"name": TASK_PAYLOAD_SECRET_ENV, "valueFrom": AWS_SECRET_ARN},
                        ]
                    },
                }
            ]
        }

        def fake_make_batch(service: str, *, region_name: str | None = None) -> Any:
            regions_seen.append(region_name)
            return batch_client3

        monkeypatch.setattr(ex3, "_make_client", fake_make_batch)
        assert ex3._resolve_effective_payload_secret_arn() == AWS_SECRET_ARN  # noqa: SLF001
        assert regions_seen == ["eu-central-1"]
        # Second call with payload_secret_arn unset hits cache with discovered value
        ex3.payload_secret_arn = None
        assert ex3._resolve_effective_payload_secret_arn() == AWS_SECRET_ARN  # noqa: SLF001

        # Bare secret name (not an ARN) falls back to secretsmanager without ARN region
        ex4 = AWSBatchExecutor()
        secrets_client4 = MagicMock()
        secrets_client4.get_secret_value.return_value = {"SecretString": "c" * 32}
        ex4._secrets_client = secrets_client4  # noqa: SLF001
        assert ex4._fetch_secret_from_arn("osimflow/hmac-secret") == "c" * 32  # noqa: SLF001
        secrets_client4.get_secret_value.assert_called_once_with(SecretId="osimflow/hmac-secret")


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

    def test_secret_ref_mode_refused_loud(self, monkeypatch: pytest.MonkeyPatch) -> None:
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
        # Issue #1682: Azure emits the same SECURITY warning the other five
        # substrates (AWS / Google / K8s / Nomad / Swarm) emit on the
        # literal-secret path so operators get a single, consistent migration
        # signal — drives them to the pool-level managed-identity + Key Vault
        # pattern documented in docs/secret-management.md (issue #1633).
        assert any("SECURITY (issue #1682)" in r.message for r in caplog.records), (
            "expected Azure literal-secret SECURITY warning (issue #1682)"
        )

    def test_default_mode_no_secret_no_warning(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Issue #1682: no warning fires when ``OSIMFLOW_TASK_PAYLOAD_SECRET`` is unset.

        Regression guard — the warning must gate on the env var being
        present, not on every ``_build_environment`` call (legacy
        unsigned mode would spam operators without it).
        """
        from osimflow.task_payload_hmac import TASK_PAYLOAD_SECRET_ENV

        monkeypatch.delenv(TASK_PAYLOAD_SECRET_ENV, raising=False)
        ex = self._executor()
        with caplog.at_level(logging.WARNING):
            env = ex._build_environment(  # noqa: SLF001
                container="nrel/openstudio:3.11.0",
                openstudio_version="3.11.0",
                task_payload=TASK_PAYLOAD,
            )
        # Legacy unsigned mode: no secret, no signature, no warning.
        names = {e["name"] for e in env}
        assert TASK_PAYLOAD_SECRET_ENV not in names
        assert not any("SECURITY (issue #1682)" in r.message for r in caplog.records), (
            "Azure must not emit the SECURITY warning when no secret is set"
        )


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

    def test_default_mode_ships_literal_secret(self, monkeypatch: pytest.MonkeyPatch) -> None:
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

    def test_legacy_mode_omits_secret_variables(self, monkeypatch: pytest.MonkeyPatch) -> None:
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

    def test_default_mode_ships_literal_secret(self, monkeypatch: pytest.MonkeyPatch) -> None:
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
