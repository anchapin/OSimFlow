"""Pure logic for applying a parameter set to a template simulation package.

This module is the *importable* core of `bin/apply_params_to_model.py`.
The CLI entry point is responsible for wrapping `import openstudio` in a
try/except (per AGENTS.md §6), but the logic itself is import-safe so it
can be unit-tested on hosts that do NOT have the OpenStudio Python
bindings installed.

The BYOS (Bring Your Own Script) contract is handled by
``osimflow.byos.load_user_function``, which is the single canonical
loader. Users supply a ``.py`` file with an ``apply_parameters``
function (the convention from ``__main__.py``). The Campaign consumes
``CampaignConfig.custom_apply_script`` when no explicit ``apply_fn``
is passed to the constructor. See issue #36 for the unification rationale.

Template conventions
-------------------
The .osw (OpenStudio Workflow) file format is JSON. Parsing it requires
no bindings.

The .osm (OpenStudio Model) file format is XML in production. The
OpenStudio Python bindings parse it. **For unit testing** we use the
convention that an .osm file starting with ``{`` is treated as a JSON
attribute map. This lets the same pre-flight + apply logic run on hosts
that do not have the OpenStudio bindings installed. The CLI entry point
attempts the real OpenStudio path first and falls back to this JSON path
with a clear log message.

PRD references
--------------
§1.4 — *Pre-flight Parameter Applicability Validation*: every LHS
       variable must map to a real measure argument or .osm attribute
       before the simulation starts. We enforce this here.
"""

from __future__ import annotations

import difflib
import importlib
import importlib.util
import json
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger("osimflow.apply_params")

# Reserved key injected by the Campaign when a parameter targets
# ``target: epw_file``.  The apply logic extracts this key before the
# pre-flight check (because it does not correspond to a measure argument
# or .osm attribute) and applies it as a top-level ``weather_file``
# mutation on the .osw after all normal mutations are complete.
EPW_FILE_KEY = "__epw_file__"


# ---------------------------------------------------------------------------
# Exception hierarchy
# ---------------------------------------------------------------------------
class OpenStudioBindingsMissingError(RuntimeError):
    """Raised when an operation requires the OpenStudio Python bindings
    but they are not installed on this host.

    Distinct from :class:`NotImplementedError`: that one signals a code
    path that has not been written yet even on hosts WITH the bindings
    (e.g. the production .osm attribute index). The CLI surfaces each
    with a different log message so the user can act accordingly.
    """


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class MappedParameter:
    """A parameter that maps to something in the template.

    Attributes:
        name: the parameter name (as it appears in the LHS dict).
        kind: "attribute" (from .osm) or "measure_argument" (from .osw).
        default: the value present in the template, if any.
        step_index: for .osw arguments, which step they live in (0-based).
            Resolved by matching the ``measure_dir_name`` field in the
            .osw step object. ``None`` for .osm attributes.
        measure_name: for .osw arguments, the ``measure_dir_name`` of
            the step this argument belongs to.  ``None`` for .osm
            attributes.
        object_type: for .osm attributes that use dotted notation
            (e.g. ``SpaceType_Office.lighting_power_density``), the
            OpenStudio object type (e.g. ``"SpaceType"``).
            ``None`` for simple attribute names and .osw arguments.
        object_name: for .osm attributes that use dotted notation,
            the name of the specific object instance (e.g. ``"Office"``).
            ``None`` for simple attribute names and .osw arguments.
    """

    name: str
    kind: str  # "attribute" | "measure_argument"
    default: Any = None
    step_index: int | None = None
    measure_name: str | None = None
    object_type: str | None = None
    object_name: str | None = None


class UnmappedParameterError(ValueError):
    """Raised when one or more LHS variables do not map to the template.

    The error message lists every unmapped name so the user can fix
    variables.yml in one pass.
    """


class AmbiguousParameterError(ValueError):
    """Raised when a plain argument name matches multiple measures.

    The error message lists which measures share the argument name and
    suggests the dotted ``MeasureName.argument_name`` form.
    """


class CrossMeasureConflictError(ValueError):
    """Raised when the same argument is specified for multiple measures.

    When a user specifies both ``MeasureA.argument`` and ``MeasureB.argument``
    (where both measures have an argument with the same name), this creates
    a potential runtime conflict because both measures will receive different
    values for what is effectively the same semantic parameter.

    The error message lists which measures share the conflicting argument
    and suggests using only one dotted specification.
    """


class OSMAttributeError(ValueError):
    """Raised when a dotted .osm attribute path cannot be resolved."""


# ---------------------------------------------------------------------------
# Dotted name parsing for .osm attributes
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class DottedName:
    """Parsed components of a dotted .osm variable name.

    Supports two forms:
      * Simple: ``"lighting_power_density"`` → attribute only.
      * Dotted: ``"SpaceType_Office.lighting_power_density"``
        → object_type + object_name + attribute.

    Attributes:
        object_type: the OpenStudio IDD object type (e.g. ``"SpaceType"``).
            ``None`` for simple (non-dotted) names.
        object_name: the name of the specific object instance
            (e.g. ``"Office"``).  ``None`` for simple names.
        attribute: the attribute/property name on the resolved object.
    """

    object_type: str | None
    object_name: str | None
    attribute: str


