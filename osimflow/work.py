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
import json  # noqa: F401  (re-export)
import logging
import os
import random  # noqa: F401  (re-export)
import re  # noqa: F401  (re-export)
import shutil
import signal  # noqa: F401  (re-export)
import sqlite3  # noqa: F401  (re-export)
import subprocess
import sys
import tempfile
import threading
import time  # noqa: F401  (tests patch osimflow.work.time.sleep)
from pathlib import Path
from typing import Any, NoReturn

# Names moved to the _work_* modules (issue #1890) stay importable from
# osimflow.work; the callers of patched seams remain defined in this module.
from osimflow._subprocess_utils import run_subprocess  # neutral location (issue #910)
from osimflow._work_artifacts import (  # noqa: F401
    _extract_severe_error,
    _find_sql_in_package_run,
    _find_workflow_osw,
    _parse_register_values,
    _publish_run_artifacts,
    _reuse_existing_simulation_output,
)
from osimflow._work_env import _WORK_SUBPROCESS_ENV_ALLOWLIST, _sanitize_env  # noqa: F401
from osimflow._work_heartbeat import (  # noqa: F401
    HEALTH_CHECK_INTERVAL_S,
    HEARTBEAT_FILENAME,
    HEARTBEAT_INTERVAL_S,
    SimulationHealth,
    SimulationHealthStatus,
    _write_heartbeat,
    check_container_health,
)
from osimflow._work_osm_mutations import (  # noqa: F401
    _apply_osm_mutations,
    _apply_osw_parameters,
    _apply_single_osm_mutation,
    _resolve_osm_object,
    _set_osm_attribute,
    _set_thermal_zone_schedule,
)
from osimflow._work_retry import (  # noqa: F401
    _NON_TRANSIENT_SUBPROCESS_SIGNALS,
    _TRANSIENT_EXIT_CODES,
    _TRANSIENT_MESSAGE_MARKERS,
    RetryDecision,
    _is_signal_killed_subprocess,
    _is_transient_error,
    classify_failure,
    run_with_retry,
)
from osimflow._work_retry import (
    SevereEnergyPlusError as SevereEnergyPlusError,  # noqa: PLC0414
)
from osimflow._work_retry import (
    TransientError as TransientError,  # noqa: PLC0414
)
from osimflow._work_stub import (  # noqa: F401
    _STUB_FLOOR_AREA_M2,
    _stub_eui_for_sample,
    _write_stub_eplusout_sql,
)
from osimflow.apply_params import OSMAttributeError  # noqa: F401
from osimflow.errors import OSimFlowRuntimeError  # noqa: F401
from osimflow.json_utils import safe_json_dumps
from osimflow.osw_results import OUT_OSW_NAME, RUN_LOG_NAME  # noqa: F401
from osimflow.storage import ResultStorage
from osimflow.version_detection import (
    VersionDetectionError,
    detect_openstudio_version,
    warn_on_cli_version_mismatch,
)
from osimflow.weather import EPWValidationError, discover_epw_files, validate_epw_header

log = logging.getLogger("osimflow.work")


