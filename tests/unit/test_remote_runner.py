from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from osimflow import remote_runner


class _FakeUploadStorage:
    def __init__(self) -> None:
        self.uploaded: list[tuple[str, str]] = []

    def upload_file(self, local_path: Path, remote_path: str) -> None:
        self.uploaded.append((str(local_path), remote_path))

    def upload_dir(self, local_dir: Path, remote_prefix: str) -> None:
        self.uploaded.append((str(local_dir), remote_prefix))


def test_resolve_step_fn_supports_aggregate_and_plots() -> None:
    assert remote_runner._resolve_step_fn("aggregate").__name__ == "aggregate_results"  # noqa: SLF001
    assert remote_runner._resolve_step_fn("plots").__name__ == "generate_plots"  # noqa: SLF001


def test_upload_artifacts_object_storage_file_and_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outdir = tmp_path / "run-1"
    file_path = outdir / "aggregated_results.csv"
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text("sample_id\n0001\n", encoding="utf-8")
    sim_dir = outdir / "work" / "sim" / "0001"
    sim_dir.mkdir(parents=True, exist_ok=True)
    (sim_dir / "eplusout.sql").write_text("-- sql --", encoding="utf-8")

    fake = _FakeUploadStorage()
    monkeypatch.setattr(remote_runner, "build_result_storage", lambda **_: fake)
    monkeypatch.setenv("OSIMFLOW_RESULT_TRANSPORT_MODE", "object_storage")
    monkeypatch.setenv("OSIMFLOW_RESULT_STORAGE_BACKEND", "s3")
    monkeypatch.setenv("OSIMFLOW_RESULT_STORAGE_BUCKET", "bucket")
    monkeypatch.setenv("OSIMFLOW_RESULT_STORAGE_PREFIX", "run-1")

    remote_runner._upload_artifacts_for_object_storage([file_path, sim_dir])  # noqa: SLF001

    assert (str(file_path), "aggregated_results.csv") in fake.uploaded
    assert (str(sim_dir), "work/sim/0001") in fake.uploaded


class TestVerifyContractVersion:
    def test_version_mismatch_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OSIMFLOW_CONTRACT_VERSION", "0.0.0")
        with pytest.raises(RuntimeError, match="BYOS contract version mismatch"):
            remote_runner._verify_contract_version()  # noqa: SLF001

    def test_version_match_succeeds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(
            "OSIMFLOW_CONTRACT_VERSION",
            remote_runner.BYOS_CONTRACT_VERSION,
        )
        remote_runner._verify_contract_version()  # noqa: SLF001 — no exception

    def test_missing_version_warns(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.delenv("OSIMFLOW_CONTRACT_VERSION", raising=False)
        with caplog.at_level("WARNING"):
            remote_runner._verify_contract_version()  # noqa: SLF001 — no exception
        assert "OSIMFLOW_CONTRACT_VERSION is not set" in caplog.text


class TestNegotiateVersion:
    def test_negotiate_version_returns_supported_versions(self) -> None:
        result = remote_runner.negotiate_version()
        assert result["ok"] is True
        assert isinstance(result["supported_versions"], list)
        assert remote_runner.BYOS_CONTRACT_VERSION in result["supported_versions"]

    def test_negotiate_version_cli_flag(self, tmp_path: Path) -> None:
        import os

        env = os.environ.copy()
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2])
        result = subprocess.run(
            [sys.executable, "-m", "osimflow.remote_runner", "--negotiate-version"],
            capture_output=True,
            text=True,
            cwd=str(tmp_path),
            env=env,
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"
        parsed = json.loads(result.stdout.strip())
        assert parsed["ok"] is True
        assert remote_runner.BYOS_CONTRACT_VERSION in parsed["supported_versions"]
