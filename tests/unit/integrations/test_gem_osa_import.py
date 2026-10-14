"""Unit tests for osimflow.importers.gem_osa (issue #1870)."""

import json
from pathlib import Path

import pytest
import yaml

from osimflow.__main__ import main
from osimflow.importers.gem_osa import (
    GemImportError,
    discover_batches,
    import_gem_project,
    parametric_space_to_variables,
)

SPACE = {
    "algorithm_setting": {"seed": 7, "number_of_samples": 12},
    "bar": {"floor_area": [1000, 5000], "stories": [1, 2, 3], "type": ["A", "B"]},
    "win": {"r": {"min": 1.0, "max": 2.0, "samplecount": 3}},
}


def _project(tmp_path: Path) -> Path:
    proj = tmp_path / "gem"
    proj.mkdir()
    (proj / "parametric_space_Batch1_base.json").write_text(json.dumps(SPACE))
    (proj / "parametric_space_Batch2_alt.json").write_text(json.dumps({"bar": {"x": [0, 1]}}))
    (proj / "measure_space_Batch1_base.json").write_text(
        json.dumps(
            {"measure_space": {"bar": {"flag": "true", "n": "3", "floor_area": "9"}, "zz": {}}}
        )
    )
    (proj / "osa_workflowBatch1_base.json").write_text(
        json.dumps({"analysis": {"problem": {"algorithm": {"sample_method": "all_variables"}}}})
    )
    (proj / "configs.yml").write_text(
        yaml.safe_dump({"osa_settings": {"analysis_settings": {"analysis_type": "lhs"}}})
    )
    return proj


def _package(tmp_path: Path) -> Path:
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "workflow.osw").write_text(
        json.dumps(
            {
                "steps": [
                    {
                        "measure_dir_name": "bar",
                        "arguments": {"flag": False, "n": 1, "floor_area": 5},
                    }
                ]
            }
        )
    )
    return pkg


def test_discover_orders_batches(tmp_path: Path) -> None:
    ids = [b.batch_id for b in discover_batches(_project(tmp_path))]
    assert ids == ["Batch1_base", "Batch2_alt"]


def test_variable_mapping() -> None:
    by_name = {v["name"]: v for v in parametric_space_to_variables(SPACE)}
    assert by_name["bar.floor_area"]["distribution"] == "uniform"
    assert by_name["bar.stories"]["distribution"] == "discrete"
    assert by_name["bar.type"]["values"] == ["A", "B"]
    assert by_name["win.r"]["max"] == 2.0
    assert "algorithm_setting" not in " ".join(by_name)


def test_import_writes_campaigns_and_manifest(tmp_path: Path) -> None:
    out = tmp_path / "out"
    manifest = import_gem_project(_project(tmp_path), out, template_package=_package(tmp_path))
    entry = manifest["batches"]["Batch1_base"]
    assert entry["n_samples"] == 12 and entry["seed"] == 7
    assert entry["sample_method"] == "all_variables"
    assert entry["unmatched_measures"] == ["zz"]
    assert json.loads((out / "batches.json").read_text()) == manifest
    osw = json.loads((out / "Batch1_base/template/workflow.osw").read_text())
    args = osw["steps"][0]["arguments"]
    assert args["flag"] is True and args["n"] == 3
    assert args["floor_area"] == 5  # varied argument is left to the sweep
    variables = yaml.safe_load((out / "Batch1_base/variables.yml").read_text())
    assert variables["seed"] == 7
    assert variables["variables"][0]["name"] == "bar.floor_area"
    variables_b2 = yaml.safe_load((out / "Batch2_alt/variables.yml").read_text())
    assert "seed" not in variables_b2


def test_batch_filter_and_errors(tmp_path: Path) -> None:
    proj = _project(tmp_path)
    manifest = import_gem_project(proj, tmp_path / "o", batches=["Batch2_alt"])
    assert list(manifest["batches"]) == ["Batch2_alt"]
    with pytest.raises(GemImportError, match="unknown batch"):
        import_gem_project(proj, tmp_path / "o2", batches=["nope"])
    with pytest.raises(GemImportError):
        discover_batches(tmp_path / "missing")
    with pytest.raises(GemImportError):
        parametric_space_to_variables({"m": {"a": "bad"}})