# ---------------------------------------------------------------------------
# Work-script resolver
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
    template: Path,
    parameters: dict[str, Any],
    sample_id: str,
    out: Path,
    *,
    max_retries: int = 3,
) -> Path:
    """Apply parameter values to an OpenStudio model using Python bindings.

    Loads the ``model.osm`` from *out* using the OpenStudio Python
    bindings, applies each entry in *parameters* as a model attribute
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

    The signature matches the canonical BYOS contract documented in
    ``osimflow.byos_contract._BYOS_CONTRACT`` (issue #1061). ``template``
    and ``sample_id`` are accepted for parity with the BYOS contract but
    are not used by the default implementation — the default mutates
    ``out / model.osm`` in place.

    Args:
        template: Path to the template_sim_package (the read-only seed
            model). The default implementation does not consult it; it
            is accepted for parity with the BYOS contract so users who
            swap in a custom function see consistent positional args.
        parameters: Dict mapping variable names to values, e.g.  ``{
            "SpaceType_Office.lighting_power_density": 10.0 }``.
        sample_id: Sample identifier (e.g. ``"0001"``). Accepted for
            parity with the BYOS contract; not used by the default.
        out: Per-sample output directory containing ``model.osm``. The
            default writes the mutated model back to ``out / model.osm``
            and returns ``out``.
        max_retries: maximum retry attempts for transient failures during
            the load-mutate-save cycle (default 3). ``<=0`` disables retry.
            Mirrors the keyword-only ``max_retries`` on
            ``run_openstudio_sim`` / ``extract_kpis`` so the Campaign can
            forward ``--max-sample-retries`` uniformly (issue #1394).

    Returns:
        The per-sample output directory ``out`` (matching the BYOS
        contract's ``apply_parameters -> Path`` return type).

    Raises:
        RuntimeError: the OpenStudio Python bindings are not installed
            on this host. Install ``openstudio`` to enable this path.
        OSMAttributeError: a dotted variable name references an object
            type or instance name that does not exist in the model.
    """
    osm_path = out / "model.osm"
    # Issue #1869: measure arguments and the weather file live in the OSW
    # (plain JSON), so they are applied without the OpenStudio bindings.
    parameters = _apply_osw_parameters(out / "workflow.osw", parameters)
    if not parameters:
        # Nothing left to mutate (issue #1812): never load the OpenStudio
        # bindings or rewrite the staged package, so an existing OSW runs
        # byte-for-byte as supplied.
        if not (out / "workflow.osw").is_file() and not osm_path.is_file():
            raise FileNotFoundError(f"neither workflow.osw nor model.osm found in {out!r}")
        log.info("default_apply_parameters: no parameters; package left unmodified")
        return out

    if not osm_path.is_file():
        raise FileNotFoundError(f"model.osm not found in {out!r}")

    if _is_stub_mode():
        log.warning(
            "default_apply_parameters: OSIMFLOW_STUB_SIM=1 is set; skipping .osm mutation "
            "(stub mode — no OpenStudio bindings required)"
        )
        return out

    try:
        import openstudio  # noqa: PLC0415
    except ImportError as exc:
        raise RuntimeError(
            "OpenStudio Python bindings are not installed on this host. "
            "Install `openstudio` to enable production .osm mutation, "
            "or set OSIMFLOW_STUB_SIM=1 to use stub mode for testing."
        ) from exc

    def _apply_impl() -> Path:
        model_opt = openstudio.openstudiomodelcore.Model.load(str(osm_path))
        if not model_opt.is_initialized():
            raise RuntimeError(f"OpenStudio failed to load model from {osm_path!r}")
        model = model_opt.get()

        _apply_osm_mutations(model, openstudio, parameters)

        model.save(str(osm_path), overwrite=True)
        return out

    result = run_with_retry(
        _apply_impl,
        max_retries=max_retries,
        sample_id=sample_id,
        step_name="APPLY_PARAMETERS",
    )
    log.info("default_apply_parameters: mutated .osm saved to %s", osm_path)
    return result


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
            env=_sanitize_env(),
        )
    except subprocess.CalledProcessError as e:
        log.error("apply_params failed for %s: %s", sample_id, e.stderr or "<empty>")
        raise RuntimeError(
            f"apply_params failed for {sample_id}: stdout={e.stdout!r} stderr={e.stderr!r}"
        ) from e
    return out_dir


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
    timeout_s: float | None = None,
) -> Path:
    """Internal implementation — wrapped with retry by ``run_openstudio_sim``.

    ``timeout_s`` bounds the real-CLI subprocess (issue #1109): when set,
    a wedged EnergyPlus run is killed instead of hanging the sample
    forever. The default is ``None`` (effectively unbounded, issue
    #1534) because annual EnergyPlus simulations routinely exceed any
    stock bound — bound a run explicitly via ``--byos-timeout-s`` or the
    executor's ``time_min`` instead.
    """
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
        workflow = _find_workflow_osw(modified_sim_package)
        _publish_run_artifacts(workflow.parent if workflow else modified_sim_package, sim_out)
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
                timeout_s=timeout_s,
            )
        else:
            cmd = [
                sys.executable,
                "-c",
                (
                    "import sys, time;"
                    f" print('openstudio CLI stub v{openstudio_version} sample={sample_id}');"
                    f" time.sleep({simulate_work_s});"
                    " print('-- eplusout.sql stub --');"
                    " sys.exit(0)"
                ),
            ]
            run_subprocess(
                cmd,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                cwd=sim_out,
                env=_sanitize_env(),
            )
            # Issue #1419 — write a valid stub SQLite database (not the
            # old "-- placeholder sql" string) so ``extract_kpis`` can
            # open it and populate the critical KPIs (``eui_kwh_m2_yr``
            # and ``total_site_energy_kwh``).  Without this fix every
            # sample's KPI extraction fails, AGGREGATE_RESULTS never
            # writes ``aggregated_results.csv``, and ``_verify_step_inputs``
            # in the GENERATE_BASIC_PLOTS iteration raises FileNotFoundError.
            _write_stub_eplusout_sql(sim_out, sample_id)
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
    timeout_s: float | None = None,
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
        timeout_s: wall-clock bound for the real-CLI subprocess in seconds
            (issue #1109). When set, a wedged EnergyPlus run is killed and
            the sample fails instead of hanging the campaign forever.
            Default ``None`` = effectively unbounded (issue #1534): annual
            EnergyPlus runs routinely exceed 10 minutes, so a stock bound
            kills the flagship workload. Bound a run explicitly via
            ``--byos-timeout-s`` or rely on the executor's ``time_min``.
            A timeout kill is non-transient: the sample fails once and is
            not re-executed ``max_retries`` times.

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

    if resolved_version != "unknown" and not _is_stub_mode():
        warn_on_cli_version_mismatch(resolved_version)

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
        timeout_s=timeout_s,
    )


def _raise_missing_sql(modified_sim_package: Path, sample_id: str) -> NoReturn:
    """Fail a real-CLI run that exited 0 without ``eplusout.sql`` (issue #1966).

    ``RuntimeError`` is classified non-transient, so the sample fails once
    instead of caching a bogus ``exit_code=0`` success.
    """
    msg = (
        f"openstudio.cli exited 0 but produced no eplusout.sql under "
        f"{modified_sim_package}/run for sample={sample_id}"
    )
    log.error(msg)
    raise RuntimeError(msg)


def _run_real_openstudio(
    *,
    modified_sim_package: Path,
    sample_id: str,
    sim_out: Path,
    stdout_path: Path,
    stderr_path: Path,
    timeout_s: float | None = None,
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
        subprocess.TimeoutExpired: when the run exceeds ``timeout_s``
            (classified non-transient — issue #1534 — so it fails once
            instead of being retried).
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
            env=_sanitize_env(),
            timeout=timeout_s,
        )
    except subprocess.SubprocessError as e:
        log.error(
            "openstudio.cli failed for sample=%s: %s",
            sample_id,
            e,
        )
        raise
    finally:
        _publish_run_artifacts(workflow_path.parent, sim_out)

    # The CLI runs with cwd=modified_sim_package, so EnergyPlus outputs land in
    # <package>/run/. Publish them into sim_out, which is what downstream
    # steps and remote result transport consume.
    if not _reuse_existing_simulation_output(
        modified_sim_package=modified_sim_package,
        sim_out=sim_out,
        sample_id=sample_id,
    ):
        _raise_missing_sql(modified_sim_package, sample_id)

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
            env=_sanitize_env(),
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
    kpis: list[str] | None = None,
) -> Path:
    """Internal implementation — wrapped with retry by ``extract_kpis``.

    Issue #1015: this used to fork a fresh Python interpreter per sample
    (``subprocess.run([sys.executable, 'extract_kpis.py', ...])``) which
    cost ~150-300 ms of import overhead per call — ~30 min of pure
    startup latency for a 10K-sample campaign.  We now call
    :func:`osimflow._work_scripts.extract_kpis.run_extract_kpis` directly
    in-process; the CLI shim still exists for the standalone
    ``bin/extract_kpis.py`` user-facing entry point.
    """
    out.mkdir(parents=True, exist_ok=True)
    kpi_path = out / f"kpi_{sample_id}.json"
    from ._work_scripts.extract_kpis import run_extract_kpis  # noqa: PLC0415

    return run_extract_kpis(
        simulation_dir=simulation_dir,
        sample_id=sample_id,
        out_path=kpi_path,
        openstudio_version=openstudio_version,
        kpis=kpis,
    )


