"""Measure discovery and introspection API endpoints (issue #348).

Provides:
  - GET  /api/v1/measures              — list all measures used in a campaign
  - GET  /api/v1/measures/{name}       — get detailed measure info and arguments

Measures are discovered from the campaign's ``workflow.osw`` and their
arguments are introspected by parsing ``measure.rb`` (Ruby) or
``measure.py`` (Python) files found in the configured ``measure_paths``.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from osimflow.api.schemas import MeasureArgument, MeasureInfo, MeasureListResponse

log = logging.getLogger("osimflow.api.measures")

measures_router = APIRouter()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _outdir_or_503(request: Request) -> Path:
    """Return the configured outdir or raise 503."""
    outdir: Path | None = getattr(request.app.state, "outdir", None)
    if outdir is None:
        raise HTTPException(status_code=503, detail="No output directory configured")
    return outdir


def _discover_workflow_osw(outdir: Path) -> Path | None:
    """Find the workflow.osw file.

    Checks the outdir itself, then modified_sim_package/ and
    template_sim_package/ subdirectories.
    """
    candidates = [
        outdir / "workflow.osw",
        outdir / "modified_sim_package" / "workflow.osw",
        outdir / "template_sim_package" / "workflow.osw",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def _load_workflow(osw_path: Path) -> dict[str, Any]:
    """Load and parse a workflow.osw JSON file."""
    try:
        return json.loads(osw_path.read_text(encoding="utf-8"))  # type: ignore[no-any-return]
    except (json.JSONDecodeError, OSError) as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to read workflow.osw: {exc}",
        ) from exc


def _introspect_ruby_measure(measure_dir: Path) -> list[MeasureArgument]:  # noqa: PLR0912, PLR0915
    """Parse argument definitions from a Ruby measure's ``measure.rb`` file.

    Looks for ``MeasureArgument.new("name", ...)`` calls and extracts
    associated display_name, description, type, default_value, required,
    and valid_choices settings.
    """
    measure_rb = measure_dir / "measure.rb"
    if not measure_rb.is_file():
        return []

    try:
        source = measure_rb.read_text(encoding="utf-8")
    except OSError:
        return []

    arguments: list[MeasureArgument] = []
    measure_dir_name = measure_dir.name

    # Match argument variable assignments: var = ...Argument.new("name", ...)
    # Accepts OpenStudio::Measure::MeasureArgument, OpenStudio::OSArgument,
    # OpenStudio::Measure::OSArgument, Octopi::Argument, etc.
    arg_pattern = re.compile(
        r"(?P<lhs>\w+)\s*=\s*\S+Argument\.new\s*\(\s*[\"'](?P<name>[^\"']+)[\"']",
        re.MULTILINE,
    )

    raw_args: list[tuple[str, str, Any, bool]] = []  # (name, type, default, required)

    lines = source.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        m = arg_pattern.search(line)
        if m:
            arg_name = m.group("name")
            arg_type = "String"
            default_val: Any = None
            required = False

            # Look ahead for associated settings (type, default, required)
            j = i + 1
            while j < min(i + 20, len(lines)):
                next_line = lines[j]

                # Type via setAttribute
                if re.search(
                    rf"{re.escape(arg_name)}\.setAttribute\s*\(\s*\"type\"\s*,\s*(?P<type>\d)",
                    next_line,
                ):
                    type_map = {0: "Boolean", 1: "String", 2: "Double", 3: "Integer", 4: "Choice"}
                    tm2 = re.search(r'setAttribute\s*\(\s*"type"\s*,\s*(?P<type>\d+)', next_line)
                    if tm2:
                        arg_type = type_map.get(int(tm2.group("type")), "String")

                # Default value
                dm = re.search(
                    rf"{re.escape(arg_name)}\.setDefaultValue\s*\(\s*(?P<val>[^)]+?)\s*\)",
                    next_line,
                )
                if dm:
                    raw_val = dm.group("val").strip()
                    if raw_val in ("true", "false"):
                        default_val = raw_val == "true"
                    elif re.match(r"^-?\d+\.\d+$", raw_val):
                        default_val = float(raw_val)
                    elif re.match(r"^-?\d+$", raw_val):
                        default_val = int(raw_val)
                    elif re.match(r'^["\'](.*)["\']\s*$', raw_val):
                        default_val = re.match(r'^["\'](.*)["\']\s*$', raw_val).group(1)  # type: ignore[union-attr]

                # Required
                if re.search(rf"{re.escape(arg_name)}\.setRequired\s*\(\s*true\s*\)", next_line):
                    required = True

                j += 1

            raw_args.append((arg_name, arg_type, default_val, required))
            i = j - 1
        i += 1

    # Second pass: display_name, description, valid_choices per variable name
    display_names: dict[str, str] = {}
    descriptions: dict[str, str] = {}
    valid_choices: dict[str, list[str]] = {}

    for line in lines:
        dm = re.search(r'(?P<oname>\w+)\.setDisplayName\s*\(\s*"([^"]+)"\s*\)', line)
        if dm:
            display_names[dm.group("oname")] = dm.group(2)

        dem = re.search(r'(?P<oname>\w+)\.setDescription\s*\(\s*"([^"]+)"\s*\)', line)
        if dem:
            descriptions[dem.group("oname")] = dem.group(2)

        vcm = re.search(r"(?P<oname>\w+)\.setValidValues\s*\(\s*\[([^\]]+)\]\s*\)", line)
        if vcm:
            choices_str = vcm.group(2)
            choices = [c.strip().strip('"').strip("'") for c in choices_str.split(",") if c.strip()]
            valid_choices[vcm.group("oname")] = choices

    for arg_name, arg_type, default_val, required in raw_args:
        arguments.append(
            MeasureArgument(
                name=arg_name,
                display_name=display_names.get(arg_name, arg_name),
                description=descriptions.get(arg_name),
                argument_type=arg_type,
                default_value=default_val,
                required=required,
                valid_choices=valid_choices.get(arg_name),
                measure_dir_name=measure_dir_name,
            )
        )

    return arguments


def _introspect_python_measure(measure_dir: Path) -> list[MeasureArgument]:  # noqa: PLR0912, PLR0915
    """Parse argument definitions from a Python measure's ``measure.py`` file.

    Looks for ``MeasureArgument.new("name", ...)`` calls and extracts
    associated display_name, description, type, default_value, required,
    and valid_choices settings.
    """
    measure_py = measure_dir / "measure.py"
    if not measure_py.is_file():
        return []

    try:
        source = measure_py.read_text(encoding="utf-8")
    except OSError:
        return []

    arguments: list[MeasureArgument] = []
    measure_dir_name = measure_dir.name

    # Match argument variable assignments: var = ...Argument.new("name", ...)
    # Accepts OpenStudio.Measure.MeasureArgument, openstudio.OpenStudio.Measure.MeasureArgument,
    # os.Measure.MeasureArgument, Octopi::Argument, etc.
    arg_pattern = re.compile(
        r"(?P<lhs>\w+)\s*=\s*\S+Argument\.new\s*\(\s*[\"'](?P<name>[^\"']+)[\"']",
        re.MULTILINE,
    )

    raw_args: list[tuple[str, str, Any, bool]] = []  # (name, type, default, required)

    lines = source.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        m = arg_pattern.search(line)
        if m:
            var_name = m.group("lhs")  # the variable name (e.g., "lpd")
            arg_name = m.group(
                "name"
            )  # the OpenStudio argument name (e.g., "lighting_power_density")
            arg_type = "String"
            default_val: Any = None
            required = False

            j = i + 1
            while j < min(i + 20, len(lines)):
                next_line = lines[j]

                # setAttribute/setDefaultValue/setRequired are called on the variable (var_name), not the arg name
                if re.search(
                    rf"{re.escape(var_name)}.setAttribute\s*\(\s*\"type\"\s*,\s*(?P<type>\d)",
                    next_line,
                ):
                    type_map = {0: "Boolean", 1: "String", 2: "Double", 3: "Integer", 4: "Choice"}
                    tm2 = re.search(r"setAttribute\s*\(\s*\"type\"\s*,\s*(?P<type>\d+)", next_line)
                    if tm2:
                        arg_type = type_map.get(int(tm2.group("type")), "String")

                dm = re.search(
                    rf"{re.escape(var_name)}.setDefaultValue\s*\(\s*(?P<val>[^)]+?)\s*\)",
                    next_line,
                )
                if dm:
                    raw_val = dm.group("val").strip()
                    if raw_val in ("true", "false"):
                        default_val = raw_val == "true"
                    elif re.match(r"^-?\d+\.\d+$", raw_val):
                        default_val = float(raw_val)
                    elif re.match(r"^-?\d+$", raw_val):
                        default_val = int(raw_val)
                    elif re.match(r'^["\'](.*)["\']\s*$', raw_val):
                        default_val = re.match(r'^["\'](.*)["\']\s*$', raw_val).group(1)  # type: ignore[union-attr]

                if re.search(rf"{re.escape(var_name)}.setRequired\s*\(\s*True\s*\)", next_line):
                    required = True

                j += 1

            raw_args.append((arg_name, arg_type, default_val, required))
            i = j - 1
        i += 1

    # Second pass: display_name, description, valid_choices
    display_names: dict[str, str] = {}
    descriptions: dict[str, str] = {}
    valid_choices: dict[str, list[str]] = {}

    for line in lines:
        dm = re.search(r'(?P<oname>\w+)\.setDisplayName\s*\(\s*"([^"]+)"\s*\)', line)
        if dm:
            display_names[dm.group("oname")] = dm.group(2)

        dem = re.search(r'(?P<oname>\w+)\.setDescription\s*\(\s*"([^"]+)"\s*\)', line)
        if dem:
            descriptions[dem.group("oname")] = dem.group(2)

        vcm = re.search(r"(?P<oname>\w+)\.setValidValues\s*\(\s*\[([^\]]+)\]\s*\)", line)
        if vcm:
            choices_str = vcm.group(2)
            choices = [c.strip().strip('"').strip("'") for c in choices_str.split(",") if c.strip()]
            valid_choices[vcm.group("oname")] = choices

    for arg_name, arg_type, default_val, required in raw_args:
        arguments.append(
            MeasureArgument(
                name=arg_name,
                display_name=display_names.get(arg_name, arg_name),
                description=descriptions.get(arg_name),
                argument_type=arg_type,
                default_value=default_val,
                required=required,
                valid_choices=valid_choices.get(arg_name),
                measure_dir_name=measure_dir_name,
            )
        )

    return arguments


def _introspect_measure(measure_dir: Path) -> list[MeasureArgument]:
    """Introspect a measure directory for argument definitions.

    Tries Ruby first (``measure.rb``), then Python (``measure.py``).
    """
    args = _introspect_ruby_measure(measure_dir)
    if args:
        return args
    return _introspect_python_measure(measure_dir)


def _build_measure_info(
    measure_dir_name: str,
    workflow_args: dict[str, Any] | None,
    measure_dir: Path | None,
) -> MeasureInfo:
    """Build MeasureInfo from a measure directory and/or workflow.osw arguments."""
    description: str | None = None
    measure_type = "Model"
    arguments: list[MeasureArgument] = []

    if measure_dir is not None and measure_dir.is_dir():
        # Try to read description from measure.rb or measure.py
        for candidate in (measure_dir / "measure.rb", measure_dir / "measure.py"):
            if candidate.is_file():
                try:
                    src = candidate.read_text(encoding="utf-8")
                    # Extract first class/doc comment as description fallback
                    class_m = re.search(r"Class:\s*(.+?)\n", src)
                    if class_m:
                        description = class_m.group(1).strip()
                except OSError:
                    pass
                break

        # Introspect arguments from source
        arguments = _introspect_measure(measure_dir)

    # Synthesize minimal arguments from workflow defaults when not introspected
    if workflow_args is not None:
        introspected_names = {a.name for a in arguments}
        for arg_name, arg_value in workflow_args.items():
            if arg_name in introspected_names:
                continue
            arguments.append(
                MeasureArgument(
                    name=arg_name,
                    display_name=arg_name.replace("_", " ").title(),
                    default_value=arg_value,
                    required=False,
                    measure_dir_name=measure_dir_name,
                )
            )

    return MeasureInfo(
        measure_dir_name=measure_dir_name,
        display_name=measure_dir_name.replace("_", " ").replace("-", " ").title(),
        description=description,
        measure_type=measure_type,
        arguments=arguments,
    )


# ---------------------------------------------------------------------------
# GET /api/v1/measures — list all measures
# ---------------------------------------------------------------------------


@measures_router.get("/api/v1/measures", response_model=MeasureListResponse)
async def list_measures(request: Request) -> MeasureListResponse:
    """List all measures referenced in the campaign's ``workflow.osw``.

    Measures are discovered from the workflow steps.  For each measure,
    argument details are introspected from ``measure.rb`` (Ruby) or
    ``measure.py`` (Python) source files when they are accessible on disk.

    Returns an empty list if no ``workflow.osw`` is found.
    """
    outdir = _outdir_or_503(request)
    osw_path = _discover_workflow_osw(outdir)

    if osw_path is None:
        return MeasureListResponse(measures=[], total=0, source="none")

    workflow = _load_workflow(osw_path)
    measure_paths: list[str] = workflow.get("measure_paths", [])
    steps: list[dict[str, Any]] = workflow.get("steps", [])

    # Build unique (measure_dir_name → workflow_args) map from steps
    seen: dict[str, dict[str, Any] | None] = {}
    for step in steps:
        mdn = step.get("measure_dir_name", "")
        if mdn:
            seen[mdn] = step.get("arguments")

    osw_dir = osw_path.parent

    measures: list[MeasureInfo] = []
    for measure_dir_name, workflow_args in seen.items():
        measure_dir: Path | None = None
        # Try each configured measure_path
        for mp in measure_paths:
            candidate = (osw_dir / mp / measure_dir_name).resolve()
            if candidate.is_dir():
                measure_dir = candidate
                break
        # Fallback: standard OpenStudio measure locations
        if measure_dir is None:
            for subdir in ("measures", "resources/measures"):
                candidate = (osw_dir / subdir / measure_dir_name).resolve()
                if candidate.is_dir():
                    measure_dir = candidate
                    break

        measures.append(_build_measure_info(measure_dir_name, workflow_args, measure_dir))

    return MeasureListResponse(
        measures=measures,
        total=len(measures),
        source="workflow.osw",
    )


# ---------------------------------------------------------------------------
# GET /api/v1/measures/{measure_name} — measure detail
# ---------------------------------------------------------------------------


@measures_router.get("/api/v1/measures/{measure_name}", response_model=MeasureInfo)
async def get_measure(measure_name: str, request: Request) -> MeasureInfo:
    """Get detailed information for a single measure, including its arguments.

    The *measure_name* is the ``measure_dir_name`` from ``workflow.osw``.
    Returns 404 if the measure is not found in the workflow.
    """
    outdir = _outdir_or_503(request)
    osw_path = _discover_workflow_osw(outdir)

    if osw_path is None:
        raise HTTPException(
            status_code=404,
            detail="workflow.osw not found — cannot discover measures",
        )

    workflow = _load_workflow(osw_path)
    measure_paths: list[str] = workflow.get("measure_paths", [])
    steps: list[dict[str, Any]] = workflow.get("steps", [])

    # Find the matching step
    workflow_args: dict[str, Any] | None = None
    found = False
    for step in steps:
        if step.get("measure_dir_name") == measure_name:
            found = True
            workflow_args = step.get("arguments")
            break

    if not found:
        raise HTTPException(
            status_code=404,
            detail=f"Measure '{measure_name}' not found in workflow.osw",
        )

    # Resolve the measure directory
    osw_dir = osw_path.parent
    measure_dir: Path | None = None
    for mp in measure_paths:
        candidate = (osw_dir / mp / measure_name).resolve()
        if candidate.is_dir():
            measure_dir = candidate
            break

    if measure_dir is None:
        for subdir in ("measures", "resources/measures"):
            candidate = (osw_dir / subdir / measure_name).resolve()
            if candidate.is_dir():
                measure_dir = candidate
                break

    return _build_measure_info(measure_name, workflow_args, measure_dir)