def test_cli(tmp_path: Path) -> None:
    rc = main(["import-gem-osa", str(_project(tmp_path)), "--output-dir", str(tmp_path / "c")])
    assert rc == 0
    assert (tmp_path / "c/batches.json").is_file()


def test_shared_and_reporting_measure_space(tmp_path: Path) -> None:
    proj = tmp_path / "g"
    proj.mkdir()
    (proj / "parametric_space_Batch1_a.json").write_text(json.dumps({"bar": {"x": [0, 1]}}))
    (proj / "measure_space.json").write_text(
        json.dumps(
            {"measure_space": {"bar": {"n": "4"}}, "measure_space_reporting": {"rep": {"k": "2"}}}
        )
    )
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "workflow.osw").write_text(
        json.dumps(
            {
                "steps": [
                    {"measure_dir_name": "bar", "arguments": {"n": 1}},
                    {"measure_dir_name": "rep", "arguments": {"k": 1}},
                ]
            }
        )
    )
    out = tmp_path / "o"
    import_gem_project(proj, out, template_package=pkg)
    steps = json.loads((out / "Batch1_a/template/workflow.osw").read_text())["steps"]
    assert steps[0]["arguments"]["n"] == 4 and steps[1]["arguments"]["k"] == 2


def test_bool_choices_and_bad_range() -> None:
    v = parametric_space_to_variables({"m": {"a": [True, False]}})[0]
    assert v["values"] == [True, False]
    with pytest.raises(GemImportError):
        parametric_space_to_variables({"m": {"a": {"min": True, "max": 5}}})
    with pytest.raises(GemImportError):
        parametric_space_to_variables({"m": {"a": {"min": "x", "max": 5}}})


def test_partial_reimport_preserves_manifest(tmp_path: Path) -> None:
    proj = _project(tmp_path)
    import_gem_project(proj, tmp_path / "o")
    manifest = import_gem_project(proj, tmp_path / "o", batches=["Batch2_alt"])
    assert set(manifest["batches"]) == {"Batch1_base", "Batch2_alt"}


def test_seed_precedence_and_omission(tmp_path: Path) -> None:
    """Issue #2030: seed precedence configs.yml -> osa_workflow -> parametric_space."""
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "configs.yml").write_text(
        yaml.safe_dump(
            {
                "osa_settings": {
                    "analysis_settings": {
                        "algorithm_settings": {"seed": 11},
                    }
                }
            }
        )
    )
    # Batch1: only configs.yml sets seed -> 11
    (proj / "parametric_space_Batch1_cfg.json").write_text(json.dumps({"bar": {"x": [0, 1]}}))
    # Batch2: osa_workflow overrides configs.yml -> 22
    (proj / "parametric_space_Batch2_osa.json").write_text(json.dumps({"bar": {"x": [0, 1]}}))
    (proj / "osa_workflowBatch2_osa.json").write_text(
        json.dumps({"analysis": {"problem": {"algorithm": {"seed": 22}}}})
    )
    # Batch3: parametric_space algorithm_setting overrides both -> 33
    (proj / "parametric_space_Batch3_space.json").write_text(
        json.dumps({"algorithm_setting": {"seed": 33}, "bar": {"x": [0, 1]}})
    )
    (proj / "osa_workflowBatch3_space.json").write_text(
        json.dumps({"analysis": {"problem": {"algorithm": {"seed": 22}}}})
    )

    out = tmp_path / "out"
    manifest = import_gem_project(proj, out)
    assert manifest["batches"]["Batch1_cfg"]["seed"] == 11
    assert manifest["batches"]["Batch2_osa"]["seed"] == 22
    assert manifest["batches"]["Batch3_space"]["seed"] == 33

    v1 = yaml.safe_load((out / "Batch1_cfg/variables.yml").read_text())
    v2 = yaml.safe_load((out / "Batch2_osa/variables.yml").read_text())
    v3 = yaml.safe_load((out / "Batch3_space/variables.yml").read_text())
    assert v1["seed"] == 11
    assert v2["seed"] == 22
    assert v3["seed"] == 33

    # When no seed is configured anywhere, variables.yml omits seed.
    proj_no_seed = tmp_path / "proj_no_seed"
    proj_no_seed.mkdir()
    (proj_no_seed / "parametric_space_Batch1_none.json").write_text(
        json.dumps({"bar": {"x": [0, 1]}})
    )
    out_none = tmp_path / "out_none"
    manifest_none = import_gem_project(proj_no_seed, out_none)
    assert manifest_none["batches"]["Batch1_none"]["seed"] is None
    v_none = yaml.safe_load((out_none / "Batch1_none/variables.yml").read_text())
    assert "seed" not in v_none


