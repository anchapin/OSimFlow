"""Tests for tools/doctor.py (issue #1895)."""

import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
_spec = importlib.util.spec_from_file_location("doctor", REPO_ROOT / "tools" / "doctor.py")
assert _spec and _spec.loader
doctor = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(doctor)


def test_missing_venv_points_to_make_install(tmp_path: Path) -> None:
    ok, warn, step = doctor.run_checks(tmp_path / ".venv")
    assert ok == []
    assert "virtualenv missing" in warn[0]
    assert step == "make install"


def test_existing_venv_reports_ok(tmp_path: Path) -> None:
    py = tmp_path / ".venv" / "bin" / "python"
    py.parent.mkdir(parents=True)
    py.touch()
    ok, _warn, step = doctor.run_checks(tmp_path / ".venv")
    assert any("virtualenv" in line for line in ok)
    assert step
