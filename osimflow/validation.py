"""Input validation for OSimFlow (issue #278).

Provides:
- ``ValidationError`` — custom exception for all validation failures
- ``validate_path_within`` — path-traversal protection
- ``validate_variables_yml`` — schema validation for variables.yml
- ``validate_template_package`` — template package structure checks
- ``sanitize_sample_id`` — API sample-ID sanitization
- ``sanitize_filename`` — API filename sanitization
"""

from __future__ import annotations

__all__ = ["ValidationError"]

import json
import logging
import os
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import jsonschema
import yaml

from .errors import OSimFlowError

log = logging.getLogger("osimflow.validation")


# ======================================================================
# Custom exception
# ======================================================================


class ValidationError(OSimFlowError):
    """Raised when user-supplied input fails validation.

    Carries a human-readable message describing the problem and
    (optionally) the field that triggered the failure.
    """

    def __init__(self, message: str, *, field: str | None = None) -> None:
        self.field = field
        super().__init__(message)


# ======================================================================
# Known distributions and their required parameters
# ======================================================================

# Maps distribution name → set of required parameter names.
DISTRIBUTION_PARAMS: dict[str, set[str]] = {
    "uniform": {"min", "max"},
    "normal": {"mean", "sigma"},
    "lognormal": {"mean", "sigma"},
    "triangular": {"min", "max"},  # 'mode' is optional
    "discrete": {"values"},
    "categorical": {"values"},
    "beta": {"alpha", "beta"},
    "gamma": {"alpha"},
    "exponential": {"rate"},
}

VALID_DISTRIBUTIONS: frozenset[str] = frozenset(DISTRIBUTION_PARAMS)


# ======================================================================
# Path-traversal protection
# ======================================================================

_MAX_PATH_LENGTH = 4096


def validate_path_within(
    path: Path | str,
    base_dir: Path | str,
    *,
    must_exist: bool = False,
    must_be_file: bool = False,
    must_be_dir: bool = False,
    readable: bool = False,
) -> Path:
    """Resolve *path* and verify it stays within *base_dir*.

    Parameters
    ----------
    path
        User-supplied path (may be relative, contain ``..``, symlinks, etc.).
    base_dir
        The allowed root directory.  Must already exist.
    must_exist
        If ``True``, raise :class:`ValidationError` when the resolved path
        does not exist on disk.
    must_be_file
        If ``True``, the resolved path must be a regular file.
    must_be_dir
        If ``True``, the resolved path must be a directory.
    readable
        If ``True``, the resolved path must be readable (``os.access``).

    Returns
    -------
    Path
        The fully resolved, validated absolute path.

    Raises
    ------
    ValidationError
        On any validation failure (traversal, type mismatch, permissions).
    """
    resolved_base = Path(base_dir).resolve()
    raw = Path(path)

    # Reject obviously malicious strings early.
    raw_str = str(raw)
    if len(raw_str) > _MAX_PATH_LENGTH:
        raise ValidationError(
            f"Path too long ({len(raw_str)} chars, max {_MAX_PATH_LENGTH})",
            field="path",
        )

    # Null bytes are illegal in paths and can be used to bypass checks.
    if "\0" in raw_str:
        raise ValidationError("Null byte in path", field="path")

    resolved = raw.resolve()

    if not resolved.is_relative_to(resolved_base):
        raise ValidationError(
            f"Path escapes allowed directory: {resolved} is not within {resolved_base}",
            field="path",
        )

    if must_exist and not resolved.exists():
        raise ValidationError(f"Path does not exist: {resolved}", field="path")

    if must_be_file and resolved.exists() and not resolved.is_file():
        raise ValidationError(f"Path is not a regular file: {resolved}", field="path")

    if must_be_dir and resolved.exists() and not resolved.is_dir():
        raise ValidationError(f"Path is not a directory: {resolved}", field="path")

    if readable and resolved.exists() and not os.access(resolved, os.R_OK):
        raise ValidationError(f"Path is not readable: {resolved}", field="path")

    return resolved


# ======================================================================
# variables.yml schema validation
# ======================================================================