@pytest.mark.parametrize(
    "bad_seed",
    [True, False, 1.5, 3.0, "42", "abc", "", [], {}],
)
def test_invalid_seed_rejected(tmp_path: Path, bad_seed: object) -> None:
    """Issue #2030: booleans, floats, and invalid strings raise ValueError."""
    proj = tmp_path / "proj_bad"
    proj.mkdir()
    (proj / "parametric_space_Batch1_bad.json").write_text(
        json.dumps({"algorithm_setting": {"seed": bad_seed}, "bar": {"x": [0, 1]}})
    )
    with pytest.raises(ValueError, match="seed must be an integer"):
        import_gem_project(proj, tmp_path / "out_bad")


def test_normalize_osw_paths_rewrites_out_of_tree_and_works_in_isolated_dir(
    tmp_path: Path,
) -> None:
    """Issue #2043: parent-relative paths in workflow.osw are normalized inside template/ and resolve when isolated."""
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "parametric_space.json").write_text(json.dumps({"bar": {"floor_area": [100, 200]}}))

    pkg = tmp_path / "staged_pkg"
    (pkg / "measures" / "bar").mkdir(parents=True)
    (pkg / "weather").mkdir(parents=True)
    (pkg / "weather" / "Chicago.epw").write_text("LOCATION,Chicago\n")
    (pkg / "seed_models").mkdir(parents=True)
    (pkg / "seed_models" / "model.osm").write_text("OS:Version,{1};\n")
    (pkg / "workflow.osw").write_text(
        json.dumps(
            {
                "seed_file": "../seed_models/model.osm",
                "weather_file": "../../weather/Chicago.epw",
                "measure_paths": ["../../measures"],
                "file_paths": ["../../weather", "../../seed_models"],
                "steps": [
                    {"measure_dir_name": "bar", "arguments": {"floor_area": 100}},
                ],
            }
        )
    )

    out = tmp_path / "out"
    manifest = import_gem_project(proj, out, template_package=pkg)
    assert list(manifest["batches"]) == ["default"]

    # Copy template/ to a completely separate isolated directory to simulate remote worker execution
    isolated = tmp_path / "isolated_worker" / "0000"
    isolated.parent.mkdir(parents=True)
    import shutil

    shutil.copytree(out / "default" / "template", isolated)
    isolated_root = isolated.resolve()
    osw = json.loads((isolated / "workflow.osw").read_text())

    assert osw["measure_paths"] == ["measures"]
    assert "seed_models" in osw["file_paths"]
    assert "weather" in osw["file_paths"]
    assert osw["seed_file"] == "seed_models/model.osm"
    assert osw["weather_file"] == "weather/Chicago.epw"

    for mp in osw["measure_paths"]:
        p = (isolated / mp).resolve()
        p.relative_to(isolated_root)
        assert p.is_dir()
    for fp in osw["file_paths"]:
        p = (isolated / fp).resolve()
        p.relative_to(isolated_root)
        assert p.is_dir()
    for field in ("seed_file", "weather_file"):
        p = (isolated / osw[field]).resolve()
        p.relative_to(isolated_root)
        assert p.is_file()


