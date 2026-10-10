"""Failed remote samples ship out.osw/run.log and are reported (issue #1878)."""

import json
import shutil
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from osimflow import remote_runner
from osimflow.executors import transport as transport_mod
from osimflow.executors.aws_batch_executor import _AWSBatchHandle
from osimflow.executors.base import BaseExecutor
from osimflow.executors.transport import ResultTransportConfig
from osimflow.input_staging import InputStager
from osimflow.s3_campaign import (
    COMPLETE_MARKER,
    FAILED_MARKER,
    S3CampaignStore,
    new_handoff,
)
from osimflow.storage import ResultStorage
from osimflow.task_payload_hmac import (
    TASK_PAYLOAD_SECRET_ENV,
    build_signature_env,
    build_transport_signature_env,
)


class DirStorage(ResultStorage):
    name = "dir"

    def __init__(self, root: Path, prefix: str = "") -> None:
        self.root = root
        self.prefix = prefix
        root.mkdir(parents=True, exist_ok=True)

    def _p(self, key: str) -> Path:
        return self.root / self.prefix / key

    def upload_file(self, local_path: Path, remote_path: str) -> None:
        dest = self._p(remote_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(local_path, dest)

    def download_file(self, remote_path: str, local_path: Path) -> None:
        src = self._p(remote_path)
        if not src.is_file():
            raise FileNotFoundError(remote_path)
        local_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, local_path)

    async def upload_file_async(self, local_path: Path, remote_path: str) -> None:
        self.upload_file(local_path, remote_path)

    async def download_file_async(self, remote_path: str, local_path: Path) -> None:
        self.download_file(remote_path, local_path)

    def list_results(self, prefix: str = "") -> list[str]:
        base = self.root / self.prefix
        return sorted(
            p.relative_to(base).as_posix()
            for p in base.rglob("*")
            if p.is_file() and p.relative_to(base).as_posix().startswith(prefix)
        )

    async def list_results_async(self, prefix: str = "") -> list[str]:
        return self.list_results(prefix)


CAMPAIGN = "camp"


@pytest.fixture
def failed_job(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any) -> dict[str, Any]:
    """Run a sim step that raises after writing diagnostics on an isolated worker."""
    ctrl = tmp_path / "ctrl"
    outdir = ctrl / CAMPAIGN
    sim_out = outdir / "work" / "sim" / "0000"
    pkg = ctrl / "pkg"
    pkg.mkdir(parents=True)
    (pkg / "workflow.osw").write_text("{}")
    bucket = tmp_path / "bucket"
    storage = DirStorage(bucket, prefix=CAMPAIGN)
    transport = ResultTransportConfig(
        mode="object_storage", backend="s3", bucket="b", prefix=CAMPAIGN
    )
    monkeypatch.setattr(remote_runner, "build_result_storage", lambda **_: storage)
    monkeypatch.setattr(transport_mod, "build_result_storage", lambda **_: storage)
    monkeypatch.setenv(TASK_PAYLOAD_SECRET_ENV, "s3cret")

    def sim_step(pkg_dir: Path, sid: str, out: Path) -> Path:
        out.mkdir(parents=True, exist_ok=True)
        (out / "out.osw").write_text('{"completed_status": "Fail"}')
        (out / "run.log").write_text("Error: measure blew up")
        (out / "eplusout.sql").write_text("huge")
        raise RuntimeError("openstudio exited 1")

    monkeypatch.setattr(remote_runner.StepFunctionRegistry, "_registry", {})
    monkeypatch.setattr(
        remote_runner,
        "_register_builtin_steps",
        lambda: remote_runner.StepFunctionRegistry.register("sim", sim_step),
    )
    monkeypatch.setattr(remote_runner.StepFunctionRegistry, "discover_plugins", lambda: 0)
    staged, _ = InputStager(storage).stage_task_paths(
        (pkg, "0000", sim_out), {}, result_hint=sim_out
    )
    payload = BaseExecutor._build_task_payload(  # noqa: SLF001
        step_name="sim", args=staged, kwargs={}, result_hint=sim_out, name="sim_0000"
    )
    for env in (build_signature_env(payload), build_transport_signature_env(transport)):
        for k, v in env.items():
            monkeypatch.setenv(k, v)
    for k, v in {
        "OSIMFLOW_TASK_PAYLOAD": payload,
        "OSIMFLOW_RESULT_TRANSPORT_MODE": "object_storage",
        "OSIMFLOW_RESULT_STORAGE_BACKEND": "s3",
        "OSIMFLOW_RESULT_STORAGE_BUCKET": "b",
        "OSIMFLOW_RESULT_STORAGE_PREFIX": CAMPAIGN,
        "OSIMFLOW_SCRATCH_DIR": str(tmp_path / "scratch"),
    }.items():
        monkeypatch.setenv(k, v)
    assert remote_runner.main() == 1
    err = json.loads(capsys.readouterr().err.strip().splitlines()[-1])
    assert err["ok"] is False
    return {
        "bucket": bucket,
        "sim_out": sim_out,
        "transport": transport,
        "storage": storage,
        "tmp": tmp_path,
    }


