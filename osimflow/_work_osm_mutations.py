"""Moved from ``osimflow.work`` (issue #1890); re-exported there."""

import json
import logging
from pathlib import Path
from typing import Any

from osimflow.apply_params import OSMAttributeError

log = logging.getLogger("osimflow.work")


def _apply_osw_parameters(osw_path: Path, parameters: dict[str, Any]) -> dict[str, Any]:
    """Apply OSW-level parameters in place; return the remaining ``.osm`` ones.

    Handles the reserved ``__epw_file__`` key (``weather_file``) and every
    parameter that maps to a measure argument in *osw_path*. Categorical
    ``{"label": ..., "index": ...}`` values are written as their label.
    """
    from .apply_params import (  # noqa: PLC0415
        EPW_FILE_KEY,
        _mutate_osw,
        mutate_osw_weather_file,
        parse_osw_arguments,
    )

    remaining = dict(parameters)
    epw_file = remaining.pop(EPW_FILE_KEY, None)
    if not osw_path.is_file():
        if epw_file is not None:
            log.warning("epw_file requested but no workflow.osw in %s; ignoring", osw_path.parent)
        return remaining
    if remaining:
        mappings = parse_osw_arguments(osw_path)
        osw_params: dict[str, Any] = {}
        for key in list(remaining):
            mapped = mappings.get(key)
            if mapped is not None and mapped.kind == "measure_argument":
                value = remaining.pop(key)
                if isinstance(value, dict) and "label" in value:
                    value = value["label"]
                osw_params[key] = value
        if osw_params:
            _mutate_osw(osw_path, osw_params, mappings)
    if epw_file is not None:
        mutate_osw_weather_file(osw_path, str(epw_file))
    return remaining


def _apply_json_stub_osm_attributes(osm_path: Path, parameters: dict[str, Any]) -> bool:
    """Mutate a test-mode JSON ``model.osm`` in place if it starts with ``{``."""
    try:
        text = osm_path.read_text()
    except OSError:
        return False
    if not text.lstrip().startswith("{"):
        return False
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return False
    if not isinstance(data, dict):
        return False
    attrs = data.setdefault("attributes", {})
    if not isinstance(attrs, dict):
        return False
    for name, value in parameters.items():
        attrs[name] = value
    osm_path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    return True


def _apply_osm_mutations(
    model: Any,
    openstudio: Any,
    variables: dict[str, Any],
) -> None:
    """Apply a dict of variable mutations to an OpenStudio model.

    Supports both simple attribute names and dotted ``Type_name.attr``
    names. For dotted names, resolves the target object by type and
    instance name before setting the attribute.
    """
    from .apply_params import parse_dotted_name  # noqa: PLC0415

    for name, value in variables.items():
        _apply_single_osm_mutation(model, openstudio, name, value, parse_dotted_name)


def _apply_single_osm_mutation(
    model: Any,
    openstudio: Any,
    name: str,
    value: Any,
    parse_dotted_name: Any,
) -> None:
    """Apply a single variable mutation to an OpenStudio model.

    Args:
        name: Variable name, either simple (``"lighting_power_density"``)
            or dotted (``"SpaceType_Office.lighting_power_density"``).
        value: The value to set.
        parse_dotted_name: Callable to parse dotted names.
    """
    parsed = parse_dotted_name(name)

    obj: Any = model
    attribute: str = parsed.attribute

    if parsed.object_type is not None and parsed.object_name is not None:
        obj = _resolve_osm_object(model, openstudio, parsed.object_type, parsed.object_name)
        if obj is None:
            raise OSMAttributeError(
                f"Cannot resolve {parsed.object_type} '{parsed.object_name}' "
                f"for variable '{name}'. The object does not exist in the model."
            )

    coerced: Any = value
    if isinstance(value, bool):
        coerced = value
    elif isinstance(value, int):
        coerced = float(value)
    elif isinstance(value, (float, str)):
        coerced = value
    else:
        raise TypeError(
            f"Cannot coerce parameter value of type {type(value).__name__} "
            f"for OpenStudio SDK: {value!r}"
        )

    _set_osm_attribute(obj, openstudio, parsed.object_type, attribute, coerced)


def _resolve_osm_object(
    model: Any,
    openstudio: Any,
    object_type: str,
    object_name: str,
) -> Any | None:
    """Resolve an OpenStudio model object by type and instance name."""
    getter_name = "get" + object_type + "s"
    getter = getattr(model, getter_name, None)
    if getter is None:
        log.warning("Unsupported .osm object type %r — skipping", object_type)
        return None
    objects = getter()
    for obj in objects:
        if obj.nameString() == object_name:
            return obj
    return None


def _set_osm_attribute(
    obj: Any,
    openstudio: Any,
    object_type: str | None,
    attribute: str,
    value: Any,
) -> None:
    """Set an attribute on an OpenStudio model object.

    Dispatches to typed setters for known object/attribute pairs, and
    falls back to ``setString`` for generic IDD attributes.
    """
    setter_map: dict[str, dict[str, Any]] = {
        "SpaceType": {
            "lighting_power_density": lambda o, v: o.setLightingPowerPerFloorArea(v),
        },
        "ThermalZone": {
            "cooling_setpoint": lambda o, v: _set_thermal_zone_schedule(
                o, openstudio, v, "cooling"
            ),
            "heating_setpoint": lambda o, v: _set_thermal_zone_schedule(
                o, openstudio, v, "heating"
            ),
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
                return
            except Exception as exc:
                log.error(
                    "Failed to set %s.%s=%r: %s",
                    object_type,
                    attribute,
                    value,
                    exc,
                    exc_info=True,
                )
                raise OSMAttributeError(
                    f"Failed to set {object_type}.{attribute}={value!r}: {exc}"
                ) from exc

    if isinstance(value, str):
        obj.setString(attribute, value)
    elif isinstance(value, (int, float)):
        obj.setString(attribute, str(value))
    else:
        raise OSMAttributeError(
            f"No setter for {object_type}.{attribute} with value type "
            f"{type(value).__name__}. Define an explicit setter in "
            f"osimflow/work.py."
        )


def _set_thermal_zone_schedule(
    zone: Any,
    openstudio: Any,
    value: float,
    kind: str,
) -> None:
    """Set a constant temperature schedule on a ThermalZone.

    Creates a ScheduleConstant and assigns it as the cooling or heating
    setpoint schedule on the zone.
    """
    schedule = openstudio.openstudiomodelcore.ScheduleConstant(zone.model())
    schedule.setValue(value)
    if kind == "cooling":
        zone.setCoolingSetpointTemperatureSchedule(schedule)
    else:
        zone.setHeatingSetpointTemperatureSchedule(schedule)
