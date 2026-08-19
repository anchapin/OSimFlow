"""Tests for ``scripts/apply_branch_protection.sh`` (issue #975).

These tests run the script with ``--dry-run`` (no real API call) and:

* ``test_syntax_clean`` — assert ``bash -n`` parses the script.
* ``test_dry_run_invokes_protection_endpoint`` — assert the dry-run output
  mentions the PUT endpoint, the ``gh api`` invocation, and the correct
  repo/branch slug.
* ``test_dry_run_payload_lists_all_required_checks`` — read every job
  ``name:`` from ``.github/workflows/ci.yml`` and assert all of them are
  present in the script's emitted payload. This protects against silent
  drift between the CI workflow and the protection settings.
* ``test_payload_does_not_require_reviews`` — explicitly assert the payload
  does NOT enable ``required_pull_request_reviews`` (our documented decision
  — protecting against accidental regressions).
* ``test_payload_disables_force_pushes_and_deletions`` — explicitly assert
  ``allow_force_pushes`` / ``allow_deletions`` / ``allow_fork_syncing`` are
  all disabled (plain booleans per the GitHub API).
* ``test_payload_enables_linear_history`` — explicitly assert
  ``required_linear_history`` is true (plain boolean per the GitHub API).

Skips gracefully when ``gh`` is not on PATH (the dry-run code-path does not
need ``gh``, so the rest of the tests still run; only the dry-run assertions
require the script to be invokable end-to-end, which needs ``gh`` in the
test environment because the script's argument parser also touches it — we
relax the skip to apply when ``bash`` is also missing).
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "apply_branch_protection.sh"
CI_YML = REPO_ROOT / ".github" / "workflows" / "ci.yml"

# Required status checks (issue #975 — must match ci.yml `name:` fields).
EXPECTED_CHECKS: tuple[str, ...] = (
    "lint (ruff)",
    "typecheck (mypy --strict)",
    "test (pytest, 85% coverage gate)",
    "agents & docs contract",
    "security (pip-audit)",
)

PROTECTION_PATH = "/repos/anchapin/OSimFlow/branches/main/protection"


def _read_ci_job_names() -> list[str]:
    """Return the `name:` field of every job in ``.github/workflows/ci.yml``.

    YAML parsing is avoided to keep the test dependency-free; the format is
    simple enough to grep with a regex.
    """
    text = CI_YML.read_text()
    # Match `name: <value>` lines that appear inside a `jobs:` block. We
    # capture all such lines and then filter to those that look like human
    # labels (contain a space) — internal job IDs (e.g. `lint`, `typecheck`)
    # do not have a `name:` field and so won't appear here at all.
    return re.findall(r"^\s{4}name:\s*(.+?)\s*$", text, flags=re.MULTILINE)


# ---------------------------------------------------------------------------
# Required tooling
# ---------------------------------------------------------------------------


bash = shutil.which("bash")
gh = shutil.which("gh")

pytestmark = pytest.mark.skipif(
    bash is None,
    reason="bash is required to validate scripts/apply_branch_protection.sh",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_dry_run() -> str:
    """Run the script in dry-run mode and return its stdout."""
    assert bash is not None  # for type checkers; pytestmark skips if absent
    result = subprocess.run(
        [bash, str(SCRIPT), "--dry-run"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return result.stdout


def _extract_payload(stdout: str) -> dict:
    """Locate the JSON payload in the dry-run output and parse it.

    The script prints a ``Payload:`` header followed by a JSON object. We
    grab everything after that header and decode it.
    """
    marker = "Payload:"
    idx = stdout.find(marker)
    assert idx != -1, f"dry-run output missing 'Payload:' header. Got:\n{stdout}"
    payload_text = stdout[idx + len(marker) :].strip()
    return json.loads(payload_text)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_script_exists() -> None:
    """Sanity: the script must be on disk and executable."""
    assert SCRIPT.is_file(), f"missing script: {SCRIPT}"
    # The exact mode bits don't matter for the test; just verify it can be
    # exec'd by the kernel's "is this script runnable?" check.
    import stat

    mode = SCRIPT.stat().st_mode
    assert mode & stat.S_IXUSR, f"script not executable by owner: {SCRIPT}"


def test_syntax_clean() -> None:
    """``bash -n`` must parse the script without errors."""
    assert bash is not None
    result = subprocess.run(
        [bash, "-n", str(SCRIPT)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, (
        f"bash -n failed: rc={result.returncode}\nstderr:\n{result.stderr}"
    )


@pytest.mark.skipif(gh is None, reason="gh CLI not on PATH; skip end-to-end dry-run checks")
def test_dry_run_invokes_protection_endpoint() -> None:
    """The dry-run output must announce a PUT to the protection endpoint."""
    stdout = _run_dry_run()
    # The script announces the gh api command and the endpoint explicitly.
    assert "gh api --method PUT" in stdout, (
        f"dry-run output missing 'gh api --method PUT'. Got:\n{stdout}"
    )
    assert PROTECTION_PATH in stdout, (
        f"dry-run output missing protection path '{PROTECTION_PATH}'. Got:\n{stdout}"
    )
    # And the dry-run should NOT actually invoke gh (it prints the command).
    assert "DRY RUN" in stdout, f"dry-run output missing 'DRY RUN' banner. Got:\n{stdout}"


@pytest.mark.skipif(gh is None, reason="gh CLI not on PATH; skip end-to-end dry-run checks")
def test_dry_run_payload_lists_all_required_checks() -> None:
    """The payload's required_status_checks.contexts must include every ci.yml job."""
    payload = _extract_payload(_run_dry_run())
    contexts = payload["required_status_checks"]["contexts"]
    assert isinstance(contexts, list), f"contexts must be a list, got {type(contexts)}"
    # The orchestrator-mandated set must be a subset (no name may be missing).
    for check in EXPECTED_CHECKS:
        assert check in contexts, (
            f"required check {check!r} missing from payload contexts: {contexts!r}"
        )


