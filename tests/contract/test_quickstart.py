"""Quickstart staleness test (issue #191).

Runs the exact README quickstart command in stub mode and asserts the
expected output artifacts exist. This ensures the README never drifts
from the actual CLI surface — if a flag is renamed or an output path
changes, this test fails.

The test uses ``OSIMFLOW_STUB_SIM=1`` so it runs without the real
OpenStudio CLI installed.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

# Docs covered by the install-instructions drift guard (issue #1459).
INSTALL_DOCS = ("README.md", "docs/DEVELOPMENT.md", "docs/CONTRIBUTING.md")

# An extras list quoted in a ``pip install -e ".[...]"`` context.
PIP_INSTALL_E_EXTRAS_RE = re.compile(r'pip install -e\s+"?\.\[([^\]]+)\]"?')

# How close (in lines) a non-canonical extras list must be to an explicit
# ``make install`` mention to count as "pointing at the supported path".
MAKE_INSTALL_MENTION_WINDOW = 15


@pytest.mark.contract
def test_readme_quickstart_produces_expected_artifacts(tmp_path: Path) -> None:
    """The README quickstart command must exit 0 and produce the three
    expected output artifacts: ``aggregated_results.csv``, ``run.json``,
    and the ``plots/`` directory.
    """
    outdir = tmp_path / "results"
    outdir.mkdir()

    env = {**os.environ, "OSIMFLOW_STUB_SIM": "1"}

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "osimflow",
            "run",
            "--executor",
            "local",
            "--input_variables",
            str(REPO_ROOT / "example_package" / "variables.yml"),
            "--template_sim_package",
            str(REPO_ROOT / "example_package"),
            "--n_samples",
            "5",
            "--outdir",
            str(outdir),
            "--openstudio_version",
            "3.11.0",
        ],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env=env,
    )

    assert result.returncode == 0, (
        f"Quickstart command failed (exit {result.returncode}):\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )

    # Expected output artifacts from the README quickstart table.
    csv_path = outdir / "aggregated_results.csv"
    run_json = outdir / "run.json"
    plots_dir = outdir / "plots"

    assert csv_path.is_file(), f"Missing expected artifact: {csv_path}"
    assert run_json.is_file(), f"Missing expected artifact: {run_json}"
    assert plots_dir.is_dir(), f"Missing expected artifact: {plots_dir}"


@pytest.mark.contract
def test_makefile_install_extras_string_is_parseable() -> None:
    """The Makefile ``install`` recipe quotes the full extras set; the
    drift guard needs it as the source of truth. Pure text parse — no
    subprocess, no network.
    """
    extras = _make_install_extras()
    assert "dev" in extras, f"Makefile extras missing 'dev': {sorted(extras)}"
    assert "api" in extras, f"Makefile extras missing 'api': {sorted(extras)}"


@pytest.mark.contract
@pytest.mark.parametrize("doc", INSTALL_DOCS)
def test_install_docs_extras_consistent_with_makefile(doc: str) -> None:
    """Drift guard (issue #1459): every ``pip install -e`` extras list in
    the install docs must either equal the full ``make install`` extras
    set (parsed from the Makefile) or appear within
    ``MAKE_INSTALL_MENTION_WINDOW`` lines of an explicit ``make install``
    mention, so the supported path is always in sight.
    """
    full_extras = _make_install_extras()
    text = (REPO_ROOT / doc).read_text(encoding="utf-8")
    assert "make install" in text, f"{doc} never mentions 'make install'"

    lines = text.splitlines()
    for lineno, line in enumerate(lines, start=1):
        for match in PIP_INSTALL_E_EXTRAS_RE.finditer(line):
            extras = {part.strip() for part in match.group(1).split(",")}
            if extras == set(full_extras):
                continue
            window = lines[
                max(0, lineno - 1 - MAKE_INSTALL_MENTION_WINDOW) : lineno
                + MAKE_INSTALL_MENTION_WINDOW
            ]
            assert any("make install" in nearby for nearby in window), (
                f"{doc}:{lineno} quotes a non-canonical extras list "
                f"'{match.group(0)}' with no 'make install' mention within "
                f"{MAKE_INSTALL_MENTION_WINDOW} lines. Quote the full "
                f"make-install extras set ({','.join(sorted(full_extras))}) "
                f"or point the reader at 'make install' nearby."
            )


def _make_install_extras() -> frozenset[str]:
    """Parse the extras set from the Makefile ``install`` rule block.

    The Makefile is the canonical dev-environment entry point; its
    ``install`` rule (plus its tab-indented recipe) contains exactly one
    ``pip install -e ".[...]"`` line. Other targets may lazily install
    smaller subsets — those are not the supported full dev environment.
    """
    makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    rule = re.search(
        r"^install:[^\n]*\n(?:\t[^\n]*\n?)*",
        makefile,
        flags=re.MULTILINE,
    )
    assert rule is not None, "No 'install:' target found in Makefile"
    matches = PIP_INSTALL_E_EXTRAS_RE.findall(rule.group(0))
    assert matches, "Makefile 'install' rule has no 'pip install -e' line"
    extras_sets = {frozenset(m.split(",")) for m in matches}
    assert len(extras_sets) == 1, (
        f"Makefile 'install' rule comment and recipe disagree on the "
        f"extras set: {sorted(map(sorted, extras_sets))}"
    )
    return frozenset(part.strip() for part in matches[0].split(",") if part.strip())
