#!/usr/bin/env python3
"""apply_params_to_model.py — Apply a single parameter set to a template.

See docs/OSimFlow.md §4.2 (PROCESS_APPLY_PARAMETERS) for the contract
and §1.4 for the Pre-flight Parameter Applicability Validation rule.

The importable core lives in `osimflow.apply_params`. This CLI module is
a thin wrapper that:

  1. Parses CLI args.
  2. Reads the parameter set JSON (must be a JSON object: dict).
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

Exit codes
----------
  0  success
  1  pre-flight check failed (one or more LHS variables unmapped)
  2  failed to read or parse the parameter set JSON
  3  OpenStudio Python bindings are not installed (required for
     binary/XML .osm)
  4  parameter set is valid JSON but is not a dict (e.g. a list or
     scalar was supplied)
  5  feature not implemented (e.g. production .osm mutation path)
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

    try:
        parameters = json.loads(args.parameter_set.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        log.error("Failed to read parameter set %s: %s", args.parameter_set, exc)
        return 2

    if not isinstance(parameters, dict):
        log.error(
            "Invalid parameter set %s: expected a JSON object mapping "
            "variable names to values, got %s.",
            args.parameter_set,
            type(parameters).__name__,
        )
        print(
            f"ERROR: parameter set must be a JSON object (got {type(parameters).__name__})",
            file=sys.stderr,
        )
        return 4

    # Lazy import so the OpenStudio binding dependency is optional at
    # import time. AGENTS.md §6 mandates this isolation.
    from osimflow.apply_params import (  # noqa: PLC0415
        OpenStudioBindingsMissingError,
        UnmappedParameterError,
        apply_parameters,
    )

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
    except OpenStudioBindingsMissingError as exc:
        log.error(
            "OpenStudio Python bindings not installed on this host "
            "(sample_id=%s): %s. Install `openstudio` on the executor "
            "host, or use the test-mode JSON .osm convention.",
            args.sample_id,
            exc,
        )
        print(f"ERROR: {exc}", file=sys.stderr)
        return 3
    except NotImplementedError as exc:
        # Bindings are installed (or this is not an .osm path); the
        # code path is simply not implemented yet. This is a separate
        # failure mode from "bindings missing".
        log.error(
            "apply_params_to_model.py: feature not implemented for "
            "sample_id=%s: %s. This is a known gap; see the issue "
            "tracker.",
            args.sample_id,
            exc,
        )
        print(f"ERROR: {exc}", file=sys.stderr)
        return 5

    log.info(
        "apply_params_to_model.py: wrote per-sample dir for sample_id=%s -> %s",
        args.sample_id,
        args.out,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
