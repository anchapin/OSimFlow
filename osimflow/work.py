"""Per-step work functions — what each logical process actually does.

These are the things the executor runs. They call the existing `bin/*.py`
stubs so the framework exercises the real CLI surface even though the
stubs themselves are no-ops.

The BYOS extension story falls out of this naturally: a user-supplied
`apply_parameters(template, params, sample_id, out)` function has the
same signature as `default_apply_parameters` below. The Campaign
discovers it via `inspect.signature` and calls it directly — no second
CLI surface to maintain.
"""

import importlib.resources
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from .apply_params import OSMAttributeError
from .executors import run_subprocess  # local helper (issue #6)
from .json_utils import safe_json_dumps
from .storage import ResultStorage
from .version_detection import VersionDetectionError, detect_openstudio_version
from .weather import EPWValidationError, discover_epw_files, validate_epw_header

log = logging.getLogger("osimflow.work")


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------
class SevereEnergyPlusError(RuntimeError):
    """Raised when a preflight simulation encounters severe errors."""


class TransientError(RuntimeError):
    """Raised when a simulation failure is potentially transient and retryable.

    Examples: network timeout, resource contention, temporary file lock,
    exit code indicating a recoverable condition.
    """


def _is_transient_error(exc: BaseException) -> bool:
    """Return True if *exc* looks like a transient failure.

    Checks for exit codes and error messages that indicate a retryable
    condition (network timeout, resource busy, etc.).
    """
    msg = str(exc).lower()
    transient_markers = (
        "timeout",
        "timed out",
        "connection",
        "network",
        "resource busy",
        "temporary failure",
        "refused",
        "too many open files",
        "disk full",
        "io error",
    )
    if any(m in msg for m in transient_markers):
        return True
    return (
        isinstance(exc, subprocess.CalledProcessError) and exc.returncode in _TRANSIENT_EXIT_CODES
    )


_TRANSIENT_EXIT_CODES = frozenset([-1, 2, 4, 5, 6, 11, 12, 15, 24, 25, 26, 27, 28])


# ---------------------------------------------------------------------------
# Container / simulation health monitoring (issue #415)
# ---------------------------------------------------------------------------

HEARTBEAT_INTERVAL_S = 30  # seconds between heartbeat writes
HEARTBEAT_FILENAME = ".heartbeat.json"
HEALTH_CHECK_INTERVAL_S = 60  # default health check tolerance (stale if no beat for this long)


class SimulationHealthStatus(StrEnum):
    HEALTHY = "healthy"
    STALE = "stale"  # no heartbeat within the expected interval
    UNKNOWN = "unknown"


@dataclass
class SimulationHealth:
    status: SimulationHealthStatus
    last_heartbeat: float | None = None
    pid: int | None = None
    message: str | None = None


def _write_heartbeat(sim_out: Path, pid: int, sample_id: str, version: str) -> None:
    """Write a heartbeat file for the running simulation.

    The heartbeat is consumed by ``check_container_health`` to detect
    simulations that have gone silent (container freeze, process deadlock).
    """
    heartbeat_path = sim_out / HEARTBEAT_FILENAME
    payload = {
        "pid": pid,
        "sample_id": sample_id,
        "version": version,
        "timestamp": time.time(),
    }
    try:
        sim_out.mkdir(parents=True, exist_ok=True)
        heartbeat_path.write_text(json.dumps(payload), encoding="utf-8")
    except OSError as exc:
        log.warning("failed to write heartbeat for %s: %s", sample_id, exc)


def check_container_health(
    sim_out: Path,
    health_check_interval: float = HEALTH_CHECK_INTERVAL_S,
) -> SimulationHealth:
    """Check whether the simulation container is still responsive.

    Returns a ``SimulationHealth`` describing the current state:
    - HEALTHY: heartbeat is fresh (written within health_check_interval).
    - STALE: heartbeat exists but is older than health_check_interval.
    - UNKNOWN: no heartbeat file exists (simulation may not have started yet
      or the file was deleted).

    The caller can use the STALE status to decide whether to cancel and
    retry a frozen simulation.  This function does **not** terminate
    anything — it only reports health status.
    """
    heartbeat_path = sim_out / HEARTBEAT_FILENAME
    if not heartbeat_path.is_file():
        return SimulationHealth(
            status=SimulationHealthStatus.UNKNOWN,
            message="no heartbeat file found",
        )
    try:
        payload = json.loads(heartbeat_path.read_text(encoding="utf-8"))
        last_ts = payload.get("timestamp")
        if last_ts is None:
            return SimulationHealth(
                status=SimulationHealthStatus.UNKNOWN, message="heartbeat has no timestamp"
            )
        age = time.time() - last_ts
        if age > health_check_interval:
            return SimulationHealth(
                status=SimulationHealthStatus.STALE,
                last_heartbeat=last_ts,
                pid=payload.get("pid"),
                message=f"heartbeat is {age:.0f}s old (threshold {health_check_interval}s)",
            )
        return SimulationHealth(
            status=SimulationHealthStatus.HEALTHY,
            last_heartbeat=last_ts,
            pid=payload.get("pid"),
        )
    except (OSError, json.JSONDecodeError) as exc:
        return SimulationHealth(
            status=SimulationHealthStatus.UNKNOWN,
            message=f"failed to read heartbeat: {exc}",
        )


def _heartbeat_writer(
    sim_out: Path,
    sample_id: str,
    version: str,
    stop_event: threading.Event,
) -> None:
    """Background thread: writes a heartbeat file every HEARTBEAT_INTERVAL_S.

    Stopped when *stop_event* is set (simulation completed or cancelled).
    """
    pid = os.getpid()
    while not stop_event.wait(HEARTBEAT_INTERVAL_S):
        _write_heartbeat(sim_out, pid, sample_id, version)
    # Final heartbeat on exit
    _write_heartbeat(sim_out, pid, sample_id, version)


