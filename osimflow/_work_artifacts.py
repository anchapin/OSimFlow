"""Moved from ``osimflow.work`` (issue #1890); re-exported there."""

import json
import logging
import re
import shutil
from pathlib import Path

from osimflow.osw_results import OUT_OSW_NAME, RUN_LOG_NAME

log = logging.getLogger("osimflow.work")

# ---------------------------------------------------------------------------
# Simulation work: the heavy step. The real `openstudio.cli run -w`
# invocation lives in `_run_real_openstudio` below (closed #31); a
# stub fallback for tests is selected via OSIMFLOW_STUB_SIM=1.
# ---------------------------------------------------------------------------


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


def _publish_run_artifacts(package_dir: Path, sim_out: Path) -> None:
    """Copy ``out.osw`` / ``run.log`` into *sim_out* (issue #1871).

    Done on success and failure so measure-reported values and failure
    messages survive; best effort, never raises.
    """
    run_dir = package_dir / "run"
    run_log = run_dir / "run.log"
    if not run_log.is_file() and run_dir.is_dir():
        # fall back to the shallowest nested log (sub-workflow runs)
        nested = sorted(run_dir.rglob("run.log"), key=lambda p: (len(p.parts), str(p)))
        run_log = nested[0] if nested else run_log
    for src, name in (
        (package_dir / "out.osw", OUT_OSW_NAME),
        (run_log, RUN_LOG_NAME),
    ):
        try:
            if src.is_file():
                sim_out.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, sim_out / name)
        except OSError:
            log.warning("could not publish %s to %s", src, sim_out, exc_info=True)


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
