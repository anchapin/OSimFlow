"""Unit tests for osimflow/config.py (issue #210).

Covers:
- CampaignConfig dataclass: all fields, defaults, validation
- load_config(): YAML loading, env var overrides, missing file errors
- Config merging and CLI arg → config field mapping
"""

from pathlib import Path

import pytest
import yaml

from osimflow.config import CampaignConfig, load_config
from osimflow.validation import ValidationError


@pytest.fixture
def variables_yml(tmp_path: Path) -> Path:
    p = tmp_path / "variables.yml"
    p.write_text(
        yaml.dump(
            {
                "variables": [
                    {
                        "name": "wall_r",
                        "distribution": "uniform",
                        "min": 1.0,
                        "max": 10.0,
                    }
                ]
            }
        )
    )
    return p


@pytest.fixture
def template_pkg(tmp_path: Path) -> Path:
    pkg = tmp_path / "template"
    pkg.mkdir()
    (pkg / "workflow.osw").write_text("{}")
    return pkg


@pytest.fixture
def outdir(tmp_path: Path) -> Path:
    od = tmp_path / "out"
    od.mkdir()
    return od


def _base_args(
    variables_yml: Path,
    template_pkg: Path,
    outdir: Path,
    **overrides: object,
) -> dict[str, object]:
    args: dict[str, object] = {
        "input_variables": str(variables_yml),
        "template_sim_package": str(template_pkg),
        "n_samples": "10",
        "outdir": str(outdir),
        "openstudio_version": "3.11.0",
        "archive_intermediates": False,
        "custom_apply_script": None,
        "custom_kpi_extractor": None,
        "mlflow_tracking_uri": None,
        "slurm_qos": None,
        "slurm_constraint": None,
        "slurm_gres": None,
        "weather_dir": "weather",
        "dry_run": False,
        "sample": None,
        "algorithm": "lhs",
        "init_script": None,
        "finalize_script": None,
        "skip_preflight": False,
        "max_generations": 1,
        "aws_batch_max_spot_price_usd": None,
        "aws_batch_fallback_to_on_demand": False,
        "aws_batch_max_retries": 3,
        "ecr_repository": None,
    }
    args.update(overrides)
    return args


class TestCampaignConfig:
    def test_defaults(self, variables_yml: Path, template_pkg: Path, outdir: Path) -> None:
        cfg = CampaignConfig(
            input_variables=variables_yml,
            template_sim_package=template_pkg,
            n_samples=5,
            outdir=outdir,
            openstudio_version="3.11.0",
        )
        assert cfg.archive_intermediates is False
        assert cfg.custom_apply_script is None
        assert cfg.custom_kpi_extractor is None
        assert cfg.mlflow_tracking_uri is None
        assert cfg.slurm_qos is None
        assert cfg.slurm_constraint is None
        assert cfg.slurm_gres is None
        assert cfg.baseline is None
        assert cfg.weather_dir == "weather"
        assert cfg.dry_run is False
        assert cfg.sample is None
        assert cfg.algorithm == "lhs"
        assert cfg.init_script is None
        assert cfg.finalize_script is None
        assert cfg.skip_preflight is False
        assert cfg.max_generations == 1
        assert cfg.aws_batch_max_spot_price_usd is None
        assert cfg.aws_batch_fallback_to_on_demand is False
        assert cfg.aws_batch_max_retries == 3
        assert cfg.ecr_repository is None

    def test_all_fields_set(self, variables_yml: Path, template_pkg: Path, outdir: Path) -> None:
        cfg = CampaignConfig(
            input_variables=variables_yml,
            template_sim_package=template_pkg,
            n_samples=100,
            outdir=outdir,
            openstudio_version="3.9.0",
            archive_intermediates=True,
            custom_apply_script=Path("/tmp/apply.py"),
            custom_kpi_extractor=Path("/tmp/kpi.py"),
            mlflow_tracking_uri="http://localhost:5000",
            slurm_qos="high",
            slurm_constraint="gpu",
            slurm_gres="gpu:1",
            baseline={"sample_id": "b0", "parameters": {"wall_r": 5.0}},
            weather_dir="epw_files",
            dry_run=True,
            sample=3,
            algorithm="sobol",
            init_script=Path("/tmp/init.sh"),
            finalize_script=Path("/tmp/final.sh"),
            skip_preflight=True,
            max_generations=10,
            aws_batch_max_spot_price_usd=0.05,
            aws_batch_fallback_to_on_demand=True,
            aws_batch_max_retries=5,
            ecr_repository="123456.dkr.ecr.us-east-1.amazonaws.com/os",
        )
        assert cfg.n_samples == 100
        assert cfg.openstudio_version == "3.9.0"
        assert cfg.archive_intermediates is True
        assert cfg.custom_apply_script == Path("/tmp/apply.py")
        assert cfg.custom_kpi_extractor == Path("/tmp/kpi.py")
        assert cfg.mlflow_tracking_uri == "http://localhost:5000"
        assert cfg.slurm_qos == "high"
        assert cfg.slurm_constraint == "gpu"
        assert cfg.slurm_gres == "gpu:1"
        assert cfg.baseline == {"sample_id": "b0", "parameters": {"wall_r": 5.0}}
        assert cfg.weather_dir == "epw_files"
        assert cfg.dry_run is True
        assert cfg.sample == 3
        assert cfg.algorithm == "sobol"
        assert cfg.init_script == Path("/tmp/init.sh")
        assert cfg.finalize_script == Path("/tmp/final.sh")
        assert cfg.skip_preflight is True
        assert cfg.max_generations == 10
        assert cfg.aws_batch_max_spot_price_usd == 0.05
        assert cfg.aws_batch_fallback_to_on_demand is True
        assert cfg.aws_batch_max_retries == 5
        assert cfg.ecr_repository == "123456.dkr.ecr.us-east-1.amazonaws.com/os"

    def test_work_dir_property(self, variables_yml: Path, template_pkg: Path, outdir: Path) -> None:
        cfg = CampaignConfig(
            input_variables=variables_yml,
            template_sim_package=template_pkg,
            n_samples=5,
            outdir=outdir,
            openstudio_version="3.11.0",
        )
        assert cfg.work_dir == outdir / "work"

    def test_samples_file_property(
        self, variables_yml: Path, template_pkg: Path, outdir: Path
    ) -> None:
        cfg = CampaignConfig(
            input_variables=variables_yml,
            template_sim_package=template_pkg,
            n_samples=5,
            outdir=outdir,
            openstudio_version="3.11.0",
        )
        assert cfg.samples_file == outdir / "work" / "samples.json"

    def test_cache_db_property(self, variables_yml: Path, template_pkg: Path, outdir: Path) -> None:
        cfg = CampaignConfig(
            input_variables=variables_yml,
            template_sim_package=template_pkg,
            n_samples=5,
            outdir=outdir,
            openstudio_version="3.11.0",
        )
        assert cfg.cache_db == outdir / "work" / "cache.sqlite"