def parse_dotted_name(name: str) -> DottedName:
    """Parse a variable name into .osm resolution components.

    Dotted names use the format ``ObjectType_InstanceName.attribute``.
    The underscore between type and instance name is the separator within
    the object specifier; the dot separates the object from its attribute.

    Examples::

        >>> parse_dotted_name("SpaceType_Office.lighting_power_density")
        DottedName(object_type='SpaceType', object_name='Office', attribute='lighting_power_density')
        >>> parse_dotted_name("lighting_power_density")
        DottedName(object_type=None, object_name=None, attribute='lighting_power_density')

    Raises:
        OSMAttributeError: if the name has a dot but the left side does
            not contain an underscore (i.e. no ``ObjectType_InstanceName``
            pair).
    """
    if "." not in name:
        return DottedName(object_type=None, object_name=None, attribute=name)
    parts = name.split(".", 1)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise OSMAttributeError(
            f"Invalid dotted .osm attribute name {name!r}: expected "
            f"'ObjectType_InstanceName.attribute'"
        )
    object_spec = parts[0]
    attribute = parts[1]
    if "_" not in object_spec:
        raise OSMAttributeError(
            f"Invalid dotted .osm attribute name {name!r}: the object "
            f"specifier '{object_spec}' must contain an underscore to "
            f"separate ObjectType from InstanceName (e.g. 'SpaceType_Office')"
        )
    obj_parts = object_spec.split("_", 1)
    return DottedName(
        object_type=obj_parts[0],
        object_name=obj_parts[1],
        attribute=attribute,
    )


# ---------------------------------------------------------------------------
# Template type detection
# ---------------------------------------------------------------------------
def detect_template_type(template: Path) -> str:
    """Return ``"osm"`` or ``"osw"`` based on the file extension.

    Raises ValueError for any other extension. We do not peek at content
    here so the error message is clear when the user passes the wrong file.
    """
    suffix = template.suffix.lower()
    if suffix == ".osm":
        return "osm"
    if suffix == ".osw":
        return "osw"
    raise ValueError(f"Unsupported template type: {suffix!r} (expected .osm or .osw)")


def _resolve_dir_template(template: Path) -> tuple[Path | None, Path | None]:
    """Single source of truth for "which files are in this template directory?".

    Returns a ``(osw, osm)`` tuple where each element is the resolved
    file path or ``None`` if the corresponding file is absent. Callers
    decide what to do with the result (raise, ignore the missing one,
    use both, etc.).

    The .osw (workflow) takes precedence over the .osm (model) because
    the .osw references the model + measures, so it is the more
    complete entry point. This is the canonical precedence; every
    other template-resolution helper defers to this one.
    """
    if not template.is_dir():
        raise ValueError(f"_resolve_dir_template requires a directory, got {template}")
    osw_path = template / "workflow.osw"
    osm_path = template / "model.osm"
    return (
        osw_path if osw_path.is_file() else None,
        osm_path if osm_path.is_file() else None,
    )


def _require_dir_template_files(template: Path) -> tuple[Path | None, Path | None]:
    """Return the (osw, osm) paths for a directory, raising if neither exists.

    A directory is considered a valid template only if it contains at
    least one of ``workflow.osw`` or ``model.osm``. Missing both is a
    configuration error and must surface as a clear ``ValueError`` so
    pre-flight does not silently produce an empty mapping.

    Each returned element is the resolved file path or ``None`` if
    that specific file is absent (but at least one will be non-None,
    per the check above).
    """
    osw_path, osm_path = _resolve_dir_template(template)
    if osw_path is None and osm_path is None:
        raise ValueError(
            f"Template directory {template} contains neither workflow.osw nor model.osm"
        )
    return osw_path, osm_path


def resolve_template_file(template: Path) -> Path:
    """Resolve a template argument to a single .osm or .osw file.

    Accepts either:
      * a path to a single .osm or .osw file
      * a directory containing ``workflow.osw`` (preferred) or
        ``model.osm``

    The .osw (workflow) takes precedence over the .osm (model) because
    the .osw references the model + measures, so it is the more
    complete entry point. A directory that contains neither is an
    error.
    """
    if not template.is_dir():
        return template
    osw_path, osm_path = _resolve_dir_template(template)
    if osw_path is not None:
        return osw_path
    if osm_path is not None:
        return osm_path
    raise ValueError(f"Template directory {template} contains neither workflow.osw nor model.osm")


def _build_mappings(template: Path) -> dict[str, MappedParameter]:
    """Build the union of all parameter mappings in a template.

    For a single file: returns the mappings from that file alone.
    For a directory: returns the union of mappings from BOTH
    ``model.osm`` and ``workflow.osw`` if present. A directory
    containing NEITHER raises ``ValueError`` so the pre-flight check
    is not silently bypassed by an empty mapping.
    """
    if template.is_dir():
        osw_path, osm_path = _require_dir_template_files(template)
        mappings: dict[str, MappedParameter] = {}
        # .osw first so measure_arguments are preferred on name collision.
        if osw_path is not None:
            mappings.update(parse_osw_arguments(osw_path))
        if osm_path is not None:
            mappings.update(parse_osm_attributes(osm_path))
        return mappings
    template_type = detect_template_type(template)
    if template_type == "osm":
        return parse_osm_attributes(template)
    return parse_osw_arguments(template)


def _select_template_file(template: Path) -> Path:
    """Pick the single file to mutate after the pre-flight check.

    For a single file: that file.
    For a directory: the .osw (workflow) if it exists, else the .osm
    (model). The .osw is preferred because it is the more complete
    artifact (it references the model + measures).
    """
    if not template.is_dir():
        return template
    osw_path, osm_path = _resolve_dir_template(template)
    if osw_path is not None:
        return osw_path
    if osm_path is not None:
        return osm_path
    raise ValueError(f"Template directory {template} contains neither workflow.osw nor model.osm")


