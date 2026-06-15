"""Automatic OpenStudio version detection.

Detects the installed OpenStudio version from the environment using
multiple strategies:

1. ``OPENSTUDIO_VERSION`` environment variable (highest priority).
2. ``openstudio --version`` CLI command.
3. Docker container label ``org.openstudio.build-string`` when running
   inside a container.

Raises :class:`VersionDetectionError` when no version can be determined.
"""

import logging
import os
import re
import shutil
import subprocess
from pathlib import Path

log = logging.getLogger("osimflow.version_detection")

# Docker container label for OpenStudio version string
_DOCKER_LABEL = "org.openstudio.build-string"
_OPENSTUDIO_CLI = "openstudio"


class VersionDetectionError(Exception):
    """Raised when OpenStudio version cannot be determined."""


def _check_env_variable() -> str | None:
    """Return ``OPENSTUDIO_VERSION`` env var value if set and non-empty."""
    version = os.environ.get("OPENSTUDIO_VERSION", "").strip()
    if version:
        log.debug("OPENSTUDIO_VERSION env var found: %s", version)
        return version
    return None


def _check_openstudio_cli() -> str | None:
    """Run ``openstudio --version`` and parse the version string."""
    if shutil.which(_OPENSTUDIO_CLI) is None:
        log.debug("openstudio CLI not found on PATH")
        return None

    try:
        result = subprocess.run(
            [_OPENSTUDIO_CLI, "--version"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        log.debug("openstudio --version failed: %s", exc)
        return None

    if result.returncode != 0:
        log.debug("openstudio --version returned exit code %d", result.returncode)
        return None

    return _parse_version_output(result.stdout + result.stderr)


def _parse_version_output(output: str) -> str | None:
    """Parse OpenStudio version from CLI output.

    OpenStudio typically outputs something like:
    ``OpenStudio 3.11.0-rc1+abc123``
    or
    ``OpenStudio 3.7.0``

    We extract the numeric version prefix (e.g. "3.11.0").
    """
    text = output.strip()
    match = re.match(r"OpenStudio\s+(\d+\.\d+\.\d+)", text, re.IGNORECASE)
    if match:
        version = match.group(1)
        log.debug("parsed OpenStudio version from CLI output: %s", version)
        return version

    # Fallback: look for any X.Y.Z pattern in the output
    match = re.search(r"(\d+\.\d+\.\d+)", text)
    if match:
        version = match.group(1)
        log.debug("found version pattern in CLI output: %s", version)
        return version

    log.debug("could not parse version from CLI output: %r", text)
    return None


def _check_docker_container_label() -> str | None:
    """Check Docker container label for OpenStudio version string.

    Reads ``/proc/1/cgroup`` to detect if running inside a Docker container,
    then queries the container label via ``docker inspect``.
    """
    if not _is_running_in_docker():
        return None

    result_version: str | None = None
    try:
        result = subprocess.run(
            ["docker", "inspect", "-f", f"{{{{.Config.Labels.{_DOCKER_LABEL}}}}}", "self"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        log.debug("docker inspect failed: %s", exc)
        return None

    if result.returncode != 0:
        log.debug("docker inspect returned exit code %d", result.returncode)
        return None

    version = result.stdout.strip()
    if not version or version == "<no value>":
        log.debug("docker label %s is empty or not set", _DOCKER_LABEL)
        return None

    # The label value typically includes the full version string
    # e.g. "OpenStudio 3.11.0-rc1+abc123" - parse just the numeric part
    parsed = _parse_version_output(version)
    if parsed:
        log.debug("parsed OpenStudio version from Docker label: %s", parsed)
        result_version = parsed
    elif re.match(r"\d+\.\d+\.\d+", version):
        # Label exists but couldn't parse - return as-is if it looks like a version
        log.debug("using Docker label value directly: %s", version)
        result_version = version
    else:
        log.debug("Docker label value does not look like a version: %r", version)

    return result_version


def _is_running_in_docker() -> bool:
    """Detect if the current process is running inside a Docker container."""
    cgroup_path = Path("/proc/1/cgroup")
    if not cgroup_path.is_file():
        return False

    try:
        text = cgroup_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False

    return "docker" in text.lower() or "containerd" in text.lower()


def detect_openstudio_version() -> str:
    """Detect the installed OpenStudio version.

    Tries multiple strategies in order of priority:

    1. ``OPENSTUDIO_VERSION`` environment variable.
    2. ``openstudio --version`` CLI command.
    3. Docker container label ``org.openstudio.build-string``.

    Returns
    -------
    str
        The detected OpenStudio version string (e.g. ``"3.11.0"``).

    Raises
    ------
    VersionDetectionError
        When no version can be determined from any available source.
    """
    log.debug("starting OpenStudio version detection")

    # Strategy 1: Environment variable
    version = _check_env_variable()
    if version:
        log.info("detected OpenStudio version from environment: %s", version)
        return version

    # Strategy 2: CLI command
    version = _check_openstudio_cli()
    if version:
        log.info("detected OpenStudio version from CLI: %s", version)
        return version

    # Strategy 3: Docker container label
    version = _check_docker_container_label()
    if version:
        log.info("detected OpenStudio version from Docker label: %s", version)
        return version

    log.error("could not detect OpenStudio version")
    raise VersionDetectionError(
        "Could not detect OpenStudio version. "
        "Set the OPENSTUDIO_VERSION environment variable or ensure "
        "openstudio CLI is on PATH."
    )


def get_compatible_container_tag(version: str) -> str:
    """Return the Docker image tag for the given OpenStudio version.

    Parameters
    ----------
    version
        OpenStudio version string (e.g. ``"3.11.0"``).

    Returns
    -------
    str
        Docker image tag in the format ``nrel/openstudio:<version>``.
    """
    tag = f"nrel/openstudio:{version}"
    log.debug("container tag for version %s: %s", version, tag)
    return tag


def verify_version_compatibility(detected: str, expected: str) -> bool:
    """Check if detected version matches expected, allowing minor version mismatch.

    Compares major and minor version components. For example:
    ``"3.11.0"`` is compatible with ``"3.11.0"`` and ``"3.11.1"``.
    ``"3.11.0"`` is NOT compatible with ``"3.12.0"`` or ``"4.0.0"``.

    Parameters
    ----------
    detected
        Auto-detected OpenStudio version string.
    expected
        Expected (user-specified) OpenStudio version string.

    Returns
    -------
    bool
        ``True`` if versions are compatible (same major.minor), ``False``
        otherwise.
    """
    try:
        detected_parts = _parse_version_tuple(detected)
        expected_parts = _parse_version_tuple(expected)
    except ValueError:
        log.warning(
            "could not parse version strings: detected=%r, expected=%r",
            detected,
            expected,
        )
        return False

    detected_major_minor = detected_parts[:2]
    expected_major_minor = expected_parts[:2]

    compatible = detected_major_minor == expected_major_minor
    log.debug(
        "version compatibility check: detected=%s expected=%s compatible=%s",
        detected,
        expected,
        compatible,
    )
    return compatible


def _parse_version_tuple(version: str) -> tuple[int, int, int]:
    """Parse a version string into a (major, minor, patch) tuple.

    Parameters
    ----------
    version
        Version string (e.g. ``"3.11.0"``).

    Returns
    -------
    tuple[int, int, int]
        (major, minor, patch) version components.

    Raises
    ------
    ValueError
        When the version string cannot be parsed.
    """
    match = re.match(r"(\d+)\.(\d+)\.(\d+)", version.strip())
    if not match:
        raise ValueError(f"invalid version string: {version!r}")
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))
