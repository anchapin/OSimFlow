"""Measure-reported results and failure messages from ``out.osw`` / ``run.log``.

Issue #1871. The openstudio-bem-to-surrogate-gem's server results
(``download_data.csv``) carry measure-reported outputs such as
``reporting_179_d.*`` taken from ``out.osw`` step results; OSimFlow's SQL
extractor never read them. This module ports that logic (and the gem's
``extract_failure_message_from_osw`` / ``collect_failure_message_from_run_log``
failure-message helpers, including benign-notice filtering).

``out.osw`` / ``run.log`` are published next to ``eplusout.sql`` in the
per-sample simulation directory by ``osimflow.work.run_openstudio_sim``
(on success *and* on a failed CLI run).
"""

import json
import logging
import re
from pathlib import Path
from typing import Any

log = logging.getLogger("osimflow.osw_results")

OUT_OSW_NAME = "out.osw"
RUN_LOG_NAME = "run.log"
MEASURE_RESULTS_TOKEN = "measure_results"
SIMULATION_FAILED_KEY = "reporting_179_d.simulation_failed_message"

# Non-fatal notices emitted even on passing runs; never a failure message.
BENIGN_ERROR_PATTERN = re.compile(
    r"UseWeatherFile' is selected in YearDescription"
    r"|run_all_orientation flag is set to False"
    r"|This Curve, Object of type 'OS:Curve:Biquadratic' and named"
)
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
    the same measure appears in several steps, the later step wins.
    Returns ``{}`` when the file is absent or unparseable.
    """
    if not path.is_file():
        return {}
    osw = _load_osw(path)
    values: dict[str, Any] = {}
    for step in (osw or {}).get("steps") or []:
        if not isinstance(step, dict):
            continue
        measure = step.get("measure_dir_name") or step.get("name")
        result = step.get("result")
        if not measure or not isinstance(result, dict):
            continue
        for item in result.get("step_values") or []:
            if isinstance(item, dict) and item.get("name") is not None and "value" in item:
                values[f"{measure}.{item['name']}"] = item["value"]
    return values


def extract_failure_message_from_osw(osw_content: str) -> str | None:
    """First failed step -> ``"<measure>: <first error line>"`` (gem parity)."""
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
        if BENIGN_ERROR_PATTERN.search(first):
            continue
        return f"{measure}: {first}"
    return None


def collect_failure_message_from_run_log(content: str) -> str | None:
    """Most informative failure message from ``run.log`` (gem parity).

    Priority: model "did not finish" error + following ERROR lines, then the
    first OSRunner ERROR + following ERROR lines, then the first FATAL line.
    The failing measure name (from the OSWorkflow line) is prepended.
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
            if stripped and not BENIGN_ERROR_PATTERN.search(stripped)
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
    """Failure message for a sample dir: ``run.log`` first, then ``out.osw``."""
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
            return extract_failure_message_from_osw(
                out_osw.read_text(encoding="utf-8", errors="replace")
            )
        except OSError:
            log.warning("could not read %s", out_osw, exc_info=True)
    return None
