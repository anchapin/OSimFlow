"""End-to-end local campaign over an existing OSW package (issue #1812)."""

import shutil
from pathlib import Path

import pytest

from osimflow import Campaign
from osimflow.config import load_config
from osimflow.executors import LocalExecutor


def test_prebuilt_campaign_runs_without_variables_and_keeps_osw(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OSIMFLOW_STUB_SIM", "1")
    pkg = tmp_path / "pkg"
    shutil.copytree(Path(__file__).resolve().parents[2] / "example_package", pkg)
    cfg = load_config(
        {
            "template_sim_package": str(pkg),
            "n_samples": 2,
            "outdir": str(tmp_path / "out"),
            "openstudio_version": "3.11.0",
            "prebuilt_workflow": True,
            "executor": "local",
        }
    )
    Campaign(cfg=cfg, executor=LocalExecutor(max_workers=1)).run()

    for sid in ("0001", "0002"):
        staged = cfg.work_dir / "apply" / sid / "workflow.osw"
        assert staged.read_bytes() == (pkg / "workflow.osw").read_bytes()
    assert (cfg.outdir / "aggregated_results.csv").is_file()
