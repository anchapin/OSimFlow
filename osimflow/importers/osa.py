"""OpenStudio Analysis (.osa / analysis.json) import converter.

Converts OSA variable definitions and distributions to OSimFlow's
``variables.yml`` schema and optionally stages a ready-to-run template
simulation package (with static workflow arguments overlaid and paths
normalised) so that users migrating from the OpenStudio Analysis
Spreadsheet / Parametric Analysis Tool can run their studies through
``osimflow run``.

Typical OSA structure (analysis.json)::

    {
      "analysis": {
        "display_name": "My Parametric Study",
        "problem": {
          "algorithm": {
            "type": "lhs",
            "number_of_samples": 100,
            "seed": 0
          },
          "variables": [
            {
              "name": "insul_r",
              "display_name": "Insulation R-value",
              "variable_type": "variable",
              "distribution": {
                "type": "uniform",
                "minimum": 5.0,
                "maximum": 30.0
              },
              "measure": {
                "display_name": "SetInsulationRValue",
                "argument": "r_value"
              }
            }
          ]
        }
      }
    }

Distribution mapping
--------------------

============  ====================  ============================  ==============
OSA type      OSimFlow distribution Parameters                    Notes
============  ====================  ============================  ==============
uniform       uniform               min, max
normal        normal                mean, sigma
lognormal     lognormal             mean, sigma
triangular    triangular            min, max, mode (optional)
discrete      discrete              values, pmf (optional)
categorical   categorical           values, mapping (optional)
pivot         categorical           values, mapping (optional)    pivot: true
(none)        static                value                         locked variables
============  ====================  ============================  ==============

Variable type mapping
---------------------

====================  =====================  ============================
OSA variable_type     OSimFlow field         Notes
====================  =====================  ============================
variable              variable_type=variable  (default)
argument              variable_type=argument  measure argument
pivot                 pivot=true              mapped to categorical + flag
====================  =====================  ============================
"""

import json
import logging
import math
import shutil
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

from osimflow.algorithms import AlgorithmRegistry, BaseAlgorithm
from osimflow.apply_params import seed_file_candidates
from osimflow.errors import OSimFlowError

log = logging.getLogger("osimflow.importers.osa")

_SKIP_COPY_NAMES = frozenset({".DS_Store", "__pycache__", ".git"})
_PREBUILT_ALGORITHM_TYPES = frozenset({"single_run", "preflight", "repeat_run"})

OSA_DISTRIBUTION_MAP: dict[str, str] = {
    "uniform": "uniform",
    "normal": "normal",
    "lognormal": "lognormal",
    "lognormal_uncertain": "lognormal",
    "triangular": "triangular",
    "discrete": "discrete",
    "categorical": "categorical",
    "enum": "categorical",
    "pivot": "categorical",
}


class OSAImportError(OSimFlowError):
    """Raised when an OSA file cannot be parsed or converted."""


def _extract_analysis_data(raw: dict[str, Any]) -> dict[str, Any]:
    """Normalise the OSA JSON into a flat analysis dict.

    Handles two common layouts:

    1. ``{"analysis": {"problem": ..., "workflow": ...}}``
    2. ``{"problem": ..., "workflow": ...}``  (top-level)
    """
    if "analysis" in raw and isinstance(raw["analysis"], dict):
        return raw["analysis"]
    if "problem" in raw:
        return raw
    raise OSAImportError(
        "Unrecognised OSA structure: expected top-level 'analysis' or 'problem' key"
    )


def _require_keys(osa_dist: dict[str, Any], keys: list[str], label: str) -> None:
    for key in keys:
        if osa_dist.get(key) is None:
            raise OSAImportError(f"{label} distribution requires '{key}'")


def _map_uniform(osa_dist: dict[str, Any]) -> dict[str, Any]:
    _require_keys(osa_dist, ["minimum", "maximum"], "Uniform")
    return {"min": float(osa_dist["minimum"]), "max": float(osa_dist["maximum"])}


def _map_normal(osa_dist: dict[str, Any]) -> dict[str, Any]:
    _require_keys(osa_dist, ["mean"], "Normal")
    sigma = osa_dist.get("stddev") or osa_dist.get("sigma")
    if sigma is None:
        raise OSAImportError("Normal distribution requires 'stddev' (or 'sigma')")
    return {"mean": float(osa_dist["mean"]), "sigma": float(sigma)}


def _map_lognormal(osa_dist: dict[str, Any]) -> dict[str, Any]:
    _require_keys(osa_dist, ["mean"], "Lognormal")
    sigma = osa_dist.get("stddev") or osa_dist.get("sigma")
    if sigma is None:
        raise OSAImportError("Lognormal distribution requires 'stddev' (or 'sigma')")
    return {"mean": float(osa_dist["mean"]), "sigma": float(sigma)}


def _map_triangular(osa_dist: dict[str, Any]) -> dict[str, Any]:
    _require_keys(osa_dist, ["minimum", "maximum"], "Triangular")
    result: dict[str, Any] = {"min": float(osa_dist["minimum"]), "max": float(osa_dist["maximum"])}
    mode = osa_dist.get("mode") or osa_dist.get("peak")
    if mode is not None:
        result["mode"] = float(mode)
    return result


def _build_pmf(osimflow_type: str, values: list[Any], weights: Any) -> dict[Any, float] | None:
    """Return ``{value: weight}`` when *weights* are present and non-uniform, else ``None``."""
    if weights is None:
        return None
    if not isinstance(weights, list) or len(weights) != len(values):
        raise OSAImportError(
            f"{osimflow_type} 'weights' must be a list with one weight per value "
            f"({len(values)} values, got {weights!r})"
        )
    try:
        numeric = [float(w) for w in weights]
    except (TypeError, ValueError) as exc:
        raise OSAImportError(f"{osimflow_type} weights must be numeric, got {weights!r}") from exc
    if any(w < 0 for w in numeric) or not math.isfinite(sum(numeric)) or sum(numeric) <= 0:
        raise OSAImportError(
            f"{osimflow_type} weights must be non-negative with a positive sum, got {weights!r}"
        )
    if len(set(numeric)) == 1:
        return None
    if len(set(map(str, values))) != len(values):
        raise OSAImportError(f"{osimflow_type} weighted values must be unique, got {values!r}")
    return dict(zip(values, numeric, strict=True))


def _map_discrete_or_categorical(osimflow_type: str, osa_dist: dict[str, Any]) -> dict[str, Any]:
    values = osa_dist.get("values") or osa_dist.get("discrete_values")
    weights = osa_dist.get("weights")
    vaw = osa_dist.get("values_and_weights")
    if not values and isinstance(vaw, list):
        values = [item["value"] for item in vaw if isinstance(item, dict) and "value" in item]
        weights = [item.get("weight") for item in vaw if isinstance(item, dict) and "value" in item]
    if not values or not isinstance(values, list):
        raise OSAImportError(f"{osimflow_type} distribution requires a non-empty 'values' list")
    result: dict[str, Any] = {"values": values}
    pmf = _build_pmf(osimflow_type, values, weights)
    if pmf is not None:
        result["pmf"] = pmf
    mapping = osa_dist.get("mapping")
    if mapping and isinstance(mapping, dict):
        result["mapping"] = mapping
    return result


