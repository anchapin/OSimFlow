"""OpenStudio Analysis (.osa / analysis.json) import converter.

Converts OSA variable definitions and distributions to OSimFlow's
``variables.yml`` schema so that users migrating from the OpenStudio
Analysis Spreadsheet / Parametric Analysis Tool can run their studies
through ``osimflow run``.

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

============  ====================  ============================
OSA type      OSimFlow distribution Parameters
============  ====================  ============================
uniform       uniform               min, max
normal        normal                mean, sigma
lognormal     lognormal             mean, sigma
triangular    triangular            min, max, mode (optional)
discrete      discrete              values
categorical   categorical           values, mapping (optional)
============  ====================  ============================
"""

import json
import logging
import zipfile
from collections.abc import Callable
from pathlib import Path

import yaml

log = logging.getLogger("osimflow.importers.osa")

OSA_DISTRIBUTION_MAP: dict[str, str] = {
    "uniform": "uniform",
    "normal": "normal",
    "lognormal": "lognormal",
    "lognormal_uncertain": "lognormal",
    "triangular": "triangular",
    "discrete": "discrete",
    "categorical": "categorical",
    "enum": "categorical",
}


class OSAImportError(Exception):
    """Raised when an OSA file cannot be parsed or converted."""


def _extract_analysis_data(raw: dict) -> dict:
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


def _require_keys(osa_dist: dict, keys: list[str], label: str) -> None:
    for key in keys:
        if osa_dist.get(key) is None:
            raise OSAImportError(f"{label} distribution requires '{key}'")


def _map_uniform(osa_dist: dict) -> dict:
    _require_keys(osa_dist, ["minimum", "maximum"], "Uniform")
    return {"min": float(osa_dist["minimum"]), "max": float(osa_dist["maximum"])}


def _map_normal(osa_dist: dict) -> dict:
    _require_keys(osa_dist, ["mean"], "Normal")
    sigma = osa_dist.get("stddev") or osa_dist.get("sigma")
    if sigma is None:
        raise OSAImportError("Normal distribution requires 'stddev' (or 'sigma')")
    return {"mean": float(osa_dist["mean"]), "sigma": float(sigma)}


def _map_lognormal(osa_dist: dict) -> dict:
    _require_keys(osa_dist, ["mean"], "Lognormal")
    sigma = osa_dist.get("stddev") or osa_dist.get("sigma")
    if sigma is None:
        raise OSAImportError("Lognormal distribution requires 'stddev' (or 'sigma')")
    return {"mean": float(osa_dist["mean"]), "sigma": float(sigma)}


def _map_triangular(osa_dist: dict) -> dict:
    _require_keys(osa_dist, ["minimum", "maximum"], "Triangular")
    result: dict = {"min": float(osa_dist["minimum"]), "max": float(osa_dist["maximum"])}
    mode = osa_dist.get("mode") or osa_dist.get("peak")
    if mode is not None:
        result["mode"] = float(mode)
    return result


def _map_discrete_or_categorical(osimflow_type: str, osa_dist: dict) -> dict:
    values = osa_dist.get("values") or osa_dist.get("discrete_values")
    if not values or not isinstance(values, list):
        raise OSAImportError(
            f"{osimflow_type} distribution requires a non-empty 'values' list"
        )
    result: dict = {"values": values}
    mapping = osa_dist.get("mapping")
    if mapping and isinstance(mapping, dict):
        result["mapping"] = mapping
    return result


_DISTRIBUTION_MAPPERS: dict[str, Callable[[dict], dict]] = {
    "uniform": _map_uniform,
    "normal": _map_normal,
    "lognormal": _map_lognormal,
    "triangular": _map_triangular,
}


def _map_distribution(osa_dist: dict) -> dict:
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

    result: dict = {"distribution": osimflow_type}

    if osimflow_type in ("discrete", "categorical"):
        result.update(_map_discrete_or_categorical(osimflow_type, osa_dist))
    else:
        mapper = _DISTRIBUTION_MAPPERS.get(osimflow_type)
        if mapper is not None:
            result.update(mapper(osa_dist))

    return result


def _convert_variable(osa_var: dict, index: int) -> tuple[dict, list[str]]:
    """Try to convert a single OSA variable; return (entry, warnings)."""
    warnings: list[str] = []
    if not isinstance(osa_var, dict):
        return {}, [f"Variable #{index}: skipped (not a dict)"]

    name = osa_var.get("name") or osa_var.get("display_name") or osa_var.get("uuid")
    if not name:
        return {}, [f"Variable #{index}: skipped (no name)"]

    osa_dist = osa_var.get("distribution")
    if not osa_dist or not isinstance(osa_dist, dict):
        return {}, [f"Variable {name!r}: skipped (no distribution)"]

    try:
        dist_entry = _map_distribution(osa_dist)
    except OSAImportError as exc:
        return {}, [f"Variable {name!r}: {exc}"]

    entry: dict = {"name": name}
    entry.update(dist_entry)

    measure_ref = _resolve_measure_argument(osa_var)
    if measure_ref:
        entry["measure_argument"] = measure_ref

    display_name = osa_var.get("display_name")
    if display_name and display_name != name:
        entry["display_name"] = display_name

    return entry, warnings


def _warn_unsupported_algorithm(algorithm: dict) -> None:
    algo_type = algorithm.get("type", "").lower()
    if algo_type not in ("lhs", "latin_hypercube", ""):
        log.warning(
            "OSA algorithm settings not supported by OSimFlow LHS sampler: %r. "
            "These will be ignored; use --n_samples on the command line.",
            algo_type,
        )


def _resolve_measure_argument(osa_var: dict) -> str | None:
    """Build the ``measure_name.argument_name`` dotted reference.

    Returns ``None`` if the variable has no measure mapping (e.g. it is
    a model-level variable rather than a measure argument).
    """
    measure = osa_var.get("measure")
    if not measure or not isinstance(measure, dict):
        return None
    measure_name = measure.get("display_name") or measure.get("name")
    argument = measure.get("argument") or measure.get("argument_name")
    if not measure_name or not argument:
        return None
    return f"{measure_name}.{argument}"


def parse_osa(osa_path: Path) -> dict:
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
                analysis_name = None
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


def parse_analysis_json(analysis_path: Path) -> dict:
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


def osa_to_variables_yml(osa_data: dict, output_path: Path) -> None:
    """Convert parsed OSA data to an OSimFlow ``variables.yml`` file.

    Parameters
    ----------
    osa_data : dict
        Normalised analysis data (as returned by :func:`parse_osa`).
    output_path : Path
        Where to write the resulting ``variables.yml``.

    Raises
    ------
    OSAImportError
        If no variables are found or a distribution cannot be mapped.
    """
    problem = osa_data.get("problem", {})
    if not isinstance(problem, dict):
        raise OSAImportError("'problem' is not a dict")

    osa_variables = problem.get("variables", [])
    if not osa_variables:
        raise OSAImportError("No variables found in OSA problem definition")

    warnings: list[str] = []
    converted: list[dict] = []

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

    algorithm = problem.get("algorithm", {})
    if isinstance(algorithm, dict):
        _warn_unsupported_algorithm(algorithm)

    variables_yml: dict = {"variables": converted}
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as f:
        yaml.dump(variables_yml, f, default_flow_style=False, sort_keys=False)

    log.info("Wrote %d variables to %s", len(converted), output_path)
