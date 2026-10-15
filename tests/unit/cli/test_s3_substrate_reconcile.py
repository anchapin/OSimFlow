"""CLI-layer AWS Batch reconciliation for ``status|download --from-s3`` (issue #2119)."""

import argparse
import json
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from osimflow.cli import s3 as cli_s3
from osimflow.executors import AWSBatchExecutor


class _Store:
    def __init__(self, executor: str, jobs: dict[str, list[str]]) -> None:
        self._executor = executor
        self._jobs = jobs
        self.status_kwargs: dict[str, Any] = {}

    def read_handoff(self, cid: str) -> Any:
        return argparse.Namespace(executor=self._executor)

    def pending_job_ids(self, record: Any) -> dict[str, list[str]]:
        return self._jobs

    def status(self, cid: str, **kw: Any) -> dict[str, Any]:
        self.status_kwargs = kw
        return {"campaign_id": cid}


def _executor_with_client(client: Any) -> AWSBatchExecutor:
    ex = AWSBatchExecutor(allow_long_lived_credentials=True)
    ex._client = client  # noqa: SLF001
    return ex


def test_non_batch_handoff_skips_substrate(monkeypatch: pytest.MonkeyPatch) -> None:
    ctor = mock.MagicMock()
    monkeypatch.setattr(cli_s3, "AWSBatchExecutor", ctor)
    args = argparse.Namespace()
    assert cli_s3._substrate_failures(args, _Store("local", {"j": ["0"]}), "c") == {}  # noqa: SLF001
    ctor.assert_not_called()


def test_no_pending_jobs_skips_executor(monkeypatch: pytest.MonkeyPatch) -> None:
    ctor = mock.MagicMock()
    monkeypatch.setattr(cli_s3, "AWSBatchExecutor", ctor)
    assert cli_s3._substrate_failures(argparse.Namespace(), _Store("aws_batch", {}), "c") == {}  # noqa: SLF001
    ctor.assert_not_called()


def test_failures_routed_through_executor(monkeypatch: pytest.MonkeyPatch) -> None:
    client = mock.MagicMock()
    client.describe_jobs.return_value = {
        "jobs": [
            {
                "jobId": "job-oom",
                "status": "FAILED",
                "container": {"exitCode": 137, "reason": "OutOfMemoryError"},
            },
            {"jobId": "job-run", "status": "RUNNING"},
        ]
    }
    monkeypatch.setattr(cli_s3, "AWSBatchExecutor", lambda **_kw: _executor_with_client(client))
    store = _Store("aws_batch", {"job-oom": ["0001", "0003"], "job-run": ["0002"]})
    got = cli_s3._substrate_failures(argparse.Namespace(), store, "c")  # noqa: SLF001
    assert set(got) == {"0001", "0003"}
    assert got["0001"] == "AWS Batch job job-oom FAILED (exit_code=137): OutOfMemoryError"


def test_executor_construction_failure_degrades(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(**_kw: Any) -> Any:
        raise RuntimeError("no boto3")

    monkeypatch.setattr(cli_s3, "AWSBatchExecutor", _boom)
    store = _Store("aws_batch", {"j": ["0"]})
    assert cli_s3._substrate_failures(argparse.Namespace(), store, "c") == {}  # noqa: SLF001


def test_describe_failure_degrades(monkeypatch: pytest.MonkeyPatch) -> None:
    client = mock.MagicMock()
    client.describe_jobs.side_effect = RuntimeError("throttled")
    monkeypatch.setattr(cli_s3, "AWSBatchExecutor", lambda **_kw: _executor_with_client(client))
    store = _Store("aws_batch", {"j": ["0"]})
    assert cli_s3._substrate_failures(argparse.Namespace(), store, "c") == {}  # noqa: SLF001


def test_cmd_s3_status_passes_overlay(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    store = _Store("aws_batch", {})
    monkeypatch.setattr(cli_s3, "_s3_store", lambda args, scratch: store)
    monkeypatch.setattr(cli_s3, "_substrate_failures", lambda a, s, c: {"0001": "boom"})
    args = argparse.Namespace(outdir=tmp_path / "camp1")
    assert cli_s3._cmd_s3(args, "status") == 0  # noqa: SLF001
    assert store.status_kwargs == {"substrate_failures": {"0001": "boom"}}
    assert json.loads(capsys.readouterr().out)["campaign_id"] == "camp1"


def test_describe_job_failures_chunks_and_formats() -> None:
    ids = [f"j{i}" for i in range(205)]
    sizes: list[int] = []

    def _describe(*, jobs: list[str]) -> dict[str, Any]:
        sizes.append(len(jobs))
        return {
            "jobs": [
                {
                    "jobId": j,
                    "status": "FAILED" if j == "j204" else "SUCCEEDED",
                    "statusReason": "Task failed to start",
                    "attempts": [{"container": {"reason": "CannotPullContainerError"}}],
                }
                for j in jobs
            ]
        }

    client = mock.MagicMock()
    client.describe_jobs.side_effect = _describe
    out = _executor_with_client(client).describe_job_failures(ids)
    assert sizes == [100, 100, 5]
    assert out == {
        "j204": "AWS Batch job j204 FAILED: CannotPullContainerError; Task failed to start"
    }
