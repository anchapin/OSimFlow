"""BYOS example: modify window-to-wall ratio (WWR) in an .osw workflow.

This is the simplest parameterization example.  It reads the
``.osw`` (OpenStudio Workflow) file from the template package,
finds any measure arguments named ``wwr`` (or ``window_to_wall_ratio``
or ``window_wall_ratio``), and overwrites the value with the sampled
parameter.  The modified copy is written to the per-sample output
directory.

The function never touches the binary ``.osm`` directly, so it works
without the OpenStudio Python bindings installed.  This is the
recommended pattern when the parameter can be expressed as a measure
argument in the workflow file.

Usage::

    osimflow run \\
        --executor local \\
        --custom_apply_script user_scripts/examples/custom_apply_wwr.py \\
        --input_variables variables.yml \\
        --template_sim_package ./example_package \\
        --n_samples 50 \\
        --outdir ./results

In your ``variables.yml``, define the parameter::

    wwr:
      distribution: uniform
      min: 0.15
      max: 0.60

See user_scripts/README.md for the full BYOS contract reference.
"""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path

log = logging.getLogger("custom_apply_wwr")

# Known argument names that BEM practitioners use for WWR.
# The pre-flight check warns if none of these match.
_WWR_ARG_NAMES = {"wwr", "window_to_wall_ratio", "window_wall_ratio"}


def apply_parameters(
    template: Path,
    parameters: dict[str, object],
    sample_id: str,
    out: Path,
) -> Path:
    """Apply WWR parameter by mutating measure arguments in the .osw.

    Args:
        template: path to the template simulation package directory
            (contains ``workflow.osw`` and any measure scripts).
        parameters: dict mapping variable names (e.g. ``"wwr"``) to
            sampled values (e.g. ``0.35``).
        sample_id: the sample identifier (e.g. ``"0001"``).
        out: per-sample output directory.

    Returns:
        The per-sample output directory (``out``), containing the
        modified ``workflow.osw`` and copies of all other template
        files.
    """
    out.mkdir(parents=True, exist_ok=True)

    # Copy the entire template package into the per-sample directory.
    # This preserves measure scripts, weather files, etc.
    shutil.copytree(template, out, dirs_exist_ok=True)

    # Find the workflow file.  OSimFlow convention: root-level
    # ``workflow.osw`` in the template package.
    osw_path = out / "workflow.osw"
    if not osw_path.exists():
        # Fall back to the first .osw found anywhere in the copy.
        candidates = list(out.rglob("*.osw"))
        if not candidates:
            raise FileNotFoundError(
                f"No .osw file found in template package {template}. "
                f"The WWR applicator requires an OpenStudio workflow file."
            )
        osw_path = candidates[0]

    # Load, mutate, write back.
    osw = json.loads(osw_path.read_text())
    _inject_wwr(osw, parameters)
    osw_path.write_text(json.dumps(osw, indent=2))

    log.info(
        "sample %s: applied parameters %s to %s",
        sample_id,
        {k: v for k, v in parameters.items() if k in _WWR_ARG_NAMES},
        osw_path,
    )
    return out


def _inject_wwr(osw: dict, parameters: dict[str, object]) -> None:
    """Walk the .osw measure steps and inject WWR arguments.

    The ``.osw`` format has a ``"steps"`` list.  Each step has an
    ``"arguments"`` dict.  We match argument names from
    ``_WWR_ARG_NAMES`` and overwrite their value with the
    corresponding entry from ``parameters``.
    """
    steps = osw.get("steps", [])
    for step in steps:
        args = step.get("arguments", {})
        for arg_name in list(args.keys()):
            if arg_name in _WWR_ARG_NAMES and arg_name in parameters:
                args[arg_name] = parameters[arg_name]
                log.debug("set %s = %s", arg_name, parameters[arg_name])
