"""Integration tests for the default per-sample sim timeout (issue #1534).

The old stock default (``--byos-timeout-s`` = 600 s) SIGKILLed every
annual EnergyPlus sample that legitimately ran longer than 10 minutes,
then — worse — classified ``subprocess.TimeoutExpired`` as transient and
re-executed the doomed sample ``max_retries`` times. These tests use a
fake ``openstudio.cli`` executable on PATH (no real OpenStudio needed)
to drive the production code path
(``run_openstudio_sim`` → ``_run_real_openstudio`` → ``run_subprocess``)
end-to-end and demonstrate:

1. a sample running longer than the *old* default wall-clock bound
   completes successfully under default (unbounded) config;
2. an explicitly configured timeout still kills a wedged run — and the
   kill fails exactly once, never re-executed (non-transient);
3. a literal >600 s run completes under default config (slow-marked so
   it only runs in the dedicated ``-m slow`` CI job).
"""

import json
import os
import stat
import subprocess
import time
from pathlib import Path

import pytest

from osimflow.work import run_openstudio_sim

# Sleep duration for the fast "longer than a small explicit bound" test.
# It must comfortably exceed the 1 s explicit timeout used in the kill
# contrast test while keeping the fast suite fast.
_FAST_SLEEP_S = 3.0


def _install_fake_openstudio_cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    sleep_s: float,
    marker: Path,
) -> None:
    """Put a fake ``openstudio.cli`` executable at the front of PATH.

    The fake records each invocation to *marker* BEFORE sleeping (so
    timeout-killed runs still count), sleeps ``sleep_s`` seconds, then
    writes placeholder EnergyPlus outputs into ``run/`` relative to the
    CWD — mirroring how the real CLI lays out artifacts in the
    modified simulation package.
    """
    bin_dir = tmp_path / "fake_bin"
    bin_dir.mkdir(exist_ok=True)
    cli = bin_dir / "openstudio.cli"
    cli.write_text(
        "#!/bin/sh\n"
        'echo "fake openstudio CLI: $*"\n'
        f"echo run >> {marker}\n"
        f"sleep {sleep_s}\n"
        "mkdir -p run\n"
        'echo "-- fake eplusout.sql --" > run/eplusout.sql\n'
        "touch run/eplusout.err\n"
        'echo "[]"\n',
        encoding="utf-8",
    )
    cli.chmod(cli.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.delenv("OSIMFLOW_STUB_SIM", raising=False)


def _make_sim_package(tmp_path: Path, name: str) -> Path:
    """Build a minimal modified simulation package with a workflow.osw."""
    pkg = tmp_path / name
    pkg.mkdir()
    (pkg / "workflow.osw").write_text(json.dumps({"steps": []}), encoding="utf-8")
    return pkg


def _run_sample(pkg: Path, tmp_path: Path, timeout_s: float | None = None) -> Path:
    """Invoke ``run_openstudio_sim`` with real-subprocess plumbing.

    Health monitoring is disabled (``health_check_interval=0``) so the
    heartbeat staleness heuristic cannot mask the timeout behaviour under
    test. ``timeout_s`` defaults to the production default (``None``).
    """
    out = tmp_path / "out"
    return run_openstudio_sim(
        modified_sim_package=pkg,
        sample_id="0001",
        openstudio_version="3.9.0",
        out=out,
        stdout_path=tmp_path / "stdout.log",
        stderr_path=tmp_path / "stderr.log",
        max_retries=3,
        health_check_interval=0.0,
        timeout_s=timeout_s,
    )


class TestDefaultSimTimeout:
    """Issue #1534 — the default sim timeout must not fail long runs."""

    def test_long_sim_completes_with_default_config(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A run exceeding a small explicit bound completes under defaults.

        The fake CLI sleeps 3 s — three times the 1 s bound used in the
        kill-contrast test — and the default config (``timeout_s=None``,
        effectively unbounded) lets it finish and produce outputs.
        """
        marker = tmp_path / "invocations.log"
        pkg = _make_sim_package(tmp_path, "pkg_default")
        _install_fake_openstudio_cli(tmp_path, monkeypatch, sleep_s=_FAST_SLEEP_S, marker=marker)

        start = time.monotonic()
        sim_out = _run_sample(pkg, tmp_path)
        elapsed = time.monotonic() - start

        assert sim_out == tmp_path / "out" / "0001"
        # The sample genuinely ran to completion — not killed, not stubbed.
        assert elapsed >= _FAST_SLEEP_S
        assert marker.read_text(encoding="utf-8").strip() == "run"
        assert (pkg / "run" / "eplusout.sql").is_file()

    def test_explicit_timeout_kills_and_fails_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An explicit ``timeout_s`` still kills — and fails exactly once.

        The fake CLI sleeps 5 s with a 1 s bound: the run must raise
        ``subprocess.TimeoutExpired`` and — per issue #1534 — the kill is
        non-transient, so with ``max_retries=3`` configured the CLI is
        still invoked exactly once instead of being re-executed 4x.
        """
        marker = tmp_path / "invocations.log"
        pkg = _make_sim_package(tmp_path, "pkg_timeout")
        _install_fake_openstudio_cli(tmp_path, monkeypatch, sleep_s=5.0, marker=marker)

        with pytest.raises(subprocess.TimeoutExpired):
            _run_sample(pkg, tmp_path, timeout_s=1.0)

        # Fail once, not max_retries+1 times (issue #1534).
        invocations = marker.read_text(encoding="utf-8").splitlines()
        assert invocations == ["run"]
        # The killed run never produced simulation outputs.
        assert not (pkg / "run" / "eplusout.sql").is_file()

    @pytest.mark.slow
    @pytest.mark.timeout(900)
    def test_sim_over_600s_completes_with_default_config(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Literal acceptance criterion: a >600 s run survives defaults.

        Under the old 600 s stock default this sample was SIGKILLed at
        600 s, retried (TimeoutExpired was misclassified as transient),
        burned ~4x600 s of compute, then failed permanently. With the
        unbounded default it runs 601 s and completes successfully.
        Slow-marked: only the dedicated ``-m slow`` CI job runs it.
        """
        marker = tmp_path / "invocations.log"
        pkg = _make_sim_package(tmp_path, "pkg_slow")
        _install_fake_openstudio_cli(tmp_path, monkeypatch, sleep_s=601.0, marker=marker)

        start = time.monotonic()
        sim_out = _run_sample(pkg, tmp_path)
        elapsed = time.monotonic() - start

        assert sim_out == tmp_path / "out" / "0001"
        assert elapsed >= 600.0
        assert marker.read_text(encoding="utf-8").strip() == "run"
        assert (pkg / "run" / "eplusout.sql").is_file()
