#!/usr/bin/env python3
"""apply_params_to_model.py — Apply a single parameter set to a template.

See docs/OSimFlow.md §4.2 (PROCESS_APPLY_PARAMETERS) for the contract
and §1.4 for the Pre-flight Parameter Applicability Validation rule.

The importable core lives in `osimflow.apply_params`. This CLI module is
a thin wrapper that:

  1. Parses CLI args.
  2. Reads the parameter set JSON.
  3. Delegates to `osimflow.apply_params.apply_parameters`, which:
       a. Detects the template type (.osm or .osw).
       b. Builds the name→mapping index.
       c. Runs the pre-flight check (fail fast on unmapped variables).
       d. If --custom_apply_script is set, dispatches to the user's
          `apply(ctx)` function (BYOS contract; see
          user_scripts/README.md).
       e. Otherwise, copies the template into the per-sample dir and
          mutates the parameters in place.

The OpenStudio Python bindings (`import openstudio`) are imported
lazily inside `osimflow.apply_params.parse_osm_attributes` so that this
script is runnable on hosts that do not have the heavy C++ stack
installed (per AGENTS.md §6). When the bindings are unavailable, the
script falls back to a JSON-mode representation of .osm (file content
must start with ``{``) for testability.
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

    # Lazy import so the OpenStudio binding dependency is optional at
    # import time. AGENTS.md §6 mandates this isolation.
    from osimflow.apply_params import (  # noqa: PLC0415
        UnmappedParameterError,
        apply_parameters,
    )

    try:
        parameters = json.loads(args.parameter_set.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        log.error("Failed to read parameter set %s: %s", args.parameter_set, exc)
        return 2

    try:
        apply_parameters(
            template=args.template,
            parameters=parameters,
            sample_id=args.sample_id,
            out=args.out,
            custom_apply_script=args.custom_apply_script,
        )
    except UnmappedParameterError as exc:
        # Pre-flight failure: print the error to stderr and exit non-zero
        # so the work layer's subprocess.run can detect the failure.
        log.error("Pre-flight check failed for sample_id=%s: %s", args.sample_id, exc)
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except NotImplementedError as exc:
        log.error(
            "apply_params_to_model.py: %s "
            "(sample_id=%s). This usually means the production .osm path "
            "requires the OpenStudio Python bindings, which are not "
            "installed on this host.",
            exc,
            args.sample_id,
        )
        print(f"ERROR: {exc}", file=sys.stderr)
        return 3

    log.info(
        "apply_params_to_model.py: wrote per-sample dir for sample_id=%s -> %s",
        args.sample_id,
        args.out,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
