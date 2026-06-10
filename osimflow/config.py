"""Campaign configuration loader.

A thin wrapper around the `variables.yml` schema, plus the CLI flags
the PRD §1.4 calls out as required (`--input_variables`,
`--template_sim_package`, `--n_samples`, `--outdir`,
`--openstudio_version`, `--archive_intermediates`).
"""

import dataclasses
import logging
from pathlib import Path

import yaml

log = logging.getLogger("osimflow.config")


@dataclasses.dataclass
class CampaignConfig:
    input_variables: Path
    template_sim_package: Path
    n_samples: int
    outdir: Path
    openstudio_version: str
    archive_intermediates: bool = False
    custom_apply_script: Path | None = None
    custom_kpi_extractor: Path | None = None
    # Optional MLflow tracking server (issue #7). When None, the campaign
    # runs without any MLflow integration (no mlflow import, no logging).
    # When set, the Campaign begins an MLflow run at start and ends it at
    # completion, logging params / metrics / artifacts.
    mlflow_tracking_uri: str | None = None
    # Optional Slurm advanced directives (issue #4). Forwarded to
    # `SlurmExecutor` when `--executor slurm` is selected. All default
    # to `None`; submitit omits unset directives from the sbatch header.
    slurm_qos: str | None = None
    slurm_constraint: str | None = None
    slurm_gres: str | None = None
    # Optional ASHRAE 90.1 baseline comparison mode (issue #64).
    # When a `baseline` section is defined in variables.yml, the
    # campaign injects a fixed-parameter baseline sample alongside
    # the LHS parametric samples, computes percentage improvement
    # for each KPI, and adds baseline reference data to outputs.
    # The dict has keys: "sample_id" (str) and "parameters" (dict).
    # When None, baseline comparison is disabled and behaviour is
    # unchanged.
    baseline: dict[str, object] | None = None
    # Weather file subdirectory convention (issue #63).
    # Name of the subdirectory inside template_sim_package that holds
    # .epw weather files. Defaults to "weather". The pre-flight
    # validation pass checks that all .epw files referenced in
    # variables.yml exist under this directory and validates their
    # EPW format (header line starts with "LOCATION").
    weather_dir: str = "weather"
    # Dry-run mode (issue #59): run exactly 1 sample locally to validate
    # setup before committing to a full campaign. Forces n_samples=1 and
    # LocalExecutor regardless of CLI flags. Runs steps 1-4 only (no
    # aggregation or plots) and prints a summary.
    dry_run: bool = False
    # Single-sample mode (issue #59): run only the sample at 0-based index
    # N through steps 2-4. Skips GENERATE_LHS_SAMPLES (reuses existing
    # samples.json). Useful for debugging a specific failed sample.
    sample: int | None = None
    # Sampling algorithm name (issue #121). Dispatched through
    # AlgorithmRegistry.get(). Defaults to "lhs" for backward
    # compatibility. Future options: "sobol", "morris", etc.
    algorithm: str = "lhs"
    # Pre/post campaign shell hooks (issue #108).
    # --init-script: runs before the first campaign step. Must exit 0
    # or the campaign aborts. Useful for S3 sync, Slack notifications.
    init_script: Path | None = None
    # --finalize-script: runs after the last campaign step. Best-effort:
    # a non-zero exit code is logged but does NOT fail the campaign.
    # Useful for cleanup, upload, notification, DynamoDB writes.
    finalize_script: Path | None = None

    @property
    def work_dir(self) -> Path:
        return self.outdir / "work"

    @property
    def samples_file(self) -> Path:
        return self.work_dir / "samples.json"

    @property
    def cache_db(self) -> Path:
        return self.work_dir / "cache.sqlite"


def load_config(args: dict[str, object]) -> CampaignConfig:
    """Resolve a config from a flat dict (e.g. argparse namespace -> vars).

    Each value is narrowed via `str()` / `bool()` / `int()` because argparse
    already validates types — the cast here is purely a type-checker
    shim, not a runtime guarantee.
    """
    variables_yml = Path(str(args["input_variables"])).resolve()
    template = Path(str(args["template_sim_package"])).resolve()
    if not variables_yml.exists():
        raise FileNotFoundError(f"variables_yml not found: {variables_yml}")
    if not template.exists():
        raise FileNotFoundError(f"template_sim_package not found: {template}")

    outdir = Path(str(args["outdir"])).resolve()
    outdir.mkdir(parents=True, exist_ok=True)

    custom_apply = args.get("custom_apply_script")
    custom_kpi = args.get("custom_kpi_extractor")

    # Parse the optional baseline section from variables.yml (issue #64).
    baseline: dict[str, object] | None = None
    try:
        with variables_yml.open() as f:
            yml_data = yaml.safe_load(f)
        if isinstance(yml_data, dict) and "baseline" in yml_data:
            raw_baseline = yml_data["baseline"]
            if isinstance(raw_baseline, dict):
                baseline = {
                    "sample_id": str(raw_baseline.get("sample_id", "baseline")),
                    "parameters": dict(raw_baseline.get("parameters", {})),
                }
                log.info(
                    "baseline comparison enabled: sample_id=%s, %d parameters",
                    baseline["sample_id"],
                    len(baseline["parameters"]),  # type: ignore[arg-type]
                )
    except Exception as exc:
        log.warning("could not parse baseline section from %s: %s", variables_yml, exc)

    return CampaignConfig(
        input_variables=variables_yml,
        template_sim_package=template,
        n_samples=int(str(args["n_samples"])),
        outdir=outdir,
        openstudio_version=str(args["openstudio_version"]),
        archive_intermediates=bool(args.get("archive_intermediates", False)),
        custom_apply_script=Path(str(custom_apply)).resolve() if custom_apply else None,
        custom_kpi_extractor=Path(str(custom_kpi)).resolve() if custom_kpi else None,
        mlflow_tracking_uri=(
            str(args["mlflow_tracking_uri"]) if args.get("mlflow_tracking_uri") else None
        ),
        slurm_qos=str(args["slurm_qos"]) if args.get("slurm_qos") else None,
        slurm_constraint=(str(args["slurm_constraint"]) if args.get("slurm_constraint") else None),
        slurm_gres=str(args["slurm_gres"]) if args.get("slurm_gres") else None,
        baseline=baseline,
        weather_dir=str(args["weather_dir"]) if args.get("weather_dir") else "weather",
        dry_run=bool(args.get("dry_run", False)),
        sample=int(str(args["sample"])) if args.get("sample") is not None else None,
        algorithm=str(args["algorithm"]) if args.get("algorithm") else "lhs",
        init_script=(Path(str(args["init_script"])).resolve() if args.get("init_script") else None),
        finalize_script=(
            Path(str(args["finalize_script"])).resolve() if args.get("finalize_script") else None
        ),
    )
