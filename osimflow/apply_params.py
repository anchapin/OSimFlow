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
import math
import os
import re
import shutil
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import OSimFlowRuntimeError, OSimFlowValueError

log = logging.getLogger("osimflow.apply_params")

# Try to dynamically bind openstudio.openstudiomodelcore if openstudio is present
# but openstudio.openstudiomodelcore is missing (as in macOS standalone installs).
try:
    import sys

    import openstudio  # noqa: F401

    try:
        import openstudio.openstudiomodelcore  # noqa: F401
    except ImportError:
        try:
            import openstudiomodel

            sys.modules["openstudio.openstudiomodelcore"] = openstudiomodel
            openstudio.openstudiomodelcore = openstudiomodel
        except ImportError:
            try:
                import openstudiomodelcore

                sys.modules["openstudio.openstudiomodelcore"] = openstudiomodelcore
                openstudio.openstudiomodelcore = openstudiomodelcore
            except ImportError:
                pass
except ImportError:
    pass

# Reserved key injected by the Campaign when a parameter targets
# ``target: epw_file``.  The apply logic extracts this key before the
# pre-flight check (because it does not correspond to a measure argument
# or .osm attribute) and applies it as a top-level ``weather_file``
# mutation on the .osw after all normal mutations are complete.
EPW_FILE_KEY = "__epw_file__"


# ---------------------------------------------------------------------------
# Exception hierarchy
# ---------------------------------------------------------------------------
class OpenStudioBindingsMissingError(OSimFlowRuntimeError):
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


class UnmappedParameterError(OSimFlowValueError):
    """Raised when one or more LHS variables do not map to the template.

    The error message lists every unmapped name so the user can fix
    variables.yml in one pass.
    """


class AmbiguousParameterError(OSimFlowValueError):
    """Raised when a plain argument name matches multiple measures.

    The error message lists which measures share the argument name and
    suggests the dotted ``MeasureName.argument_name`` form.
    """


class CrossMeasureConflictError(OSimFlowValueError):
    """Raised when the same argument is specified for multiple measures.

    When a user specifies both ``MeasureA.argument`` and ``MeasureB.argument``
    (where both measures have an argument with the same name), this creates
    a potential runtime conflict because both measures will receive different
    values for what is effectively the same semantic parameter.

    The error message lists which measures share the conflicting argument
    and suggests using only one dotted specification.
    """


class OSMAttributeError(OSimFlowValueError):
    """Raised when a dotted .osm attribute path cannot be resolved."""


class ArgumentCoercionError(OSimFlowValueError):
    """Raised when a value cannot be coerced to its ``measure.xml`` argument type."""


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
    osm_path = _resolve_seed_osm(template, osw_path if osw_path.is_file() else None)
    return (osw_path if osw_path.is_file() else None, osm_path)


def resolve_seed_osm(pkg: Path) -> Path | None:
    """Seed ``.osm`` of a package directory (see :func:`_resolve_seed_osm`)."""
    osw = pkg / "workflow.osw"
    return _resolve_seed_osm(pkg, osw if osw.is_file() else None)


