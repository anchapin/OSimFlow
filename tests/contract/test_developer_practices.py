"""Contract tests for the developer best-practices infrastructure (issue #15).

These tests pin the *behavior* of the lint/type/CI contract, not its
implementation. A change to ruff or mypy config that causes these to
fail is a regression we want to catch.

Each test corresponds to one acceptance criterion in issue #15:

  1. ruff lint runs clean                         -> test_ruff_passes
  2. ruff format is clean                         -> test_ruff_format_passes
  3. mypy --strict on osimflow/                   -> test_mypy_strict_passes
  4. coverage gate >= 85%                         -> test_coverage_gate
  5. AGENTS.md / code contract                    -> test_agents_md_contract
  6. pre-commit config validates                  -> test_precommit_config_valid
  7. CI workflow YAMLs parse                      -> test_workflows_yaml_valid
  8. docs/ cross-references resolve               -> test_docs_sync
"""

import subprocess
import sys
from pathlib import Path

import pytest

# Project root is the parent of this package.
REPO_ROOT = Path(__file__).resolve().parents[2]


def _run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
    """Run a subprocess and return CompletedProcess. We do not assert here;
    each test decides what a non-zero exit means."""
    return subprocess.run(
        cmd,
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
        **kwargs,
    )


@pytest.fixture(scope="module")
def ruff_result() -> subprocess.CompletedProcess[str]:
    return _run([sys.executable, "-m", "ruff", "check", "."])


@pytest.fixture(scope="module")
def ruff_format_result() -> subprocess.CompletedProcess[str]:
    return _run([sys.executable, "-m", "ruff", "format", "--check", "."])


@pytest.fixture(scope="module")
def mypy_result() -> subprocess.CompletedProcess[str]:
    return _run([sys.executable, "-m", "mypy", "osimflow"])


@pytest.fixture(scope="module")
def pytest_cov_result() -> subprocess.CompletedProcess[str]:
    # Recursion guard: this test runs inside a pytest process, so the
    # inner pytest must NOT re-collect this directory. Restrict to the
    # integration and unit suites that exercise the osimflow/ package surface.
    return _run(
        [
            sys.executable,
            "-m",
            "coverage",
            "run",
            "--source=osimflow",
            "--branch",
            "-m",
            "pytest",
            "-q",
            "--no-cov",
            "tests/integration",
            "tests/unit",
        ]
    )


def test_ruff_passes(ruff_result: subprocess.CompletedProcess[str]) -> None:
    """ruff check must exit 0 on the whole repo."""
    assert ruff_result.returncode == 0, (
        f"ruff check failed:\nstdout:\n{ruff_result.stdout}\nstderr:\n{ruff_result.stderr}"
    )


def test_ruff_format_passes(ruff_format_result: subprocess.CompletedProcess[str]) -> None:
    """ruff format --check must exit 0 on the whole repo."""
    assert ruff_format_result.returncode == 0, (
        f"ruff format --check failed:\nstdout:\n{ruff_format_result.stdout}\n"
        f"stderr:\n{ruff_format_result.stderr}"
    )


def test_mypy_strict_passes(mypy_result: subprocess.CompletedProcess[str]) -> None:
    """mypy --strict (configured in pyproject.toml) must exit 0 on osimflow/."""
    assert mypy_result.returncode == 0, (
        f"mypy failed:\nstdout:\n{mypy_result.stdout}\nstderr:\n{mypy_result.stderr}"
    )


def test_coverage_gate(pytest_cov_result: subprocess.CompletedProcess[str]) -> None:
    """The 85% line-coverage gate on the osimflow/ package must pass.

    Runs `coverage run -m pytest` for the test suites, then a separate
    `coverage report --fail-under=85` subprocess that returns the actual
    gate signal (the in-process pytest return code is the test suite's,
    not the coverage gate's).
    """
    assert pytest_cov_result.returncode == 0, (
        f"pytest (under coverage) failed:\nstdout:\n{pytest_cov_result.stdout}\n"
        f"stderr:\n{pytest_cov_result.stderr}"
    )
    report = _run(
        [
            sys.executable,
            "-m",
            "coverage",
            "report",
            "--fail-under=85",
        ]
    )
    assert report.returncode == 0, (
        f"coverage --fail-under=85 failed:\nstdout:\n{report.stdout}\nstderr:\n{report.stderr}"
    )


