"""Unit tests for osimflow/byos.py — BYOS (Bring Your Own Script) loader.

Covers:
  * load_user_function: valid apply_parameters, extract_kpis, apply (deprecated)
  * Function not found in module: AttributeError with helpful message
  * Script path not found: ImportError when spec cannot be loaded
  * Deprecated 'apply' name triggers DeprecationWarning
  * Non-callable attribute with matching name is skipped
  * Module with syntax errors raises SyntaxError
  * Module with side effects on import
  * Subprocess isolation (issue #269): default trust level, kwargs forwarding
  * _parse_subprocess_response: empty stdout, JSON errors, error responses,
    missing result path (lines 207-222)

Note (issue #1005): the BYOS discovery path now runs ``exec_module``
inside a subprocess (see ``_discover_in_subprocess``), so tests that
relied on monkeypatching ``importlib.util.spec_from_file_location`` in
the orchestrator process are no longer meaningful — the subprocess has
its own importlib and the monkeypatch does not propagate.  Subprocess
discovery is exercised end-to-end via real-path tests below
(``test_nonexistent_path_raises_file_not_found``, ``test_syntax_error_raises``).
The malicious-exec_module regression test lives in
``test_byos_exec_module_isolation.py``.
"""

from __future__ import annotations

import json
import warnings
from pathlib import Path

import pytest

from osimflow.byos import (
    ByosTrustLevel,
    _parse_subprocess_response,
    load_user_function,
    validate_trust_level,
)


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

    def test_function_is_callable_inprocess(self, user_scripts: Path) -> None:
        """In-process mode: the raw function is returned and callable."""
        path = _write_script(
            user_scripts,
            "callable.py",
            "def apply_parameters(template, parameters, sample_id, out):\n    return 42\n",
        )
        func = load_user_function(path, trust_level=ByosTrustLevel.INPROCESS)
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