def test_normalize_osw_paths_preserves_valid_in_package_paths(tmp_path: Path) -> None:
    """Issue #2043: valid in-package relative paths and file_paths-resolved basenames are preserved."""
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "parametric_space_Batch1.json").write_text(json.dumps({"bar": {"x": [0, 1]}}))

    pkg = tmp_path / "pkg"
    (pkg / "custom_measures" / "bar").mkdir(parents=True)
    (pkg / "files" / "seeds").mkdir(parents=True)
    (pkg / "files" / "seeds" / "seed.osm").write_text("OS:Version,{1};\n")
    (pkg / "files" / "epw").mkdir(parents=True)
    (pkg / "files" / "epw" / "site.epw").write_text("LOCATION,Site\n")
    (pkg / "workflow.osw").write_text(
        json.dumps(
            {
                "seed_file": "seed.osm",
                "weather_file": "files/epw/site.epw",
                "measure_paths": ["custom_measures"],
                "file_paths": ["files/seeds"],
                "steps": [{"measure_dir_name": "bar", "arguments": {"x": 0}}],
            }
        )
    )

    out = tmp_path / "out"
    import_gem_project(proj, out, template_package=pkg)
    osw = json.loads((out / "Batch1" / "template" / "workflow.osw").read_text())
    assert osw["seed_file"] == "seed.osm"
    assert osw["weather_file"] == "files/epw/site.epw"
    assert osw["measure_paths"] == ["custom_measures"]
    assert osw["file_paths"] == ["files/seeds", "files/epw"]


def test_normalize_osw_paths_missing_or_ambiguous_raises_gem_import_error(
    tmp_path: Path,
) -> None:
    """Issue #2043: zero or multiple in-package matches for seed_file / weather_file raise GemImportError."""
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "parametric_space_Batch1.json").write_text(json.dumps({"bar": {"x": [0, 1]}}))

    # Missing seed_file
    pkg_missing = tmp_path / "pkg_missing"
    pkg_missing.mkdir()
    (pkg_missing / "workflow.osw").write_text(
        json.dumps(
            {
                "seed_file": "../outside/missing.osm",
                "steps": [{"measure_dir_name": "bar", "arguments": {"x": 0}}],
            }
        )
    )
    with pytest.raises(GemImportError, match="no file named 'missing.osm'"):
        import_gem_project(proj, tmp_path / "out_missing", template_package=pkg_missing)

    # Ambiguous weather_file
    pkg_ambig = tmp_path / "pkg_ambig"
    (pkg_ambig / "weather_a").mkdir(parents=True)
    (pkg_ambig / "weather_b").mkdir(parents=True)
    (pkg_ambig / "weather_a" / "dup.epw").write_text("A\n")
    (pkg_ambig / "weather_b" / "dup.epw").write_text("B\n")
    (pkg_ambig / "workflow.osw").write_text(
        json.dumps(
            {
                "weather_file": "../outside/dup.epw",
                "steps": [{"measure_dir_name": "bar", "arguments": {"x": 0}}],
            }
        )
    )
    with pytest.raises(GemImportError, match="ambiguous inside template package"):
        import_gem_project(proj, tmp_path / "out_ambig", template_package=pkg_ambig)

    # Ambiguous measure_dir_name when measure_paths escaped package
    pkg_ambig_m = tmp_path / "pkg_ambig_m"
    (pkg_ambig_m / "custom_a" / "bar").mkdir(parents=True)
    (pkg_ambig_m / "custom_b" / "bar").mkdir(parents=True)
    (pkg_ambig_m / "workflow.osw").write_text(
        json.dumps(
            {
                "measure_paths": ["../../measures"],
                "steps": [{"measure_dir_name": "bar", "arguments": {"x": 0}}],
            }
        )
    )
    with pytest.raises(GemImportError, match="measure 'bar' is ambiguous"):
        import_gem_project(proj, tmp_path / "out_ambig_m", template_package=pkg_ambig_m)

    # Missing measure_dir_name when measure_paths escaped package
    pkg_missing_m = tmp_path / "pkg_missing_m"
    pkg_missing_m.mkdir()
    (pkg_missing_m / "workflow.osw").write_text(
        json.dumps(
            {
                "measure_paths": ["../../measures"],
                "steps": [{"measure_dir_name": "bar", "arguments": {"x": 0}}],
            }
        )
    )
    with pytest.raises(GemImportError, match="could not resolve step measure 'bar'"):
        import_gem_project(proj, tmp_path / "out_missing_m", template_package=pkg_missing_m)


