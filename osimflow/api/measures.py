"""Measure discovery and introspection API endpoints (issue #348).

Provides:
  - GET  /api/v1/measures              — list all measures used in a campaign
  - GET  /api/v1/measures/{name}       — get detailed measure info and arguments

Measures are discovered from the campaign's ``workflow.osw`` and their
arguments are introspected by parsing ``measure.rb`` (Ruby) or
``measure.py`` (Python) files found in the configured ``measure_paths``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
import tarfile
import tempfile
import uuid
import zipfile
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, UploadFile

from osimflow.api.schemas import (
    MeasureArgument,
    MeasureDetailResponse,
    MeasureInfo,
    MeasureListResponse,
    MeasureMetadataUpdate,
    MeasureUploadResponse,
)

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
# Uploaded-measures registry helpers
# ---------------------------------------------------------------------------


def _measures_dir_or_503(request: Request) -> Path:
    """Return the configured measures_dir or raise 503."""
    measures_dir: Path | None = getattr(request.app.state, "measures_dir", None)
    if measures_dir is None:
        raise HTTPException(status_code=503, detail="No measures directory configured")
    return measures_dir


def _load_measures_registry(measures_dir: Path) -> dict[str, Any]:
    """Load the measures registry JSON file, returning an empty dict if missing."""
    registry_path = measures_dir / "measures_registry.json"
    if not registry_path.is_file():
        return {}
    try:
        return json.loads(registry_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save_measures_registry(measures_dir: Path, registry: dict[str, Any]) -> None:
    """Atomically write the measures registry JSON file."""
    measures_dir.mkdir(parents=True, exist_ok=True)
    registry_path = measures_dir / "measures_registry.json"
    tmp_path = measures_dir / f".measures_registry.json.tmp.{uuid.uuid4().hex}"
    try:
        tmp_path.write_text(json.dumps(registry, indent=2), encoding="utf-8")
        tmp_path.rename(registry_path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def _compute_version_uuid(content: bytes) -> str:
    """Compute a version UUID from measure source content using SHA-256 hash."""
    return hashlib.sha256(content).hexdigest()[:16]


def _extract_measure_archive(
    archive_path: Path,
    dest_dir: Path,
) -> Path:
    """Extract a zip or tar.gz archive into dest_dir.

    Returns the path to the extracted measure directory (the first directory
    inside the archive that contains measure.rb or measure.py).
    Raises ValueError if no measure is found.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)

    if zipfile.is_zipfile(archive_path):
        with zipfile.ZipFile(archive_path, "r") as zf:
            # Security: reject zip bombs and path traversal attempts
            for member in zf.infolist():
                if member.is_dir():
                    continue
                # Reject any member that writes outside dest_dir
                member_path = (dest_dir / member.filename).resolve()
                if not member_path.is_relative_to(dest_dir.resolve()):
                    raise ValueError(f"Archive member {member.filename} escapes extraction directory")
            zf.extractall(dest_dir)
    elif tarfile.is_tarfile(archive_path):
        with tarfile.open(archive_path, "r:*") as tf:
            for member in tf.getmembers():
                if member.isfile():
                    member_path = (dest_dir / member.name).resolve()
                    if not member_path.is_relative_to(dest_dir.resolve()):
                        raise ValueError(f"Archive member {member.name} escapes extraction directory")
            tf.extractall(dest_dir)
    else:
        raise ValueError("Archive is neither a valid zip nor tar.gz file")

    # Find the measure directory inside the extracted archive
    for item in sorted(dest_dir.iterdir()):
        if item.is_dir() and (
            (item / "measure.rb").is_file() or (item / "measure.py").is_file()
        ):
            return item
    raise ValueError("Archive does not contain a directory with measure.rb or measure.py")


# ---------------------------------------------------------------------------
# POST /api/v1/measures/upload — upload a measure bundle
# ---------------------------------------------------------------------------