class TestLoadConfig:
    def test_basic_load(self, variables_yml: Path, template_pkg: Path, outdir: Path) -> None:
        args = _base_args(variables_yml, template_pkg, outdir)
        cfg = load_config(args)
        assert cfg.input_variables == variables_yml.resolve()
        assert cfg.template_sim_package == template_pkg.resolve()
        assert cfg.n_samples == 10
        assert cfg.outdir == outdir.resolve()
        assert cfg.openstudio_version == "3.11.0"

    def test_missing_variables_yml(self, template_pkg: Path, outdir: Path) -> None:
        args = _base_args(Path("/nonexistent/variables.yml"), template_pkg, outdir)
        with pytest.raises(FileNotFoundError, match="variables_yml not found"):
            load_config(args)

    def test_missing_template_sim_package(self, variables_yml: Path, outdir: Path) -> None:
        args = _base_args(variables_yml, Path("/nonexistent/pkg"), outdir)
        with pytest.raises(FileNotFoundError, match="template_sim_package not found"):
            load_config(args)

    def test_creates_outdir(self, variables_yml: Path, template_pkg: Path, tmp_path: Path) -> None:
        new_outdir = tmp_path / "nested" / "outdir"
        assert not new_outdir.exists()
        args = _base_args(variables_yml, template_pkg, new_outdir)
        cfg = load_config(args)
        assert cfg.outdir.exists()

    def test_cli_flags_map_correctly(
        self, variables_yml: Path, template_pkg: Path, outdir: Path
    ) -> None:
        args = _base_args(
            variables_yml,
            template_pkg,
            outdir,
            archive_intermediates=True,
            dry_run=True,
            skip_preflight=True,
            algorithm="sobol",
            max_generations=5,
            sample="2",
            openstudio_version="3.9.0",
            n_samples="500",
        )
        cfg = load_config(args)
        assert cfg.archive_intermediates is True
        assert cfg.dry_run is True
        assert cfg.skip_preflight is True
        assert cfg.algorithm == "sobol"
        assert cfg.max_generations == 5
        assert cfg.sample == 2
        assert cfg.openstudio_version == "3.9.0"
        assert cfg.n_samples == 500

    def test_custom_scripts_resolved(
        self, variables_yml: Path, template_pkg: Path, outdir: Path, tmp_path: Path
    ) -> None:
        apply_script = tmp_path / "my_apply.py"
        apply_script.write_text("def apply(*a): pass")
        kpi_script = tmp_path / "my_kpi.py"
        kpi_script.write_text("def extract(*a): pass")
        args = _base_args(
            variables_yml,
            template_pkg,
            outdir,
            custom_apply_script=str(apply_script),
            custom_kpi_extractor=str(kpi_script),
        )
        cfg = load_config(args)
        assert cfg.custom_apply_script == apply_script.resolve()
        assert cfg.custom_kpi_extractor == kpi_script.resolve()

    def test_slurm_advanced_directives(
        self, variables_yml: Path, template_pkg: Path, outdir: Path
    ) -> None:
        args = _base_args(
            variables_yml,
            template_pkg,
            outdir,
            slurm_qos="high",
            slurm_constraint="gpu",
            slurm_gres="gpu:1",
        )
        cfg = load_config(args)
        assert cfg.slurm_qos == "high"
        assert cfg.slurm_constraint == "gpu"
        assert cfg.slurm_gres == "gpu:1"

    def test_aws_batch_options(self, variables_yml: Path, template_pkg: Path, outdir: Path) -> None:
        args = _base_args(
            variables_yml,
            template_pkg,
            outdir,
            aws_batch_max_spot_price_usd="0.05",
            aws_batch_fallback_to_on_demand=True,
            aws_batch_max_retries="5",
            ecr_repository="123.dkr.ecr.us-east-1.amazonaws.com/os",
        )
        cfg = load_config(args)
        assert cfg.aws_batch_max_spot_price_usd == pytest.approx(0.05)
        assert cfg.aws_batch_fallback_to_on_demand is True
        assert cfg.aws_batch_max_retries == 5
        assert cfg.ecr_repository == "123.dkr.ecr.us-east-1.amazonaws.com/os"

    def test_mlflow_tracking_uri(
        self, variables_yml: Path, template_pkg: Path, outdir: Path
    ) -> None:
        args = _base_args(
            variables_yml,
            template_pkg,
            outdir,
            mlflow_tracking_uri="http://mlflow:5000",
        )
        cfg = load_config(args)
        assert cfg.mlflow_tracking_uri == "http://mlflow:5000"

    def test_init_finalize_scripts(
        self, variables_yml: Path, template_pkg: Path, outdir: Path, tmp_path: Path
    ) -> None:
        init = tmp_path / "init.sh"
        init.write_text("#!/bin/bash\ntrue\n")
        finalize = tmp_path / "finalize.sh"
        finalize.write_text("#!/bin/bash\ntrue\n")
        args = _base_args(
            variables_yml,
            template_pkg,
            outdir,
            init_script=str(init),
            finalize_script=str(finalize),
        )
        cfg = load_config(args)
        assert cfg.init_script == init.resolve()
        assert cfg.finalize_script == finalize.resolve()