# ---------------------------------------------------------------------------
# .osm parsing (test-mode JSON; in production, the OpenStudio bindings)
# ---------------------------------------------------------------------------
def parse_osm_attributes(template: Path) -> dict[str, MappedParameter]:
    """Parse an .osm file and return a name→MappedParameter map.

    In production this uses ``openstudio.openstudiomodelcore.Model`` to
    walk the model and collect settable attribute names. For unit tests,
    we support a JSON convention: if the file content starts with ``{``,
    it is treated as ``{"attributes": {name: default, ...}}``.

    The CLI entry point logs a clear message when the JSON path is taken.
    """
    text = template.read_text()
    if text.lstrip().startswith("{"):
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"Failed to parse {template} as JSON .osm representation: {exc}"
            ) from exc
        attrs = data.get("attributes", {})
        return {
            str(name): MappedParameter(name=str(name), kind="attribute", default=val)
            for name, val in attrs.items()
        }
    # Production path: openstudio bindings. We do not import at module
    # top-level (per AGENTS.md §6). Use importlib to probe availability
    # without a top-level `import openstudio` statement (which ruff
    # would flag as unused inside the function).
    if importlib.util.find_spec("openstudio") is None:
        raise OpenStudioBindingsMissingError(
            "Cannot parse a binary .osm without the OpenStudio Python "
            "bindings. Either install `openstudio` or use the test-mode "
            "JSON convention (file content must start with '{')."
        )
    return _parse_osm_production(template)


def _parse_osm_production(template: Path) -> dict[str, MappedParameter]:
    """Walk an OpenStudio model and collect settable attributes.

    Uses the OpenStudio Python bindings (lazy-imported) to load the model,
    then iterates over model objects to discover attributes that can be
    parameterized. Each discovered attribute is registered as a
    :class:`MappedParameter` with ``object_type`` and ``object_name``
    populated from the model object.

    The discovery covers common object types with well-known setters:
    ``SpaceType``, ``Construction``, ``ThermalZone``, ``Lights``,
    ``People``, and ``BuildingStory``. The attribute names follow the
    convention ``<ObjectType>_<instanceName>.<attribute>``.

    Raises:
        OpenStudioBindingsMissingError: if the bindings are not importable.
    """
    try:
        import openstudio  # noqa: PLC0415
    except ImportError as exc:
        raise OpenStudioBindingsMissingError(
            "Cannot parse a binary .osm without the OpenStudio Python "
            "bindings. Install `openstudio` on the executor host."
        ) from exc

    model = openstudio.openstudiomodelcore.Model.load(str(template))
    if model is None:
        raise ValueError(f"OpenStudio failed to load model from {template}")

    result: dict[str, MappedParameter] = {}
    # Map OpenStudio object types to their attribute discoverers.
    # Each entry is (getter_fn, [(sdk_attr, display_attr, default_val)]).
    _discover_space_types(model, result)
    _discover_thermal_zones(model, result)
    _discover_constructions(model, result)
    _discover_lights(model, result)
    _discover_people(model, result)
    return result


def _discover_space_types(model: Any, result: dict[str, MappedParameter]) -> None:
    """Discover settable attributes from SpaceType objects."""
    import openstudio  # noqa: PLC0415

    for st in openstudio.openstudiomodelcore.SpaceType.getSpaceTypes(model):
        name = st.nameString()
        if not name:
            continue
        lpd = st.lightingPowerPerFloorArea()
        if lpd.is_initialized():
            key = f"SpaceType_{name}.lighting_power_density"
            result[key] = MappedParameter(
                name=key,
                kind="attribute",
                default=lpd.get(),
                object_type="SpaceType",
                object_name=name,
            )


def _discover_thermal_zones(model: Any, result: dict[str, MappedParameter]) -> None:
    """Discover settable attributes from ThermalZone objects."""
    import openstudio  # noqa: PLC0415

    for tz in openstudio.openstudiomodelcore.ThermalZone.getThermalZones(model):
        name = tz.nameString()
        if not name:
            continue
        # Cooling setpoint
        clg = tz.coolingSetpointTemperatureSchedule()
        if clg.is_initialized():
            key = f"ThermalZone_{name}.cooling_setpoint"
            result[key] = MappedParameter(
                name=key,
                kind="attribute",
                default=None,
                object_type="ThermalZone",
                object_name=name,
            )
        # Heating setpoint
        htg = tz.heatingSetpointTemperatureSchedule()
        if htg.is_initialized():
            key = f"ThermalZone_{name}.heating_setpoint"
            result[key] = MappedParameter(
                name=key,
                kind="attribute",
                default=None,
                object_type="ThermalZone",
                object_name=name,
            )


def _discover_constructions(model: Any, result: dict[str, MappedParameter]) -> None:
    """Discover settable attributes from Construction objects."""
    import openstudio  # noqa: PLC0415

    for c in openstudio.openstudiomodelcore.Construction.getConstructions(model):
        name = c.nameString()
        if not name:
            continue
        # Thermal conductance via assembly U-factor
        uf = c.thermalConductance()
        if uf.is_initialized():
            key = f"Construction_{name}.u_value"
            result[key] = MappedParameter(
                name=key,
                kind="attribute",
                default=uf.get(),
                object_type="Construction",
                object_name=name,
            )


def _discover_lights(model: Any, result: dict[str, MappedParameter]) -> None:
    """Discover settable attributes from Lights objects."""
    import openstudio  # noqa: PLC0415

    for lt in openstudio.openstudiomodelcore.Lights.getLights(model):
        name = lt.nameString()
        if not name:
            continue
        # Lighting level (W)
        ll = lt.lightingLevel()
        if ll.is_initialized():
            key = f"Lights_{name}.lighting_level"
            result[key] = MappedParameter(
                name=key,
                kind="attribute",
                default=ll.get(),
                object_type="Lights",
                object_name=name,
            )


