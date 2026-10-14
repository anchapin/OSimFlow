"""Real (non-mocked) coverage for the non-POSIX kill fallback (issues #2010, #2021).

``_signal_process_tree`` falls back to ``Popen.terminate()`` / ``kill()`` on
the direct child only when process groups are unavailable (Windows). Grand-
children are NOT killed on that path. These tests use real child processes;
the ``windows-subprocess`` CI workflow installs ``osimflow`` and runs them on
``windows-latest``.
"""

import subprocess
import sys
from pathlib import Path

import pytest

import osimflow
import osimflow._subprocess_utils as su

_SLEEPER = [sys.executable, "-c", "import time; time.sleep(60)"]


def test_import_osimflow() -> None:
    """``import osimflow`` succeeds cleanly on Windows (issues #2015, #2021)."""
    assert hasattr(osimflow, "Campaign")
    assert hasattr(su, "run_subprocess")


@pytest.fixture
def force_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(su, "_POSIX", False)


@pytest.mark.parametrize("kill", [False, True])
def test_fallback_signals_real_direct_child(force_fallback: None, kill: bool) -> None:
    proc = subprocess.Popen(_SLEEPER, text=True)  # noqa: S603
    try:
        su._signal_process_tree(proc, kill=kill)
        assert proc.wait(timeout=15) != 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_run_subprocess_timeout_kills_direct_child(force_fallback: None, tmp_path: Path) -> None:
    with pytest.raises(subprocess.TimeoutExpired):
        su.run_subprocess(
            _SLEEPER,
            stdout_path=tmp_path / "out.log",
            stderr_path=tmp_path / "err.log",
            timeout=1,
        )
    assert not su._ACTIVE_SUBPROCESSES


@pytest.mark.skipif(sys.platform != "win32", reason="native Windows path")
def test_native_windows_uses_fallback(tmp_path: Path) -> None:
    assert su._POSIX is False
    with pytest.raises(subprocess.TimeoutExpired):
        su.run_subprocess(
            _SLEEPER,
            stdout_path=tmp_path / "out.log",
            stderr_path=tmp_path / "err.log",
            timeout=1,
        )