def test_agents_md_contract() -> None:
    """tools/check_agents_contract.py must exit 0.

    It pins: every public symbol in osimflow/__init__.py, every bin/*.py
    script, every osimflow/executors/*.py file, every campaign step name,
    and every CLI flag is mentioned in AGENTS.md. PRs that break this
    contract are blocked by CI.
    """
    res = _run([sys.executable, "tools/check_agents_contract.py"])
    assert res.returncode == 0, (
        f"AGENTS.md contract check failed:\nstdout:\n{res.stdout}\nstderr:\n{res.stderr}"
    )


def test_precommit_config_valid() -> None:
    """pre-commit validate-config must exit 0 on .pre-commit-config.yaml."""
    cfg = REPO_ROOT / ".pre-commit-config.yaml"
    if not cfg.exists():
        pytest.fail(f"{cfg} does not exist yet — see issue #15")
    res = _run(
        [sys.executable, "-m", "pre_commit", "validate-config", str(cfg)],
    )
    assert res.returncode == 0, (
        f"pre-commit validate-config failed:\nstdout:\n{res.stdout}\nstderr:\n{res.stderr}"
    )


def test_workflows_yaml_valid() -> None:
    """Every .github/workflows/*.yml must be parseable YAML and contain a `jobs:` block."""
    import yaml  # PyYAML is a hard dep

    wf_dir = REPO_ROOT / ".github" / "workflows"
    assert wf_dir.is_dir(), f"{wf_dir} missing"
    errors: list[str] = []
    for path in sorted(wf_dir.glob("*.yml")):
        try:
            data = yaml.safe_load(path.read_text())
        except yaml.YAMLError as e:
            errors.append(f"{path.name}: YAML parse error: {e}")
            continue
        if not isinstance(data, dict) or "jobs" not in data:
            errors.append(f"{path.name}: missing top-level 'jobs:' key")
            continue
        if not data["jobs"]:
            errors.append(f"{path.name}: 'jobs:' is empty")
    assert not errors, "workflow validation errors:\n  " + "\n  ".join(errors)


def test_docs_sync() -> None:
    """tools/check_docs_sync.py must exit 0.

    The check walks docs/**/*.md, extracts path-like references (in
    backticks) and `bin/*.py` script names, and asserts each one exists
    in the working tree. `<!-- docs-skip -->` opts a file out.
    """
    res = _run([sys.executable, "tools/check_docs_sync.py"])
    assert res.returncode == 0, (
        f"docs sync check failed:\nstdout:\n{res.stdout}\nstderr:\n{res.stderr}"
    )


def test_agents_md_section_5_mentions_ci_workflow() -> None:
    """Issue #8 acceptance criterion: AGENTS.md §5 (Testing) must mention
    the CI workflow file so contributors know where the green/red signal
    for `pytest` comes from.

    We extract §5 by splitting on `## N. ` headings and assert the
    section body contains a backticked `.github/workflows/` path. The
    exact file referenced (`ci.yml` is the canonical one) is checked
    separately so the test fails with a precise diagnostic.
    """
    import re

    agents_md = (REPO_ROOT / "AGENTS.md").read_text()
    # Split on `## ` headings and grab the slice whose header is "5. Testing".
    sections = re.split(r"^## ", agents_md, flags=re.MULTILINE)
    section_5: str | None = None
    for chunk in sections:
        if chunk.startswith("5. Testing"):
            section_5 = chunk
            break
    assert section_5 is not None, "AGENTS.md is missing the `## 5. Testing` section"

    # Acceptance criterion: the section references a `.github/workflows/`
    # path. Pin to `ci.yml` (the canonical pytest+lint+typecheck job) so a
    # contributor gets a precise failure if they reference a non-existent
    # workflow file.
    assert ".github/workflows/" in section_5, (
        "AGENTS.md §5 (Testing) must mention a `.github/workflows/` path "
        "so contributors can find the CI workflow (issue #8 acceptance "
        "criterion)."
    )
    ci_path = REPO_ROOT / ".github" / "workflows" / "ci.yml"
    assert ci_path.is_file(), f"{ci_path} referenced from AGENTS.md §5 does not exist on disk"


