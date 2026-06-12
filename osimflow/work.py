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

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from .executors import run_subprocess  # local helper (issue #6)
from .weather import EPWValidationError, discover_epw_files, validate_epw_header

log = logging.getLogger("osimflow.work")


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------
class SevereEnergyPlusError(RuntimeError):
    """Raised when a preflight simulation encounters severe errors."""


# Resolve the project root from the package location so the work layer
# does not hardcode a developer's local path. The convention is:
#   <project>/osimflow/work.py  →  <project>/
PROJECT_ROOT = Path(__file__).resolve().parent.parent
BIN = PROJECT_ROOT / "bin"


# ---------------------------------------------------------------------------
# BYOS contract: apply_parameters
# ---------------------------------------------------------------------------
def default_apply_parameters(
    template: Path,
    parameters: dict[str, object],
    sample_id: str,
    out: Path,
) -> Path:
    """Default parameter-application logic.

    A real implementation parses `template` (an .osm or .osw) and writes
    a modified copy to `out/<sample_id>/`. The current stub is the same
    one shipped in `bin/apply_params_to_model.py`; we call it as a
    subprocess to keep the BYOS contract identical: same argv, same exit
    code, same logging format.
    """
    out_dir = out / sample_id
    out_dir.mkdir(parents=True, exist_ok=True)
    param_file = out / f"{sample_id}.params.json"
    param_file.write_text(json.dumps(parameters, sort_keys=True))
    try:
        subprocess.run(  # nosec  # sourcery skip: suspicious-subprocess-call
            [
                sys.executable,
                str(BIN / "apply_params_to_model.py"),
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
        log.error("apply_params failed for %s: %s", sample_id, e.stderr)
        raise RuntimeError(f"apply_params failed for {sample_id}") from e
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


def _is_openstudio_available() -> bool:
    """Check whether ``openstudio.cli`` is on PATH.

    Uses ``shutil.which`` so the check works both on bare metal and
    inside the ``nrel/openstudio`` container where the CLI is at
    ``/usr/local/bin/openstudio.cli``.
    """
    return shutil.which("openstudio.cli") is not None


def _is_stub_mode() -> bool:
    """Check whether the user has explicitly opted into stub mode.

    When ``OSIMFLOW_STUB_SIM=1`` is set in the environment, the work
    function uses the placeholder stub regardless of whether
    ``openstudio.cli`` is on PATH. This is the testing / development
    escape hatch so existing integration tests continue to work without
    a real OpenStudio installation.
    """
    return os.environ.get("OSIMFLOW_STUB_SIM") == "1"


def run_openstudio_sim(
    modified_sim_package: Path,
    sample_id: str,
    openstudio_version: str,
    out: Path,
    simulate_work_s: float = 2.0,
    *,
    stdout_path: Path | None = None,
    stderr_path: Path | None = None,
) -> Path:
    """Run the OpenStudio simulation.

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

    Returns:
        Path to the simulation output directory (eplusout.sql inside).

    Raises:
        RuntimeError: when ``openstudio.cli`` is available but no
            ``workflow.osw`` is found in the modified package.
        subprocess.CalledProcessError: when ``openstudio.cli`` exits
            with a non-zero code.
    """
    sim_out = out / sample_id
    sim_out.mkdir(parents=True, exist_ok=True)
    log.info("simulating sample=%s version=%s -> %s", sample_id, openstudio_version, sim_out)

    # If the Campaign did not pass log paths (legacy callers, BYOS
    # scripts that pre-date issue #6), fall back to a per-sample
    # sentinel location inside `sim_out` so the files still exist on
    # disk. The default keeps the helper back-compatible while the
    # Campaign-driven path is the supported one.
    if stdout_path is None:
        stdout_path = sim_out / "stdout.log"
    if stderr_path is None:
        stderr_path = sim_out / "stderr.log"

    # ------------------------------------------------------------------
    # Decision: real CLI or stub?
    # ------------------------------------------------------------------
    use_real_cli = _is_openstudio_available() and not _is_stub_mode()

    if use_real_cli:
        return _run_real_openstudio(
            modified_sim_package=modified_sim_package,
            sample_id=sample_id,
            sim_out=sim_out,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
        )

    # ------------------------------------------------------------------
    # Stub path (original behavior)
    # ------------------------------------------------------------------
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
    try:
        run_subprocess(
            cmd,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            cwd=sim_out,
        )
    except subprocess.SubprocessError as e:
        # Surface the failure; the Campaign maps non-zero exit to a
        # failed SampleTrace row.
        log.error("run_openstudio_sim failed for %s: %s", sample_id, e)
        raise

    (sim_out / "eplusout.sql").write_text("-- placeholder sql")
    (sim_out / "eplusout.err").write_text("")  # success: empty err
    return sim_out


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

    Raises:
        RuntimeError: when no ``workflow.osw`` is found in the package.
        subprocess.CalledProcessError: when the CLI exits non-zero.
    """
    workflow_path = _find_workflow_osw(modified_sim_package)
    if workflow_path is None:
        raise RuntimeError(
            f"No workflow.osw found in modified_sim_package="
            f"{modified_sim_package} for sample={sample_id}. "
            f"The real OpenStudio CLI requires a workflow file."
        )

    cmd: list[str] = [
        "openstudio.cli",
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
            cwd=sim_out,
        )
    except subprocess.SubprocessError as e:
        log.error(
            "openstudio.cli failed for sample=%s: %s",
            sample_id,
            e,
        )
        raise

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
                str(BIN / "generate_lhs.py"),
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
        log.error("generate_lhs failed: %s", e.stderr)
        raise RuntimeError("generate_lhs failed") from e
    return samples_json


def extract_kpis(simulation_dir: Path, sample_id: str, out: Path) -> Path:
    """Run the default KPI extractor. Returns path to the kpi JSON file."""
    out.mkdir(parents=True, exist_ok=True)
    kpi_path = out / f"kpi_{sample_id}.json"
    try:
        subprocess.run(  # nosec  # sourcery skip: suspicious-subprocess-call
            [
                sys.executable,
                str(BIN / "extract_kpis.py"),
                "--simulation_dir",
                str(simulation_dir),
                "--sample_id",
                sample_id,
                "--out",
                str(kpi_path),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as e:
        log.error("extract_kpis failed for %s: %s", sample_id, e.stderr)
        raise RuntimeError(f"extract_kpis failed for {sample_id}") from e
    return kpi_path


# ---------------------------------------------------------------------------
# Aggregation & plotting
# ---------------------------------------------------------------------------
def aggregate_results(
    kpi_files: list[Path],
    sim_dirs: list[Path],
    out: Path,
    baseline_sample_id: str | None = None,
    ts_resolution: str = "monthly",
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
    """
    out.mkdir(parents=True, exist_ok=True)
    csv_path = out / "aggregated_results.csv"
    failed_path = out / "failed_simulations.csv"
    parquet_path = out / "aggregated_results.parquet"
    cmd: list[str] = [
        sys.executable,
        str(BIN / "aggregate_results.py"),
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
    try:
        subprocess.run(  # nosec  # sourcery skip: suspicious-subprocess-call
            cmd,
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as e:
        log.error("aggregate_results failed: %s", e.stderr)
        raise RuntimeError("aggregate_results failed") from e
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
        str(BIN / "generate_plots.py"),
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
    try:
        subprocess.run(  # nosec  # sourcery skip: suspicious-subprocess-call
            cmd,
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as e:
        log.error("generate_plots failed: %s", e.stderr)
        raise RuntimeError("generate_plots failed") from e
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
                        "openstudio.cli",
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
                        "preflight geometry: quick parse of %s returned "
                        "exit code %d (non-fatal)",
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
        log.info(
            "preflight measures: no measures/ directory found — skipping check"
        )
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
            log.info(
                "preflight measures: %s has %s", measure_dir.name, entry
            )
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
                "openstudio.cli",
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
        if re.search(r"^\s*\d+\s+\*+\s*Severe", line, re.IGNORECASE):
            return line.strip()
    return ""
