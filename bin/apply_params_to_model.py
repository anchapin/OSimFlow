#!/usr/bin/env python3
"""apply_params_to_model.py — Apply a single parameter set to a template.

See docs/OSimFlow.md §4.2 (PROCESS_APPLY_PARAMETERS) for the contract.

This is a SKELETON. Implementation TODO:

  1. Open the template_sim_package (a base .osm model OR a .osw workflow).
  2. For each key in the parameter_set dict, look up the corresponding
     OpenStudio model attribute or measure argument.
  3. PRE-FLIGHT CHECK (PRD §1.4): if a parameter name doesn't map to any
     existing attribute/argument, FAIL FAST with a clear error message —
     do NOT start a doomed simulation.
  4. If `--custom_apply_script` is provided, defer to that script via a
     defined interface (see user_scripts/README.md).
  5. Write a per-sample modified directory containing the .osm/.osw +
     any bundled custom measure scripts.

Run with `--help` once implemented.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("apply_params")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--template", required=True, type=Path)
    parser.add_argument("--parameter_set", required=True, type=Path)
    parser.add_argument("--sample_id", required=True)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--custom_apply_script", type=Path, default=None)
    args = parser.parse_args()

    log.warning("apply_params_to_model.py is a stub — copying template verbatim")
    args.out.mkdir(parents=True, exist_ok=True)

    # Stub: copy template contents (without the dir itself) into out/.
    # A real implementation will modify the .osm/.osw in place.
    for src in args.template.iterdir():
        dest = args.out / src.name
        if src.is_dir():
            import shutil
            shutil.copytree(src, dest, dirs_exist_ok=True)
        else:
            import shutil
            shutil.copy2(src, dest)

    return 0


if __name__ == "__main__":
    sys.exit(main())
