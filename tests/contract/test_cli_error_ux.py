"""CLI error-UX contract for the ``run`` subcommand (issue #1461).

Invalid run inputs — a missing ``--input_variables`` file, a missing
``--template_sim_package`` directory, or a malformed variables.yml — must
surface as a one-line, flag-referencing error on stderr (plus a pointer to
``--help``) with a non-zero exit and **no raw Python traceback**. This pins
the friendly-error UX for the single most common first-run mistakes.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_PACKAGE = REPO_ROOT / "example_package"

pytestmark = pytest.mark.contract


def _run_cli(*cli_args: str) -> subprocess.CompletedProcess[str]:
    """Invoke ``python -m osimflow run ...`` exactly as a user would."""
    return subprocess.run(
        [sys.executable, "-m", "osimflow", "run", *cli_args],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env={**os.environ, "OSIMFLOW_STUB_SIM": "1"},
    )


def _base_args(input_variables: str, template_sim_package: str, outdir: Path) -> list[str]:
    """Minimal valid ``run`` invocation; callers break one input at a time."""
    return [
        "--executor",
        "local",
        "--input_variables",
        input_variables,
        "--template_sim_package",
        template_sim_package,
        "--n_samples",
        "1",
        "--outdir",
        str(outdir),
        "--openstudio_version",
        "3.11.0",
    ]


def _assert_friendly_failure(result: subprocess.CompletedProcess[str]) -> None:
    """Shared shape: non-zero exit, --help pointer, and no traceback."""
    assert result.returncode != 0, (
        f"expected non-zero exit, got 0\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "See 'osimflow run --help' for usage." in result.stderr, result.stderr
    assert "Traceback" not in result.stderr, f"raw traceback leaked to stderr:\n{result.stderr}"
    assert "Traceback" not in result.stdout, f"raw traceback leaked to stdout:\n{result.stdout}"


def test_run_missing_input_variables_friendly_error(tmp_path: Path) -> None:
    """Missing --input_variables file: one-line error naming the flag."""
    result = _run_cli(
        *_base_args("/nonexistent/variables.yml", str(EXAMPLE_PACKAGE), tmp_path / "out")
    )
    _assert_friendly_failure(result)
    assert "error: --input_variables: file not found: /nonexistent/variables.yml" in (result.stderr)


def test_run_missing_template_sim_package_friendly_error(tmp_path: Path) -> None:
    """Missing --template_sim_package dir: one-line error naming the flag."""
    result = _run_cli(
        *_base_args(
            str(EXAMPLE_PACKAGE / "variables.yml"), "/nonexistent/template_pkg", tmp_path / "out"
        )
    )
    _assert_friendly_failure(result)
    assert (
        "error: --template_sim_package: file not found: /nonexistent/template_pkg" in result.stderr
    )


def test_run_malformed_variables_yml_friendly_error(tmp_path: Path) -> None:
    """Malformed YAML in --input_variables: friendly one-liner, no traceback."""
    bad_yml = tmp_path / "variables_bad.yml"
    bad_yml.write_text("variables: [unclosed\n", encoding="utf-8")
    result = _run_cli(*_base_args(str(bad_yml), str(EXAMPLE_PACKAGE), tmp_path / "out"))
    _assert_friendly_failure(result)
    assert "error: --input_variables: Invalid YAML in variables.yml:" in result.stderr
    # The YAML parse detail (line/column context) must be flattened onto the
    # single friendly line, not dumped as a multi-line blob.
    error_lines = [line for line in result.stderr.splitlines() if "Invalid YAML" in line]
    assert len(error_lines) == 1, result.stderr
    assert error_lines[0].startswith("error: --input_variables:"), result.stderr
