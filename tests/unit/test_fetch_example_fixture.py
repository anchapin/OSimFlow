"""Unit tests for scripts/fetch_example_fixture.py (issue #1832); network mocked."""

import importlib.util
import io
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "fetch_example_fixture.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("fetch_example_fixture", SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Resp(io.BytesIO):
    status = 200

    def __enter__(self) -> "_Resp":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


def _fake_urlopen(req: Any, timeout: float = 0) -> _Resp:
    url = req.full_url
    body = b"LOCATION,Golden\n" if url.endswith(".epw") else b"OS:Version,\n  1.14.0;\n"
    return _Resp(body)


def _snapshot(d: Path) -> dict[str, bytes]:
    return {str(p.relative_to(d)): p.read_bytes() for p in d.rglob("*") if p.is_file()}


def test_default_dest_is_separate_from_example_package() -> None:
    mod = _load()
    assert mod.DEFAULT_DEST == REPO_ROOT / "tests" / "fixtures" / "real"
    assert mod.DEFAULT_DEST != REPO_ROOT / "example_package"


def test_fetch_does_not_write_into_example_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = _load()
    monkeypatch.setattr(mod.urllib.request, "urlopen", _fake_urlopen)
    pkg = REPO_ROOT / "example_package"
    before = _snapshot(pkg)

    dest = tmp_path / "real"
    assert mod.main(["--dest", str(dest)]) == 0

    assert (dest / "model.osm").read_text().startswith("OS:Version")
    assert (dest / mod.WEATHER_FILENAME).read_text().startswith("LOCATION")
    assert _snapshot(pkg) == before


def test_gitignore_covers_real_fixture_dir() -> None:
    text = (REPO_ROOT / ".gitignore").read_text()
    assert "tests/fixtures/real/" in text
