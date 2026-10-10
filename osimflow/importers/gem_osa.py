"""Import openstudio-bem-to-surrogate-gem project files (issue #1870).

The gem describes a study with three JSON families, optionally suffixed per
batch (``Batch<N>_<name>``):

* ``parametric_space[_Batch<N>_<name>].json`` — ``{measure: {argument: spec}}``
  where *spec* is ``[min, max]`` (two numbers), ``{"min", "max",
  "samplecount"}``, or a list of choices; plus an optional
  ``algorithm_setting`` block (``seed``, ``number_of_samples``).
* ``measure_space[_Batch...].json`` — ``{"measure_space": {measure:
  {argument: value}}}``: the static baseline arguments.
* ``osa_workflow[Batch...].json`` — the openstudio-server analysis; only its
  ``analysis.problem.algorithm`` settings are consumed here.

``configs.yml`` (``osa_settings.analysis_settings``) supplies defaults.

Mapping to OSimFlow
-------------------

==============================  ================================================
Gem spec                        ``variables.yml`` entry
==============================  ================================================
``[min, max]`` / ``{min,max}``  ``uniform`` (``samplecount`` is ignored)
list of >= 3 numbers            ``discrete`` ``values``
list containing any string      ``categorical`` ``values``
==============================  ================================================

Variables are named ``<measure>.<argument>`` (the qualified form OSimFlow's
OSW argument mapping understands), so identical argument names in different
measures never collide. One campaign directory is emitted per batch under
``<output_dir>/<batch_id>/`` and ``<output_dir>/batches.json`` records the
stable batch -> campaign/outdir mapping.
"""

import json
import logging
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from osimflow.errors import OSimFlowError

log = logging.getLogger("osimflow.importers.gem_osa")

BATCHES_MANIFEST = "batches.json"
DEFAULT_BATCH_ID = "default"

_FILE_RE = re.compile(
    r"^(?P<kind>parametric_space|measure_space|osa_workflow)"
    r"(?:_?(?P<batch>Batch(?P<num>\d+)(?:_(?P<name>.+))?))?\.json$"
)
_NON_MEASURE_KEYS = {"algorithm_setting"}


class GemImportError(OSimFlowError):
    """The gem project files are missing or malformed."""


@dataclass
class GemBatch:
    """The gem files that belong to one batch."""

    batch_id: str
    number: int | None
    name: str | None
    parametric_space: Path | None = None
    measure_space: Path | None = None
    osa_workflow: Path | None = None


def discover_batches(project_dir: Path) -> list[GemBatch]:
    """Group the gem JSON files in *project_dir* by batch, ordered by batch number."""
    project_dir = Path(project_dir)
    if not project_dir.is_dir():
        raise GemImportError(f"gem project directory not found: {project_dir}")
    batches: dict[str, GemBatch] = {}
    for path in sorted(project_dir.iterdir()):
        match = _FILE_RE.match(path.name)
        if not match:
            continue
        batch_id = match.group("batch") or DEFAULT_BATCH_ID
        num = match.group("num")
        batch = batches.setdefault(
            batch_id,
            GemBatch(batch_id=batch_id, number=int(num) if num else None, name=match.group("name")),
        )
        setattr(batch, match.group("kind"), path)
    shared = batches.get(DEFAULT_BATCH_ID)
    usable = [b for b in batches.values() if b.parametric_space or b.osa_workflow]
    if shared is not None and shared.measure_space is not None:
        for batch in usable:
            if batch.measure_space is None:
                batch.measure_space = shared.measure_space
    if not usable:
        raise GemImportError(
            f"no parametric_space*.json or osa_workflow*.json found in {project_dir}"
        )
    return sorted(usable, key=lambda b: (b.number is None, b.number or 0, b.batch_id))


