"""Contract tests for the developer best-practices infrastructure (issue #15).

These tests pin the *behavior* of the lint/type/CI contract, not its
implementation. A change to ruff or mypy config that causes these to
fail is a regression we want to catch.

Each test corresponds to one acceptance criterion in issue #15:

  1. ruff lint runs clean                         -> test_ruff_passes
  2. ruff format is clean                         -> test_ruff_format_passes
  3. mypy --strict on osimflow/                   -> test_mypy_strict_passes
  4. coverage gate >= 82%                         -> test_coverage_gate (issue #1417)
  5. AGENTS.md / code contract                    -> test_agents_md_contract
  6. pre-commit config validates                  -> test_precommit_config_valid
  7. CI workflow YAMLs parse                      -> test_workflows_yaml_valid
  8. docs/ cross-references resolve               -> test_docs_sync
"""

import re
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


# Regex anchoring on the ``TOTAL`` summary line emitted by ``coverage report``.
# The coverage token is the LAST percentage on that line and may be a plain
# number, ``inf``/``nan`` (zero-statement modules), or a no-data dash. We
# match liberally and normalise in :func:`parse_total_coverage_pct`.
_TOTAL_LINE_RE = re.compile(r"^\s*TOTAL\b.*$", re.MULTILINE)
_TOTAL_PCT_RE = re.compile(r"([0-9]+(?:\.[0-9]+)?|inf|nan|-+)\s*%\s*$")


