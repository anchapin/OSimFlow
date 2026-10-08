"""Isolated-filesystem tests for S3 input staging (issue #1809)."""

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from osimflow import remote_runner
from osimflow.executors import transport as transport_mod
from osimflow.executors.base import BaseExecutor
from osimflow.executors.transport import (
    ResultTransportConfig,
    decode_transport_value,
    materialize_object_storage_result,
)
from osimflow.input_staging import (
    INPUT_KEY_PREFIX,
    PAYLOAD_REF_KEY,
    InputStager,
    InputStagingError,
    WorkerPathRemapper,
    fetch_spilled_payload,
    spill_task_payload,
)
from osimflow.storage import ResultStorage
from osimflow.task_payload_hmac import (
    TASK_PAYLOAD_SECRET_ENV,
    build_signature_env,
    build_transport_signature_env,
)


class DirStorage(ResultStorage):
    """Bucket emulated by a directory; shares nothing else with its users."""

    name = "dir"

    def __init__(self, root: Path, prefix: str = "") -> None:
        self.root = root
        self.prefix = prefix
        self.fail_download: set[str] = set()
        root.mkdir(parents=True, exist_ok=True)

    def _p(self, key: str) -> Path:
        return self.root / self.prefix / key

    def upload_file(self, local_path: Path, remote_path: str) -> None:
        dest = self._p(remote_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(local_path, dest)

    def download_file(self, remote_path: str, local_path: Path) -> None:
        if remote_path in self.fail_download:
            raise PermissionError("AccessDenied")
        src = self._p(remote_path)
        if not src.is_file():
            raise FileNotFoundError(f"NoSuchKey {remote_path}")
        local_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, local_path)

    async def upload_file_async(self, local_path: Path, remote_path: str) -> None:
        self.upload_file(local_path, remote_path)

    async def download_file_async(self, remote_path: str, local_path: Path) -> None:
        self.download_file(remote_path, local_path)

    async def list_results_async(self, prefix: str = "") -> list[str]:
        return self.list_results(prefix)

    def list_results(self, prefix: str = "") -> list[str]:
        base = self.root / self.prefix
        return sorted(
            p.relative_to(base).as_posix()
            for p in base.rglob("*")
            if p.is_file() and p.relative_to(base).as_posix().startswith(prefix)
        )


def _make_package(root: Path) -> Path:
    pkg = root / "pkg"
    (pkg / "measures" / "m1" / "resources").mkdir(parents=True)
    (pkg / "workflow.osw").write_text('{"seed_file": "model.osm"}')
    (pkg / "model.osm").write_text("OS:Version,\n")
    (pkg / "weather.epw").write_text("LOCATION,x\n")
    (pkg / "measures" / "m1" / "measure.rb").write_text("# measure\n")
    (pkg / "measures" / "m1" / "resources" / "util.rb").write_text("# nested\n")
    (pkg / "empty_dir").mkdir()
    script = pkg / "run.sh"
    script.write_text("#!/bin/sh\n")
    script.chmod(0o755)
    return pkg


@pytest.fixture
def remapper_factory(tmp_path: Path) -> Any:
    def make(storage: ResultStorage) -> WorkerPathRemapper:
        return WorkerPathRemapper(storage, tmp_path / "scratch")

    return make


def test_package_roundtrip_with_nested_files_and_modes(
    tmp_path: Path, remapper_factory: Any
) -> None:
    pkg = _make_package(tmp_path / "ctrl")
    storage = DirStorage(tmp_path / "bucket")
    args, _ = InputStager(storage).stage_task_paths((pkg,), {})
    ref = args[0]
    assert ref["kind"] == "dir"

    shutil.rmtree(tmp_path / "ctrl")  # controller paths are gone for the worker
    out = decode_transport_value(remapper_factory(storage).resolve(ref))
    assert not str(out).startswith(str(tmp_path / "ctrl"))
    assert (out / "measures/m1/resources/util.rb").read_text() == "# nested\n"
    assert (out / "weather.epw").is_file()
    assert (out / "empty_dir").is_dir()
    assert (out / "run.sh").stat().st_mode & 0o111


def test_staged_keys_are_content_addressed_and_deduplicated(tmp_path: Path) -> None:
    pkg = _make_package(tmp_path / "ctrl")
    storage = DirStorage(tmp_path / "bucket")
    stager = InputStager(storage)
    a = stager.stage_task_paths((pkg,), {})[0][0]
    before = set(storage.list_results(INPUT_KEY_PREFIX))
    b = stager.stage_task_paths((pkg,), {})[0][0]
    assert a["manifest_sha256"] == b["manifest_sha256"]
    assert set(storage.list_results(INPUT_KEY_PREFIX)) == before
    (pkg / "model.osm").write_text("changed")
    c = stager.stage_task_paths((pkg,), {})[0][0]
    assert c["manifest_sha256"] != a["manifest_sha256"]


