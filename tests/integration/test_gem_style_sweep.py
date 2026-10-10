"""Variable-aware sweep over a gem-style OSW package, no model.osm (issue #1869)."""

import json
from pathlib import Path

import pytest

from osimflow import Campaign
from osimflow.config import load_config
from osimflow.executors import LocalExecutor


def test_sweep_records_per_sample_values_in_staged_osw(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OSIMFLOW_STUB_SIM", "1")
    pkg = tmp_path / "pkg"
    (pkg / "measures" / "RubyMeasure").mkdir(parents=True)
    (pkg / "files").mkdir()
    (pkg / "files" / "a.epw.txt").write_text("x")
    (pkg / "workflow.osw").write_text(
        json.dumps(
            {
                "weather_file": "",
                "measure_paths": ["measures"],
                "steps": [
                    {
                        "measure_dir_name": "RubyMeasure",
                        "arguments": {"window_type": "single", "r_value": 1.0},
                    }
                ],
            }
        )
    )
    variables = tmp_path / "variables.yml"
    variables.write_text(
        "algorithm: lhs\n"
        "variables:\n"
        "  - name: r_value\n    distribution: uniform\n    min: 1.0\n    max: 5.0\n"
        "    measure_argument: RubyMeasure.r_value\n"
        "  - name: window_type\n    distribution: discrete\n    values: [single, double]\n"
        "    measure_argument: RubyMeasure.window_type\n"
    )
    cfg = load_config(
        {
            "template_sim_package": str(pkg),
            "input_variables": str(variables),
            "n_samples": 4,
            "outdir": str(tmp_path / "out"),
            "openstudio_version": "3.11.0",
            "executor": "local",
        }
    )
    Campaign(cfg=cfg, executor=LocalExecutor(max_workers=1)).run()

    r_values = set()
    for i in range(1, 5):
        osw = json.loads((cfg.work_dir / "apply" / f"{i:04d}" / "workflow.osw").read_text())
        args = osw["steps"][0]["arguments"]
        assert args["window_type"] in ("single", "double")
        r_values.add(args["r_value"])
    assert len(r_values) == 4
