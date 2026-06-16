"""Weather file (.epw) operations — validation, discovery, and download.

This module provides helpers for working with EnergyPlus Weather (EPW)
files within the OSimFlow framework. The primary operations are:

  * **Validation** — verify that a file is a valid EPW by checking its
    header line starts with ``LOCATION``.
  * **Discovery** — list all ``.epw`` files in the weather subdirectory
    of a ``template_sim_package``.
  * **Download** — opt-in utility to download EPW files from the
    EnergyPlus weather repository given a station name or coordinates.

Package structure convention (issue #63)::

    template_sim_package/
    ├── model/
    │   ├── workflow.osw
    │   └── base_model.osm
    ├── weather/
    │   ├── USA_CA_Los.Angeles.epw
    │   └── USA_NY_New.York.epw
    ├── measures/
    │   └── ...
    └── variables.yml

The ``weather/`` subdirectory is configurable via
:class:`osimflow.config.CampaignConfig.weather_dir`.
"""

import logging
import re
import urllib.request
from pathlib import Path

log = logging.getLogger("osimflow.weather")

# Base URL for the EnergyPlus weather file repository.
# The onebuilding.org repository hosts EPW files organized by WMO region.
EPW_REPOSITORY_BASE = "https://energyplus-weather.s3.amazonaws.com"

# Maximum size for an EPW file download (50 MB). Typical EPW files are
# 1–5 MB; anything larger is likely an error or corrupted download.
MAX_EPW_DOWNLOAD_BYTES = 50 * 1024 * 1024


class EPWValidationError(ValueError):
    """Raised when a file fails EPW format validation."""


class EPWDownloadError(RuntimeError):
    """Raised when an EPW file download fails."""


def validate_epw(path: Path) -> bool:
    """Validate that a file is a well-formed EPW file.

    Checks that the file exists, is non-empty, and its first line starts
    with ``LOCATION`` — the standard EPW header row that contains station
    metadata (name, latitude, longitude, timezone, elevation).

    Args:
        path: absolute or relative path to the ``.epw`` file.

    Returns:
        ``True`` if the file passes validation.

    Raises:
        EPWValidationError: the file does not exist, is empty, or the
            header line does not start with ``LOCATION``.
    """
    if not path.is_file():
        raise EPWValidationError(f"EPW file not found: {path}")

    try:
        first_line = path.read_text(encoding="utf-8", errors="replace").splitlines()[0]
    except (OSError, IndexError) as exc:
        raise EPWValidationError(f"Cannot read EPW file {path}: {exc}") from exc

    if not first_line.strip().startswith("LOCATION"):
        raise EPWValidationError(
            f"Invalid EPW file {path}: first line must start with 'LOCATION', "
            f"got: {first_line[:80]!r}"
        )

    log.debug("EPW validation passed: %s", path)
    return True


def validate_epw_header(path: Path) -> dict[str, str]:
    """Parse the LOCATION header of an EPW file into a metadata dict.

    The EPW ``LOCATION`` row has 9 comma-separated fields::

        LOCATION, City, StateProvince, Country, WMO, Latitude, Longitude, TZ, Elevation

    Args:
        path: path to the EPW file.

    Returns:
        A dict with keys: ``city``, ``state_province``, ``country``,
        ``wmo``, ``latitude``, ``longitude``, ``timezone``, ``elevation``.

    Raises:
        EPWValidationError: the file is not a valid EPW or the LOCATION
            row has fewer than the expected 9 fields.
    """
    validate_epw(path)  # raises on invalid

    first_line = path.read_text(encoding="utf-8", errors="replace").splitlines()[0]
    fields = first_line.split(",")
    if len(fields) < 9:
        raise EPWValidationError(
            f"EPW LOCATION row in {path} has {len(fields)} fields; expected at least 9"
        )

    return {
        "city": fields[1].strip(),
        "state_province": fields[2].strip(),
        "country": fields[3].strip(),
        "wmo": fields[4].strip(),
        "latitude": fields[5].strip(),
        "longitude": fields[6].strip(),
        "timezone": fields[7].strip(),
        "elevation": fields[8].strip(),
    }


def discover_epw_files(
    template_dir: Path,
    weather_subdir: str = "weather",
) -> list[Path]:
    """Discover all ``.epw`` files in the template package's weather directory.

    Looks for ``.epw`` files in ``template_dir / weather_subdir`` and
    returns them as a sorted list. If the weather directory does not
    exist, returns an empty list (this is not an error — a campaign
    may not use per-sample weather files).

    Args:
        template_dir: the ``template_sim_package`` directory.
        weather_subdir: name of the weather subdirectory (default
            ``"weather"``). Configurable via
            :attr:`osimflow.config.CampaignConfig.weather_dir`.

    Returns:
        Sorted list of ``.epw`` file paths.
    """
    weather_dir = template_dir / weather_subdir
    if not weather_dir.is_dir():
        log.debug("weather directory %s does not exist; no EPW files found", weather_dir)
        return []
    epw_files = sorted(weather_dir.glob("*.epw"))
    log.debug("discovered %d EPW file(s) in %s", len(epw_files), weather_dir)
    return epw_files


