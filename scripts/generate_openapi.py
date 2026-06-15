#!/usr/bin/env python3
"""Export the OpenAPI specification from the FastAPI app (issue #433).

Generates ``docs/openapi.json`` from the ``osimflow.api.create_app()``
factory so external integrators can auto-generate clients in any language.

Usage::

    python scripts/generate_openapi.py [--output docs/openapi.json]

The script is intentionally side-effect free (does not start a server).
It instantiates the app, reads ``app.openapi()``, and writes the JSON.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# The API module is an optional extra — provide a clear error message.
try:
    from osimflow.api import create_app
except ModuleNotFoundError as exc:
    if "fastapi" in str(exc).lower() or "starlette" in str(exc).lower():
        sys.stderr.write(
            "FastAPI is not installed.  Install the api extra:\n    pip install -e '.[api]'\n"
        )
        raise SystemExit(1) from exc
    raise


def generate_openapi(output: Path = Path("docs/openapi.json")) -> Path:
    """Generate the OpenAPI JSON spec and write it to *output*.

    Returns the path that was written.
    """
    app = create_app()
    spec: dict[str, object] = app.openapi()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(spec, indent=2, sort_keys=False) + "\n")
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        default=Path("docs/openapi.json"),
        help="Output path for the OpenAPI JSON file (default: docs/openapi.json)",
    )
    args = parser.parse_args(argv)
    written = generate_openapi(args.output)
    sys.stderr.write(f"OpenAPI spec written to {written}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