def test_normalize_osw_paths_contained_absolute_path_with_duplicate_basename(
    tmp_path: Path,
) -> None:
    """Issue #2043: an absolute path contained inside template_package resolves directly without basename ambiguity."""
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "parametric_space_Batch1.json").write_text(json.dumps({"bar": {"x": [0, 1]}}))

    pkg = tmp_path / "pkg_abs"
    (pkg / "measures" / "bar").mkdir(parents=True)
    (pkg / "seed_a").mkdir(parents=True)
    (pkg / "seed_b").mkdir(parents=True)
    (pkg / "seed_a" / "model.osm").write_text("A\n")
    (pkg / "seed_b" / "model.osm").write_text("B\n")
    (pkg / "weather_a").mkdir(parents=True)
    (pkg / "weather_b").mkdir(parents=True)
    (pkg / "weather_a" / "site.epw").write_text("WA\n")
    (pkg / "weather_b" / "site.epw").write_text("WB\n")
    (pkg / "workflow.osw").write_text(
        json.dumps(
            {
                "seed_file": str((pkg / "seed_b" / "model.osm").resolve()),
                "weather_file": str((pkg / "weather_a" / "site.epw").resolve()),
                "measure_paths": [str((pkg / "measures").resolve())],
                "file_paths": [str((pkg / "seed_b").resolve())],
                "steps": [{"measure_dir_name": "bar", "arguments": {"x": 0}}],
            }
        )
    )

    out = tmp_path / "out_abs"
    import_gem_project(proj, out, template_package=pkg)
    osw = json.loads((out / "Batch1" / "template" / "workflow.osw").read_text())
    assert osw["seed_file"] == "seed_b/model.osm"
    assert osw["weather_file"] == "weather_a/site.epw"
    assert osw["measure_paths"] == ["measures"]
    assert "seed_b" in osw["file_paths"]
    assert "weather_a" in osw["file_paths"]


def test_discover_batches_supports_custom_suffixes_default_and_collisions(
    tmp_path: Path,
) -> None:
    """Issue #2043: Batch<N> sorted numerically, then custom suffixes lexically, then default; collisions raise GemImportError."""
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "parametric_space.json").write_text(json.dumps({"bar": {"x": [0, 1]}}))
    (proj / "parametric_space_Batch10_ten.json").write_text(json.dumps({"bar": {"x": [0, 1]}}))
    (proj / "parametric_space_Batch2_two.json").write_text(json.dumps({"bar": {"x": [0, 1]}}))
    (proj / "parametric_space_zebra.json").write_text(json.dumps({"bar": {"x": [0, 1]}}))
    (proj / "parametric_space_alpha.json").write_text(json.dumps({"bar": {"x": [0, 1]}}))
    (proj / "measure_space_alpha.json").write_text(json.dumps({"measure_space": {"bar": {"x": 0}}}))

    batches = discover_batches(proj)
    assert [b.batch_id for b in batches] == [
        "Batch2_two",
        "Batch10_ten",
        "alpha",
        "zebra",
        "default",
    ]
    alpha = next(b for b in batches if b.batch_id == "alpha")
    assert alpha.number is None and alpha.name == "alpha"
    assert alpha.measure_space == proj / "measure_space_alpha.json"

    # Collision on (kind, batch_id): parametric_space_Batch2_two.json vs parametric_spaceBatch2_two.json
    (proj / "parametric_spaceBatch2_two.json").write_text(json.dumps({"bar": {"x": [0, 1]}}))
    with pytest.raises(
        GemImportError, match="duplicate parametric_space files for batch 'Batch2_two'"
    ):
        discover_batches(proj)


