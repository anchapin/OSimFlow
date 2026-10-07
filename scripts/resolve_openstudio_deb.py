"""Pick the Ubuntu ``.deb`` asset URL from a GitHub release JSON (issue #1813).

Reads the NREL/OpenStudio release API payload on stdin and prints the
preferred asset URL (Ubuntu 22.04, then 20.04, then any Ubuntu ``.deb``).
Lives in a real file (not inline in the workflow YAML) so it is unit-tested.
"""

import json
import logging
import sys
from collections.abc import Mapping
from typing import Any

log = logging.getLogger("resolve_openstudio_deb")

PREFERRED_UBUNTU = ("22.04", "20.04")


def select_deb_url(release: Mapping[str, Any]) -> str:
    """Return the preferred Ubuntu ``.deb`` URL, or raise ``LookupError``."""
    urls = [a["browser_download_url"] for a in release.get("assets", [])]
    debs = [u for u in urls if "Ubuntu" in u and u.endswith(".deb")]
    if not debs:
        raise LookupError("no Ubuntu .deb asset found for this release")
    for pref in PREFERRED_UBUNTU:
        for url in debs:
            if pref in url:
                return url
    return debs[0]


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        print(select_deb_url(json.load(sys.stdin)))
    except (LookupError, ValueError) as exc:
        log.error("%s", exc, exc_info=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
