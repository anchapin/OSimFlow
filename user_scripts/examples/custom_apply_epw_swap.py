"""BYOS example: swap the .epw weather file for multi-climate studies.

This parameter applicator modifies the ``.osw`` workflow file to
point at a different weather file (``.epw``) for each sample.
It is designed for parametric campaigns that study building
performance across multiple climate zones.

The weather file path in the ``.osw`` is found under
``"weather_file"`` at the top level, or inside a measure step's
arguments (e.g. a ``set_weather_file`` or ``ChangeBuildingLocation``
measure).  This script handles both locations.

Usage::

    osimflow run \\
        --executor local \\
        --custom_apply_script user_scripts/examples/custom_apply_epw_swap.py \\
        --input_variables variables.yml \\
        --template_sim_package ./example_package \\
        --n_samples 30 \\
        --outdir ./results

In your ``variables.yml``, define the parameter::

    epw_file:
      distribution: choice
      choices:
        - weather/USA_CO_Denver.epw
        - weather/USA_FL_Miami.epw
        - weather/USA_MN_Minneapolis.epw

The ``epw_file`` parameter value should be a relative path from the
template package root.  This script validates the file exists before
mutating the workflow.

See user_scripts/README.md for the full BYOS contract reference.
"""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path

log = logging.getLogger("custom_apply_epw_swap")


def apply_parameters(
    template: Path,
    parameters: dict[str, object],
    sample_id: str,
    out: Path,
) -> Path:
    """Apply weather-file swap by mutating the .osw workflow.

    Args:
        template: path to the template simulation package directory.
        parameters: dict containing ``"epw_file"`` with a relative
            path to the desired weather file.
        sample_id: the sample identifier (e.g. ``"0001"``).
        out: per-sample output directory.

    Returns:
        The per-sample output directory containing the modified
        ``workflow.osw`` and all template files.
    """
    out.mkdir(parents=True, exist_ok=True)
    shutil.copytree(template, out, dirs_exist_ok=True)

    epw_relative = parameters.get("epw_file")
    if epw_relative is None:
        log.warning(
            "sample %s: no 'epw_file' in parameters, skipping weather swap",
            sample_id,
        )
        return out

    epw_relative = str(epw_relative)
    epw_abs = out / epw_relative
    if not epw_abs.exists():
        raise FileNotFoundError(
            f"Weather file not found for sample {sample_id}: "
            f"{epw_abs} (parameter: epw_file={epw_relative}). "
            f"Ensure the .epw file exists in the template package."
        )

    osw_path = _find_osw(out)
    osw = json.loads(osw_path.read_text())
    _set_weather_file(osw, epw_relative)
    osw_path.write_text(json.dumps(osw, indent=2))

    log.info("sample %s: set weather_file=%s", sample_id, epw_relative)
    return out


def _find_osw(directory: Path) -> Path:
    """Locate the workflow.osw in a directory tree."""
    root_osw = directory / "workflow.osw"
    if root_osw.is_file():
        return root_osw
    for osw in directory.rglob("*.osw"):
        return osw
    raise FileNotFoundError(
        f"No .osw file found in {directory}. "
        f"The EPW swap applicator requires an OpenStudio workflow file."
    )


def _set_weather_file(osw: dict, epw_relative: str) -> None:
    """Set the weather file path in the .osw structure.

    Mutates two locations:
      1. The top-level ``"weather_file"`` key (used by OpenStudio CLI).
      2. Any measure argument named ``epw_file`` or ``weather_file``
         inside the ``"steps"`` list (used by weather-swap measures).
    """
    osw["weather_file"] = epw_relative

    for step in osw.get("steps", []):
        args = step.get("arguments", {})
        if "epw_file" in args:
            args["epw_file"] = epw_relative
        if "weather_file" in args:
            args["weather_file"] = epw_relative
