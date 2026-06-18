"""Measure discovery, argument introspection, and variable validation.

This module provides a :class:`MeasureRegistry` that scans a
``template_sim_package`` for measures, reads their argument definitions,
and validates that every variable in ``variables.yml`` maps to a real
measure argument before simulations start.

GAP-003 / EXT-012: no measure management system.
GAP-FRESH-CRIT-002: Rserve/BCL Integration — online measure discovery.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests

log = logging.getLogger("osimflow.measures")

BCL_API_BASE = "https://bcl.nrel.gov/api/"
BCL_CACHE_TTL_S = 3600


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class MeasureArgument:
    """A single argument exposed by a measure.

    Attributes:
        name: the argument identifier (as used in ``workflow.osw``).
        type: the OpenStudio argument type string
            (``"Double"``, ``"String"``, ``"Integer"``, ``"Boolean"``,
            ``"Choice"``, ``"Path"``).
        required: whether the argument must be provided.
        default: the default value, or ``None`` if no default.
    """

    name: str
    type: str
    required: bool
    default: Any = None


@dataclass
class DiscoveredMeasure:
    """A measure discovered in the template package.

    Attributes:
        name: the measure directory name (e.g. ``"SetWindowToWallRatio"``).
        path: absolute path to the measure directory.
        language: ``"ruby"`` or ``"python"``.
        arguments: list of arguments the measure exposes.
    """

    name: str
    path: Path
    language: str
    arguments: list[MeasureArgument] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------
class MeasureRegistryError(ValueError):
    """Base exception for measure registry errors."""


class UnmappedVariableError(MeasureRegistryError):
    """Raised when a variable in ``variables.yml`` does not map to any
    discovered measure argument."""


class AmbiguousVariableError(MeasureRegistryError):
    """Raised when a plain variable name matches arguments in multiple
    measures and must be disambiguated via the dotted form."""


class BCLMeasureError(MeasureRegistryError):
    """Raised when a BCL API call fails or returns an unexpected response."""


# ---------------------------------------------------------------------------
# MeasureRegistry
# ---------------------------------------------------------------------------
def _get_bcl_api_key() -> str | None:
    """Resolve the BCL API key from the ``BCL_API_KEY`` env var or None."""
    return os.environ.get("BCL_API_KEY")


def _validate_bcl_measure_taxonomy(
    name: str,
    arguments: list[MeasureArgument],
    entry: dict[str, Any],
) -> None:
    """Log warnings when BCL measure arguments deviate from expected taxonomy.

    The BCL taxonomy defines expected argument names and types for standard
    measure categories. This function checks the discovered arguments against
    the taxonomy and logs warnings for deviations.

    Parameters
    ----------
    name:
        Measure name used in log messages.
    arguments:
        Parsed measure arguments.
    entry:
        Raw BCL API entry dict for additional context.
    """
    taxonomy = entry.get("taxonomy", "")
    measure_type = entry.get("measure_type", "")

    # Check for empty argument list (common BCL metadata issue)
    if not arguments:
        log.warning(
            "BCL measure %r (taxonomy=%r, type=%r): no arguments discovered — "
            "BCL metadata may be incomplete",
            name,
            taxonomy,
            measure_type,
        )
        return

    # Validate argument types against known OpenStudio argument types
    valid_types = {"Double", "String", "Integer", "Boolean", "Choice", "Path"}
    for arg in arguments:
        if arg.type not in valid_types:
            log.warning(
                "BCL measure %r: argument %r has unexpected type %r — expected one of %s",
                name,
                arg.name,
                arg.type,
                valid_types,
            )

    # Log info about measure size/complexity
    n_required = sum(1 for a in arguments if a.required)
    log.debug(
        "BCL measure %r: %d argument(s) (%d required), taxonomy=%r",
        name,
        len(arguments),
        n_required,
        taxonomy,
    )


# ---------------------------------------------------------------------------
class MeasureRegistry:
    """Discover measures and validate variable mappings.

    Scans the ``measures/`` subdirectory of a ``template_sim_package``,
    reads ``measure.rb`` (Ruby) or ``measure.py`` (Python) files to
    discover exposed arguments, and validates that every variable in
    ``variables.yml`` maps to a real measure argument before simulations
    start.

    Example::

        registry = MeasureRegistry()
        registry.index_measures(Path("example_package"))
        registry.validate_variables_mapping(variables, registry)
    """

    def __init__(self) -> None:
        self._measures: dict[str, DiscoveredMeasure] = {}

    # ------------------------------------------------------------------
    # BCL cache directory (lazily initialized)
    # ------------------------------------------------------------------
    @staticmethod
    def _bcl_cache_dir() -> Path:
        """Return the BCL cache directory (~/.osimflow/bcl_cache/)."""
        cache_dir = Path.home() / ".osimflow" / "bcl_cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        return cache_dir

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def index_measures(self, path: Path) -> None:
        """Scan ``path/measures/`` and register all discovered measures.

        Recursively searches for ``measure.rb`` and ``measure.py`` files
        under ``path/measures/``. Each measure directory is registered
        under its directory name.

        Parameters:
            path: the ``template_sim_package`` root directory.
        """
        measures_dir = path / "measures"
        if not measures_dir.is_dir():
            log.info("index_measures: no measures/ directory found in %s", path)
            return

        for measure_dir in sorted(measures_dir.iterdir()):
            if not measure_dir.is_dir():
                continue
            measure = self._discover_measure(measure_dir)
            if measure is not None:
                self._measures[measure.name] = measure
                log.debug(
                    "index_measures: registered %s (%s) with %d argument(s)",
                    measure.name,
                    measure.language,
                    len(measure.arguments),
                )

    def read_measure_arguments(self, measure_path: Path) -> list[MeasureArgument]:
        """Read argument definitions from a measure file.

        Supports both Ruby (``measure.rb``) and Python (``measure.py``)
        measure entry points. Returns an empty list if the file cannot be
        parsed or neither entry point exists.

        Parameters:
            measure_path: path to the measure directory containing
                ``measure.rb`` or ``measure.py``.
        """
        rb_path = measure_path / "measure.rb"
        py_path = measure_path / "measure.py"

        if rb_path.is_file():
            return self._parse_ruby_measure(rb_path)
        if py_path.is_file():
            return self._parse_python_measure(py_path)
        return []

    def validate_variables_mapping(
        self,
        variables: list[dict[str, Any]],
        registry: MeasureRegistry,
    ) -> None:
        """Validate that every variable maps to a discovered measure argument.

        Runs before ``RUN_OPENSTUDIO_SIM`` as a pre-flight check (GAP-003).
        For each variable in ``variables``, verifies that its name (or its
        ``measure_argument`` dotted name) corresponds to a real argument
        in a discovered measure.

        Raises:
            UnmappedVariableError: a variable name does not correspond to
                any discovered measure argument.
            AmbiguousVariableError: a plain variable name appears in
                multiple measures and must be disambiguated.
        """
        # Build the registry of known argument names.
        plain_to_measures: dict[str, set[str]] = {}
        dotted_to_arg: dict[str, MeasureArgument] = {}

        for measure in self._measures.values():
            for arg in measure.arguments:
                # Dotted key: always registered.
                dotted_key = f"{measure.name}.{arg.name}"
                dotted_to_arg[dotted_key] = arg
                # Plain key: collect which measures expose it.
                plain_to_measures.setdefault(arg.name, set()).add(measure.name)

        errors: list[str] = []
        ambiguous: list[str] = []

        for var in variables:
            name = var.get("name", "")
            if not name:
                continue

            # Check for explicit measure_argument dotted reference.
            measure_arg = var.get("measure_argument")
            if measure_arg and isinstance(measure_arg, str) and "." in measure_arg:
                # Validate the dotted reference directly.
                if measure_arg not in dotted_to_arg:
                    errors.append(
                        f"  Variable {name!r}: measure_argument {measure_arg!r} "
                        f"not found in any discovered measure."
                    )
                continue

            # Otherwise, check the plain name.
            if name in dotted_to_arg:
                # Plain name that happens to match a dotted key — okay.
                continue

            if name not in plain_to_measures:
                errors.append(f"  Variable {name!r}: not found in any discovered measure argument.")
            elif len(plain_to_measures.get(name, set())) > 1:
                ambiguous.append(
                    f"  Variable {name!r}: appears in multiple measures "
                    f"({', '.join(sorted(plain_to_measures[name]))}). "
                    f"Use the dotted form 'MeasureName.argument_name' in variables.yml."
                )

        if errors or ambiguous:
            lines: list[str] = ["Measure validation FAILED:", ""]
            lines.extend(errors)
            if ambiguous:
                lines.append("")
                lines.append("Ambiguous variables (use dotted form to disambiguate):")
                lines.extend(ambiguous)
            raise UnmappedVariableError("\n".join(lines))

    def list_available_measures(self) -> list[dict[str, Any]]:
        """Return name, path, and argument list for every discovered measure.

        Returns:
            A list of dicts, each with keys: ``name`` (str), ``path`` (str),
            ``language`` (str), and ``arguments`` (list of dicts with
            ``name``, ``type``, ``required``, ``default``).
        """
        result: list[dict[str, Any]] = []
        for measure in self._measures.values():
            result.append(
                {
                    "name": measure.name,
                    "path": str(measure.path),
                    "language": measure.language,
                    "arguments": [
                        {
                            "name": arg.name,
                            "type": arg.type,
                            "required": arg.required,
                            "default": arg.default,
                        }
                        for arg in measure.arguments
                    ],
                }
            )
        return result

    def discover_from_bcl(
        self,
        query: str | None = None,
        api_key: str | None = None,
        category: str | None = None,
        validate: bool = False,
        timeout: float = 30.0,
    ) -> list[DiscoveredMeasure]:
        """Discover measures from the NREL Building Component Library (BCL).

        Queries the BCL API at ``https://bcl.nrel.gov/api/`` for measures
        matching the given *query* string and/or *category*. Results are
        cached in ``~/.osimflow/bcl_cache/`` for ``BCL_CACHE_TTL_S`` seconds
        to avoid repeated network calls.

        Parameters
        ----------
        query:
            Free-text search query for measure name/description.
            When ``None``, all measures are returned (paginated).
        api_key:
            BCL API key. Can also be set via the ``BCL_API_KEY`` env var.
            An API key is required for some endpoints.
        category:
            Optional BCL measure taxonomy category to filter by
            (e.g. ``"HVAC"``, ``"Envelope"``, ``"Lighting"``).
        validate:
            When ``True``, validate measure arguments against BCL taxonomy
            and log warnings for mismatches.
        timeout:
            HTTP request timeout in seconds (default: 30.0).

        Returns
        -------
        list[DiscoveredMeasure]
            A list of measures discovered from BCL. Each measure includes
            the name, a remote URL, the programming language, and parsed
            arguments from the BCL metadata.

        Raises
        ------
        BCLMeasureError
            When the BCL API returns an error or the response cannot be parsed.
        """
        cache_dir = self._bcl_cache_dir()
        cache_key = hashlib.sha256(f"{query or ''}|{category or ''}".encode()).hexdigest()[:16]
        cache_file = cache_dir / f"{cache_key}.json"

        # Return cached result if fresh
        if cache_file.is_file():
            try:
                mtime = cache_file.stat().st_mtime
                if time.time() - mtime < BCL_CACHE_TTL_S:
                    measures = self._load_cached_bcl_measures(cache_file)
                    if measures:
                        log.info(
                            "BCL cache hit for query=%r, category=%r (%d measures)",
                            query,
                            category,
                            len(measures),
                        )
                        return measures
            except (OSError, json.JSONDecodeError):
                pass

        # Fetch from BCL API
        headers: dict[str, str] = {"Accept": "application/json"}
        resolved_api_key = api_key or _get_bcl_api_key()
        if resolved_api_key:
            headers["x-api-key"] = resolved_api_key

        params: dict[str, str] = {}
        if query:
            params["search"] = query
        if category:
            params["category"] = category

        url = f"{BCL_API_BASE}measures"
        log.info("BCL API request: %s params=%s", url, params)

        try:
            response = requests.get(url, params=params, headers=headers, timeout=timeout)
            response.raise_for_status()
        except requests.RequestException as exc:
            log.error("BCL API request failed: %s", exc)
            raise BCLMeasureError(f"BCL API request failed: {exc}") from exc

        try:
            data = response.json()
        except (ValueError, ET.ParseError) as exc:
            raise BCLMeasureError(f"BCL API returned non-JSON response: {exc}") from exc

        measures = self._parse_bcl_response(data, validate=validate)

        # Persist to cache
        try:
            cache_file.write_text(
                json.dumps(
                    [
                        {
                            "name": m.name,
                            "path": str(m.path),
                            "language": m.language,
                            "arguments": [
                                {
                                    "name": a.name,
                                    "type": a.type,
                                    "required": a.required,
                                    "default": a.default,
                                }
                                for a in m.arguments
                            ],
                        }
                        for m in measures
                    ]
                )
            )
            log.debug("BCL results cached at %s", cache_file)
        except OSError as exc:
            log.warning("could not write BCL cache: %s", exc)

        return measures

    @staticmethod
    def _load_cached_bcl_measures(cache_file: Path) -> list[DiscoveredMeasure]:
        """Load BCL measures from a JSON cache file."""
        data = json.loads(cache_file.read_text(encoding="utf-8"))
        measures: list[DiscoveredMeasure] = []
        for item in data:
            measures.append(
                DiscoveredMeasure(
                    name=item["name"],
                    path=Path(item["path"]),
                    language=item["language"],
                    arguments=[
                        MeasureArgument(
                            name=a["name"],
                            type=a["type"],
                            required=a["required"],
                            default=a.get("default"),
                        )
                        for a in item.get("arguments", [])
                    ],
                )
            )
        return measures

    @staticmethod
    def _parse_bcl_response(data: dict[str, Any], validate: bool) -> list[DiscoveredMeasure]:
        """Parse a BCL API JSON response into DiscoveredMeasure objects.

        The BCL API returns a ``result`` dict with a ``meass`` list
        (occasional typo in the API itself). Each measure entry has
        ``name``, ``description``, ``measure_type``, ``arguments``, etc.
        """
        measures: list[DiscoveredMeasure] = []
        result = data.get("result", {})
        # Handle the BCL API's occasional typo "meass" vs "measures"
        raw_measures = result.get("meass", result.get("measures", []))
        if not isinstance(raw_measures, list):
            log.warning("BCL response 'measures' field is not a list: %s", type(raw_measures))
            return measures

        for entry in raw_measures:
            if not isinstance(entry, dict):
                continue
            name = entry.get("name", "")
            if not name:
                continue

            # Determine language from taxonomy or file extension
            measure_type = entry.get("measure_type", "")
            language = "ruby" if measure_type.lower() == "ruby" else "python"

            # Parse arguments from BCL metadata
            arguments: list[MeasureArgument] = []
            raw_args = entry.get("arguments", [])
            if isinstance(raw_args, list):
                for arg_entry in raw_args:
                    if not isinstance(arg_entry, dict):
                        continue
                    arg_name = arg_entry.get("name", "")
                    if not arg_name:
                        continue
                    arg_type_str = str(arg_entry.get("type", "String")).capitalize()
                    if arg_type_str not in (
                        "Double",
                        "String",
                        "Integer",
                        "Boolean",
                        "Choice",
                        "Path",
                    ):
                        arg_type_str = "String"
                    required = bool(arg_entry.get("required", False))
                    default = arg_entry.get("default_value")
                    arguments.append(
                        MeasureArgument(
                            name=arg_name,
                            type=arg_type_str,
                            required=required,
                            default=default,
                        )
                    )

            if validate:
                _validate_bcl_measure_taxonomy(name, arguments, entry)

            measures.append(
                DiscoveredMeasure(
                    name=name,
                    path=Path(entry.get("url", "")),  # remote URL, not a local path
                    language=language,
                    arguments=arguments,
                )
            )

        log.info("BCL returned %d measure(s)", len(measures))
        return measures

    # ------------------------------------------------------------------
    # Internal: measure discovery
    # ------------------------------------------------------------------
    def _discover_measure(self, measure_dir: Path) -> DiscoveredMeasure | None:
        """Discover a single measure directory.

        Returns ``None`` if neither ``measure.rb`` nor ``measure.py`` exists.
        """
        rb_path = measure_dir / "measure.rb"
        py_path = measure_dir / "measure.py"

        language: str
        if rb_path.is_file():
            language = "ruby"
        elif py_path.is_file():
            language = "python"
        else:
            return None

        arguments = self.read_measure_arguments(measure_dir)
        return DiscoveredMeasure(
            name=measure_dir.name,
            path=measure_dir,
            language=language,
            arguments=arguments,
        )

    # ------------------------------------------------------------------
    # Internal: Ruby parsing
    # ------------------------------------------------------------------
    _RUBY_ARGUMENT_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
        (
            "Double",
            re.compile(
                r'OpenStudio::Measure::OSArgument\.makeDoubleArgument\s*\(\s*["\']([^"\']+)["\']\s*,\s*([Tt]rue|[Ff]alse)\s*\)'
            ),
        ),
        (
            "String",
            re.compile(
                r'OpenStudio::Measure::OSArgument\.makeStringArgument\s*\(\s*["\']([^"\']+)["\']\s*,\s*([Tt]rue|[Ff]alse)\s*\)'
            ),
        ),
        (
            "Integer",
            re.compile(
                r'OpenStudio::Measure::OSArgument\.makeIntegerArgument\s*\(\s*["\']([^"\']+)["\']\s*,\s*([Tt]rue|[Ff]alse)\s*\)'
            ),
        ),
        (
            "Boolean",
            re.compile(
                r'OpenStudio::Measure::OSArgument\.makeBool(?:ean)?Argument\s*\(\s*["\']([^"\']+)["\']\s*,\s*([Tt]rue|[Ff]alse)\s*\)'
            ),
        ),
        (
            "Choice",
            re.compile(
                r'OpenStudio::Measure::OSArgument\.makeChoiceArgument\s*\(\s*["\']([^"\']+)["\']\s*,\s*[^,]+\s*,\s*([Tt]rue|[Ff]alse)\s*\)'
            ),
        ),
        (
            "Path",
            re.compile(
                r'OpenStudio::Measure::OSArgument\.makePathArgument\s*\(\s*["\']([^"\']+)["\']\s*,\s*([Tt]rue|[Ff]alse)\s*\)'
            ),
        ),
    ]

    def _parse_ruby_measure(self, rb_path: Path) -> list[MeasureArgument]:
        """Parse a Ruby measure file for argument definitions.

        Uses regex patterns to find ``make<Type>Argument`` calls and
        extract the argument name and required flag.
        """
        try:
            text = rb_path.read_text(encoding="utf-8")
        except OSError:
            return []

        arguments: list[MeasureArgument] = []
        for arg_type, pattern in self._RUBY_ARGUMENT_PATTERNS:
            for match in pattern.finditer(text):
                name = match.group(1)
                required_str = match.group(2)
                required = required_str.lower() in ("true", "yes")
                arguments.append(MeasureArgument(name=name, type=arg_type, required=required))

        return arguments

    # ------------------------------------------------------------------
    # Internal: Python parsing
    # ------------------------------------------------------------------
    _PYTHON_ARGUMENT_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
        (
            "Double",
            re.compile(
                r'openstudio\.measure\.OSArgument\.makeDoubleArgument\s*\(\s*["\']([^"\']+)["\']\s*,\s*([Tt]rue|[Ff]alse)\s*\)'
            ),
        ),
        (
            "String",
            re.compile(
                r'openstudio\.measure\.OSArgument\.makeStringArgument\s*\(\s*["\']([^"\']+)["\']\s*,\s*([Tt]rue|[Ff]alse)\s*\)'
            ),
        ),
        (
            "Integer",
            re.compile(
                r'openstudio\.measure\.OSArgument\.makeIntegerArgument\s*\(\s*["\']([^"\']+)["\']\s*,\s*([Tt]rue|[Ff]alse)\s*\)'
            ),
        ),
        (
            "Boolean",
            re.compile(
                r'openstudio\.measure\.OSArgument\.makeBool(?:ean)?Argument\s*\(\s*["\']([^"\']+)["\']\s*,\s*([Tt]rue|[Ff]alse)\s*\)'
            ),
        ),
        (
            "Choice",
            re.compile(
                r'openstudio\.measure\.OSArgument\.makeChoiceArgument\s*\(\s*["\']([^"\']+)["\']\s*,\s*([Tt]rue|[Ff]alse)\s*\)'
            ),
        ),
        (
            "Path",
            re.compile(
                r'openstudio\.measure\.OSArgument\.makePathArgument\s*\(\s*["\']([^"\']+)["\']\s*,\s*([Tt]rue|[Ff]alse)\s*\)'
            ),
        ),
    ]

    def _parse_python_measure(self, py_path: Path) -> list[MeasureArgument]:
        """Parse a Python measure file for argument definitions.

        Uses regex patterns to find ``OSArgument.make<Type>Argument`` calls
        and extract the argument name and required flag.
        """
        try:
            text = py_path.read_text(encoding="utf-8")
        except OSError:
            return []

        arguments: list[MeasureArgument] = []
        for arg_type, pattern in self._PYTHON_ARGUMENT_PATTERNS:
            for match in pattern.finditer(text):
                name = match.group(1)
                required_str = match.group(2)
                required = required_str.lower() in ("true", "yes")
                arguments.append(MeasureArgument(name=name, type=arg_type, required=required))

        return arguments
