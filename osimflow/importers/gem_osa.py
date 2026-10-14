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
import math
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from osimflow.algorithms import AlgorithmRegistry
from osimflow.errors import OSimFlowError

log = logging.getLogger("osimflow.importers.gem_osa")

BATCHES_MANIFEST = "batches.json"
DEFAULT_BATCH_ID = "default"

_FILE_RE = re.compile(
    r"^(?P<kind>parametric_space|measure_space|osa_workflow)"
    r"(?:(?:_(?P<custom>[A-Za-z0-9].*))|(?:_?(?P<batch>Batch(?P<num>\d+)(?:_(?P<name>.+))?)))?\.json$"
)
_BATCH_RE = re.compile(r"^Batch(?P<num>\d+)(?:_(?P<name>.+))?$")
_NON_MEASURE_KEYS = {
    "algorithm_setting",
    "algorithm_settings",
    "analysis_settings",
    "osa_settings",
}
_GEM_ANALYSIS_TYPE_MAP: dict[str, str] = {
    "": "lhs",
    "lhs": "lhs",
    "single_run": "lhs",
    "repeat_run": "lhs",
    "preflight": "lhs",
    "doe": "full_factorial",
    "full_factorial": "full_factorial",
    "sobol": "sobol",
    "morris": "morris",
}


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
        raw_suffix = match.group("batch") or match.group("custom")
        if raw_suffix:
            bmatch = _BATCH_RE.match(raw_suffix)
            if bmatch:
                batch_id = raw_suffix
                num: int | None = int(bmatch.group("num"))
                bname: str | None = bmatch.group("name")
            else:
                batch_id = raw_suffix
                num = None
                bname = raw_suffix
        else:
            batch_id = DEFAULT_BATCH_ID
            num = None
            bname = None
        batch = batches.setdefault(
            batch_id,
            GemBatch(batch_id=batch_id, number=num, name=bname),
        )
        kind = match.group("kind")
        existing = getattr(batch, kind)
        if existing is not None and existing != path:
            raise GemImportError(
                f"duplicate {kind} files for batch {batch_id!r}: {existing.name} and {path.name}"
            )
        setattr(batch, kind, path)
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

    def _sort_key(b: GemBatch) -> tuple[int, int, str]:
        if b.number is not None:
            return (0, b.number, b.batch_id)
        if b.batch_id != DEFAULT_BATCH_ID:
            return (1, 0, b.batch_id)
        return (2, 0, b.batch_id)

    return sorted(usable, key=_sort_key)


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


def _factorial_levels_for_entry(entry: dict[str, Any], spec: Any) -> list[Any]:
    if isinstance(spec, list) and spec and "values" not in entry:
        return list(spec)
    if "values" in entry:
        return list(entry["values"])
    return [entry["min"], entry["max"]]


def _spec_to_variable(
    measure: str, argument: str, spec: Any, *, full_factorial: bool = False
) -> dict[str, Any]:
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
    if full_factorial:
        entry["levels"] = _factorial_levels_for_entry(entry, spec)
    return entry


def parametric_space_to_variables(
    space: dict[str, Any],
    *,
    full_factorial: bool = False,
    allow_empty: bool = False,
) -> list[dict[str, Any]]:
    """Convert a gem ``parametric_space`` mapping to ``variables.yml`` entries."""
    variables: list[dict[str, Any]] = []
    for measure, arguments in space.items():
        if measure in _NON_MEASURE_KEYS:
            continue
        if not isinstance(arguments, dict):
            raise GemImportError(f"{measure}: expected an object of argument specs")
        for argument, spec in arguments.items():
            variables.append(
                _spec_to_variable(measure, argument, spec, full_factorial=full_factorial)
            )
    if not variables and not allow_empty:
        raise GemImportError("parametric_space defines no variables")
    return variables