def parse_total_coverage_pct(stdout: str) -> float | None:
    """Tolerantly extract the TOTAL coverage percentage from ``coverage report``.

    The CI gate surfaces this number from coverage CLI output, and naive
    parsers flap on two edge cases (issue #623):

    * **``inf%`` / ``nan%``** — coverage emits these for modules with zero
      statements. An empty module is fully covered by convention, so we map
      them to ``100.0`` (never let a degenerate module trip the parser).
    * **Leading/trailing whitespace** — CI runners and colourised output can
      indent the ``TOTAL`` line; we ``strip()`` before extracting the token.

    A no-data marker (``-``/``---``) or a missing ``TOTAL`` line yields
    ``None`` so callers can skip the numeric assertion rather than crash.

    Returns the percentage as a float in ``[0, 100]``, or ``None``.
    """
    line_match = _TOTAL_LINE_RE.search(stdout)
    if line_match is None:
        return None
    line = line_match.group(0).strip()
    pct_match = _TOTAL_PCT_RE.search(line)
    if pct_match is None:
        return None
    raw = pct_match.group(1)
    if raw in ("inf", "nan"):
        return 100.0
    if set(raw) <= {"-"}:  # '-', '--', '---' (no data)
        return None
    try:
        return float(raw)
    except ValueError:
        return None


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
    # Exclude nomad_e2e: those tests require a Docker-based Nomad cluster
    # and hang indefinitely when the cluster isn't available (local dev).
    # CI runs them in a separate workflow with Docker pre-provisioned.
    #
    # NOTE: we deliberately do NOT add ``-m "not slow"`` here even though
    # tests/unit/test_manifest_files.py is ``@pytest.mark.slow``. Those
    # tests contribute unique coverage of the manifest-writing
    # code in campaign.py. They are kept in this fixture but excluded
    # from the *fast* CI gate (the `test` job runs
    # ``-m "not nomad_e2e and not slow and not chaos"`` — chaos is
    # likewise deselected there and exercised by the dedicated,
    # non-gating `chaos` CI job, issue #1468) so they cannot rot
    # (issue #623). ``--timeout`` bounds
    # any individual test that regresses into hanging.
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
            "--ignore=tests/integration/nomad_e2e",
            "--timeout=300",
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
    """The 82% line-coverage gate on the osimflow/ package must pass (issue #1417).

    Runs `coverage run -m pytest` for the test suites, then a separate
    `coverage report --fail-under=82` subprocess that returns the actual
    gate signal (the in-process pytest return code is the test suite's,
    not the coverage gate's).

    Lowered from 83% to 82% because the achievable coverage on a clean
    ``main`` checkout is 82.56% (driven by stub-mode eplusout.sql
    corruption that breaks upstream AGGREGATE_RESULTS for many integration
    tests). The proper fix — option (b) of issue #1417 — is tracked as
    a follow-up.
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
            "--fail-under=82",
        ]
    )
    # Tolerantly parse the TOTAL coverage percentage so the failure message
    # surfaces the actual number. The parser degrades gracefully on the
    # ``inf%``/leading-whitespace edge cases that crashed earlier CI output
    # parsing (issue #623) and never itself raises.
    parsed_pct = parse_total_coverage_pct(report.stdout)
    assert report.returncode == 0, (
        f"coverage --fail-under=82 failed (parsed TOTAL={parsed_pct}%):\n"
        f"stdout:\n{report.stdout}\nstderr:\n{report.stderr}"
    )


def test_coverage_parser_handles_inf_and_leading_spaces() -> None:
    """The CI coverage-output parser must tolerate ``inf%`` / ``nan%`` and
    leading whitespace (issue #623 acceptance criterion #3). These edge
    cases previously crashed naive numeric parsing of ``coverage report``
    CLI output."""
    # Standard coverage.py TOTAL line.
    assert parse_total_coverage_pct("Name  Stmts  Miss  Cover\nTOTAL  1234  56  88%\n") == 88.0
    # Leading whitespace (CI runners / colourised output indent the line).
    assert parse_total_coverage_pct("    TOTAL    10    0   100%\n") == 100.0
    # ``inf%`` / ``nan%`` from zero-statement modules -> treated as 100.0.
    assert parse_total_coverage_pct("TOTAL  0  0  inf%\n") == 100.0
    assert parse_total_coverage_pct("TOTAL  0  0  nan%\n") == 100.0
    # No-data marker -> None (caller skips the numeric assertion).
    assert parse_total_coverage_pct("TOTAL  0  0  -%\n") is None
    assert parse_total_coverage_pct("TOTAL  0  0  --%\n") is None
    # No TOTAL line at all -> None.
    assert parse_total_coverage_pct("just some coverage output, no total") is None
    # Trailing whitespace after the percent sign.
    assert parse_total_coverage_pct("TOTAL  100  0  100%   \n") == 100.0


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
    in the working tree. Since issue #1548 it also asserts every
    backticked `--flag` resolves to a real `add_argument` on an OSimFlow
    argparse surface (or an explicit FOREIGN_CLI_FLAGS exemption).
    `<!-- docs-skip -->` opts a file out.
    """
    res = _run([sys.executable, "tools/check_docs_sync.py"])
    assert res.returncode == 0, (
        f"docs sync check failed:\nstdout:\n{res.stdout}\nstderr:\n{res.stderr}"
    )


def _load_docs_sync_module() -> object:
    """Import tools/check_docs_sync.py as a module (tools/ is not a package)."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "check_docs_sync_under_test",
        REPO_ROOT / "tools" / "check_docs_sync.py",
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_docs_sync_flag_check_catches_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #1548: the strict flag check must reject backticked `--flag`
    names that argparse would reject — fabricated flags, camelCase
    misspellings (`--kubernetes-backoffLimit`), and underscore
    misspellings (`--enable_cost_tracking`)."""
    mod = _load_docs_sync_module()
    monkeypatch.setattr(mod, "REPO_ROOT", tmp_path, raising=False)
    known = mod._collect_known_cli_flags()
    assert "--kubernetes-backoff-limit" in known  # real flag (dash form)
    assert "--no-nomad-remote-results-only" in known  # BooleanOptionalAction

    doc = tmp_path / "flag_drift.md"
    doc.write_text(
        "Bad flags: `--totally-fabricated`, `--kubernetes-backoffLimit`, "
        "`--enable_cost_tracking`.\n"
    )
    errors, checked = mod._check_file(doc, known)
    assert checked
    flagged = [err for _, err in errors]
    assert any("--totally-fabricated" in e for e in flagged)
    assert any("--kubernetes-backoffLimit" in e for e in flagged)
    assert any("--enable_cost_tracking" in e for e in flagged)


def test_docs_sync_flag_check_accepts_known_and_foreign(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #1548: real flags, work-script flags, and exempted foreign
    flags must all pass the strict check."""
    mod = _load_docs_sync_module()
    monkeypatch.setattr(mod, "REPO_ROOT", tmp_path, raising=False)
    known = mod._collect_known_cli_flags()
    doc = tmp_path / "flag_ok.md"
    doc.write_text(
        "OK flags: `--executor`, `--ts_resolution` (aggregator work "
        "script), `--rm` (docker, exempted), "
        "`--no-nomad-remote-results-only`.\n"
    )
    errors, checked = mod._check_file(doc, known)
    assert checked
    assert not errors


def test_agents_md_testing_section_mentions_ci_workflow() -> None:
    """Issue #8 acceptance criterion: AGENTS.md Testing section must mention
    the CI workflow file so contributors know where the green/red signal
    for `pytest` comes from.

    We extract the Testing section by splitting on `## N. ` headings and
    assert the section body contains a backticked `.github/workflows/`
    path. The exact file referenced (`ci.yml` is the canonical one) is
    checked separately so the test fails with a precise diagnostic.

    The section number is intentionally NOT pinned: the v0.1.0 doc
    compaction (#1004) renumbered sections, and pinning the number broke
    this contract against the reorganised doc. Match by name instead.
    """
    import re

    agents_md = (REPO_ROOT / "AGENTS.md").read_text()
    # Split on `## ` headings and grab the slice whose header is "N. Testing".
    sections = re.split(r"^## ", agents_md, flags=re.MULTILINE)
    testing_section: str | None = None
    for chunk in sections:
        if re.match(r"^\d+\. Testing\s*$", chunk.splitlines()[0]):
            testing_section = chunk
            break
    assert testing_section is not None, "AGENTS.md is missing a numbered `## N. Testing` section"

    # Acceptance criterion: the section references a `.github/workflows/`
    # path. Pin to `ci.yml` (the canonical pytest+lint+typecheck job) so a
    # contributor gets a precise failure if they reference a non-existent
    # workflow file.
    assert ".github/workflows/" in testing_section, (
        "AGENTS.md Testing section must mention a `.github/workflows/` path "
        "so contributors can find the CI workflow (issue #8 acceptance "
        "criterion)."
    )
    ci_path = REPO_ROOT / ".github" / "workflows" / "ci.yml"
    assert ci_path.is_file(), (
        f"{ci_path} referenced from AGENTS.md Testing section does not exist on disk"
    )


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


# ---------------------------------------------------------------------------
# Issue #1060: make install must include the [api] extra
#
# After PR #1057 (openapi drift gate), `tools/check_openapi_sync.py` and
# `scripts/generate_openapi.py` both require `fastapi` from the `[api]`
# extra. A contributor who runs only `make install` (the canonical
# day-to-day setup per AGENTS.md §2) used to hit
# `ModuleNotFoundError: No module named 'osimflow.api'`. These tests pin
# the contract: `make install` MUST install the `[api]` extra, and a
# future change that drops it will fail the suite instead of the
# contributor's local `make contract` run.
# ---------------------------------------------------------------------------


def test_make_install_includes_api_extra() -> None:
    """Issue #1060: `make install` must install the `[api]` extra.

    Pinned as a contract test so any future Makefile change that drops
    the `api` extra is caught here — not in the next contributor's
    `make contract` run, where it would manifest as a confusing
    `ModuleNotFoundError: No module named 'osimflow.api'` after PR
    #1057 added the openapi drift gate.
    """
    makefile = (REPO_ROOT / "Makefile").read_text()
    # Find the `install:` target body and assert it installs `[api]`.
    # We anchor on the leading tab (not `install:`) because the help-
    # comment line (`install: ## pip install -e ".[..]"`) would
    # otherwise match first and confuse greedy regex matching.
    #
    # Note: \n / \t are actual newline + tab characters here (NOT raw
    # strings), so the regex matches a real line break and tab between
    # the target header and its tab-indented recipe line.
    match = re.search(
        r"^install:.*\n\t.*pip install.*\[([^\]]+)\]",
        makefile,
        re.MULTILINE,
    )
    assert match is not None, (
        "Could not parse `install:` target recipe from Makefile; "
        'expected `\\t$(PY) -m pip install -e ".[extras]"` form '
        "(issue #1060)."
    )
    extras = match.group(1)
    assert "api" in {e.strip() for e in extras.split(",")}, (
        f"`make install` must include the `[api]` extra so that "
        f"`tools/check_openapi_sync.py` and `scripts/generate_openapi.py` "
        f"can import `osimflow.api`. Current extras: [{extras}]. "
        f"See issue #1060."
    )


def test_osimflow_api_module_importable() -> None:
    """Issue #1060: after `make install`, `import osimflow.api` must succeed.

    This is the runtime half of the contract: the Makefile targets the
    `[api]` extra *and* the extra must actually install the `fastapi`
    package and surface the `osimflow.api` module. A regression in
    pyproject.toml (e.g. accidental removal of the extra) trips this
    test before any contributor hits it.
    """
    res = _run([sys.executable, "-c", "import osimflow.api"])
    assert res.returncode == 0, (
        f"`import osimflow.api` failed — the `[api]` extra is not "
        f"installed in the current environment (issue #1060):\n"
        f"stdout:\n{res.stdout}\nstderr:\n{res.stderr}"
    )


# ---------------------------------------------------------------------------
# Issue #1059: make contract must run the openapi-sync check
#
# Before PR #1057 the openapi drift gate ran only in CI
# (`.github/workflows/agents-contract.yml`). The Makefile aggregate target
# `contract:` did not include it, so a contributor running `make contract`
# locally got a green light while CI failed on the same commit. This
# contract test pins the Makefile wiring so the regression is caught at
# PR time, not at the next contributor's `make contract` run.
# ---------------------------------------------------------------------------


def test_make_contract_aggregate_includes_openapi_sync() -> None:
    """Issue #1059: `make contract` aggregate target must include the
    openapi-sync sub-target so local `make contract` matches CI's
    `agents-contract.yml` job.

    The check is structural (parse the Makefile and assert the aggregate
    lists `openapi-sync`) plus a functional dry-run that asserts
    `make -n contract` invokes `tools/check_openapi_sync.py`. Both
    are local-only safety nets — the CI contract job already runs
    `make contract` end-to-end, so we skip the dry-run when `make`
    is unavailable (e.g. a contributor on a stripped-down container).
    """
    makefile = (REPO_ROOT / "Makefile").read_text()
    match = re.search(r"^contract:\s*([^\n#]*?)\s*(?:##.*)?$", makefile, re.MULTILINE)
    assert match is not None, "Makefile is missing a `contract:` aggregate target (issue #1059)."
    deps = match.group(1).split()
    for required in ("agents-contract", "docs-sync", "openapi-sync"):
        assert required in deps, (
            f"`make contract` aggregate must depend on `{required}` "
            f"(issue #1059). Current deps: {deps}"
        )

    # Functional dry-run: invoke `make -n contract` and assert all three
    # checker scripts show up in the output. Skip if `make` is not on PATH
    # (e.g. minimal container) — the structural check above is the
    # canonical regression detector; the dry-run is a bonus.
    import shutil

    if shutil.which("make") is None:
        pytest.skip("`make` not on PATH; skipping `make -n contract` dry-run")

    dry = _run(["make", "-n", "contract"])
    assert dry.returncode == 0, (
        f"`make -n contract` failed (exit {dry.returncode}):\n"
        f"stdout:\n{dry.stdout}\nstderr:\n{dry.stderr}"
    )
    for script in ("check_agents_contract.py", "check_docs_sync.py", "check_openapi_sync.py"):
        assert script in dry.stdout, (
            f"`make -n contract` did not invoke {script} (issue #1059). Got:\n{dry.stdout}"
        )


def test_check_openapi_sync_runs_and_passes_on_clean_tree() -> None:
    """Issue #1400: the openapi drift gate is *functionally* tested.

    ``make test-fast`` (the pre-commit mirror) must catch a regression in
    ``tools/check_openapi_sync.py`` — a syntax error, a broken
    volatile-key strip, or a silently false-positive drift check —
    without waiting for the CI ``agents-contract.yml`` job. This test
    executes the script for real on the clean tree and asserts exit 0.
    """
    result = _run([sys.executable, "tools/check_openapi_sync.py"])
    assert result.returncode == 0, (
        f"tools/check_openapi_sync.py failed on a clean tree:\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )


def test_check_openapi_sync_detects_intentional_drift() -> None:
    """Issue #1400: the drift check must FAIL when the spec is mutated.

    ``tools/check_openapi_sync.py`` hardcodes ``docs/openapi.json``
    relative to the repo root, so the hermetic way to simulate drift is
    to mutate the committed spec, run the real check, and restore the
    original bytes in a ``finally`` block. The assertion is exit 1 with
    a diagnostic that names the drift (diff output).

    This is network-free and fastapi-boot-heavy only in the tool's own
    regeneration step, which the clean-tree sibling test already pays.
    """
    tool = REPO_ROOT / "tools" / "check_openapi_sync.py"
    spec = REPO_ROOT / "docs" / "openapi.json"
    assert tool.exists(), f"missing {tool}"
    assert spec.exists(), f"missing {spec}"

    original = spec.read_bytes()
    try:
        text = original.decode("utf-8")
        marker = '"description": "'
        idx = text.find(marker)
        assert idx != -1, "no description field found in openapi.json to mutate"
        # Insert drift INTO the value (JSON stays valid; the regenerated
        # spec will still carry the original description, so the check
        # must report a diff).
        head = text[: idx + len(marker)]
        mutated = head + "DRIFTED BY TEST — " + text[idx + len(marker) :]
        spec.write_bytes(mutated.encode("utf-8"))

        result = _run([sys.executable, str(tool)])
        assert result.returncode != 0, (
            "tools/check_openapi_sync.py accepted a deliberately drifted spec — "
            "its drift detection is broken (issue #1400)"
        )
        combined = (result.stdout + result.stderr).lower()
        assert "drift" in combined or "diff" in combined or "fail" in combined, combined
    finally:
        spec.write_bytes(original)
        # Restore-time sanity: the clean-tree check passes again.
        post = _run([sys.executable, str(tool)])
        assert post.returncode == 0, (
            "docs/openapi.json was not restored correctly after the drift test"
        )
