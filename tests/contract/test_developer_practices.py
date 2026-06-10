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
    # integration suite that exercises the osimflow/ package surface.
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