@measures_router.post("/api/v1/measures/upload", response_model=MeasureUploadResponse)
async def upload_measure(request: Request, file: UploadFile) -> MeasureUploadResponse:  # noqa: PLR0912
    """Upload a measure bundle (zip or tar.gz).

    The bundle must contain a ``measure.rb`` (Ruby) or ``measure.py`` (Python)
    file inside a named directory. Optional ``tests/`` and ``resources/``
    subdirectories are also accepted.

    The measure is introspected, stored in the configured measures directory,
    and registered with a content-based ``version_uuid`` for idempotent re-upload.
    """
    measures_dir = _measures_dir_or_503(request)

    # Read uploaded file content into memory
    try:
        content = await file.read()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Failed to read uploaded file: {exc}") from exc

    if not content:
        raise HTTPException(status_code=400, detail="Uploaded file is empty")

    # Write to a temp file for extraction
    with tempfile.NamedTemporaryFile(suffix=file.filename or ".zip", delete=False) as tmp:
        tmp.write(content)
        archive_path = Path(tmp.name)

    try:
        # Determine where to extract — use a temp dir first to validate
        with tempfile.TemporaryDirectory() as tmp_extract:
            extract_base = Path(tmp_extract)
            try:
                measure_dir = _extract_measure_archive(archive_path, extract_base)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

            measure_name = measure_dir.name
            measure_rb = measure_dir / "measure.rb"
            measure_py = measure_dir / "measure.py"

            if measure_rb.is_file():
                source_content = measure_rb.read_bytes()
                version_uuid = _compute_version_uuid(source_content)
            elif measure_py.is_file():
                source_content = measure_py.read_bytes()
                version_uuid = _compute_version_uuid(source_content)
            else:
                raise HTTPException(
                    status_code=400,
                    detail="Archive must contain measure.rb or measure.py at the top level of a directory",
                )

            # Introspect arguments
            arguments = _introspect_measure(measure_dir)

            # Check for existing measure with same version_uuid
            registry = _load_measures_registry(measures_dir)
            for mid, mentry in registry.items():
                if mentry.get("version_uuid") == version_uuid:
                    # Idempotent: return existing measure
                    return MeasureUploadResponse(
                        measure_id=mid,
                        name=mentry["name"],
                        version_uuid=version_uuid,
                        argument_count=len(mentry.get("arguments", [])),
                        detail=f"Measure already exists (idempotent re-upload): {mentry['name']}",
                    )

            # Allocate new measure_id
            measure_id = str(uuid.uuid4())

            # Persist to measures_dir
            persist_dir = measures_dir / measure_id
            persist_dir.mkdir(parents=True, exist_ok=True)

            # Copy measure directory contents
            for item in measure_dir.iterdir():
                dst = persist_dir / item.name
                if item.is_dir():
                    shutil.copytree(item, dst, dirs_exist_ok=True)
                else:
                    shutil.copy2(item, dst)

            # Write registry entry
            registry[measure_id] = {
                "name": measure_name,
                "version_uuid": version_uuid,
                "taxonomy": None,
                "description": None,
                "tags": [],
                "measure_group": None,
                "arguments": [a.model_dump() for a in arguments],
                "is_uploaded": True,
            }
            _save_measures_registry(measures_dir, registry)

            return MeasureUploadResponse(
                measure_id=measure_id,
                name=measure_name,
                version_uuid=version_uuid,
                argument_count=len(arguments),
                detail=f"Measure uploaded successfully: {measure_name}",
            )
    finally:
        if archive_path.exists():
            archive_path.unlink()


# ---------------------------------------------------------------------------
# GET /api/v1/measures — list all measures (enhanced with search/taxonomy/tag)
# ---------------------------------------------------------------------------


def _discover_workflow_measures(outdir: Path) -> tuple[list[MeasureInfo], str]:
    """Discover measures from the workflow.osw file.

    Returns a tuple of (measures_list, source_label).
    Returns ([], "") when no workflow.osw is found.
    """
    osw_path = _discover_workflow_osw(outdir)
    if osw_path is None:
        return [], ""

    workflow = _load_workflow(osw_path)
    measure_paths: list[str] = workflow.get("measure_paths", [])
    steps: list[dict[str, Any]] = workflow.get("steps", [])

    seen: dict[str, dict[str, Any] | None] = {}
    for step in steps:
        mdn = step.get("measure_dir_name", "")
        if mdn:
            seen[mdn] = step.get("arguments")

    osw_dir = osw_path.parent
    measures: list[MeasureInfo] = []

    for measure_dir_name, workflow_args in seen.items():
        measure_dir: Path | None = None
        for mp in measure_paths:
            candidate = (osw_dir / mp / measure_dir_name).resolve()
            if candidate.is_dir():
                measure_dir = candidate
                break
        if measure_dir is None:
            for subdir in ("measures", "resources/measures"):
                candidate = (osw_dir / subdir / measure_dir_name).resolve()
                if candidate.is_dir():
                    measure_dir = candidate
                    break
        measures.append(_build_measure_info(measure_dir_name, workflow_args, measure_dir))

    return measures, "workflow.osw"