def test_import_forces_server_directory_cleanup_sql_false_unless_varied(tmp_path: Path) -> None:
    """Issue #2051: ServerDirectoryCleanup.sql is forced to False/'false' unless varied."""
    # Case 1: pre-populated workflow.osw without measure_space.json (bool True -> False)
    proj1 = tmp_path / "proj1"
    proj1.mkdir()
    (proj1 / "parametric_space_Batch1.json").write_text(json.dumps({"bar": {"x": [0, 1]}}))
    pkg1 = tmp_path / "pkg1"
    pkg1.mkdir()
    (pkg1 / "workflow.osw").write_text(
        json.dumps(
            {
                "steps": [
                    {"measure_dir_name": "bar", "arguments": {"x": 0}},
                    {
                        "measure_dir_name": "ServerDirectoryCleanup",
                        "arguments": {"sql": True, "eso": True},
                    },
                ]
            }
        )
    )
    out1 = tmp_path / "out1"
    import_gem_project(proj1, out1, template_package=pkg1)
    osw1 = json.loads((out1 / "Batch1/template/workflow.osw").read_text())
    assert osw1["steps"][1]["arguments"]["sql"] is False
    assert osw1["steps"][1]["arguments"]["eso"] is True

    # Case 2: measure_space_reporting sets sql: "true" on string-typed OSW (string "true" -> "false")
    proj2 = tmp_path / "proj2"
    proj2.mkdir()
    (proj2 / "parametric_space_Batch1.json").write_text(json.dumps({"bar": {"x": [0, 1]}}))
    (proj2 / "measure_space_Batch1.json").write_text(
        json.dumps(
            {
                "measure_space": {"bar": {"x": "1"}},
                "measure_space_reporting": {
                    "ServerDirectoryCleanup": {"sql": "true", "eso": "true", "__SKIP__": "false"}
                },
            }
        )
    )
    pkg2 = tmp_path / "pkg2"
    pkg2.mkdir()
    (pkg2 / "workflow.osw").write_text(
        json.dumps(
            {
                "steps": [
                    {"measure_dir_name": "bar", "arguments": {"x": 0}},
                    {
                        "measure_dir_name": "ServerDirectoryCleanup",
                        "arguments": {"sql": "true", "eso": "false"},
                    },
                ]
            }
        )
    )
    out2 = tmp_path / "out2"
    import_gem_project(proj2, out2, template_package=pkg2)
    osw2 = json.loads((out2 / "Batch1/template/workflow.osw").read_text())
    assert osw2["steps"][1]["arguments"]["sql"] == "false"
    assert osw2["steps"][1]["arguments"]["eso"] == "true"

    # Case 3: ServerDirectoryCleanup.sql explicitly varied in parametric_space -> preserved
    proj3 = tmp_path / "proj3"
    proj3.mkdir()
    (proj3 / "parametric_space_Batch1.json").write_text(
        json.dumps({"ServerDirectoryCleanup": {"sql": [True, False]}})
    )
    out3 = tmp_path / "out3"
    import_gem_project(proj3, out3, template_package=pkg1)
    osw3 = json.loads((out3 / "Batch1/template/workflow.osw").read_text())
    assert osw3["steps"][1]["arguments"]["sql"] is True