def test_missing_object_is_explicit_failure(tmp_path: Path, remapper_factory: Any) -> None:
    pkg = _make_package(tmp_path / "ctrl")
    storage = DirStorage(tmp_path / "bucket")
    ref = InputStager(storage).stage_task_paths((pkg,), {})[0][0]
    victim = next(p for p in (tmp_path / "bucket" / "_inputs" / "blobs").rglob("*") if p.is_file())
    victim.unlink()
    with pytest.raises(InputStagingError, match="failed to fetch"):
        remapper_factory(storage).resolve(ref)


def test_truncated_and_corrupt_blobs_rejected(tmp_path: Path, remapper_factory: Any) -> None:
    pkg = _make_package(tmp_path / "ctrl")
    storage = DirStorage(tmp_path / "bucket")
    ref = InputStager(storage).stage_task_paths((pkg,), {})[0][0]
    blob = tmp_path / "bucket" / "_inputs" / "blobs"
    victim = next(p for p in sorted(blob.rglob("*")) if p.is_file() and p.stat().st_size > 3)
    victim.write_bytes(victim.read_bytes()[:3])
    with pytest.raises(InputStagingError, match="incomplete download"):
        remapper_factory(storage).resolve(ref)
    victim.write_bytes(b"x" * 20)
    with pytest.raises(InputStagingError, match="incomplete download|checksum"):
        remapper_factory(storage).resolve(ref)


def test_invalid_and_tampered_manifest_rejected(tmp_path: Path, remapper_factory: Any) -> None:
    pkg = _make_package(tmp_path / "ctrl")
    storage = DirStorage(tmp_path / "bucket")
    ref = InputStager(storage).stage_task_paths((pkg,), {})[0][0]
    mpath = tmp_path / "bucket" / "_inputs" / "manifests" / f"{ref['manifest_sha256']}.json"
    mpath.write_text('{"schema_version": 1}')
    with pytest.raises(InputStagingError, match="integrity"):
        remapper_factory(storage).resolve(ref)

    bad = (
        b'{"schema_version":1,"kind":"dir","name":"p","dirs":[],"files":[{"path":"../x","size":1,"sha256":"'
        + b"a" * 64
        + b'"}]}'
    )
    sha = hashlib.sha256(bad).hexdigest()
    (tmp_path / "bucket" / "_inputs" / "manifests" / f"{sha}.json").write_bytes(bad)
    with pytest.raises(InputStagingError, match="unsafe path"):
        remapper_factory(storage).resolve({**ref, "manifest_sha256": sha})


def test_permission_failure_is_explicit(tmp_path: Path, remapper_factory: Any) -> None:
    pkg = _make_package(tmp_path / "ctrl")
    storage = DirStorage(tmp_path / "bucket")
    ref = InputStager(storage).stage_task_paths((pkg,), {})[0][0]
    storage.fail_download.add(f"_inputs/manifests/{ref['manifest_sha256']}.json")
    with pytest.raises(InputStagingError, match="AccessDenied"):
        remapper_factory(storage).resolve(ref)


def test_stage_requires_object_storage_transport(tmp_path: Path) -> None:
    pkg = _make_package(tmp_path / "ctrl")
    with pytest.raises(InputStagingError, match="no shared filesystem"):
        BaseExecutor._stage_task_inputs(  # noqa: SLF001
            (pkg,), {}, result_hint=None, transport=ResultTransportConfig(mode="shared_fs")
        )
    # No Path args -> nothing to stage, no storage required.
    assert BaseExecutor._stage_task_inputs(  # noqa: SLF001
        ("x", 1), {}, result_hint=None, transport=None
    ) == (("x", 1), {})


