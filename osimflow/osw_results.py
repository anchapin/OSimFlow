"""Measure-reported results and failure messages from ``out.osw`` / ``run.log``.

Issue #1871. OpenStudio Server results
(``download_data.csv``) carry measure-reported outputs such as
``openstudio_results.*`` taken from ``out.osw`` step results; OSimFlow's SQL
extractor never read them. This module ports that logic (and the server's
``extract_failure_message_from_osw`` / ``collect_failure_message_from_run_log``
failure-message helpers; benign-notice filtering is opt-in via ``benign_pattern``).

``out.osw`` / ``run.log`` are published next to ``eplusout.sql`` in the
per-sample simulation directory by ``osimflow.work.run_openstudio_sim``
(on success *and* on a failed CLI run).
"""

import json
import logging
import re
from pathlib import Path
from re import Pattern
from typing import Any

log = logging.getLogger("osimflow.osw_results")

OUT_OSW_NAME = "out.osw"
RUN_LOG_NAME = "run.log"
FAILED_MARKER_NAME = "_OSIMFLOW_FAILED"
RETRYING_STATE = "retrying"
MEASURE_RESULTS_TOKEN = "measure_results"
SIMULATION_FAILED_KEY = "simulation_failed_message"

_TS_PREFIX = re.compile(r"^\[[^\]]+\]\s*")
_ERROR_LINE = re.compile(r"^\[[^\]]+ERROR")
_MODEL_FAIL = re.compile(r"\[openstudio\.model\.Model\].*did not finish.*errors:", re.IGNORECASE)
_WORKFLOW_MEASURE = re.compile(r"\[openstudio\.workflow\.OSWorkflow\].*Measure '([^']+)'")
_FATAL = re.compile(r"\bFATAL\b", re.IGNORECASE)