def _discover_people(model: Any, result: dict[str, MappedParameter]) -> None:
    """Discover settable attributes from People objects."""
    import openstudio  # noqa: PLC0415

    for p in openstudio.openstudiomodelcore.People.getPeople(model):
        name = p.nameString()
        if not name:
            continue
        # People per floor area
        ppd = p.peopleperSpaceFloorArea()
        if ppd.is_initialized():
            key = f"People_{name}.people_per_floor_area"
            result[key] = MappedParameter(
                name=key,
                kind="attribute",
                default=ppd.get(),
                object_type="People",
                object_name=name,
            )


# ---------------------------------------------------------------------------
# .osw parsing
# ---------------------------------------------------------------------------
def parse_osw_arguments(template: Path) -> dict[str, MappedParameter]:
    """Parse an .osw file and return a name→MappedParameter map for all
    measure arguments across all steps.

    The .osw format is JSON: ``{"steps": [{"measure_dir_name": "...",
    "arguments": {name: value}}]}``.

    Two key behaviours:

    1. **Dotted-name keys** — for each argument we also register the
       qualified form ``MeasureName.argument_name`` so that users can
       disambiguate via ``variables.yml``.
    2. **First-match for plain names** — if two steps expose the same
       argument name, the *first* step wins in the plain-key entry.
       Use the dotted form to target a specific measure.
    """
    try:
        data = json.loads(template.read_text())
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid .osw JSON in {template}: {exc}") from exc
    result: dict[str, MappedParameter] = {}
    for step_idx, step in enumerate(data.get("steps", [])):
        arguments = step.get("arguments", {})
        # Resolve the measure name from measure_dir_name or name field.
        measure_name = step.get("measure_dir_name") or step.get("name") or ""
        for arg_name, default in arguments.items():
            param = MappedParameter(
                name=str(arg_name),
                kind="measure_argument",
                default=default,
                step_index=step_idx,
                measure_name=str(measure_name) if measure_name else None,
            )
            # Plain name: first-match wins.
            plain_key = str(arg_name)
            if plain_key not in result:
                result[plain_key] = param
            # Dotted name: always registered (no collision possible;
            # each measure+arg pair is unique).
            if measure_name:
                dotted_key = f"{measure_name}.{arg_name}"
                result[dotted_key] = param
    return result


# ---------------------------------------------------------------------------
# Pre-flight check (PRD §1.4)
# ---------------------------------------------------------------------------
def preflight_check(
    parameters: dict[str, Any],
    mappings: dict[str, MappedParameter],
) -> None:
    """Raise if any LHS variable is not in mappings or is ambiguous.

    Two checks run in sequence:

    1. **Unmapped** — every key in *parameters* must appear in
       *mappings* (either as a plain name or a dotted name).
       For unmapped names, fuzzy-match suggestions are generated using
       :func:`difflib.get_close_matches` so the user can fix typos
       in one pass.
    2. **Ambiguous** — a plain argument name that maps to a measure
       argument but the .osw contains *multiple* measures that expose
       the same argument name is rejected.  The user must use the
       dotted ``MeasureName.argument_name`` form instead.

    The error message lists every problematic name so the user can fix
    ``variables.yml`` in one pass.
    """
    unmapped = sorted(name for name in parameters if name not in mappings)
    if unmapped:
        available = list(mappings.keys())
        lines: list[str] = [
            "PRE-FLIGHT VALIDATION FAILED:",
            "",
        ]
        for name in unmapped:
            lines.append(f"  Parameter {name!r} not found in any measure step or .osm attribute.")
            suggestions = difflib.get_close_matches(name, available, n=3, cutoff=0.6)
            if suggestions:
                lines.append(f"  Did you mean {suggestions[0]!r}?")
                if len(suggestions) > 1:
                    lines.append(
                        f"  Other similar names: {', '.join(repr(s) for s in suggestions[1:])}"
                    )
            lines.append("")
        lines.append(
            "Fix the variable names in variables.yml or extend the template to expose them."
        )
        raise UnmappedParameterError("\n".join(lines))
    # Detect ambiguous plain names.
    # An ambiguous name is a *plain* (non-dotted) parameter key whose
    # mapping is a measure_argument AND whose argument name appears in
    # more than one step's arguments in the same .osw. The caller must
    # disambiguate by switching to the dotted form.
    _check_ambiguous_parameters(parameters, mappings)
    # Detect cross-measure conflicts: when the user specifies the same
    # argument via two different measures (e.g. both
    # SetThermostatSchedule.heating_setpoint AND
    # SetEnvelopePerformance.heating_setpoint). Both are valid individually
    # but together they represent a potential runtime conflict.
    _check_cross_measure_conflicts(parameters, mappings)


def _check_ambiguous_parameters(
    parameters: dict[str, Any],
    mappings: dict[str, MappedParameter],
) -> None:
    """Detect plain argument names that are ambiguous across measures.

    Builds a reverse index from *all* dotted keys in mappings to discover
    which plain argument names appear in multiple measures. A plain
    argument name is ambiguous if it has dotted keys pointing to >1
    distinct measure_name.

    Only checks keys that appear in *parameters* — we don't warn about
    names the user isn't actually varying.
    """
    # Collect which measure_names carry each plain argument name.
    # We scan ALL dotted keys (not plain keys) because plain keys were
    # first-match-wins — only dotted keys expose every (measure, arg) pair.
    arg_to_measures: dict[str, set[str]] = {}
    for key, mapping in mappings.items():
        if mapping.kind != "measure_argument":
            continue
        if mapping.measure_name is None:
            continue
        # Only consider dotted keys — they carry the full (measure, arg)
        # pair. Plain keys are first-match-wins and don't reveal the
        # collision.
        if "." not in key:
            continue
        # Extract the plain argument name from "MeasureName.arg"
        _, _, plain_arg = key.rpartition(".")
        if plain_arg:
            arg_to_measures.setdefault(plain_arg, set()).add(mapping.measure_name)

    ambiguous: list[str] = []
    for name in parameters:
        if "." in name:
            continue  # dotted form is already disambiguated
        measures = arg_to_measures.get(name)
        if measures is not None and len(measures) > 1:
            ambiguous.append(f"  {name} — shared by: {', '.join(sorted(measures))}")

    if ambiguous:
        raise AmbiguousParameterError(
            "Pre-flight check failed: the following argument names are "
            "ambiguous (they appear in multiple measures). Use the dotted "
            "form 'MeasureName.argument_name' in variables.yml to "
            "disambiguate:\n" + "\n".join(ambiguous)
        )


