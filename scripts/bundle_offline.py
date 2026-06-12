#!/usr/bin/env python3
"""Bundle OSimFlow assets for air-gapped / offline deployment.

Usage:
    # Bundle everything (pip + docker + weather)
    python scripts/bundle_offline.py \
        --openstudio-version 3.11.0 \
        --pip-extras dev,aws,slurm \
        --output /tmp/osimflow-offline.tar.gz

    # Bundle pip packages only
    python scripts/bundle_offline.py --pip-only --output /tmp/pip-bundle.tar.gz

    # Bundle Docker images only
    python scripts/bundle_offline.py --docker-only \
        --openstudio-version 3.11.0 \
        --output /tmp/docker-bundle.tar.gz

    # Bundle weather files from a variables.yml
    python scripts/bundle_offline.py --weather-only \
        --variables variables.yml \
        --weather-dir ./weather \
        --output /tmp/weather-bundle.tar.gz

The script produces a tar.gz archive containing:
    offline/
        pip/           # pip wheels
        docker/        # Docker image tar files
        weather/       # EPW weather files
        bundle_manifest.json  # metadata + checksums
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import shutil
import subprocess
import sys
import tarfile
import tempfile
from datetime import datetime
from pathlib import Path

log = logging.getLogger("bundle_offline")


# ---------------------------------------------------------------------------
# Supported OpenStudio versions
# ---------------------------------------------------------------------------
SUPPORTED_OS_VERSIONS = ["3.7.0", "3.8.0", "3.9.0", "3.10.0", "3.11.0"]

# Default weather file URLs (EnergyPlus sample year files)
KNOWN_EPW_URLS = {
    "USA_CA_San.Francisco.Intl.AP.724940_TMY3.epw": (
        "https://energyplus.net/weather-download/USA_CA_San.Francisco.Intl.AP.724940_TMY3.epw"
    ),
    "USA_CO_Denver.Intl.AP.725650_TMY3.epw": (
        "https://energyplus.net/weather-download/USA_CO_Denver.Intl.AP.725650_TMY3.epw"
    ),
    "USA_FL_Miami.Intl.AP.722020_TMY3.epw": (
        "https://energyplus.net/weather-download/USA_FL_Miami.Intl.AP.722020_TMY3.epw"
    ),
    "USA_IL_Chicago.OHare.Intl.AP.725300_TMY3.epw": (
        "https://energyplus.net/weather-download/USA_IL_Chicago.OHare.Intl.AP.725300_TMY3.epw"
    ),
    "USA_NY_New.York.LaGuardia.725030_TMY3.epw": (
        "https://energyplus.net/weather-download/USA_NY_New.York.LaGuardia.725030_TMY3.epw"
    ),
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
    log.info("Running: %s", " ".join(cmd))
    return subprocess.run(cmd, check=True, **kwargs)  # type: ignore[no-any-return, call-overload]


def _pip_download(packages: list[str], dest: Path) -> None:
    """Download pip wheels into dest directory."""
    dest.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable,
        "-m",
        "pip",
        "download",
        "--no-deps",
        "--dest",
        str(dest),
        *packages,
    ]
    _run(cmd)
    log.info("Pip wheels saved to %s", dest)


def _docker_pull(image: str, dest: Path) -> Path:
    """Pull a Docker image and save it as a tar archive."""
    dest.mkdir(parents=True, exist_ok=True)
    safe_name = image.replace("/", "_").replace(":", "_")
    tar_path = dest / f"{safe_name}.tar"
    log.info("Pulling Docker image: %s", image)
    _run(["docker", "pull", image])
    _run(["docker", "save", image, "-o", str(tar_path)])
    log.info("Docker image saved to %s", tar_path)
    return tar_path


def _download_epw(url: str, dest: Path) -> Path:
    """Download a single EPW file."""
    dest.mkdir(parents=True, exist_ok=True)
    filename = Path(url).name
    out_path = dest / filename
    log.info("Downloading EPW: %s", url)
    _run(["curl", "-L", "-o", str(out_path), url])
    return out_path


def _discover_epw_in_variables(variables_yml: Path, weather_dir: Path) -> list[Path]:
    """Parse variables.yml and return list of EPW file paths."""
    import yaml  # noqa: PLC0415

    epw_files: list[Path] = []
    try:
        with variables_yml.open() as f:
            data = yaml.safe_load(f)
        if isinstance(data, dict):
            for key, value in data.items():
                if key == "weather_file" and isinstance(value, str):
                    epw_path = weather_dir / value
                    if epw_path.exists():
                        epw_files.append(epw_path)
                elif isinstance(value, dict) and "weather_file" in value:
                    epw_path = weather_dir / value["weather_file"]
                    if epw_path.exists():
                        epw_files.append(epw_path)
    except Exception as exc:
        log.warning("Could not parse variables.yml: %s", exc)
    return epw_files


def _build_manifest(
    pip_dir: Path,
    docker_dir: Path,
    weather_dir: Path,
    openstudio_version: str,
    pip_extras: str,
) -> dict[str, object]:
    """Build bundle metadata JSON."""
    pip_wheels = list(pip_dir.glob("*.whl")) if pip_dir.exists() else []
    docker_images = list(docker_dir.glob("*.tar")) if docker_dir.exists() else []
    weather_files = list(weather_dir.glob("*.epw")) if weather_dir.exists() else []

    manifest = {
        "created_at": datetime.now(datetime.UTC).isoformat(),
        "openstudio_version": openstudio_version,
        "pip_extras": pip_extras,
        "pip_wheels": [{"name": w.name, "sha256": _sha256(w)} for w in pip_wheels],
        "docker_images": [{"name": d.name, "sha256": _sha256(d)} for d in docker_images],
        "weather_files": [{"name": w.name, "sha256": _sha256(w)} for w in weather_files],
    }
    return manifest  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Main bundle logic
# ---------------------------------------------------------------------------
def _bundle_all(
    output: Path,
    openstudio_version: str,
    pip_extras: str,
    variables_yml: Path | None,
    weather_dir: Path | None,
) -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        offline_dir = tmp / "offline"
        pip_dir = offline_dir / "pip"
        docker_dir = offline_dir / "docker"
        weather_bundle_dir = offline_dir / "weather"

        # --- pip ---
        log.info("=== Bundling pip packages ===")
        _pip_download([f"osimflow[{pip_extras}]"], pip_dir)

        # --- Docker ---
        log.info("=== Bundling Docker images ===")
        os_image = f"nrel/openstudio:{openstudio_version}"
        _docker_pull(os_image, docker_dir)
        # Also pull the scientific Python image
        try:
            _docker_pull("ghcr.io/anchapin/scientific_python_image:latest", docker_dir)
        except Exception as exc:
            log.warning("Could not pull scientific_python_image: %s", exc)

        # --- Weather ---
        log.info("=== Bundling weather files ===")
        if variables_yml and weather_dir:
            epw_files = _discover_epw_in_variables(variables_yml, weather_dir)
            for epw in epw_files:
                shutil.copy2(epw, weather_bundle_dir / epw.name)
                log.info("  Copied: %s", epw.name)
        elif weather_dir:
            for epw in weather_dir.glob("*.epw"):
                shutil.copy2(epw, weather_bundle_dir / epw.name)
                log.info("  Copied: %s", epw.name)

        # --- Manifest ---
        manifest = _build_manifest(
            pip_dir,
            docker_dir,
            weather_bundle_dir,
            openstudio_version,
            pip_extras,
        )
        manifest_path = offline_dir / "bundle_manifest.json"
        with manifest_path.open("w") as f:
            json.dump(manifest, f, indent=2)
        log.info("Manifest written to %s", manifest_path)

        # --- Tarball ---
        log.info("=== Creating tarball: %s ===", output)
        with tarfile.open(output, "w:gz") as tf:
            tf.add(offline_dir, arcname="offline")

        log.info("Bundle created: %s (%.1f MB)", output, output.stat().st_size / 1e6)


def _bundle_pip_only(output: Path, pip_extras: str) -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        pip_dir = tmp / "offline" / "pip"
        _pip_download([f"osimflow[{pip_extras}]"], pip_dir)

        manifest = _build_manifest(pip_dir, Path(""), Path(""), "", pip_extras)
        manifest_path = pip_dir.parent / "bundle_manifest.json"
        with manifest_path.open("w") as f:
            json.dump(manifest, f, indent=2)

        with tarfile.open(output, "w:gz") as tf:
            tf.add(pip_dir.parent, arcname="offline")

        log.info("Pip bundle created: %s", output)


def _bundle_docker_only(output: Path, openstudio_version: str) -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        docker_dir = tmp / "offline" / "docker"

        os_image = f"nrel/openstudio:{openstudio_version}"
        _docker_pull(os_image, docker_dir)
        try:
            _docker_pull("ghcr.io/anchapin/scientific_python_image:latest", docker_dir)
        except Exception as exc:
            log.warning("Could not pull scientific_python_image: %s", exc)

        manifest = _build_manifest(Path(""), docker_dir, Path(""), openstudio_version, "")
        manifest_path = docker_dir.parent / "bundle_manifest.json"
        with manifest_path.open("w") as f:
            json.dump(manifest, f, indent=2)

        with tarfile.open(output, "w:gz") as tf:
            tf.add(docker_dir.parent, arcname="offline")

        log.info("Docker bundle created: %s", output)


def _bundle_weather_only(
    output: Path,
    variables_yml: Path | None,
    weather_dir: Path | None,
) -> None:
    if not weather_dir:
        raise ValueError("--weather-dir is required for weather-only bundle")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        weather_bundle_dir = tmp / "offline" / "weather"

        if variables_yml:
            epw_files = _discover_epw_in_variables(variables_yml, weather_dir)
            for epw in epw_files:
                shutil.copy2(epw, weather_bundle_dir / epw.name)
        else:
            for epw in weather_dir.glob("*.epw"):
                shutil.copy2(epw, weather_bundle_dir / epw.name)

        manifest = _build_manifest(Path(""), Path(""), weather_bundle_dir, "", "")
        manifest_path = weather_bundle_dir.parent / "bundle_manifest.json"
        with manifest_path.open("w") as f:
            json.dump(manifest, f, indent=2)

        with tarfile.open(output, "w:gz") as tf:
            tf.add(weather_bundle_dir.parent, arcname="offline")

        log.info("Weather bundle created: %s", output)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Bundle OSimFlow assets for air-gapped / offline deployment.",
    )
    p.add_argument(
        "--output",
        "-o",
        type=Path,
        default=Path("osimflow-offline.tar.gz"),
        help="Output tarball path (default: osimflow-offline.tar.gz)",
    )
    p.add_argument(
        "--openstudio-version",
        default="3.11.0",
        choices=SUPPORTED_OS_VERSIONS,
        help="OpenStudio version to bundle (default: 3.11.0)",
    )
    p.add_argument(
        "--pip-extras",
        default="dev,aws,slurm",
        help="Comma-separated pip extras to bundle (default: dev,aws,slurm)",
    )
    p.add_argument(
        "--variables",
        type=Path,
        default=None,
        help="Path to variables.yml to extract weather file references from",
    )
    p.add_argument(
        "--weather-dir",
        type=Path,
        default=None,
        help="Directory containing .epw weather files",
    )
    p.add_argument(
        "--pip-only",
        action="store_true",
        help="Bundle pip packages only",
    )
    p.add_argument(
        "--docker-only",
        action="store_true",
        help="Bundle Docker images only",
    )
    p.add_argument(
        "--weather-only",
        action="store_true",
        help="Bundle weather files only",
    )
    p.add_argument(
        "--verbose",
        "-v",
        action="count",
        default=0,
        help="Increase verbosity (-v, -vv, -vvv)",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    p = _build_parser()
    args = p.parse_args(argv)

    verbosity = min(args.verbose, 3)
    level = [logging.WARNING, logging.INFO, logging.DEBUG][verbosity]
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(message)s")

    output: Path = args.output

    if args.pip_only:
        _bundle_pip_only(output, args.pip_extras)
    elif args.docker_only:
        _bundle_docker_only(output, args.openstudio_version)
    elif args.weather_only:
        _bundle_weather_only(output, args.variables, args.weather_dir)
    else:
        _bundle_all(
            output=output,
            openstudio_version=args.openstudio_version,
            pip_extras=args.pip_extras,
            variables_yml=args.variables,
            weather_dir=args.weather_dir,
        )

    print(f"\nBundle created: {output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
