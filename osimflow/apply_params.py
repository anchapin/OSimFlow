"""Pure logic for applying a parameter set to a template simulation package.

This module is the *importable* core of `bin/apply_params_to_model.py`.
The CLI entry point is responsible for wrapping `import openstudio` in a
try/except (per AGENTS.md §6), but the logic itself is import-safe so it
can be unit-tested on hosts that do NOT have the OpenStudio Python
bindings installed.

The BYOS (Bring Your Own Script) contract lives here: a user-supplied
`apply(ctx)` function in `user_scripts/my_apply.py` has the same
interface as the default logic, and the Campaign can substitute it via
the `--custom_apply_script` flag.

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

import importlib
import importlib.util
import json
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger("osimflow.apply_params")


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
            ``None`` for .osm attributes.
    """

    name: str
    kind: str  # "attribute" | "measure_argument"
    default: Any = None
    step_index: int | None = None


class UnmappedParameterError(ValueError):
    """Raised when one or more LHS variables do not map to the template.

    The error message lists every unmapped name so the user can fix
    variables.yml in one pass.
    """


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
    if template.is_dir():
        osw = template / "workflow.osw"
        if osw.is_file():
            return osw
        osm = template / "model.osm"
        if osm.is_file():
            return osm
        raise ValueError(
            f"Template directory {template} contains neither workflow.osw nor model.osm"
        )
    return template


def _build_mappings(template: Path) -> dict[str, MappedParameter]:
    """Build the union of all parameter mappings in a template.

    For a single file: returns the mappings from that file alone.
    For a directory: returns the union of mappings from BOTH
    ``model.osm`` and ``workflow.osw`` if present. This is the
    most useful semantic: every LHS variable must map to *something*
    in the template package, and we want to discover all options.
    """
    if template.is_dir():
        mappings: dict[str, MappedParameter] = {}
        # .osw first so measure_arguments are preferred on name collision.
        osw = template / "workflow.osw"
        if osw.is_file():
            mappings.update(parse_osw_arguments(osw))
        osm = template / "model.osm"
        if osm.is_file():
            mappings.update(parse_osm_attributes(osm))
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
    osw = template / "workflow.osw"
    if osw.is_file():
        return osw
    osm = template / "model.osm"
    if osm.is_file():
        return osm
    raise ValueError(f"Template directory {template} contains neither workflow.osw nor model.osm")