# ---------------------------------------------------------------------------
# Issue #29: chore(docs) — revise AI agent instructions
#
# The "Backend Specialist" generic role prompt and this project's
# AGENTS.md have several conflicts and ambiguities (see issue #29).
# The in-scope fix for the OSimFlow repo is to (a) state a precedence
# rule between the role prompt and AGENTS.md, (b) document the actual
# project architecture (Orchestrator → Executor → Work function), (c)
# explicitly note there is no authentication layer, and (d) add a
# tool-selection decision tree to §9. Each contract test below pins one
# acceptance criterion.
# ---------------------------------------------------------------------------


def _section_with_heading_containing(
    text: str,
    needle: str,
    heading_prefix: str = "##",
) -> str | None:
    """Return the body of the first section whose heading line starts
    with ``heading_prefix`` (default ``##``) and contains ``needle``
    (case-insensitive). Works for both top-level (``##``) and
    sub-section (``###``) headings by passing the appropriate prefix.

    The section body is the heading line and everything below it up
    to (but not including) the next heading of the same or higher
    level.
    """
    import re

    lines = text.splitlines()
    escaped_prefix = re.escape(heading_prefix)
    headings = [
        (i, line) for i, line in enumerate(lines) if re.match(rf"^{escaped_prefix}\s", line)
    ]
    for idx, (i, line) in enumerate(headings):
        if needle.lower() in line.lower():
            end = headings[idx + 1][0] if idx + 1 < len(headings) else len(lines)
            return "\n".join(lines[i:end])
    return None


def test_agents_md_has_precedence_rule_section() -> None:
    """Issue #29 acceptance criterion: AGENTS.md must contain a section
    that states the precedence rule between the generic role prompt and
    this project's AGENTS.md (which side wins for project-scoped vs.
    cross-project decisions).

    The rule is a small section near the top, titled e.g. "Precedence
    and project-type boundaries" or similar. We accept any section whose
    first-line heading contains "precedence" or "boundaries" (or
    "boundary"). The body must explicitly contrast the project
    AGENTS.md with the generic role prompt AND use language that
    signals which side overrides the other ("wins", "overrides", or
    "takes precedence").
    """
    agents_md = (REPO_ROOT / "AGENTS.md").read_text()
    section = _section_with_heading_containing(agents_md, "precedence", heading_prefix="##")
    if section is None:
        section = _section_with_heading_containing(agents_md, "boundary", heading_prefix="##")
    assert section is not None, (
        "AGENTS.md is missing a 'Precedence and project-type boundaries' "
        "(or similar) section (issue #29 acceptance criterion). Add a "
        "section near the top of AGENTS.md that documents which of the "
        "role prompt vs. AGENTS.md wins for project-scoped decisions."
    )
    body = section.lower()
    # Both the project doc and the role prompt must be named in the
    # section so the contrast is explicit, not just implied.
    assert "agents.md" in body, (
        "The precedence section must mention 'AGENTS.md' (this project "
        "doc) so the rule is unambiguous (issue #29)."
    )
    assert "role" in body and "prompt" in body, (
        "The precedence section must mention the generic role prompt "
        "so the rule is unambiguous (issue #29)."
    )
    # The section must use actionable language that names which side
    # wins, not just descriptive prose.
    assert any(w in body for w in ("wins", "override", "overrides", "takes precedence")), (
        "The precedence section must state which side wins, e.g. "
        "'AGENTS.md wins for project-scoped decisions' (issue #29)."
    )


def test_agents_md_documents_orchestrator_executor_work_pattern() -> None:
    """Issue #29 acceptance criterion: AGENTS.md must document the
    project's actual layered architecture (Orchestrator → Executor →
    Work function) — not the generic web-service Router → Service →
    Repository → Models pattern that a generic role prompt might
    prescribe.

    The current prose mentions "orchestrator", "executor", and
    "work functions" individually, but the layered pattern is never
    stated as a single architecture statement. This test requires the
    arrowed form ("Orchestrator → Executor → Work function" or
    "Orchestrator -> Executor -> Work function") so the pattern is
    discoverable at a glance.
    """
    import re

    agents_md = (REPO_ROOT / "AGENTS.md").read_text()
    arrow_pattern = re.compile(
        r"orchestrator\s*(?:→|->|&#8594;)\s*executor"
        r"\s*(?:→|->|&#8594;)\s*work(?:\s+function)?",
        re.IGNORECASE,
    )
    assert arrow_pattern.search(agents_md), (
        "AGENTS.md must state the project's actual layered architecture "
        "as 'Orchestrator → Executor → Work function' (or '->' / '&#8594;' "
        "form) so agents don't apply a generic web-service pattern to "
        "this CLI/library project (issue #29 acceptance criterion)."
    )


