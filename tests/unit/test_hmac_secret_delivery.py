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

import logging
from typing import Any

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
