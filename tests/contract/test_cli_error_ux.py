"""Contract tests for the CLI error UX (issue #1461).

Verifies that invalid config inputs produce a friendly one-line error
message referencing the user-facing flag, a pointer to --help, and NO
Python traceback.
"""

import subprocess
import sys
import tempfile
from pathlib import Path

import pytest


def _run_cli(args: list[str]) -> subprocess.CompletedProcess:
    """Run osimflow CLI as a subprocess and capture output."""
    return subprocess.run(
        [sys.executable, "-m", "osimflow", *args],
        capture_output=True,
        text=True,
        timeout=30,
    )


def _valid_variables_yml() -> str:
    return """\
variables:
  - name: example_var
    type: float
    distribution:
      type: uniform
      min: 0
      max: 1
"""


class TestCLIErrorUX:
    """Contract tests for friendly CLI error messages."""

    def test_missing_input_variables_no_traceback(self, tmp_path):
        """--input_variables for nonexistent file: friendly error, exit 1, no traceback."""
        nonexistent = tmp_path / "variables.yml"
        # Ensure it doesn't exist
        assert not nonexistent.exists()

        result = _run_cli([
            "run",
            "--executor", "local",
            "--input_variables", str(nonexistent),
            "--template_sim_package", str(tmp_path),
            "--n_samples", "1",
            "--outdir", str(tmp_path / "out"),
            "--openstudio_version", "3.11.0",
            "--no-tui",
        ])

        assert result.returncode == 1
        assert "error:" in result.stderr
        assert "--input_variables" in result.stderr
        assert "file not found" in result.stderr
        assert "--help" in result.stderr
        # No Python traceback
        assert "Traceback" not in result.stderr
        assert "FileNotFoundError" not in result.stderr

    def test_missing_template_sim_package_no_traceback(self, tmp_path):
        """--template_sim_package for nonexistent dir: friendly error, exit 1, no traceback."""
        variables_yml = tmp_path / "variables.yml"
        variables_yml.write_text(_valid_variables_yml())

        nonexistent_template = tmp_path / "nonexistent_template"
        assert not nonexistent_template.exists()

        result = _run_cli([
            "run",
            "--executor", "local",
            "--input_variables", str(variables_yml),
            "--template_sim_package", str(nonexistent_template),
            "--n_samples", "1",
            "--outdir", str(tmp_path / "out"),
            "--openstudio_version", "3.11.0",
            "--no-tui",
        ])

        assert result.returncode == 1
        assert "error:" in result.stderr
        assert "--template_sim_package" in result.stderr
        assert "file not found" in result.stderr
        assert "--help" in result.stderr
        assert "Traceback" not in result.stderr

    def test_malformed_yaml_no_traceback(self, tmp_path):
        """Malformed variables.yml: friendly error, exit 1, no traceback."""
        variables_yml = tmp_path / "variables.yml"
        variables_yml.write_text("this is: not: valid: yaml: [")

        template_dir = tmp_path / "template"
        template_dir.mkdir()
        (template_dir / "measures").mkdir()

        result = _run_cli([
            "run",
            "--executor", "local",
            "--input_variables", str(variables_yml),
            "--template_sim_package", str(template_dir),
            "--n_samples", "1",
            "--outdir", str(tmp_path / "out"),
            "--openstudio_version", "3.11.0",
            "--no-tui",
        ])

        assert result.returncode == 1
        assert "error:" in result.stderr
        assert "--help" in result.stderr
        assert "Traceback" not in result.stderr
