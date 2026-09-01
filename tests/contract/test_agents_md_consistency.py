"""Contract tests pinning AGENTS.md's coverage-gate references to CI (issue #1454).

AGENTS.md is the source of truth the contract checker gates on, but
``tools/check_agents_contract.py`` only verifies substring *mention*, not
*accuracy* — so a stale percentage in §4's CI job list drifts silently
(issue #1454: §4 said ``pytest + 83%`` after the gate had been lowered to
82% by #1417 / commit 7885f4c).

The authoritative gate value lives in ``.github/workflows/ci.yml``
(``--cov-fail-under=N``, mirrored by the ``make test-cov`` target in the
``Makefile``). These tests parse it from CI and assert AGENTS.md describes
exactly that number, so a future gate change fails here until AGENTS.md is
updated. Pure file reads — hermetic and fast.

  1. AGENTS.md states the CI gate percentage      -> test_agents_md_states_ci_coverage_gate
  2. AGENTS.md carries no stale 83% reference     -> test_agents_md_has_no_stale_83_percent
"""

import re
from pathlib import Path

import pytest

# Project root is the parent of this package.
REPO_ROOT = Path(__file__).resolve().parents[2]

_CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
_MAKEFILE = REPO_ROOT / "Makefile"
_AGENTS_MD = REPO_ROOT / "AGENTS.md"

_COV_FAIL_UNDER_RE = re.compile(r"--cov-fail-under=(\d+)")


def _parse_ci_gate_pct() -> int:
    """Extract the coverage gate percentage from the CI workflow.

    Returns the single distinct ``--cov-fail-under`` value declared in
    ``.github/workflows/ci.yml``. Fails loudly if the directive moves or
    becomes ambiguous — a silent fallback would defeat the drift guard.
    """
    matches = _COV_FAIL_UNDER_RE.findall(_CI_WORKFLOW.read_text(encoding="utf-8"))
    assert matches, (
        "No --cov-fail-under directive found in .github/workflows/ci.yml. "
        "The coverage gate moved; update this test to parse the new location."
    )
    values = {int(v) for v in matches}
    assert len(values) == 1, (
        f"Ambiguous coverage gate in .github/workflows/ci.yml: {sorted(values)}. "
        "CI must declare exactly one --cov-fail-under value."
    )
    return values.pop()


def test_agents_md_states_ci_coverage_gate() -> None:
    """AGENTS.md must mention exactly the gate percentage CI enforces.

    A future bump of ``--cov-fail-under`` in ci.yml fails this test until
    AGENTS.md is updated — the drift #1454 fixed cannot reintroduce.
    """
    gate = _parse_ci_gate_pct()
    assert f"{gate}%" in _AGENTS_MD.read_text(encoding="utf-8"), (
        f"AGENTS.md does not mention the CI coverage gate ({gate}%). "
        "Update AGENTS.md §4 (and any other gate references) to match "
        ".github/workflows/ci.yml."
    )
    makefile_gate = _COV_FAIL_UNDER_RE.findall(_MAKEFILE.read_text(encoding="utf-8"))
    assert {int(v) for v in makefile_gate} == {gate}, (
        f"Makefile test-cov gate {sorted(makefile_gate)} disagrees with ci.yml gate {gate}."
    )


def test_agents_md_has_no_stale_83_percent() -> None:
    """The literal stale reference from issue #1454 must stay gone.

    Skips the negative assertion if the gate ever legitimately becomes 83
    (then ``83%`` in AGENTS.md would be correct, and the positive test
    above is the binding check).
    """
    gate = _parse_ci_gate_pct()
    if gate == 83:
        pytest.skip("gate is legitimately 83 now; the positive-gate test covers it")
    assert "83%" not in _AGENTS_MD.read_text(encoding="utf-8"), (
        "AGENTS.md contains a stale '83%' coverage-gate reference "
        f"(issue #1454 regression). The CI gate is {gate}% — see "
        ".github/workflows/ci.yml."
    )
