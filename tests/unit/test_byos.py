"""Unit tests for osimflow/byos.py — BYOS (Bring Your Own Script) loader.

Covers:
  * load_user_function: valid apply_parameters, extract_kpis, apply (deprecated)
  * Function not found in module: AttributeError with helpful message
  * Script path not found: ImportError when spec cannot be loaded
  * Deprecated 'apply' name triggers DeprecationWarning
  * Non-callable attribute with matching name is skipped
  * Module with syntax errors raises ImportError
"""

from __future__ import annotations

import warnings
from pathlib import Path

import pytest

from osimflow.byos import load_user_function


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def user_scripts(tmp_path: Path) -> Path:
    d = tmp_path / "user_scripts"
    d.mkdir()
    return d


def _write_script(directory: Path, filename: str, content: str) -> Path:
    p = directory / filename
    p.write_text(content)
    return p


# ===========================================================================
# Valid function discovery
# ===========================================================================
class TestLoadUserFunctionValid:
    def test_discovers_apply_parameters(self, user_scripts: Path) -> None:
        path = _write_script(
            user_scripts,
            "my_apply.py",
            "def apply_parameters(template, parameters, sample_id, out):\n    pass\n",
        )
        func = load_user_function(path)
        assert callable(func)
        assert func.__name__ == "apply_parameters"

    def test_discovers_extract_kpis(self, user_scripts: Path) -> None:
        path = _write_script(
            user_scripts,
            "my_kpis.py",
            "def extract_kpis(simulation_dir, sample_id, out):\n    return {}\n",
        )
        func = load_user_function(path)
        assert callable(func)
        assert func.__name__ == "extract_kpis"

    def test_apply_parameters_takes_precedence_over_extract_kpis(
        self,
        user_scripts: Path,
    ) -> None:
        path = _write_script(
            user_scripts,
            "both.py",
            "def apply_parameters(t, p, s, o): pass\ndef extract_kpis(s, sid, o): pass\n",
        )
        func = load_user_function(path)
        assert func.__name__ == "apply_parameters"

    def test_function_is_callable(self, user_scripts: Path) -> None:
        path = _write_script(
            user_scripts,
            "callable.py",
            "def apply_parameters(template, parameters, sample_id, out):\n    return 42\n",
        )
        func = load_user_function(path)
        assert func(template="t", parameters={}, sample_id="s", out="o") == 42


# ===========================================================================
# Deprecated 'apply' function name
# ===========================================================================
class TestLoadUserFunctionDeprecated:
    def test_apply_name_discovered_with_warning(self, user_scripts: Path) -> None:
        path = _write_script(
            user_scripts,
            "legacy.py",
            "def apply(template, parameters, sample_id, out):\n    pass\n",
        )
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            func = load_user_function(path)
        assert func.__name__ == "apply"
        deprecation = [x for x in w if issubclass(x.category, DeprecationWarning)]
        assert len(deprecation) == 1
        assert "deprecated" in str(deprecation[0].message).lower()
        assert "apply_parameters" in str(deprecation[0].message)

    def test_apply_parameters_takes_precedence_over_apply(
        self,
        user_scripts: Path,
    ) -> None:
        path = _write_script(
            user_scripts,
            "both_new.py",
            "def apply_parameters(t, p, s, o): pass\ndef apply(t, p, s, o): pass\n",
        )
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            func = load_user_function(path)
        assert func.__name__ == "apply_parameters"
        deprecation = [x for x in w if issubclass(x.category, DeprecationWarning)]
        assert len(deprecation) == 0


# ===========================================================================
# Function not found
# ===========================================================================
class TestLoadUserFunctionNotFound:
    def test_no_matching_name_raises_attribute_error(
        self,
        user_scripts: Path,
    ) -> None:
        path = _write_script(
            user_scripts,
            "no_func.py",
            "def some_other_function():\n    pass\n",
        )
        with pytest.raises(AttributeError, match="apply_parameters"):
            load_user_function(path)

    def test_error_message_mentions_required_names(
        self,
        user_scripts: Path,
    ) -> None:
        path = _write_script(
            user_scripts,
            "blank.py",
            "x = 42\n",
        )
        with pytest.raises(AttributeError, match="extract_kpis"):
            load_user_function(path)


# ===========================================================================
# Non-callable attribute with matching name
# ===========================================================================
class TestLoadUserFunctionNonCallable:
    def test_non_callable_apply_parameters_skipped(
        self,
        user_scripts: Path,
    ) -> None:
        path = _write_script(
            user_scripts,
            "non_callable.py",
            "apply_parameters = 'not a function'\nextract_kpis = 42\n",
        )
        with pytest.raises(AttributeError):
            load_user_function(path)


# ===========================================================================
# Invalid file path
# ===========================================================================
class TestLoadUserFunctionPathErrors:
    def test_nonexistent_path_raises_file_not_found(
        self,
        user_scripts: Path,
    ) -> None:
        missing = user_scripts / "does_not_exist.py"
        with pytest.raises((ImportError, FileNotFoundError)):
            load_user_function(missing)


# ===========================================================================
# Module with syntax errors
# ===========================================================================
class TestLoadUserFunctionSyntaxError:
    def test_syntax_error_raises(self, user_scripts: Path) -> None:
        path = _write_script(
            user_scripts,
            "bad_syntax.py",
            "def apply_parameters(\n",
        )
        with pytest.raises(SyntaxError):
            load_user_function(path)


# ===========================================================================
# Module with side effects on import
# ===========================================================================
class TestLoadUserFunctionModuleIsolation:
    def test_module_code_runs_on_load(self, user_scripts: Path) -> None:
        marker = user_scripts / "marker.txt"
        path = _write_script(
            user_scripts,
            "side_effects.py",
            "from pathlib import Path\n"
            f"Path('{marker!s}').write_text('loaded')\n"
            "def apply_parameters(t, p, s, o): pass\n",
        )
        assert not marker.exists()
        load_user_function(path)
        assert marker.exists()
        assert marker.read_text() == "loaded"