def _load_osw(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        log.warning("could not parse %s", path, exc_info=True)
        return None
    return data if isinstance(data, dict) else None


def parse_out_osw(path: Path) -> dict[str, Any]:
    """Return ``{"<measure_dir_name>.<attribute>": value}`` for every step value.

    Reads ``steps[].result.step_values`` (``registerValue`` outputs). When
    the same measure directory appears in several steps, the
    ``<measure_dir_name>.*`` keys hold the last instance (unchanged), and each
    instance's values are additionally kept under its distinct step ``"name"``
    (``"<step_name>.<attribute>"``) so earlier instances are not lost. Step
    names that are not unique or that equal another ``measure_dir_name`` are
    ambiguous and not emitted.
    Returns ``{}`` when the file is absent or unparseable.
    """
    if not path.is_file():
        return {}
    osw = _load_osw(path)
    values: dict[str, Any] = {}
    steps = (osw or {}).get("steps") or []
    dir_counts: dict[str, int] = {}
    name_counts: dict[str, int] = {}
    for step in steps:
        if isinstance(step, dict) and step.get("measure_dir_name"):
            key = str(step["measure_dir_name"])
            dir_counts[key] = dir_counts.get(key, 0) + 1
            if isinstance(step.get("name"), str) and step["name"]:
                name_counts[step["name"]] = name_counts.get(step["name"], 0) + 1
    for step in steps:
        if not isinstance(step, dict):
            continue
        measure = step.get("measure_dir_name") or step.get("name")
        result = step.get("result")
        if not measure or not isinstance(result, dict):
            continue
        prefixes = [str(measure)]
        step_name = step.get("name")
        if (
            step.get("measure_dir_name")
            and dir_counts.get(str(measure), 0) > 1
            and isinstance(step_name, str)
            and step_name
            and step_name not in dir_counts
            and name_counts.get(step_name) == 1
        ):
            prefixes.append(step_name)
        for item in result.get("step_values") or []:
            if isinstance(item, dict) and item.get("name") is not None and "value" in item:
                for prefix in prefixes:
                    values[f"{prefix}.{item['name']}"] = item["value"]
    return values


def has_osw_or_marker_failure(sim_dir: Path) -> bool:
    """Return ``True`` when *sim_dir* has a non-retrying ``_OSIMFLOW_FAILED``
    marker or an ``out.osw`` reporting workflow/step failure (issue #2059)."""
    failed_marker = sim_dir / FAILED_MARKER_NAME
    if failed_marker.is_file():
        try:
            content = failed_marker.read_text(encoding="utf-8", errors="replace").strip().lower()
        except OSError:
            content = ""
        if content != RETRYING_STATE:
            return True

    osw = _load_osw(sim_dir / OUT_OSW_NAME)
    if osw is None:
        return False
    if str(osw.get("completed_status") or "").strip().lower() == "fail":
        return True
    steps = osw.get("steps")
    return isinstance(steps, list) and any(
        isinstance(step, dict)
        and isinstance(step.get("result"), dict)
        and str(step["result"].get("step_result") or "").strip().lower() == "fail"
        for step in steps
    )


def has_valid_osw_completion(osw_path: Path) -> bool:
    """Return ``True`` when *osw_path* proves a clean, value-bearing ``out.osw`` run.

    Strict completion gate (issue #2051): returns ``True`` only when:
    1. *osw_path* exists and parses as a JSON object,
    2. top-level ``completed_status == "Success"``,
    3. no step in ``steps`` has ``result.step_result == "Fail"``, and
    4. :func:`parse_out_osw` returns at least one measure-registered step value.
    """
    if not osw_path.is_file():
        return False
    osw = _load_osw(osw_path)
    if osw is None or osw.get("completed_status") != "Success":
        return False
    steps = osw.get("steps")
    if isinstance(steps, list):
        for step in steps:
            if not isinstance(step, dict):
                continue
            result = step.get("result")
            if isinstance(result, dict) and result.get("step_result") == "Fail":
                return False
    return bool(parse_out_osw(osw_path))


def extract_failure_message_from_osw(
    osw_content: str, benign_pattern: Pattern[str] | None = None
) -> str | None:
    """First failed step -> ``"<measure>: <first error line>"`` from ``out.osw`` ``steps``.

    Errors matching the optional caller-supplied *benign_pattern* are skipped;
    by default nothing is filtered.
    """
    try:
        osw = json.loads(osw_content)
    except json.JSONDecodeError:
        return None
    for step in (osw.get("steps") or []) if isinstance(osw, dict) else []:
        result = step.get("result") if isinstance(step, dict) else None
        if not isinstance(result, dict) or result.get("step_result") != "Fail":
            continue
        errors = result.get("step_errors") or []
        if not errors:
            continue
        measure = step.get("measure_dir_name") or step.get("name") or "unknown"
        lines = str(errors[0]).splitlines()
        first = lines[0].strip() if lines else ""
        if benign_pattern is not None and benign_pattern.search(first):
            continue
        return f"{measure}: {first}"
    return None


def collect_failure_message_from_run_log(
    content: str, benign_pattern: Pattern[str] | None = None
) -> str | None:
    """Most informative failure message from ``run.log``.

    Priority: model "did not finish" error + following ERROR lines, then the
    first OSRunner ERROR + following ERROR lines, then the first FATAL line.
    The failing measure name (from the OSWorkflow line) is prepended. Lines
    matching the optional caller-supplied *benign_pattern* are dropped.
    """
    lines = content.splitlines()
    measure_name = None
    for line in lines:
        match = _WORKFLOW_MEASURE.search(line)
        if match:
            measure_name = match.group(1)
            break

    def build(collected: list[str]) -> str | None:
        text = "\n".join(
            stripped
            for stripped in (_TS_PREFIX.sub("", ln).strip() for ln in collected)
            if stripped and not (benign_pattern and benign_pattern.search(stripped))
        )
        if not text:
            return None
        return f"{measure_name}: {text}" if measure_name else text

    def collect_from(index: int) -> list[str]:
        collected: list[str] = []
        for line in lines[index:]:
            if collected and not _ERROR_LINE.match(line):
                break
            collected.append(line)
        return collected

    start = next((i for i, ln in enumerate(lines) if _MODEL_FAIL.search(ln)), None)
    if start is not None and (result := build(collect_from(start))):
        return result
    start = next(
        (
            i
            for i, ln in enumerate(lines)
            if _ERROR_LINE.match(ln) and "[openstudio.measure.OSRunner]" in ln
        ),
        None,
    )
    if start is not None and (result := build(collect_from(start))):
        return result
    fatal = next((ln for ln in lines if _FATAL.search(ln)), None)
    if fatal is not None:
        text = _TS_PREFIX.sub("", fatal).strip()
        return f"{measure_name}: {text}" if measure_name else text
    return None


def failure_message(sim_dir: Path) -> str | None:
    """Failure message for a sample dir: ``run.log``, ``out.osw``, then ``_OSIMFLOW_FAILED``."""
    run_log = sim_dir / RUN_LOG_NAME
    if run_log.is_file():
        try:
            message = collect_failure_message_from_run_log(
                run_log.read_text(encoding="utf-8", errors="replace")
            )
        except OSError:
            log.warning("could not read %s", run_log, exc_info=True)
            message = None
        if message:
            return message
    out_osw = sim_dir / OUT_OSW_NAME
    if out_osw.is_file():
        try:
            message = extract_failure_message_from_osw(
                out_osw.read_text(encoding="utf-8", errors="replace")
            )
        except OSError:
            log.warning("could not read %s", out_osw, exc_info=True)
            message = None
        if message:
            return message
    failed_marker = sim_dir / FAILED_MARKER_NAME
    if failed_marker.is_file():
        try:
            marker_text = failed_marker.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            log.warning("could not read %s", failed_marker, exc_info=True)
            marker_text = ""
        if marker_text and marker_text.lower() != RETRYING_STATE:
            return marker_text
    return None
