from __future__ import annotations

import io
import json
import logging
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from osimflow import remote_runner
from osimflow.storage import ResultStorage
from osimflow.task_payload_hmac import sign_task_payload


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


def test_upload_artifacts_retries_transient_storage_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transient 5xx during the runner-side upload is retried (#1398)."""
    outdir = tmp_path / "run-1"
    file_path = outdir / "aggregated_results.csv"
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text("sample_id\n0001\n", encoding="utf-8")

    calls: list[int] = []

    class _FlakyS3Client:
        def upload_file(self, local: str, bucket: str, remote: str) -> None:
            calls.append(1)
            if len(calls) < 3:
                raise ConnectionResetError("503 Service Unavailable")

    from osimflow.storage import S3Storage

    store = S3Storage(bucket="bucket")
    monkeypatch.setattr(type(store), "client", property(lambda self: _FlakyS3Client()))
    monkeypatch.setattr(remote_runner, "build_result_storage", lambda **_: store)
    monkeypatch.setattr("osimflow.storage.time.sleep", lambda _s: None)
    monkeypatch.setenv("OSIMFLOW_RESULT_TRANSPORT_MODE", "object_storage")
    monkeypatch.setenv("OSIMFLOW_RESULT_STORAGE_BACKEND", "s3")
    monkeypatch.setenv("OSIMFLOW_RESULT_STORAGE_BUCKET", "bucket")
    monkeypatch.setenv("OSIMFLOW_RESULT_STORAGE_PREFIX", "run-1")

    # Must not raise: the transient 503 is retried inside S3Storage.upload_file.
    remote_runner._upload_artifacts_for_object_storage([file_path])  # noqa: SLF001
    assert len(calls) == 3


# ---------------------------------------------------------------------------
# Issue #1482: main() must surface upload failures as a non-zero exit code
# and must never emit a success transport record when the upload raises.
# ---------------------------------------------------------------------------


_SECRET = "test-secret-for-issue-1482"


def _sign_and_set_payload(
    monkeypatch: pytest.MonkeyPatch,
    *,
    step: str,
    args: list[object] | None = None,
    kwargs: dict[str, object] | None = None,
) -> str:
    """Serialize and sign a task payload, then set the env vars main() expects."""
    payload_dict = {
        "step": step,
        "args": args if args is not None else [],
        "kwargs": kwargs if kwargs is not None else {},
    }
    payload_str = json.dumps(payload_dict)
    signature = sign_task_payload(payload_str, _SECRET)
    monkeypatch.setenv("OSIMFLOW_TASK_PAYLOAD_SECRET", _SECRET)
    monkeypatch.setenv("OSIMFLOW_TASK_PAYLOAD", payload_str)
    monkeypatch.setenv("OSIMFLOW_TASK_PAYLOAD_SIG", signature)
    return payload_str


def _set_object_storage_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Configure env vars so _upload_artifacts_for_object_storage is engaged."""
    monkeypatch.setenv("OSIMFLOW_RESULT_TRANSPORT_MODE", "object_storage")
    monkeypatch.setenv("OSIMFLOW_RESULT_STORAGE_BACKEND", "s3")
    monkeypatch.setenv("OSIMFLOW_RESULT_STORAGE_BUCKET", "bucket")
    monkeypatch.setenv("OSIMFLOW_RESULT_STORAGE_PREFIX", "run-1")


def _register_paths_step(monkeypatch: pytest.MonkeyPatch, *, paths: list[Path]) -> str:
    """Register a stub step that returns the supplied paths and return its name."""
    step_name = "_test_step_issue_1482_paths"

    def _stub(*_a: object, **_kw: object) -> list[Path]:
        return list(paths)

    remote_runner.StepFunctionRegistry.register(step_name, _stub)
    monkeypatch.setattr(remote_runner, "_register_builtin_steps", lambda: None)  # do not overwrite
    return step_name


def _run_main_capture(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> tuple[int, str, str]:
    """Invoke remote_runner.main() and return (returncode, stdout, stderr)."""
    # argparse sees pytest's sys.argv by default; the runner only has one
    # known flag (--negotiate-version), so unknown args are ignored. Reset
    # to a clean argv to make the test independent of how pytest was invoked.
    monkeypatch.setattr(sys, "argv", ["osimflow.remote_runner"])
    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", stdout_buf)
    monkeypatch.setattr(sys, "stderr", stderr_buf)
    rc = remote_runner.main()
    return rc, stdout_buf.getvalue(), stderr_buf.getvalue()


def test_upload_raises_first_call_exits_nonzero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Issue #1482: an upload exception on the very first call must propagate.

    The runner must exit non-zero, log the failure with exc_info, and must
    NOT emit a success transport record on stdout.
    """
    file_a = tmp_path / "a.csv"
    file_a.write_text("sample_id\n0001\n", encoding="utf-8")
    file_b = tmp_path / "b.csv"
    file_b.write_text("sample_id\n0002\n", encoding="utf-8")

    step = _register_paths_step(monkeypatch, paths=[file_a, file_b])
    _sign_and_set_payload(monkeypatch, step=step)
    _set_object_storage_env(monkeypatch)

    failing_storage = MagicMock(spec=ResultStorage)
    failing_storage.upload_file.side_effect = RuntimeError("simulated S3 outage")
    monkeypatch.setattr(remote_runner, "build_result_storage", lambda **_: failing_storage)

    with caplog.at_level(logging.ERROR, logger="osimflow.remote_runner"):
        rc, stdout, stderr = _run_main_capture(monkeypatch, capsys)

    assert rc == 1, f"main() must exit non-zero when upload raises (got {rc}); stderr={stderr!r}"
    assert "remote runner failed" in caplog.text
    # log.exception() always carries exc_info; the underlying RuntimeError
    # must be present in the captured record.
    assert (
        any(
            rec.exc_info is not None
            and "simulated S3 outage" in rec.getMessage() + str(rec.exc_info)
            for rec in caplog.records
        )
        or "simulated S3 outage" in caplog.text
    )
    # Success transport record must NOT be emitted on stdout.
    assert '"ok": true' not in stdout.lower().replace(" ", ""), (
        f"success transport record must not be emitted; stdout={stdout!r}"
    )
    # Error envelope must be emitted on stderr.
    parsed = json.loads(stderr.strip())
    assert parsed["ok"] is False
    assert "RuntimeError" in parsed["error"]
    # Only the first file should have been attempted (b fails fast).
    assert failing_storage.upload_file.call_count == 1


def test_upload_raises_mid_batch_exits_nonzero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Issue #1482: an upload exception mid-batch must propagate.

    Two files are uploaded; the second call raises. The runner must exit
    non-zero, log with exc_info, and must NOT emit a success transport
    record even though one file uploaded successfully.
    """
    file_a = tmp_path / "a.csv"
    file_a.write_text("sample_id\n0001\n", encoding="utf-8")
    file_b = tmp_path / "b.csv"
    file_b.write_text("sample_id\n0002\n", encoding="utf-8")
    file_c = tmp_path / "c.csv"
    file_c.write_text("sample_id\n0003\n", encoding="utf-8")

    step = _register_paths_step(monkeypatch, paths=[file_a, file_b, file_c])
    _sign_and_set_payload(monkeypatch, step=step)
    _set_object_storage_env(monkeypatch)

    failing_storage = MagicMock(spec=ResultStorage)
    failing_storage.upload_file.side_effect = [
        None,  # first call succeeds
        RuntimeError("simulated S3 mid-batch outage"),  # second call raises
    ]
    monkeypatch.setattr(remote_runner, "build_result_storage", lambda **_: failing_storage)

    with caplog.at_level(logging.ERROR, logger="osimflow.remote_runner"):
        rc, stdout, stderr = _run_main_capture(monkeypatch, capsys)

    assert rc == 1, (
        f"main() must exit non-zero when upload raises mid-batch (got {rc}); stderr={stderr!r}"
    )
    assert "remote runner failed" in caplog.text
    assert "simulated S3 mid-batch outage" in caplog.text
    # Success transport record must NOT be emitted: a partial upload is not
    # a complete result, so the substrate must see the job as FAILED.
    assert '"ok": true' not in stdout.lower().replace(" ", ""), (
        f"partial success must not be reported; stdout={stdout!r}"
    )
    parsed = json.loads(stderr.strip())
    assert parsed["ok"] is False
    assert "RuntimeError" in parsed["error"]
    # Exactly two calls were attempted before the raise aborted the batch.
    assert failing_storage.upload_file.call_count == 2


def test_partial_upload_does_not_emit_success_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Issue #1482: a partial upload must never be reported as success.

    The transport record is the only correctness signal the substrate sees;
    if it says ok=true when half the artifacts failed to upload, the
    campaign silently loses data. This test asserts that contract.
    """
    file_a = tmp_path / "a.csv"
    file_a.write_text("sample_id\n0001\n", encoding="utf-8")
    file_b = tmp_path / "b.csv"
    file_b.write_text("sample_id\n0002\n", encoding="utf-8")

    step = _register_paths_step(monkeypatch, paths=[file_a, file_b])
    _sign_and_set_payload(monkeypatch, step=step)
    _set_object_storage_env(monkeypatch)

    failing_storage = MagicMock(spec=ResultStorage)
    failing_storage.upload_file.side_effect = [
        None,  # one artifact uploads
        OSError("network reset"),
    ]
    monkeypatch.setattr(remote_runner, "build_result_storage", lambda **_: failing_storage)

    with caplog.at_level(logging.ERROR, logger="osimflow.remote_runner"):
        rc, stdout, stderr = _run_main_capture(monkeypatch, capsys)

    # Non-zero exit is the substrate-visible signal.
    assert rc == 1
    # No success envelope on stdout, even though one file uploaded.
    assert stdout.strip() == "", f"stdout must be empty on upload failure; got {stdout!r}"
    assert '"ok": true' not in stdout.lower().replace(" ", "")
    # The error envelope on stderr must explicitly say ok=False so the
    # substrate's parser cannot mistake the failure for success.
    parsed = json.loads(stderr.strip())
    assert parsed["ok"] is False
    # Verify the recorded call sequence: the first upload actually ran, but
    # the failure on the second one aborts the batch.
    assert failing_storage.upload_file.call_count == 2
    uploaded_paths = [call.args[0] for call in failing_storage.upload_file.call_args_list]
    assert uploaded_paths[0] == file_a
    assert uploaded_paths[1] == file_b


def test_missing_result_path_logs_warning_but_does_not_mask_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Issue #1482: a missing path logs the skipped-path warning.

    The existing "object-storage upload skipped missing path" warning
    is informational — it must not flip the exit code by itself. This
    test covers the missing-only case (no upload attempted, exit 0).
    The companion failure case is exercised by
    ``test_upload_raises_first_call_exits_nonzero`` /
    ``test_upload_raises_mid_batch_exits_nonzero``.
    """
    missing_path = tmp_path / "does_not_exist.csv"

    step = _register_paths_step(monkeypatch, paths=[missing_path])
    _sign_and_set_payload(monkeypatch, step=step)
    _set_object_storage_env(monkeypatch)

    succeeding_storage = MagicMock(spec=ResultStorage)
    monkeypatch.setattr(remote_runner, "build_result_storage", lambda **_: succeeding_storage)

    with caplog.at_level(logging.WARNING, logger="osimflow.remote_runner"):
        rc, stdout, stderr = _run_main_capture(monkeypatch, capsys)

    assert rc == 0, f"missing-only path must not flip exit code; got {rc}; stderr={stderr!r}"
    assert "skipped missing path" in caplog.text
    assert str(missing_path) in caplog.text
    # No uploads were attempted; the success record is emitted.
    assert succeeding_storage.upload_file.call_count == 0
    assert succeeding_storage.upload_dir.call_count == 0
    parsed = json.loads(stdout.strip())
    assert parsed["ok"] is True