def _discover_uploaded_measures(
    measures_dir: Path,
    search: str | None,
    taxonomy: str | None,
    tag: str | None,
) -> tuple[list[MeasureDetailResponse], bool]:
    """Discover and filter uploaded measures from the measures registry.

    Returns a tuple of (filtered_measure_list, has_any).
    """
    if not measures_dir.is_dir():
        return [], False

    registry = _load_measures_registry(measures_dir)
    if not registry:
        return [], False

    results: list[MeasureDetailResponse] = []
    for mid, mentry in registry.items():
        if taxonomy is not None and not (mentry.get("taxonomy") or "").startswith(taxonomy):
            continue
        if tag is not None and tag not in mentry.get("tags", []):
            continue
        if search is not None:
            hay = (mentry.get("name", "") + " " + (mentry.get("description") or "")).lower()
            if search.lower() not in hay:
                continue
        args = [
            MeasureArgument(**a) for a in mentry.get("arguments", [])
            if isinstance(a, dict)
        ]
        results.append(
            MeasureDetailResponse(
                measure_id=mid,
                name=mentry["name"],
                version_uuid=mentry.get("version_uuid", ""),
                taxonomy=mentry.get("taxonomy"),
                description=mentry.get("description"),
                tags=mentry.get("tags", []),
                measure_group=mentry.get("measure_group"),
                arguments=args,
                is_uploaded=True,
            )
        )
    return results, True