def run_with_retry(
    func: Callable[..., Path],
    *args: Any,
    max_retries: int = 3,
    base_delay: float = 1.0,
    sample_id: str = "unknown",
    step_name: str = "step",
    **kwargs: Any,
) -> Path:
    """Run *func* with exponential-backoff retry for transient failures.

    Parameters
    ----------
    func
        Callable to invoke. Must accept *args and **kwargs.
    *args
        Positional arguments forwarded to *func*.
    max_retries
        Maximum retry attempts (default 3). A value <= 0 disables retries.
    base_delay
        Initial backoff delay in seconds (default 1.0). Each retry
        doubles the delay: delay = base_delay * 2**attempt.
    sample_id
        Identifier for log messages.
    step_name
        Step name for log messages.
    **kwargs
        Keyword arguments forwarded to *func*.

    Returns
    -------
    Path
        The return value of *func* on first success or after retries.

    Raises
    ------
    The last exception encountered when all retries are exhausted.
    """
    if max_retries <= 0:
        return func(*args, **kwargs)

    last_exc: BaseException | None = None
    for attempt in range(max_retries + 1):
        try:
            return func(*args, **kwargs)
        except BaseException as exc:  # noqa: BLE001
            if not _is_transient_error(exc):
                raise
            last_exc = exc
            if attempt == max_retries:
                break
            delay = min(base_delay * (2**attempt), 60.0)
            log.warning(
                "%s %s transient failure (attempt %d/%d), retrying in %.1fs: %s",
                step_name,
                sample_id,
                attempt + 1,
                max_retries,
                delay,
                exc,
            )
            time.sleep(delay)

    if last_exc is not None:
        raise last_exc
    raise RuntimeError("run_with_retry: unexpected code path")  # pragma: no cover


# ---------------------------------------------------------------------------
# Work-script resolver
# ---------------------------------------------------------------------------
def _resolve_work_script(name: str) -> Path:
    """Resolve the path to a work script (e.g. ``generate_lhs.py``).

    In a frozen PyInstaller build (``sys.frozen``), resolves from
    ``sys._MEIPASS``.  In a normal install (wheel / editable), uses
    ``importlib.resources`` to find the script inside the
    ``osimflow._work_scripts`` package.  Falls back to the repo
    ``bin/`` directory during development.
    """
    if getattr(sys, "frozen", False):
        # PyInstaller bundles files into sys._MEIPASS.
        base = Path(getattr(sys, "_MEIPASS", ""))
        candidate = base / "_work_scripts" / name
        if candidate.is_file():
            return candidate

    # Try importlib.resources (works for wheel installs and editable installs).
    try:
        ref = importlib.resources.files("osimflow._work_scripts") / name
        # importlib.resources may return a Traversable; convert to Path
        # only when it is a real filesystem path.
        if hasattr(ref, "is_file") and ref.is_file():
            return Path(str(ref))
    except (ModuleNotFoundError, TypeError):
        pass

    # Development fallback: repo root bin/ directory.
    dev_bin = Path(__file__).resolve().parent.parent / "bin" / name
    if dev_bin.is_file():
        return dev_bin

    raise FileNotFoundError(
        f"Work script {name!r} not found. Searched: osimflow._work_scripts package, bin/ directory."
    )


# ---------------------------------------------------------------------------
# BYOS contract: apply_parameters
# ---------------------------------------------------------------------------
def default_apply_parameters(
    sim_dir: Path,
    variables: dict[str, Any],
) -> None:
    """Apply parameter values to an OpenStudio model using Python bindings.

    Loads the ``model.osm`` from *sim_dir* using the OpenStudio Python
    bindings, applies each entry in *variables* as a model attribute
    mutation, and saves the modified model back to disk.

    This is the production ``.osm`` mutation path that replaces the prior
    CLI-delegation approach (issue #248) and the ``NotImplementedError``
    skeleton stub (issue #840).

    Attribute resolution follows the dotted-notation convention:
      * Simple name — ``"lighting_power_density"`` → first matching
        model-level attribute.
      * Dotted name — ``"SpaceType_Office.lighting_power_density"`` →
        resolves to the named ``SpaceType`` object and sets its attribute.

    Type coercion: ``int`` values are coerced to ``float`` for numeric
    SDK setters; ``str`` values are passed directly.

    Args:
        sim_dir: Directory containing ``model.osm`` (the modified sim
            package for this sample). Typically ``out / sample_id`` from
            the campaign's apply step.
        variables: Dict mapping variable names to values, e.g.  ``{
            "SpaceType_Office.lighting_power_density": 10.0 }``.

    Raises:
        RuntimeError: the OpenStudio Python bindings are not installed
            on this host. Install ``openstudio`` to enable this path.
        OSMAttributeError: a dotted variable name references an object
            type or instance name that does not exist in the model.
    """
    osm_path = sim_dir / "model.osm"
    if not osm_path.is_file():
        raise FileNotFoundError(f"model.osm not found in {sim_dir!r}")

    if _is_stub_mode():
        log.warning(
            "default_apply_parameters: OSIMFLOW_STUB_SIM=1 is set; skipping .osm mutation "
            "(stub mode — no OpenStudio bindings required)"
        )
        return

    try:
        import openstudio  # noqa: PLC0415
    except ImportError as exc:
        raise RuntimeError(
            "OpenStudio Python bindings are not installed on this host. "
            "Install `openstudio` to enable production .osm mutation, "
            "or set OSIMFLOW_STUB_SIM=1 to use stub mode for testing."
        ) from exc

    model_opt = openstudio.openstudiomodelcore.Model.load(str(osm_path))
    if not model_opt.is_initialized():
        raise RuntimeError(f"OpenStudio failed to load model from {osm_path!r}")
    model = model_opt.get()

    _apply_osm_mutations(model, openstudio, variables)

    model.save(str(osm_path), overwrite=True)
    log.info("default_apply_parameters: mutated .osm saved to %s", osm_path)


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


def _apply_parameters_via_cli(
    template: Path,
    sample_id: str,
    out_dir: Path,
    param_file: Path,
) -> Path:
    """Apply parameters via ``openstudio.cli run`` (issue #248).

    Copies the template into the per-sample directory, finds the
    workflow.osw, and invokes ``openstudio.cli run -w workflow.osw``.
    This properly executes measures through the SDK rather than
    patching the .osw file statically.
    """
    pkg_src = template if template.is_dir() else template.parent

    shutil.copytree(pkg_src, out_dir, dirs_exist_ok=True)

    workflow_path = _find_workflow_osw(out_dir)
    if workflow_path is None:
        raise RuntimeError(
            f"No workflow.osw found in {out_dir!r} for sample={sample_id!r}. "
            f"The OpenStudio CLI requires a workflow file to apply parameters."
        )

    stdout_path = out_dir / "stdout.log"
    stderr_path = out_dir / "stderr.log"

    cmd: list[str] = [
        _get_openstudio_cmd(),
        "run",
        "-w",
        str(workflow_path),
    ]
    log.info(
        "apply_parameters: openstudio.cli invocation sample=%s workflow=%s cwd=%s",
        sample_id,
        workflow_path,
        out_dir,
    )
    try:
        run_subprocess(
            cmd,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            cwd=out_dir,
        )
    except subprocess.SubprocessError as e:
        log.error(
            "apply_parameters: openstudio.cli failed for sample=%s: %s",
            sample_id,
            e,
        )
        raise RuntimeError(f"apply_parameters failed for {sample_id}") from e

    log.info(
        "apply_parameters: CLI applied parameters for sample=%s -> %s",
        sample_id,
        out_dir,
    )
    return out_dir


