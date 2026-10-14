"""apply_parameters honors ``workflow.osw`` ``seed_file`` (issue #2079)."""

import json
import shutil
from pathlib import Path

import pytest

from osimflow.apply_params import _resolve_dir_template, apply_parameters


def _pkg(tmp_path: Path, seed_rel: str | None, osm_rel: str) -> Path:
    pkg = tmp_path / "pkg"
    (pkg / osm_rel).parent.mkdir(parents=True, exist_ok=True)
    (pkg / osm_rel).write_text(json.dumps({"attributes": {"lpd": 8.0, "wwr": 0.4}}))
    osw: dict[str, object] = {"steps": []}
    if seed_rel is not None:
        osw["seed_file"] = seed_rel
    (pkg / "workflow.osw").write_text(json.dumps(osw))
    return pkg


def test_seed_file_in_subdir_is_mutated(tmp_path: Path) -> None:
    pkg = _pkg(tmp_path, "seeds/baseline.osm", "seeds/baseline.osm")
    out = tmp_path / "out"
    apply_parameters(template=pkg, parameters={"lpd": 10.0}, sample_id="1", out=out)
    data = json.loads((out / "seeds" / "baseline.osm").read_text())
    assert data["attributes"] == {"lpd": 10.0, "wwr": 0.4}
    assert not (out / "model.osm").exists()


def test_seed_file_top_level_non_default_name(tmp_path: Path) -> None:
    pkg = _pkg(tmp_path, "example_model.osm", "example_model.osm")
    _, osm = _resolve_dir_template(pkg)
    assert osm == pkg.resolve() / "example_model.osm"


def test_falls_back_to_model_osm_then_single_osm(tmp_path: Path) -> None:
    pkg = _pkg(tmp_path, None, "model.osm")
    assert _resolve_dir_template(pkg)[1] == pkg / "model.osm"
    pkg2 = tmp_path / "pkg2"
    pkg2.mkdir()
    (pkg2 / "only.osm").write_text("{}")
    assert _resolve_dir_template(pkg2)[1] == pkg2 / "only.osm"


def test_seed_file_escaping_template_is_ignored(tmp_path: Path) -> None:
    pkg = _pkg(tmp_path, "../outside.osm", "model.osm")
    (tmp_path / "outside.osm").write_text("{}")
    assert _resolve_dir_template(pkg)[1] == pkg / "model.osm"


def test_work_deferral_and_pending_apply_use_seed_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from osimflow import work
    from osimflow.campaign.helpers import _package_content_digest

    monkeypatch.delenv("OSIMFLOW_STUB_SIM", raising=False)
    pkg = _pkg(tmp_path, "seeds/baseline.osm", "seeds/baseline.osm")
    out = tmp_path / "out"
    shutil.copytree(pkg, out)

    work._apply_parameters_with_osm_deferral(pkg, {"lpd": 3.0}, "1", out)
    seed = out / "seeds" / "baseline.osm"
    assert json.loads(seed.read_text())["attributes"]["lpd"] == 3.0

    before = _package_content_digest(pkg)
    seed2 = pkg / "seeds" / "baseline.osm"
    seed2.write_text(json.dumps({"attributes": {"lpd": 9.0}}))
    assert _package_content_digest(pkg) != before

    work._apply_pending_osm_parameters(out, "1")
    assert json.loads(seed.read_text())["attributes"]["lpd"] == 3.0


def test_archive_includes_nested_seed(tmp_path: Path) -> None:
    from osimflow.campaign.artifacts import CampaignArtifactWriter

    pkg = _pkg(tmp_path, "seeds/baseline.osm", "seeds/baseline.osm")
    dst = tmp_path / "arch"
    CampaignArtifactWriter.archive_sample_artifacts(pkg, dst, ["*.osw", "*.osm"])
    assert (dst / "baseline.osm").is_file()
    assert (dst / "workflow.osw").is_file()
