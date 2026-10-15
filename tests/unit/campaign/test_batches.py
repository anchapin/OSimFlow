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
    assert batch_campaign_name(1, "base line") == "base-line"
    assert batch_campaign_name(1, "base line", "Batch{id}_{name}") == "Batch1_base-line"


def test_campaign_name_invalid_template() -> None:
    with pytest.raises(BatchManifestError):
        batch_campaign_name(1, "a", "{bogus}")
    with pytest.raises(BatchManifestError):
        batch_campaign_name(1, "a", "{id!x}")


@pytest.mark.parametrize("template", ["{name[0]}", "{name!r}", "{id:>20}", "{}", "{x}"])
def test_campaign_name_rejects_exotic_template_fields(template: str) -> None:
    with pytest.raises(BatchManifestError):
        batch_campaign_name(1, "abc", template)


@pytest.mark.parametrize(
    "text",
    [
        "batches: [{name: A}, {name: a}]",
        "batches: [{name: null}]",
        "batches: [{name: x, campaign: null}]",
        "common: {name_template: null}\nbatches: [{name: x}]",
    ],
)
def test_invalid_names_and_case_collisions(tmp_path: Path, text: str) -> None:
    with pytest.raises(BatchManifestError):
        load_manifest(_write(tmp_path, text))


def test_manifest_naming_variants(tmp_path: Path) -> None:
    text = (
        "common: {name_template: 'Batch{id}_{name}', executor: local}\n"
        "batches:\n"
        "  - {id: 7, name: a}\n"
        "  - {name: b}\n"
        "  - {name: c, campaign: custom-c}\n"
    )
    common, entries = load_manifest(_write(tmp_path, text))
    assert [e["campaign"] for e in entries] == ["Batch7_a", "Batch2_b", "custom-c"]
    argv = build_run_argv(common, entries[2], tmp_path / "o")
    assert "--name_template" not in argv and "--campaign" not in argv
    assert "--executor" in argv


def test_manifest_default_name_is_neutral(tmp_path: Path) -> None:
    _, entries = load_manifest(_write(tmp_path, "batches: [{name: only}]"))
    assert entries[0]["campaign"] == "only"


def test_build_argv(tmp_path: Path) -> None:
    common, entries = load_manifest(_write(tmp_path))
    argv = build_run_argv(common, entries[0], tmp_path / "o")
    assert argv[:5] == [sys.executable, "-m", "osimflow", "run", "--executor"]
    assert "--detach-s3" in argv and "--skip-preflight" not in argv
    assert argv[argv.index("--kpis") : argv.index("--kpis") + 3] == ["--kpis", "eui", "cost"]
    assert argv[-2:] == ["--outdir", str(tmp_path / "o")]


@pytest.mark.parametrize(
    "text",
    [
        "[]",
        "batches: []",
        "batches: [{id: 1}]",
        "batches: [{name: a}, {id: 2, name: a}]",
        "batches: [{id: 1, name: a}, {id: 1, name: a}]",
    ],
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
    assert summary[0]["campaign"] == "base-line"


def test_continue_on_error(tmp_path: Path) -> None:
    rcs = iter([1, 0])
    with patch.object(batches.subprocess, "run", lambda *a, **k: _Proc(next(rcs))):
        res = run_batches(_write(tmp_path), tmp_path / "root", continue_on_error=True)
    assert [r.status for r in res] == ["failed", "success"]


def test_dry_run_does_not_execute(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with patch.object(batches.subprocess, "run", side_effect=AssertionError):
        res = run_batches(_write(tmp_path), tmp_path / "root", dry_run=True)
    assert all(r.status == "dry-run" for r in res)
    assert "second" in capsys.readouterr().out


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


def test_append_and_boolean_optional_flags(tmp_path: Path) -> None:
    argv = build_run_argv(
        {"uq-failure-threshold": [0.1, 0.2], "nomad-remote-results-only": False},
        {"id": 1, "name": "a"},
        tmp_path,
    )
    assert argv.count("--uq-failure-threshold") == 2
    assert "--no-nomad-remote-results-only" in argv


def test_unknown_flag_rejected_before_any_run(tmp_path: Path) -> None:
    m = _write(tmp_path, "batches: [{id: 1, name: a, bogus-flag: 3}]")
    with patch.object(batches.subprocess, "run", side_effect=AssertionError):
        with pytest.raises(BatchManifestError):
            run_batches(m, tmp_path / "r")


def test_driver_holds_lock_for_whole_run(tmp_path: Path) -> None:
    seen: list[bool] = []

    def fake_run(argv: list[str], check: bool = False) -> _Proc:
        try:
            with submit_lock(tmp_path / "root", timeout_s=0.1, poll_s=0.02):
                seen.append(False)
        except SubmitLockTimeout:
            seen.append(True)
        return _Proc(0)

    with patch.object(batches.subprocess, "run", fake_run):
        run_batches(_write(tmp_path), tmp_path / "root")
    assert seen == [True, True]
