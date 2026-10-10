"""Tests for the sequential multi-batch driver (issue #1874)."""

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from osimflow import batches
from osimflow.batches import (
    BatchManifestError,
    SubmitLockTimeout,
    batch_campaign_name,
    build_run_argv,
    load_manifest,
    run_batches,
    submit_lock,
)

MANIFEST = """
common:
  executor: aws_batch
  detach-s3: true
  skip-preflight: false
batches:
  - {id: 1, name: base line, n_samples: 4, kpis: [eui, cost]}
  - {id: 2, name: second, n_samples: 8}
"""


def _write(tmp_path: Path, text: str = MANIFEST) -> Path:
    p = tmp_path / "m.yml"
    p.write_text(text)
    return p


def test_campaign_name_is_stable_and_safe() -> None:
    assert batch_campaign_name(1, "base line") == "Batch1_base-line"


def test_build_argv(tmp_path: Path) -> None:
    common, entries = load_manifest(_write(tmp_path))
    argv = build_run_argv(common, entries[0], tmp_path / "o")
    assert argv[:5] == [sys.executable, "-m", "osimflow", "run", "--executor"]
    assert "--detach-s3" in argv and "--skip-preflight" not in argv
    assert argv[argv.index("--kpis") : argv.index("--kpis") + 3] == ["--kpis", "eui", "cost"]
    assert argv[-2:] == ["--outdir", str(tmp_path / "o")]


@pytest.mark.parametrize(
    "text",
    ["[]", "batches: []", "batches: [{id: 1}]", "batches: [{id: 1, name: a}, {id: 1, name: a}]"],
)
def test_invalid_manifest(tmp_path: Path, text: str) -> None:
    with pytest.raises(BatchManifestError):
        load_manifest(_write(tmp_path, text))


class _Proc:
    def __init__(self, rc: int) -> None:
        self.returncode = rc


def test_sequential_order_and_skip_after_failure(tmp_path: Path) -> None:
    calls: list[str] = []

    def fake_run(argv: list[str], check: bool = False) -> _Proc:
        calls.append(argv[argv.index("--outdir") + 1])
        return _Proc(1)

    with patch.object(batches.subprocess, "run", fake_run):
        res = run_batches(_write(tmp_path), tmp_path / "root")
    assert [r.status for r in res] == ["failed", "skipped"]
    assert len(calls) == 1
    summary = json.loads((tmp_path / "root" / "batches_summary.json").read_text())
    assert summary[0]["campaign"] == "Batch1_base-line"


def test_continue_on_error(tmp_path: Path) -> None:
    rcs = iter([1, 0])
    with patch.object(batches.subprocess, "run", lambda *a, **k: _Proc(next(rcs))):
        res = run_batches(_write(tmp_path), tmp_path / "root", continue_on_error=True)
    assert [r.status for r in res] == ["failed", "success"]


def test_dry_run_does_not_execute(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with patch.object(batches.subprocess, "run", side_effect=AssertionError):
        res = run_batches(_write(tmp_path), tmp_path / "root", dry_run=True)
    assert all(r.status == "dry-run" for r in res)
    assert "Batch2_second" in capsys.readouterr().out


def test_submit_lock_excludes_second_holder(tmp_path: Path) -> None:
    with submit_lock(tmp_path):
        with pytest.raises(SubmitLockTimeout):
            with submit_lock(tmp_path, timeout_s=0.2, poll_s=0.05):
                pass
    with submit_lock(tmp_path, timeout_s=0.2):
        pass


def test_cli_run_batches_dry_run(tmp_path: Path) -> None:
    from osimflow.__main__ import main  # noqa: PLC0415

    argv = [
        "run-batches",
        "--manifest",
        str(_write(tmp_path)),
        "--root",
        str(tmp_path / "r"),
        "--dry-run",
    ]
    assert main(argv) == 0