def validate_all_epw_files(
    template_dir: Path,
    weather_subdir: str = "weather",
) -> list[Path]:
    """Validate all EPW files in the template package's weather directory.

    Convenience wrapper around :func:`discover_epw_files` and
    :func:`validate_epw`. Validates each file and returns the list of
    valid paths.

    Args:
        template_dir: the ``template_sim_package`` directory.
        weather_subdir: name of the weather subdirectory.

    Returns:
        List of validated EPW file paths.

    Raises:
        EPWValidationError: one or more files fail validation. The error
            message lists every failure.
    """
    epw_files = discover_epw_files(template_dir, weather_subdir)
    if not epw_files:
        return []

    errors: list[str] = []
    valid: list[Path] = []
    for epw in epw_files:
        try:
            validate_epw(epw)
            valid.append(epw)
        except EPWValidationError as exc:
            errors.append(str(exc))

    if errors:
        raise EPWValidationError(
            "EPW file validation failed for the following files:\n"
            + "\n".join(f"  {e}" for e in errors)
        )

    log.info("validated %d EPW file(s) in %s/%s", len(valid), template_dir, weather_subdir)
    return valid


def download_epw(
    station_name: str,
    dest_dir: Path,
    *,
    region: str = "north_and_central_america_wmo_region_4",
    country: str = "USA",
) -> Path:
    """Download an EPW file from the EnergyPlus weather repository.

    This is an **opt-in** utility (issue #63). It respects
    offline/corporate-network users — it is never called automatically
    by the campaign pipeline. The user must explicitly invoke it
    (e.g. from a setup script or CLI helper).

    The URL is constructed as::

        {EPW_REPOSITORY_BASE}/{region}/{country}/{station_name}/{station_name}.epw

    For example::

        https://energyplus-weather.s3.amazonaws.com/north_and_central_america_wmo_region_4/USA/USA_CA_Los.Angeles/USA_CA_Los.Angeles.epw

    Args:
        station_name: the station identifier used in the EPW filename
            (e.g. ``"USA_CA_Los.Angeles"``).
        dest_dir: directory to save the downloaded file. Created if it
            does not exist.
        region: WMO region subdirectory (default
            ``"north_and_central_america_wmo_region_4"``).
        country: country code subdirectory (default ``"USA"``).

    Returns:
        Path to the downloaded EPW file.

    Raises:
        EPWDownloadError: the download failed (network error, HTTP
            error, or the file exceeds the size limit).
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / f"{station_name}.epw"

    if dest_path.exists():
        log.info("EPW file already exists at %s; skipping download", dest_path)
        return dest_path

    url = f"{EPW_REPOSITORY_BASE}/{region}/{country}/{station_name}/{station_name}.epw"
    log.info("downloading EPW: %s -> %s", url, dest_path)

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "osimflow/0.1"})
        with urllib.request.urlopen(req, timeout=120) as resp:  # nosec
            content_length = resp.headers.get("Content-Length")
            if content_length and int(content_length) > MAX_EPW_DOWNLOAD_BYTES:
                raise EPWDownloadError(
                    f"EPW download too large: {content_length} bytes "
                    f"(max {MAX_EPW_DOWNLOAD_BYTES}). URL: {url}"
                )
            data = resp.read(MAX_EPW_DOWNLOAD_BYTES + 1)
            if len(data) > MAX_EPW_DOWNLOAD_BYTES:
                raise EPWDownloadError(
                    f"EPW download exceeded {MAX_EPW_DOWNLOAD_BYTES} bytes. URL: {url}"
                )
    except urllib.error.URLError as exc:
        raise EPWDownloadError(f"Failed to download EPW from {url}: {exc}") from exc

    dest_path.write_bytes(data)
    log.info("downloaded EPW: %s (%d bytes)", dest_path, len(data))
    return dest_path


def detect_climate_zone_from_stat(stat_path: Path) -> str | None:
    """Detect ASHRAE climate zone from the first line of a .stat file.

    The ASHRAE .stat file header (EnergyPlus weather file metadata) contains
    the climate zone in its first line, e.g.::

        USA_CA_Los.Angeles - TMYx, ASHRAE 169-2006-, 6B

    The climate zone appears at the end of the first line as a numeric
    subcategory code (``A``/``B``/``C``) appended to a number (1-8),
    e.g. ``6B``, ``3C``, ``4A``.

    Args:
        stat_path: path to the ``.stat`` companion file of an EPW.

    Returns:
        The detected ASHRAE climate zone string (e.g. ``"6B"``), or
        ``None`` if the file is missing, empty, or no zone pattern was found.
    """
    if not stat_path.is_file():
        log.debug("stat file not found: %s", stat_path)
        return None

    try:
        first_line = stat_path.read_text(encoding="utf-8", errors="replace").splitlines()[0]
    except (OSError, IndexError) as exc:
        log.debug("failed to read stat file %s: %s", stat_path, exc)
        return None

    if not first_line.strip():
        log.debug("stat file is empty: %s", stat_path)
        return None

    match = re.search(r"\b(\d+[A-Za-z])\b", first_line)
    if not match:
        log.debug("no ASHRAE climate zone pattern found in: %s", stat_path)
        return None

    zone = match.group(1)
    log.debug("detected climate zone %r from %s", zone, stat_path)
    return zone
