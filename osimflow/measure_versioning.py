"""Measure versioning support for OSimFlow campaigns (issue #430).

This module provides:

- Version detection from ``measure.rb`` and ``measure.py`` files.
- Version info stored alongside campaign results.
- Comparison of required vs installed measure versions.
- Listing all measure versions used in a campaign.

Measure version is read from the ``version_id`` field in the measure
entry-point file header comment.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import cast

log = logging.getLogger("osimflow.measure_versioning")

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class MeasureVersion:
    """Version information for a single measure.

    Attributes:
        name: measure directory name.
        path: absolute path to the measure directory.
        version: version string, or ``"unknown"`` if not found.
        language: ``"ruby"`` or ``"python"``.
    """

    name: str
    path: Path
    version: str
    language: str


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class MeasureVersioningError(ValueError):
    """Base exception for measure versioning errors."""


# ---------------------------------------------------------------------------
# Version detection
# ---------------------------------------------------------------------------


def detect_measure_version(measure_dir: Path) -> MeasureVersion:
    """Read version from a measure's entry-point file.

    Inspects ``measure.rb`` (Ruby) or ``measure.py`` (Python) in
    *measure_dir* and extracts the ``version_id`` from the header
    comment block.  If no version is found, returns ``"unknown"``.

    Parameters
    ----------
    measure_dir
        Path to the measure directory containing ``measure.rb`` or
        ``measure.py``.

    Returns
    -------
    MeasureVersion
        A ``MeasureVersion`` with the detected version or ``"unknown"``.

    Raises
    ------
    MeasureVersioningError
        If *measure_dir* does not contain a ``measure.rb`` or
        ``measure.py`` file.
    """
    rb_path = measure_dir / "measure.rb"
    py_path = measure_dir / "measure.py"

    if rb_path.is_file():
        version = _extract_version_from_ruby(rb_path)
        return MeasureVersion(
            name=measure_dir.name,
            path=measure_dir,
            version=version,
            language="ruby",
        )
    if py_path.is_file():
        version = _extract_version_from_python(py_path)
        return MeasureVersion(
            name=measure_dir.name,
            path=measure_dir,
            version=version,
            language="python",
        )

    raise MeasureVersioningError(f"No measure.rb or measure.py found in {measure_dir}")


def _extract_version_from_ruby(rb_path: Path) -> str:
    """Extract ``version_id`` from a Ruby measure file.

    Looks for a header comment block containing
    ``version_id`` followed by a string or number literal.
    Falls back to ``"unknown"`` if not found.
    """
    try:
        text = rb_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        log.warning("Could not read %s: %s", rb_path, exc)
        return "unknown"

    version = _parse_version_from_comment(text)
    if version:
        return version

    return "unknown"


def _extract_version_from_python(py_path: Path) -> str:
    """Extract ``version_id`` from a Python measure file.

    Looks for a header docstring or comment block containing
    ``version_id``.  Falls back to ``"unknown"`` if not found.
    """
    try:
        text = py_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        log.warning("Could not read %s: %s", py_path, exc)
        return "unknown"

    version = _parse_version_from_comment(text)
    if version:
        return version

    return "unknown"


_VERSION_PATTERN = re.compile(
    r"""
    (?P<prefix>version[_\s]?id\s*[=:]\s*)
    (?P<quote>["'])?
    (?P<version>\S+)
    """,
    re.IGNORECASE | re.VERBOSE,
)


def _parse_version_from_comment(text: str) -> str | None:
    """Parse a version string from a measure file's header comment.

    Searches the first 30 lines for a ``version_id`` declaration.
    Returns the version string (stripped) or ``None`` if not found.
    """
    lines = text.splitlines()[:30]
    for line in lines:
        m = _VERSION_PATTERN.search(line)
        if m:
            raw = m.group("version").rstrip("'\",; \t")
            if raw and raw != "unknown":
                return raw
    return None


# ---------------------------------------------------------------------------
# Scan measures in a package
# ---------------------------------------------------------------------------


def scan_measure_versions(template_sim_package: Path) -> list[MeasureVersion]:
    """Scan all measures under ``template_sim_package/measures/``.

    Recursively discovers ``measure.rb`` and ``measure.py`` files and
    reads the version from each.  Returns an empty list if no measures
    are found.

    Parameters
    ----------
    template_sim_package
        Path to the template simulation package directory.

    Returns
    -------
    list[MeasureVersion]
        List of discovered measure versions, in sorted order by name.
    """
    measures_dir = template_sim_package / "measures"
    if not measures_dir.is_dir():
        log.info("scan_measure_versions: no measures/ directory found in %s", template_sim_package)
        return []

    results: list[MeasureVersion] = []
    for measure_dir in sorted(measures_dir.iterdir()):
        if not measure_dir.is_dir():
            continue
        rb_path = measure_dir / "measure.rb"
        py_path = measure_dir / "measure.py"
        if not (rb_path.is_file() or py_path.is_file()):
            continue
        try:
            mv = detect_measure_version(measure_dir)
            results.append(mv)
            log.debug(
                "scan_measure_versions: %s version=%s",
                mv.name,
                mv.version,
            )
        except MeasureVersioningError:
            log.warning(
                "scan_measure_versions: could not read measure at %s",
                measure_dir,
            )
    return results


# ---------------------------------------------------------------------------
# Version comparison
# ---------------------------------------------------------------------------


@dataclass
class VersionMismatch:
    """A single version incompatibility between required and installed."""

    measure_name: str
    required_version: str
    installed_version: str


def compare_measure_versions(
    required: dict[str, str],
    installed: dict[str, str],
) -> list[VersionMismatch]:
    """Compare required vs installed measure versions.

    Parameters
    ----------
    required
        Mapping of measure name to required version string.
    installed
        Mapping of measure name to installed version string.

    Returns
    -------
    list[VersionMismatch]
        List of mismatches where the installed version does not match
        the required version.  Measures in *installed* but not in
        *required* are ignored.  ``"unknown"`` versions always produce
        a mismatch.
    """
    mismatches: list[VersionMismatch] = []
    for name, req_ver in sorted(required.items()):
        inst_ver = installed.get(name, "unknown")
        if inst_ver == "unknown":
            mismatches.append(
                VersionMismatch(
                    measure_name=name,
                    required_version=req_ver,
                    installed_version="unknown",
                )
            )
        elif inst_ver != req_ver:
            mismatches.append(
                VersionMismatch(
                    measure_name=name,
                    required_version=req_ver,
                    installed_version=inst_ver,
                )
            )
    return mismatches


# ---------------------------------------------------------------------------
# Campaign run.json integration
# ---------------------------------------------------------------------------


def write_measure_versions(outdir: Path, measures: list[MeasureVersion]) -> Path:
    """Write measure version info to a JSON file in the campaign output.

    Writes ``${outdir}/measure_versions.json`` containing a list of
    measure version records.

    Parameters
    ----------
    outdir
        Campaign output directory.
    measures
        List of discovered ``MeasureVersion`` objects.

    Returns
    -------
    Path
        Absolute path to the written JSON file.
    """
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    path = outdir / "measure_versions.json"

    records: list[dict[str, str]] = [
        {
            "name": m.name,
            "path": str(m.path),
            "version": m.version,
            "language": m.language,
        }
        for m in measures
    ]

    path.write_text(json.dumps(records, indent=2), encoding="utf-8")
    log.info("Wrote measure versions to %s (%d measures)", path, len(measures))
    return path


def read_measure_versions(path: Path) -> list[dict[str, str]]:
    """Read measure versions from a JSON file.

    Parameters
    ----------
    path
        Path to ``measure_versions.json`` (usually in the campaign
        output directory).

    Returns
    -------
    list[dict]
        List of measure version records.  Returns an empty list if
        *path* does not exist or cannot be parsed.
    """
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return cast(list[dict[str, str]], data)
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("Could not read measure versions from %s: %s", path, exc)
        return []


def installed_versions_from_json(path: Path) -> dict[str, str]:
    """Build a name→version dict from a measure_versions.json file.

    Parameters
    ----------
    path
        Path to ``measure_versions.json``.

    Returns
    -------
    dict[str, str]
        Mapping of measure name to version string.
    """
    records = read_measure_versions(path)
    return {rec["name"]: rec["version"] for rec in records}


# ---------------------------------------------------------------------------
# List all measure versions from a campaign directory
# ---------------------------------------------------------------------------


def list_campaign_measure_versions(outdir: Path) -> list[dict[str, str]]:
    """List all measure versions recorded in a campaign's output.

    Reads ``${outdir}/measure_versions.json`` if it exists.

    Parameters
    ----------
    outdir
        Campaign output directory.

    Returns
    -------
    list[dict]
        List of measure version records.  Empty list if no version
        info is available.
    """
    path = Path(outdir) / "measure_versions.json"
    return read_measure_versions(path)
