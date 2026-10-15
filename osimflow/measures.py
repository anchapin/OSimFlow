"""Measure discovery, argument introspection, and variable validation.

This module provides a :class:`MeasureRegistry` that scans a
``template_sim_package`` for measures, reads their argument definitions,
and validates that every variable in ``variables.yml`` maps to a real
measure argument before simulations start.

GAP-003 / EXT-012: no measure management system.
GAP-FRESH-CRIT-002: Rserve/BCL Integration — online measure discovery.
"""

from __future__ import annotations

__all__ = [
    "AmbiguousVariableError",
    "DiscoveredMeasure",
    "MeasureArgument",
    "MeasureRegistry",
    "MeasureRegistryError",
    "UnmappedVariableError",
]

import hashlib
import importlib
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

from .apply_params import (
    EPW_FILE_KEY,
    _infer_osm_attribute_mapping,
    _measure_search_dirs,
    read_measure_xml_aliases,
)
from .errors import OSimFlowValueError

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
        aliases: extra identifiers from ``measure.xml`` (``<name>``,
            ``<class_name>``) accepted in dotted ``measure_argument`` refs.
    """

    name: str
    path: Path
    language: str
    arguments: list[MeasureArgument] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------
class MeasureRegistryError(OSimFlowValueError):
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
        self._indexed_path: Path | None = None
        # workflow.osw step "name" -> measure_dir_name (multi-instance steps)
        self._step_aliases: dict[str, str] = {}

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
        """Scan the measure directories of a package and register all measures.

        Searches ``path/measures/`` plus every ``measure_paths`` entry declared
        in ``path/workflow.osw`` (relative entries resolve against the ``.osw``
        directory). A measure directory name found in an earlier location wins.
        Per-step ``"name"`` aliases from ``workflow.osw`` are recorded so
        ``<step_name>.<argument>`` variables validate for multi-instance steps.

        Parameters:
            path: the ``template_sim_package`` root directory.
        """
        self._indexed_path = path
        self._step_aliases = {}
        osw_data = self._load_workflow_osw(path / "workflow.osw")
        search_dirs = _measure_search_dirs(path / "workflow.osw", osw_data)
        if not any(d.is_dir() for d in search_dirs):
            log.info("index_measures: no measure directories found in %s", path)
            return

        for measures_dir in search_dirs:
            if not measures_dir.is_dir():
                continue
            for measure_dir in sorted(measures_dir.iterdir()):
                if not measure_dir.is_dir() or measure_dir.name in self._measures:
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
        self._index_step_aliases(osw_data)

    @staticmethod
    def _load_workflow_osw(osw_path: Path) -> dict[str, Any]:
        if not osw_path.is_file():
            return {}
        try:
            data = json.loads(osw_path.read_text())
        except (OSError, ValueError):
            log.warning("index_measures: could not parse %s", osw_path, exc_info=True)
            return {}
        return data if isinstance(data, dict) else {}

    def _index_step_aliases(self, osw_data: dict[str, Any]) -> None:
        """Map distinct per-step ``name`` values to their ``measure_dir_name``."""
        claims: dict[str, set[str]] = {}
        for step in osw_data.get("steps") or []:
            if not isinstance(step, dict):
                continue
            dir_name = step.get("measure_dir_name")
            step_name = step.get("name")
            if (
                isinstance(dir_name, str)
                and isinstance(step_name, str)
                and step_name
                and step_name != dir_name
                and dir_name in self._measures
            ):
                claims.setdefault(step_name, set()).add(dir_name)
        # A name claimed for two different measures is ambiguous: drop it.
        self._step_aliases = {n: next(iter(d)) for n, d in claims.items() if len(d) == 1}

    def read_measure_arguments(self, measure_path: Path) -> list[MeasureArgument]:
        """Read argument definitions from a measure directory.

        Parses ``measure.xml`` first as authoritative metadata (preserving
        XML document order), then appends any additional arguments
        discovered from ``measure.rb`` or ``measure.py`` whose ``name`` is
        not already present (preserving source order). Falls back to
        source parsing if ``measure.xml`` is absent or malformed.

        Parameters:
            measure_path: path to the measure directory containing
                ``measure.xml``, ``measure.rb``, or ``measure.py``.
        """
        xml_path = measure_path / "measure.xml"
        rb_path = measure_path / "measure.rb"
        py_path = measure_path / "measure.py"

        xml_args: list[MeasureArgument] = []
        if xml_path.is_file():
            xml_args = self._parse_measure_xml(xml_path)

        src_args: list[MeasureArgument] = []
        if rb_path.is_file():
            src_args = self._parse_ruby_measure(rb_path)
        elif py_path.is_file():
            src_args = self._parse_python_measure(py_path)

        merged: list[MeasureArgument] = []
        seen: set[str] = set()
        for arg in xml_args:
            if arg.name not in seen:
                seen.add(arg.name)
                merged.append(arg)
        for arg in src_args:
            if arg.name not in seen:
                seen.add(arg.name)
                merged.append(arg)
        return merged

    @staticmethod
    def _resolve_effective_has_osm(target: MeasureRegistry, has_osm: bool | None) -> bool:
        if has_osm is not None:
            return has_osm
        if target._indexed_path is not None:
            return (target._indexed_path / "model.osm").is_file() or any(
                target._indexed_path.glob("*.osm")
            )
        return False

    @staticmethod
    def _is_valid_skip_ref(ref: str, known_measures: set[str]) -> bool | None:
        """Return ``True``/``False`` if *ref* is ``<Measure>.__SKIP__``, else ``None``."""
        if "." not in ref:
            return None
        m_prefix, arg_suffix = ref.split(".", 1)
        if arg_suffix != "__SKIP__":
            return None
        return m_prefix in known_measures

    @classmethod
    def _validate_dotted_measure_argument(
        cls,
        name: str,
        measure_arg: str,
        *,
        known_measures: set[str],
        dotted_to_arg: dict[str, MeasureArgument],
        effective_has_osm: bool,
        errors: list[str],
    ) -> None:
        skip_ok = cls._is_valid_skip_ref(measure_arg, known_measures)
        if skip_ok is True or measure_arg in dotted_to_arg:
            return
        if (
            skip_ok is None
            and effective_has_osm
            and measure_arg == name
            and _infer_osm_attribute_mapping(measure_arg) is not None
        ):
            return
        errors.append(
            f"  Variable {name!r}: measure_argument {measure_arg!r} "
            f"not found in any discovered measure."
        )

    @classmethod
    def _validate_plain_variable_name(
        cls,
        name: str,
        *,
        known_measures: set[str],
        dotted_to_arg: dict[str, MeasureArgument],
        plain_to_measures: dict[str, set[str]],
        effective_has_osm: bool,
        errors: list[str],
        ambiguous: list[str],
    ) -> None:
        skip_ok = cls._is_valid_skip_ref(name, known_measures)
        if skip_ok is not None:
            if not skip_ok:
                errors.append(f"  Variable {name!r}: not found in any discovered measure argument.")
            return
        if name in dotted_to_arg:
            return
        if name in plain_to_measures:
            if len(plain_to_measures[name]) > 1:
                ambiguous.append(
                    f"  Variable {name!r}: appears in multiple measures "
                    f"({', '.join(sorted(plain_to_measures[name]))}). "
                    f"Use the dotted form 'MeasureName.argument_name' in variables.yml."
                )
            return
        if effective_has_osm and _infer_osm_attribute_mapping(name) is not None:
            return
        errors.append(f"  Variable {name!r}: not found in any discovered measure argument.")

    @classmethod
    def _validate_single_variable(
        cls,
        var: dict[str, Any],
        *,
        known_measures: set[str],
        dotted_to_arg: dict[str, MeasureArgument],
        plain_to_measures: dict[str, set[str]],
        effective_has_osm: bool,
        errors: list[str],
        ambiguous: list[str],
    ) -> None:
        name = var.get("name", "")
        measure_arg = var.get("measure_argument")
        if (not name and not measure_arg) or EPW_FILE_KEY in (name, measure_arg):
            return
        if measure_arg and isinstance(measure_arg, str) and "." in measure_arg:
            cls._validate_dotted_measure_argument(
                name,
                measure_arg,
                known_measures=known_measures,
                dotted_to_arg=dotted_to_arg,
                effective_has_osm=effective_has_osm,
                errors=errors,
            )
            return
        if name:
            cls._validate_plain_variable_name(
                name,
                known_measures=known_measures,
                dotted_to_arg=dotted_to_arg,
                plain_to_measures=plain_to_measures,
                effective_has_osm=effective_has_osm,
                errors=errors,
                ambiguous=ambiguous,
            )

    def validate_variables_mapping(
        self,
        variables: list[dict[str, Any]],
        registry: MeasureRegistry | None = None,
        *,
        has_osm: bool | None = None,
    ) -> None:
        """Validate that every variable maps to a discovered measure argument.

        Runs before ``RUN_OPENSTUDIO_SIM`` as a pre-flight check (GAP-003).
        For each variable in ``variables``, verifies that its name (or its
        ``measure_argument`` dotted name) corresponds to a real argument
        in a discovered measure, the special ``__epw_file__`` key, a
        ``<KnownMeasure>.__SKIP__`` toggle, or (when a ``.osm`` model is
        present in the template package) a valid ``.osm`` attribute.

        Raises:
            UnmappedVariableError: a variable name does not correspond to
                any discovered measure argument (or valid ``.osm`` attribute).
        """
        target = registry if registry is not None else self

        # Build the registry of known argument names.
        plain_to_measures: dict[str, set[str]] = {}
        dotted_to_arg: dict[str, MeasureArgument] = {}
        known_measures: set[str] = set(target._measures.keys())

        # An alias is usable only if it is unique and not another measure's name.
        alias_claims: dict[str, int] = {}
        for measure in target._measures.values():
            for alias in measure.aliases:
                alias_claims[alias] = alias_claims.get(alias, 0) + 1
        for measure in target._measures.values():
            usable = [
                a for a in measure.aliases if alias_claims[a] == 1 and a not in target._measures
            ]
            for arg in measure.arguments:
                dotted_to_arg[f"{measure.name}.{arg.name}"] = arg
                plain_to_measures.setdefault(arg.name, set()).add(measure.name)
                for alias in usable:
                    dotted_to_arg[f"{alias}.{arg.name}"] = arg
            known_measures.update(usable)
        # Step names colliding with a measure name or any measure.xml alias are
        # ambiguous and never usable (same policy as parse_osw_arguments).
        taken = set(target._measures) | set(alias_claims)
        for step_name, dir_name in target._step_aliases.items():
            step_measure = target._measures.get(dir_name)
            if step_measure is None or step_name in taken:
                continue
            for arg in step_measure.arguments:
                dotted_to_arg[f"{step_name}.{arg.name}"] = arg
            known_measures.add(step_name)

        effective_has_osm = self._resolve_effective_has_osm(target, has_osm)
        errors: list[str] = []
        ambiguous: list[str] = []

        for var in variables:
            self._validate_single_variable(
                var,
                known_measures=known_measures,
                dotted_to_arg=dotted_to_arg,
                plain_to_measures=plain_to_measures,
                effective_has_osm=effective_has_osm,
                errors=errors,
                ambiguous=ambiguous,
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

        Returns ``None`` if none of ``measure.rb``, ``measure.py``, or
        ``measure.xml`` exists. Always keys the measure by
        ``measure_dir.name`` to match ``measure_dir_name`` in ``workflow.osw``.
        """
        rb_path = measure_dir / "measure.rb"
        py_path = measure_dir / "measure.py"
        xml_path = measure_dir / "measure.xml"

        language: str
        if rb_path.is_file():
            language = "ruby"
        elif py_path.is_file():
            language = "python"
        elif xml_path.is_file():
            language = self._detect_xml_measure_language(xml_path)
        else:
            return None

        arguments = self.read_measure_arguments(measure_dir)
        return DiscoveredMeasure(
            name=measure_dir.name,
            path=measure_dir,
            language=language,
            arguments=arguments,
            aliases=[a for a in read_measure_xml_aliases(xml_path) if a != measure_dir.name]
            if xml_path.is_file()
            else [],
        )

    # ------------------------------------------------------------------
    # Internal: measure.xml parsing
    # ------------------------------------------------------------------
    _XML_ARGUMENT_TYPE_MAP: dict[str, str] = {
        "double": "Double",
        "integer": "Integer",
        "int": "Integer",
        "boolean": "Boolean",
        "bool": "Boolean",
        "string": "String",
        "str": "String",
        "choice": "Choice",
        "path": "Path",
    }

    @staticmethod
    def _parse_xml_root(xml_text: str) -> ET.Element:
        """Parse XML text using ``defusedxml.ElementTree`` when installed."""
        try:
            defused_et = importlib.import_module("defusedxml.ElementTree")
            root: ET.Element = defused_et.fromstring(xml_text)
            return root
        except ModuleNotFoundError:
            return ET.fromstring(xml_text)

    @classmethod
    def _detect_xml_measure_language(cls, xml_path: Path) -> str:
        """Infer ``"python"`` or ``"ruby"`` from ``measure.xml`` script entries."""
        try:
            root = cls._parse_xml_root(xml_path.read_text(encoding="utf-8"))
        except (OSError, ET.ParseError, ValueError):
            return "ruby"
        for file_elem in root.findall("./files/file"):
            usage = (file_elem.findtext("usage_type") or "").strip().lower()
            ftype = (file_elem.findtext("filetype") or "").strip().lower()
            if usage == "script" and ftype in ("py", "python"):
                return "python"
        return "ruby"

    def _parse_measure_xml(self, xml_path: Path) -> list[MeasureArgument]:
        """Parse ``<measure><arguments><argument>`` entries from ``measure.xml``."""
        try:
            xml_text = xml_path.read_text(encoding="utf-8")
            root = self._parse_xml_root(xml_text)
        except (OSError, ET.ParseError, ValueError):
            log.warning(
                "Failed to parse measure.xml at %s; falling back to source parsing",
                xml_path,
                exc_info=True,
            )
            return []

        arguments: list[MeasureArgument] = []
        seen: set[str] = set()
        for arg_elem in root.findall("./arguments/argument"):
            raw_name = arg_elem.findtext("name")
            if raw_name is None or not raw_name.strip():
                continue
            name = raw_name.strip()
            if name in seen:
                continue
            seen.add(name)

            raw_type = (arg_elem.findtext("type") or "").strip().lower()
            arg_type = self._XML_ARGUMENT_TYPE_MAP.get(raw_type, "String")

            raw_required = (arg_elem.findtext("required") or "").strip().lower()
            required = raw_required in ("true", "yes", "1")

            default_elem = arg_elem.find("default_value")
            default = default_elem.text if default_elem is not None else None

            arguments.append(
                MeasureArgument(
                    name=name,
                    type=arg_type,
                    required=required,
                    default=default,
                )
            )
        return arguments

    # ------------------------------------------------------------------
    # Internal: Delimiter-aware Ruby & Python OSArgument call scanner
    # ------------------------------------------------------------------
    _RUBY_OSARGUMENT_CALL_RE: re.Pattern[str] = re.compile(
        r"OpenStudio::(?:Measure|Ruleset)::OSArgument(?:\.|::)make"
        r"(Double|String|Integer|Bool(?:ean)?|Choice|Path)Argument\s*\("
    )

    _PYTHON_OSARGUMENT_CALL_RE: re.Pattern[str] = re.compile(
        r"openstudio\.(?:measure|ruleset)\.OSArgument\.make"
        r"(Double|String|Integer|Bool(?:ean)?|Choice|Path)Argument\s*\("
    )

    @staticmethod
    def _strip_hash_comments(text: str) -> str:
        """Strip ``#`` line comments outside single/double-quoted string literals."""
        out: list[str] = []
        i = 0
        n = len(text)
        in_quote: str | None = None
        escape = False
        while i < n:
            ch = text[i]
            if in_quote is not None:
                out.append(ch)
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == in_quote:
                    in_quote = None
                i += 1
            elif ch in ("'", '"'):
                in_quote = ch
                out.append(ch)
                i += 1
            elif ch == "#":
                while i < n and text[i] != "\n":
                    i += 1
            else:
                out.append(ch)
                i += 1
        return "".join(out)

    @staticmethod
    def _advance_quote_state(
        ch: str,
        in_quote: str | None,
        escape: bool,
    ) -> tuple[str | None, bool]:
        """Advance string-literal state for character *ch* inside quotes."""
        if escape:
            return in_quote, False
        if ch == "\\":
            return in_quote, True
        if ch == in_quote:
            return None, False
        return in_quote, False

    @classmethod
    def _extract_balanced_call_args(cls, text: str, open_paren_pos: int) -> tuple[str, int] | None:
        """Return ``(inner_args_text, close_pos)`` for ``(`` at *open_paren_pos*."""
        depth_paren = 1
        in_quote: str | None = None
        escape = False
        for i in range(open_paren_pos + 1, len(text)):
            ch = text[i]
            if in_quote is not None:
                in_quote, escape = cls._advance_quote_state(ch, in_quote, escape)
            elif ch in ("'", '"'):
                in_quote = ch
            elif ch == "(":
                depth_paren += 1
            elif ch == ")":
                depth_paren -= 1
                if depth_paren == 0:
                    return text[open_paren_pos + 1 : i], i + 1
        return None

    @classmethod
    def _split_top_level_args(cls, inner: str) -> list[str]:
        """Split comma-separated call arguments at top-level nesting depth."""
        parts: list[str] = []
        cur: list[str] = []
        depth = 0
        in_quote: str | None = None
        escape = False
        for ch in inner:
            if in_quote is not None:
                cur.append(ch)
                in_quote, escape = cls._advance_quote_state(ch, in_quote, escape)
            elif ch in ("'", '"'):
                in_quote = ch
                cur.append(ch)
            elif ch in "([{":
                depth += 1
                cur.append(ch)
            elif ch in ")]}":
                depth = max(0, depth - 1)
                cur.append(ch)
            elif ch == "," and depth == 0:
                parts.append("".join(cur).strip())
                cur = []
            else:
                cur.append(ch)
        tail = "".join(cur).strip()
        if tail:
            parts.append(tail)
        return parts

    @staticmethod
    def _unquote_string_literal(expr: str) -> str | None:
        """Extract string contents if *expr* is a single- or double-quoted literal."""
        token = expr.strip()
        if len(token) >= 2 and token[0] == token[-1] and token[0] in ("'", '"'):
            return token[1:-1]
        return None

    @classmethod
    def _scan_osargument_calls(
        cls,
        text: str,
        call_re: re.Pattern[str],
    ) -> list[MeasureArgument]:
        """Extract ``MeasureArgument`` entries from Ruby or Python source text."""
        cleaned = cls._strip_hash_comments(text)
        arguments: list[MeasureArgument] = []
        seen: set[str] = set()

        for match in call_re.finditer(cleaned):
            raw_kind = match.group(1)
            arg_type = "Boolean" if raw_kind in ("Bool", "Boolean") else raw_kind
            open_paren_pos = match.end() - 1
            extracted = cls._extract_balanced_call_args(cleaned, open_paren_pos)
            if extracted is None:
                continue
            inner, _ = extracted
            call_args = cls._split_top_level_args(inner)
            if len(call_args) < 2:
                continue

            name = cls._unquote_string_literal(call_args[0])
            if not name or name in seen:
                continue

            if arg_type == "Choice":
                req_expr = call_args[1] if len(call_args) == 2 else call_args[2]
            elif arg_type == "Path" and len(call_args) >= 4:
                req_expr = call_args[3]
            else:
                req_expr = call_args[1]

            req_clean = req_expr.strip()
            if req_clean.lower().startswith("required"):
                _, _, rhs = req_clean.partition("=")
                if rhs:
                    req_clean = rhs.strip()
            required = req_clean.lower() in ("true", "yes", "1")

            seen.add(name)
            arguments.append(MeasureArgument(name=name, type=arg_type, required=required))

        return arguments

    def _parse_ruby_measure(self, rb_path: Path) -> list[MeasureArgument]:
        """Parse a Ruby measure file for argument definitions."""
        try:
            text = rb_path.read_text(encoding="utf-8")
        except OSError:
            return []
        return self._scan_osargument_calls(text, self._RUBY_OSARGUMENT_CALL_RE)

    def _parse_python_measure(self, py_path: Path) -> list[MeasureArgument]:
        """Parse a Python measure file for argument definitions."""
        try:
            text = py_path.read_text(encoding="utf-8")
        except OSError:
            return []
        return self._scan_osargument_calls(text, self._PYTHON_OSARGUMENT_CALL_RE)