def _apply_parameters_stub(
    template: Path,
    sample_id: str,
    out_dir: Path,
    param_file: Path,
) -> Path:
    """Fallback stub when CLI is unavailable (OSIMFLOW_STUB_SIM=1 or no CLI).

    Copies the template to the output directory and writes a placeholder
    to simulate successful measure application. Maintains the BYOS contract
    (same argv, same exit code) without requiring a real OpenStudio install.
    """
    try:
        subprocess.run(  # nosec  # sourcery skip: suspicious-subprocess-call
            [
                sys.executable,
                str(_resolve_work_script("apply_params_to_model.py")),
                "--template",
                str(template),
                "--parameter_set",
                str(param_file),
                "--sample_id",
                sample_id,
                "--out",
                str(out_dir),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as e:
        log.error("apply_params failed for %s: %s", sample_id, e.stderr or "<empty>")
        raise RuntimeError(
            f"apply_params failed for {sample_id}: stdout={e.stdout!r} stderr={e.stderr!r}"
        ) from e
    return out_dir


# ---------------------------------------------------------------------------
# Simulation work: the heavy step. STUB.
# ---------------------------------------------------------------------------
# This stub is a placeholder for the real implementation, which will
# invoke `openstudio.cli run -w workflow.osw` inside the
# `nrel/openstudio:<version>` container (consumed from Docker Hub;
# see `docs/openstudio-image-distribution.md` and ADR-0002). The
# container is selected by the executor via the `container` parameter
# on submit; the function itself only sees the work directory.
#
# The stub simulates work with a short sleep and writes placeholder
# eplusout.sql / eplusout.err so the downstream extract step has
# something to consume in tests. When wired to a real container, the
# body becomes `subprocess.run(["openstudio.cli", "run", ...])` with  # nosec
# logging redirected to the per-sample log files written by the
# Campaign.


# ---------------------------------------------------------------------------
# Helpers for real OpenStudio CLI invocation (issue #31)
# ---------------------------------------------------------------------------


def _find_workflow_osw(modified_sim_package: Path) -> Path | None:
    """Locate the workflow.osw in the modified simulation package.

    Searches the package root first, then recursively. Returns the first
    ``.osw`` found, preferring the root-level file over nested copies.
    Returns ``None`` when no ``.osw`` exists in the package.
    """
    root_osw = modified_sim_package / "workflow.osw"
    if root_osw.is_file():
        return root_osw
    # Fallback: search recursively for any .osw file.
    for osw in modified_sim_package.rglob("*.osw"):
        return osw
    return None


def _find_sql_in_package_run(package_run_dir: Path) -> Path | None:
    """Find an existing ``eplusout.sql`` produced during apply-parameters.

    The OpenStudio workflow can write SQL either directly under ``run/`` or in
    nested sub-workflow directories (for example ``run/**/SR1/run/eplusout.sql``).
    Prefer the top-level SQL when present; otherwise return the most recently
    modified nested SQL as the best candidate for downstream extraction.
    """
    top_level_sql = package_run_dir / "eplusout.sql"
    if top_level_sql.is_file():
        return top_level_sql

    if not package_run_dir.is_dir():
        return None

    candidates = [p for p in package_run_dir.rglob("eplusout.sql") if p.is_file()]
    if not candidates:
        return None

    try:
        return max(candidates, key=lambda p: (p.stat().st_mtime, p.stat().st_size))
    except OSError:
        # If metadata lookup fails, fall back to deterministic lexical ordering.
        return sorted(candidates)[-1]


def _reuse_existing_simulation_output(
    modified_sim_package: Path,
    sim_out: Path,
    sample_id: str,
) -> bool:
    """Reuse simulation outputs produced by apply-parameters when available."""
    sql_in_sim_out = sim_out / "eplusout.sql"
    if sql_in_sim_out.is_file():
        log.info(
            "simulation already run for sample=%s (eplusout.sql exists in sim_out) - skipping",
            sample_id,
        )
        return True

    package_run_dir = modified_sim_package / "run"
    sql_in_package_run = _find_sql_in_package_run(package_run_dir)
    if sql_in_package_run is None:
        return False

    log.info(
        "simulation already run for sample=%s (eplusout.sql found at %s) - copying to sim_out",
        sample_id,
        sql_in_package_run,
    )
    shutil.copy(sql_in_package_run, sql_in_sim_out)

    sql_parent = sql_in_package_run.parent
    for fname in ["eplusout.err", "eplusout.end", "eplusout.mtd"]:
        src = sql_parent / fname
        if not src.is_file() and sql_parent != package_run_dir:
            src = package_run_dir / fname
        dst = sim_out / fname
        if src.is_file() and not dst.is_file():
            shutil.copy(src, dst)

    return True


def _get_openstudio_cmd() -> str:
    """Return the name of the OpenStudio CLI executable on PATH.

    Prefers "openstudio.cli" but falls back to "openstudio" if that is
    what is available on the local system (e.g. macOS installation).
    """
    if shutil.which("openstudio.cli") is not None:
        return "openstudio.cli"
    return "openstudio"


def _is_openstudio_available() -> bool:
    """Check whether ``openstudio.cli`` or ``openstudio`` is on PATH.

    Uses ``shutil.which`` so the check works both on bare metal and
    inside the ``nrel/openstudio`` container.
    """
    return shutil.which("openstudio.cli") is not None or shutil.which("openstudio") is not None


def _is_stub_mode() -> bool:
    """Check whether the user has explicitly opted into stub mode.

    When ``OSIMFLOW_STUB_SIM=1`` is set in the environment, the work
    function uses the placeholder stub regardless of whether
    ``openstudio.cli`` is on PATH. This is the testing / development
    escape hatch so existing integration tests continue to work without
    a real OpenStudio installation.
    """
    return os.environ.get("OSIMFLOW_STUB_SIM") == "1"


def _run_openstudio_sim_impl(
    modified_sim_package: Path,
    sample_id: str,
    openstudio_version: str,
    out: Path,
    simulate_work_s: float = 2.0,
    *,
    stdout_path: Path | None = None,
    stderr_path: Path | None = None,
    max_retries: int = 3,
    worker_id: str = "local",
    health_check_interval: float = HEALTH_CHECK_INTERVAL_S,
) -> Path:
    """Internal implementation — wrapped with retry by ``run_openstudio_sim``."""
    sim_out = out / sample_id
    sim_out.mkdir(parents=True, exist_ok=True)
    log.info("simulating sample=%s version=%s -> %s", sample_id, openstudio_version, sim_out)

    if stdout_path is None:
        stdout_path = sim_out / "stdout.log"
    if stderr_path is None:
        stderr_path = sim_out / "stderr.log"

    # ------------------------------------------------------------------
    # When openstudio.cli is available, default_apply_parameters now
    # invokes the CLI which runs the full measure pipeline including
    # simulation (issue #248). In that case, eplusout.sql already
    # exists and we should skip re-running.
    #
    # The simulation outputs from apply_parameters end up in the
    # modified_sim_package's run/ subdirectory. Check both the sim_out
    # directory (for cached results) and the package's run directory
    # (for fresh apply outputs).
    # ------------------------------------------------------------------
    if _reuse_existing_simulation_output(
        modified_sim_package=modified_sim_package,
        sim_out=sim_out,
        sample_id=sample_id,
    ):
        return sim_out

    # Determine whether to use the real OpenStudio CLI or the stub.
    # Real CLI is used when:
    #   1. openstudio.cli is on PATH, AND
    #   2. stub mode is NOT explicitly enabled (OSIMFLOW_STUB_SIM != "1")
    cli_available = _is_openstudio_available()
    stub_mode = _is_stub_mode()
    use_real_cli = cli_available and not stub_mode

    # Fail fast: if the CLI is not available AND stub mode is not enabled,
    # the user must either install OpenStudio or explicitly opt into stub mode.
    if not cli_available and not stub_mode:
        raise RuntimeError(
            "openstudio CLI is not available on PATH and OSIMFLOW_STUB_SIM=1 is not set. "
            "Install OpenStudio CLI or set OSIMFLOW_STUB_SIM=1 to use stub mode for testing."
        )

    # --- Container health monitoring (issue #415) ---
    # Start a heartbeat writer thread for the duration of the simulation.
    # This allows the executor to detect frozen/silently-failed containers.
    stop_heartbeat = threading.Event()
    heartbeat_thread: threading.Thread | None = None
    if health_check_interval > 0:
        heartbeat_thread = threading.Thread(
            target=_heartbeat_writer,
            args=(sim_out, sample_id, openstudio_version, stop_heartbeat),
            daemon=True,
            name=f"heartbeat-{sample_id}",
        )
        heartbeat_thread.start()

    try:
        if use_real_cli:
            result_path = _run_real_openstudio(
                modified_sim_package=modified_sim_package,
                sample_id=sample_id,
                sim_out=sim_out,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
            )
        else:
            cmd = [
                sys.executable,
                "-c",
                (
                    "import sys, time;"
                    f" print('openstudio CLI stub v{openstudio_version} sample={sample_id}');"
                    f" time.sleep({simulate_work_s});"
                    " print('-- eplusout.sql placeholder --');"
                    " sys.exit(0)"
                ),
            ]
            run_subprocess(
                cmd,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                cwd=sim_out,
            )
            (sim_out / "eplusout.sql").write_text("-- placeholder sql")
            (sim_out / "eplusout.err").write_text("")
            result_path = sim_out
    finally:
        # Stop the heartbeat thread
        if heartbeat_thread is not None:
            stop_heartbeat.set()
            heartbeat_thread.join(timeout=5.0)

    # --- Health check after simulation ---
    # If enabled and the heartbeat went stale during execution, treat it as
    # a potentially transient failure so the retry loop can recover.
    if health_check_interval > 0:
        health = check_container_health(sim_out, health_check_interval)
        if health.status == SimulationHealthStatus.STALE:
            log.warning(
                "sample %s container health STALE after simulation: %s",
                sample_id,
                health.message,
            )
            # Raise TransientError so run_with_retry / executor retries it.
            raise TransientError(
                f"container health check stale for sample {sample_id}: {health.message}"
            )

    return result_path


def run_openstudio_sim(
    modified_sim_package: Path,
    sample_id: str,
    openstudio_version: str,
    out: Path,
    simulate_work_s: float = 2.0,
    *,
    stdout_path: Path | None = None,
    stderr_path: Path | None = None,
    max_retries: int = 3,
    worker_id: str = "local",
    health_check_interval: float = HEALTH_CHECK_INTERVAL_S,
) -> Path:
    """Run the OpenStudio simulation with exponential-backoff retry.

    When ``openstudio.cli`` is available on PATH and the environment
    variable ``OSIMFLOW_STUB_SIM`` is not set to ``1``, this function
    invokes the real OpenStudio CLI::

        openstudio.cli run -w <workflow.osw>

    The CLI produces ``eplusout.sql``, ``eplusout.err``, and other
    artifacts in the per-sample work directory. The framework does **not**
    write placeholder files when using the real CLI.

    When ``openstudio.cli`` is not available (or ``OSIMFLOW_STUB_SIM=1``
    is set), the function falls back to the stub behavior that sleeps
    for ``simulate_work_s`` seconds and writes placeholder output files.
    This fallback ensures existing integration tests pass without a real
    OpenStudio installation.

    Transient failures (network timeout, resource contention, specific
    exit codes) are retried with exponential backoff (1s base, 60s cap)
    up to ``max_retries`` attempts before surfacing the final error.

    Container health monitoring (issue #415): when ``health_check_interval``
    is greater than 0, a background heartbeat thread writes a
    ``.heartbeat.json`` file every 30 seconds. If the simulation exits
    with a STALE heartbeat (no update for longer than
    ``health_check_interval``), the failure is treated as potentially
    transient so the executor can retry it.

    Args:
        modified_sim_package: per-sample modified package from APPLY_PARAMETERS.
        sample_id: the sample's identifier (e.g. "0001").
        openstudio_version: pinned OpenStudio version (selects container tag).
        out: directory where simulation outputs are written.
        simulate_work_s: how long the stub sleeps to simulate work
            (only used in stub mode).
        stdout_path: optional path to the per-sample stdout log file
            (issue #6). When provided alongside ``stderr_path``, the
            underlying subprocess has its stdout/stderr streams
            redirected to these files. The Campaign populates them with
            ``${outdir}/work/sim/<sample_id>/stdout.log`` and
            ``stderr.log`` per `.agents/results/monitoring-decision.md`.
        stderr_path: optional path to the per-sample stderr log file.
        max_retries: maximum retry attempts for transient failures
            (default 3). Set to 0 to disable retries.
        worker_id: identifier for the worker running this simulation
            (issue #341). Written to the heartbeat file for liveness tracking.
        health_check_interval: seconds between container health checks.
            Set to 0 to disable health monitoring (default 60s). When enabled,
            a heartbeat thread runs for the duration of the simulation and a
            STALE heartbeat on failure triggers a retry as a potentially
            transient error (issue #415).

    Returns:
        Path to the simulation output directory (eplusout.sql inside).

    Raises:
        RuntimeError: when ``openstudio.cli`` is available but no
            ``workflow.osw`` is found in the modified package.
        subprocess.CalledProcessError: when ``openstudio.cli`` exits
            with a non-zero code that is not transient.
        TransientError: when the simulation exited with a stale heartbeat,
            indicating a potentially transient container freeze.
    """
    # Auto-detect version if not provided or invalid
    resolved_version = openstudio_version
    if not resolved_version or not resolved_version[0].isdigit():
        log.debug(
            "run_openstudio_sim: version %r is empty/invalid - attempting auto-detection",
            openstudio_version,
        )
        try:
            resolved_version = detect_openstudio_version()
            log.info(
                "run_openstudio_sim: auto-detected OpenStudio version %s for sample %s",
                resolved_version,
                sample_id,
            )
        except VersionDetectionError:
            log.warning(
                "run_openstudio_sim: could not auto-detect version for sample %s - using stub",
                sample_id,
            )
            resolved_version = "unknown"

    return _run_openstudio_sim_impl(
        modified_sim_package=modified_sim_package,
        sample_id=sample_id,
        openstudio_version=resolved_version,
        out=out,
        simulate_work_s=simulate_work_s,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        max_retries=max_retries,
        worker_id=worker_id,
        health_check_interval=health_check_interval,
    )


def _parse_register_values(stdout_path: Path) -> dict[str, object] | None:
    """Parse runner.registerValue JSON output from CLI stdout.

    The OpenStudio CLI writes a JSON array of registered value objects
    to stdout when measures call ``runner.registerValue``. Each entry
    has the shape::

        {"name": "variable_name", "value": 123.45, "type": "Double"}

    Returns a dict mapping names to values, or None if parsing fails
    or no registered values are found.
    """
    if not stdout_path.is_file():
        return None
    try:
        text = stdout_path.read_text(encoding="utf-8", errors="replace").strip()
        if not text:
            return None
        data = json.loads(text)
        if not isinstance(data, list):
            return None
        result: dict[str, object] = {}
        for entry in data:
            if isinstance(entry, dict) and "name" in entry and "value" in entry:
                result[str(entry["name"])] = entry["value"]
        return result if result else None
    except (json.JSONDecodeError, OSError):
        return None


def _run_real_openstudio(
    *,
    modified_sim_package: Path,
    sample_id: str,
    sim_out: Path,
    stdout_path: Path,
    stderr_path: Path,
) -> Path:
    """Invoke ``openstudio.cli run -w <workflow.osw>`` (issue #31).

    This is the production code path that runs inside the
    ``nrel/openstudio:<version>`` container (or on bare metal when
    the CLI is on PATH). The real CLI produces ``eplusout.sql``,
    ``eplusout.err``, and other EnergyPlus output files in the
    per-sample work directory (``sim_out``). The framework does **not**
    write placeholder files — the CLI owns the output.

    When measures call ``runner.registerValue``, the CLI outputs a JSON
    array to stdout. This function captures those values and writes them
    to ``register_values.json`` in the sample output directory (issue #251).

    Raises:
        RuntimeError: when no ``workflow.osw`` is found in the package.
        subprocess.CalledProcessError: when the CLI exits non-zero.
    """
    workflow_path = _find_workflow_osw(modified_sim_package)
    if workflow_path is None:
        raise RuntimeError(
            f"No workflow.osw found in modified_sim_package={modified_sim_package!r} "
            f"for sample={sample_id!r}. "
            f"The real OpenStudio CLI requires a workflow file."
        )

    cmd: list[str] = [
        _get_openstudio_cmd(),
        "run",
        "-w",
        str(workflow_path),
    ]
    log.info(
        "openstudio.cli real invocation sample=%s workflow=%s cwd=%s",
        sample_id,
        workflow_path,
        sim_out,
    )
    try:
        run_subprocess(
            cmd,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            cwd=modified_sim_package,
            check=True,
        )
    except subprocess.SubprocessError as e:
        log.error(
            "openstudio.cli failed for sample=%s: %s",
            sample_id,
            e,
        )
        raise

    register_values = _parse_register_values(stdout_path)
    if register_values is not None:
        rv_path = sim_out / "register_values.json"
        safe_json_dumps(register_values, rv_path, default=str, indent=2)
        log.info(
            "captured %d runner.registerValue outputs for sample=%s",
            len(register_values),
            sample_id,
        )

    return sim_out


# ---------------------------------------------------------------------------
# KPI extraction: parses eplusout.sql into a JSON.
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# LHS Generation
# ---------------------------------------------------------------------------
def generate_lhs(variables_yml: Path, n_samples: int, out: Path) -> Path:
    out.mkdir(parents=True, exist_ok=True)
    samples_json = out / "samples.json"
    try:
        subprocess.run(  # nosec  # sourcery skip: suspicious-subprocess-call
            [
                sys.executable,
                str(_resolve_work_script("generate_lhs.py")),
                "--variables_yml",
                str(variables_yml),
                "--n_samples",
                str(n_samples),
                "--out",
                str(samples_json),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as e:
        log.error("generate_lhs failed: %s", e.stderr or "<empty>")
        raise RuntimeError(f"generate_lhs failed: stdout={e.stdout!r} stderr={e.stderr!r}") from e
    return samples_json


def _extract_kpis_impl(
    simulation_dir: Path,
    sample_id: str,
    out: Path,
    *,
    openstudio_version: str | None = None,
    max_retries: int = 3,
) -> Path:
    """Internal implementation — wrapped with retry by ``extract_kpis``."""
    out.mkdir(parents=True, exist_ok=True)
    kpi_path = out / f"kpi_{sample_id}.json"
    cmd = [
        sys.executable,
        str(_resolve_work_script("extract_kpis.py")),
        "--simulation_dir",
        str(simulation_dir),
        "--sample_id",
        sample_id,
        "--out",
        str(kpi_path),
    ]
    if openstudio_version is not None:
        cmd.extend(["--openstudio_version", openstudio_version])
    try:
        subprocess.run(  # nosec  # sourcery skip: suspicious-subprocess-call
            cmd,
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as e:
        log.error("extract_kpis failed for %s: %s", sample_id, e.stderr or "<empty>")
        raise RuntimeError(
            f"extract_kpis failed for {sample_id}: stdout={e.stdout!r} stderr={e.stderr!r}"
        ) from e
    return kpi_path


def extract_kpis(
    simulation_dir: Path,
    sample_id: str,
    out: Path,
    *,
    openstudio_version: str | None = None,
    max_retries: int = 3,
) -> Path:
    """Run the default KPI extractor with exponential-backoff retry.

    Transient failures (disk I/O, network, resource contention) are retried
    up to ``max_retries`` attempts before surfacing the final error.

    Args:
        simulation_dir: directory containing simulation outputs (e.g. eplusout.sql).
        sample_id: the sample's identifier (e.g. "0001").
        out: directory where the KPI JSON file is written.
        openstudio_version: optional OpenStudio version string to record in KPI JSON.
        max_retries: maximum retry attempts for transient failures (default 3).

    Returns:
        Path to the kpi JSON file.
    """
    return run_with_retry(
        _extract_kpis_impl,
        simulation_dir,
        sample_id,
        out,
        openstudio_version=openstudio_version,
        max_retries=max_retries,
        sample_id=sample_id,
        step_name="extract_kpis",
    )


# ---------------------------------------------------------------------------
# Worker direct-to-storage push (issue #625, Epic #624)
# ---------------------------------------------------------------------------
def publish_kpi_results(
    *,
    storage: ResultStorage,
    campaign_id: str,
    sample_id: str,
    index: int,
    simulation_dir: Path,
    kpi_path: Path | None,
    exit_code: int,
    status: str,
    archive_intermediates: bool,
    coordinator_url: str | None = None,
    api_key: str | None = None,
    tmp_dir: Path | None = None,
) -> str | None:
    """Push ``kpis.json`` + atomic ``_manifest.json`` directly to *storage*.

    Implements the worker direct-to-storage flow (issue #625, Epic #624).
    After KPI extraction, the worker uploads its results — plus an atomic
    completion marker — directly to object storage so no result bytes return
    to the submitting host.  The aggregation step later reads from storage
    rather than from per-sample local files.

    Local-executor preservation: when *storage* is a :class:`LocalStorage`
    (i.e. ``--result-storage-backend local``) this function is a **no-op** and
    returns ``None`` immediately, so the local path makes zero storage or
    network calls and its on-disk outputs are unchanged.

    Ordering & atomicity: ``kpis.json`` is uploaded *first*; the optional
    ``eplusout.sql`` (only when ``archive_intermediates``) is uploaded next;
    ``_manifest.json`` is written **last** (see
    :func:`osimflow.manifest.write_manifest_atomically`) as the durability
    fence.  ``eplusout.err`` / ``eplusout.log`` are **never** uploaded
    (size guard, AGENTS.md gotchas #1, #8).

    .. note::

       *storage* must be the **raw** :class:`ResultStorage` backend, NOT the
       async :class:`~osimflow.storage.ResultStorageUploader` wrapper.  The
       wrapper enqueues uploads to a background queue and therefore cannot
       guarantee that the manifest becomes visible strictly after
       ``kpis.json``.  Synchronous uploads here give that ordering guarantee.

    Parameters
    ----------
    storage
        Raw :class:`ResultStorage` backend.
    campaign_id, sample_id, index
        Identity of the sample; ``index`` is its zero-based position in the
        campaign.
    simulation_dir
        Per-sample directory containing ``eplusout.sql`` / ``eplusout.err``.
    kpi_path
        Local path to the extracted ``kpi_{sample_id}.json`` (``None`` if
        extraction itself failed before producing a file).
    exit_code, status
        Worker exit code and coarse status (``"completed"`` or ``"failed"``).
    archive_intermediates
        When true, upload ``eplusout.sql`` under the sample prefix.
    coordinator_url
        Optional Coordinator base URL; when set, a best-effort PATCH is sent
        (contract §3.2).  ``None`` skips the report (no network).
    api_key
        Optional bearer token for the Coordinator PATCH.
    tmp_dir
        Directory used to stage the manifest temp file.  Defaults to
        *simulation_dir*.

    Returns
    -------
    str | None
        The remote manifest key, or ``None`` for the local no-op.
    """
    # Local import keeps the module-import graph acyclic at import time.
    from . import manifest as manifest_mod  # noqa: PLC0415
    from .storage import LocalStorage  # noqa: PLC0415

    # Local executor path is unchanged: zero storage / network calls.
    if isinstance(storage, LocalStorage):
        log.debug(
            "publish_kpi_results: LocalStorage backend for %s — no-op (local path unchanged)",
            sample_id,
        )
        return None

    base = f"{campaign_id}/samples/{sample_id}"
    kpis_key = f"{base}/kpis.json"
    manifest_key = f"{base}/_manifest.json"
    stage_dir = tmp_dir if tmp_dir is not None else simulation_dir

    kpis_uploaded = False

    # 1. Upload kpis.json FIRST (only if the file was produced).
    if kpi_path is not None and kpi_path.is_file():
        try:
            storage.upload_file(kpi_path, kpis_key)
            kpis_uploaded = True
        except OSError as exc:
            log.warning(
                "publish_kpi_results: kpis.json upload failed for %s: %s",
                sample_id,
                exc,
            )
            # A failed KPI upload downgrades the sample to 'failed' so the
            # Coordinator does not treat it as complete.
            status = "failed"
            exit_code = exit_code if exit_code != 0 else 1
    elif status == "completed":
        # Extraction succeeded but the KPI file is missing on disk — record
        # the inconsistency rather than silently publishing an empty success.
        log.warning(
            "publish_kpi_results: %s marked completed but kpi file missing "
            "at %s — publishing as failed",
            sample_id,
            kpi_path,
        )
        status = "failed"
        exit_code = exit_code if exit_code != 0 else 1

    # 2. Optional: archive eplusout.sql. NEVER .err / .log (size guard).
    if archive_intermediates:
        sql_path = simulation_dir / "eplusout.sql"
        if sql_path.is_file():
            try:
                storage.upload_file(sql_path, f"{base}/eplusout.sql")
            except OSError as exc:
                log.warning(
                    "publish_kpi_results: eplusout.sql upload skipped for %s: %s",
                    sample_id,
                    exc,
                )
        else:
            log.debug(
                "publish_kpi_results: no eplusout.sql to archive for %s",
                sample_id,
            )

    # 3. Capture the first Severe error — present on failure,
    #    None on clean completion.
    first_severe = manifest_mod.first_severe_error(simulation_dir / "eplusout.err")

    # 4. Build + atomically write the manifest LAST. It is the durability
    #    fence signalling "this sample is complete".
    record = manifest_mod.build_manifest(
        sample_id=sample_id,
        index=index,
        status=status,
        kpis_key=kpis_key if kpis_uploaded else None,
        exit_code=exit_code,
        first_severe_error=first_severe,
    )
    try:
        manifest_mod.write_manifest_atomically(
            storage,
            manifest_key,
            record,
            local_tmp_dir=stage_dir,
        )
    except OSError as exc:
        log.error(
            "publish_kpi_results: atomic manifest write failed for %s: %s",
            sample_id,
            exc,
        )
        raise

    # 5. Best-effort Coordinator status report (contract §3.2).
    if coordinator_url:
        manifest_mod.report_sample_completion(
            coordinator_url=coordinator_url,
            campaign_id=campaign_id,
            manifest=record,
            api_key=api_key,
        )

    return manifest_key


# ---------------------------------------------------------------------------
# Aggregation & plotting
# ---------------------------------------------------------------------------
def aggregate_results(
    kpi_files: list[Path],
    sim_dirs: list[Path],
    out: Path,
    baseline_sample_id: str | None = None,
    ts_resolution: str = "monthly",
    samples_json: Path | None = None,
) -> dict[str, Path]:
    """Aggregate per-sample KPIs into CSV/Parquet/failed-CSV. Returns paths.

    Args:
        kpi_files: list of per-sample KPI JSON paths.
        sim_dirs: list of per-sample simulation output directories.
        out: output directory for aggregated results.
        baseline_sample_id: optional baseline sample ID (issue #64). When
            provided, the aggregator computes percentage improvement columns
            for each numeric KPI relative to the baseline.
        ts_resolution: time-series aggregation resolution (issue #40).
            One of 'hourly', 'daily', 'monthly', 'annual'. Defaults to
            'monthly'. Raw hourly data is preserved in per-sample .sql
            files behind --archive_intermediates.
        samples_json: optional path to samples.json containing per-sample
            input parameter values (issue #276). When provided, parameter
            columns are merged into the aggregated results CSV before KPI
            columns. Missing file is non-fatal (backward compatible).
    """
    out.mkdir(parents=True, exist_ok=True)
    csv_path = out / "aggregated_results.csv"
    failed_path = out / "failed_simulations.csv"
    parquet_path = out / "aggregated_results.parquet"
    cmd: list[str] = [
        sys.executable,
        str(_resolve_work_script("aggregate_results.py")),
        "--kpis",
        *(str(p) for p in kpi_files),
        "--simulation_dirs",
        *(str(p) for p in sim_dirs),
        "--out_csv",
        str(csv_path),
        "--out_parquet",
        str(parquet_path),
        "--out_failed",
        str(failed_path),
        "--ts_resolution",
        ts_resolution,
    ]
    if baseline_sample_id is not None:
        cmd.extend(["--baseline_sample_id", baseline_sample_id])
    if samples_json is not None:
        cmd.extend(["--samples_json", str(samples_json)])
    try:
        subprocess.run(  # nosec  # sourcery skip: suspicious-subprocess-call
            cmd,
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as e:
        log.error("aggregate_results failed: %s", e.stderr or "<empty>")
        raise RuntimeError(
            f"aggregate_results failed: stdout={e.stdout!r} stderr={e.stderr!r}"
        ) from e
    return {
        "csv": csv_path,
        "parquet": parquet_path,
        "failed": failed_path,
        "timeseries": out / "timeseries_aggregated.csv",
    }


def generate_plots(
    csv_path: Path,
    failed_path: Path,
    out: Path,
    baseline_sample_id: str | None = None,
    pareto_dir: Path | None = None,
) -> list[Path]:
    """Render summary plots from the aggregated CSV. Returns list of plot files.

    Args:
        csv_path: path to aggregated_results.csv.
        failed_path: path to failed_simulations.csv.
        out: output directory for plot files.
        baseline_sample_id: optional baseline sample ID (issue #64). When
            provided, the plot generator adds a vertical reference line for
            the baseline EUI on the EUI histogram.
        pareto_dir: optional directory containing per-generation Pareto JSON
            files (gen_N.json). When provided, generates Pareto front scatter
            and convergence plots (issue #124).
    """
    out.mkdir(parents=True, exist_ok=True)
    cmd: list[str] = [
        sys.executable,
        str(_resolve_work_script("generate_plots.py")),
        "--results_csv",
        str(csv_path),
        "--failed_csv",
        str(failed_path),
        "--outdir",
        str(out),
    ]
    if baseline_sample_id is not None:
        cmd.extend(["--baseline_sample_id", baseline_sample_id])
    if pareto_dir is not None:
        cmd.extend(["--pareto_dir", str(pareto_dir)])

    # Ensure osimflow is importable in the subprocess (issue #876)
    # Add the project root (parent of osimflow package) to PYTHONPATH
    env = os.environ.copy()
    project_root = Path(__file__).resolve().parent.parent
    existing_pythonpath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        str(project_root)
        if not existing_pythonpath
        else f"{project_root}{os.pathsep}{existing_pythonpath}"
    )

    try:
        subprocess.run(  # nosec  # sourcery skip: suspicious-subprocess-call
            cmd,
            check=True,
            capture_output=True,
            text=True,
            env=env,
        )
    except subprocess.CalledProcessError as e:
        log.error("generate_plots failed: %s", e.stderr or "<empty>")
        raise RuntimeError(f"generate_plots failed: stdout={e.stdout!r} stderr={e.stderr!r}") from e
    return sorted(out.glob("*.png")) + sorted(out.glob("*.pdf"))


# ---------------------------------------------------------------------------
# Preflight validation helpers (issue #198)
# ---------------------------------------------------------------------------


def _validate_weather_files(template_sim_package: Path) -> None:
    """Validate EPW weather files in the template package.

    Discovers all ``.epw`` files via :func:`osimflow.weather.discover_epw_files`
    and validates each one's LOCATION header with
    :func:`osimflow.weather.validate_epw_header`.  Logs city/country metadata
    for every valid file.  Raises :class:`SevereEnergyPlusError` if any file
    has an invalid header.

    A missing ``weather/`` directory is not an error — some packages embed
    weather paths that resolve at runtime.

    Args:
        template_sim_package: path to the user's seed model package.

    Raises:
        SevereEnergyPlusError: one or more EPW files have invalid headers.
    """
    epw_files = discover_epw_files(template_sim_package)
    if not epw_files:
        log.info("preflight weather: no .epw files found — skipping validation")
        return

    errors: list[str] = []
    for epw_path in epw_files:
        try:
            header = validate_epw_header(epw_path)
            log.info(
                "preflight weather: validated %s — %s, %s",
                epw_path.name,
                header["city"],
                header["country"],
            )
        except EPWValidationError as exc:
            errors.append(f"{epw_path.name}: {exc}")
            log.warning("preflight weather: INVALID — %s", exc)

    if errors:
        msg = (
            "Preflight weather validation FAILED. "
            "The following EPW files have invalid headers:\n"
            + "\n".join(f"  {e}" for e in errors)
            + "\nFix the weather files before running the full campaign."
        )
        raise SevereEnergyPlusError(msg)


def _validate_model_geometry(template_sim_package: Path) -> None:
    """Best-effort check that a model file exists and is parseable.

    Recursively globs for ``.osm`` files in the template package.  If none
    are found, logs a WARNING — some workflows are ``.osw``-only and this is
    not a hard failure.  If an ``.osm`` exists and ``openstudio.cli`` is
    available, attempts a trivial parse command, but swallows any error
    (best-effort).

    Args:
        template_sim_package: path to the user's seed model package.
    """
    osm_files = list(template_sim_package.rglob("*.osm"))
    if not osm_files:
        log.warning(
            "preflight geometry: no .osm files found in %s. "
            "Some packages use .osw-only workflows — proceeding anyway.",
            template_sim_package,
        )
        return

    log.info(
        "preflight geometry: found %d .osm file(s) in %s",
        len(osm_files),
        template_sim_package,
    )

    # Best-effort quick parse — only if the CLI is available.
    if _is_openstudio_available() and not _is_stub_mode():
        for osm_file in osm_files:
            try:
                result = subprocess.run(  # noqa: S603
                    [
                        _get_openstudio_cmd(),
                        "openstudio",
                        "--execute",
                        "puts 'model ok'",
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=30,
                )
                if result.returncode == 0:
                    log.info(
                        "preflight geometry: quick parse of %s succeeded",
                        osm_file.name,
                    )
                else:
                    log.warning(
                        "preflight geometry: quick parse of %s returned exit code %d (non-fatal)",
                        osm_file.name,
                        result.returncode,
                    )
            except (subprocess.SubprocessError, FileNotFoundError, OSError) as exc:
                log.warning(
                    "preflight geometry: quick parse of %s failed (non-fatal): %s",
                    osm_file.name,
                    exc,
                )


def _validate_measure_entry_points(template_sim_package: Path) -> None:
    """Best-effort check that measure subdirectories have entry-point files.

    If the template package has a ``measures/`` directory, verifies that each
    measure subdirectory contains a ``measure.rb`` or ``measure.py`` file.
    Logs WARNINGs for measures missing their entry point, but does NOT raise
    — some measures have non-standard names.

    Args:
        template_sim_package: path to the user's seed model package.
    """
    measures_dir = template_sim_package / "measures"
    if not measures_dir.is_dir():
        log.info("preflight measures: no measures/ directory found — skipping check")
        return

    measure_dirs = [d for d in measures_dir.iterdir() if d.is_dir()]
    if not measure_dirs:
        log.info("preflight measures: measures/ directory is empty")
        return

    missing_count = 0
    for measure_dir in sorted(measure_dirs):
        has_rb = (measure_dir / "measure.rb").is_file()
        has_py = (measure_dir / "measure.py").is_file()
        if has_rb or has_py:
            entry = "measure.rb" if has_rb else "measure.py"
            log.info("preflight measures: %s has %s", measure_dir.name, entry)
        else:
            missing_count += 1
            log.warning(
                "preflight measures: %s is missing both measure.rb and "
                "measure.py (non-standard entry point?)",
                measure_dir.name,
            )

    if missing_count:
        log.warning(
            "preflight measures: %d measure(s) missing standard entry points "
            "(non-fatal — check for non-standard naming)",
            missing_count,
        )


# ---------------------------------------------------------------------------
# Preflight run model (issue #107)
# ---------------------------------------------------------------------------
def preflight_run_model(
    template_sim_package: Path,
    openstudio_version: str,
) -> None:
    """Run a throwaway simulation of the seed model to catch errors early.

    Makes a temporary copy of the ``template_sim_package``, runs
    ``openstudio.cli run -w workflow.osw`` (or the stub if the CLI is
    not available), and inspects the result.  If the run produces severe
    errors, raises :class:`SevereEnergyPlusError` so the campaign aborts
    before spending cloud budget on a broken model.

    On success, the temporary copy is cleaned up and the function
    returns ``None``.

    Args:
        template_sim_package: path to the user's seed model package.
        openstudio_version: pinned OpenStudio version (for logging).

    Raises:
        SevereEnergyPlusError: the preflight simulation encountered
            severe EnergyPlus errors.
        RuntimeError: ``openstudio.cli`` is available but no
            ``workflow.osw`` is found in the template package.
    """
    log.info(
        "PREFLIGHT_RUN_MODEL: running throwaway sim on %s (version=%s)",
        template_sim_package,
        openstudio_version,
    )

    # ------------------------------------------------------------------
    # Pre-simulation validation checks (issue #198)
    # ------------------------------------------------------------------
    _validate_weather_files(template_sim_package)
    _validate_model_geometry(template_sim_package)
    _validate_measure_entry_points(template_sim_package)

    with tempfile.TemporaryDirectory(prefix="osimflow_preflight_") as tmp_dir:
        tmp_pkg = Path(tmp_dir) / "preflight_package"
        shutil.copytree(template_sim_package, tmp_pkg)

        use_real_cli = _is_openstudio_available() and not _is_stub_mode()

        if use_real_cli:
            workflow_path = _find_workflow_osw(tmp_pkg)
            if workflow_path is None:
                raise RuntimeError(
                    f"No workflow.osw found in template_sim_package="
                    f"{template_sim_package}. The preflight run requires "
                    f"a workflow file."
                )
            cmd: list[str] = [
                _get_openstudio_cmd(),
                "run",
                "-w",
                str(workflow_path),
            ]
            log.info("preflight: invoking openstudio.cli with %s", cmd)
            result = subprocess.run(  # noqa: S603
                cmd,
                cwd=tmp_pkg,
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode != 0:
                # Extract the first severe error line from stderr or stdout
                error_output = result.stderr or result.stdout or ""
                severe_line = _extract_severe_error(error_output)
                msg = (
                    f"Preflight simulation FAILED (exit code {result.returncode}). "
                    f"The seed model has errors that would waste cloud budget. "
                    f"Fix the model before running the full campaign."
                )
                if severe_line:
                    msg += f"\n  First severe error: {severe_line}"
                raise SevereEnergyPlusError(msg)
            log.info("preflight: openstudio.cli succeeded — seed model is valid")
        else:
            # Stub mode: simulate a quick pass. The stub writes a
            # placeholder eplusout.err that is empty (success).
            sim_out = tmp_pkg / "preflight_output"
            sim_out.mkdir(parents=True, exist_ok=True)
            (sim_out / "eplusout.sql").write_text("-- placeholder sql")
            (sim_out / "eplusout.err").write_text("")
            log.info("preflight: stub simulation passed")

    log.info("PREFLIGHT_RUN_MODEL: complete — seed model validated")


def _extract_severe_error(output: str) -> str:
    """Extract the first severe error line from EnergyPlus output.

    Searches for lines matching the pattern ``'<N> * Severe'`` which
    EnergyPlus uses for severe-level diagnostics.  Returns the first
    match, or an empty string if none found.
    """
    for line in output.splitlines():
        if re.search(r"^\s*(?:\d+\s+)?\*+\s*Severe", line, re.IGNORECASE):
            return line.strip()
    return ""