_DISTRIBUTION_MAPPERS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "uniform": _map_uniform,
    "normal": _map_normal,
    "lognormal": _map_lognormal,
    "triangular": _map_triangular,
}


def _map_distribution(osa_dist: dict[str, Any]) -> dict[str, Any]:
    """Convert an OSA distribution block to an OSimFlow variable entry.

    Parameters
    ----------
    osa_dist : dict
        The ``distribution`` sub-object from an OSA variable. Must contain
        a ``type`` key and the appropriate parameter keys.

    Returns
    -------
    dict
        Keys suitable for a ``variables.yml`` entry (``distribution`` plus
        distribution-specific parameters).

    Raises
    ------
    OSAImportError
        If the distribution type is unknown or required parameters are
        missing.
    """
    osa_type = osa_dist.get("type", "").lower()
    osimflow_type = OSA_DISTRIBUTION_MAP.get(osa_type)

    if osimflow_type is None:
        raise OSAImportError(
            f"Unsupported OSA distribution type {osa_type!r}; "
            f"supported: {', '.join(sorted(OSA_DISTRIBUTION_MAP))}"
        )

    result: dict[str, Any] = {"distribution": osimflow_type}

    if osimflow_type in ("discrete", "categorical"):
        result.update(_map_discrete_or_categorical(osimflow_type, osa_dist))
    else:
        mapper = _DISTRIBUTION_MAPPERS.get(osimflow_type)
        if mapper is not None:
            result.update(mapper(osa_dist))

    return result


def _attach_common_fields(
    entry: dict[str, Any],
    osa_var: dict[str, Any],
    name: str,
    osa_variable_type: str,
) -> None:
    """Attach measure_argument, display_name, and variable_type to *entry*."""
    if osa_variable_type and osa_variable_type != "variable":
        entry["variable_type"] = osa_variable_type
    measure_ref = _resolve_measure_argument(osa_var)
    if measure_ref:
        entry["measure_argument"] = measure_ref
    display_name = osa_var.get("display_name")
    if display_name and display_name != name:
        entry["display_name"] = display_name


def _convert_static_variable(
    name: str, osa_var: dict[str, Any], osa_variable_type: str
) -> tuple[dict[str, Any], list[str]]:
    """Convert a variable with no distribution (static / locked)."""
    default_value = osa_var.get("default_value")
    if default_value is None:
        default_value = osa_var.get("value") or osa_var.get("static_value")
    entry: dict[str, Any] = {"name": name, "distribution": "static"}
    if default_value is not None:
        entry["value"] = default_value
    _attach_common_fields(entry, osa_var, name, osa_variable_type)
    return entry, []


def _convert_pivot_variable(
    name: str, osa_var: dict[str, Any], osa_dist: dict[str, Any]
) -> tuple[dict[str, Any], list[str]]:
    """Convert a pivot variable to categorical with ``pivot: true``."""
    warnings: list[str] = []
    dist_entry: dict[str, Any] = {"distribution": "categorical"}
    values = (
        osa_dist.get("values") or osa_dist.get("discrete_values") or osa_dist.get("enumerations")
    )
    if values and isinstance(values, list):
        dist_entry["values"] = values
    else:
        dist_entry["values"] = []
        warnings.append(
            f"Variable {name!r}: pivot distribution has no 'values'; imported with empty list"
        )
    mapping = osa_dist.get("mapping")
    if mapping and isinstance(mapping, dict):
        dist_entry["mapping"] = mapping
    dist_entry["pivot"] = True

    entry: dict[str, Any] = {"name": name}
    entry.update(dist_entry)
    entry["variable_type"] = "pivot"
    _attach_common_fields(entry, osa_var, name, "pivot")
    return entry, warnings


def _normalize_uncertainty_description(unc: dict[str, Any]) -> dict[str, Any]:
    dist = {"type": unc.get("type", "")}
    for attr in unc.get("attributes", []):
        name = attr.get("name")
        if name == "lower_bounds":
            dist["minimum"] = attr.get("value")
        elif name == "upper_bounds":
            dist["maximum"] = attr.get("value")
        elif name == "modes":
            dist["mode"] = attr.get("value")
        elif name in ("stddev", "sigma"):
            if attr.get("value") is not None:
                dist["stddev"] = attr.get("value")
        elif name == "mean":
            if attr.get("value") is not None:
                dist["mean"] = attr.get("value")
        elif name == "discrete":
            vaw = [i for i in attr.get("values_and_weights", []) if isinstance(i, dict)]
            vaw = [item for item in vaw if "value" in item]
            dist["values"] = [item["value"] for item in vaw]
            if vaw and all(item.get("weight") is not None for item in vaw):
                dist["weights"] = [item["weight"] for item in vaw]
        elif name == "weights" and isinstance(attr.get("value"), list):
            dist["weights"] = attr["value"]
        elif name in ("values", "discrete_values") and isinstance(attr.get("value"), list):
            dist["values"] = attr["value"]
    return dist


def _convert_variable(osa_var: dict[str, Any], index: int) -> tuple[dict[str, Any], list[str]]:
    """Try to convert a single OSA variable; return (entry, warnings)."""
    if not isinstance(osa_var, dict):
        return {}, [f"Variable #{index}: skipped (not a dict)"]

    name = osa_var.get("name") or osa_var.get("display_name") or osa_var.get("uuid")
    if not name:
        return {}, [f"Variable #{index}: skipped (no name)"]

    osa_variable_type = osa_var.get("variable_type", "variable")
    is_pivot = osa_variable_type == "pivot"
    osa_dist = osa_var.get("distribution")
    if (
        not osa_dist
        and "uncertainty_description" in osa_var
        and isinstance(osa_var["uncertainty_description"], dict)
    ):
        osa_dist = _normalize_uncertainty_description(osa_var["uncertainty_description"])

    if not osa_dist or not isinstance(osa_dist, dict) or not osa_dist.get("type"):
        return _convert_static_variable(name, osa_var, osa_variable_type)

    if is_pivot or osa_dist.get("type", "").lower() == "pivot":
        return _convert_pivot_variable(name, osa_var, osa_dist)

    try:
        dist_entry = _map_distribution(osa_dist)
    except OSAImportError as exc:
        return {}, [f"Variable {name!r}: {exc}"]

    entry: dict[str, Any] = {"name": name}
    entry.update(dist_entry)
    _attach_common_fields(entry, osa_var, name, osa_variable_type)
    return entry, []


