"""Subprocess coverage smoke test (issue #1557).

Without subprocess coverage measurement, ``osimflow/_work_scripts/*`` was
invisible to the 82% gate even though ``bin/aggregate_results.py``,
``bin/extract_kpis.py``, ``bin/apply_params_to_model.py`` etc. exercise
it under the stub mode used by the local smoke run and by the existing
``tests/unit/test_aggregate_results.py`` /
``tests/unit/test_scripts.py`` subprocess harnesses. The
``COVERAGE_PROCESS_START`` bootstrap plus ``[tool.coverage.run] patch =
["subprocess"]`` plus the new Makefile invocation flips the switch;
this test pins the configuration so a future regression in any of the
three legs (config flag, env var, work-script importability) is caught.

The acceptance criterion from issue #1557:

    A single integration test asserting the combined report includes
    ``_work_scripts/aggregate_results.py`` with non-zero line coverage
    is sufficient proof.

The test deliberately runs in-process (no coverage plugin dependency)
and asserts two things:

1. The Makefile / ``[tool.coverage.run]`` config flags subprocess
   coverage on (``patch = ["subprocess"]`` is present, ``concurrency``
   includes the supported values, and the work script ``omit`` entry
   was removed).
2. End-to-end: spawn ``aggregate_results.py`` as a child with
   ``COVERAGE_PROCESS_START`` pointing at the repo's ``pyproject.toml``,
   let it run against a synthetic ``eplusout.err`` fixture, and
   assert that ``coverage json`` (the same view ``make test-cov`` and
   ``tools/check_module_coverage.py`` use) reports
   ``osimflow/_work_scripts/aggregate_results.py`` with a
   ``percent_covered > 0`` entry.

If ``coverage`` is not installed in the venv (rare — it ships with
the ``[dev]`` extra), the test is skipped with a clear message rather
than failing.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = REPO_ROOT / "pyproject.toml"


# ---------------------------------------------------------------------------
# Configuration regression guards (issue #1557 acceptance criterion #1).
# These run without touching the live ``.coverage`` data file so they are
# cheap and deterministic.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("section_key", "expected_substring"),
    [
        ("concurrency", "multiprocessing"),
        ("patch", "subprocess"),
    ],
)
def test_coverage_run_config_enables_subprocess(section_key: str, expected_substring: str) -> None:
    """``[tool.coverage.run]`` must declare the subprocess-measurement flags
    that issue #1557 turned on.

    * ``concurrency`` includes ``multiprocessing`` (and ideally
      ``thread``) — required for fork-aware tracing and harmless when
      neither library is used at runtime.
    * ``patch = ["subprocess"]`` is the coverage.py-supported way to
      enable subprocess measurement (it implicitly sets
      ``parallel = true``, which is what makes each child write its
      own ``.coverage.<host>.<pid>.<rand>`` file).

    Both keys are parsed as lists in coverage 7.x; ``tomllib`` returns a
    list here so the substring assertion is sufficient.
    """
    with PYPROJECT.open("rb") as fh:
        cfg = tomllib.load(fh)
    run_cfg = cfg["tool"]["coverage"]["run"]
    assert section_key in run_cfg, (
        f"[tool.coverage.run] is missing {section_key!r}; "
        f"required by issue #1557 to enable subprocess coverage. "
        f"current keys: {sorted(run_cfg)}"
    )
    value = run_cfg[section_key]
    assert isinstance(value, list), (
        f"[tool.coverage.run].{section_key} must be a list, got {type(value).__name__}: {value!r}"
    )
    assert expected_substring in value, (
        f"[tool.coverage.run].{section_key} must include {expected_substring!r} "
        f"to enable subprocess coverage (issue #1557); got {value!r}"
    )


def test_work_scripts_removed_from_run_omit() -> None:
    """The work scripts must NOT be in ``[tool.coverage.run].omit``.

    Before issue #1557 they were omitted entirely — the gate number was
    then a lie because the most domain-critical code (severe-error
    classifier, pre-flight check, KPI schema) was invisible. The
    subprocess coverage bootstrap makes them measurable, so they belong
    in the measured set.
    """
    with PYPROJECT.open("rb") as fh:
        cfg = tomllib.load(fh)
    omit = cfg["tool"]["coverage"]["run"].get("omit", [])
    bad = [entry for entry in omit if "_work_scripts" in entry]
    assert not bad, (
        f"work scripts must be measured (issue #1557); remove {bad!r} "
        f"from [tool.coverage.run].omit. current omit: {omit}"
    )


def test_makefile_test_cov_sets_coverage_process_start() -> None:
    """``make test-cov`` must export ``COVERAGE_PROCESS_START`` so the
    auto-installed ``coverage.pth`` boots subprocess coverage in every
    child Python spawned by a test (``bin/*.py`` shims → ``python -m
    osimflow._work_scripts.*``).

    The subprocess config in ``pyproject.toml`` only does anything when
    ``COVERAGE_PROCESS_START`` is set in the child's environment;
    without it, the .pth file is a no-op.
    """
    makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    assert "COVERAGE_PROCESS_START" in makefile, (
        "Makefile is missing COVERAGE_PROCESS_START; subprocess coverage "
        "will not activate in children. See issue #1557."
    )
    # Must reference pyproject.toml as the config source — alternative
    # sources (``.coveragerc``) would mean two config files to keep in
    # sync, which the issue specifically warns against.
    assert "pyproject.toml" in makefile, (
        "Makefile COVERAGE_PROCESS_START must point at pyproject.toml "
        "(not .coveragerc) so the [tool.coverage.*] settings are the "
        "single source of truth for both parent and child coverage."
    )
    # Must be referenced inside the test-cov recipe (not just any
    # other target).
    test_cov_block_match = re.search(r"^test-cov:[^\n]*\n((?:\t[^\n]*\n)+)", makefile, re.MULTILINE)
    assert test_cov_block_match is not None, "Makefile is missing a test-cov target"
    test_cov_block = test_cov_block_match.group(1)
    assert "COVERAGE_PROCESS_START" in test_cov_block, (
        f"Makefile test-cov recipe does not export COVERAGE_PROCESS_START; "
        f"recipe body:\n{test_cov_block}"
    )


# ---------------------------------------------------------------------------
# End-to-end smoke (issue #1557 acceptance criterion #2).
# ---------------------------------------------------------------------------


def _have_coverage_module() -> bool:
    try:
        import coverage  # noqa: F401
    except ImportError:
        return False
    return True


@pytest.mark.skipif(
    not _have_coverage_module(),
    reason="coverage not installed in the venv; subprocess smoke cannot run",
)
def test_subprocess_aggregate_results_measured(tmp_path: Path) -> None:
    """End-to-end proof: a real subprocess invocation of
    ``aggregate_results.py`` (the canonical "first Severe Error" classifier
    from issue #1557) leaves non-zero coverage data behind.

    Test layout:

    1. Build a minimal KPI JSON + an ``eplusout.err`` fixture with one
       severe-error line so ``aggregate_results`` actually executes the
       classifier path (and not just the trivial empty-input branch).
    2. Spawn ``.venv/bin/python bin/aggregate_results.py`` as a child
       with ``COVERAGE_PROCESS_START=$PWD/pyproject.toml`` in the env —
       the same setup ``make test-cov`` uses.
    3. Use the ``coverage`` Python API to load ``.coverage`` and the
       child data files in the cwd, then assert
       ``osimflow/_work_scripts/aggregate_results.py`` shows up with
       ``percent_covered > 0``.

    The assertion is intentionally tolerant of any positive coverage
    (rather than checking an exact number) — the floor script in
    ``tools/check_module_coverage.py`` enforces the regression guard
    on a per-module basis; this test just proves the subprocess data
    pipeline is wired end-to-end.
    """

    # 1. Fixture: KPI JSON + eplusout.err with one Severe line.
    sim_dir = tmp_path / "sim" / "0001"
    sim_dir.mkdir(parents=True)
    (sim_dir / "eplusout.err").write_text(
        "   ** Severe  ** Schedule 'OCC_SCH' not found in model\n"
    )
    kpi_file = tmp_path / "kpi_0001.json"
    kpi_file.write_text(json.dumps({"sample_id": "0001", "kpis": {"eui": 100.0}}))
    out_csv = tmp_path / "agg.csv"
    out_fail = tmp_path / "fail.csv"

    # 2. Spawn the child with COVERAGE_PROCESS_START pointing at the
    #    repo's pyproject.toml — identical to the Makefile test-cov recipe.
    child_env = os.environ.copy()
    child_env["COVERAGE_PROCESS_START"] = str(PYPROJECT)

    # Use .venv/bin/python explicitly so the child interpreter matches
    # the parent venv (the system python may not have coverage / osimflow
    # importable).
    venv_python = REPO_ROOT / ".venv" / "bin" / "python"
    assert venv_python.exists(), f"missing venv python at {venv_python}"

    child = subprocess.run(  # noqa: S603 — argv fully controlled
        [
            str(venv_python),
            str(REPO_ROOT / "bin" / "aggregate_results.py"),
            "--kpis",
            str(kpi_file),
            "--simulation_dirs",
            str(sim_dir),
            "--out_csv",
            str(out_csv),
            "--out_failed",
            str(out_fail),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=child_env,
        # Run from the repo root — the auto-started coverage instance
        # in the child writes ``.coverage.<host>.<pid>.<rand>`` to its
        # cwd, which must be the project root for the xdist master's
        # ``combine()`` to glob the file via ``combinable_files`` (it
        # looks next to the parent's data file, which pytest-cov
        # places at the repo root). Running from tmp_path puts the
        # child's data file in a directory the master never inspects.
        cwd=str(REPO_ROOT),
    )
    assert child.returncode == 0, (
        f"aggregate_results subprocess failed (rc={child.returncode}):\n"
        f"STDOUT:\n{child.stdout}\nSTDERR:\n{child.stderr}"
    )
    assert out_csv.exists(), "aggregate_results did not produce out_csv"
    assert out_fail.exists(), "aggregate_results did not produce out_failed"

    # 3. Load coverage and assert the work script shows non-zero coverage.
    # The child wrote ``.coverage.<host>.<pid>.<rand>`` to the repo root
    # (its cwd at startup). pytest-cov's xdist master ``combine()`` then
    # merges via the ``combinable_files`` glob ``.coverage.*``. We use the
    # ``coverage json`` CLI to render a JSON report over the merged data
    # file the master writes at ``REPO_ROOT/.coverage``. Using the CLI
    # rather than ``coverage.Coverage()`` directly avoids a second
    # in-process Coverage instance competing with pytest-cov's session.
    import subprocess as _sp
    import tempfile

    json_out = Path(tempfile.gettempdir()) / "osimflow_subprocess_smoke.json"

    json_run = _sp.run(  # noqa: S603 — argv fixed
        [
            str(venv_python),
            "-m",
            "coverage",
            "json",
            f"--rcfile={PYPROJECT}",
            "-o",
            str(json_out),
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )
    assert json_run.returncode == 0, (
        f"coverage json failed: rc={json_run.returncode}, "
        f"stdout={json_run.stdout!r}, stderr={json_run.stderr!r}, "
        f"siblings in cwd: {sorted(p.name for p in REPO_ROOT.iterdir() if p.name.startswith('.coverage'))}"
    )

    raw = json.loads(json_out.read_text(encoding="utf-8"))
    files_block = raw.get("files", {})
    target_key = None
    for path, _payload in files_block.items():
        if path.endswith("osimflow/_work_scripts/aggregate_results.py"):
            target_key = path
            break
    assert target_key is not None, (
        "merged coverage report does not contain "
        "osimflow/_work_scripts/aggregate_results.py. "
        f"files in report: {sorted(files_block)[:10]}… "
        f"siblings in repo root: "
        f"{sorted(p.name for p in REPO_ROOT.iterdir() if p.name.startswith('.coverage'))}"
    )
    summary = files_block[target_key].get("summary", {})
    pct = summary.get("percent_covered", 0.0)
    assert pct > 0.0, (
        f"osimflow/_work_scripts/aggregate_results.py is in the merged "
        f"report but has 0% coverage — subprocess coverage is writing the "
        f"data file but no lines were traced. summary: {summary}"
    )