def test_agents_md_states_no_authentication_layer() -> None:
    """Issue #29 acceptance criterion: AGENTS.md must explicitly state
    that OSimFlow has no authentication layer, so agents don't waste
    cycles searching for JWT / bcrypt / user-model code that the
    project does not have.

    The 'Backend Specialist' role prompt's auth rule is not applicable
    to this project. The fix is to make the project's stance explicit
    in AGENTS.md so a future agent reads "no auth" before applying the
    role prompt's "JWT + bcrypt" rule.
    """
    agents_md = (REPO_ROOT / "AGENTS.md").read_text()
    lower = agents_md.lower()
    # We accept any wording that conveys the project's "no auth"
    # stance. Typical phrasings: "no auth", "no authentication",
    # "no user accounts", "no user model", "no login", "no jwt".
    no_auth_phrases = (
        "no auth",
        "no authentication",
        "no user account",
        "no user model",
        "no login",
        "no jwt",
        "no password",
    )
    matches = [p for p in no_auth_phrases if p in lower]
    assert matches, (
        "AGENTS.md must explicitly state that OSimFlow has no "
        "authentication layer, so agents don't search for JWT/bcrypt/"
        "user-model code that does not exist (issue #29 acceptance "
        "criterion). Add a sentence like: 'OSimFlow has no "
        "authentication layer — there are no user accounts, no "
        "passwords, and no JWT/bcrypt code.'"
    )


def test_agents_md_section_9_has_tool_selection_tree() -> None:
    """Issue #29 (optional) acceptance criterion: §9 (Task routing hints
    for AI agents) must include a tool-selection decision tree so
    agents know which tool family to reach for when the same task can
    be done several ways (Read vs. ctx_execute_file vs.
    codebase-memory-mcp_search_graph, etc.).

    The decision tree should map common tasks to specific tools. We
    accept any subset of the canonical anchors used by opencode /
    context-mode / codebase-memory-mcp:

      Read, ctx_execute_file, search_graph, Grep, Bash, ctx_execute,
      ctx_fetch_and_index

    Requiring >= 4 of those anchors keeps the test tolerant of
    minor wording changes while still ensuring the decision tree is
    substantive.
    """
    import re

    agents_md = (REPO_ROOT / "AGENTS.md").read_text()
    sections = re.split(r"^## ", agents_md, flags=re.MULTILINE)
    section_9: str | None = None
    for chunk in sections:
        if chunk.startswith("9. Task routing hints"):
            section_9 = chunk
            break
    assert section_9 is not None, (
        "AGENTS.md is missing the `## 9. Task routing hints for AI agents` section."
    )
    # Pin a "Tool selection" or "Decision tree" sub-heading inside §9
    # so the decision tree is a discoverable, separate block — not just
    # inline prose.
    sub_section = _section_with_heading_containing(
        section_9, "tool selection", heading_prefix="###"
    )
    if sub_section is None:
        sub_section = _section_with_heading_containing(
            section_9, "decision tree", heading_prefix="###"
        )
    assert sub_section is not None, (
        "AGENTS.md §9 must include a 'Tool selection' (or 'Decision tree') "
        "sub-section so the tool-picker is discoverable, not buried in "
        "prose (issue #29)."
    )
    canonical_anchors = (
        "Read",
        "ctx_execute_file",
        "search_graph",
        "Grep",
        "Bash",
        "ctx_execute",
        "ctx_fetch_and_index",
    )
    matches = [a for a in canonical_anchors if a in sub_section]
    assert len(matches) >= 4, (
        f"AGENTS.md §9 'Tool selection' sub-section must list at least 4 "
        f"of the canonical tool anchors {canonical_anchors} (issue #29). "
        f"Only found: {matches}."
    )