def _check_cross_measure_conflicts(
    parameters: dict[str, Any],
    mappings: dict[str, MappedParameter],
) -> None:
    """Detect when the same argument is specified for multiple measures.

    When a user specifies both ``MeasureA.argument`` AND
    ``MeasureB.argument`` (where both measures have an argument with the
    same name), this creates a potential runtime conflict because both
    measures will receive different values for what is effectively the
    same semantic parameter.

    Only checks DOTTED keys that appear in *parameters* — we don't warn
    about dotted names the user isn't actually varying.
    """
    # Build a map of dotted name -> (measure_name, plain_arg)
    # This lets us detect when multiple dotted names share the same
    # plain argument but target different measures.
    dotted_params: dict[str, tuple[str, str]] = {}
    for key in parameters:
        if "." not in key:
            continue  # Plain names handled by _check_ambiguous_parameters
        mapping = mappings.get(key)
        if mapping is None or mapping.kind != "measure_argument":
            continue
        if mapping.measure_name is None:
            continue
        # Extract the plain argument name from "MeasureName.arg"
        _, _, plain_arg = key.rpartition(".")
        if plain_arg:
            dotted_params[key] = (mapping.measure_name, plain_arg)

    # Group by plain argument name: arg_name -> list of (dotted_key, measure_name)
    arg_to_dotted: dict[str, list[tuple[str, str]]] = {}
    for dotted_key, (measure_name, plain_arg) in dotted_params.items():
        arg_to_dotted.setdefault(plain_arg, []).append((dotted_key, measure_name))

    conflicts: list[str] = []
    for plain_arg, dotted_list in arg_to_dotted.items():
        if len(dotted_list) > 1:
            measures = sorted(set(measure_name for _, measure_name in dotted_list))
            dotted_keys = sorted(set(dotted_key for dotted_key, _ in dotted_list))
            conflicts.append(
                f"  {plain_arg!r} specified for multiple measures: "
                f"{', '.join(measures)}. Choose ONE dotted form "
                f"(e.g. {dotted_keys[0]!r}) to avoid ambiguous runtime behavior."
            )

    if conflicts:
        import os
        if not os.environ.get("OSIMFLOW_ALLOW_CROSS_MEASURE_CONFLICT"):
            raise CrossMeasureConflictError(
                "Pre-flight check failed: the same argument is specified for "
                "multiple measures via dotted names, creating a potential runtime "
                "conflict:\n" + "\n".join(conflicts)
            )
        else:
            log.warning(
                "Bypassing cross-measure conflicts due to OSIMFLOW_ALLOW_CROSS_MEASURE_CONFLICT:\n"
                + "\n".join(conflicts)
            )


def preflight_validate_osm_paths(
    parameters: dict[str, Any],
    mappings: dict[str, MappedParameter],
    model: Any | None = None,
) -> list[str]:
    """Validate that dotted .osm attribute paths reference real model objects.

    For parameters whose mappings have ``object_type`` and ``object_name``
    set (i.e. dotted names like ``SpaceType_Office.lighting_power_density``),
    this function checks that the referenced object exists in the model.

    When ``model`` is ``None`` (bindings not available or JSON-mode .osm),
    the function is a no-op and returns an empty list — the path
    validation is only meaningful against a loaded OpenStudio model.

    Args:
        parameters: the LHS parameter dict.
        mappings: the name→MappedParameter map from ``_build_mappings``.
        model: a loaded ``openstudio.openstudiomodelcore.Model`` instance,
            or ``None`` to skip validation.

    Returns:
        A list of warning messages for paths that could not be validated
        (empty on success).

    Raises:
        OSMAttributeError: if a dotted path references an object type or
            name that does not exist in the model.
    """
    if model is None:
        return []
    try:
        import openstudio  # noqa: PLC0415
    except ImportError:
        return []

    warnings: list[str] = []
    invalid_paths: list[str] = []

    for name in parameters:
        mapping = mappings.get(name)
        if mapping is None:
            continue
        if mapping.kind != "attribute":
            continue
        if mapping.object_type is None or mapping.object_name is None:
            continue
        # Dotted path: validate that the object exists in the model.
        found = _resolve_model_object(model, openstudio, mapping.object_type, mapping.object_name)
        if found is None:
            invalid_paths.append(
                f"{mapping.object_type} '{mapping.object_name}' (from variable '{name}')"
            )

    if invalid_paths:
        raise OSMAttributeError(
            "Pre-flight .osm path validation failed: the following "
            "object references do not exist in the model: "
            + "; ".join(invalid_paths)
            + ". Check the object_type and object_name in variables.yml."
        )
    return warnings