# ===========================================================================
# Subprocess isolation (issue #269)
# ===========================================================================
class TestSubprocessIsolation:
    """Tests for the default BYOS subprocess isolation mode."""

    def test_subprocess_mode_returns_path(self, user_scripts: Path) -> None:
        """Subprocess mode: function returns a Path via the child process."""
        out_marker = user_scripts / "apply_out"
        path = _write_script(
            user_scripts,
            "sub_apply.py",
            "from pathlib import Path\n"
            "def apply_parameters(template, parameters, sample_id, out):\n"
            "    p = Path(str(out)) / sample_id\n"
            "    p.mkdir(parents=True, exist_ok=True)\n"
            "    return p\n",
        )
        func = load_user_function(path, trust_level=ByosTrustLevel.SUBPROCESS)
        result = func(str(user_scripts / "template"), {"k": 1.0}, "0001", str(out_marker))
        assert isinstance(result, Path)
        assert result.name == "0001"

    def test_subprocess_mode_extract_kpis(self, user_scripts: Path) -> None:
        """Subprocess mode: extract_kpis function works in child process."""
        path = _write_script(
            user_scripts,
            "sub_kpi.py",
            "import json\nfrom pathlib import Path\n"
            "def extract_kpis(simulation_dir, sample_id, out):\n"
            "    kpi_file = Path(str(out)) / f'kpi_{sample_id}.json'\n"
            "    kpi_file.parent.mkdir(parents=True, exist_ok=True)\n"
            "    kpi_file.write_text(json.dumps({'sample_id': sample_id, 'eui': 100}))\n"
            "    return kpi_file\n",
        )
        func = load_user_function(path, trust_level=ByosTrustLevel.SUBPROCESS)
        sim_dir = user_scripts / "sim"
        sim_dir.mkdir()
        out_dir = user_scripts / "kpi_out"
        result = func(str(sim_dir), "0001", str(out_dir))
        assert isinstance(result, Path)
        assert result.name == "kpi_0001.json"
        data = json.loads(result.read_text())
        assert data["sample_id"] == "0001"

    def test_subprocess_mode_error_propagates(self, user_scripts: Path) -> None:
        """Subprocess mode: errors in the BYOS script are surfaced."""
        path = _write_script(
            user_scripts,
            "sub_error.py",
            "def apply_parameters(template, parameters, sample_id, out):\n"
            "    raise ValueError('deliberate error')\n",
        )
        func = load_user_function(path, trust_level=ByosTrustLevel.SUBPROCESS)
        with pytest.raises(RuntimeError, match="deliberate error"):
            func(str(user_scripts), {}, "0001", str(user_scripts / "out"))

    def test_subprocess_mode_nonzero_exit(self, user_scripts: Path) -> None:
        """Subprocess mode: sys.exit() in the script surfaces as RuntimeError."""
        path = _write_script(
            user_scripts,
            "sub_exit.py",
            "import sys\n"
            "def apply_parameters(template, parameters, sample_id, out):\n"
            "    sys.exit(1)\n",
        )
        func = load_user_function(path, trust_level=ByosTrustLevel.SUBPROCESS)
        with pytest.raises(RuntimeError, match="exit"):
            func(str(user_scripts), {}, "0001", str(user_scripts / "out"))

    def test_warning_logged_on_load(
        self, user_scripts: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A warning is always logged when a BYOS script is loaded."""
        path = _write_script(
            user_scripts,
            "warn_apply.py",
            "def apply_parameters(t, p, s, o): pass\n",
        )
        import logging

        with caplog.at_level(logging.WARNING, logger="osimflow.byos"):
            func = load_user_function(path, trust_level=ByosTrustLevel.SUBPROCESS)
        assert callable(func)
        assert any("untrusted" in r.message.lower() for r in caplog.records)

    def test_default_trust_level_is_subprocess(self, user_scripts: Path) -> None:
        """Default trust level is subprocess (isolated)."""
        path = _write_script(
            user_scripts,
            "default_apply.py",
            "from pathlib import Path\n"
            "def apply_parameters(template, parameters, sample_id, out):\n"
            "    p = Path(str(out)) / sample_id\n"
            "    p.mkdir(parents=True, exist_ok=True)\n"
            "    return p\n",
        )
        func = load_user_function(path)  # No trust_level specified
        # The wrapper should have the metadata showing subprocess mode.
        assert func.__name__ == "apply_parameters"
        assert getattr(func, "_byos_trust_level", None) == ByosTrustLevel.SUBPROCESS

    def test_inprocess_mode_direct_call(self, user_scripts: Path) -> None:
        """Inprocess mode: function runs directly in the caller process."""
        path = _write_script(
            user_scripts,
            "inprocess_apply.py",
            "def apply_parameters(template, parameters, sample_id, out):\n    return 42\n",
        )
        func = load_user_function(path, trust_level=ByosTrustLevel.INPROCESS)
        assert func.__name__ == "apply_parameters"
        # In-process mode returns the raw function, so we get 42 back.
        assert func(template="t", parameters={}, sample_id="s", out="o") == 42


# ===========================================================================
# _parse_subprocess_response error paths (lines 207-222)
# ===========================================================================


class TestParseSubprocessResponse:
    """Unit tests for _parse_subprocess_response private function."""

    def test_empty_stdout_raises_runtime_error(self) -> None:
        """Line 207-210: empty stdout raises RuntimeError."""
        with pytest.raises(RuntimeError, match="no output"):
            _parse_subprocess_response("", Path("script.py"), "apply_parameters")

    def test_invalid_json_raises_runtime_error(self) -> None:
        """Lines 214-215: JSONDecodeError raises RuntimeError."""
        with pytest.raises(RuntimeError, match="invalid JSON"):
            _parse_subprocess_response("not valid json {{{", Path("script.py"), "extract_kpis")

    def test_error_in_response_raises_runtime_error(self) -> None:
        """Line 218: 'error' key in response raises RuntimeError."""
        response = {"error": "something went wrong"}
        with pytest.raises(RuntimeError, match="something went wrong"):
            _parse_subprocess_response(json.dumps(response), Path("script.py"), "apply_parameters")

    def test_missing_result_key_raises_runtime_error(self) -> None:
        """Line 222: result key missing raises RuntimeError."""
        response = {"result": None}
        with pytest.raises(RuntimeError, match="did not return a result path"):
            _parse_subprocess_response(json.dumps(response), Path("script.py"), "extract_kpis")

    def test_valid_response_returns_path(self) -> None:
        """Valid JSON with result returns Path."""
        response = {"result": "/tmp/out/kpi_0001.json"}
        result = _parse_subprocess_response(json.dumps(response), Path("script.py"), "extract_kpis")
        assert result == Path("/tmp/out/kpi_0001.json")


# ===========================================================================
# In-process security warning (issue #908)
# ===========================================================================
class TestInprocessSecurityWarning:
    """Tests for the UserWarning emitted when loading scripts in-process."""

    def test_inprocess_mode_emits_security_warning(self, user_scripts: Path) -> None:
        """load_user_function(inprocess) must emit a UserWarning about the risk.

        Verifies the warning text mentions 'security risk' and references
        production deployments, so operators are alerted before running
        untrusted code in the orchestrator process (issue #908).
        """
        path = _write_script(
            user_scripts,
            "inprocess_warn.py",
            "def apply_parameters(template, parameters, sample_id, out):\n    pass\n",
        )
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            load_user_function(path, trust_level=ByosTrustLevel.INPROCESS)

        user_warnings = [w for w in caught if issubclass(w.category, UserWarning)]
        assert len(user_warnings) >= 1, "expected a UserWarning for inprocess trust level"
        message = str(user_warnings[0].message).lower()
        assert "security risk" in message
        assert "production" in message

    def test_subprocess_mode_does_not_emit_security_warning(self, user_scripts: Path) -> None:
        """subprocess mode must NOT emit the inprocess security UserWarning."""
        path = _write_script(
            user_scripts,
            "subprocess_no_warn.py",
            "def apply_parameters(template, parameters, sample_id, out):\n    pass\n",
        )
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            load_user_function(path, trust_level=ByosTrustLevel.SUBPROCESS)

        security_warnings = [
            w
            for w in caught
            if issubclass(w.category, UserWarning) and "security risk" in str(w.message).lower()
        ]
        assert security_warnings == []


# ===========================================================================
# validate_trust_level — production hardening guard (issue #908)
# ===========================================================================
class TestValidateTrustLevel:
    """Tests for the validate_trust_level BYOS hardening helper."""

    def test_rejects_inprocess_when_trusted_scripts_required(self) -> None:
        """INPROCESS + require_trusted_scripts=True raises ValueError."""
        with pytest.raises(ValueError, match="not allowed"):
            validate_trust_level(ByosTrustLevel.INPROCESS, require_trusted_scripts=True)

    def test_error_message_mentions_inprocess_and_subprocess(self) -> None:
        """The error message guides the operator to the fix."""
        with pytest.raises(ValueError) as exc_info:
            validate_trust_level(ByosTrustLevel.INPROCESS, require_trusted_scripts=True)
        message = str(exc_info.value).lower()
        assert "inprocess" in message
        assert "subprocess" in message
        assert "require-trusted-scripts" in message

    def test_allows_subprocess_when_trusted_scripts_required(self) -> None:
        """SUBPROCESS is always allowed, even with require_trusted_scripts."""
        # Must not raise.
        validate_trust_level(ByosTrustLevel.SUBPROCESS, require_trusted_scripts=True)

    def test_allows_inprocess_when_trusted_scripts_not_required(self) -> None:
        """INPROCESS is allowed when require_trusted_scripts is False (default)."""
        # Must not raise.
        validate_trust_level(ByosTrustLevel.INPROCESS, require_trusted_scripts=False)

    def test_defaults_allow_all(self) -> None:
        """Default (no hardening) allows both trust levels without error."""
        validate_trust_level(ByosTrustLevel.INPROCESS, require_trusted_scripts=False)
        validate_trust_level(ByosTrustLevel.SUBPROCESS, require_trusted_scripts=False)