# Translation table: OSA algorithm type → OSimFlow registry name.
# See https://github.com/NREL/openstudio-analysis-gem for the canonical
# OSA algorithm types.
_OSA_ALGORITHM_MAP: dict[str, str] = {
    "lhs": "lhs",
    "latin_hypercube": "lhs",
    "nsga_nrel": "nsga2",
    "pso": "pso",
    "ga": "de",
    "optim": "de",
    "sobol": "sobol",
    "morris": "morris",
    "fast99": "fast99",
    "doe": "full_factorial",
    "full_factorial": "full_factorial",
    "grid": "grid",
    "single_run": "lhs",
    "preflight": "lhs",
    "repeat_run": "lhs",
    "halton": "halton",
    "random": "random",
    "repeat_all": "repeat_all",
    "spea_nrel": "spea2",
    "gaisl": "gaisl",
    "rgenoud": "rgenoud",
}


def _resolve_algorithm(osa_algo: dict[str, Any]) -> BaseAlgorithm:
    """Resolve an OSA algorithm dict to an OSimFlow :class:`BaseAlgorithm`.

    Parameters
    ----------
    osa_algo
        The ``algorithm`` sub-object from an OSA problem definition.
        Must contain a ``type`` key.

    Returns
    -------
    BaseAlgorithm
        An instantiated algorithm from the OSimFlow registry.

    Raises
    ------
    OSAImportError
        If the algorithm type is unknown or the OSimFlow registry does
        not contain the mapped algorithm (e.g. pymoo not installed for
        ``nsga2``).
    """
    algo_type = osa_algo.get("type", "").lower()
    osimflow_name = _OSA_ALGORITHM_MAP.get(algo_type)
    if osimflow_name is None:
        supported_osa = ", ".join(sorted(_OSA_ALGORITHM_MAP))
        supported_osimflow = ", ".join(AlgorithmRegistry.list_available())
        raise OSAImportError(
            f"Unknown algorithm '{algo_type}'. "
            f"Supported OSA types: {supported_osa}. "
            f"Registered OSimFlow algorithms: {supported_osimflow}"
        )
    try:
        return AlgorithmRegistry.get(osimflow_name)
    except ValueError as exc:
        raise OSAImportError(
            f"OSA algorithm '{algo_type}' maps to OSimFlow algorithm "
            f"'{osimflow_name}', but it is not available. "
            f"Original error: {exc}. "
            f"Install the required optional dependency (e.g. "
            f"pip install osimflow[pymoo]) and retry."
        ) from exc


def _resolve_measure_argument(osa_var: dict[str, Any]) -> str | None:
    """Build the ``measure_name.argument_name`` dotted reference.

    Returns ``None`` if the variable has no measure mapping (e.g. it is
    a model-level variable rather than a measure argument).
    """
    measure = osa_var.get("measure")
    if not measure or not isinstance(measure, dict):
        return None
    measure_name = measure.get("name") or measure.get("display_name")
    argument = measure.get("argument") or measure.get("argument_name")
    if not measure_name or not argument:
        return None
    return f"{measure_name}.{argument}"