def _resolve_model_object(
    model: Any,
    openstudio: Any,
    object_type: str,
    object_name: str,
) -> Any | None:
    """Resolve an OpenStudio model object by type and name.

    Returns the model object, or ``None`` if no matching object exists.
    """
    type_dispatch: dict[str, Any] = {
        "SpaceType": openstudio.openstudiomodelcore.SpaceType,
        "ThermalZone": openstudio.openstudiomodelcore.ThermalZone,
        "Construction": openstudio.openstudiomodelcore.Construction,
        "Lights": openstudio.openstudiomodelcore.Lights,
        "People": openstudio.openstudiomodelcore.People,
        "BuildingStory": openstudio.openstudiomodelcore.BuildingStory,
    }
    idd_type = type_dispatch.get(object_type)
    if idd_type is None:
        log.warning("Unsupported .osm object type %r — skipping validation", object_type)
        return None
    objects = idd_type.__getattribute__("get" + object_type + "s")(model)
    for obj in objects:
        if obj.nameString() == object_name:
            return obj
    return None


# ---------------------------------------------------------------------------
# Default logic: copy template to out/<sample_id>/, mutate in place
# ---------------------------------------------------------------------------
def _copy_template_artifacts(template: Path, out: Path) -> list[Path]:
    """Copy all template artifacts into a clean per-sample output directory.

    If ``template`` is a single .osm/.osw file, copy just that file
    into ``out/<template.name>``. If it is a directory, recursively
    copy the whole directory contents (without the dir itself) into
    ``out/``.

    The ``out`` directory is ALWAYS cleaned before copying. This
    avoids a subtle failure mode where a previous run left stale or
    partial artifacts in ``out`` that the next run (or a BYOS user
    script) would silently consume. If ``out`` exists as a
    non-directory, this function raises ``ValueError``.

    Returns the list of destination paths actually created.
    """
    # Ensure `out` is a clean directory so we don't leave stale artifacts
    if out.exists():
        if out.is_dir():
            for child in out.iterdir():
                if child.is_dir():
                    shutil.rmtree(child)
                else:
                    child.unlink()
        else:
            raise ValueError(f"Output path {out} exists and is not a directory")
    out.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    if template.is_dir():
        for src in template.iterdir():
            dest = out / src.name
            if src.is_dir():
                shutil.copytree(src, dest, dirs_exist_ok=True)
            else:
                shutil.copy2(src, dest)
            written.append(dest)
    else:
        dest = out / template.name
        shutil.copy2(template, dest)
        written.append(dest)
    return written


def _mutate_osm(
    osm_path: Path,
    parameters: dict[str, Any],
    mappings: dict[str, MappedParameter],
) -> None:
    """Mutate an .osm file in place (JSON-mode or production XML).

    For JSON-mode files (starting with ``{``), applies simple attribute
    writes. For production XML .osm files, delegates to
    :func:`_mutate_osm_production` which uses the OpenStudio Python
    bindings.

    Raises:
        OpenStudioBindingsMissingError: the file is a binary/XML .osm
            and the OpenStudio Python bindings are not installed on
            this host. Install them to enable the production path.
    """
    text = osm_path.read_text()
    if not text.lstrip().startswith("{"):
        # Real OpenStudio XML .osm: distinguish "bindings missing" from
        # "feature not implemented" so the CLI can give an actionable
        # error message.
        if importlib.util.find_spec("openstudio") is None:
            raise OpenStudioBindingsMissingError(
                "Cannot mutate a binary .osm without the OpenStudio "
                "Python bindings. Install `openstudio` on the executor "
                "host, or use the test-mode JSON convention (file "
                "content must start with '{')."
            )
        _mutate_osm_production(osm_path, parameters, mappings)
        return
    # JSON-mode stub
    data = json.loads(text)
    for name, value in parameters.items():
        if name in mappings and mappings[name].kind == "attribute":
            data.setdefault("attributes", {})[name] = value
    osm_path.write_text(json.dumps(data, indent=2, sort_keys=True))


def _mutate_osm_production(
    osm_path: Path,
    parameters: dict[str, Any],
    mappings: dict[str, MappedParameter],
) -> None:
    """Mutate a production XML .osm file using the OpenStudio Python bindings.

    Loads the model via ``openstudio.openstudiomodelcore.Model.load``,
    resolves each parameter to the corresponding model object and
    attribute, applies the mutation, and saves the model back to disk.

    Variable names can be:
      * Simple: ``"lighting_power_density"`` — applies to the model
        level or the first matching attribute.
      * Dotted: ``"SpaceType_Office.lighting_power_density"`` — resolves
        to a specific model object by type and name.

    Type coercion rules:
      * ``float`` → passed directly to SDK numeric setters.
      * ``str`` → passed directly to SDK string setters.
      * ``int`` → coerced to ``float`` for numeric setters; for enum-like
        attributes that expect an integer index, the int is passed as-is.

    Raises:
        OpenStudioBindingsMissingError: if the bindings are not importable.
        OSMAttributeError: if a dotted path does not resolve to a model object.
    """
    try:
        import openstudio  # noqa: PLC0415
    except ImportError as exc:
        raise OpenStudioBindingsMissingError(
            "Cannot mutate a production .osm without the OpenStudio Python "
            "bindings. Install `openstudio` on the executor host."
        ) from exc

    model = openstudio.openstudiomodelcore.Model.load(str(osm_path))
    if model is None:
        raise ValueError(f"OpenStudio failed to load model from {osm_path}")

    for name, value in parameters.items():
        mapping = mappings.get(name)
        if mapping is None or mapping.kind != "attribute":
            continue
        _apply_osm_mutation(model, openstudio, mapping, value)

    # Save the mutated model back to the same path.
    model.save(str(osm_path), overwrite=True)
    log.info("Mutated production .osm saved to %s", osm_path)