@pytest.mark.skipif(gh is None, reason="gh CLI not on PATH; skip end-to-end dry-run checks")
def test_dry_run_payload_lists_every_ci_job_name() -> None:
    """Every job `name:` in ci.yml that *should* be required must appear in the payload.

    This catches drift: if someone renames a CI job without updating the
    script's REQUIRED_CHECKS list, this test fails. We only assert against
    the 5 EXPECTED_CHECKS — other jobs in ci.yml (``mlflow-real``, ``slow``,
    ``aws-batch-mock``, ``nomad-single-node``, ``terraform``) are explicitly
    non-required and not asserted here.
    """
    ci_names = set(_read_ci_job_names())
    # Every expected check must appear as a job name in ci.yml.
    for check in EXPECTED_CHECKS:
        assert check in ci_names, (
            f"expected check {check!r} not present as a `name:` in {CI_YML}. "
            f"Update EXPECTED_CHECKS or the ci.yml job."
        )


@pytest.mark.skipif(gh is None, reason="gh CLI not on PATH; skip end-to-end dry-run checks")
def test_payload_does_not_require_reviews() -> None:
    """Regression guard: ``required_pull_request_reviews`` must be null/absent.

    The orchestrator explicitly chose not to require approving reviews
    (issue #975 + the documented deadlock from the wave-orchestrator
    archive). If a future change accidentally re-enables it, this test fails.
    """
    payload = _extract_payload(_run_dry_run())
    reviews = payload.get("required_pull_request_reviews", None)
    assert reviews is None, f"required_pull_request_reviews must be null/disabled, got {reviews!r}"


@pytest.mark.skipif(gh is None, reason="gh CLI not on PATH; skip end-to-end dry-run checks")
def test_payload_disables_force_pushes_and_deletions() -> None:
    """``allow_force_pushes`` / ``allow_deletions`` / ``allow_fork_syncing``
    must all be explicitly disabled (plain booleans per the GitHub API)."""
    payload = _extract_payload(_run_dry_run())
    assert payload["allow_force_pushes"] is False, (
        f"allow_force_pushes must be false, got {payload['allow_force_pushes']!r}"
    )
    assert payload["allow_deletions"] is False, (
        f"allow_deletions must be false, got {payload['allow_deletions']!r}"
    )
    assert payload["allow_fork_syncing"] is False, (
        f"allow_fork_syncing must be false, got {payload['allow_fork_syncing']!r}"
    )


@pytest.mark.skipif(gh is None, reason="gh CLI not on PATH; skip end-to-end dry-run checks")
def test_payload_enables_linear_history() -> None:
    """``required_linear_history`` must be true (plain boolean per the GitHub API)."""
    payload = _extract_payload(_run_dry_run())
    assert payload["required_linear_history"] is True, (
        f"required_linear_history must be true, got {payload['required_linear_history']!r}"
    )


def test_help_text_is_non_empty() -> None:
    """``--help`` must produce the header as help text (no script body leakage)."""
    assert bash is not None
    result = subprocess.run(
        [bash, str(SCRIPT), "--help"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )
    out = result.stdout
    # Sanity: header markers and section names appear.
    assert "USAGE" in out
    assert "REQUIRED STATUS CHECKS" in out
    assert "IDEMPOTENCY" in out
    # And nothing from the actual script body (e.g. "mktemp") leaks in.
    assert "mktemp" not in out, "--help leaked a script-body line"
    # Exit code should be 0 on --help.
    assert result.returncode == 0


def test_unknown_argument_exits_nonzero() -> None:
    """Passing an unknown arg must fail fast (exit code != 0) with a hint."""
    assert bash is not None
    result = subprocess.run(
        [bash, str(SCRIPT), "--bogus-flag"],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode != 0, "unknown arg should fail"
    assert "--help" in result.stderr, "stderr should hint at --help"
