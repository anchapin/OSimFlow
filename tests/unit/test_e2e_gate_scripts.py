"""Non-cloud tests for the real-E2E workflow helpers (issue #1813)."""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / "scripts" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


gate = _load("e2e_gate")
deb = _load("resolve_openstudio_deb")

JUNIT = '<testsuites><testsuite tests="{t}" failures="{f}" errors="0" skipped="{s}"/></testsuites>'


def _report(tmp_path: Path, t: int, f: int, s: int) -> Path:
    p = tmp_path / "r.xml"
    p.write_text(JUNIT.format(t=t, f=f, s=s))
    return p


def test_missing_names_treats_blank_as_missing() -> None:
    assert gate.missing_names(["A", "B", "C"], {"A": "x", "B": "  "}) == ["B", "C"]


@pytest.mark.parametrize(
    ("t", "f", "s", "ok"),
    [(0, 0, 0, False), (3, 0, 3, False), (3, 1, 0, False), (3, 0, 1, True)],
)
def test_judge_junit(tmp_path: Path, t: int, f: int, s: int, ok: bool) -> None:
    counts = gate.parse_junit(_report(tmp_path, t, f, s))
    assert (gate.judge_junit(counts) is None) is ok


def test_preflight_strict_fails_and_optional_reports(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    out, summary = tmp_path / "out", tmp_path / "sum"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.delenv("E2E_GATE_TEST_VAR", raising=False)
    argv = ["preflight", "--label", "t", "--require", "E2E_GATE_TEST_VAR"]
    assert gate.main([*argv, "--strict"]) == 1
    assert gate.main(argv) == 0
    assert "available=false" in out.read_text()
    assert "NOT a successful run" in summary.read_text()
    monkeypatch.setenv("E2E_GATE_TEST_VAR", "v")
    assert gate.main(argv) == 0
    assert "available=true" in out.read_text()


def test_select_deb_prefers_2204() -> None:
    base = "https://x/OpenStudio-3.11.0+abc-Ubuntu-{}-x86_64.deb"
    rel = {
        "assets": [
            {"browser_download_url": base.format("20.04")},
            {"browser_download_url": "https://x/OpenStudio-Windows.exe"},
            {"browser_download_url": base.format("22.04")},
        ]
    }
    assert "22.04" in deb.select_deb_url(rel)
    with pytest.raises(LookupError):
        deb.select_deb_url({"assets": []})


def test_resolve_script_runs_as_cli() -> None:
    rel = {"assets": [{"browser_download_url": "https://x/a-Ubuntu-20.04.deb"}]}
    res = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "resolve_openstudio_deb.py")],
        input=json.dumps(rel),
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode == 0
    assert res.stdout.strip() == "https://x/a-Ubuntu-20.04.deb"
