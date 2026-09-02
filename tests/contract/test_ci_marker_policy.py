"""Contract tests pinning the CI pytest marker policy (issue #1468).

pyproject.toml documents the ``chaos`` marker as "deselected by default
in fast-CI", and the dedicated ``chaos`` job in ci.yml is intentionally
NON-gating ("chaos scenarios are probabilistic [...] a flake here should
not block PRs"). Issue #1468 found that the required ``test`` job's
``-m`` filter did NOT deselect chaos — the gate and the documented
policy had drifted. Since #1476 the filter lives in the Makefile
(``PYTEST_CI_FLAGS``), consumed by the CI ``test`` job via
``make test-cov`` (do not re-inline it in ci.yml).

These tests parse the three declaration sites and assert they agree,
so the marker docs and the merge-gate filter cannot drift apart again:

  1. PYTEST_CI_FLAGS deselects nomad_e2e, slow AND chaos -> test_pytest_ci_flags_deselect_gating_markers
  2. pyproject.toml registers the chaos marker           -> test_pyproject_registers_chaos_marker
  3. chaos marker doc and the filter agree bidirectionally -> test_chaos_marker_doc_matches_filter
  4. ci.yml's chaos job selects chaos explicitly          -> test_ci_chaos_job_selects_chaos_explicitly
  5. ci.yml's chaos job ignores the Makefile gate flags   -> test_ci_chaos_job_does_not_consume_pytest_ci_flags

Pure file reads — hermetic and fast.
"""

import re
from pathlib import Path

# Project root is the parent of this package.
REPO_ROOT = Path(__file__).resolve().parents[2]

_MAKEFILE = REPO_ROOT / "Makefile"
_PYPROJECT = REPO_ROOT / "pyproject.toml"
_CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"

_PYTEST_CI_FLAGS_RE = re.compile(r"^PYTEST_CI_FLAGS\s*:=\s*(.+)$", re.MULTILINE)
_MARKER_EXPR_RE = re.compile(r"-m\s+\"([^\"]+)\"")
_CHAOS_MARKER_RE = re.compile(r"^\s*\"(chaos:[^\"]*)\",\s*$", re.MULTILINE)
# A top-level ci.yml job key: exactly two spaces of indent, bare `key:`.
_CI_JOB_KEY_RE = re.compile(r"^  [A-Za-z0-9_-]+:\s*$")


def _pytest_ci_flags() -> str:
    """Return the single PYTEST_CI_FLAGS assignment from the Makefile."""
    makefile = _MAKEFILE.read_text(encoding="utf-8")
    matches = _PYTEST_CI_FLAGS_RE.findall(makefile)
    assert len(matches) == 1, (
        f"Expected exactly one PYTEST_CI_FLAGS assignment in the Makefile, "
        f"found {len(matches)} — the CI gate filter must stay single-sourced "
        f"(issue #1476)."
    )
    return matches[0]


def _deselected_markers() -> set[str]:
    """Return the ``not <marker>`` tokens of PYTEST_CI_FLAGS' ``-m`` filter."""
    match = _MARKER_EXPR_RE.search(_pytest_ci_flags())
    assert match is not None, (
        'PYTEST_CI_FLAGS has no `-m "..."` marker filter — the required '
        "CI `test` job would run every test (nomad_e2e would hang; see "
        "tests/contract/test_developer_practices.py)."
    )
    return {token.strip() for token in match.group(1).split(" and ")}


def _ci_job_block(name: str) -> str:
    """Return the raw ci.yml text of one job, from `  <name>:` to the next job key."""
    lines = _CI_WORKFLOW.read_text(encoding="utf-8").splitlines(keepends=True)
    start = next((i for i, ln in enumerate(lines) if ln.rstrip("\n") == f"  {name}:"), None)
    assert start is not None, f"No `{name}:` job found in {_CI_WORKFLOW}."
    end = next(
        (j for j in range(start + 1, len(lines)) if _CI_JOB_KEY_RE.match(lines[j])),
        len(lines),
    )
    return "".join(lines[start:end])


