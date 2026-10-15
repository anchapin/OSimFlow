"""Round-trip: ``osimflow export`` -> ``osimflow import-osa`` (issue #2113)."""

import json
import zipfile
from pathlib import Path

import pytest
import yaml

from osimflow.__main__ import main
from osimflow.config import CampaignConfig
from osimflow.exporters.osa import OSAExporter

_OSA_TYPE = {"nsga2": "nsga_nrel"}

VARIABLES = {
    "variables": [
        {
            "name": "wall.r_value",
            "distribution": "uniform",
            "min": 1.0,
            "max": 5.0,
            "measure_argument": "wall.r_value",
        },
        {
            "name": "lights.lpd",
            "distribution": "uniform",
            "min": 0.5,
            "max": 2.0,
            "measure_argument": "lights.lpd",
        },
    ]
}

OSW = {
    "seed_file": "building.osm",
    "weather_file": "city.epw",
    "steps": [
        {"measure_dir_name": "wall", "arguments": {"r_value": 2.0, "label": "w"}},
        {"measure_dir_name": "lights", "arguments": {"lpd": 1.0}},
        {"measure_dir_name": "report", "name": "report_a", "arguments": {"x": 1}},
        {"measure_dir_name": "report", "name": "report_b", "arguments": {"x": 2}},
    ],
}


def _make_package(root: Path) -> Path:
    pkg = root / "pkg"
    (pkg / "seeds").mkdir(parents=True)
    (pkg / "seeds" / "building.osm").write_text("SEED\n", encoding="utf-8")
    (pkg / "weather").mkdir()
    (pkg / "weather" / "city.epw").write_text("LOCATION,x\n", encoding="utf-8")
    (pkg / "files").mkdir()
    (pkg / "files" / "extra.osm").write_text("EXTRA\n", encoding="utf-8")
    (pkg / "measures" / "wall" / "tests").mkdir(parents=True)
    (pkg / "measures" / "wall" / "measure.rb").write_text("# m\n", encoding="utf-8")
    (pkg / "measures" / "wall" / "tests" / "t.osm").write_text("T\n", encoding="utf-8")
    (pkg / "workflow.osw").write_text(json.dumps(OSW), encoding="utf-8")
    return pkg


def _variables(root: Path) -> Path:
    path = root / "variables.yml"
    path.write_text(yaml.safe_dump(VARIABLES), encoding="utf-8")
    return path


def _osa_in(outdir: Path) -> Path:
    return next(outdir.glob("*.osa"))


@pytest.mark.parametrize("algorithm", ["lhs", "sobol", "ga", "de", "nsga2"])
def test_cli_export_then_import_osa(tmp_path: Path, algorithm: str) -> None:
    pkg = _make_package(tmp_path)
    outdir = tmp_path / "out"
    rc = main(
        [
            "export",
            "--target", "pat",
            "--variables", str(_variables(tmp_path)),
            "--outdir", str(outdir),
            "--template-package", str(pkg),
            "--n_samples", "7",
            "--algorithm", algorithm,
        ]
    )  # fmt: skip
    assert rc == 0
    osa = _osa_in(outdir)
    assert not (outdir / "_osa_staging").exists()

    with zipfile.ZipFile(osa) as zf:
        names = set(zf.namelist())
        analysis = json.loads(zf.read("analysis.json"))["analysis"]
    assert {"seed.osm", "weather/city.epw", "files/extra.osm"} <= names
    assert "measures/wall/tests/t.osm" in names
    workflow = analysis["problem"]["workflow"]
    assert [s["measure_dir_name"] for s in workflow] == ["wall", "lights", "report", "report"]
    assert [s["name"] for s in workflow][2:] == ["report_a", "report_b"]
    assert analysis["problem"]["algorithm"]["type"] == _OSA_TYPE.get(algorithm, algorithm)

    yml = tmp_path / "re" / "variables.yml"
    yml.parent.mkdir()
    pkg_out = tmp_path / "re" / "pkg"
    rc = main(["import-osa", str(osa), "--output", str(yml), "--package-output", str(pkg_out)])
    assert rc == 0

    loaded = yaml.safe_load(yml.read_text(encoding="utf-8"))
    assert {v["name"] for v in loaded["variables"]} == {"wall.r_value", "lights.lpd"}
    meta = loaded.get("_metadata", {})
    assert (meta.get("algorithm") or loaded.get("algorithm")) in (algorithm, None)

    osw = json.loads((pkg_out / "workflow.osw").read_text(encoding="utf-8"))
    assert [s["measure_dir_name"] for s in osw["steps"]] == ["wall", "lights", "report", "report"]
    assert [s.get("name") for s in osw["steps"]][2:] == ["report_a", "report_b"]
    assert osw["steps"][0]["arguments"]["label"] == "w"
    assert osw["steps"][2]["arguments"]["x"] == 1
    assert osw["steps"][3]["arguments"]["x"] == 2
    assert (pkg_out / osw["seed_file"]).is_file()
    assert (pkg_out / "weather" / "city.epw").is_file()
    assert (pkg_out / "files" / "extra.osm").is_file()


def test_cli_export_falls_back_to_archived_package(tmp_path: Path) -> None:
    outdir = tmp_path / "out"
    archived = outdir / "inputs" / "template_sim_package"
    pkg = _make_package(tmp_path)
    archived.parent.mkdir(parents=True)
    pkg.rename(archived)
    rc = main(["export", "--target", "pat", "--variables", str(_variables(tmp_path)),
               "--outdir", str(outdir)])  # fmt: skip
    assert rc == 0
    with zipfile.ZipFile(_osa_in(outdir)) as zf:
        assert "workflow.osw" in zf.namelist()


def test_cli_export_without_package_writes_only_analysis(tmp_path: Path) -> None:
    outdir = tmp_path / "out"
    rc = main(["export", "--target", "pat", "--variables", str(_variables(tmp_path)),
               "--outdir", str(outdir)])  # fmt: skip
    assert rc == 0
    assert (outdir / "analysis.json").is_file()
    assert not list(outdir.glob("*.osa"))


def test_cli_export_missing_package_errors(tmp_path: Path) -> None:
    rc = main(["export", "--target", "pat", "--variables", str(_variables(tmp_path)),
               "--outdir", str(tmp_path / "o"), "--template-package", str(tmp_path / "nope")])  # fmt: skip
    assert rc == 1


def test_ga_exported_as_ga_not_lhs(tmp_path: Path) -> None:
    cfg = CampaignConfig(
        input_variables=_variables(tmp_path),
        template_sim_package=_make_package(tmp_path),
        n_samples=3,
        outdir=tmp_path / "o",
        algorithm="ga",
        openstudio_version="3.10.0",
    )
    osa = OSAExporter().pack_osa(cfg, tmp_path / "pack")
    with zipfile.ZipFile(osa) as zf:
        assert json.loads(zf.read("analysis.json"))["analysis"]["algorithm"]["type"] == "ga"
    assert not (tmp_path / "pack" / "_osa_staging").exists()
