"""workflow.osw ``file_paths`` / ``weather_file`` resolution (issue #2094)."""

import json
from pathlib import Path

import pytest

from osimflow.campaign import Campaign
from osimflow.campaign.epw import CampaignEpwResolver
from osimflow.config import CampaignConfig
from osimflow.weather import find_weather_file

EPW = "LOCATION,X,CA,USA,1,33.9,-118.4,-8.0,21.0\nDATA PERIODS,1,1,Data,Sunday, 1/ 1,12/31,8760,\n"


def _pkg(tmp_path: Path, osw: dict) -> Path:
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "workflow.osw").write_text(json.dumps(osw))
    return pkg


def _resolver(pkg: Path, tmp_path: Path) -> CampaignEpwResolver:
    vars_yml = tmp_path / "v.yml"
    vars_yml.write_text("variables: []\n")
    cfg = CampaignConfig(
        input_variables=vars_yml,
        template_sim_package=pkg,
        n_samples=1,
        outdir=tmp_path / "out",
        openstudio_version="3.11.0",
    )
    return CampaignEpwResolver(cfg)


@pytest.mark.parametrize("subdir", ["files", "custom_files"])
def test_find_weather_file_in_files_and_file_paths(tmp_path: Path, subdir: str) -> None:
    pkg = _pkg(tmp_path, {"file_paths": ["custom_files"]})
    (pkg / subdir).mkdir()
    (pkg / subdir / "a.epw").write_text(EPW)
    assert find_weather_file(pkg, "a.epw") == pkg / subdir / "a.epw"
    assert find_weather_file(pkg, "missing.epw") is None


def test_preflight_accepts_osw_weather_file_via_file_paths(tmp_path: Path) -> None:
    pkg = _pkg(tmp_path, {"file_paths": ["wx"], "weather_file": "a.epw"})
    (pkg / "wx").mkdir()
    (pkg / "wx" / "a.epw").write_text(EPW)
    _resolver(pkg, tmp_path).preflight_validate_epw_files([])


def test_preflight_rejects_missing_osw_weather_file(tmp_path: Path) -> None:
    pkg = _pkg(tmp_path, {"weather_file": "nope.epw"})
    with pytest.raises(FileNotFoundError, match="nope.epw"):
        _resolver(pkg, tmp_path).preflight_validate_epw_files([])


def test_preflight_mapping_resolved_from_files_dir(tmp_path: Path) -> None:
    pkg = _pkg(tmp_path, {})
    (pkg / "files").mkdir()
    (pkg / "files" / "a.epw").write_text(EPW)
    defs = [{"name": "cz", "target": "epw_file", "mapping": {"x": "a.epw"}}]
    _resolver(pkg, tmp_path).preflight_validate_epw_files(defs)


def test_stage_external_epw_accepts_file_paths_relative(tmp_path: Path) -> None:
    pkg = _pkg(tmp_path, {"file_paths": ["wx"]})
    (pkg / "wx").mkdir()
    (pkg / "wx" / "a.epw").write_text(EPW)
    out = tmp_path / "sample"
    out.mkdir()
    params = {"__epw_file__": "a.epw"}
    assert Campaign._stage_external_epw(params, pkg, out) == params
    with pytest.raises(FileNotFoundError):
        Campaign._stage_external_epw({"__epw_file__": "zzz.epw"}, pkg, out)


def test_preflight_skips_osw_placeholder_when_mapping_replaces_it(tmp_path: Path) -> None:
    pkg = _pkg(tmp_path, {"weather_file": "REPLACED.epw"})
    (pkg / "files").mkdir()
    (pkg / "files" / "a.epw").write_text(EPW)
    defs = [{"name": "cz", "target": "epw_file", "mapping": {"x": "a.epw"}}]
    _resolver(pkg, tmp_path).preflight_validate_epw_files(defs)


def test_preflight_skips_osw_placeholder_when_all_samples_override(tmp_path: Path) -> None:
    pkg = _pkg(tmp_path, {"weather_file": "REPLACED.epw"})
    r = _resolver(pkg, tmp_path)
    r.preflight_validate_epw_files([], osw_weather_replaced=True)
    with pytest.raises(FileNotFoundError):
        r.preflight_validate_epw_files([], osw_weather_replaced=False)


def test_preflight_rejects_malformed_osw_weather_file(tmp_path: Path) -> None:
    from osimflow.weather import EPWValidationError

    pkg = _pkg(tmp_path, {"file_paths": ["wx"], "weather_file": "bad.epw"})
    (pkg / "wx").mkdir()
    (pkg / "wx" / "bad.epw").write_text("not an epw\n")
    with pytest.raises(EPWValidationError):
        _resolver(pkg, tmp_path).preflight_validate_epw_files([])


def test_file_paths_escaping_package_ignored(tmp_path: Path) -> None:
    (tmp_path / "shared").mkdir()
    (tmp_path / "shared" / "a.epw").write_text(EPW)
    pkg = _pkg(tmp_path, {"file_paths": ["../shared", str(tmp_path / "shared")]})
    assert find_weather_file(pkg, "a.epw") is None