def test_pytest_ci_flags_deselect_gating_markers() -> None:
    """The merge-gate filter must deselect nomad_e2e, slow AND chaos.

    Chaos tests may use probabilistic fault injection (``--chaos-probability``);
    keeping them out of the required gate is the documented policy (issue
    #1468) — they run in the dedicated non-gating ``chaos`` CI job instead.
    """
    deselected = _deselected_markers()
    for marker in ("not nomad_e2e", "not slow", "not chaos"):
        assert marker in deselected, (
            f"PYTEST_CI_FLAGS' -m filter no longer deselects `{marker}` "
            f"(tokens: {sorted(deselected)}). If this is intentional, update "
            f"the chaos marker docs in pyproject.toml and this test together "
            f"(issue #1468)."
        )


def test_pyproject_registers_chaos_marker() -> None:
    """pyproject.toml must register the chaos marker under [tool.pytest.ini_options].

    ``--strict-markers`` (addopts) turns an unregistered marker into a
    collection error, so the registration itself is load-bearing; this test
    keeps the marker visible to the policy contract above.
    """
    match = _CHAOS_MARKER_RE.search(_PYPROJECT.read_text(encoding="utf-8"))
    assert match is not None, (
        "pyproject.toml no longer registers the `chaos` marker in "
        "[tool.pytest.ini_options].markers — pytest --strict-markers would "
        "fail on every @pytest.mark.chaos test."
    )
    assert "fault-injection" in match.group(1)


def test_chaos_marker_doc_matches_filter() -> None:
    """The chaos marker doc claims deselection; the filter must deliver it.

    This is the bidirectional drift pin from issue #1468: the marker doc
    says "deselected by default in fast-CI", so PYTEST_CI_FLAGS must
    deselect chaos — and if the doc ever stops claiming deselection, this
    test forces the doc and the filter to be reconciled in the same change.
    """
    doc = _CHAOS_MARKER_RE.search(_PYPROJECT.read_text(encoding="utf-8"))
    assert doc is not None, "chaos marker registration missing (see test above)."
    assert "deselected" in doc.group(1), (
        "The pyproject.toml chaos marker doc no longer states that chaos is "
        "deselected by default — either restore the deselection policy or "
        "update PYTEST_CI_FLAGS and this test together (issue #1468)."
    )
    assert "not chaos" in _deselected_markers(), (
        "The chaos marker doc promises deselection in fast-CI, but "
        "PYTEST_CI_FLAGS does not contain `not chaos` — the required merge "
        "gate would run probabilistic chaos tests (issue #1468)."
    )


def test_ci_chaos_job_selects_chaos_explicitly() -> None:
    """The ci.yml chaos job must invoke chaos tests with its own `-m chaos`.

    It must not depend on the merge gate's (deselecting) filter, otherwise
    chaos coverage would silently vanish.
    """
    block = _ci_job_block("chaos")
    assert "-m chaos" in block, (
        "The ci.yml `chaos` job no longer runs `pytest -m chaos` explicitly. "
        "It has its own invocation on purpose (issue #1468): the Makefile's "
        "PYTEST_CI_FLAGS deselects chaos for the required gate."
    )


def test_ci_chaos_job_does_not_consume_pytest_ci_flags() -> None:
    """The chaos job must not consume the Makefile gate targets/flags.

    PYTEST_CI_FLAGS deselects chaos (issue #1468); if the chaos job were
    rewired to `make test` / `make test-cov` / $(PYTEST_CI_FLAGS), it would
    deselect its own tests and chaos coverage would rot silently.
    """
    block = _ci_job_block("chaos")
    for forbidden in ("PYTEST_CI_FLAGS", "PYTEST_COV_FLAGS", "make test-cov", "make test"):
        assert forbidden not in block, (
            f"The ci.yml `chaos` job references `{forbidden}` — it must keep "
            f"its own `pytest -m chaos` invocation because PYTEST_CI_FLAGS "
            f"deselects chaos in the merge gate (issue #1468)."
        )
