"""Contract tests pinning AGENTS.md's coverage-gate references to CI (issue #1454).

AGENTS.md is the source of truth the contract checker gates on, but
``tools/check_agents_contract.py`` only verifies substring *mention*, not
*accuracy* — so a stale percentage in §4's CI job list drifts silently
(issue #1454: §4 said ``pytest + 83%`` after the gate had been lowered to
82% by #1417 / commit 7885f4c).

The authoritative gate value lives in the ``Makefile``
(``PYTEST_COV_FLAGS`` / ``--cov-fail-under=N``), which the CI ``test``
job consumes via ``make test-cov`` — single-sourced by issue #1476 so
local targets and the merge gate cannot drift apart. These tests parse
the gate from the Makefile and assert (a) AGENTS.md describes exactly
that number and (b) ci.yml does not re-declare a competing inline gate,
so a future gate change fails here until every mirror is updated.
Pure file reads — hermetic and fast.

Issue #1455 extends the same guard to prose that the substring-only
checker cannot verify: the ``circuit_breaker.py`` §5 entry claimed
``_consecutive_failures`` reset to **1** on ``half_open`` → ``open``,
but #1379 (commit d1056b8) made a failed half-open probe reset the
counter to **0**. The tests below parse the reset value straight from
``osimflow/circuit_breaker.py`` so the doc tracks the code.

  1. AGENTS.md states the CI gate percentage      -> test_agents_md_states_ci_coverage_gate
  2. AGENTS.md carries no stale 83% reference     -> test_agents_md_has_no_stale_83_percent
  3. AGENTS.md states the half_open reset value   -> test_agents_md_states_half_open_reset_value
  4. AGENTS.md carries no stale reset-to-1 claim  -> test_agents_md_has_no_stale_half_open_reset
  5. CLI-flag derivations agree                   -> test_contract_flag_derivations_agree
"""

import importlib.util
import re
from pathlib import Path

import pytest

# Project root is the parent of this package.
REPO_ROOT = Path(__file__).resolve().parents[2]

_CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
_MAKEFILE = REPO_ROOT / "Makefile"
_AGENTS_MD = REPO_ROOT / "AGENTS.md"
_CIRCUIT_BREAKER = REPO_ROOT / "osimflow" / "circuit_breaker.py"

_COV_FAIL_UNDER_RE = re.compile(r"--cov-fail-under=(\d+)")
_HALF_OPEN_RESET_RE = re.compile(r"was_half_open[^0-9]*_consecutive_failures\s*=\s*(\d+)")


