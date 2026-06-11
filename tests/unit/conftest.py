"""Shared pytest fixtures for the OSimFlow unit test suite.

Provides:
- Session-scoped ``_session_example_package`` — copies example_package/ once
  per test session, avoiding ~40 repeated shutil.copytree calls.
- Per-test ``template_pkg`` / ``variables_yml`` / ``outdir`` / ``workdir``
  fixtures that copy from the session cache instead of from the repo root.
- ``xdist_group`` markers for AlgorithmRegistry-mutating tests.

Both Option A (xdist loadgroup) and Option C (session-scoped package cache)
live here so every test file can use them without local fixture duplication.
"""

import shutil
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_PKG = REPO_ROOT / "example_package"
EXAMPLE_VARS_YML = REPO_ROOT / "variables.yml"


# ---------------------------------------------------------------------------
# Session-scoped package cache (Option C)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def _session_example_package(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Copy ``example_package/`` once per test session.

    Individual tests still get their own writable copy via the
    ``template_pkg`` fixture below, but each copy sources from this
    session-scoped cache rather than from the repo tree, saving ~40
    ``shutil.copytree`` syscalls from hitting the real filesystem.
    """
    dest = tmp_path_factory.mktemp("session_pkg") / "example_package"
    shutil.copytree(EXAMPLE_PKG, dest)
    return dest


@pytest.fixture(scope="session")
def _session_variables_yml(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Copy ``variables.yml`` once per test session."""
    dest = tmp_path_factory.mktemp("session_vars") / "variables.yml"
    shutil.copy2(EXAMPLE_VARS_YML, dest)
    return dest


# ---------------------------------------------------------------------------
# Per-test fixtures (fast copies from session cache)
# ---------------------------------------------------------------------------


@pytest.fixture
def template_pkg(tmp_path: Path, _session_example_package: Path) -> Path:
    """Writable copy of example_package/ sourced from the session cache."""
    pkg = tmp_path / "template"
    shutil.copytree(_session_example_package, pkg)
    return pkg


@pytest.fixture
def variables_yml(tmp_path: Path, _session_variables_yml: Path) -> Path:
    """Writable copy of variables.yml sourced from the session cache."""
    vyml = tmp_path / "variables.yml"
    shutil.copy2(_session_variables_yml, vyml)
    return vyml


@pytest.fixture
def outdir(tmp_path: Path) -> Path:
    """Fresh output directory for a campaign."""
    od = tmp_path / "out"
    od.mkdir()
    return od


@pytest.fixture
def workdir(tmp_path: Path, _session_variables_yml: Path) -> Path:
    """Work directory with variables.yml pre-populated.

    Used by tests that need a base directory containing both the
    variables file and a template package (e.g. dry_run, hooks).
    """
    wd = tmp_path / "work"
    wd.mkdir()
    (wd / "variables.yml").write_text(_session_variables_yml.read_text())
    return wd


# ---------------------------------------------------------------------------
# xdist group marker registration (Option A)
# ---------------------------------------------------------------------------


def pytest_configure(config: pytest.Config) -> None:
    """Register the ``xdist_group`` marker so ``--strict-markers`` passes."""
    config.addinivalue_line(
        "markers",
        "xdist_group(name): group tests that must run on the same xdist worker",
    )
