"""Quality-ratchet contract (issue #1892, AGENTS.md §6).

The ratchet rules (BLE001/S110 swallowed exceptions, T20 print, PGH003
blanket ``type: ignore``) are enforced on ``osimflow/`` and baselined per
file in ``pyproject.toml [tool.ruff.lint.per-file-ignores]``.  The baseline
may only SHRINK, so this suite proves that (a) a freshly introduced
violation is rejected, (b) the repo itself is clean under the config and
(c) no baseline entry is stale (names a rule its file no longer violates).
"""

import json
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
RATCHET_RULES = ("BLE001", "S110", "T201", "PGH003")
# Baseline selectors that expand to the concrete ratchet rule codes.
SELECTOR_EXPANSION = {"BLE": {"BLE001"}, "T20": {"T201", "T203"}, "S110": {"S110"}}


def _ruff() -> list[str]:
    exe = shutil.which("ruff", path=str(Path(sys.executable).parent)) or shutil.which("ruff")
    if exe is None:
        pytest.skip("ruff not installed")
    return [exe]


def _run(args: list[str], cwd: Path = REPO_ROOT) -> subprocess.CompletedProcess[str]:
    return subprocess.run([*_ruff(), *args], cwd=cwd, capture_output=True, text=True, check=False)


@pytest.mark.parametrize(
    ("rule", "source"),
    [
        ("BLE001", "def f():\n    try:\n        pass\n    except Exception:\n        return 1\n"),
        ("S110", "def f():\n    try:\n        int('x')\n    except Exception:\n        pass\n"),
        ("T201", "def f():\n    print('hi')\n"),
        ("PGH003", "x: int = 'a'  # type: ignore\n"),
    ],
)
def test_new_violation_fails_ratchet(tmp_path: Path, rule: str, source: str) -> None:
    mod = tmp_path / "newmod.py"
    mod.write_text(source)
    proc = _run(["check", "--isolated", "--select", rule, str(mod)], cwd=tmp_path)
    assert proc.returncode != 0, proc.stdout
    assert rule in proc.stdout


def test_repo_is_clean_under_ratchet_config() -> None:
    proc = _run(["check", "osimflow"])
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_baseline_has_no_stale_entries() -> None:
    cfg = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    ignores: dict[str, list[str]] = cfg["tool"]["ruff"]["lint"]["per-file-ignores"]

    proc = _run(
        [
            "check",
            "osimflow",
            "--isolated",
            "--select",
            ",".join(RATCHET_RULES),
            "--output-format",
            "json",
        ]
    )
    violations: dict[str, set[str]] = {}
    for item in json.loads(proc.stdout or "[]"):
        rel = Path(item["filename"]).resolve().relative_to(REPO_ROOT).as_posix()
        violations.setdefault(rel, set()).add(item["code"])

    stale: list[str] = []
    for pattern, rules in ignores.items():
        if not pattern.startswith("osimflow/") or "*" in pattern:
            continue
        for sel in rules:
            codes = SELECTOR_EXPANSION.get(sel)
            if codes is None:
                continue
            if not codes & violations.get(pattern, set()):
                stale.append(f"{pattern}: {sel}")
    assert not stale, f"stale ratchet baseline entries (remove them): {stale}"
