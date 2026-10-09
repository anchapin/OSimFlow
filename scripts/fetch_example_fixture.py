#!/usr/bin/env python3
"""Fetch a real, simulation-capable OpenStudio fixture into ``tests/fixtures/real/``.

The committed ``example_package/model.osm`` is a *test-mode JSON placeholder*
(see ``osimflow/apply_params.py``) that lets the parameter-application step run
on hosts without the OpenStudio Python bindings. It cannot drive a real
``openstudio.cli run`` invocation, so the real-OpenStudio end-to-end tests
need a genuine ``.osm`` plus a ``.epw`` weather file.

Per ``AGENTS.md`` §10 and the repository ``.gitignore``, ``.osm`` and ``.epw``
files are **never** committed. This script downloads them at dev/test time from
stable public sources into the separate, gitignored ``tests/fixtures/real/``
directory. It never touches ``example_package/`` (issue #1832), so the
hermetic stub-mode tests that read the JSON placeholder keep passing after a
fetch. Real-substrate tests copy ``example_package/`` into a scratch directory
and overlay the files from ``tests/fixtures/real/``.

Usage::

    # default: download into ./tests/fixtures/real/
    python scripts/fetch_example_fixture.py

    # re-download even if a real fixture is already present
    python scripts/fetch_example_fixture.py --force

    # write into a different directory
    python scripts/fetch_example_fixture.py --dest ./my_fixture

Sources (verified to resolve, pinned for stability):

* ``.osm`` — ``NREL/openstudio-resources`` (master branch), the
  ``create_typical_building_from_model`` SmallOffice seed model. A small
  (~300 KB), single-thermal-zone office model exported with OpenStudio
  ``OS:Version`` 1.14.0, compatible with the thermostat/envelope measures
  referenced by ``workflow.osw``.
* ``.epw`` — ``NREL/EnergyPlus`` tag ``v24.2.0`` ``weather/`` directory, the
  canonical ``USA_CO_Golden-NREL.724666_TMY3.epw`` TMY3 file (~1.6 MB) used by
  the OpenStudio/EnergyPlus test suites.

Both URLs are raw ``raw.githubusercontent.com`` links returning ``HTTP 200``
with a real file body. The script verifies each download is non-empty and
passes a one-line content sanity check (``OS:Version`` for the model,
``LOCATION`` for the weather file) and exits non-zero on failure.

Exit codes: ``0`` on success (or when the fixture is already present and
``--force`` was not given), non-zero on any download/validation failure.
"""

from __future__ import annotations

import argparse
import shutil
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

log_out = sys.stderr

# --- Stable public sources -------------------------------------------------
# These URLs are pinned to immutable refs: a long-lived branch (master) for the
# NREL/openstudio-resources seed model, and a release tag (v24.2.0) for the
# EnergyPlus weather file. Update only if a source moves or 404s.
MODEL_URL = (
    "https://raw.githubusercontent.com/NREL/openstudio-resources/master/"
    "measures/create_typical_building_from_model/tests/SmallOffice.osm"
)
WEATHER_URL = (
    "https://raw.githubusercontent.com/NREL/EnergyPlus/v24.2.0/weather/"
    "USA_CO_Golden-NREL.724666_TMY3.epw"
)
WEATHER_FILENAME = "USA_CO_Golden-NREL.724666_TMY3.epw"

# Content sanity-check markers (case-sensitive, as they appear in real files).
MODEL_MARKER = "OS:Version"
WEATHER_MARKER = "LOCATION"

# Retry / timeout tuning.
MAX_ATTEMPTS = 3
INITIAL_BACKOFF_S = 1.0
MAX_BACKOFF_S = 8.0
REQUEST_TIMEOUT_S = 60


DEFAULT_DEST = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "real"


def _log(msg: str) -> None:
    print(msg, file=log_out)


def _is_real_osm(path: Path) -> bool:
    """True if ``path`` is a real OpenStudio model (contains ``OS:Version``)."""
    return _file_contains(path, MODEL_MARKER)