def test_worker_uploads_diagnostics_and_failed_marker(failed_job: dict[str, Any]) -> None:
    sim = failed_job["bucket"] / CAMPAIGN / "work" / "sim" / "0000"
    assert (sim / "out.osw").read_text() == '{"completed_status": "Fail"}'
    assert (sim / "run.log").read_text() == "Error: measure blew up"
    assert (sim / FAILED_MARKER).is_file()
    assert not (sim / COMPLETE_MARKER).exists()
    assert not (sim / "eplusout.sql").exists()


def test_batch_handle_materializes_failure_artifacts(
    failed_job: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    sim_out: Path = failed_job["sim_out"]
    handle = _AWSBatchHandle(
        "job-1",
        MagicMock(),
        {},
        result_hint=sim_out,
        transport=failed_job["transport"],
    )
    err = handle._failure_error({"status": "FAILED", "statusReason": "Essential exit 1"})  # noqa: SLF001
    assert "FAILED" in str(err)
    assert (sim_out / "out.osw").is_file()
    assert "measure blew up" in (sim_out / "run.log").read_text()


def test_materialize_failure_is_best_effort(tmp_path: Path) -> None:
    handle = _AWSBatchHandle(
        "job-2",
        MagicMock(),
        {},
        result_hint=tmp_path / "nope",
        transport=ResultTransportConfig(mode="object_storage", backend="s3", bucket="b"),
    )
    handle._failure_error({"status": "FAILED"})  # noqa: SLF001


def test_download_reports_failed_sample_without_allow_partial(
    failed_job: dict[str, Any],
) -> None:
    bucket: Path = failed_job["bucket"]
    tmp: Path = failed_job["tmp"]
    store_storage = DirStorage(bucket)
    store = S3CampaignStore(store_storage, tmp / "store-scratch")
    (bucket / CAMPAIGN / "samples.json").write_text(json.dumps({"samples": []}))
    store.write_handoff(
        new_handoff(CAMPAIGN, ["0000"], openstudio_version="3.10.0", kpis=None, job_ids={})
    )
    status = store.status(CAMPAIGN)
    assert status["state"] == "completed"
    assert status["failed"] == ["0000"]
    out = tmp / "dl"
    result = store.download(CAMPAIGN, out)
    assert result["failed"] == ["0000"]
    assert (out / "work" / "sim" / "0000" / "out.osw").is_file()
    assert (out / "work" / "sim" / "0000" / FAILED_MARKER).is_file()
    failed_csv = (out / "failed_simulations.csv").read_text()
    assert "0000" in failed_csv


def test_success_marker_wins_over_stale_failure(failed_job: dict[str, Any]) -> None:
    bucket: Path = failed_job["bucket"]
    (bucket / CAMPAIGN / "work" / "sim" / "0000" / COMPLETE_MARKER).write_text("")
    store = S3CampaignStore(DirStorage(bucket), failed_job["tmp"] / "s")
    record = new_handoff(CAMPAIGN, ["0000"], openstudio_version="", kpis=None, job_ids={})
    assert store.sample_states(record) == {"0000": "complete"}
