"""Pytest configuration for OSimFlow tests.

Adds the project root to sys.path so `import osimflow` works without an
editable install. Tests that need the project on sys.path will work
either way.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