def test_gem_import_doe_and_full_factorial_analysis_type(tmp_path: Path) -> None:
    """Issue #2057: analysis_type doe / full_factorial maps to full_factorial with levels and Cartesian product n_samples."""
    proj = tmp_path / "proj_doe"
    proj.mkdir()
    (proj / "configs.yml").write_text(
        yaml.safe_dump(
            {
                "osa_settings": {
                    "analysis_settings": {
                        "analysis_type": "doe",
                        "algorithm_settings": {"number_of_samples": 99},
                    }
                }
            }
        )
    )
    (proj / "parametric_space_Batch1_doe.json").write_text(
        json.dumps(
            {
                "SetWallR": {"r_value": [5.0, 25.0], "construction": ["steel", "wood", "mass"]},
                "SetInfiltration": {"ach": [0.1, 0.25, 0.5]},
                "SetRoofR": {"r_value": {"min": 10, "max": 40}},
            }
        )
    )
    (proj / "parametric_space_Batch2_fact.json").write_text(
        json.dumps(
            {
                "osa_settings": {"analysis_type": "full_factorial"},
                "SetWallR": {"r_value": [10.0, 20.0, 30.0]},
                "SetInfiltration": {"enabled": [True, False]},
            }
        )
    )
    out = tmp_path / "out_doe"
    manifest = import_gem_project(proj, out)

    b1 = manifest["batches"]["Batch1_doe"]
    # 2 * 3 * 3 * 2 = 36
    assert b1["n_samples"] == 36
    assert "--n_samples" in b1["command"]
    assert b1["command"][b1["command"].index("--n_samples") + 1] == "36"
    v1 = yaml.safe_load(Path(b1["variables_yml"]).read_text())
    assert v1["algorithm"] == "full_factorial"
    by_name1 = {v["name"]: v for v in v1["variables"]}
    assert by_name1["SetWallR.r_value"]["levels"] == [5.0, 25.0]
    assert by_name1["SetWallR.construction"]["levels"] == ["steel", "wood", "mass"]
    assert by_name1["SetInfiltration.ach"]["levels"] == [0.1, 0.25, 0.5]
    assert by_name1["SetRoofR.r_value"]["levels"] == [10.0, 40.0]

    b2 = manifest["batches"]["Batch2_fact"]
    # 3 * 2 = 6
    assert b2["n_samples"] == 6
    v2 = yaml.safe_load(Path(b2["variables_yml"]).read_text())
    assert v2["algorithm"] == "full_factorial"
    by_name2 = {v["name"]: v for v in v2["variables"]}
    assert by_name2["SetWallR.r_value"]["levels"] == [10.0, 20.0, 30.0]
    assert by_name2["SetInfiltration.enabled"]["levels"] == [True, False]


def test_gem_import_single_run_sobol_morris_and_non_measure_keys(tmp_path: Path) -> None:
    """Issue #2057: single_run defaults n_samples=1, sobol/morris map cleanly, and all _NON_MEASURE_KEYS are merged."""
    proj = tmp_path / "proj_types"
    proj.mkdir()
    (proj / "parametric_space_Batch1_single.json").write_text(
        json.dumps(
            {
                "analysis_settings": {"analysis_type": "single_run"},
                "SetWallR": {"r_value": [5.0, 25.0]},
            }
        )
    )
    (proj / "parametric_space_Batch2_sobol.json").write_text(
        json.dumps(
            {
                "algorithm_settings": {"analysis_type": "sobol", "number_of_samples": 16},
                "SetWallR": {"r_value": [5.0, 25.0]},
            }
        )
    )
    (proj / "parametric_space_Batch3_morris.json").write_text(
        json.dumps(
            {
                "osa_settings": {
                    "analysis_settings": {"analysis_type": "Morris", "number_of_samples": 8}
                },
                "SetWallR": {"r_value": [5.0, 25.0]},
            }
        )
    )
    out = tmp_path / "out_types"
    manifest = import_gem_project(proj, out)

    b1 = manifest["batches"]["Batch1_single"]
    assert b1["n_samples"] == 1
    assert b1["command"][-2:] == ["--n_samples", "1"]
    v1 = yaml.safe_load(Path(b1["variables_yml"]).read_text())
    assert v1["algorithm"] == "lhs"
    assert len(v1["variables"]) == 1

    b2 = manifest["batches"]["Batch2_sobol"]
    assert b2["n_samples"] == 16
    v2 = yaml.safe_load(Path(b2["variables_yml"]).read_text())
    assert v2["algorithm"] == "sobol"

    b3 = manifest["batches"]["Batch3_morris"]
    assert b3["n_samples"] == 8
    v3 = yaml.safe_load(Path(b3["variables_yml"]).read_text())
    assert v3["algorithm"] == "morris"