def _apply_osm_mutation(
    model: Any,
    openstudio: Any,
    mapping: MappedParameter,
    value: Any,
) -> None:
    """Apply a single parameter mutation to an OpenStudio model.

    Resolves the target object and attribute, performs type coercion,
    and calls the appropriate SDK setter.

    Raises:
        OSMAttributeError: if the object or attribute cannot be resolved.
    """
    attribute = mapping.name
    obj: Any = model  # Default: model-level attribute

    # If dotted name, resolve to the specific model object.
    if mapping.object_type is not None and mapping.object_name is not None:
        obj = _resolve_model_object(model, openstudio, mapping.object_type, mapping.object_name)
        if obj is None:
            raise OSMAttributeError(
                f"Cannot resolve {mapping.object_type} "
                f"'{mapping.object_name}' for variable '{mapping.name}'. "
                f"The object does not exist in the model."
            )
        # For dotted names, use the parsed attribute (the part after the dot).
        parsed = parse_dotted_name(mapping.name)
        attribute = parsed.attribute

    # Coerce the value type and apply.
    coerced = _coerce_value(value)
    _set_attribute(obj, openstudio, mapping.object_type, attribute, coerced)


def _coerce_value(value: Any) -> Any:
    """Coerce a parameter value for the OpenStudio SDK.

    Rules:
      * ``int`` → ``float`` (OpenStudio numeric setters expect doubles).
      * ``float`` → ``float`` (pass-through).
      * ``str`` → ``str`` (pass-through).
      * ``bool`` → ``bool`` (pass-through).
      * Other types → raise ``TypeError``.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return float(value)
    if isinstance(value, float):
        return value
    if isinstance(value, str):
        return value
    raise TypeError(
        f"Cannot coerce parameter value of type {type(value).__name__} "
        f"for OpenStudio SDK: {value!r}"
    )


def _set_attribute(
    obj: Any,
    openstudio: Any,
    object_type: str | None,
    attribute: str,
    value: Any,
) -> None:
    """Set an attribute on a resolved OpenStudio model object.

    Dispatches to the correct SDK setter based on the object type and
    attribute name. Falls back to ``obj.setString`` for unknown attributes.

    Raises:
        OSMAttributeError: if the setter fails or the attribute is
            not recognized.
    """
    setter_map: dict[str, dict[str, Any]] = {
        "SpaceType": {
            "lighting_power_density": lambda o, v: o.setLightingPowerPerFloorArea(v),
        },
        "ThermalZone": {
            "cooling_setpoint": lambda o, v: _set_schedule_constant(o, openstudio, v, "cooling"),
            "heating_setpoint": lambda o, v: _set_schedule_constant(o, openstudio, v, "heating"),
        },
        "Construction": {
            "u_value": lambda o, v: o.setThermalConductance(v),
        },
        "Lights": {
            "lighting_level": lambda o, v: o.setLightingLevel(v),
        },
        "People": {
            "people_per_floor_area": lambda o, v: o.setPeopleperSpaceFloorArea(v),
        },
    }

    if object_type is not None and object_type in setter_map:
        attr_map = setter_map[object_type]
        setter = attr_map.get(attribute)
        if setter is not None:
            try:
                setter(obj, value)
            except Exception as exc:
                raise OSMAttributeError(
                    f"Failed to set {object_type}.{attribute}={value!r}: {exc}"
                ) from exc
            return

    # Fallback: try setString (generic IDD attribute).
    if isinstance(value, str):
        obj.setString(attribute, value)
    elif isinstance(value, (int, float)):
        obj.setString(attribute, str(value))
    else:
        raise OSMAttributeError(
            f"No setter for {object_type}.{attribute} with value type "
            f"{type(value).__name__}. Define an explicit setter in "
            f"osimflow/apply_params.py."
        )


def _set_schedule_constant(
    zone: Any,
    openstudio: Any,
    value: float,
    kind: str,
) -> None:
    """Set a constant temperature schedule on a ThermalZone.

    Creates a :class:`ScheduleConstant` and assigns it as the cooling
    or heating setpoint schedule on the zone.

    Args:
        zone: the ThermalZone model object.
        openstudio: the openstudio module.
        value: the temperature value (°C).
        kind: ``"cooling"`` or ``"heating"``.
    """
    schedule = openstudio.openstudiomodelcore.ScheduleConstant(zone.model())
    schedule.setValue(value)
    if kind == "cooling":
        zone.setCoolingSetpointTemperatureSchedule(schedule)
    else:
        zone.setHeatingSetpointTemperatureSchedule(schedule)


def mutate_osw_weather_file(osw_path: Path, epw_path: str) -> None:
    """Update the ``weather_file`` field in an .osw JSON file.

    This is the core mutation for the ``target: epw_file`` feature
    (issue #55).  The ``weather_file`` field is a top-level string in
    the .osw format that points to the ``.epw`` file the simulation
    should use.

    Args:
        osw_path: path to the ``.osw`` file to mutate (in place).
        epw_path: the weather file path to write.  This is a
            *relative* path resolved against the template simulation
            package directory (not an absolute path), consistent with
            how OpenStudio resolves ``weather_file``.

    Raises:
        ValueError: if the .osw file cannot be parsed as JSON.
    """
    try:
        data = json.loads(osw_path.read_text())
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid .osw JSON in {osw_path}: {exc}") from exc
    data["weather_file"] = epw_path
    osw_path.write_text(json.dumps(data, indent=2, sort_keys=True))
    log.info("mutated .osw weather_file -> %s in %s", epw_path, osw_path)


def _mutate_osw(
    osw_path: Path,
    parameters: dict[str, Any],
    mappings: dict[str, MappedParameter],
) -> None:
    """Mutate an .osw file in place.

    For each parameter, looks up the mapping to find which step and
    which argument name to write. The mapping's ``step_index`` was
    resolved during parsing by matching the ``measure_dir_name``
    field in the .osw step object, so reordering steps does not
    break the mutation. The mapping's ``name`` field holds the
    *plain* argument name (the key inside ``step["arguments"]``).
    """
    data = json.loads(osw_path.read_text())
    steps = data.setdefault("steps", [])
    for param_key, value in parameters.items():
        mapping = mappings.get(param_key)
        if mapping is None or mapping.kind != "measure_argument":
            continue
        step_index = mapping.step_index
        if step_index is None or step_index >= len(steps):
            continue
        # mapping.name is the plain argument name inside the step's
        # arguments dict (never dotted).
        steps[step_index].setdefault("arguments", {})[mapping.name] = value
    osw_path.write_text(json.dumps(data, indent=2, sort_keys=True))


# ---------------------------------------------------------------------------
# BYOS: deprecated shim (issue #36). Use osimflow.byos.load_user_function
# or pass --custom_apply_script on the CLI. The Campaign now consumes
# CampaignConfig.custom_apply_script directly; there is no second loader
# in this module.
# ---------------------------------------------------------------------------
def _load_custom_apply(script_path: Path) -> Any:  # noqa: ARG001
    """Deprecated. Use ``osimflow.byos.load_user_function`` instead.

    This function exists solely so that any code that was importing it
    directly gets a clear deprecation path instead of an ``ImportError``.
    It always raises ``DeprecationWarning`` + ``RuntimeError``.

    .. deprecated:: 0.2.0
        Removed in the fix for issue #36. The canonical BYOS loader is
        ``osimflow.byos.load_user_function``.
    """
    import warnings  # noqa: PLC0415

    warnings.warn(
        "_load_custom_apply is deprecated and no longer functional. "
        "Use osimflow.byos.load_user_function or pass "
        "--custom_apply_script on the CLI. "
        "See issue #36 for details.",
        DeprecationWarning,
        stacklevel=2,
    )
    raise RuntimeError(
        "_load_custom_apply has been removed. Use osimflow.byos.load_user_function instead."
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
def apply_parameters(
    template: Path,
    parameters: dict[str, Any],
    sample_id: str,
    out: Path,
) -> Path:
    """Apply a parameter set to a template and write the per-sample output.

    Args:
        template: the template_sim_package file (.osm or .osw).
        parameters: the LHS dict for this sample, e.g. ``{"wwr": 0.4}``.
            May contain the reserved key :data:`EPW_FILE_KEY`
            (``"__epw_file__"``) when the Campaign has resolved an
            ``epw_file`` target.  This key is extracted before the
            pre-flight check (because it does not correspond to a
            measure argument or .osm attribute) and applied as a
            top-level ``weather_file`` mutation on the .osw after all
            normal mutations are complete.
        sample_id: the sample's identifier, e.g. ``"0001"``.
        out: the per-sample output directory. Created if missing.

    Returns:
        The per-sample output directory (``out``).

    Raises:
        UnmappedParameterError: at least one LHS variable does not map
            to a real template attribute or measure argument. This is
            enforced *before* any writes happen.
        OpenStudioBindingsMissingError: a binary/XML .osm was provided
            and the OpenStudio Python bindings are not installed on
            this host. The CLI surfaces this with an actionable message
            ("install openstudio or use the JSON convention").
        OSMAttributeError: a dotted .osm attribute path references an
            object type or instance name that does not exist in the
            model. Raised during pre-flight path validation.
    """
    original_template = template
    # Extract the epw_file reserved key BEFORE the pre-flight check.
    # This key is not a measure argument or .osm attribute — it is
    # a top-level .osw field handled by mutate_osw_weather_file.
    epw_file_value: str | None = parameters.pop(EPW_FILE_KEY, None)
    # Build the union of mappings from BOTH files (if directory).
    mappings = _build_mappings(template)
    # The file we will mutate (or that the user_ctx references).
    target_file = _select_template_file(template)
    template_type = detect_template_type(target_file)

    # Pre-flight (PRD §1.4).
    preflight_check(parameters, mappings)

    # Default path: copy all template artifacts into the per-sample
    # output, then mutate BOTH files (when applicable) so every
    # mapped parameter lands somewhere sensible. We mutate in two
    # passes: first the .osw (so measure arguments are applied), then
    # the .osm (so model attributes are applied). Each pass skips
    # parameters that don't apply to its file.
    _copy_template_artifacts(original_template, out)
    if original_template.is_dir():
        osw_in_out = out / "workflow.osw"
        osm_in_out = out / "model.osm"
        if osw_in_out.is_file():
            _mutate_osw(osw_in_out, parameters, mappings)
        if osm_in_out.is_file():
            _mutate_osm(osm_in_out, parameters, mappings)
    else:
        target_in_out = out / target_file.name
        if template_type == "osm":
            _mutate_osm(target_in_out, parameters, mappings)
        else:
            _mutate_osw(target_in_out, parameters, mappings)

    # Apply .epw weather file mutation AFTER normal mutations.
    # This updates the top-level ``weather_file`` field in the .osw.
    if epw_file_value is not None:
        osw_target: Path | None = None
        if original_template.is_dir():
            osw_target = out / "workflow.osw"
        elif template_type == "osw":
            osw_target = out / target_file.name
        if osw_target is not None and osw_target.is_file():
            mutate_osw_weather_file(osw_target, epw_file_value)
        else:
            log.warning(
                "epw_file target specified but no .osw found to mutate "
                "for sample_id=%s; skipping weather_file update",
                sample_id,
            )

    return out
