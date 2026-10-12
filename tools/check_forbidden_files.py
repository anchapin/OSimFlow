"""Fail when simulation artifacts are tracked outside an allowlist (issue #1894).

Usage: ``check_forbidden_files.py [paths...]``; with no paths, scans
``git ls-files``.
"""

import fnmatch
import subprocess
import sys
from pathlib import PurePosixPath

FORBIDDEN_PATTERNS = (
    "eplusout.*",
    "*.osm",
    "*.osw",
    "*.sqlite",
    "*.job",
    "data_point.zip",
)

ALLOWLIST = frozenset(
    {
        "example_package/model.osm",
        "example_package/workflow.osw",
    }
)


def find_forbidden(paths: list[str]) -> list[str]:
    bad = []
    for raw in paths:
        path = PurePosixPath(raw.replace("\\", "/"))
        if str(path) in ALLOWLIST:
            continue
        if any(fnmatch.fnmatch(path.name, pat) for pat in FORBIDDEN_PATTERNS):
            bad.append(str(path))
    return bad


def main(argv: list[str]) -> int:
    paths = (
        argv
        or subprocess.run(
            ["git", "ls-files"], check=True, capture_output=True, text=True
        ).stdout.splitlines()
    )
    bad = find_forbidden(paths)
    for p in bad:
        print(f"forbidden artifact file: {p}", file=sys.stderr)
    if bad:
        print(
            "Simulation artifacts must not be committed; extend ALLOWLIST in "
            "tools/check_forbidden_files.py only for deliberate fixtures.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
