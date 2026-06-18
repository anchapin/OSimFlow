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

from osimflow import Campaign, CampaignConfig
from osimflow.executors import LocalExecutor

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_PKG = REPO_ROOT / "example_package"
EXAMPLE_VARS_YML = REPO_ROOT / "variables.yml"
TEST_VARS_YML = REPO_ROOT / "tests/unit/fixtures/test_variables.yml"


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


_TEST_VARIABLES_YML = """\
algorithm: lhs
variables:
- name: heating_setpoint
  distribution: uniform
  min: 18.0
  max: 22.0
  measure_argument: SetThermostatSchedule.heating_setpoint
- name: cooling_setpoint
  distribution: uniform
  min: 23.0
  max: 28.0
  measure_argument: SetThermostatSchedule.cooling_setpoint
- name: wwr
  distribution: uniform
  min: 0.2
  max: 0.6
  measure_argument: SetEnvelopePerformance.wwr
- name: wall_r_value
  distribution: uniform
  min: 2.0
  max: 5.0
  measure_argument: SetEnvelopePerformance.wall_r_value
"""


@pytest.fixture(scope="session")
def _session_variables_yml(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Write test-compatible ``variables.yml`` matching example_package measures.

    The repo-root ``variables.yml`` references measures not present in
    ``example_package/workflow.osw``, causing pre-flight validation to fail.
    This fixture writes a test-specific variables file that uses only the
    measure arguments actually exposed by the two example_package steps:
    SetThermostatSchedule (heating_setpoint, cooling_setpoint) and
    SetEnvelopePerformance (wwr, wall_r_value).
    """
    dest = tmp_path_factory.mktemp("session_vars") / "variables.yml"
    dest.write_text(_TEST_VARIABLES_YML)
    return dest


@pytest.fixture(scope="session")
def _session_campaign_variables_yml(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Copy ``tests/unit/fixtures/test_variables.yml`` once per test session.

    This fixture provides variables that match the measures available in
    ``example_package/workflow.osw`` (SetThermostatSchedule and
    SetEnvelopePerformance), avoiding pre-flight validation failures when
    running campaign simulations against the minimal example package.
    """
    dest = tmp_path_factory.mktemp("session_campaign_vars") / "test_variables.yml"
    shutil.copy2(TEST_VARS_YML, dest)
    return dest


@pytest.fixture(scope="session")
def _session_preseeded_outdir(
    tmp_path_factory: pytest.TempPathFactory,
    _session_example_package: Path,
    _session_variables_yml: Path,
) -> Path:
    """Run a 3-sample campaign once per session to seed ``samples.json``.

    Individual tests that need a pre-seeded outdir should use the
    ``preseeded_outdir`` per-test fixture below, which copies this
    session-scoped directory.  This avoids re-running the 3-sample
    campaign for every test.
    """
    wd = tmp_path_factory.mktemp("preseed_wd")
    (wd / "variables.yml").write_text(_session_variables_yml.read_text())
    out = tmp_path_factory.mktemp("preseed_out") / "out"
    out.mkdir(parents=True)

    cfg = CampaignConfig(
        input_variables=wd / "variables.yml",
        template_sim_package=_session_example_package,
        n_samples=3,
        outdir=out,
        openstudio_version="3.11.0",
    )
    campaign = Campaign(cfg=cfg, executor=LocalExecutor(max_workers=3))
    campaign.run()
    return out


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
def tmp_dirs(
    tmp_path: Path, _session_example_package: Path, _session_variables_yml: Path
) -> tuple[Path, Path, Path]:
    """Convenience fixture: (variables_yml, template_pkg, outdir) in one tuple."""
    template_pkg = tmp_path / "template"
    shutil.copytree(_session_example_package, template_pkg)
    vyml = tmp_path / "variables.yml"
    shutil.copy2(_session_variables_yml, vyml)
    out = tmp_path / "out"
    out.mkdir()
    return vyml, template_pkg, out


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


@pytest.fixture
def campaign_workdir(tmp_path: Path, _session_campaign_variables_yml: Path) -> Path:
    """Work directory with ``test_variables.yml`` pre-populated.

    Use this fixture for tests that run full campaign simulations
    (dry-run, cache integration, DAG ordering, etc.) against
    ``example_package``.  This fixture uses variables that match
    the two measures in ``example_package/workflow.osw``:
    SetThermostatSchedule (heating_setpoint, cooling_setpoint) and
    SetEnvelopePerformance (wwr, wall_r_value).

    This avoids ``UnmappedParameterError`` during pre-flight validation
    when the repo-root ``variables.yml`` contains ``measure_argument``
    fields referencing measures not present in ``example_package``.
    """
    wd = tmp_path / "work"
    wd.mkdir()
    (wd / "variables.yml").write_text(_session_campaign_variables_yml.read_text())
    return wd


@pytest.fixture
def preseeded_outdir(tmp_path: Path, _session_preseeded_outdir: Path) -> Path:
    """Writable copy of the session-scoped pre-seeded campaign output.

    Contains ``work/samples.json`` with 3 samples from a completed
    campaign.  Uses ``shutil.copytree`` from the session cache to
    avoid re-running the campaign for every test.
    """
    dest = tmp_path / "preseed_out"
    shutil.copytree(_session_preseeded_outdir, dest)
    return dest


# ---------------------------------------------------------------------------
# xdist group marker registration (Option A)
# ---------------------------------------------------------------------------


def pytest_configure(config: pytest.Config) -> None:
    """Register the ``xdist_group`` marker so ``--strict-markers`` passes."""
    config.addinivalue_line(
        "markers",
        "xdist_group(name): group tests that must run on the same xdist worker",
    )