def _load_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.error("failed to read %s", path, exc_info=True)
        raise GemImportError(f"cannot read {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise GemImportError(f"{path} must contain a JSON object")
    return data


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _spec_to_variable(measure: str, argument: str, spec: Any) -> dict[str, Any]:
    name = f"{measure}.{argument}"
    entry: dict[str, Any] = {"name": name, "measure_argument": name}
    if isinstance(spec, dict):
        if "min" not in spec or "max" not in spec:
            raise GemImportError(f"{name}: range spec needs 'min' and 'max', got {sorted(spec)}")
        if not (_is_number(spec["min"]) and _is_number(spec["max"])):
            raise GemImportError(f"{name}: range 'min'/'max' must be numbers, got {spec!r}")
        entry.update(distribution="uniform", min=float(spec["min"]), max=float(spec["max"]))
    elif isinstance(spec, list) and spec:
        if len(spec) == 2 and all(_is_number(v) for v in spec):
            entry.update(distribution="uniform", min=float(spec[0]), max=float(spec[1]))
        elif all(_is_number(v) for v in spec):
            entry.update(distribution="discrete", values=list(spec))
        else:
            if not all(isinstance(v, (str, bool, int, float)) for v in spec):
                raise GemImportError(f"{name}: unsupported choice values {spec!r}")
            values = [v if isinstance(v, bool) else str(v) for v in spec]
            entry.update(distribution="categorical", values=values)
    else:
        raise GemImportError(f"{name}: unsupported parametric_space spec {spec!r}")
    return entry


def parametric_space_to_variables(space: dict[str, Any]) -> list[dict[str, Any]]:
    """Convert a gem ``parametric_space`` mapping to ``variables.yml`` entries."""
    variables: list[dict[str, Any]] = []
    for measure, arguments in space.items():
        if measure in _NON_MEASURE_KEYS:
            continue
        if not isinstance(arguments, dict):
            raise GemImportError(f"{measure}: expected an object of argument specs")
        for argument, spec in arguments.items():
            variables.append(_spec_to_variable(measure, argument, spec))
    if not variables:
        raise GemImportError("parametric_space defines no variables")
    return variables


def _algorithm_settings(
    batch: GemBatch, space: dict[str, Any] | None, configs: dict[str, Any]
) -> dict[str, Any]:
    """Merge seed / number_of_samples / sample_method (batch files win over configs.yml)."""
    settings: dict[str, Any] = {}
    cfg_settings = (
        (configs.get("osa_settings") or {}).get("analysis_settings", {}).get("algorithm_settings")
    )
    if isinstance(cfg_settings, dict):
        settings.update(cfg_settings)
    cfg_type = (configs.get("osa_settings") or {}).get("analysis_settings", {}).get("analysis_type")
    if cfg_type:
        settings.setdefault("analysis_type", cfg_type)
    if batch.osa_workflow:
        problem = _load_json(batch.osa_workflow).get("analysis", {}).get("problem", {})
        algo = problem.get("algorithm") if isinstance(problem, dict) else None
        if isinstance(algo, dict):
            settings.update({k: v for k, v in algo.items() if k in _ALGO_KEYS and v is not None})
        if isinstance(problem, dict) and problem.get("analysis_type"):
            settings["analysis_type"] = problem["analysis_type"]
    if space and isinstance(space.get("algorithm_setting"), dict):
        settings.update(space["algorithm_setting"])
    return settings


_ALGO_KEYS = {"seed", "number_of_samples", "sample_method"}


def _as_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _coerce_like(existing: Any, value: Any) -> Any:
    """Coerce the gem's string-typed *value* to the type already used in the OSW."""
    if not isinstance(value, str):
        return value
    if isinstance(existing, bool):
        lowered = value.strip().lower()
        if lowered in ("true", "false"):
            return lowered == "true"
    elif isinstance(existing, (int, float)):
        try:
            number = float(value)
        except ValueError:
            return value
        return int(number) if isinstance(existing, int) and number.is_integer() else number
    return value


def _overlay_measure_space(
    osw_path: Path, measure_space: dict[str, Any], varied: set[str]
) -> list[str]:
    """Write static measure_space arguments into *osw_path*; return unmatched measures."""
    osw = json.loads(osw_path.read_text(encoding="utf-8"))
    steps = osw.get("steps", [])
    by_dir = {s.get("measure_dir_name"): s for s in steps if isinstance(s, dict)}
    unmatched: list[str] = []
    for measure, arguments in measure_space.items():
        step = by_dir.get(measure)
        if step is None:
            unmatched.append(measure)
            continue
        if not isinstance(arguments, dict):
            continue
        step_args = step.setdefault("arguments", {})
        for argument, value in arguments.items():
            if f"{measure}.{argument}" in varied:
                continue
            step_args[argument] = _coerce_like(step_args.get(argument), value)
    osw_path.write_text(json.dumps(osw, indent=2), encoding="utf-8")
    return unmatched


def _load_configs(project_dir: Path, configs: Path | None) -> dict[str, Any]:
    path = configs if configs is not None else project_dir / "configs.yml"
    if not path.is_file():
        if configs is not None:
            raise GemImportError(f"configs file not found: {path}")
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        log.error("failed to read %s", path, exc_info=True)
        raise GemImportError(f"cannot read {path}: {exc}") from exc
    return data if isinstance(data, dict) else {}


def _import_batch(
    batch: GemBatch,
    output_dir: Path,
    configs: dict[str, Any],
    template_package: Path | None,
) -> dict[str, Any]:
    if batch.parametric_space is None:
        raise GemImportError(
            f"{batch.batch_id}: no parametric_space file (osa_workflow alone is not "
            "enough; use `osimflow import-osa` for server analysis JSON)"
        )
    space = _load_json(batch.parametric_space)
    variables = parametric_space_to_variables(space)
    settings = _algorithm_settings(batch, space, configs)

    batch_dir = output_dir / batch.batch_id
    batch_dir.mkdir(parents=True, exist_ok=True)
    variables_path = batch_dir / "variables.yml"
    variables_path.write_text(
        yaml.safe_dump({"algorithm": "lhs", "variables": variables}, sort_keys=False),
        encoding="utf-8",
    )

    unmatched: list[str] = []
    package_path: Path | None = None
    if template_package is not None:
        package_path = batch_dir / "template"
        if package_path.exists():
            shutil.rmtree(package_path)
        shutil.copytree(template_package, package_path)
        osw_path = package_path / "workflow.osw"
        if batch.measure_space is not None and osw_path.is_file():
            raw = _load_json(batch.measure_space)
            static = dict(raw.get("measure_space", raw))
            reporting = raw.get("measure_space_reporting")
            if isinstance(reporting, dict):
                for measure, arguments in reporting.items():
                    static.setdefault(measure, arguments)
            varied = {str(v["name"]) for v in variables}
            unmatched = _overlay_measure_space(osw_path, static, varied)
            for measure in unmatched:
                log.warning("%s: measure %r has no step in %s", batch.batch_id, measure, osw_path)

    n_samples = _as_int(settings.get("number_of_samples"))
    outdir = batch_dir / "results"
    command = ["osimflow", "run", "--input_variables", str(variables_path)]
    if package_path is not None:
        command += ["--template_sim_package", str(package_path)]
    command += ["--outdir", str(outdir)]
    if n_samples is not None:
        command += ["--n_samples", str(n_samples)]
    return {
        "batch_id": batch.batch_id,
        "batch_number": batch.number,
        "batch_name": batch.name,
        "campaign_id": batch.batch_id,
        "variables_yml": str(variables_path),
        "template_package": str(package_path) if package_path else None,
        "outdir": str(outdir),
        "n_samples": n_samples,
        "seed": _as_int(settings.get("seed")),
        "sample_method": settings.get("sample_method"),
        "analysis_type": settings.get("analysis_type"),
        "n_variables": len(variables),
        "unmatched_measures": unmatched,
        "command": command,
    }


def import_gem_project(
    project_dir: Path,
    output_dir: Path,
    *,
    batches: list[str] | None = None,
    template_package: Path | None = None,
    configs: Path | None = None,
) -> dict[str, Any]:
    """Convert a gem project into one OSimFlow campaign directory per batch.

    Returns (and writes to ``<output_dir>/batches.json``) the manifest mapping
    each stable batch id to its variables file, template package, outdir,
    sampling settings and a ready-to-run ``osimflow run`` command.
    """
    project_dir = Path(project_dir)
    output_dir = Path(output_dir)
    if template_package is not None and not (Path(template_package) / "workflow.osw").is_file():
        raise GemImportError(f"--template-package must contain workflow.osw: {template_package}")
    found = discover_batches(project_dir)
    if batches:
        known = {b.batch_id for b in found}
        missing = sorted(set(batches) - known)
        if missing:
            raise GemImportError(f"unknown batch(es) {missing}; available: {sorted(known)}")
        found = [b for b in found if b.batch_id in batches]
    cfg = _load_configs(project_dir, configs)
    output_dir.mkdir(parents=True, exist_ok=True)
    entries = [
        _import_batch(b, output_dir, cfg, Path(template_package) if template_package else None)
        for b in found
    ]
    merged: dict[str, Any] = {}
    previous = output_dir / BATCHES_MANIFEST
    if batches and previous.is_file():
        # a partial re-import must not drop mappings for other batches
        merged.update(_load_json(previous).get("batches", {}))
    merged.update({e["batch_id"]: e for e in entries})
    manifest: dict[str, Any] = {"project_dir": str(project_dir.resolve()), "batches": merged}
    (output_dir / BATCHES_MANIFEST).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    log.info("imported %d batch(es) into %s", len(entries), output_dir)
    return manifest