def _merge_settings_dict(source: dict[str, Any], target: dict[str, Any]) -> None:
    """Recursively merge non-measure setting dicts from *source* into *target*."""
    for key in ("osa_settings", "analysis_settings", "algorithm_settings", "algorithm_setting"):
        block = source.get(key)
        if isinstance(block, dict):
            for k, v in block.items():
                if k in _NON_MEASURE_KEYS and isinstance(v, dict):
                    _merge_settings_dict({k: v}, target)
                elif v is not None:
                    target[k] = v


def _algorithm_settings(
    batch: GemBatch, space: dict[str, Any] | None, configs: dict[str, Any]
) -> dict[str, Any]:
    """Merge seed / number_of_samples / sample_method / analysis_type (batch files win over configs.yml)."""
    settings: dict[str, Any] = {}
    _merge_settings_dict(configs, settings)
    if batch.osa_workflow:
        raw_osa = _load_json(batch.osa_workflow)
        analysis_obj = raw_osa.get("analysis", raw_osa)
        problem = analysis_obj.get("problem", {}) if isinstance(analysis_obj, dict) else {}
        algo = problem.get("algorithm") if isinstance(problem, dict) else None
        if isinstance(algo, dict):
            settings.update({k: v for k, v in algo.items() if k in _ALGO_KEYS and v is not None})
        if isinstance(problem, dict) and problem.get("analysis_type"):
            settings["analysis_type"] = problem["analysis_type"]
    if space:
        _merge_settings_dict(space, settings)
    return settings


_ALGO_KEYS = {"seed", "number_of_samples", "sample_method", "analysis_type"}


def _resolve_gem_algorithm(analysis_type: Any) -> str:
    """Map gem ``analysis_type`` to an OSimFlow algorithm name."""
    if analysis_type is None:
        return "lhs"
    if not isinstance(analysis_type, str):
        raise GemImportError(f"analysis_type must be a string, got {analysis_type!r}")
    norm = analysis_type.strip().lower()
    if norm in _GEM_ANALYSIS_TYPE_MAP:
        return _GEM_ANALYSIS_TYPE_MAP[norm]
    if norm in AlgorithmRegistry.available():
        return norm
    raise GemImportError(
        f"unsupported gem analysis_type {analysis_type!r}; "
        f"expected one of {sorted(set(_GEM_ANALYSIS_TYPE_MAP) - {''})} "
        f"or a registered algorithm ({', '.join(AlgorithmRegistry.available())})"
    )


