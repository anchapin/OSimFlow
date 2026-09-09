#!/usr/bin/env python3
"""check_api_doc_coverage.py — verify every path in docs/openapi.json has
prose coverage in docs/api.md.

Issue #1677: docs/openapi.json contains every endpoint the FastAPI app
exposes, but docs/api.md only documented a subset. ``make contract`` /
``tools/check_openapi_sync.py`` only check that the JSON spec matches the
live app — they never verify the prose docs cover every path, so the gap
was invisible to CI. This script closes that gap: it parses the OpenAPI
``paths`` map, walks ``docs/api.md`` for backticked path references, and
exits non-zero (with a clear list of missing paths) when a path has no
prose section or cross-reference.

Path templates (e.g. ``/api/v1/campaigns/{campaign_id}/samples/{sample_id}``)
match anything in the doc containing that template, where each ``{...}``
placeholder accepts any non-whitespace, non-``/`` sequence of characters
(typical of markdown inline code + path arguments like
``/api/v1/campaigns/campaign-aaa/samples/sample_000``).

Run locally:
    python tools/check_api_doc_coverage.py

In CI:
    wired into ``make docs-sync`` (issue #1677 acceptance criterion #3)
    so it runs alongside ``tools/check_docs_sync.py`` and
    ``tools/check_openapi_sync.py`` on every PR.

Exit code 0 on success, 1 if any openapi path is unreferenced in api.md.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
OPENAPI_SPEC = REPO_ROOT / "docs" / "openapi.json"
API_DOC = REPO_ROOT / "docs" / "api.md"


def _path_to_regex(path: str) -> re.Pattern[str]:
    """Build a regex from an OpenAPI path template that matches any prose
    reference to that path (or a concrete instance of it).

    Each ``{placeholder}`` matches one or more non-whitespace,
    non-``/`` characters so that markdown inline code like
    ``/api/v1/campaigns/{campaign_id}/samples/{sample_id}`` is found
    alongside concrete instances such as
    ``/api/v1/campaigns/campaign-aaa/samples/sample_000``. We deliberately
    keep the surrounding ``/`` separators literal so unrelated paths
    (e.g. ``/api/v1/campaigns`` vs ``/api/v1/campaigns/{id}``) don't
    match each other.
    """
    out: list[str] = []
    i = 0
    while i < len(path):
        c = path[i]
        if c == "{":
            j = path.find("}", i)
            if j == -1:
                out.append(re.escape(c))
                i += 1
                continue
            out.append(r"[^/\s)]+")
            i = j + 1
        else:
            out.append(re.escape(c))
            i += 1
    return re.compile("".join(out))


def _collect_openapi_paths(spec_path: Path) -> set[str]:
    """Return every key under ``paths`` in the OpenAPI spec, sorted."""
    if not spec_path.exists():
        print(f"ERROR: {spec_path} not found", file=sys.stderr)
        sys.exit(1)
    try:
        spec = json.loads(spec_path.read_text())
    except json.JSONDecodeError as exc:
        print(f"ERROR: {spec_path} is not valid JSON: {exc}", file=sys.stderr)
        sys.exit(1)
    paths = spec.get("paths", {})
    if not isinstance(paths, dict):
        print(
            f"ERROR: {spec_path} top-level 'paths' is not an object",
            file=sys.stderr,
        )
        sys.exit(1)
    return {p for p in paths if isinstance(p, str)}


def _check_doc_coverage(paths: set[str], doc_path: Path) -> list[str]:
    """Return the sorted list of OpenAPI paths that are NOT referenced
    anywhere in the given markdown doc."""
    if not doc_path.exists():
        print(f"ERROR: {doc_path} not found", file=sys.stderr)
        sys.exit(1)
    text = doc_path.read_text()
    missing: list[str] = []
    for path in paths:
        if _path_to_regex(path).search(text):
            continue
        missing.append(path)
    return sorted(missing)


def main() -> int:
    paths = _collect_openapi_paths(OPENAPI_SPEC)
    missing = _check_doc_coverage(paths, API_DOC)

    if not missing:
        print(f"docs/api.md coverage OK ({len(paths)} paths in docs/openapi.json all referenced)")
        return 0

    print(
        f"docs/api.md coverage FAILED — {len(missing)}/{len(paths)} "
        f"OpenAPI path(s) are not referenced in {API_DOC.relative_to(REPO_ROOT)}:",
        file=sys.stderr,
    )
    for path in missing:
        print(f"  - {path}", file=sys.stderr)
    print(
        "\nFix: add a `### <METHOD> <path>` subsection under the relevant "
        "section of docs/api.md, or add an inline backticked cross-reference "
        "to the path. See issue #1677.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
