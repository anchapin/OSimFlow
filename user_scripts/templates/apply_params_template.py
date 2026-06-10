"""BYOS template: custom parameter applicator.

Copy this file into ``user_scripts/`` and customise the
``apply_parameters`` function to modify the template model for
each sample.  The framework calls this function once per sample.

Usage::

    cp user_scripts/templates/apply_params_template.py \\
       user_scripts/my_apply.py

    # Edit my_apply.py, then run:
    osimflow run \\
        --custom_apply_script user_scripts/my_apply.py \\
        ...

Required function signature::

    def apply_parameters(
        template: Path,
        parameters: dict[str, object],
        sample_id: str,
        out: Path,
    ) -> Path

Args:
    template:   path to the template simulation package directory.
    parameters: dict mapping variable names to sampled values.
    sample_id:  the sample identifier (e.g. ``"0001"``).
    out:        per-sample output directory (created for you).

Returns:
    The per-sample output directory containing the modified model.

See user_scripts/README.md for the full BYOS contract.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

log = logging.getLogger("my_apply_params")


def apply_parameters(
    template: Path,
    parameters: dict[str, object],
    sample_id: str,
    out: Path,
) -> Path:
    out.mkdir(parents=True, exist_ok=True)

    # Copy the template package into the per-sample directory.
    shutil.copytree(template, out, dirs_exist_ok=True)

    # TODO: Modify the copy here.
    #
    # Common patterns:
    #
    # 1. JSON-mode .osw mutation (no OpenStudio bindings needed):
    #    osw_path = out / "workflow.osw"
    #    osw = json.loads(osw_path.read_text())
    #    for step in osw["steps"]:
    #        args = step.get("arguments", {})
    #        if "my_param" in args and "my_param" in parameters:
    #            args["my_param"] = parameters["my_param"]
    #    osw_path.write_text(json.dumps(osw, indent=2))
    #
    # 2. OpenStudio Python bindings (requires openstudio on PATH):
    #    try:
    #        import openstudio
    #    except ImportError:
    #        raise RuntimeError("OpenStudio Python bindings required")
    #    model = openstudio.model.Model.load(str(out / "in.osm")).get()
    #    # ... modify model ...
    #    model.save(str(out / "in.osm"))

    log.info("sample %s: applied parameters %s", sample_id, list(parameters.keys()))
    return out