class TestLoadConfigBaseline:
    def test_baseline_section_parsed(
        self, tmp_path: Path, template_pkg: Path, outdir: Path
    ) -> None:
        vyml = tmp_path / "variables.yml"
        vyml.write_text(
            yaml.dump(
                {
                    "variables": [{"name": "x", "distribution": "uniform", "min": 0.0, "max": 1.0}],
                    "baseline": {
                        "sample_id": "baseline_901",
                        "parameters": {"wall_r": 3.5, "window_shgc": 0.4},
                    },
                }
            )
        )
        args = _base_args(vyml, template_pkg, outdir)
        cfg = load_config(args)
        assert cfg.baseline is not None
        assert cfg.baseline["sample_id"] == "baseline_901"
        assert cfg.baseline["parameters"] == {"wall_r": 3.5, "window_shgc": 0.4}

    def test_no_baseline_section(
        self, variables_yml: Path, template_pkg: Path, outdir: Path
    ) -> None:
        args = _base_args(variables_yml, template_pkg, outdir)
        cfg = load_config(args)
        assert cfg.baseline is None

    def test_baseline_defaults_sample_id(
        self, tmp_path: Path, template_pkg: Path, outdir: Path
    ) -> None:
        vyml = tmp_path / "variables.yml"
        vyml.write_text(
            yaml.dump(
                {
                    "variables": [{"name": "x", "distribution": "uniform", "min": 0.0, "max": 1.0}],
                    "baseline": {"parameters": {"wall_r": 3.0}},
                }
            )
        )
        args = _base_args(vyml, template_pkg, outdir)
        cfg = load_config(args)
        assert cfg.baseline is not None
        assert cfg.baseline["sample_id"] == "baseline"

    def test_malformed_yaml_rejected(
        self, tmp_path: Path, template_pkg: Path, outdir: Path
    ) -> None:
        """Malformed YAML (non-dict) is now rejected by validation (issue #278)."""
        vyml = tmp_path / "variables.yml"
        vyml.write_text("just some text without yaml structure")
        args = _base_args(vyml, template_pkg, outdir)
        with pytest.raises(ValidationError):
            load_config(args)

    def test_empty_yaml_rejected(self, tmp_path: Path, template_pkg: Path, outdir: Path) -> None:
        """Empty YAML is now rejected by validation (issue #278)."""
        vyml = tmp_path / "variables.yml"
        vyml.write_text("")
        args = _base_args(vyml, template_pkg, outdir)
        with pytest.raises(ValidationError):
            load_config(args)


class TestLoadConfigWeatherDir:
    def test_custom_weather_dir(
        self, variables_yml: Path, template_pkg: Path, outdir: Path
    ) -> None:
        args = _base_args(variables_yml, template_pkg, outdir, weather_dir="epw_data")
        cfg = load_config(args)
        assert cfg.weather_dir == "epw_data"

    def test_default_weather_dir(
        self, variables_yml: Path, template_pkg: Path, outdir: Path
    ) -> None:
        args = _base_args(variables_yml, template_pkg, outdir)
        cfg = load_config(args)
        assert cfg.weather_dir == "weather"
