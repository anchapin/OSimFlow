#!/usr/bin/env python3
"""check_openapi_sync.py — verify docs/openapi.json matches the live app.

Issue #1045 + #1049: PRs that change route signatures, request/response
schemas, or add new routes in ``osimflow/api/**/*.py`` must regenerate
``docs/openapi.json`` in the same PR. This script enforces that invariant
by regenerating the spec from the running FastAPI app and diffing against
the committed ``docs/openapi.json``.

Volatile fields that legitimately drift between regenerations
(``info.x-timestamp``, ``info.version``, ``info.commit-sha``, etc.) are
stripped before comparison so the check focuses on schema content.

Run locally:
    python tools/check_openapi_sync.py

In CI:
    see .github/workflows/agents-contract.yml

Exit code 0 on success, 1 if docs/openapi.json is out of sync.
"""

from __future__ import annotations

import argparse
import difflib
import json
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
COMMITTED_SPEC = REPO_ROOT / "docs" / "openapi.json"
REGEN_SCRIPT = REPO_ROOT / "scripts" / "generate_openapi.py"

# Path fragments that, when present as keys, are expected to differ
# between regenerations and must be stripped before diffing.
VOLATILE_KEY_FRAGMENTS = (
    "timestamp",
    "x-timestamp",
    "x-commit-sha",
    "commit_sha",
    "commit-sha",
    "build_date",
    "build-date",
    "regenerated-at",
    "regenerated_at",
)

# OpenAPI ``info.version`` keys are also volatile — we strip the entire
# ``info.version`` if the caller passes ``--allow-version-drift`` (default).
INFO_VOLATILE_KEYS = frozenset({"version"})


def _strip_volatile(obj: object) -> object:
    """Recursively drop volatile keys from a JSON-like object."""
    if isinstance(obj, dict):
        out: dict[str, object] = {}
        for k, v in obj.items():
            if any(frag in k.lower() for frag in VOLATILE_KEY_FRAGMENTS):
                continue
            out[k] = _strip_volatile(v)
        return out
    if isinstance(obj, list):
        return [_strip_volatile(item) for item in obj]
    return obj


def _coerce_dict(obj: object) -> dict[str, object]:
    """Assert the top-level spec is a dict (JSON object)."""
    if not isinstance(obj, dict):
        raise TypeError(f"Expected dict at top level, got {type(obj).__name__}")
    return obj


def _regenerate(out_path: Path) -> None:
    """Run ``scripts/generate_openapi.py`` to write a fresh spec to *out_path*."""
    subprocess.run(
        [
            sys.executable,
            str(REGEN_SCRIPT.relative_to(REPO_ROOT)),
            "--output",
            str(out_path),
        ],
        cwd=REPO_ROOT,
        check=True,
    )


def _diff_specs(committed: dict[str, object], regenerated: dict[str, object]) -> str:
    """Return a unified-diff string if the specs differ, else empty string."""
    if committed == regenerated:
        return ""
    committed_clean = json.dumps(committed, indent=2, sort_keys=True).splitlines()
    regenerated_clean = json.dumps(regenerated, indent=2, sort_keys=True).splitlines()

    diff = difflib.unified_diff(
        committed_clean,
        regenerated_clean,
        fromfile="docs/openapi.json (committed)",
        tofile="docs/openapi.json (regenerated)",
        lineterm="",
        n=3,
    )
    return "\n".join(diff)


def main() -> int:  # noqa: PLR0911
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Fail on any drift, including volatile fields.",
    )
    parser.add_argument(
        "--summary",
        action="store_true",
        help="Print a summary of additions/deletions instead of a full diff.",
    )
    args = parser.parse_args()

    if not COMMITTED_SPEC.exists():
        print(f"FAIL: {COMMITTED_SPEC} does not exist", file=sys.stderr)
        return 1
    if not REGEN_SCRIPT.exists():
        print(f"FAIL: {REGEN_SCRIPT} does not exist", file=sys.stderr)
        return 1

    committed: dict[str, object] = json.loads(COMMITTED_SPEC.read_text())

    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        _regenerate(tmp_path)
        regenerated: dict[str, object] = json.loads(tmp_path.read_text())
    finally:
        tmp_path.unlink(missing_ok=True)

    if args.strict:
        if committed == regenerated:
            print("docs/openapi.json: in sync (strict)")
            return 0
        print("docs/openapi.json: drift detected (strict mode)", file=sys.stderr)
        print(_diff_specs(committed, regenerated), file=sys.stderr)
        return 1

    # Default mode: strip volatile keys before comparing.
    committed_clean = _coerce_dict(_strip_volatile(committed))
    regenerated_clean = _coerce_dict(_strip_volatile(regenerated))
    diff = _diff_specs(committed_clean, regenerated_clean)
    if not diff:
        print("docs/openapi.json: in sync (volatile fields stripped)")
        return 0

    if args.summary:
        # Count additions vs removals
        adds = sum(
            1 for line in diff.splitlines() if line.startswith("+") and not line.startswith("+++")
        )
        dels = sum(
            1 for line in diff.splitlines() if line.startswith("-") and not line.startswith("---")
        )
        print(
            f"docs/openapi.json: drift detected — {adds} lines added, "
            f"{dels} lines removed (excluding volatile fields)",
            file=sys.stderr,
        )
        return 1

    print(
        "docs/openapi.json: drift detected — regenerate via "
        "`python scripts/generate_openapi.py --output docs/openapi.json` "
        "and commit the result. Diff:",
        file=sys.stderr,
    )
    print(diff, file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