def _load_agents_contract_module() -> object:
    """Import tools/check_agents_contract.py (tools/ is not a package)."""
    spec = importlib.util.spec_from_file_location(
        "check_agents_contract_under_test",
        REPO_ROOT / "tools" / "check_agents_contract.py",
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_contract_flag_derivations_agree() -> None:
    """Issue #1575: the contract checker derives the CLI-flag list twice —
    by building the real parser (which calls the per-executor
    ``add_arguments`` hooks) and, as a fallback for bare-python
    environments, by textually scanning ``add_argument("--…')`` literals
    in ``__main__.py`` + ``executor_configs/*.py``. The two derivations
    must produce the same set, otherwise CI (fallback) and pre-commit
    (introspection) enforce different contracts.
    """
    mod = _load_agents_contract_module()
    parsed = mod._cli_flags_from_parser()
    assert parsed is not None, (
        "osimflow must be importable in the test venv for the parser-walk "
        "derivation; if this fails, the textual fallback and this test both "
        "need revisiting"
    )
    textual = mod._cli_flags_from_sources()
    assert parsed == textual, (
        f"parser-walk flags missing from textual scan: {sorted(parsed - textual)}; "
        f"textual-scan flags missing from parser walk: {sorted(textual - parsed)}"
    )


def _parse_gate_pct() -> int:
    """Extract the coverage gate percentage from the Makefile.

    Returns the single distinct ``--cov-fail-under`` value declared in
    the ``Makefile`` (``PYTEST_COV_FLAGS`` — the single source of truth
    since issue #1476; the CI ``test`` job runs ``make test-cov``).
    Fails loudly if the directive moves or becomes ambiguous — a silent
    fallback would defeat the drift guard.
    """
    matches = _COV_FAIL_UNDER_RE.findall(_MAKEFILE.read_text(encoding="utf-8"))
    assert matches, (
        "No --cov-fail-under directive found in the Makefile. "
        "The coverage gate moved; update this test to parse the new location."
    )
    values = {int(v) for v in matches}
    assert len(values) == 1, (
        f"Ambiguous coverage gate in the Makefile: {sorted(values)}. "
        "The Makefile must declare exactly one --cov-fail-under value."
    )
    return values.pop()


def test_agents_md_states_ci_coverage_gate() -> None:
    """AGENTS.md must mention exactly the gate percentage CI enforces.

    A future bump of ``--cov-fail-under`` in the Makefile fails this test
    until AGENTS.md is updated — the drift #1454 fixed cannot reintroduce.
    """
    gate = _parse_gate_pct()
    assert f"{gate}%" in _AGENTS_MD.read_text(encoding="utf-8"), (
        f"AGENTS.md does not mention the CI coverage gate ({gate}%). "
        "Update AGENTS.md §4 (and any other gate references) to match "
        "the Makefile PYTEST_COV_FLAGS."
    )
    ci_workflow = _CI_WORKFLOW.read_text(encoding="utf-8")
    assert "make test-cov" in ci_workflow, (
        ".github/workflows/ci.yml no longer runs `make test-cov`. "
        "The CI pytest invocation must stay single-sourced in the "
        "Makefile (issue #1476) — re-inline flags only by also updating "
        "this test."
    )
    inline_gates = _COV_FAIL_UNDER_RE.findall(ci_workflow)
    assert not inline_gates, (
        f".github/workflows/ci.yml declares an inline --cov-fail-under "
        f"({sorted(inline_gates)}) instead of consuming the Makefile's "
        "PYTEST_COV_FLAGS — that is the dual-source drift issue #1476 "
        "closed. Move the gate back to the Makefile."
    )


def test_agents_md_has_no_stale_83_percent() -> None:
    """The literal stale reference from issue #1454 must stay gone.

    Skips the negative assertion if the gate ever legitimately becomes 83
    (then ``83%`` in AGENTS.md would be correct, and the positive test
    above is the binding check).
    """
    gate = _parse_gate_pct()
    if gate == 83:
        pytest.skip("gate is legitimately 83 now; the positive-gate test covers it")
    assert "83%" not in _AGENTS_MD.read_text(encoding="utf-8"), (
        "AGENTS.md contains a stale '83%' coverage-gate reference "
        f"(issue #1454 regression). The CI gate is {gate}% — see "
        "the Makefile PYTEST_COV_FLAGS."
    )


def _parse_half_open_reset_value() -> int:
    """Extract the half_open reset value from osimflow/circuit_breaker.py.

    Returns the single distinct value ``_consecutive_failures`` is assigned
    inside the failed half-open-probe branch (``if was_half_open:``).
    Fails loudly if the assignment moves or becomes ambiguous — a silent
    fallback would defeat the drift guard.
    """
    matches = _HALF_OPEN_RESET_RE.findall(_CIRCUIT_BREAKER.read_text(encoding="utf-8"))
    assert matches, (
        "No was_half_open -> _consecutive_failures assignment found in "
        "osimflow/circuit_breaker.py. The reset moved; update this test "
        "to parse the new location."
    )
    values = {int(v) for v in matches}
    assert len(values) == 1, (
        f"Ambiguous half_open reset in osimflow/circuit_breaker.py: "
        f"{sorted(values)}. The code must assign exactly one reset value."
    )
    return values.pop()


def test_agents_md_states_half_open_reset_value() -> None:
    """AGENTS.md must describe exactly the half_open reset value the code uses.

    A future change of the ``was_half_open`` reset value in
    ``osimflow/circuit_breaker.py`` fails this test until AGENTS.md is
    updated — the inversion #1455 fixed cannot reintroduce.
    """
    reset = _parse_half_open_reset_value()
    text = _AGENTS_MD.read_text(encoding="utf-8")
    assert f"reset to {reset} on a failed ``half_open`` → ``open`` transition" in text, (
        f"AGENTS.md does not describe the circuit-breaker half_open reset "
        f"value ({reset}). Update the §5 circuit_breaker.py entry to match "
        "osimflow/circuit_breaker.py (issue #1379 behavior)."
    )


def test_agents_md_has_no_stale_half_open_reset() -> None:
    """The literal stale claim from issue #1455 must stay gone.

    Skips the negative assertion if the reset value ever legitimately
    becomes 1 (then ``reset to 1 on`` in AGENTS.md would be correct, and
    the positive test above is the binding check).
    """
    reset = _parse_half_open_reset_value()
    if reset == 1:
        pytest.skip("reset value is legitimately 1 now; the positive-reset test covers it")
    assert "reset to 1 on ``half_open``" not in _AGENTS_MD.read_text(encoding="utf-8"), (
        "AGENTS.md contains a stale 'reset to 1 on half_open → open' claim "
        "(issue #1455 regression). The code resets _consecutive_failures to "
        f"{reset} on a failed half-open probe — see osimflow/circuit_breaker.py."
    )
