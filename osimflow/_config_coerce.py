"""Variable type coercion split out of ``config.py`` (issue #1890)."""

import logging
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger("osimflow.config")


# ======================================================================
# Type coercion (issue #409)
# ======================================================================

#: Maps type-name strings (as used in a variable's ``type`` field) to
#: their corresponding Python built-in types.
_TYPE_NAME_MAP: dict[str, type] = {
    "float": float,
    "double": float,
    "int": int,
    "integer": int,
    "bool": bool,
    "boolean": bool,
    "str": str,
    "string": str,
    "list": list,
}

#: Numeric distribution-parameter keys whose YAML values should be
#: coerced to ``float`` during loading (e.g. ``min: "1.0"`` → ``1.0``).
_NUMERIC_PARAM_KEYS: frozenset[str] = frozenset(
    {"min", "max", "mean", "sigma", "mode", "alpha", "beta", "loc", "scale", "rate"}
)


def coerce_variable_type(  # noqa: PLR0911, PLR0912, PLR0915
    value: Any, expected_type: str | type
) -> Any:
    """Coerce *value* to *expected_type*.

    Handles the common YAML-to-Python mismatches that arise when users
    write ``variables.yml`` by hand:

    - ``str → float``  (``"1.5"`` → ``1.5``)
    - ``str → int``    (``"3"`` → ``3``, ``"3.0"`` → ``3``)
    - ``str → bool``   (``"true"`` / ``"1"`` → ``True``, ``"false"`` / ``"0"`` → ``False``)
    - ``str → list``   (``"a, b, c"`` → ``["a", "b", "c"]``)
    - ``int → float``  (widening, always safe)
    - ``float → int``  (only when no precision is lost)

    When *value* is already an instance of *expected_type* it is returned
    unchanged.  Each coercion is logged at ``DEBUG`` level.

    Parameters
    ----------
    value
        The value to coerce (often a string from YAML).
    expected_type
        Target type — either a Python ``type`` object (``float``, ``int``,
        ``bool``, ``str``, ``list``) or a type-name string (``"float"``,
        ``"int"``, ``"bool"``, ``"str"``, ``"list"``; aliases ``"double"``,
        ``"integer"``, ``"boolean"``, ``"string"`` are also accepted).

    Returns
    -------
    The coerced value.

    Raises
    ------
    ValueError
        If *expected_type* is an unknown string, or if the value cannot
        be losslessly coerced.
    """
    # --- resolve string type name → Python type ----------------------
    if isinstance(expected_type, str):
        key = expected_type.lower().strip()
        if key not in _TYPE_NAME_MAP:
            raise ValueError(
                f"unknown type '{expected_type}'. Valid names: {', '.join(sorted(_TYPE_NAME_MAP))}"
            )
        expected_type = _TYPE_NAME_MAP[key]

    # --- already correct type → identity ------------------------------
    # Special-case: ``bool`` is a subclass of ``int`` in Python, so
    # ``isinstance(True, int)`` is ``True``.  We must *not* treat a bool
    # as an int (or vice-versa) when the caller explicitly requests one.
    if type(value) is expected_type:
        return value
    if (
        expected_type is not bool
        and isinstance(value, expected_type)
        and not isinstance(value, bool)
    ):
        return value

    # --- numeric widening / narrowing ---------------------------------
    if expected_type is float:
        if isinstance(value, bool):
            raise ValueError(
                f"cannot coerce bool {value!r} to float — use an explicit int or float value"
            )
        if isinstance(value, int):
            result = float(value)
            log.debug("coerced %r → %r (int→float)", value, result)
            return result
        if isinstance(value, str):
            result = float(value)  # may raise ValueError — let it propagate
            log.debug("coerced %r → %r (str→float)", value, result)
            return result

    if expected_type is int:
        if isinstance(value, bool):
            raise ValueError(f"cannot coerce bool {value!r} to int — use an explicit int value")
        if isinstance(value, float):
            if value != int(value):
                raise ValueError(f"cannot coerce {value!r} to int without loss of precision")
            result = int(value)
            log.debug("coerced %r → %r (float→int)", value, result)
            return result
        if isinstance(value, str):
            try:
                result = int(value)
            except ValueError:
                # Try float intermediary: "3.0" → 3
                f = float(value)
                if f != int(f):
                    raise ValueError(
                        f"cannot coerce {value!r} to int without loss of precision"
                    ) from None
                result = int(f)
            log.debug("coerced %r → %r (str→int)", value, result)
            return result

    # --- bool ---------------------------------------------------------
    if expected_type is bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            norm = value.lower().strip()
            if norm in ("true", "1", "yes", "on"):
                log.debug("coerced %r → True (str→bool)", value)
                return True
            if norm in ("false", "0", "no", "off", ""):
                log.debug("coerced %r → False (str→bool)", value)
                return False
            raise ValueError(f"cannot coerce {value!r} to bool")
        if isinstance(value, (int, float)):
            result = bool(value)
            log.debug("coerced %r → %r (num→bool)", value, result)
            return result

    # --- list (comma-separated string → list) -------------------------
    if expected_type is list:
        if isinstance(value, list):
            return value
        if isinstance(value, str):
            parts: list[str] = [item.strip() for item in value.split(",") if item.strip()]
            log.debug("coerced %r → %r (str→list)", value, parts)
            return parts
        raise ValueError(f"cannot coerce {value!r} to list")

    # --- str (stringify anything) -------------------------------------
    if expected_type is str:
        return str(value)

    # --- fallback: try the type constructor ---------------------------
    try:
        return expected_type(value)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"cannot coerce {value!r} to {expected_type.__name__}") from exc


