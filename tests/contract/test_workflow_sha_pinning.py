"""Contract test: every GitHub Actions `uses:` must be pinned to a full
40-char commit SHA (issue #1448).

Tag-pinned actions (``actions/checkout@v7``) float silently on tag rewrites
— the exact vector behind CVE-2025-30066 (tj-actions/changed-files, March
2025), where version tags were rewritten to dump CI secrets. A hijacked tag
executes before and around in-workflow scanners (gitleaks, pip-audit), so
no in-workflow tool can catch it; SHA pinning closes the vector at the
source.

This test is a pure text parse of ``.github/workflows/*.yml`` — no network,
no subprocesses. It fails on any non-SHA `uses:` so drift cannot land.
Dependabot (``.github/dependabot.yml``, ``github-actions`` ecosystem,
weekly) keeps the pinned SHAs updated with version comments.
"""

import re
from pathlib import Path

import pytest

WORKFLOWS_DIR = Path(__file__).resolve().parents[2] / ".github" / "workflows"

USES_RE = re.compile(r"^\s*(?:-\s+)?uses:\s*(?P<value>\S.*?)\s*$")
SHA_RE = re.compile(r"^(?P<owner>[^@/]+/[^@]+)@(?P<sha>[0-9a-f]{40})(?:\s+#\s*(?P<version>\S+))?$")


def _iter_workflow_files() -> list[Path]:
    files = sorted(WORKFLOWS_DIR.glob("*.yml")) + sorted(WORKFLOWS_DIR.glob("*.yaml"))
    if not files:
        pytest.fail(f"no workflow files found under {WORKFLOWS_DIR}")
    return files


def _iter_uses_lines(path: Path) -> list[tuple[int, str, str]]:
    """Yield (line_number, raw_line, uses_value) for non-comment `uses:` lines."""
    found: list[tuple[int, str, str]] = []
    for lineno, line in enumerate(path.read_text().splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        match = USES_RE.match(line)
        if match:
            found.append((lineno, line, match.group("value")))
    return found


def test_every_workflow_use_is_sha_pinned() -> None:
    violations: list[str] = []
    checked = 0
    for wf in _iter_workflow_files():
        for lineno, _raw, uses_value in _iter_uses_lines(wf):
            checked += 1
            if not SHA_RE.match(uses_value):
                violations.append(
                    f"{wf.relative_to(WORKFLOWS_DIR.parents[2])}:{lineno}: {uses_value}"
                )
    assert not violations, (
        f"{len(violations)} non-SHA-pinned `uses:` reference(s) found. "
        "Pin every action to a full 40-char commit SHA with a version "
        "comment (e.g. `actions/checkout@<40-char-sha> # v7`). "
        "Violations:\n" + "\n".join(violations)
    )
    assert checked > 0, "no `uses:` lines found — test is not exercising anything"


def test_sha_pins_carry_a_version_comment() -> None:
    """SHA pinning without a human-readable version comment makes updates
    opaque; Dependabot and reviewers both rely on the trailing comment."""
    missing: list[str] = []
    for wf in _iter_workflow_files():
        for lineno, _raw, uses_value in _iter_uses_lines(wf):
            match = SHA_RE.match(uses_value)
            if match and not match.group("version"):
                missing.append(f"{wf.relative_to(WORKFLOWS_DIR.parents[2])}:{lineno}: {uses_value}")
    assert not missing, (
        "SHA-pinned `uses:` reference(s) missing a trailing version comment. "
        "Violations:\n" + "\n".join(missing)
    )


def test_dependabot_covers_github_actions_ecosystem() -> None:
    dependabot = WORKFLOWS_DIR.parent / "dependabot.yml"
    assert dependabot.is_file(), ".github/dependabot.yml must exist (issue #1448)"
    import yaml

    config = yaml.safe_load(dependabot.read_text())
    updates = config.get("updates") or []
    ecosystems = [u.get("package-ecosystem") for u in updates]
    assert "github-actions" in ecosystems, (
        "dependabot.yml must include a `github-actions` update stream so "
        "pinned SHAs are kept current (issue #1448)"
    )
    actions_update = next(u for u in updates if u.get("package-ecosystem") == "github-actions")
    interval = (actions_update.get("schedule") or {}).get("interval")
    assert interval == "weekly", f"github-actions update stream must run weekly (got: {interval!r})"