def _resolve_seed_osm(template: Path, osw_path: Path | None) -> Path | None:
    """Find the seed ``.osm`` of a template directory.

    Order mirrors the OpenStudio CLI: ``workflow.osw`` ``seed_file`` against the
    template root, each ``file_paths`` entry, ``files/<name>`` and ``seeds/<name>``
    (all confined to the template), then ``model.osm``, then a single top-level ``*.osm``.
    """
    root = template.resolve()
    if osw_path is not None:
        try:
            data = json.loads(osw_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = None
        seed = data.get("seed_file") if isinstance(data, dict) else None
        if isinstance(seed, str) and seed.strip():
            for resolved in seed_file_candidates(root, seed.strip(), _osw_file_paths(data)):
                # Entries escaping the package are ignored: they would not be staged.
                if resolved.is_file() and resolved.is_relative_to(root):
                    return root / resolved.relative_to(root)
    default = template / "model.osm"
    if default.is_file():
        return default
    osms = sorted(template.glob("*.osm"))
    return osms[0] if len(osms) == 1 else None


def seed_file_candidates(root: Path, seed: str, file_paths: list[str]) -> list[Path]:
    """Resolved candidate locations for an OSW ``seed_file``, in OpenStudio precedence order."""
    root = root.resolve()
    name = Path(seed).name
    candidates = [root / seed, *(root / p / seed for p in file_paths)]
    candidates += [root / "files" / name, root / "seeds" / name]
    return [c.resolve() for c in candidates]


def _osw_file_paths(data: dict[str, Any]) -> list[str]:
    paths = data.get("file_paths")
    return [p for p in paths if isinstance(p, str)] if isinstance(paths, list) else []


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


_KNOWN_SIMPLE_OSM_ATTRIBUTES: frozenset[str] = frozenset(
    {
        "lighting_power_density",
        "people_per_floor_area",
        "cooling_setpoint",
        "heating_setpoint",
    }
)

_KNOWN_OSM_OBJECT_TYPES: frozenset[str] = frozenset(
    {
        "SpaceType",
        "ThermalZone",
        "Lights",
        "People",
        "Construction",
        "BuildingStory",
    }
)


def _infer_osm_attribute_mapping(name: str) -> MappedParameter | None:
    """Infer an ``attribute`` mapping for *name* when OpenStudio bindings are absent."""
    if "." not in name:
        if name in _KNOWN_SIMPLE_OSM_ATTRIBUTES:
            return MappedParameter(name=name, kind="attribute", default=None)
        return None
    try:
        dotted = parse_dotted_name(name)
    except OSMAttributeError:
        return None
    if dotted.object_type in _KNOWN_OSM_OBJECT_TYPES and dotted.object_name and dotted.attribute:
        return MappedParameter(
            name=name,
            kind="attribute",
            default=None,
            object_type=dotted.object_type,
            object_name=dotted.object_name,
        )
    return None


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


def _build_mappings_without_osm_bindings(
    template: Path,
    param_keys: Any,
) -> dict[str, MappedParameter]:
    """Build pre-flight mappings when local OpenStudio bindings are absent (issue #2024).

    Parses ``workflow.osw`` directly and recognises valid ``.osm``
    attribute variable names when ``model.osm`` is present without
    requiring ``import openstudio`` on the submit host. Unknown names
    are left out so :func:`preflight_check` still raises
    :class:`UnmappedParameterError`.
    """
    mappings: dict[str, MappedParameter] = {}
    has_osm = False
    if template.is_dir():
        osw_path, osm_path = _require_dir_template_files(template)
        if osw_path is not None:
            mappings.update(parse_osw_arguments(osw_path))
        has_osm = osm_path is not None
    else:
        template_type = detect_template_type(template)
        if template_type == "osw":
            mappings.update(parse_osw_arguments(template))
        else:
            has_osm = True

    if has_osm:
        for key in param_keys:
            if key in mappings:
                continue
            inferred = _infer_osm_attribute_mapping(str(key))
            if inferred is not None:
                mappings[str(key)] = inferred
    return mappings


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

    model_opt = openstudio.openstudiomodelcore.Model.load(str(template))
    if not model_opt.is_initialized():
        raise ValueError(f"OpenStudio failed to load model from {template}")
    model = model_opt.get()

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
    for st in model.getSpaceTypes():
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
    for tz in model.getThermalZones():
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
    for c in model.getConstructions():
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
    for lt in model.getLights():
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
    for p in model.getPeople():
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
def _parse_xml_root(xml_text: str) -> ET.Element:
    """Parse XML text using ``defusedxml.ElementTree`` when installed."""
    try:
        defused_et = importlib.import_module("defusedxml.ElementTree")
        root: ET.Element = defused_et.fromstring(xml_text)
        return root
    except ModuleNotFoundError:
        return ET.fromstring(xml_text)


def _measure_search_dirs(osw_path: Path, data: dict[str, Any]) -> list[Path]:
    """Return ordered candidate directories where measure folders may live."""
    base_dir = osw_path.parent
    candidates: list[Path] = [base_dir / "measures"]
    raw_paths = data.get("measure_paths")
    if isinstance(raw_paths, list):
        for raw in raw_paths:
            if not isinstance(raw, str) or not raw.strip():
                continue
            p = Path(raw)
            if not p.is_absolute():
                p = base_dir / p
            if p not in candidates:
                candidates.append(p)
    return candidates


def _locate_step_measure_xml(
    measure_name: str,
    search_dirs: list[Path],
) -> Path | None:
    """Return the first matching ``<dir>/<measure_name>/measure.xml`` if present."""
    if not measure_name:
        return None
    for base in search_dirs:
        xml_path = base / measure_name / "measure.xml"
        if xml_path.is_file():
            return xml_path
    return None


def _parse_measure_xml_arg_info(xml_path: Path) -> list[tuple[str, str, Any]]:
    """Return ``(name, type, default)`` tuples declared in ``measure.xml``."""
    try:
        root = _parse_xml_root(xml_path.read_text(encoding="utf-8"))
    except (OSError, ET.ParseError, ValueError):
        log.warning("Failed to parse measure.xml at %s", xml_path, exc_info=True)
        return []
    entries: list[tuple[str, str, Any]] = []
    seen: set[str] = set()
    for arg_elem in root.findall("./arguments/argument"):
        raw_name = arg_elem.findtext("name")
        if raw_name is None or not raw_name.strip():
            continue
        arg_name = raw_name.strip()
        if arg_name in seen:
            continue
        seen.add(arg_name)
        raw_type = (arg_elem.findtext("type") or "").strip().lower()
        arg_type = "Boolean" if raw_type in ("boolean", "bool") else raw_type.capitalize()
        default_elem = arg_elem.find("default_value")
        default_val: Any = default_elem.text if default_elem is not None else None
        if arg_type == "Boolean" and isinstance(default_val, str):
            lowered = default_val.strip().lower()
            if lowered == "true":
                default_val = True
            elif lowered == "false":
                default_val = False
        entries.append((arg_name, arg_type, default_val))
    return entries


def read_measure_xml_aliases(xml_path: Path) -> list[str]:
    """Return the ``<name>`` and ``<class_name>`` identifiers declared in ``measure.xml``."""
    try:
        root = _parse_xml_root(xml_path.read_text(encoding="utf-8"))
    except (OSError, ET.ParseError, ValueError):
        return []
    aliases: list[str] = []
    for tag in ("name", "class_name"):
        text = (root.findtext(tag) or "").strip()
        if text and text not in aliases:
            aliases.append(text)
    return aliases


def _register_osw_argument(
    result: dict[str, MappedParameter],
    *,
    arg_name: str,
    default: Any,
    step_idx: int,
    measure_name: str,
    aliases: tuple[str, ...] = (),
) -> None:
    """Register plain (first-match) and dotted (per-measure) OSW argument mappings.

    *aliases* are extra measure identifiers (``measure.xml`` ``<name>`` /
    ``<class_name>``) that also resolve to this step as ``<alias>.<arg>``.
    """
    param = MappedParameter(
        name=str(arg_name),
        kind="measure_argument",
        default=default,
        step_index=step_idx,
        measure_name=str(measure_name) if measure_name else None,
    )
    plain_key = str(arg_name)
    if plain_key not in result:
        result[plain_key] = param
    if measure_name:
        dotted_key = f"{measure_name}.{arg_name}"
        if dotted_key not in result:
            result[dotted_key] = param
    for alias in aliases:
        alias_key = f"{alias}.{arg_name}"
        if alias and alias != measure_name and alias_key not in result:
            result[alias_key] = param


def _collect_step_aliases(
    steps: list[Any],
    search_dirs: list[Path],
) -> dict[int, tuple[str, ...]]:
    """Per-step extra measure identifiers (step ``name``, ``measure.xml`` names).

    An alias is dropped when it equals any step's canonical measure name or is
    claimed by more than one step, so ``<alias>.<arg>`` can never silently
    resolve to a different measure than ``<measure_dir_name>.<arg>`` would.
    """
    canonical: set[str] = set()
    raw: dict[int, list[str]] = {}
    for idx, step in enumerate(steps):
        if not isinstance(step, dict):
            continue
        dir_name = step.get("measure_dir_name")
        canonical.add(str(dir_name or step.get("name") or ""))
        if not dir_name:
            continue
        found: list[str] = []
        if step.get("name"):
            found.append(str(step["name"]))
        xml_path = _locate_step_measure_xml(str(dir_name), search_dirs)
        if xml_path is not None:
            found.extend(read_measure_xml_aliases(xml_path))
        raw[idx] = list(dict.fromkeys(found))
    claims: dict[str, set[int]] = {}
    for idx, names in raw.items():
        for name in names:
            claims.setdefault(name, set()).add(idx)
    return {
        idx: tuple(n for n in names if n not in canonical and len(claims[n]) == 1)
        for idx, names in raw.items()
    }


def parse_osw_arguments(template: Path) -> dict[str, MappedParameter]:
    """Parse an .osw file and return a name→MappedParameter map for all
    measure arguments across all steps.

    The .osw format is JSON: ``{"steps": [{"measure_dir_name": "...",
    "arguments": {name: value}}]}``.

    Key behaviours:

    1. **Dotted-name keys** — for each argument we also register the
       qualified form ``MeasureName.argument_name`` so that users can
       disambiguate via ``variables.yml``.
    2. **First-match for plain names** — if two steps expose the same
       argument name, the *first* step wins in the plain-key entry.
       Use the dotted form to target a specific measure.
    3. **measure.xml + ``__SKIP__`` discovery (issue #2067)** — for each
       step with a ``measure_dir_name``, arguments declared in the
       step's ``measure.xml`` (under ``measures/`` or ``measure_paths``)
       and OpenStudio's built-in ``__SKIP__`` toggle are also indexed
       even when omitted from ``step["arguments"]`` in the seed ``.osw``.
    """
    try:
        data = json.loads(template.read_text())
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid .osw JSON in {template}: {exc}") from exc
    result: dict[str, MappedParameter] = {}
    search_dirs = _measure_search_dirs(template, data if isinstance(data, dict) else {})
    step_aliases = _collect_step_aliases(data.get("steps", []), search_dirs)
    for step_idx, step in enumerate(data.get("steps", [])):
        if not isinstance(step, dict):
            continue
        arguments = step.get("arguments", {})
        if not isinstance(arguments, dict):
            arguments = {}
        # Resolve the measure name from measure_dir_name or name field.
        measure_dir_name = step.get("measure_dir_name")
        measure_name = str(measure_dir_name or step.get("name") or "")
        xml_path = (
            _locate_step_measure_xml(str(measure_dir_name), search_dirs)
            if measure_dir_name
            else None
        )
        aliases = step_aliases.get(step_idx, ())
        for arg_name, default in arguments.items():
            _register_osw_argument(
                result,
                arg_name=str(arg_name),
                default=default,
                step_idx=step_idx,
                measure_name=measure_name,
                aliases=aliases,
            )
        if xml_path is not None:
            for xml_arg_name, _xml_arg_type, xml_default in _parse_measure_xml_arg_info(xml_path):
                if xml_arg_name not in arguments:
                    _register_osw_argument(
                        result,
                        arg_name=xml_arg_name,
                        default=xml_default,
                        step_idx=step_idx,
                        measure_name=measure_name,
                        aliases=aliases,
                    )
        if "__SKIP__" not in arguments:
            _register_osw_argument(
                result,
                arg_name="__SKIP__",
                default=False,
                step_idx=step_idx,
                measure_name=measure_name,
                aliases=aliases,
            )
    return result


def build_measure_argument_alias_map(variable_defs: list[dict[str, Any]]) -> dict[str, str]:
    """Map each variable ``name`` to its explicit ``measure_argument`` target.

    Only variables whose ``measure_argument`` differs from ``name`` are
    included; ``epw_file`` variables are skipped (they use ``target``).
    Raises :class:`CrossMeasureConflictError` when two variables resolve to
    the same target.
    """
    alias_map: dict[str, str] = {}
    owners: dict[str, str] = {}
    for var in variable_defs:
        name = var.get("name")
        target = var.get("measure_argument")
        if not name or not isinstance(target, str) or not target.strip():
            continue
        name, target = str(name), target.strip()
        if var.get("target") == "epw_file" or EPW_FILE_KEY in (name, target):
            continue
        if target in owners and owners[target] != name:
            raise CrossMeasureConflictError(
                f"Variables {owners[target]!r} and {name!r} both map to "
                f"measure_argument {target!r}; each target may be varied only once."
            )
        owners[target] = name
        if target != name:
            alias_map[name] = target
    return alias_map


def resolve_measure_argument_keys(
    parameters: dict[str, Any],
    alias_map: dict[str, str],
) -> dict[str, Any]:
    """Return *parameters* with variable names rewritten to their ``measure_argument`` targets."""
    if not alias_map:
        return dict(parameters)
    resolved: dict[str, Any] = {}
    for key, value in parameters.items():
        target = alias_map.get(key, key)
        if target in resolved:
            raise CrossMeasureConflictError(
                f"Parameter {key!r} resolves to {target!r}, which is already set by another variable."
            )
        resolved[target] = value
    return resolved


# ---------------------------------------------------------------------------
# Pre-flight check
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
    # Detect colliding parameter targets (e.g., an unqualified `<arg>`
    # combined with `<Measure>.<arg>`, or dotted keys that lack distinct
    # OSW step isolation). Disambiguated `<MeasureA>.<arg>` and
    # `<MeasureB>.<arg>` targeting distinct OSW steps are allowed (#2065).
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
    """Detect when two parameters collide on the same target argument.

    In OpenStudio ``.osw`` workflows, each step has its own isolated
    ``arguments`` dictionary and ``_mutate_osw`` writes ``<MeasureA>.<arg>``
    exclusively to step ``MeasureA``'s ``step_index``.  Consequently,
    disambiguated ``<MeasureA>.<arg>`` and ``<MeasureB>.<arg>`` targeting
    distinct OSW steps (different ``step_index`` values) do not collide
    (issue #2065).

    A true conflict arises when:
    1. Both an unqualified ``<arg>`` and a qualified ``<Measure>.<arg>``
       are specified together in *parameters*.
    2. Multiple parameters resolve to the same ``(step_index, plain_arg)``
       target (or have ``step_index=None``, where measure prefixes are not
       isolated per OSW step).
    """
    plain_params_by_arg: dict[str, list[tuple[str, str | None, int | None]]] = {}
    dotted_params_by_arg: dict[str, list[tuple[str, str, int | None]]] = {}

    for key in parameters:
        mapping = mappings.get(key)
        if mapping is None or mapping.kind != "measure_argument":
            continue
        if "." not in key:
            plain_params_by_arg.setdefault(mapping.name, []).append(
                (key, mapping.measure_name, mapping.step_index)
            )
            continue
        if mapping.measure_name is None:
            continue
        _, _, plain_arg = key.rpartition(".")
        if plain_arg:
            dotted_params_by_arg.setdefault(plain_arg, []).append(
                (key, mapping.measure_name, mapping.step_index)
            )

    conflicts: list[str] = []
    all_args = sorted(set(plain_params_by_arg) | set(dotted_params_by_arg))
    for plain_arg in all_args:
        plain_entries = plain_params_by_arg.get(plain_arg, [])
        dotted_entries = dotted_params_by_arg.get(plain_arg, [])
        if plain_entries and dotted_entries:
            dotted_keys = sorted({dk for dk, _, _ in dotted_entries})
            measures = sorted(
                {m for _, m, _ in plain_entries if m is not None}
                | {m for _, m, _ in dotted_entries}
            )
            conflicts.append(
                f"  {plain_arg!r} specified as both unqualified {plain_arg!r} and "
                f"qualified {', '.join(repr(k) for k in dotted_keys)} "
                f"(measures: {', '.join(measures)}). Choose ONE dotted form "
                f"(e.g. {dotted_keys[0]!r}) to avoid ambiguous runtime behavior."
            )
            continue

        if len(dotted_entries) > 1:
            step_indices = [step_idx for _, _, step_idx in dotted_entries]
            # Distinct non-None OSW step_index values are isolated per step.
            if all(idx is not None for idx in step_indices) and len(set(step_indices)) == len(
                step_indices
            ):
                continue
            measures = sorted({measure_name for _, measure_name, _ in dotted_entries})
            dotted_keys = sorted({dotted_key for dotted_key, _, _ in dotted_entries})
            conflicts.append(
                f"  {plain_arg!r} specified for multiple measures without step isolation: "
                f"{', '.join(measures)}. Choose ONE dotted form "
                f"(e.g. {dotted_keys[0]!r}) to avoid ambiguous runtime behavior."
            )

    if conflicts:
        if not os.environ.get("OSIMFLOW_ALLOW_CROSS_MEASURE_CONFLICT"):
            raise CrossMeasureConflictError(
                "Pre-flight check failed: conflicting measure argument specifications "
                "detected, creating a potential runtime conflict:\n" + "\n".join(conflicts)
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

    from ._work_osm_mutations import _resolve_osm_object  # noqa: PLC0415

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
        found = _resolve_osm_object(model, openstudio, mapping.object_type, mapping.object_name)
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
    # JSON-mode stub (shared with work.default_apply_parameters' fallback).
    from ._work_osm_mutations import _apply_json_stub_osm_attributes  # noqa: PLC0415

    attrs = {
        name: value
        for name, value in parameters.items()
        if name in mappings and mappings[name].kind == "attribute"
    }
    if not _apply_json_stub_osm_attributes(osm_path, attrs):
        raise ValueError(f"Invalid JSON-mode .osm (expected a JSON object): {osm_path}")


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

    from ._work_osm_mutations import _apply_osm_mutations  # noqa: PLC0415

    model_opt = openstudio.openstudiomodelcore.Model.load(str(osm_path))
    if not model_opt.is_initialized():
        raise ValueError(f"OpenStudio failed to load model from {osm_path}")
    model = model_opt.get()

    attributes = {
        name: value
        for name, value in parameters.items()
        if name in mappings and mappings[name].kind == "attribute"
    }
    _apply_osm_mutations(model, openstudio, attributes)

    # Save the mutated model back to the same path.
    model.save(str(osm_path), overwrite=True)
    log.info("Mutated production .osm saved to %s", osm_path)


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


def _normalize_scalar(value: Any) -> Any:
    """Unwrap NumPy-like scalars (``np.int64``, ``np.bool_``) to Python scalars."""
    if not isinstance(value, (str, bytes, int, float)) or type(value).__module__ == "numpy":
        item = getattr(value, "item", None)
        if callable(item) and getattr(value, "shape", None) == ():
            try:
                return item()
            except (TypeError, ValueError):
                return value
    return value


def _coerce_boolean(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return {"true": True, "1": True, "false": False, "0": False}.get(value.strip().lower())
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    return None


def _coerce_number(value: Any, *, integer: bool) -> float | int | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value.strip()) if isinstance(value, str) else float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    if not integer:
        return number
    if isinstance(value, str) and re.fullmatch(r"[+-]?\d+", value.strip()):
        return int(value.strip())
    return int(number) if number.is_integer() else None


def _coerce_typed_osw_value(arg_name: str, value: Any, arg_type: str, measure: str) -> Any:
    """Coerce *value* to the OpenStudio *arg_type* declared in ``measure.xml``.

    ``Boolean`` -> ``bool``, ``Integer`` -> ``int``, ``Double`` -> ``float``,
    ``String`` / ``Choice`` -> ``str``. Other types (e.g. ``Path``) and
    ``None`` pass through. Uncoercible values raise
    :class:`ArgumentCoercionError`.
    """
    value = _normalize_scalar(value)
    if value is None:
        return value
    result: Any
    if arg_type == "Boolean":
        result = _coerce_boolean(value)
    elif arg_type in ("Double", "Integer"):
        result = _coerce_number(value, integer=arg_type == "Integer")
    elif arg_type in ("String", "Choice"):
        return ("true" if value else "false") if isinstance(value, bool) else str(value)
    else:
        return value
    if result is None:
        raise ArgumentCoercionError(
            f"measure {measure!r} argument {arg_name!r} expects {arg_type}, got {value!r}"
        )
    return result


def _coerce_osw_argument_value(
    arg_name: str,
    value: Any,
    *,
    existing_value: Any,
    mapping_default: Any,
    arg_types: dict[str, str],
    measure_name: str = "",
) -> Any:
    """Coerce *value* for ``measure.xml``-typed OSW arguments.

    Typed arguments follow :func:`_coerce_typed_osw_value`. Untyped arguments
    keep the legacy behavior: ``true``/``false`` strings become booleans for
    ``__SKIP__`` or when the existing/default value is boolean.
    """
    arg_type = arg_types.get(arg_name)
    if arg_type is not None and arg_name != "__SKIP__":
        return _coerce_typed_osw_value(arg_name, value, arg_type, measure_name)
    if not isinstance(value, str):
        return value
    is_boolean_arg = (
        arg_name == "__SKIP__"
        or isinstance(existing_value, bool)
        or isinstance(mapping_default, bool)
    )
    if not is_boolean_arg:
        return value
    lowered = value.strip().lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    return value


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
    search_dirs = _measure_search_dirs(osw_path, data if isinstance(data, dict) else {})
    step_arg_types: dict[int, dict[str, str]] = {}
    for param_key, value in parameters.items():
        mapping = mappings.get(param_key)
        if mapping is None or mapping.kind != "measure_argument":
            continue
        step_index = mapping.step_index
        if step_index is None or step_index >= len(steps):
            continue
        step_obj = steps[step_index]
        step_args = step_obj.setdefault("arguments", {})
        if step_index not in step_arg_types:
            arg_types: dict[str, str] = {}
            measure_dir_name = step_obj.get("measure_dir_name")
            if isinstance(measure_dir_name, str) and measure_dir_name:
                xml_path = _locate_step_measure_xml(measure_dir_name, search_dirs)
                if xml_path is not None:
                    for xml_arg_name, xml_arg_type, _ in _parse_measure_xml_arg_info(xml_path):
                        arg_types[xml_arg_name] = xml_arg_type
            step_arg_types[step_index] = arg_types
        coerced = _coerce_osw_argument_value(
            mapping.name,
            value,
            existing_value=step_args.get(mapping.name),
            mapping_default=mapping.default,
            arg_types=step_arg_types[step_index],
            measure_name=str(step_obj.get("measure_dir_name") or step_obj.get("name") or ""),
        )
        # mapping.name is the plain argument name inside the step's
        # arguments dict (never dotted).
        step_args[mapping.name] = coerced
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
def load_variable_defs(variables_yml: Path) -> list[dict[str, Any]]:
    """Load the ``variables`` list from a ``variables.yml`` file."""
    import yaml  # noqa: PLC0415

    try:
        data = yaml.safe_load(variables_yml.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise OSimFlowValueError(f"{variables_yml}: invalid YAML: {exc}") from exc
    variables = data.get("variables") if isinstance(data, dict) else data
    if not isinstance(variables, list):
        raise OSimFlowValueError(f"{variables_yml}: expected a 'variables' list")
    return [v for v in variables if isinstance(v, dict)]


def _resolve_variable_defs(
    parameters: dict[str, Any],
    variable_defs: list[dict[str, Any]],
) -> dict[str, Any]:
    """Apply ``variables.yml`` ``epw_file`` targets and ``measure_argument`` aliases.

    ``target: epw_file`` variables become the reserved :data:`EPW_FILE_KEY`
    (via the variable's ``mapping``); variables with a ``measure_argument``
    are renamed to that target, mirroring ``Campaign.step_apply_parameters``.
    """
    resolved = dict(parameters)
    for var in variable_defs:
        name = var.get("name")
        if var.get("target") != "epw_file" or name not in resolved:
            continue
        raw = resolved.pop(name)
        label = raw["label"] if isinstance(raw, dict) and "label" in raw else raw
        epw_path = (var.get("mapping") or {}).get(label)
        if epw_path is None:
            raise ValueError(f"epw_file variable {name!r}: value {label!r} not in its mapping")
        resolved[EPW_FILE_KEY] = str(epw_path)
    return resolve_measure_argument_keys(resolved, build_measure_argument_alias_map(variable_defs))


def apply_parameters(
    template: Path,
    parameters: dict[str, Any],
    sample_id: str,
    out: Path,
    *,
    variable_defs: list[dict[str, Any]] | None = None,
) -> Path:
    """Apply a parameter set to a template and write the per-sample output.

    Shares its OSW/OSM mutation semantics with
    :func:`osimflow.work.default_apply_parameters` (via
    ``osimflow._work_osm_mutations``): categorical ``{"label", "index"}``
    values are written as their label, the reserved :data:`EPW_FILE_KEY`
    sets ``weather_file``, and ``.osm`` attributes are mutated through the
    OpenStudio bindings (JSON stub fallback for test-mode ``.osm`` files).

    Args:
        template: the template_sim_package file (.osm or .osw) or directory.
        parameters: the LHS dict for this sample, e.g. ``{"wwr": 0.4}``.
            May contain the reserved key :data:`EPW_FILE_KEY`
            (``"__epw_file__"``); it is extracted before the pre-flight
            check and applied as a top-level ``weather_file`` mutation.
            The caller's dict is not modified.
        sample_id: the sample's identifier, e.g. ``"0001"``.
        out: the per-sample output directory. Created if missing.
        variable_defs: optional ``variables.yml`` ``variables`` list (see
            :func:`load_variable_defs`). Enables ``measure_argument``
            aliases and ``target: epw_file`` resolution.

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
    from ._work_osm_mutations import _apply_osw_parameters  # noqa: PLC0415

    original_template = template
    params = (
        _resolve_variable_defs(parameters, variable_defs) if variable_defs else dict(parameters)
    )
    # The epw key is not a measure argument or .osm attribute; keep it out
    # of the pre-flight check and apply it via the shared OSW path.
    epw_file_value = params.pop(EPW_FILE_KEY, None)
    mappings = _build_mappings(template)
    target_file = _select_template_file(template)
    template_type = detect_template_type(target_file)

    preflight_check(params, mappings)

    _copy_template_artifacts(original_template, out)
    osm_in_out: Path | None
    if original_template.is_dir():
        osw_in_out = out / "workflow.osw"
        _, template_osm = _resolve_dir_template(original_template)
        osm_in_out = (
            out / template_osm.resolve().relative_to(original_template.resolve())
            if template_osm is not None
            else out / "model.osm"
        )
    elif template_type == "osm":
        osw_in_out = out / "workflow.osw"
        osm_in_out = out / target_file.name
    else:
        osw_in_out = out / target_file.name
        osm_in_out = None

    osw_params = dict(params)
    if epw_file_value is not None:
        osw_params[EPW_FILE_KEY] = epw_file_value
    # OSW pass: measure arguments + weather_file. The .osm pass takes every
    # parameter mapped to an attribute, even one the OSW also consumed
    # (a name colliding across both files is mutated in each, as before).
    _apply_osw_parameters(osw_in_out, osw_params)
    osm_params = {
        k: v for k, v in params.items() if k in mappings and mappings[k].kind == "attribute"
    }
    if osm_params and osm_in_out is not None and osm_in_out.is_file():
        _mutate_osm(osm_in_out, osm_params, mappings)

    return out