def validate_variables_yml(path: Path) -> list[dict[str, Any]]:
    """Load and validate a ``variables.yml`` file.

    Parameters
    ----------
    path
        Path to the YAML file.

    Returns
    -------
    list[dict[str, Any]]
        The validated list of variable definitions.

    Raises
    ------
    ValidationError
        When the file is empty, malformed, or contains invalid variable
        definitions.
    """
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        raise ValidationError("variables.yml is empty", field="variables")

    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ValidationError(f"Invalid YAML in variables.yml: {exc}") from exc

    if not isinstance(data, dict):
        raise ValidationError(
            f"variables.yml must be a mapping, got {type(data).__name__}",
            field="variables",
        )

    if "variables" not in data:
        raise ValidationError(
            "variables.yml missing required 'variables' key",
            field="variables",
        )

    variables = data["variables"]
    if not isinstance(variables, list):
        raise ValidationError(
            f"'variables' must be a list, got {type(variables).__name__}",
            field="variables",
        )

    if len(variables) == 0:
        raise ValidationError(
            "'variables' list is empty — at least one variable is required",
            field="variables",
        )

    validated: list[dict[str, Any]] = []
    for i, var in enumerate(variables, start=1):
        field_prefix = f"variables[{i}]"
        if not isinstance(var, dict):
            raise ValidationError(
                f"{field_prefix}: must be a mapping, got {type(var).__name__}",
                field=field_prefix,
            )
        _validate_single_variable(var, field_prefix)
        validated.append(var)

    return validated


def _validate_single_variable(var: dict[str, Any], prefix: str) -> None:
    """Validate a single variable definition from variables.yml."""
    # Required: name
    if "name" not in var:
        raise ValidationError(f"{prefix}: missing required field 'name'", field=prefix)
    name = var["name"]
    if not isinstance(name, str) or not name.strip():
        raise ValidationError(f"{prefix}: 'name' must be a non-empty string", field=prefix)

    # Required: distribution
    if "distribution" not in var:
        raise ValidationError(f"{prefix}: missing required field 'distribution'", field=prefix)
    dist = var["distribution"]
    if not isinstance(dist, str):
        raise ValidationError(
            f"{prefix}: 'distribution' must be a string, got {type(dist).__name__}",
            field=prefix,
        )
    if dist not in VALID_DISTRIBUTIONS:
        available = ", ".join(sorted(VALID_DISTRIBUTIONS))
        raise ValidationError(
            f"{prefix}: unknown distribution '{dist}'. Valid distributions: {available}",
            field=prefix,
        )

    # Validate distribution-specific parameters.
    required_params = DISTRIBUTION_PARAMS[dist]
    for param in required_params:
        if param not in var:
            raise ValidationError(
                f"{prefix}: distribution '{dist}' requires parameter '{param}'",
                field=prefix,
            )

    # Numeric range checks for applicable distributions.
    _validate_distribution_params(var, dist, prefix)


def _validate_distribution_params(var: dict[str, Any], dist: str, prefix: str) -> None:
    """Validate numeric constraints on distribution parameters."""
    _DISPATCH_TABLE[dist](var, prefix)


def _validate_uniform(var: dict[str, Any], prefix: str) -> None:
    _require_numeric(var, "min", prefix)
    _require_numeric(var, "max", prefix)
    if var["min"] >= var["max"]:
        raise ValidationError(
            f"{prefix}: 'min' ({var['min']}) must be less than 'max' ({var['max']})",
            field=prefix,
        )


def _validate_normal_lognormal(var: dict[str, Any], prefix: str) -> None:
    _require_numeric(var, "mean", prefix)
    _require_numeric(var, "sigma", prefix)
    if var["sigma"] <= 0:
        raise ValidationError(
            f"{prefix}: 'sigma' must be positive, got {var['sigma']}",
            field=prefix,
        )


def _validate_triangular(var: dict[str, Any], prefix: str) -> None:
    _require_numeric(var, "min", prefix)
    _require_numeric(var, "max", prefix)
    if var["min"] >= var["max"]:
        raise ValidationError(
            f"{prefix}: 'min' ({var['min']}) must be less than 'max' ({var['max']})",
            field=prefix,
        )
    if "mode" in var:
        _require_numeric(var, "mode", prefix)
        if not (var["min"] <= var["mode"] <= var["max"]):
            raise ValidationError(
                f"{prefix}: 'mode' ({var['mode']}) must be between "
                f"'min' ({var['min']}) and 'max' ({var['max']})",
                field=prefix,
            )


def _validate_discrete_categorical(var: dict[str, Any], prefix: str) -> None:
    values = var.get("values")
    if not isinstance(values, list):
        raise ValidationError(
            f"{prefix}: 'values' must be a list, got {type(values).__name__}",
            field=prefix,
        )
    if len(values) == 0:
        raise ValidationError(
            f"{prefix}: 'values' list must not be empty",
            field=prefix,
        )


def _validate_beta(var: dict[str, Any], prefix: str) -> None:
    _require_numeric(var, "alpha", prefix)
    _require_numeric(var, "beta", prefix)
    if var["alpha"] <= 0:
        raise ValidationError(
            f"{prefix}: 'alpha' must be positive, got {var['alpha']}",
            field=prefix,
        )
    if var["beta"] <= 0:
        raise ValidationError(
            f"{prefix}: 'beta' must be positive, got {var['beta']}",
            field=prefix,
        )


