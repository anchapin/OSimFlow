from __future__ import annotations

from pathlib import Path

from osimflow.executors.transport import materialize_object_storage_result


class _FakeStorage:
    def __init__(self, objects: dict[str, str]) -> None:
        self._objects = dict(objects)

    def download_file(self, remote_path: str, local_path: Path) -> None:
        value = self._objects.get(remote_path)
        if value is None:
            raise FileNotFoundError(remote_path)
        local_path.parent.mkdir(parents=True, exist_ok=True)
        local_path.write_text(value, encoding="utf-8")

    def list_results(self, prefix: str = "") -> list[str]:
        return sorted(key for key in self._objects if key.startswith(prefix))


def test_materialize_object_storage_downloads_file(tmp_path: Path, monkeypatch: object) -> None:
    file_hint = tmp_path / "run-a" / "aggregated_results.csv"
    storage = _FakeStorage({"aggregated_results.csv": "sample_id,eui\n0001,10.0\n"})
    monkeypatch.setattr(
        "osimflow.executors.transport.build_result_storage",
        lambda **_: storage,
    )

    result = materialize_object_storage_result(
        {"csv": file_hint},
        transport_mode="object_storage",
        result_storage_backend="s3",
        result_storage_bucket="bucket",
        result_storage_prefix="run-a",
    )

    assert result == {"csv": file_hint}
    assert file_hint.read_text(encoding="utf-8").startswith("sample_id")


def test_materialize_object_storage_downloads_directory(
    tmp_path: Path, monkeypatch: object
) -> None:
    dir_hint = tmp_path / "run-b" / "work" / "sim" / "0001"
    storage = _FakeStorage(
        {
            "work/sim/0001/eplusout.sql": "-- sql --",
            "work/sim/0001/eplusout.err": "",
        }
    )
    monkeypatch.setattr(
        "osimflow.executors.transport.build_result_storage",
        lambda **_: storage,
    )

    result = materialize_object_storage_result(
        dir_hint,
        transport_mode="object_storage",
        result_storage_backend="s3",
        result_storage_bucket="bucket",
        result_storage_prefix="run-b",
    )

    assert result == dir_hint
    assert (dir_hint / "eplusout.sql").is_file()
    assert (dir_hint / "eplusout.err").is_file()