# ---------------------------------------------------------------------------
# .osm parsing (test-mode JSON; in production, the OpenStudio bindings)
# ---------------------------------------------------------------------------
def parse_osm_attributes(template: Path) -> dict[str, MappedParameter]:
    """Parse an .osm file and return a name→MappedParameter map.

    In production this would use `openstudio.model.Model.load(template)`
    to walk the model and collect attribute names. For unit tests, we
    support a JSON convention: if the file content starts with ``{``, it
    is treated as ``{"attributes": {name: default, ...}}``.

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
        raise RuntimeError(
            "Cannot parse a binary .osm without the OpenStudio Python "
            "bindings. Either install `openstudio` or use the test-mode "
            "JSON convention (file content must start with '{')."
        )
    # If the bindings are available, we *should* be using them. But
    # building the full attribute index requires walking the model —
    # a non-trivial implementation. For the scope of this issue we
    # document the integration point and surface a clear error if the
    # user is on the production path.
    raise NotImplementedError(
        "Production .osm attribute index requires a full OpenStudio "
        "model walk; tracked separately. The test-mode JSON convention "
        "is fully supported."
    )


# ---------------------------------------------------------------------------
# .osw parsing
# ---------------------------------------------------------------------------
def parse_osw_arguments(template: Path) -> dict[str, MappedParameter]:
    """Parse an .osw file and return a name→MappedParameter map for all
    measure arguments across all steps.

    The .osw format is JSON: ``{"steps": [{"arguments": {name: value}}]}``.
    """
    try:
        data = json.loads(template.read_text())
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid .osw JSON in {template}: {exc}") from exc
    result: dict[str, MappedParameter] = {}
    for step_idx, step in enumerate(data.get("steps", [])):
        arguments = step.get("arguments", {})
        for name, default in arguments.items():
            # Last-wins on name collision across steps; the user can
            # disambiguate via a future step_name.name convention.
            result[str(name)] = MappedParameter(
                name=str(name),
                kind="measure_argument",
                default=default,
                step_index=step_idx,
            )
    return result


# ---------------------------------------------------------------------------
# Pre-flight check (PRD §1.4)
# ---------------------------------------------------------------------------
def preflight_check(
    parameters: dict[str, Any],
    mappings: dict[str, MappedParameter],
) -> None:
    """Raise UnmappedParameterError if any LHS variable is not in mappings.

    The error message lists every unmapped name so the user can fix
    variables.yml in one pass.
    """
    unmapped = sorted(name for name in parameters if name not in mappings)
    if unmapped:
        raise UnmappedParameterError(
            "Pre-flight check failed: the following LHS variables do not "
            "map to any template attribute or measure argument: "
            + ", ".join(unmapped)
            + ". Fix the variable names in variables.yml or extend the "
            "template to expose them."
        )


# ---------------------------------------------------------------------------
# Default logic: copy template to out/<sample_id>/, mutate in place
# ---------------------------------------------------------------------------
def _copy_template_artifacts(template: Path, out: Path) -> list[Path]:
    """Copy all template artifacts into the per-sample output directory.

    If `template` is a single .osm/.osw file, copy just that file into
    `out/<template.name>`. If it is a directory, recursively copy the
    whole directory contents (without the dir itself) into `out/`.
    Returns the list of destination paths actually created.
    """
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
    """Mutate a JSON-mode .osm file in place."""
    text = osm_path.read_text()
    if not text.lstrip().startswith("{"):
        # Real OpenStudio XML .osm: the CLI must have wired the bindings.
        # We refuse rather than corrupt the model silently.
        raise NotImplementedError(
            "Production .osm mutation requires the OpenStudio Python "
            "bindings; this code path is reached only on hosts without "
            "them. The CLI entry point must gate this."
        )
    data = json.loads(text)
    for name, value in parameters.items():
        if name in mappings and mappings[name].kind == "attribute":
            data.setdefault("attributes", {})[name] = value
    osm_path.write_text(json.dumps(data, indent=2, sort_keys=True))


def _mutate_osw(
    osw_path: Path,
    parameters: dict[str, Any],
    mappings: dict[str, MappedParameter],
) -> None:
    """Mutate an .osw file in place."""
    data = json.loads(osw_path.read_text())
    steps = data.setdefault("steps", [])
    for name, value in parameters.items():
        mapping = mappings.get(name)
        if mapping is None or mapping.kind != "measure_argument":
            continue
        step_index = mapping.step_index
        if step_index is None or step_index >= len(steps):
            continue
        steps[step_index].setdefault("arguments", {})[name] = value
    osw_path.write_text(json.dumps(data, indent=2, sort_keys=True))


# ---------------------------------------------------------------------------
# BYOS: custom_apply_script override
# ---------------------------------------------------------------------------
def _load_custom_apply(script_path: Path) -> Any:
    """Load a user script and return its `apply(ctx)` callable.

    The contract is defined in `user_scripts/README.md`:
    ``apply(ctx) -> dict`` with ``ctx = {"template_dir", "parameters",
    "sample_id", "openstudio", "out_dir"}``.
    """
    spec = importlib.util.spec_from_file_location("user_apply", str(script_path))
    if spec is None or spec.loader is None:
        raise ValueError(f"Cannot load custom_apply_script {script_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    apply_fn = getattr(module, "apply", None)
    if apply_fn is None or not callable(apply_fn):
        raise ValueError(
            f"custom_apply_script {script_path} must define an "
            f"`apply(ctx) -> dict` function (see user_scripts/README.md)."
        )
    return apply_fn


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
def apply_parameters(
    template: Path,
    parameters: dict[str, Any],
    sample_id: str,
    out: Path,
    custom_apply_script: Path | None = None,
) -> Path:
    """Apply a parameter set to a template and write the per-sample output.

    Args:
        template: the template_sim_package file (.osm or .osw).
        parameters: the LHS dict for this sample, e.g. ``{"wwr": 0.4}``.
        sample_id: the sample's identifier, e.g. ``"0001"``.
        out: the per-sample output directory. Created if missing.
        custom_apply_script: optional user-supplied override (BYOS).

    Returns:
        The per-sample output directory (``out``).

    Raises:
        UnmappedParameterError: at least one LHS variable does not map
            to a real template attribute or measure argument. This is
            enforced *before* any writes happen.
        NotImplementedError: the template type is supported for parsing
            but not for mutation (production .osm XML path).
    """
    original_template = template
    # Build the union of mappings from BOTH files (if directory).
    mappings = _build_mappings(template)
    # The file we will mutate (or that the user_ctx references).
    target_file = _select_template_file(template)
    template_type = detect_template_type(target_file)

    # Pre-flight (PRD §1.4). We do this BEFORE custom-script dispatch so
    # the user always gets the canonical pre-flight check, regardless of
    # whether they supplied a BYOS override.
    preflight_check(parameters, mappings)

    if custom_apply_script is not None:
        log.info(
            "apply_parameters: dispatching to custom_apply_script %s for sample_id=%s",
            custom_apply_script,
            sample_id,
        )
        user_apply = _load_custom_apply(custom_apply_script)
        # BYOS contract: ctx contains all the inputs the user needs.
        # We pass the openstudio module only if it is importable —
        # never raise here, the user script is responsible for
        # handling the missing-bindings case.
        openstudio_mod = None
        if importlib.util.find_spec("openstudio") is not None:
            import openstudio as _os  # noqa: PLC0415

            openstudio_mod = _os
        # Copy the template artifacts so the BYOS user can write into
        # `out_dir` (the per-sample directory) without us putting anything
        # else there. The user is expected to manage the full file layout.
        if not out.exists():
            _copy_template_artifacts(original_template, out)
        ctx = {
            "template_dir": original_template
            if original_template.is_dir()
            else original_template.parent,
            "template_path": target_file,
            "parameters": parameters,
            "sample_id": sample_id,
            "openstudio": openstudio_mod,
            "out_dir": out,
        }
        user_apply(ctx)
        return out

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
    return out