def parse_osa(osa_path: Path) -> dict[str, Any]:
    """Parse an ``.osa`` file and extract the analysis definition.

    An ``.osa`` file is a ZIP archive containing ``analysis.json`` (and
    typically measure/model files). This function extracts and returns the
    parsed JSON data.

    If *osa_path* is a plain JSON file (extension ``.json``), it is read
    directly — this supports the common case where the user already
    extracted ``analysis.json`` from the archive.

    Parameters
    ----------
    osa_path : Path
        Path to the ``.osa`` ZIP file or an ``analysis.json`` file.

    Returns
    -------
    dict
        The parsed analysis data (normalised via :func:`_extract_analysis_data`).

    Raises
    ------
    OSAImportError
        If the file cannot be read or parsed.
    FileNotFoundError
        If *osa_path* does not exist.
    """
    osa_path = Path(osa_path)
    if not osa_path.exists():
        raise FileNotFoundError(f"OSA file not found: {osa_path}")

    suffix = osa_path.suffix.lower()

    if suffix == ".osa" or zipfile.is_zipfile(osa_path):
        try:
            with zipfile.ZipFile(osa_path) as zf:
                names = zf.namelist()
                analysis_name: str | None = None
                for candidate in ("analysis.json", "analysis/analysis.json"):
                    if candidate in names:
                        analysis_name = candidate
                        break
                if analysis_name is None:
                    for n in names:
                        if n.lower().endswith("analysis.json"):
                            analysis_name = n
                            break
                if analysis_name is None:
                    raise OSAImportError(
                        f"No analysis.json found inside {osa_path}; "
                        f"archive contains: {', '.join(names[:10])}"
                    )
                raw = json.loads(zf.read(analysis_name))
        except json.JSONDecodeError as exc:
            raise OSAImportError(f"Invalid JSON in {osa_path}: {exc}") from exc
        except zipfile.BadZipFile as exc:
            raise OSAImportError(f"Not a valid ZIP/OSA file: {osa_path}: {exc}") from exc
    else:
        try:
            raw = json.loads(osa_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise OSAImportError(f"Invalid JSON in {osa_path}: {exc}") from exc

    return _extract_analysis_data(raw)


def parse_analysis_json(analysis_path: Path) -> dict[str, Any]:
    """Parse an OpenStudio ``analysis.json`` file directly.

    This is a convenience wrapper around :func:`parse_osa` for callers
    that know they have a plain JSON file (not a ZIP).

    Parameters
    ----------
    analysis_path : Path
        Path to the ``analysis.json`` file.

    Returns
    -------
    dict
        The normalised analysis data.
    """
    return parse_osa(analysis_path)


def _measure_step_name(measure: dict[str, Any]) -> str | None:
    """Resolve the canonical measure directory/step name from an OSA workflow step."""
    m_dir = (
        measure.get("measure_definition_directory")
        or measure.get("measure_definition_directory_local")
        or measure.get("measure_dir_name")
    )
    if isinstance(m_dir, str) and m_dir.strip():
        return Path(m_dir.strip()).name
    for key in ("measure_definition_name", "name", "measure_definition_class_name"):
        val = measure.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return None


def _dir_instance_counts(steps: list[Any]) -> dict[str, int]:
    """Count workflow steps per canonical ``measure_dir_name``."""
    counts: dict[str, int] = {}
    for step in steps:
        if isinstance(step, dict):
            c = _measure_step_name(step)
            if c:
                counts[c] = counts.get(c, 0) + 1
    return counts


def _step_instance_name(step: dict[str, Any], dir_counts: dict[str, int]) -> str | None:
    """Name used to reference *step*: its own ``name`` when its measure dir repeats.

    Single-instance steps keep the canonical ``measure_dir_name`` (backward
    compatible); steps sharing a directory are referenced by their unique
    per-step ``"name"`` so they do not collide in ``variables.yml``.
    """
    canonical = _measure_step_name(step)
    if canonical and dir_counts.get(canonical, 0) > 1:
        name = step.get("name")
        if isinstance(name, str) and name.strip():
            return name.strip()
    return canonical


def _is_varied_workflow_variable(mv: dict[str, Any]) -> bool:
    """Return True if a workflow ``variables[]`` entry represents a varied parameter.

    In standard OpenStudio ``.osa`` / ``analysis.json`` workflow steps, entries
    under ``variables[]`` with ``"variable": false`` (and no distribution or
    uncertainty description) represent static step argument assignments rather
    than varied campaign parameters.
    """
    return mv.get("variable") is not False


def _extract_workflow_variables(workflow: list[Any]) -> list[dict[str, Any]]:
    """Extract variables nested inside workflow measures.

    Parameters
    ----------
    workflow : list
        The workflow list from an OSA problem definition.

    Returns
    -------
    list[dict[str, Any]]
        Extracted variables with measure information attached.
    """
    extracted: list[dict[str, Any]] = []
    dir_counts = _dir_instance_counts(workflow)
    for measure in workflow:
        if not isinstance(measure, dict):
            continue
        measure_vars = measure.get("variables", [])
        if not isinstance(measure_vars, list):
            continue
        m_name = _step_instance_name(measure, dir_counts)
        for mv in measure_vars:
            if not isinstance(mv, dict) or not _is_varied_workflow_variable(mv):
                continue
            mv_copy = dict(mv)
            arg_name: str | None = None
            if "argument" in mv_copy and isinstance(mv_copy["argument"], dict):
                arg_name = mv_copy["argument"].get("name")
            elif isinstance(mv_copy.get("argument_name"), str):
                arg_name = mv_copy["argument_name"]
            if m_name and arg_name:
                mv_copy["measure"] = {"name": m_name, "argument": arg_name}
            extracted.append(mv_copy)
    return extracted


_STEP_ALIAS_KEYS = (
    "name",
    "display_name",
    "measure_definition_display_name",
    "measure_definition_name",
    "measure_definition_class_name",
)


def _build_step_alias_map(workflow: Any) -> dict[str, str]:
    """Map every identifier of each workflow step to the name that references it.

    Single-instance steps map to their canonical ``measure_dir_name``. Steps that
    share a ``measure_dir_name`` map their own unique identifiers to the step's
    ``"name"`` (the shared directory itself is not an alias). Identifiers that
    resolve to more than one distinct step are dropped as ambiguous.
    """
    aliases: dict[str, set[str]] = {}
    if not isinstance(workflow, list):
        return {}
    steps = [s for s in workflow if isinstance(s, dict)]
    dir_counts = _dir_instance_counts(steps)
    for step in steps:
        canonical = _measure_step_name(step)
        if not canonical:
            continue
        target = _step_instance_name(step, dir_counts)
        if not target:
            continue
        shared = dir_counts[canonical] > 1
        if shared and target == canonical:
            continue
        candidates: list[Any] = [target] if shared else [canonical]
        candidates += [step.get(k) for k in _STEP_ALIAS_KEYS]
        for cand in candidates:
            if isinstance(cand, str) and cand.strip():
                if shared and cand.strip() == canonical:
                    continue
                aliases.setdefault(cand.strip(), set()).add(target)
    return {a: next(iter(c)) for a, c in aliases.items() if len(c) == 1}


def _normalize_variable_measures(
    osa_vars: list[dict[str, Any]], alias_map: dict[str, str]
) -> list[dict[str, Any]]:
    """Rewrite variable measure references to the canonical workflow ``measure_dir_name``."""
    if not alias_map:
        return osa_vars
    out: list[dict[str, Any]] = []
    for var in osa_vars:
        new = dict(var)
        measure = var.get("measure")
        if isinstance(measure, dict):
            ids = [
                measure.get("name"),
                measure.get("display_name"),
                measure.get("measure_definition_class_name"),
                var.get("measure_definition_class_name"),
            ]
            canonical = next(
                (alias_map[i] for i in ids if isinstance(i, str) and i in alias_map), None
            )
            if canonical:
                new["measure"] = {**measure, "name": canonical}
        elif isinstance(var.get("name"), str) and "." in var["name"]:
            prefix, arg = var["name"].split(".", 1)
            if prefix in alias_map and "measure_argument" not in var:
                new["measure"] = {"name": alias_map[prefix], "argument": arg}
        out.append(new)
    return out


_OUTPUT_KEYS = (
    "name",
    "display_name",
    "units",
    "objective_function",
    "objective_function_index",
    "objective_function_target",
)


def _extract_outputs(osa_data: dict[str, Any], problem: dict[str, Any]) -> list[dict[str, Any]]:
    """Collect ``output_variables`` / ``outputs`` declarations from the analysis."""
    raw: Any = None
    for container, key in (
        (osa_data, "output_variables"),
        (problem, "output_variables"),
        (problem, "outputs"),
    ):
        if isinstance(container.get(key), list) and container[key]:
            raw = container[key]
            break
    outputs: list[dict[str, Any]] = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        kept = {k: item[k] for k in _OUTPUT_KEYS if item.get(k) is not None}
        if kept.get("name") or kept.get("display_name"):
            outputs.append(kept)
    return outputs


def _objective_from_outputs(outputs: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Derive an ``objective`` section from the first ``objective_function: true`` output."""
    flagged = [o for o in outputs if o.get("objective_function") is True]
    if not flagged:
        return None
    flagged.sort(key=lambda o: o.get("objective_function_index", 0))
    first = flagged[0]
    objective: dict[str, Any] = {"name": first.get("name") or first.get("display_name")}
    target = first.get("objective_function_target")
    if isinstance(target, (int, float)) and not isinstance(target, bool):
        objective["target"] = float(target)
    return objective


def _extract_seed(algorithm: dict[str, Any]) -> int | None:
    """Extract and validate an optional integer ``seed`` from an OSA algorithm block."""
    raw_seed = algorithm.get("seed")
    if raw_seed is None:
        return None
    if isinstance(raw_seed, bool) or not isinstance(raw_seed, int):
        raise OSAImportError(f"OSA algorithm 'seed' must be an integer, got {raw_seed!r}")
    return int(raw_seed)


def _extract_n_samples(algorithm: dict[str, Any]) -> int | None:
    """Extract and validate an optional positive ``number_of_samples`` from an OSA algorithm block."""
    raw_n = algorithm.get("number_of_samples")
    if raw_n is None:
        raw_n = algorithm.get("n_samples")
    if raw_n is None:
        return None
    if isinstance(raw_n, bool) or not isinstance(raw_n, int) or raw_n < 1:
        raise OSAImportError(
            f"OSA algorithm 'number_of_samples' must be a positive integer, got {raw_n!r}"
        )
    return int(raw_n)


def _cartesian_sample_count(variables: list[dict[str, Any]]) -> int | None:
    """Product of per-variable level counts (``values`` or ``[min, max]``), or ``None``."""
    count = 1
    seen = False
    for var in variables:
        if var.get("distribution") == "conditional":
            continue
        if var.get("distribution") == "static":
            seen = True
            continue
        values = var.get("values")
        if isinstance(values, list) and values:
            count *= len(values)
        elif var.get("min") is not None and var.get("max") is not None:
            count *= 2
        else:
            return None
        seen = True
    return count if seen else None


def _supports_prebuilt_workflow(problem: dict[str, Any], allow_empty: bool) -> bool:
    """Return True when a 0-variable OSA problem represents a valid prebuilt workflow."""
    if allow_empty:
        return True
    workflow = problem.get("workflow")
    if isinstance(workflow, list) and len(workflow) > 0:
        return True
    algorithm = problem.get("algorithm")
    if isinstance(algorithm, dict):
        algo_type = str(algorithm.get("type") or problem.get("analysis_type") or "").lower()
        if algo_type in _PREBUILT_ALGORITHM_TYPES:
            return True
    analysis_type = str(problem.get("analysis_type") or "").lower()
    return analysis_type in _PREBUILT_ALGORITHM_TYPES


def _extract_problem_variables(
    problem: dict[str, Any],
    *,
    allow_empty: bool,
) -> tuple[list[dict[str, Any]], bool]:
    """Convert active OSA problem variables or determine prebuilt-workflow mode."""
    raw_problem_vars = problem.get("variables", [])
    if not isinstance(raw_problem_vars, list):
        raise OSAImportError("'problem.variables' must be a list")
    osa_variables = [
        v for v in raw_problem_vars if isinstance(v, dict) and _is_varied_workflow_variable(v)
    ]
    if not osa_variables and not raw_problem_vars:
        workflow_raw = problem.get("workflow", [])
        if isinstance(workflow_raw, list):
            osa_variables = _extract_workflow_variables(workflow_raw)
    osa_variables = _normalize_variable_measures(
        osa_variables, _build_step_alias_map(problem.get("workflow"))
    )

    if not osa_variables:
        if not raw_problem_vars and _supports_prebuilt_workflow(problem, allow_empty):
            return [], True
        raise OSAImportError("No variables found in OSA problem definition or workflow measures")

    warnings: list[str] = []
    converted: list[dict[str, Any]] = []
    for i, osa_var in enumerate(osa_variables):
        entry, var_warnings = _convert_variable(osa_var, i)
        warnings.extend(var_warnings)
        if entry:
            converted.append(entry)

    if not converted:
        raise OSAImportError(
            "No variables could be converted from the OSA file. Warnings:\n"
            + "\n".join(f"  - {w}" for w in warnings)
        )

    for w in warnings:
        log.warning(w)
    return converted, False


def _resolve_problem_algorithm_dict(problem: dict[str, Any]) -> dict[str, Any]:
    """Extract and normalise the ``algorithm`` dictionary from *problem*."""
    algorithm_raw = problem.get("algorithm", {})
    if algorithm_raw is not None and not isinstance(algorithm_raw, dict):
        raise OSAImportError("'problem.algorithm' must be a dict")
    algorithm: dict[str, Any] = dict(algorithm_raw) if isinstance(algorithm_raw, dict) else {}
    if "type" not in algorithm and "analysis_type" in problem:
        algorithm["type"] = problem["analysis_type"]
    return algorithm


def _resolve_objective(
    algorithm: dict[str, Any], outputs: list[dict[str, Any]]
) -> dict[str, Any] | None:
    explicit = algorithm.get("objective")
    if isinstance(explicit, dict):
        objective = {
            k: explicit[k]
            for k in ("name", "direction", "weight", "target", "scaling_factor")
            if k in explicit
        }
        if objective:
            return objective
    return _objective_from_outputs(outputs)


def osa_to_variables_yml(
    osa_data: dict[str, Any],
    output_path: Path,
    *,
    allow_empty: bool = False,
) -> None:
    """Convert parsed OSA data to an OSimFlow ``variables.yml`` file.

    Parameters
    ----------
    osa_data : dict
        Normalised analysis data (as returned by :func:`parse_osa`).
    output_path : Path
        Where to write the resulting ``variables.yml``.
    allow_empty : bool, optional
        When True, allow 0-variable problems to emit ``variables: []`` with
        ``prebuilt_workflow: true`` even when no ``workflow`` steps or
        single-run algorithm type are present.

    Raises
    ------
    OSAImportError
        If no variables are found (and the problem is not a prebuilt/single-run
        workflow) or a distribution/algorithm cannot be mapped.
    """
    problem = osa_data.get("problem", {})
    if not isinstance(problem, dict):
        raise OSAImportError("'problem' is not a dict")

    algorithm = _resolve_problem_algorithm_dict(problem)
    algorithm_name: str = "lhs"
    if algorithm.get("type"):
        resolved = _resolve_algorithm(algorithm)
        algorithm_name = resolved.name()
        log.info(
            "OSA algorithm %r resolved to OSimFlow algorithm %r",
            algorithm.get("type", ""),
            algorithm_name,
        )

    seed = _extract_seed(algorithm)
    n_samples = _extract_n_samples(algorithm)
    converted, is_prebuilt = _extract_problem_variables(problem, allow_empty=allow_empty)

    metadata: dict[str, Any] = {"algorithm": algorithm_name}
    if n_samples is not None:
        metadata["n_samples"] = n_samples
    elif is_prebuilt:
        metadata["n_samples"] = 1
    elif algorithm_name == "full_factorial":
        cartesian = _cartesian_sample_count(converted)
        if cartesian is not None:
            metadata["n_samples"] = cartesian
    if seed is not None:
        metadata["seed"] = seed
    outputs = _extract_outputs(osa_data, problem)
    if outputs:
        metadata["outputs"] = outputs

    variables_yml: dict[str, Any] = {
        "_metadata": metadata,
        "algorithm": algorithm_name,
    }
    if seed is not None:
        variables_yml["seed"] = seed
    if is_prebuilt:
        variables_yml["prebuilt_workflow"] = True
    objective = _resolve_objective(algorithm, outputs)
    if objective:
        variables_yml["objective"] = objective
    variables_yml["variables"] = converted

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as f:
        yaml.dump(variables_yml, f, default_flow_style=False, sort_keys=False)

    log.info("Wrote %d variables to %s", len(converted), output_path)


def _ignore_copy_names(_dir: str, names: list[str]) -> set[str]:
    return {name for name in names if name in _SKIP_COPY_NAMES}


def _copy_package_tree(src: Path, dst: Path) -> None:
    """Copy a template simulation package directory into *dst*."""
    src_resolved = src.resolve()
    dst_resolved = dst.resolve()
    if src_resolved == dst_resolved:
        return
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst, ignore=_ignore_copy_names)


def _extract_osa_zip(osa_path: Path, dst: Path) -> None:
    """Safely extract an ``.osa`` ZIP archive into *dst* (preventing ZipSlip)."""
    dst.mkdir(parents=True, exist_ok=True)
    dst_resolved = dst.resolve()
    try:
        with zipfile.ZipFile(osa_path) as zf:
            for member in zf.infolist():
                member_name = member.filename
                if not member_name or Path(member_name).name in _SKIP_COPY_NAMES:
                    continue
                target_path = (dst / member_name).resolve()
                try:
                    target_path.relative_to(dst_resolved)
                except ValueError as exc:
                    raise OSAImportError(
                        f"Refusing to extract archive member outside destination: {member_name!r}"
                    ) from exc
                if member.is_dir():
                    target_path.mkdir(parents=True, exist_ok=True)
                else:
                    target_path.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(member) as src_fh, target_path.open("wb") as dst_fh:
                        shutil.copyfileobj(src_fh, dst_fh)
    except zipfile.BadZipFile as exc:
        raise OSAImportError(f"Not a valid ZIP/OSA file: {osa_path}: {exc}") from exc


def _coerce_int_or_float(existing: int | float, value: Any) -> Any:
    """Coerce *value* to ``int`` or ``float`` matching *existing* when lossless."""
    result = value
    if isinstance(existing, int):
        if isinstance(value, float) and value.is_integer():
            result = int(value)
        elif isinstance(value, str):
            try:
                result = int(value)
            except ValueError:
                result = value
    elif isinstance(value, int | str) and not isinstance(value, bool):
        try:
            result = float(value)
        except ValueError:
            result = value
    return result


def _coerce_like(existing: Any, value: Any) -> Any:
    """Coerce *value* to match the type of *existing* in ``workflow.osw`` when safe."""
    result = value
    if existing is None or isinstance(value, type(existing)):
        return result
    if isinstance(existing, bool):
        if isinstance(value, str) and value.lower() in ("true", "false"):
            result = value.lower() == "true"
    elif isinstance(existing, int | float):
        result = _coerce_int_or_float(existing, value)
    elif isinstance(existing, str) and isinstance(value, int | float | bool):
        result = ("true" if value else "false") if isinstance(value, bool) else str(value)
    return result


def _static_var_arg_and_value(mv: dict[str, Any]) -> tuple[str | None, Any, bool]:
    """Extract ``(arg_name, value, found)`` from a non-varied workflow variable dict."""
    arg_name: str | None = None
    if isinstance(mv.get("argument"), dict):
        arg_name = mv["argument"].get("name")
    elif isinstance(mv.get("argument_name"), str):
        arg_name = mv["argument_name"]
    elif isinstance(mv.get("name"), str):
        arg_name = mv["name"]
    if not isinstance(arg_name, str) or not arg_name:
        return None, None, False
    for val_key in ("static_value", "value", "default_value"):
        if val_key in mv and mv[val_key] is not None:
            return arg_name, mv[val_key], True
    arg_block = mv.get("argument")
    if isinstance(arg_block, dict) and arg_block.get("default_value") is not None:
        return arg_name, arg_block["default_value"], True
    return arg_name, None, False


def _extract_step_static_arguments(step: dict[str, Any]) -> dict[str, Any]:
    """Extract static argument assignments from an OSA workflow step entry."""
    result: dict[str, Any] = {}
    raw_args = step.get("arguments")
    if isinstance(raw_args, dict):
        result.update({k: v for k, v in raw_args.items() if isinstance(k, str) and k})
    elif isinstance(raw_args, list):
        for item in raw_args:
            if not isinstance(item, dict):
                continue
            arg_name = item.get("name") or item.get("argument_name")
            if isinstance(arg_name, str) and arg_name and "value" in item:
                result[arg_name] = item["value"]

    raw_vars = step.get("variables")
    if isinstance(raw_vars, list):
        for mv in raw_vars:
            if not isinstance(mv, dict) or _is_varied_workflow_variable(mv):
                continue
            arg_name, val, found = _static_var_arg_and_value(mv)
            if found and arg_name is not None:
                result[arg_name] = val
    return result


def _collect_varied_measure_args(osa_data: dict[str, Any]) -> set[tuple[str, str]]:
    """Return the set of ``(measure_name, argument_name)`` pairs varied by *osa_data*."""
    problem = osa_data.get("problem", {})
    if not isinstance(problem, dict):
        return set()
    raw_problem_vars = problem.get("variables", [])
    osa_variables: list[dict[str, Any]] = []
    if isinstance(raw_problem_vars, list):
        osa_variables = [
            v for v in raw_problem_vars if isinstance(v, dict) and _is_varied_workflow_variable(v)
        ]
    if not osa_variables and isinstance(problem.get("workflow"), list):
        osa_variables = _extract_workflow_variables(problem["workflow"])
    osa_variables = _normalize_variable_measures(
        osa_variables, _build_step_alias_map(problem.get("workflow"))
    )

    varied: set[tuple[str, str]] = set()
    for osa_var in osa_variables:
        ref = _resolve_measure_argument(osa_var) or osa_var.get("name")
        if isinstance(ref, str) and "." in ref:
            m_name, a_name = ref.split(".", 1)
            varied.add((m_name, a_name))
    return varied


def _resolve_file_in_package(
    pkg_dir: Path,
    raw_ref: str,
    candidate_subdirs: tuple[str, ...],
    field_name: str,
    source_root: Path | None = None,
) -> str:
    """Resolve an OSW file reference to a path relative to *pkg_dir*."""
    pkg_resolved = pkg_dir.resolve()
    ref_path = Path(raw_ref)
    if ref_path.is_absolute():
        for root in (pkg_resolved, source_root.resolve() if source_root is not None else None):
            if root is None:
                continue
            try:
                rel = ref_path.resolve().relative_to(root)
                candidate = (pkg_dir / rel).resolve()
                if candidate.is_file():
                    return candidate.relative_to(pkg_resolved).as_posix()
            except ValueError:
                continue
    else:
        direct = (pkg_dir / ref_path).resolve()
        try:
            rel = direct.relative_to(pkg_resolved)
            if direct.is_file():
                return rel.as_posix()
        except ValueError:
            pass

    basename = ref_path.name
    for subdir in candidate_subdirs:
        candidate = (pkg_dir / subdir / basename) if subdir else (pkg_dir / basename)
        if candidate.is_file():
            return candidate.resolve().relative_to(pkg_resolved).as_posix()

    matches = sorted(
        p.resolve().relative_to(pkg_resolved).as_posix()
        for p in pkg_dir.rglob(basename)
        if p.is_file()
    )
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise OSAImportError(
            f"workflow.osw {field_name}={raw_ref!r} could not be resolved uniquely in "
            f"{pkg_dir}: multiple files named {basename!r} found ({matches})"
        )
    raise OSAImportError(
        f"workflow.osw {field_name}={raw_ref!r} does not exist in package {pkg_dir}"
    )


def _resolve_seed_in_package(
    osw: dict[str, Any], pkg_dir: Path, seed: str, source_root: Path | None
) -> str | None:
    """Pick the seed the way :func:`resolve_seed_osm` does (OSW ``file_paths`` precedence)."""
    if Path(seed).is_absolute():
        return None
    root = pkg_dir.resolve()
    entries = osw.get("file_paths")
    fpaths = (
        _normalize_dir_entries(pkg_dir, entries, source_root) if isinstance(entries, list) else []
    )
    for cand in seed_file_candidates(root, seed, fpaths):
        if cand.is_file() and cand.is_relative_to(root):
            return cand.relative_to(root).as_posix()
    return None


def _normalize_dir_entries(
    pkg_dir: Path, entries: list[Any], source_root: Path | None = None
) -> list[str]:
    """Return relative directory paths from *entries* that resolve inside *pkg_dir*."""
    pkg_resolved = pkg_dir.resolve()
    src_resolved = source_root.resolve() if source_root is not None else None
    normalized: list[str] = []
    for entry in entries:
        if not isinstance(entry, str):
            continue
        entry_path = Path(entry)
        if entry_path.is_absolute():
            rel: Path | None = None
            for root in (pkg_resolved, src_resolved):
                if root is None:
                    continue
                try:
                    rel = entry_path.resolve().relative_to(root)
                    break
                except ValueError:
                    continue
            if rel is None:
                continue
            cand = (pkg_dir / rel).resolve()
            if cand.is_dir():
                normalized.append(cand.relative_to(pkg_resolved).as_posix())
            continue
        cand = (pkg_dir / entry).resolve()
        try:
            rel = cand.relative_to(pkg_resolved)
        except ValueError:
            continue
        if cand.is_dir():
            normalized.append(rel.as_posix())
    return normalized


def _resolve_step_measure_dirs(
    pkg_dir: Path,
    steps: list[Any],
    existing_mpaths: list[str],
) -> list[str]:
    """Verify every step's ``measure_dir_name`` resolves under *existing_mpaths* or discover it."""
    pkg_resolved = pkg_dir.resolve()
    resolved_mpaths = list(existing_mpaths)
    for step in steps:
        if not isinstance(step, dict):
            continue
        mdir = step.get("measure_dir_name")
        if not isinstance(mdir, str) or not mdir.strip():
            continue
        mdir = mdir.strip()
        if any((pkg_dir / mp / mdir).is_dir() for mp in resolved_mpaths):
            continue
        if (pkg_dir / "measures" / mdir).is_dir():
            if "measures" not in resolved_mpaths:
                resolved_mpaths.append("measures")
            continue
        matches = sorted(
            p.parent.resolve().relative_to(pkg_resolved).as_posix()
            for p in pkg_dir.rglob(mdir)
            if p.is_dir() and p.name == mdir
        )
        if len(matches) == 1:
            if matches[0] not in resolved_mpaths:
                resolved_mpaths.append(matches[0])
        elif len(matches) > 1:
            raise OSAImportError(
                f"workflow.osw step measure {mdir!r} is ambiguous in {pkg_dir}: "
                f"found under {matches}"
            )
        else:
            raise OSAImportError(
                f"workflow.osw could not resolve step measure {mdir!r} in {pkg_dir}"
            )
    return resolved_mpaths


def _normalize_osw_file_paths(
    osw: dict[str, Any], pkg_dir: Path, source_root: Path | None = None
) -> None:
    """Normalize ``file_paths`` in *osw* so all referenced asset directories are included."""
    file_paths = osw.get("file_paths")
    if not isinstance(file_paths, list):
        return
    normalized_fpaths = _normalize_dir_entries(pkg_dir, file_paths, source_root=source_root)
    for field in ("seed_file", "weather_file"):
        fval = osw.get(field)
        if isinstance(fval, str) and "/" in fval:
            parent_rel = Path(fval).parent.as_posix()
            if parent_rel and parent_rel != "." and parent_rel not in normalized_fpaths:
                normalized_fpaths.append(parent_rel)
    for fallback in ("weather", "seeds", "seed", "seed_models", "files"):
        if (pkg_dir / fallback).is_dir() and fallback not in normalized_fpaths:
            normalized_fpaths.append(fallback)
    if "." not in normalized_fpaths:
        normalized_fpaths.append(".")
    osw["file_paths"] = normalized_fpaths


def _normalize_osw_paths(
    osw: dict[str, Any], pkg_dir: Path, source_root: Path | None = None
) -> None:
    """Rewrite parent-relative or external OSW paths to resolve inside *pkg_dir*."""
    seed_file = osw.get("seed_file")
    if isinstance(seed_file, str) and seed_file.strip():
        osw["seed_file"] = _resolve_seed_in_package(
            osw,
            pkg_dir,
            seed_file.strip(),
            source_root,
        ) or _resolve_file_in_package(
            pkg_dir,
            seed_file.strip(),
            ("", "files", "seeds", "seed", "seed_models", "models"),
            "seed_file",
            source_root=source_root,
        )

    weather_file = osw.get("weather_file")
    if isinstance(weather_file, str) and weather_file.strip():
        osw["weather_file"] = _resolve_file_in_package(
            pkg_dir,
            weather_file.strip(),
            ("", "weather", "epw"),
            "weather_file",
            source_root=source_root,
        )

    measures_dir = pkg_dir / "measures"
    measure_paths = osw.get("measure_paths")
    steps = osw.get("steps")
    if isinstance(measure_paths, list):
        normalized_mpaths = _normalize_dir_entries(pkg_dir, measure_paths, source_root=source_root)
        had_escaped_mpath = len(normalized_mpaths) < len(
            [m for m in measure_paths if isinstance(m, str)]
        )
        if not normalized_mpaths and measures_dir.is_dir():
            normalized_mpaths.append("measures")
        if had_escaped_mpath and isinstance(steps, list):
            normalized_mpaths = _resolve_step_measure_dirs(pkg_dir, steps, normalized_mpaths)
        osw["measure_paths"] = list(dict.fromkeys(normalized_mpaths))
    elif measures_dir.is_dir():
        osw["measure_paths"] = ["measures"]

    _normalize_osw_file_paths(osw, pkg_dir, source_root=source_root)


def _extract_file_name_field(raw_val: Any) -> str | None:
    """Extract a filename string from either ``{"file_name": "..."}`` or ``"..."``."""
    if isinstance(raw_val, dict) and isinstance(raw_val.get("file_name"), str):
        val = raw_val["file_name"].strip()
        return val or None
    if isinstance(raw_val, str) and raw_val.strip():
        return raw_val.strip()
    return None


def _build_osw_from_osa(osa_data: dict[str, Any]) -> dict[str, Any]:
    """Synthesise a ``workflow.osw`` dict from *osa_data* when none exists on disk."""
    osw: dict[str, Any] = {}
    seed_name = _extract_file_name_field(osa_data.get("seed"))
    if seed_name:
        osw["seed_file"] = seed_name

    weather_name = _extract_file_name_field(osa_data.get("weather_file"))
    if weather_name:
        osw["weather_file"] = weather_name

    steps: list[dict[str, Any]] = []
    problem = osa_data.get("problem", {})
    workflow = problem.get("workflow", []) if isinstance(problem, dict) else []
    if isinstance(workflow, list):
        for wf_step in workflow:
            if not isinstance(wf_step, dict):
                continue
            m_name = _measure_step_name(wf_step)
            if not m_name:
                continue
            osw_step: dict[str, Any] = {
                "measure_dir_name": m_name,
                "arguments": _extract_step_static_arguments(wf_step),
            }
            if isinstance(wf_step.get("name"), str):
                osw_step["name"] = wf_step["name"]
            steps.append(osw_step)
    osw["steps"] = steps
    return osw


def _apply_step_static_arguments(
    osw_step: dict[str, Any],
    measure_name: str,
    static_args: dict[str, Any],
    varied_pairs: set[tuple[str, str]],
    instance_name: str | None = None,
) -> None:
    """Overlay non-varied *static_args* onto *osw_step* with type coercion."""
    step_args = osw_step.setdefault("arguments", {})
    if not isinstance(step_args, dict):
        step_args = {}
        osw_step["arguments"] = step_args
    for arg_name, val in static_args.items():
        if (measure_name, arg_name) in varied_pairs or (
            instance_name is not None and (instance_name, arg_name) in varied_pairs
        ):
            continue
        step_args[arg_name] = _coerce_like(step_args.get(arg_name), val)


def _find_matching_osw_steps(
    steps: list[Any],
    m_name: str,
    wf_step_name: Any,
    *,
    multi_instance: bool = False,
    occurrence: int = 0,
) -> list[dict[str, Any]]:
    """Return existing OSW steps matching *m_name* for one OSA workflow step.

    With several OSW steps (or a multi-instance OSA workflow) sharing *m_name*,
    match by step ``"name"`` first; otherwise pair the *occurrence*-th OSA step
    with the *occurrence*-th OSW step so instances never overwrite each other.
    """
    matching = [s for s in steps if isinstance(s, dict) and s.get("measure_dir_name") == m_name]
    if not (multi_instance or len(matching) > 1):
        return matching
    if isinstance(wf_step_name, str) and wf_step_name:
        named = [s for s in matching if s.get("name") == wf_step_name]
        if named:
            return named
    if multi_instance:
        unclaimed = [s for s in matching if not s.get("name")]
        pool = unclaimed if len(unclaimed) > occurrence else []
        return [pool[occurrence]] if pool else []
    return matching


def _overlay_osa_workflow_onto_osw(
    osw: dict[str, Any],
    osa_data: dict[str, Any],
    varied_pairs: set[tuple[str, str]],
) -> None:
    """Overlay static step arguments and seed/weather references from *osa_data* onto *osw*."""
    if not osw.get("seed_file"):
        seed_name = _extract_file_name_field(osa_data.get("seed"))
        if seed_name:
            osw["seed_file"] = seed_name

    if not osw.get("weather_file"):
        weather_name = _extract_file_name_field(osa_data.get("weather_file"))
        if weather_name:
            osw["weather_file"] = weather_name

    problem = osa_data.get("problem", {})
    workflow = problem.get("workflow", []) if isinstance(problem, dict) else []
    if not isinstance(workflow, list) or not workflow:
        return

    steps = osw.get("steps")
    if not isinstance(steps, list):
        steps = []
        osw["steps"] = steps

    dir_counts = _dir_instance_counts(workflow)
    seen: dict[str, int] = {}
    for wf_step in workflow:
        if not isinstance(wf_step, dict):
            continue
        m_name = _measure_step_name(wf_step)
        if not m_name:
            continue
        static_args = _extract_step_static_arguments(wf_step)
        wf_step_name = wf_step.get("name")
        occurrence = seen.get(m_name, 0)
        seen[m_name] = occurrence + 1
        instance = _step_instance_name(wf_step, dir_counts)
        matching_steps = _find_matching_osw_steps(
            steps,
            m_name,
            wf_step_name,
            multi_instance=dir_counts[m_name] > 1,
            occurrence=occurrence,
        )
        if not matching_steps:
            filtered_args = {
                k: v
                for k, v in static_args.items()
                if (m_name, k) not in varied_pairs and (instance, k) not in varied_pairs
            }
            new_step: dict[str, Any] = {
                "measure_dir_name": m_name,
                "arguments": filtered_args,
            }
            if isinstance(wf_step_name, str) and wf_step_name:
                new_step["name"] = wf_step_name
            steps.append(new_step)
            continue

        for osw_step in matching_steps:
            _apply_step_static_arguments(osw_step, m_name, static_args, varied_pairs, instance)


def stage_osa_template_package(
    osa_path: Path,
    package_output: Path,
    *,
    template_package: Path | None = None,
    osa_data: dict[str, Any] | None = None,
) -> Path:
    """Stage a ready-to-run template simulation package from an OSA file and/or seed package.

    Copies *template_package* (if provided) and/or extracts the ``.osa`` ZIP
    archive (when *osa_path* is a ZIP archive) into *package_output*, overlays
    static measure arguments from the OSA workflow definition onto
    ``workflow.osw``, normalises ``seed_file`` / ``weather_file`` /
    ``measure_paths`` / ``file_paths`` so they resolve inside *package_output*.
    Measure arguments are never rewritten beyond the OSA overlay.
    """
    osa_path = Path(osa_path)
    package_output = Path(package_output)
    is_zip = osa_path.exists() and (
        osa_path.suffix.lower() == ".osa" or zipfile.is_zipfile(osa_path)
    )

    if template_package is not None:
        template_package = Path(template_package)
        if not template_package.is_dir():
            raise OSAImportError(f"Template package directory not found: {template_package}")
        _copy_package_tree(template_package, package_output)
        if is_zip:
            _extract_osa_zip(osa_path, package_output)
    elif is_zip:
        if package_output.exists():
            shutil.rmtree(package_output)
        _extract_osa_zip(osa_path, package_output)
    else:
        raise OSAImportError(
            "Staging a template package from a plain JSON analysis file requires "
            "a source --template-package directory"
        )

    parsed_osa = osa_data if osa_data is not None else parse_osa(osa_path)
    varied_pairs = _collect_varied_measure_args(parsed_osa)

    canonical_osw = package_output / "workflow.osw"
    source_osw = canonical_osw
    if not source_osw.is_file():
        alt_osws = sorted(package_output.glob("*.osw"))
        if len(alt_osws) == 1:
            source_osw = alt_osws[0]

    if source_osw.is_file():
        try:
            osw_raw = json.loads(source_osw.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise OSAImportError(f"Invalid JSON in {source_osw}: {exc}") from exc
        if not isinstance(osw_raw, dict):
            raise OSAImportError(f"{source_osw} must contain a JSON object")
        _overlay_osa_workflow_onto_osw(osw_raw, parsed_osa, varied_pairs)
    else:
        osw_raw = _build_osw_from_osa(parsed_osa)
    # Always emit the canonical workflow.osw that validate_template_package requires.
    osw_path = canonical_osw

    _normalize_osw_paths(osw_raw, package_output, source_root=template_package)
    osw_path.write_text(json.dumps(osw_raw, indent=2) + "\n", encoding="utf-8")
    log.info("Staged template simulation package to %s", package_output)
    return package_output
