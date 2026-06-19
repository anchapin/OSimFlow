"""Pytest configuration for OSimFlow tests.

Adds the project root to sys.path so `import osimflow` works without an
editable install. Tests that need the project on sys.path will work
either way.
"""

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def pytest_configure(config: pytest.Config) -> None:
    """Force stub simulation mode so tests work without a real OpenStudio CLI.

    When ``openstudio`` or ``openstudio.cli`` is on PATH but the test
    template lacks a proper workflow.osw, the work functions would
    otherwise try to invoke the real CLI and fail. Setting
    ``OSIMFLOW_STUB_SIM=1`` forces the stub code path in both
    ``run_openstudio_sim`` and ``default_apply_parameters``.
    """
    os.environ.setdefault("OSIMFLOW_STUB_SIM", "1")
