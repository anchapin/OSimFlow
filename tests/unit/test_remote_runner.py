from __future__ import annotations

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
