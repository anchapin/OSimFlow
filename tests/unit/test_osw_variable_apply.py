"""OSW-level variable application without OpenStudio bindings (issue #1869)."""

import csv
import json
from pathlib import Path

import pytest

from osimflow.algorithms.custom import CustomDOEAlgorithm
from osimflow.campaign import cast_samples
from osimflow.work import default_apply_parameters


def _gem_package(root: Path) -> Path:
    pkg = root / "pkg"
    (pkg / "measures" / "RubyMeasure").mkdir(parents=True)
    (pkg / "workflow.osw").write_text(
        json.dumps(
            {
                "weather_file": "",
                "measure_paths": ["measures"],
                "steps": [
                    {
                        "measure_dir_name": "RubyMeasure",
                        "arguments": {"window_type": "single", "r_value": 1.0},
                    },
                    {"measure_dir_name": "Other", "arguments": {"r_value": 9.0}},
                ],
            }
        )
    )
    return pkg


def _steps(out: Path) -> list[dict[str, object]]:
    steps: list[dict[str, object]] = json.loads((out / "workflow.osw").read_text())["steps"]
    return steps


def test_measure_arguments_applied_without_osm_or_bindings(tmp_path: Path) -> None:
    out = _gem_package(tmp_path)
    default_apply_parameters(
        out,
        {"RubyMeasure.r_value": 3.5, "window_type": {"label": "double", "index": 1}},
        "0001",
        out,
    )
    first = _steps(out)[0]["arguments"]
    assert first == {"window_type": "double", "r_value": 3.5}  # type: ignore[comparison-overlap]
    assert _steps(out)[1]["arguments"] == {"r_value": 9.0}


def test_epw_reserved_key_sets_weather_file(tmp_path: Path) -> None:
    out = _gem_package(tmp_path)
    default_apply_parameters(out, {"__epw_file__": "files/a.epw"}, "0001", out)
    assert json.loads((out / "workflow.osw").read_text())["weather_file"] == "files/a.epw"


def test_unmapped_parameter_without_osm_still_fails(tmp_path: Path) -> None:
    out = _gem_package(tmp_path)
    with pytest.raises(FileNotFoundError):
        default_apply_parameters(out, {"not_an_argument": 1.0}, "0001", out)


def test_custom_csv_emits_per_sample_overrides(tmp_path: Path) -> None:
    csv_path = tmp_path / "s.csv"
    with csv_path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["r_value", "weather_file", "seed_model"])
        w.writerow(["1.5", "files/a.epw", "/seeds/a"])
        w.writerow(["2.5", "", ""])
    variables = {
        "algorithm": {"type": "custom", "samples_file": str(csv_path)},
        "variables": [{"name": "r_value", "distribution": "uniform", "min": 0, "max": 5}],
    }
    path = CustomDOEAlgorithm().generate_samples(variables, 2, None, tmp_path / "o")
    raw = json.loads(path.read_text())["samples"]
    assert raw[0]["weather_file"] == "files/a.epw"
    assert raw[0]["seed_model"] == "/seeds/a"
    assert "weather_file" not in raw[1]
    assert raw[0]["values"] == {"r_value": 1.5}
    kept = cast_samples(raw)
    assert kept[0]["weather_file"] == "files/a.epw"
    assert "seed_model" not in kept[1]