def extract_kpis(
    simulation_dir: Path,
    sample_id: str,
    out: Path,
    *,
    openstudio_version: str | None = None,
    max_retries: int = 3,
    kpis: list[str] | None = None,
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
        kpis: optional list of KPI names to extract (issue #1082). When
            ``None`` (default), all available KPIs are extracted.

    Returns:
        Path to the kpi JSON file.
    """
    return run_with_retry(
        _extract_kpis_impl,
        simulation_dir,
        sample_id,
        out,
        openstudio_version=openstudio_version,
        kpis=kpis,
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
    allow_insecure_coordinator: bool = False,
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
    allow_insecure_coordinator
        Explicit opt-in for plaintext ``http://`` coordinator URLs
        (issue #1550); the Campaign forwards
        ``cfg.allow_insecure_storage_endpoint`` here.  Defaults to
        ``False`` (fail-closed — non-loopback ``http://`` is refused).
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
            allow_insecure=allow_insecure_coordinator,
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
            env=_sanitize_env(),
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

    # Ensure osimflow is importable in the subprocess (issue #876) while
    # still passing the sanitized env (issue #1027).  We start from the
    # sanitized dict (which intentionally drops OSIMFLOW_TASK_PAYLOAD_*
    # — issue #1388) and only override ``PYTHONPATH`` to prepend the
    # project root so the child can ``import osimflow``.  We do NOT
    # call ``os.environ.copy()`` here: that would re-leak every parent
    # secret.
    env = _sanitize_env()
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
            # valid (minimal) eplusout.sql so downstream consumers see
            # a parseable SQLite database, and an empty eplusout.err
            # that signals success.  Issue #1419 — the previous
            # ``"-- placeholder sql"`` string was invalid SQLite and
            # cascaded into a critical-KPI quality failure on every
            # subsequent per-sample run.
            sim_out = tmp_pkg / "preflight_output"
            sim_out.mkdir(parents=True, exist_ok=True)
            _write_stub_eplusout_sql(sim_out, sample_id="preflight")
            (sim_out / "eplusout.err").write_text("")
            log.info("preflight: stub simulation passed")

    log.info("PREFLIGHT_RUN_MODEL: complete — seed model validated")