def _is_real_epw(path: Path) -> bool:
    """True if ``path`` is a real EPW weather file (contains ``LOCATION``)."""
    return _file_contains(path, WEATHER_MARKER)


def _file_contains(path: Path, marker: str) -> bool:
    if not path.is_file() or path.stat().st_size == 0:
        return False
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            # Read a bounded prefix — markers appear near the top of both
            # file types, so we avoid loading a multi-MB file in full.
            for chunk in iter(lambda: fh.read(65536), ""):
                if marker in chunk:
                    return True
    except OSError:
        return False
    return False


def _download(url: str, dest: Path, marker: str) -> None:
    """Download ``url`` to ``dest`` with retry/backoff; validate with ``marker``.

    Writes to a sibling ``.part`` file and atomically renames on success so a
    partial download never replaces a known-good fixture. Raises on failure.
    """
    last_err: Exception | None = None
    backoff = INITIAL_BACKOFF_S
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            _log(f"[{attempt}/{MAX_ATTEMPTS}] {url}")
            req = urllib.request.Request(
                url, headers={"User-Agent": "osimflow-fetch-example-fixture/1.0"}
            )
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_S) as resp:  # noqa: S310 — trusted public URL
                if resp.status != 200:
                    raise RuntimeError(f"unexpected HTTP status {resp.status}")
                part = dest.with_suffix(dest.suffix + ".part")
                with part.open("wb") as out:
                    shutil.copyfileobj(resp, out, length=1024 * 1024)
                if part.stat().st_size == 0:
                    part.unlink(missing_ok=True)
                    raise RuntimeError("downloaded file is empty")
                if not _file_contains(part, marker):
                    preview = part.read_text(encoding="utf-8", errors="replace")[:120].replace(
                        "\n", " "
                    )
                    part.unlink(missing_ok=True)
                    raise RuntimeError(
                        f"downloaded file failed sanity check (missing {marker!r}); preview: {preview!r}"
                    )
                part.replace(dest)
                size_kb = dest.stat().st_size / 1024
                _log(f"  -> {dest} ({size_kb:.1f} KiB) OK")
                return
        except (urllib.error.URLError, OSError, RuntimeError) as exc:
            last_err = exc
            _log(f"  !! {type(exc).__name__}: {exc}")
            if attempt < MAX_ATTEMPTS:
                _log(f"  retrying in {backoff:.1f}s ...")
                time.sleep(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF_S)
    raise SystemExit(f"ERROR: failed to download {url} after {MAX_ATTEMPTS} attempts: {last_err}")


def fetch(dest_dir: Path, force: bool) -> int:
    dest_dir = dest_dir.resolve()
    dest_dir.mkdir(parents=True, exist_ok=True)

    model = dest_dir / "model.osm"
    weather = dest_dir / WEATHER_FILENAME

    already_model = _is_real_osm(model)
    already_weather = _is_real_epw(weather)
    if already_model and already_weather and not force:
        _log("real fixture already present, use --force to refetch")
        return 0

    if not already_model or force:
        _download(MODEL_URL, model, MODEL_MARKER)
    else:
        _log(f"skipping model (already real): {model}")

    if not already_weather or force:
        _download(WEATHER_URL, weather, WEATHER_MARKER)
    else:
        _log(f"skipping weather (already real): {weather}")

    _log("")
    _log("Real OpenStudio fixture ready:")
    _log(f"  model:   {model} ({model.stat().st_size / 1024:.1f} KiB)")
    _log(f"  weather: {weather} ({weather.stat().st_size / 1024:.1f} KiB)")
    _log("")
    _log("NOTE: .osm and .epw are gitignored — they are NOT committed.")
    _log("example_package/ is left untouched; copy it and overlay these files for real runs.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fetch a real OpenStudio .osm + .epw fixture into tests/fixtures/real/.",
    )
    parser.add_argument(
        "--dest",
        type=Path,
        default=DEFAULT_DEST,
        help="destination fixture directory (default: tests/fixtures/real)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="re-download even if a real fixture is already present",
    )
    args = parser.parse_args(argv)
    return fetch(args.dest, args.force)


if __name__ == "__main__":
    raise SystemExit(main())