def _coerce_variables_yml_file(path: Path) -> bool:  # noqa: PLR0912
    """Normalise type representations in a ``variables.yml`` file in-place.

    Reads the YAML, coerces string-valued numeric distribution parameters
    (``min``, ``max``, ``mean``, ``sigma``, ``mode``, ``alpha``, ``beta``,
    ``loc``, ``scale``, ``rate``) to ``float``, and coerces comma-separated
    strings to lists for ``discrete`` / ``categorical`` distributions.

    The file is written back **only** when at least one value was changed.
    Individual coercion failures are silently skipped — the downstream
    ``validate_variables_yml`` call will report the original error with a
    clear message.

    Parameters
    ----------
    path
        Path to the ``variables.yml`` file.

    Returns
    -------
    bool
        ``True`` if the file was modified, ``False`` otherwise.
    """
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        return False

    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        return False  # let validate_variables_yml report the syntax error

    if not isinstance(data, dict) or "variables" not in data:
        return False

    variables = data["variables"]
    if not isinstance(variables, list):
        return False

    changed = False
    for var in variables:
        if not isinstance(var, dict):
            continue

        # Coerce numeric distribution params from string to float.
        for key in _NUMERIC_PARAM_KEYS:
            if key in var and isinstance(var[key], str):
                try:
                    var[key] = coerce_variable_type(var[key], float)
                    changed = True
                except ValueError:
                    pass  # validation will report the error

        # Coerce comma-separated string → list for discrete/categorical.
        dist = var.get("distribution")
        if (
            dist in ("discrete", "categorical")
            and "values" in var
            and isinstance(var["values"], str)
        ):
            try:
                var["values"] = coerce_variable_type(var["values"], list)
                changed = True
            except ValueError:
                pass

    if changed:
        try:
            with path.open("w", encoding="utf-8") as f:
                yaml.dump(data, f, default_flow_style=False, sort_keys=False)
            log.debug(
                "type coercion normalised %s (%d variable definitions)",
                path,
                len(variables),
            )
        except OSError as exc:
            log.warning("could not write type-coerced %s: %s", path, exc)
            return False

    return changed
