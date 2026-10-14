"""Campaign honors ``name`` + ``measure_argument`` with duplicate argument names (issue #2078)."""

import json
from pathlib import Path

import pytest

from osimflow import Campaign
from osimflow.config import load_config
from osimflow.executors import LocalExecutor


@pytest.mark.parametrize("ident", ["dir", "xml_name", "class_name"])
def test_alias_names_target_distinct_measures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ident: str
) -> None:
    monkeypatch.setenv("OSIMFLOW_STUB_SIM", "1")
    pkg = tmp_path / "pkg"
    ids = {
        "WallInsulation": {
            "dir": "WallInsulation",
            "xml_name": "wall_ins",
            "class_name": "WallCls",
        },
        "RoofInsulation": {
            "dir": "RoofInsulation",
            "xml_name": "roof_ins",
            "class_name": "RoofCls",
        },
    }
    for m, names in ids.items():
        (pkg / "measures" / m).mkdir(parents=True)
        (pkg / "measures" / m / "measure.xml").write_text(
            f"<measure><name>{names['xml_name']}</name><class_name>{names['class_name']}</class_name>"
            "<arguments><argument><name>r_value</name><type>Double</type>"
            "<required>true</required><default_value>1.0</default_value></argument></arguments>"
            "</measure>"
        )
    (pkg / "workflow.osw").write_text(
        json.dumps(
            {
                "measure_paths": ["measures"],
                "steps": [
                    {"measure_dir_name": "WallInsulation", "arguments": {"r_value": 1.0}},
                    {"measure_dir_name": "RoofInsulation", "arguments": {"r_value": 2.0}},
                ],
            }
        )
    )
    variables = tmp_path / "variables.yml"
    variables.write_text(
        "algorithm: lhs\n"
        "variables:\n"
        "  - name: wall_r\n    distribution: uniform\n    min: 1.0\n    max: 5.0\n"
        f"    measure_argument: {ids['WallInsulation'][ident]}.r_value\n"
        "  - name: roof_r\n    distribution: uniform\n    min: 10.0\n    max: 20.0\n"
        f"    measure_argument: {ids['RoofInsulation'][ident]}.r_value\n"
    )
    cfg = load_config(
        {
            "template_sim_package": str(pkg),
            "input_variables": str(variables),
            "n_samples": 3,
            "outdir": str(tmp_path / "out"),
            "openstudio_version": "3.11.0",
            "executor": "local",
        }
    )
    Campaign(cfg=cfg, executor=LocalExecutor(max_workers=1)).run()

    for i in range(1, 4):
        steps = json.loads((cfg.work_dir / "apply" / f"{i:04d}" / "workflow.osw").read_text())[
            "steps"
        ]
        assert 1.0 <= steps[0]["arguments"]["r_value"] <= 5.0
        assert 10.0 <= steps[1]["arguments"]["r_value"] <= 20.0