def _as_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _parse_seed(value: Any) -> int | None:
    """Validate and return an integer sampling seed, or ``None`` when unset."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"seed must be an integer, got boolean {value!r}")
    if isinstance(value, int):
        return int(value)
    raise ValueError(f"seed must be an integer, got {value!r}")


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


def _rel_dir_if_contained(root_resolved: Path, candidate: Path) -> str | None:
    """Return POSIX package-relative path if *candidate* resolves inside *root_resolved* and is a dir."""
    try:
        resolved = candidate.resolve()
        rel = resolved.relative_to(root_resolved)
    except (OSError, ValueError):
        return None
    if not resolved.is_dir():
        return None
    return "." if rel == Path(".") else rel.as_posix()


def _rel_file_if_contained(root_resolved: Path, candidate: Path) -> Path | None:
    """Return relative Path if *candidate* resolves inside *root_resolved* and is a file."""
    try:
        resolved = candidate.resolve()
        rel = resolved.relative_to(root_resolved)
    except (OSError, ValueError):
        return None
    return rel if resolved.is_file() else None


def _check_existing_file_in_package(
    root_resolved: Path,
    raw_path: Path,
    valid_file_dirs: list[Path],
    source_root_resolved: Path | None = None,
) -> tuple[str, str | None] | None:
    """Return ``(field_val, required_dir)`` if *raw_path* already resolves inside *root_resolved*."""
    rel = _rel_file_if_contained(root_resolved, root_resolved / raw_path)
    if rel is not None:
        parent_rel = rel.parent.as_posix() if rel.parent != Path(".") else None
        return rel.as_posix(), parent_rel
    if raw_path.is_absolute():
        if source_root_resolved is not None:
            src_rel = _rel_file_if_contained(source_root_resolved, raw_path)
            if src_rel is not None:
                copied_rel = _rel_file_if_contained(root_resolved, root_resolved / src_rel)
                if copied_rel is not None:
                    parent_rel = (
                        copied_rel.parent.as_posix() if copied_rel.parent != Path(".") else None
                    )
                    return copied_rel.as_posix(), parent_rel
        return None
    for fdir in valid_file_dirs:
        if _rel_file_if_contained(root_resolved, fdir / raw_path) is not None:
            fdir_rel = fdir.resolve().relative_to(root_resolved)
            return (
                raw_path.as_posix(),
                "." if fdir_rel == Path(".") else fdir_rel.as_posix(),
            )
    return None


def _resolve_file_in_package(
    root_resolved: Path,
    raw_value: str,
    valid_file_dirs: list[Path],
    field_name: str,
    source_root_resolved: Path | None = None,
) -> tuple[str, str | None]:
    """Resolve *raw_value* (seed_file or weather_file) within *root_resolved*.

    Returns ``(new_field_value, required_file_path_dir_or_None)``.
    Raises :class:`GemImportError` if no candidate or multiple candidates match.
    """
    raw_path = Path(raw_value)
    existing = _check_existing_file_in_package(
        root_resolved, raw_path, valid_file_dirs, source_root_resolved
    )
    if existing is not None:
        return existing

    target_name = raw_path.name
    if not target_name:
        raise GemImportError(f"workflow.osw {field_name} {raw_value!r} has an empty filename")
    matches = [
        rel
        for p in sorted(root_resolved.rglob(target_name))
        if (rel := _rel_file_if_contained(root_resolved, p)) is not None and rel.name == target_name
    ]
    if not matches:
        raise GemImportError(
            f"workflow.osw {field_name} {raw_value!r} escapes or is missing from template package "
            f"and no file named {target_name!r} was found inside {root_resolved}"
        )
    if len(matches) > 1:
        rel_matches = [m.as_posix() for m in matches]
        raise GemImportError(
            f"workflow.osw {field_name} {raw_value!r} is ambiguous inside template package; "
            f"candidates: {rel_matches}"
        )
    chosen_rel = matches[0]
    parent_rel = chosen_rel.parent.as_posix() if chosen_rel.parent != Path(".") else None
    return chosen_rel.as_posix(), parent_rel


def _collect_valid_rel_dirs(
    root_resolved: Path, raw_entries: Any, source_root_resolved: Path | None = None
) -> list[str]:
    """Keep existing path entries that resolve to directories inside *root_resolved*."""
    result: list[str] = []
    if not isinstance(raw_entries, list):
        return result
    for entry in raw_entries:
        if not isinstance(entry, str) or not entry.strip():
            continue
        p = Path(entry.strip())
        rel_dir = _rel_dir_if_contained(root_resolved, root_resolved / p)
        if rel_dir is None and p.is_absolute() and source_root_resolved is not None:
            src_rel = _rel_dir_if_contained(source_root_resolved, p)
            if src_rel is not None:
                rel_dir = _rel_dir_if_contained(root_resolved, root_resolved / src_rel)
        if rel_dir is not None and rel_dir not in result:
            result.append(rel_dir)
    return result


def _find_step_measure_parent(root_resolved: Path, mdir: str, had_invalid_mp: bool) -> str | None:
    """Locate the unambiguous package-relative parent directory containing measure *mdir*."""
    candidates: list[str] = []
    for cand in sorted(root_resolved.rglob(Path(mdir).name)):
        parent_str = _rel_dir_if_contained(root_resolved, cand.parent) if cand.is_dir() else None
        if (
            parent_str is not None
            and (root_resolved / parent_str / mdir).is_dir()
            and parent_str not in candidates
        ):
            candidates.append(parent_str)
    if len(candidates) > 1:
        raise GemImportError(
            f"workflow.osw measure {mdir!r} is ambiguous inside template package; "
            f"candidate measure_paths: {candidates}"
        )
    if not candidates and had_invalid_mp:
        raise GemImportError(
            f"workflow.osw measure_paths could not resolve step measure {mdir!r} "
            f"inside template package {root_resolved}"
        )
    return candidates[0] if candidates else None


def _normalize_measure_paths(
    root_resolved: Path, osw: dict[str, Any], source_root_resolved: Path | None = None
) -> None:
    """Normalize ``measure_paths`` in *osw* in-place."""
    raw_mp = osw.get("measure_paths")
    valid_mp = _collect_valid_rel_dirs(root_resolved, raw_mp, source_root_resolved)
    had_invalid_mp = isinstance(raw_mp, list) and len(valid_mp) < len(raw_mp)
    if (root_resolved / "measures").is_dir() and "measures" not in valid_mp:
        valid_mp.append("measures")

    steps = osw.get("steps")
    if isinstance(steps, list):
        for step in steps:
            mdir = step.get("measure_dir_name") if isinstance(step, dict) else None
            if not isinstance(mdir, str) or not mdir.strip():
                continue
            if any((root_resolved / mp / mdir).is_dir() for mp in valid_mp):
                continue
            parent_str = _find_step_measure_parent(root_resolved, mdir, had_invalid_mp)
            if parent_str is not None and parent_str not in valid_mp:
                valid_mp.append(parent_str)

    if valid_mp or "measure_paths" in osw:
        osw["measure_paths"] = valid_mp


def _normalize_osw_paths(package_path: Path, source_package_path: Path | None = None) -> None:
    """Normalize ``measure_paths``, ``file_paths``, ``seed_file``, and ``weather_file`` in ``workflow.osw``."""
    osw_path = package_path / "workflow.osw"
    if not osw_path.is_file():
        return
    root_resolved = package_path.resolve()
    source_root_resolved = source_package_path.resolve() if source_package_path else None
    osw = _load_json(osw_path)

    _normalize_measure_paths(root_resolved, osw, source_root_resolved)
    valid_fp = _collect_valid_rel_dirs(root_resolved, osw.get("file_paths"), source_root_resolved)
    valid_file_dirs = [root_resolved / fp for fp in valid_fp]

    for field_name in ("seed_file", "weather_file"):
        raw_val = osw.get(field_name)
        if isinstance(raw_val, str) and raw_val.strip():
            new_val, req_dir = _resolve_file_in_package(
                root_resolved,
                raw_val.strip(),
                valid_file_dirs,
                field_name,
                source_root_resolved,
            )
            osw[field_name] = new_val
            if req_dir is not None and req_dir not in valid_fp:
                valid_fp.append(req_dir)

    for std_dir in ("weather", "seed_models"):
        if (root_resolved / std_dir).is_dir() and std_dir not in valid_fp:
            valid_fp.append(std_dir)

    if valid_fp or "file_paths" in osw:
        osw["file_paths"] = valid_fp

    osw_path.write_text(json.dumps(osw, indent=2), encoding="utf-8")


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


def _preserve_cleanup_sql(osw_path: Path, varied: set[str]) -> None:
    """Force ``ServerDirectoryCleanup.sql`` to ``False``/``"false"`` unless varied in *varied*."""
    if not osw_path.is_file() or "ServerDirectoryCleanup.sql" in varied:
        return
    osw = _load_json(osw_path)
    updated = False
    for step in osw.get("steps") or []:
        if isinstance(step, dict) and step.get("measure_dir_name") == "ServerDirectoryCleanup":
            step_args = step.setdefault("arguments", {})
            if isinstance(step_args, dict):
                step_args["sql"] = (
                    _coerce_like(step_args["sql"], "false") if "sql" in step_args else False
                )
                updated = True
    if updated:
        osw_path.write_text(json.dumps(osw, indent=2), encoding="utf-8")


def _resolve_batch_n_samples(
    settings: dict[str, Any],
    raw_analysis_type: Any,
    variables: list[dict[str, Any]],
    *,
    is_full_factorial: bool,
) -> int | None:
    if not variables:
        return _as_int(settings.get("number_of_samples")) or 1
    if is_full_factorial:
        return math.prod(len(v["levels"]) for v in variables) if variables else 0
    n_samples = _as_int(settings.get("number_of_samples"))
    if (
        n_samples is None
        and isinstance(raw_analysis_type, str)
        and raw_analysis_type.strip().lower() == "single_run"
    ):
        return 1
    return n_samples


def _stage_batch_template(
    batch: GemBatch,
    batch_dir: Path,
    template_package: Path | None,
    varied: set[str],
) -> tuple[Path | None, list[str]]:
    if template_package is None:
        return None, []
    package_path = batch_dir / "template"
    if package_path.exists():
        shutil.rmtree(package_path)
    shutil.copytree(template_package, package_path)
    osw_path = package_path / "workflow.osw"
    if osw_path.is_file():
        _normalize_osw_paths(package_path, source_package_path=template_package)
    unmatched: list[str] = []
    if batch.measure_space is not None and osw_path.is_file():
        raw = _load_json(batch.measure_space)
        static = dict(raw.get("measure_space", raw))
        reporting = raw.get("measure_space_reporting")
        if isinstance(reporting, dict):
            for measure, arguments in reporting.items():
                static.setdefault(measure, arguments)
        unmatched = _overlay_measure_space(osw_path, static, varied)
        for measure in unmatched:
            log.warning("%s: measure %r has no step in %s", batch.batch_id, measure, osw_path)
    _preserve_cleanup_sql(osw_path, varied)
    return package_path, unmatched


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
    settings = _algorithm_settings(batch, space, configs)
    raw_analysis_type = settings.get("analysis_type")
    algorithm = _resolve_gem_algorithm(raw_analysis_type)
    is_full_factorial = algorithm == "full_factorial"
    variables = parametric_space_to_variables(
        space, full_factorial=is_full_factorial, allow_empty=True
    )
    seed = _parse_seed(settings.get("seed"))

    batch_dir = output_dir / batch.batch_id
    batch_dir.mkdir(parents=True, exist_ok=True)
    variables_path = batch_dir / "variables.yml"
    variables_doc: dict[str, Any] = {"algorithm": algorithm}
    if seed is not None:
        variables_doc["seed"] = seed
    if not variables:
        variables_doc["prebuilt_workflow"] = True
    variables_doc["variables"] = variables
    variables_path.write_text(
        yaml.safe_dump(variables_doc, sort_keys=False),
        encoding="utf-8",
    )

    varied = {str(v["name"]) for v in variables}
    package_path, unmatched = _stage_batch_template(batch, batch_dir, template_package, varied)

    n_samples = _resolve_batch_n_samples(
        settings, raw_analysis_type, variables, is_full_factorial=is_full_factorial
    )
    outdir = batch_dir / "results"
    command = ["osimflow", "run", "--input_variables", str(variables_path)]
    if package_path is not None:
        command += ["--template_sim_package", str(package_path)]
    command += ["--outdir", str(outdir)]
    if n_samples is not None:
        command += ["--n_samples", str(n_samples)]
    if not variables:
        command.append("--prebuilt-workflow")
    return {
        "batch_id": batch.batch_id,
        "batch_number": batch.number,
        "batch_name": batch.name,
        "campaign_id": batch.batch_id,
        "variables_yml": str(variables_path),
        "template_package": str(package_path) if package_path else None,
        "outdir": str(outdir),
        "n_samples": n_samples,
        "seed": seed,
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