def _validate_gamma(var: dict[str, Any], prefix: str) -> None:
    _require_numeric(var, "alpha", prefix)
    if var["alpha"] <= 0:
        raise ValidationError(
            f"{prefix}: 'alpha' must be positive, got {var['alpha']}",
            field=prefix,
        )


def _validate_exponential(var: dict[str, Any], prefix: str) -> None:
    _require_numeric(var, "rate", prefix)
    if var["rate"] <= 0:
        raise ValidationError(
            f"{prefix}: 'rate' must be positive, got {var['rate']}",
            field=prefix,
        )


_DISPATCH_TABLE: dict[str, Callable[[dict[str, Any], str], None]] = {
    "uniform": _validate_uniform,
    "normal": _validate_normal_lognormal,
    "lognormal": _validate_normal_lognormal,
    "triangular": _validate_triangular,
    "discrete": _validate_discrete_categorical,
    "categorical": _validate_discrete_categorical,
    "beta": _validate_beta,
    "gamma": _validate_gamma,
    "exponential": _validate_exponential,
}


def _require_numeric(var: dict[str, Any], key: str, prefix: str) -> None:
    """Raise ValidationError if var[key] is not an int or float."""
    val = var.get(key)
    if not isinstance(val, (int, float)):
        raise ValidationError(
            f"{prefix}: '{key}' must be numeric, got {type(val).__name__}",
            field=prefix,
        )


# ======================================================================
# Template package validation
# ======================================================================

_MAX_PACKAGE_SIZE_BYTES = 500 * 1024 * 1024  # 500 MB
_MAX_SINGLE_FILE_BYTES = 100 * 1024 * 1024  # 100 MB
_REQUIRED_TEMPLATE_FILES: frozenset[str] = frozenset({"workflow.osw"})


def validate_template_package(path: Path) -> Path:
    """Validate a template simulation package directory.

    Parameters
    ----------
    path
        Path to the template package directory.

    Returns
    -------
    Path
        The resolved, validated path.

    Raises
    ------
    ValidationError
        When the package is missing required files, exceeds size limits,
        or has permission issues.
    """
    resolved = path.resolve()

    if not resolved.is_dir():
        raise ValidationError(
            f"template_sim_package is not a directory: {resolved}",
            field="template_sim_package",
        )

    # Check required files exist.
    for required in _REQUIRED_TEMPLATE_FILES:
        required_path = resolved / required
        if not required_path.exists():
            raise ValidationError(
                f"template_sim_package missing required file '{required}': {resolved}",
                field="template_sim_package",
            )
        if not required_path.is_file():
            raise ValidationError(
                f"template_sim_package '{required}' is not a regular file: {required_path}",
                field="template_sim_package",
            )

    # Check readability of required files.
    for required in _REQUIRED_TEMPLATE_FILES:
        required_path = resolved / required
        if not os.access(required_path, os.R_OK):
            raise ValidationError(
                f"template_sim_package file '{required}' is not readable: {required_path}",
                field="template_sim_package",
            )

    # Check individual file sizes.
    total_size = 0
    for child in resolved.rglob("*"):
        if child.is_file():
            stat = child.stat()
            total_size += stat.st_size
            if stat.st_size > _MAX_SINGLE_FILE_BYTES:
                raise ValidationError(
                    f"File in template_sim_package exceeds size limit "
                    f"({stat.st_size} > {_MAX_SINGLE_FILE_BYTES} bytes): {child}",
                    field="template_sim_package",
                )

    if total_size > _MAX_PACKAGE_SIZE_BYTES:
        raise ValidationError(
            f"template_sim_package total size ({total_size} bytes) exceeds limit "
            f"({_MAX_PACKAGE_SIZE_BYTES} bytes): {resolved}",
            field="template_sim_package",
        )

    return resolved


# ======================================================================
# API input sanitization helpers
# ======================================================================

# Safe sample-ID pattern: alphanumeric, hyphens, underscores, dots.
_SAFE_SAMPLE_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")

# Safe filename pattern: alphanumeric, hyphens, underscores, dots.
_SAFE_FILENAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")

# Characters that must never appear in user-supplied strings that are
# interpolated into paths, SQL, or HTML.
_DANGEROUS_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f<>\"';&]")


