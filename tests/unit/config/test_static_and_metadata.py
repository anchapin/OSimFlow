"""``distribution: static`` support and ``_metadata`` defaults (issue #2095)."""

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml

from osimflow.algorithms import AlgorithmRegistry
from osimflow.campaign import Campaign
from osimflow.config import load_config
from osimflow.executors import LocalExecutor
from osimflow.importers.osa import osa_to_variables_yml, parse_analysis_json
from osimflow.validation import ValidationError, validate_variables_yml

EXAMPLE = Path(__file__).resolve().parents[3] / "example_package"

VARS: dict[str, Any] = {
    "variables": [
        {"name": "x", "distribution": "uniform", "min": 0.0, "max": 1.0},
        {"name": "k", "distribution": "static", "value": 7},
        {"name": "label", "distribution": "static", "value": "office"},
    ]
}


def _write(tmp_path: Path, data: dict[str, Any]) -> Path:
    p = tmp_path / "variables.yml"
    p.write_text(yaml.safe_dump(data))
    return p


class TestValidation:
    def test_static_accepted(self, tmp_path: Path) -> None:
        assert len(validate_variables_yml(_write(tmp_path, VARS))) == 3

    @pytest.mark.parametrize(
        "entry",
        [
            {"name": "k", "distribution": "static"},
            {"name": "k", "distribution": "static", "value": None},
        ],
    )
    def test_static_requires_value(self, tmp_path: Path, entry: dict[str, Any]) -> None:
        with pytest.raises(ValidationError):
            validate_variables_yml(_write(tmp_path, {"variables": [entry]}))


@pytest.mark.parametrize("algo", ["lhs", "sobol", "morris", "fast99", "halton", "random", "uq"])
def test_samplers_emit_static_constants(tmp_path: Path, algo: str) -> None:
    if algo in ("sobol", "morris", "fast99"):
        pytest.importorskip("SALib")
    alg = AlgorithmRegistry.get(algo)
    path = alg.generate_samples(VARS, 70, 1, tmp_path / algo)
    samples = json.loads(path.read_text())["samples"]
    assert samples
    for s in samples:
        assert s["values"]["k"] == 7
        assert s["values"]["label"] == "office"
        assert 0.0 <= float(s["values"]["x"]) <= 1.0


def test_sampler_only_static(tmp_path: Path) -> None:
    only = {"variables": [{"name": "k", "distribution": "static", "value": 3}]}
    pytest.importorskip("SALib")
    for algo in ("lhs", "sobol"):
        path = AlgorithmRegistry.get(algo).generate_samples(only, 3, 1, tmp_path / algo)
        samples = json.loads(path.read_text())["samples"]
        assert [s["values"] for s in samples] == [{"k": 3}] * 3


def test_generate_lhs_script_static() -> None:
    from osimflow._work_scripts.generate_lhs import _apply_distribution

    assert _apply_distribution(0.3, "static", {"value": 5}) == 5
    with pytest.raises(ValueError, match="static"):
        _apply_distribution(0.3, "static", {})


def _template(tmp_path: Path) -> Path:
    pkg = tmp_path / "pkg"
    shutil.copytree(EXAMPLE, pkg)
    return pkg


class TestMetadataDefaults:
    def _args(self, tmp_path: Path, yml: Path, **extra: Any) -> dict[str, object]:
        return {
            "input_variables": str(yml),
            "template_sim_package": str(_template(tmp_path)),
            "outdir": str(tmp_path / "out"),
            "openstudio_version": "3.10.0",
            **extra,
        }

    def _yml(self, tmp_path: Path, meta: dict[str, Any]) -> Path:
        return _write(tmp_path, {"_metadata": meta, **VARS})

    def test_fallbacks_applied(self, tmp_path: Path) -> None:
        yml = self._yml(
            tmp_path,
            {"n_samples": 6, "algorithm": "sobol", "outputs": [{"name": "eui"}, "peak"]},
        )
        cfg = load_config(self._args(tmp_path, yml))
        assert (cfg.n_samples, cfg.algorithm, cfg.kpis) == (6, "sobol", ["eui", "peak"])

    def test_cli_overrides_metadata(self, tmp_path: Path) -> None:
        yml = self._yml(tmp_path, {"n_samples": 6, "algorithm": "sobol", "outputs": ["eui"]})
        cfg = load_config(self._args(tmp_path, yml, n_samples=2, algorithm="lhs", kpis=["a"]))
        assert (cfg.n_samples, cfg.algorithm, cfg.kpis) == (2, "lhs", ["a"])

    def test_missing_n_samples_everywhere(self, tmp_path: Path) -> None:
        with pytest.raises(ValidationError, match="n_samples"):
            load_config(self._args(tmp_path, self._yml(tmp_path, {})))

    def test_metadata_seed_used(self) -> None:
        from osimflow._work_scripts.generate_lhs import _resolve_seed

        assert _resolve_seed({"_metadata": {"seed": 11}}) == 11
        assert _resolve_seed({"seed": 3, "_metadata": {"seed": 11}}) == 3


def test_import_osa_to_campaign_end_to_end(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OSIMFLOW_STUB_SIM", "1")
    analysis = {
        "analysis": {
            "display_name": "static+uncertain",
            "problem": {
                "algorithm": {"type": "lhs", "number_of_samples": 3, "seed": 5},
                "variables": [
                    {
                        "name": "heating_setpoint",
                        "variable_type": "variable",
                        "distribution": {"type": "uniform", "minimum": 18.0, "maximum": 22.0},
                        "measure": {
                            "display_name": "SetThermostatSchedule",
                            "argument": "heating_setpoint",
                        },
                    },
                    {
                        "name": "cooling_setpoint",
                        "variable_type": "variable",
                        "default_value": 26.0,
                        "measure": {
                            "display_name": "SetThermostatSchedule",
                            "argument": "cooling_setpoint",
                        },
                    },
                ],
            },
        }
    }
    src = tmp_path / "analysis.json"
    src.write_text(json.dumps(analysis))
    yml = tmp_path / "variables.yml"
    osa_to_variables_yml(parse_analysis_json(src), yml)
    data = yaml.safe_load(yml.read_text())
    assert any(v["distribution"] == "static" for v in data["variables"])

    cfg = load_config(
        {
            "input_variables": str(yml),
            "template_sim_package": str(_template(tmp_path)),
            "outdir": str(tmp_path / "out"),
            "openstudio_version": "3.10.0",
        }
    )
    assert cfg.n_samples == 3
    Campaign(cfg=cfg, executor=LocalExecutor(max_workers=1)).run()
    samples = json.loads(next((tmp_path / "out").rglob("samples.json")).read_text())["samples"]
    assert len(samples) == 3
    assert all(s["values"]["cooling_setpoint"] == 26.0 for s in samples)