@measures_router.get("/api/v1/measures", response_model=MeasureListResponse)
async def list_measures(
    request: Request,
    search: str | None = Query(None, description="Search in measure name and description"),
    taxonomy: str | None = Query(None, description="Filter by taxonomy prefix (e.g. Economics)"),
    tag: str | None = Query(None, description="Filter by tag"),
) -> MeasureListResponse:
    """List all measures referenced in the campaign's ``workflow.osw`` **and**
    uploaded measures from the measures directory.

    For workflow measures, argument details are introspected from
    ``measure.rb`` (Ruby) or ``measure.py`` (Python) source files.

    Supports filtering across both sources:
    - ``search``: substring match in measure name and description
    - ``taxonomy``: prefix match on the taxonomy field (uploaded measures only)
    - ``tag``: exact tag match (uploaded measures only)

    Returns an empty list if neither workflow.osw nor uploaded measures exist.
    """
    outdir: Path | None = getattr(request.app.state, "outdir", None)
    measures_dir: Path | None = getattr(request.app.state, "measures_dir", None)
    all_measures: list[MeasureInfo] = []
    source_parts: list[str] = []

    # --- Workflow measures ---
    if outdir is not None:
        wf_measures, wf_source = _discover_workflow_measures(outdir)
        if wf_measures:
            all_measures.extend(wf_measures)
            source_parts.append(wf_source)

    # --- Uploaded measures ---
    uploaded: list[MeasureDetailResponse] = []
    if measures_dir is not None:
        uploaded, has_uploaded = _discover_uploaded_measures(
            measures_dir, search, taxonomy, tag
        )
        if has_uploaded:
            source_parts.append("uploaded")

    # Neither source available: raise 503 to stay consistent with the
    # original behaviour where a missing outdir → 503.
    if outdir is None and measures_dir is None:
        raise HTTPException(status_code=503, detail="No output directory or measures directory configured")
        uploaded, has_uploaded = _discover_uploaded_measures(
            measures_dir, search, taxonomy, tag
        )
        if has_uploaded:
            source_parts.append("uploaded")

    # Merge: convert uploaded MeasureDetailResponse to MeasureInfo for response shape
    for um in uploaded:
        all_measures.append(
            MeasureInfo(
                measure_dir_name=um.name,
                display_name=um.name.replace("_", " ").replace("-", " ").title(),
                description=um.description,
                measure_type="Model",
                arguments=um.arguments,
            )
        )

    # Apply search filter to workflow measures (uploaded already filtered)
    if search is not None and not uploaded:
        hay = search.lower()
        all_measures = [
            m
            for m in all_measures
            if hay in m.measure_dir_name.lower()
            or (m.description and hay in m.description.lower())
        ]

    if not source_parts:
        source = "none"
    elif len(source_parts) == 2:
        source = "workflow.osw+uploaded"
    else:
        source = source_parts[0]

    return MeasureListResponse(
        measures=all_measures,
        total=len(all_measures),
        source=source,
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


# ---------------------------------------------------------------------------
# Uploaded-measure CRUD (uuid-based)
# ---------------------------------------------------------------------------


def _get_uploaded_measure(
    request: Request,
    measure_id: str,
) -> tuple[Path, dict[str, Any]]:
    """Load an uploaded measure entry from the registry.

    Returns (measure_dir, registry_entry). Raises HTTPException 404 if not found.
    """
    measures_dir = _measures_dir_or_503(request)
    registry = _load_measures_registry(measures_dir)
    if measure_id not in registry:
        raise HTTPException(
            status_code=404,
            detail=f"Measure '{measure_id}' not found in uploaded measures registry",
        )
    measure_dir = measures_dir / measure_id
    if not measure_dir.is_dir():
        raise HTTPException(
            status_code=404,
            detail=f"Measure directory for '{measure_id}' not found on disk",
        )
    return measure_dir, registry[measure_id]


@measures_router.get(
    "/api/v1/measures/by-id/{measure_id}",
    response_model=MeasureDetailResponse,
)
async def get_uploaded_measure(
    measure_id: str,
    request: Request,
) -> MeasureDetailResponse:
    """Get full metadata for an uploaded measure by its UUID.

    Returns 404 for workflow-discovered measures (use ``GET /api/v1/measures/{name}`` instead).
    """
    _measures_dir_or_503(request)  # Ensure measures_dir is configured
    _, entry = _get_uploaded_measure(request, measure_id)
    args = [MeasureArgument(**a) for a in entry.get("arguments", []) if isinstance(a, dict)]
    return MeasureDetailResponse(
        measure_id=measure_id,
        name=entry["name"],
        version_uuid=entry.get("version_uuid", ""),
        taxonomy=entry.get("taxonomy"),
        description=entry.get("description"),
        tags=entry.get("tags", []),
        measure_group=entry.get("measure_group"),
        arguments=args,
        is_uploaded=True,
    )


@measures_router.patch(
    "/api/v1/measures/by-id/{measure_id}",
    response_model=MeasureDetailResponse,
)
async def patch_uploaded_measure(
    measure_id: str,
    request: Request,
    update: MeasureMetadataUpdate,
) -> MeasureDetailResponse:
    """Update metadata for an uploaded measure.

    Supports updating: ``taxonomy``, ``description``, ``tags``, ``measure_group``.
    Returns 404 for workflow-discovered measures.
    """
    measures_dir = _measures_dir_or_503(request)
    registry = _load_measures_registry(measures_dir)
    if measure_id not in registry:
        raise HTTPException(
            status_code=404,
            detail=f"Measure '{measure_id}' not found in uploaded measures registry",
        )

    entry = registry[measure_id]
    if update.taxonomy is not None:
        entry["taxonomy"] = update.taxonomy
    if update.description is not None:
        entry["description"] = update.description
    if update.tags is not None:
        entry["tags"] = update.tags
    if update.measure_group is not None:
        entry["measure_group"] = update.measure_group

    registry[measure_id] = entry
    _save_measures_registry(measures_dir, registry)

    args = [MeasureArgument(**a) for a in entry.get("arguments", []) if isinstance(a, dict)]
    return MeasureDetailResponse(
        measure_id=measure_id,
        name=entry["name"],
        version_uuid=entry.get("version_uuid", ""),
        taxonomy=entry.get("taxonomy"),
        description=entry.get("description"),
        tags=entry.get("tags", []),
        measure_group=entry.get("measure_group"),
        arguments=args,
        is_uploaded=True,
    )


@measures_router.delete(
    "/api/v1/measures/by-id/{measure_id}",
)
async def delete_uploaded_measure(
    measure_id: str,
    request: Request,
) -> dict[str, str]:
    """Delete an uploaded measure.

    Refuses to delete workflow-discovered measures (returns 403).
    Returns 404 if the measure_id is not in the uploaded registry.
    """
    measures_dir = _measures_dir_or_503(request)
    registry = _load_measures_registry(measures_dir)
    if measure_id not in registry:
        raise HTTPException(
            status_code=404,
            detail=f"Measure '{measure_id}' not found in uploaded measures registry",
        )

    entry = registry[measure_id]
    if not entry.get("is_uploaded", False):
        raise HTTPException(
            status_code=403,
            detail="Cannot delete workflow-discovered measures via this endpoint",
        )

    # Remove from registry
    del registry[measure_id]
    _save_measures_registry(measures_dir, registry)

    # Remove measure directory
    measure_dir = measures_dir / measure_id

    if measure_dir.is_dir():
        shutil.rmtree(measure_dir)

    return {"measure_id": measure_id, "status": "deleted"}