def test_full_handoff_in_isolated_worker_filesystems(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """apply -> sim -> extract, each worker sees none of the controller paths."""
    ctrl = tmp_path / "ctrl"
    outdir = ctrl / "results"
    pkg = _make_package(ctrl)
    bucket = tmp_path / "bucket"
    transport = ResultTransportConfig(
        mode="object_storage", backend="s3", bucket="b", prefix=outdir.name
    )
    storage = DirStorage(bucket, prefix=outdir.name)
    monkeypatch.setattr(remote_runner, "build_result_storage", lambda **_: storage)
    monkeypatch.setattr(transport_mod, "build_result_storage", lambda **_: storage)
    monkeypatch.setenv(TASK_PAYLOAD_SECRET_ENV, "s3cret")
    seen: list[Path] = []

    def apply_step(template: Path, params: dict[str, Any], sid: str, out: Path) -> Path:
        seen.extend([template, out])
        assert not template.is_relative_to(ctrl)
        assert (template / "measures/m1/resources/util.rb").is_file()
        out.mkdir(parents=True, exist_ok=True)
        shutil.copytree(template, out, dirs_exist_ok=True)
        (out / "applied.txt").write_text(f"{sid}:{params['x']}")
        return out

    def sim_step(pkg_dir: Path, sid: str, version: str, out: Path) -> Path:
        seen.extend([pkg_dir, out])
        assert (pkg_dir / "applied.txt").read_text() == "s1:5"
        out.mkdir(parents=True, exist_ok=True)
        (out / "eplusout.sql").write_text("sql")
        return out

    def extract_step(sim_dir: Path, sid: str, out: Path) -> Path:
        assert (sim_dir / "eplusout.sql").read_text() == "sql"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("{}")
        return out

    monkeypatch.setattr(remote_runner.StepFunctionRegistry, "_registry", {})

    def register_fakes() -> None:
        for step, fn in (("apply", apply_step), ("sim", sim_step), ("extract", extract_step)):
            remote_runner.StepFunctionRegistry.register(step, fn)

    monkeypatch.setattr(remote_runner, "_register_builtin_steps", register_fakes)
    monkeypatch.setattr(remote_runner.StepFunctionRegistry, "discover_plugins", lambda: 0)

    stager = InputStager(DirStorage(bucket, prefix=outdir.name))

    def run_job(step: str, name: str, args: tuple[Any, ...], hint: Path) -> None:
        staged, _ = stager.stage_task_paths(args, {}, result_hint=hint)
        payload = BaseExecutor._build_task_payload(  # noqa: SLF001
            step_name=step, args=staged, kwargs={}, result_hint=hint, name=name
        )
        for env in (build_signature_env(payload), build_transport_signature_env(transport)):
            for k, v in env.items():
                monkeypatch.setenv(k, v)
        for k, v in {
            "OSIMFLOW_TASK_PAYLOAD": payload,
            "OSIMFLOW_RESULT_TRANSPORT_MODE": "object_storage",
            "OSIMFLOW_RESULT_STORAGE_BACKEND": "s3",
            "OSIMFLOW_RESULT_STORAGE_BUCKET": "b",
            "OSIMFLOW_RESULT_STORAGE_PREFIX": outdir.name,
            "OSIMFLOW_SCRATCH_DIR": str(tmp_path / "worker-scratch"),
        }.items():
            monkeypatch.setenv(k, v)
        hidden = ctrl.with_name("ctrl-hidden")
        ctrl.rename(hidden)  # controller filesystem is absent while the worker runs
        try:
            assert remote_runner.main() == 0
        finally:
            hidden.rename(ctrl)
        result = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert result["ok"] is True
        materialize_object_storage_result(
            decode_transport_value(result["result"]),
            transport_mode="object_storage",
            result_storage_backend="s3",
            result_storage_bucket="b",
            result_storage_prefix=outdir.name,
        )

    apply_out = outdir / "work" / "apply" / "s1"
    apply_out.mkdir(parents=True)
    run_job("apply", "apply_s1", (pkg, {"x": 5}, "s1", apply_out), apply_out)
    # run_job materialized the apply result on the controller (as the Handle does).
    assert (apply_out / "applied.txt").read_text() == "s1:5"
    assert (apply_out / "measures/m1/resources/util.rb").is_file()

    sim_out = outdir / "work" / "sim" / "s1"
    run_job("sim", "sim_s1", (apply_out, "s1", "3.11.0", sim_out), sim_out)
    shutil.rmtree(apply_out)  # nothing from the apply job's scratch is relied upon
    kpi = outdir / "work" / "kpi" / "s1.json"
    run_job("extract", "kpi_s1", (sim_out, "s1", kpi), kpi)
    assert (bucket / outdir.name / "work" / "kpi" / "s1.json").read_text() == "{}"
    assert not (tmp_path / "worker-scratch").exists() or not any(
        (tmp_path / "worker-scratch").iterdir()
    )
    assert seen


def test_unsigned_staged_payload_without_object_storage_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    pkg = _make_package(tmp_path / "ctrl")
    staged, _ = InputStager(DirStorage(tmp_path / "bucket")).stage_task_paths((pkg,), {})
    payload = BaseExecutor._build_task_payload(  # noqa: SLF001
        step_name="apply", args=staged, kwargs={}, result_hint=None, name="apply_s1"
    )
    monkeypatch.setenv(TASK_PAYLOAD_SECRET_ENV, "s3cret")
    monkeypatch.setenv("OSIMFLOW_TASK_PAYLOAD", payload)
    for k, v in build_signature_env(payload).items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("OSIMFLOW_RESULT_TRANSPORT_MODE", raising=False)
    assert remote_runner.main() == 1
    assert "refusing to fall back" in capsys.readouterr().err


def test_spilled_payload_roundtrip_and_tamper_detection(tmp_path: Path) -> None:
    storage = DirStorage(tmp_path / "bucket")
    raw = json.dumps({"step": "aggregate", "args": ["x" * 20000]})
    pointer = json.loads(spill_task_payload(storage, raw))
    sha = pointer[PAYLOAD_REF_KEY]
    assert sha == hashlib.sha256(raw.encode()).hexdigest()
    assert fetch_spilled_payload(storage, sha) == raw

    (tmp_path / "bucket" / INPUT_KEY_PREFIX / "payloads" / f"{sha}.json").write_text("{}")
    with pytest.raises(InputStagingError, match="integrity"):
        fetch_spilled_payload(storage, sha)
    with pytest.raises(InputStagingError, match="invalid"):
        fetch_spilled_payload(storage, "../etc/passwd")
