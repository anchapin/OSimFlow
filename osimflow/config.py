"""Campaign configuration loader.

A thin wrapper around the `variables.yml` schema, plus the CLI flags
the PRD §1.4 calls out as required (`--input_variables`,
`--template_sim_package`, `--n_samples`, `--outdir`,
`--openstudio_version`, `--archive_intermediates`).
"""
import dataclasses
import logging
from pathlib import Path
from typing import Optional

log = logging.getLogger("osimflow.config")


@dataclasses.dataclass
class CampaignConfig:
    input_variables: Path
    template_sim_package: Path
    n_samples: int
    outdir: Path
    openstudio_version: str
    archive_intermediates: bool = False
    custom_apply_script: Optional[Path] = None
    custom_kpi_extractor: Optional[Path] = None

    @property
    def work_dir(self) -> Path:
        return self.outdir / "work"

    @property
    def samples_file(self) -> Path:
        return self.work_dir / "samples.json"

    @property
    def cache_db(self) -> Path:
        return self.work_dir / "cache.sqlite"


def load_config(args: dict) -> CampaignConfig:
    """Resolve a config from a flat dict (e.g. argparse namespace -> vars)."""
    variables_yml = Path(args["input_variables"]).resolve()
    template = Path(args["template_sim_package"]).resolve()
    if not variables_yml.exists():
        raise FileNotFoundError(f"variables_yml not found: {variables_yml}")
    if not template.exists():
        raise FileNotFoundError(f"template_sim_package not found: {template}")

    outdir = Path(args["outdir"]).resolve()
    outdir.mkdir(parents=True, exist_ok=True)

    return CampaignConfig(
        input_variables=variables_yml,
        template_sim_package=template,
        n_samples=int(args["n_samples"]),
        outdir=outdir,
        openstudio_version=str(args["openstudio_version"]),
        archive_intermediates=bool(args.get("archive_intermediates", False)),
        custom_apply_script=Path(args["custom_apply_script"]).resolve() if args.get("custom_apply_script") else None,
        custom_kpi_extractor=Path(args["custom_kpi_extractor"]).resolve() if args.get("custom_kpi_extractor") else None,
    )
