"""Round-trip fidelity tests for OSAExporter (issue #2100)."""

import json
import zipfile
from pathlib import Path

import pytest
import yaml

from osimflow.config import CampaignConfig
from osimflow.exporters.osa import _OSIMFLOW_ALGO_TO_OSA, OSAExporter
from osimflow.importers.osa import _OSA_ALGORITHM_MAP, osa_to_variables_yml, parse_osa

VARIABLES = {
    "_metadata": {
        "seed": 7,
        "outputs": [
            {"name": "eui", "display_name": "EUI", "units": "kWh/m2", "objective_function": True}
        ],
    },
    "variables": [
        {
            "name": "wall.r",
            "distribution": "uniform",
            "min": 1.0,
            "max": 5.0,
            "measure_argument": "m.r",
        },
        {
            "name": "wall.kind",
            "distribution": "categorical",
            "values": ["a", "b", "c"],
            "pmf": {"a": 0.2, "b": 0.3, "c": 0.5},
            "measure_argument": "m.kind",
        },
    ],
}


def _config(tmp_path: Path, osw: dict | None = None, files: dict[str, str] | None = None):
    yml = tmp_path / "variables.yml"
    yml.write_text(yaml.safe_dump(VARIABLES), encoding="utf-8")
    tpl = tmp_path / "tpl"
    (tpl / "seeds").mkdir(parents=True)
    (tpl / "seeds" / "building.osm").write_text("OSM\n", encoding="utf-8")
    (tpl / "weather").mkdir()
    (tpl / "weather" / "city.epw").write_text("LOCATION,x\n", encoding="utf-8")
    for rel, text in (files or {}).items():
        (tpl / rel).write_text(text, encoding="utf-8")
    (tpl / "workflow.osw").write_text(
        json.dumps(osw or {"seed_file": "building.osm", "weather_file": "city.epw", "steps": []}),
        encoding="utf-8",
    )
    return CampaignConfig(
        input_variables=yml,
        template_sim_package=tpl,
        n_samples=5,
        outdir=tmp_path / "out",
        openstudio_version="3.10.0",
        algorithm="lhs",
        kpis=["eui"],
    )


def _analysis(osa: Path) -> dict:
    with zipfile.ZipFile(osa) as zf:
        return json.loads(zf.read("analysis.json"))


@pytest.mark.parametrize("ours,osa_type", sorted(_OSIMFLOW_ALGO_TO_OSA.items()))
def test_algorithm_map_is_inverse(ours: str, osa_type: str) -> None:
    assert _OSA_ALGORITHM_MAP[osa_type] == ours


def test_seed_fallback_rewrites_osw(tmp_path: Path) -> None:
    cfg = _config(tmp_path, osw={"seed_file": "missing.osm", "steps": []})
    osa = OSAExporter().pack_osa(cfg, tmp_path / "pack")
    with zipfile.ZipFile(osa) as zf:
        assert "seed.osm" in zf.namelist()
        assert json.loads(zf.read("workflow.osw"))["seed_file"] == "seed.osm"


def test_exports_weights_seed_outputs(tmp_path: Path) -> None:
    osa = OSAExporter().pack_osa(_config(tmp_path), tmp_path / "pack")
    data = _analysis(osa)["analysis"]
    variables = data["problem"]["variables"]
    cat = next(v for v in variables if v["name"] == "wall.kind")
    assert "weights" in json.dumps(cat)
    assert data["problem"]["algorithm"]["seed"] == 7
    assert data["output_variables"][0]["name"] == "eui"


def test_pack_resolves_seed_and_weather_from_osw(tmp_path: Path) -> None:
    osa = OSAExporter().pack_osa(_config(tmp_path), tmp_path / "pack")
    with zipfile.ZipFile(osa) as zf:
        names = zf.namelist()
        assert "seed.osm" in names
        assert "weather/city.epw" in names
        osw = json.loads(zf.read("workflow.osw"))
    assert osw["seed_file"] == "seed.osm"
    assert json.loads(json.dumps(_analysis(osa)))["analysis"]["weather_file"] == {
        "file_name": "weather/city.epw"
    }


def test_round_trip_preserves_variables_and_settings(tmp_path: Path) -> None:
    osa = OSAExporter().pack_osa(_config(tmp_path), tmp_path / "pack")
    out = tmp_path / "re.yml"
    osa_to_variables_yml(parse_osa(osa), out)
    loaded = yaml.safe_load(out.read_text(encoding="utf-8"))
    by_name = {v["name"]: v for v in loaded["variables"]}
    assert set(by_name) == {"wall.r", "wall.kind"}
    kind = by_name["wall.kind"]
    assert kind["values"] == ["a", "b", "c"]
    assert kind["pmf"] == {"a": 0.2, "b": 0.3, "c": 0.5}
    meta = loaded.get("_metadata", {})
    assert meta.get("n_samples") == 5 or loaded.get("n_samples") == 5
    assert meta.get("seed") == 7 or loaded.get("seed") == 7
