"""Unit tests for container signature verification (issue #1385).

Covers:

* ``verify_image_signature`` success / rejection / missing-binary paths
  (with a stubbed ``subprocess.run`` — no real cosign or registry).
* Command construction: identity, issuer, and image ref ordering must
  match the keyless ``cosign verify`` contract.
* ``build_cosign_image_ref`` digest-pinned vs version-tagged forms.
* ``Campaign`` init wiring: the campaign refuses to construct when
  verification fails, and proceeds when it passes.
* CLI flag plumbing (``--require-cosign-identity`` / ``--cosign-oidc-issuer``).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from osimflow import CampaignConfig
from osimflow.cosign import (
    COSIGN_VERIFY_TIMEOUT_S,
    DEFAULT_COSIGN_OIDC_ISSUER,
    CosignVerificationError,
    build_cosign_image_ref,
    verify_image_signature,
    write_cosign_receipt,
)
from osimflow.executors import LocalExecutor


def _completed(returncode: int, stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout="", stderr=stderr)


class TestVerifyImageSignature:
    def test_success_invokes_cosign_with_keyless_flags(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, Any] = {}

        def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            captured["cmd"] = cmd
            captured["kwargs"] = kwargs
            return _completed(0)

        monkeypatch.setattr("osimflow.cosign.subprocess.run", fake_run)
        verify_image_signature(
            "docker.io/nrel/openstudio@sha256:abc",
            "https://github.com/NREL/.github/.github/workflows/build.yml@refs/heads/main",
            "https://token.actions.githubusercontent.com",
            cosign_binary="/usr/local/bin/cosign",
        )
        assert captured["cmd"] == [
            "/usr/local/bin/cosign",
            "verify",
            "--certificate-identity",
            "https://github.com/NREL/.github/.github/workflows/build.yml@refs/heads/main",
            "--certificate-oidc-issuer",
            "https://token.actions.githubusercontent.com",
            "docker.io/nrel/openstudio@sha256:abc",
        ]
        assert captured["kwargs"]["timeout"] == COSIGN_VERIFY_TIMEOUT_S
        assert captured["kwargs"]["capture_output"] is True

    def test_nonzero_exit_raises_with_stderr_tail(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "osimflow.cosign.subprocess.run",
            lambda cmd, **kw: _completed(1, stderr="ERROR: no matching signatures"),
        )
        with pytest.raises(CosignVerificationError, match="no matching signatures"):
            verify_image_signature(
                "docker.io/nrel/openstudio@sha256:bad",
                "identity",
                cosign_binary="cosign",
            )

    def test_timeout_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake_run(cmd: list[str], **kwargs: Any) -> None:
            raise subprocess.TimeoutExpired(cmd, 120)

        monkeypatch.setattr("osimflow.cosign.subprocess.run", fake_run)
        with pytest.raises(CosignVerificationError, match="timed out"):
            verify_image_signature("ref", "identity", cosign_binary="cosign")

    def test_os_error_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake_run(cmd: list[str], **kwargs: Any) -> None:
            raise OSError("exec format error")

        monkeypatch.setattr("osimflow.cosign.subprocess.run", fake_run)
        with pytest.raises(CosignVerificationError, match="could not execute"):
            verify_image_signature("ref", "identity", cosign_binary="/nonexistent/cosign")

    def test_missing_binary_raises_before_subprocess(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("osimflow.cosign.shutil.which", lambda _: None)
        called = False

        def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            nonlocal called
            called = True
            return _completed(0)

        monkeypatch.setattr("osimflow.cosign.subprocess.run", fake_run)
        with pytest.raises(CosignVerificationError, match="not found on PATH"):
            verify_image_signature("ref", "identity")
        assert not called

    def test_default_issuer_applied(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, Any] = {}

        def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            captured["cmd"] = cmd
            return _completed(0)

        monkeypatch.setattr("osimflow.cosign.subprocess.run", fake_run)
        verify_image_signature("ref", "identity", cosign_binary="cosign")
        assert captured["cmd"][5] == DEFAULT_COSIGN_OIDC_ISSUER


class TestBuildCosignImageRef:
    def test_digest_pinned_wins(self) -> None:
        ref = build_cosign_image_ref(
            container="docker.io/nrel/openstudio",
            container_digest="sha256:" + "a" * 64,
            openstudio_version="3.11.0",
        )
        assert ref == f"docker.io/nrel/openstudio@sha256:{'a' * 64}"

    def test_version_tag_fallback(self) -> None:
        ref = build_cosign_image_ref(
            container="docker.io/nrel/openstudio",
            container_digest=None,
            openstudio_version="3.11.0",
        )
        assert ref == "docker.io/nrel/openstudio:3.11.0"

    def test_trailing_slash_stripped(self) -> None:
        ref = build_cosign_image_ref(
            container="docker.io/nrel/openstudio/",
            container_digest=None,
            openstudio_version="24.1.0",
        )
        assert ref == "docker.io/nrel/openstudio:24.1.0"


class TestCampaignWiring:
    @staticmethod
    def _cfg(tmp_path: Path, **overrides: Any) -> CampaignConfig:
        variables = tmp_path / "variables.yml"
        variables.write_text(
            "algorithm: lhs\n"
            "variables:\n"
            "  - name: wwr\n"
            "    distribution: uniform\n"
            "    min: 0.2\n"
            "    max: 0.6\n"
            "    measure_argument: SetEnvelopePerformance.wwr\n"
        )
        pkg = tmp_path / "template"
        pkg.mkdir(exist_ok=True)
        (pkg / "workflow.osw").write_text("{}")
        return CampaignConfig(
            input_variables=variables,
            template_sim_package=pkg,
            n_samples=1,
            outdir=tmp_path / "out",
            openstudio_version="3.11.0",
            **overrides,
        )

    def test_verification_failure_refuses_to_construct(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(*args: Any, **kwargs: Any) -> None:
            raise CosignVerificationError("substituted image")

        monkeypatch.setattr("osimflow.campaign.verify_image_signature", boom)
        from osimflow import Campaign

        cfg = self._cfg(tmp_path, require_cosign_identity="id@example.com")
        with pytest.raises(CosignVerificationError, match="substituted"):
            Campaign(cfg=cfg, executor=LocalExecutor(max_workers=1))

    def test_verification_success_constructs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, Any] = {}

        def fake_verify(image_ref: str, identity: str, issuer: str) -> None:
            captured["image_ref"] = image_ref
            captured["identity"] = identity
            captured["issuer"] = issuer

        monkeypatch.setattr("osimflow.campaign.verify_image_signature", fake_verify)
        from osimflow import Campaign

        cfg = self._cfg(
            tmp_path,
            require_cosign_identity="id@example.com",
            cosign_oidc_issuer="https://issuer.example.test",
            container_digest="sha256:" + "b" * 64,
        )
        Campaign(cfg=cfg, executor=LocalExecutor(max_workers=1))
        assert captured["identity"] == "id@example.com"
        assert captured["issuer"] == "https://issuer.example.test"
        assert captured["image_ref"] == f"docker.io/nrel/openstudio@sha256:{'b' * 64}"

    def test_flag_not_set_skips_verification(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        called = False

        def fake_verify(*args: Any, **kwargs: Any) -> None:
            nonlocal called
            called = True

        monkeypatch.setattr("osimflow.campaign.verify_image_signature", fake_verify)
        from osimflow import Campaign

        Campaign(cfg=self._cfg(tmp_path), executor=LocalExecutor(max_workers=1))
        assert not called


class TestCliFlag:
    def test_flags_parse_into_config_namespace(self) -> None:

        from osimflow.__main__ import _build_parser

        parser = _build_parser()
        args = parser.parse_args(
            [
                "run",
                "--executor",
                "local",
                "--input_variables",
                "v.yml",
                "--template_sim_package",
                "pkg",
                "--n_samples",
                "1",
                "--outdir",
                "out",
                "--require-cosign-identity",
                "id@example.com",
                "--cosign-oidc-issuer",
                "https://issuer.example.test",
            ]
        )
        ns = vars(args)
        assert ns["require_cosign_identity"] == "id@example.com"
        assert ns["cosign_oidc_issuer"] == "https://issuer.example.test"


class TestReceipt:
    def test_write_cosign_receipt(self, tmp_path: Path) -> None:
        path = write_cosign_receipt(
            tmp_path,
            image_ref="docker.io/nrel/openstudio@sha256:abc",
            certificate_identity="id@example.com",
            certificate_oidc_issuer="https://issuer.example.test",
        )
        data = json.loads(path.read_text())
        assert data["image_ref"] == "docker.io/nrel/openstudio@sha256:abc"
        assert data["certificate_identity"] == "id@example.com"
        assert data["certificate_oidc_issuer"] == "https://issuer.example.test"
        assert data["verified"] is True