def sanitize_sample_id(sid: str) -> str:
    """Validate and return a sample ID string.

    Parameters
    ----------
    sid
        User-supplied sample ID from a URL path parameter.

    Returns
    -------
    str
        The validated sample ID.

    Raises
    ------
    ValidationError
        If the sample ID contains path-traversal characters, is empty,
        or exceeds a reasonable length.
    """
    if not sid:
        raise ValidationError("Sample ID must not be empty", field="sid")

    if len(sid) > 256:
        raise ValidationError(f"Sample ID too long ({len(sid)} chars, max 256)", field="sid")

    # Block path separators and traversal sequences.
    if "/" in sid or "\\" in sid or ".." in sid:
        raise ValidationError(
            f"Sample ID contains invalid characters: {sid!r}",
            field="sid",
        )

    # Block dangerous characters (null bytes, HTML/SQL injection chars).
    if _DANGEROUS_CHARS_RE.search(sid):
        raise ValidationError(
            f"Sample ID contains disallowed characters: {sid!r}",
            field="sid",
        )

    if not _SAFE_SAMPLE_ID_RE.match(sid):
        raise ValidationError(
            f"Sample ID contains invalid characters: {sid!r}",
            field="sid",
        )

    return sid


def sanitize_filename(filename: str) -> str:
    """Validate and return a filename string for API endpoints.

    Parameters
    ----------
    filename
        User-supplied filename from a URL path parameter.

    Returns
    -------
    str
        The validated filename.

    Raises
    ------
    ValidationError
        If the filename contains path separators, traversal sequences,
        null bytes, or is empty.
    """
    if not filename:
        raise ValidationError("Filename must not be empty", field="filename")

    if len(filename) > 256:
        raise ValidationError(
            f"Filename too long ({len(filename)} chars, max 256)",
            field="filename",
        )

    # Block path separators and traversal.
    if "/" in filename or "\\" in filename or ".." in filename:
        raise ValidationError(
            f"Filename contains path separator or traversal: {filename!r}",
            field="filename",
        )

    # Block null bytes and control characters.
    if "\0" in filename:
        raise ValidationError(
            f"Filename contains null byte: {filename!r}",
            field="filename",
        )

    if not _SAFE_FILENAME_RE.match(filename):
        raise ValidationError(
            f"Filename contains invalid characters: {filename!r}",
            field="filename",
        )

    return filename


def validate_path_within_base(
    resolved_path: Path,
    base_dir: Path,
) -> Path:
    """Check that *resolved_path* is within *base_dir* (both must be resolved).

    This is a lighter-weight version of :func:`validate_path_within` that
    skips the existence/permission checks — it only guards against
    directory traversal.

    Parameters
    ----------
    resolved_path
        A pre-resolved absolute path.
    base_dir
        The allowed root directory (resolved).

    Returns
    -------
    Path
        The validated path.

    Raises
    ------
    ValidationError
        If the path escapes *base_dir*.
    """
    if not resolved_path.is_relative_to(base_dir):
        raise ValidationError(
            f"Path escapes allowed directory: {resolved_path} is not within {base_dir}",
            field="path",
        )
    return resolved_path


# ======================================================================
# OSW (OpenStudio Workflow) schema validation
# ======================================================================

OSW_SCHEMA: dict[str, Any] = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "title": "OpenStudio Workflow",
    "type": "object",
    "additionalProperties": True,
    "required": ["steps"],
    "properties": {
        "workflow_type": {
            "type": "string",
            "enum": ["Complete", "Singleton"],
        },
        "steps": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "required": ["measure_dir_name", "arguments"],
                "properties": {
                    "measure_dir_name": {
                        "type": "string",
                    },
                    "arguments": {
                        "type": "object",
                    },
                },
            },
        },
        "file_paths": {
            "type": "array",
            "items": {
                "type": "string",
            },
        },
    },
}


def validate_osw(path: Path) -> bool:
    """Validate an OpenStudio Workflow (.osw) file against the schema.

    Parameters
    ----------
    path
        Path to the ``.osw`` JSON file.

    Returns
    -------
    bool
        ``True`` if the file is valid.

    Raises
    ------
    ValidationError
        When the file is not a valid JSON, is not a dict, or fails
        schema validation. The error message indicates which field
        failed.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ValidationError(
            f"workflow.osw is not valid UTF-8: {exc}",
            field="workflow.osw",
        ) from exc

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValidationError(
            f"workflow.osw is not valid JSON: {exc}",
            field="workflow.osw",
        ) from exc

    if not isinstance(data, dict):
        raise ValidationError(
            f"workflow.osw must be a JSON object, got {type(data).__name__}",
            field="workflow.osw",
        )

    try:
        jsonschema.validate(instance=data, schema=OSW_SCHEMA)
    except jsonschema.ValidationError as exc:
        field_path = ".".join(str(p) for p in exc.absolute_path) if exc.absolute_path else "root"
        message = exc.message
        raise ValidationError(
            f"workflow.osw validation failed at '{field_path}': {message}",
            field="workflow.osw",
        ) from exc
    except jsonschema.SchemaError as exc:
        raise ValidationError(
            f"workflow.osw schema is invalid: {exc}",
            field="workflow.osw",
        ) from exc

    return True
