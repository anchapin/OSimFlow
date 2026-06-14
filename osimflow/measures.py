"""Measure discovery, argument introspection, and variable validation.

This module provides a :class:`MeasureRegistry` that scans a
``template_sim_package`` for measures, reads their argument definitions,
and validates that every variable in ``variables.yml`` maps to a real
measure argument before simulations start.

GAP-003 / EXT-012: no measure management system.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger("osimflow.measures")


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


# ---------------------------------------------------------------------------
# MeasureRegistry
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

        for measure in registry._measures.values():
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
                r'OpenStudio::Measure::OSArgument\.makeBooleanArgument\s*\(\s*["\']([^"\']+)["\']\s*,\s*([Tt]rue|[Ff]alse)\s*\)'
            ),
        ),
        (
            "Choice",
            re.compile(
                r'OpenStudio::Measure::OSArgument\.makeChoiceArgument\s*\(\s*["\']([^"\']+)["\']\s*,\s*([Tt]rue|[Ff]alse)\s*\)'
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
                r'openstudio\.measure\.OSArgument\.makeBooleanArgument\s*\(\s*["\']([^"\']+)["\']\s*,\s*([Tt]rue|[Ff]alse)\s*\)'
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
