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
  * _sanitize_env: credential-leak guard (issue #1007 / #764) — malicious
    secrets dropped, whitelisted vars preserved, `KEY=default` defaults
    applied when absent, empty allowlist is deny-all

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
    _sanitize_env,
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

    def test_subprocess_timeout_configurable(self, user_scripts: Path) -> None:
        """Subprocess mode honors timeout_s and reports the configured value (#1109)."""
        path = _write_script(
            user_scripts,
            "sub_slow.py",
            "import time\n"
            "def apply_parameters(template, parameters, sample_id, out):\n"
            "    time.sleep(30)\n",
        )
        func = load_user_function(path, trust_level=ByosTrustLevel.SUBPROCESS, timeout_s=0.5)
        with pytest.raises(RuntimeError, match=r"timed out after 0\.5s"):
            func(str(user_scripts), {}, "0001", str(user_scripts / "out"))

    def test_subprocess_default_timeout_is_unbounded(self, user_scripts: Path) -> None:
        """Default timeout_s is None (effectively unbounded, issue #1534).

        The old 600 s stock default killed legitimate long simulations;
        callers who want a bound must pass ``timeout_s`` explicitly.
        """
        path = _write_script(
            user_scripts,
            "sub_fast.py",
            "from pathlib import Path\n"
            "def apply_parameters(template, parameters, sample_id, out):\n"
            "    return Path(str(out)) / sample_id\n",
        )
        func = load_user_function(path, trust_level=ByosTrustLevel.SUBPROCESS)
        wrapper_attrs = getattr(func, "_byos_timeout_s", None)
        assert wrapper_attrs is None

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
# validate_trust_level — production hardening guard (issues #908, #1207)
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

    def test_allows_inprocess_when_require_trusted_scripts_explicitly_false(self) -> None:
        """INPROCESS is allowed when require_trusted_scripts is explicitly False."""
        # Must not raise.
        validate_trust_level(ByosTrustLevel.INPROCESS, require_trusted_scripts=False)

    def test_blocks_inprocess_when_require_trusted_scripts_not_set(self) -> None:
        """INPROCESS is blocked when --require-trusted-scripts is not explicitly set.

        This is the core fix for issue #1207: without this guard, a production
        deployment that omits --require-trusted-scripts would silently allow
        inprocess BYOS mode, granting user scripts full access to the
        orchestrator's memory and credentials.
        """
        with pytest.raises(ValueError, match="not allowed by default"):
            validate_trust_level(ByosTrustLevel.INPROCESS, require_trusted_scripts=None)

    def test_error_message_for_none_mentions_byos_trust_level_and_subprocess(self) -> None:
        """The error message when require_trusted_scripts=None guides the operator."""
        with pytest.raises(ValueError) as exc_info:
            validate_trust_level(ByosTrustLevel.INPROCESS, require_trusted_scripts=None)
        message = str(exc_info.value).lower()
        assert "inprocess" in message
        assert "subprocess" in message
        assert "require-trusted-scripts" in message

    def test_defaults_block_inprocess(self) -> None:
        """Default (None) blocks INPROCESS; SUBPROCESS is always allowed."""
        validate_trust_level(ByosTrustLevel.SUBPROCESS, require_trusted_scripts=None)
        with pytest.raises(ValueError):
            validate_trust_level(ByosTrustLevel.INPROCESS, require_trusted_scripts=None)


# ===========================================================================
# _sanitize_env — credential-leak guard (issue #1007 / #764)
# ===========================================================================
class TestSanitizeEnv:
    """Direct tests for ``osimflow.byos._sanitize_env``.

    ``_sanitize_env`` is the single defence against accidental credential
    leakage from the parent orchestrator process to user-supplied BYOS
    subprocesses.  It must:

    1. Drop credential-like secrets (``AWS_*``, ``GITHUB_TOKEN``, etc.) even
       when they are set in the parent environment.
    2. Preserve whitelisted variables (``PATH``, ``HOME``, ``LANG``, the
       ``OSIMFLOW_STUB_SIM`` opt-in flag) so the child can locate its
       interpreter, locale data, and read the stub-mode switch.
    3. Honour ``KEY=default_value`` syntax in the whitelist: when ``KEY``
       is absent from the parent env the default value is injected into the
       child env.
    4. Treat an empty allowlist as deny-all: no parent-derived vars are
       forwarded, only explicit ``KEY=default`` entries (of which there are
       none in an empty list).
    """

    def test_malicious_secrets_dropped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """AWS_SECRET_ACCESS_KEY / AWS_ACCESS_KEY_ID / GITHUB_TOKEN must
        never be forwarded to the BYOS subprocess even if they are set in
        the parent process env (issue #764)."""
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "leaked_aws_secret")
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "leaked_aws_id")
        monkeypatch.setenv("GITHUB_TOKEN", "leaked_github_token")

        clean = _sanitize_env()

        assert "AWS_SECRET_ACCESS_KEY" not in clean
        assert "AWS_ACCESS_KEY_ID" not in clean
        assert "GITHUB_TOKEN" not in clean
        assert not any(k.startswith("AWS_") for k in clean), (
            f"unexpected AWS_* key leaked to child env: "
            f"{[k for k in clean if k.startswith('AWS_')]}"
        )

    def test_whitelisted_vars_preserved(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """PATH, HOME, LANG and OSIMFLOW_STUB_SIM must be forwarded when
        present in the parent env.  An OSIMFLOW_* variable that is NOT in
        the whitelist (e.g. OSIMFLOW_RANDOM) must still be filtered."""
        monkeypatch.setenv("PATH", "/sandbox/bin:/usr/bin")
        monkeypatch.setenv("HOME", "/sandbox/home")
        monkeypatch.setenv("LANG", "C.UTF-8")
        monkeypatch.setenv("OSIMFLOW_STUB_SIM", "1")
        monkeypatch.setenv("OSIMFLOW_RANDOM", "should_not_leak")

        clean = _sanitize_env()

        assert clean["PATH"] == "/sandbox/bin:/usr/bin"
        assert clean["HOME"] == "/sandbox/home"
        assert clean["LANG"] == "C.UTF-8"
        assert clean["OSIMFLOW_STUB_SIM"] == "1"
        assert "OSIMFLOW_RANDOM" not in clean

    def test_default_value_applied_when_var_absent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A whitelist entry of the form ``KEY=default_value`` must inject
        the default value when ``KEY`` is absent from the parent env.

        The production whitelist contains no ``=``-syntax entries, so this
        test exercises the branch in isolation by swapping the module-level
        whitelist for a single ``KEY=default`` entry.
        """
        monkeypatch.delenv("OSIMFLOW_MOCK_FOO", raising=False)
        monkeypatch.setattr(
            "osimflow.byos._SAFE_ENV_WHITELIST",
            ["OSIMFLOW_MOCK_FOO=mock_default"],
        )

        clean = _sanitize_env()

        assert clean == {"OSIMFLOW_MOCK_FOO": "mock_default"}

    def test_empty_allowlist_is_deny_all(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An empty allowlist drops every parent-derived var from the child
        env.  This proves the deny-all branch works in isolation.

        Plant a mix of secrets and whitelisted-looking vars so the test
        fails loudly if the allowlist is silently ignored.
        """
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "leaked")
        monkeypatch.setenv("GITHUB_TOKEN", "leaked")
        monkeypatch.setenv("PATH", "/should/not/appear")
        monkeypatch.setenv("HOME", "/should/not/appear")
        monkeypatch.setattr("osimflow.byos._SAFE_ENV_WHITELIST", [])

        clean = _sanitize_env()

        assert clean == {}
        assert "AWS_SECRET_ACCESS_KEY" not in clean
        assert "GITHUB_TOKEN" not in clean
        assert "PATH" not in clean
        assert "HOME" not in clean


# ---------------------------------------------------------------------------
# Signature validation (issue #1048)
# ---------------------------------------------------------------------------
class TestByosContractValidation:
    """Issue #1048: a BYOS user function whose signature does not match the
    documented contract must fail fast at load time with ``ByosContractError``,
    rather than silently accepting the wrong-arity function and confusing the
    orchestrator later.  These tests cover both the in-process trust path
    (direct ``inspect.signature`` validation) and the subprocess trust path
    (the inline ``_SUBPROCESS_RUNNER`` script re-validates after
    ``exec_module``).
    """

    def test_inprocess_wrong_arity_apply_parameters_raises_byos_contract_error(
        self, tmp_path: Path
    ) -> None:
        """A user-supplied ``apply_parameters`` with only 2 args must raise
        ``ByosContractError`` at load time (not silently accept)."""
        from osimflow.byos import ByosContractError, ByosTrustLevel, load_user_function

        path = _write_script(
            tmp_path,
            "wrong_arity.py",
            "def apply_parameters(template, params):  # wrong arity (2, expected 4)\n"
            "    return template\n",
        )
        with pytest.raises(ByosContractError) as excinfo:
            load_user_function(path, trust_level=ByosTrustLevel.INPROCESS)
        assert "apply_parameters" in str(excinfo.value)
        assert "expected 4 required positional" in str(excinfo.value)

    def test_inprocess_wrong_arity_extract_kpis_raises_byos_contract_error(
        self, tmp_path: Path
    ) -> None:
        """A user-supplied ``extract_kpis`` with only 1 arg must raise
        ``ByosContractError`` (extract_kpis expects 3 positional, accepts
        ``**kwargs``)."""
        from osimflow.byos import ByosContractError, ByosTrustLevel, load_user_function

        path = _write_script(
            tmp_path,
            "wrong_arity_extract.py",
            "def extract_kpis(sim_dir):  # wrong arity (1, expected 3)\n"
            "    return sim_dir / 'kpis.json'\n",
        )
        with pytest.raises(ByosContractError) as excinfo:
            load_user_function(path, trust_level=ByosTrustLevel.INPROCESS)
        assert "extract_kpis" in str(excinfo.value)
        assert "expected 3 required positional" in str(excinfo.value)

    def test_inprocess_correct_signature_loads_cleanly(self, tmp_path: Path) -> None:
        """A correctly-arity ``apply_parameters`` (4 positional) loads without
        raising."""
        from osimflow.byos import ByosTrustLevel, load_user_function

        path = _write_script(
            tmp_path,
            "good.py",
            "from pathlib import Path\n"
            "def apply_parameters(template, parameters, sample_id, out):\n"
            "    return out\n",
        )
        func = load_user_function(path, trust_level=ByosTrustLevel.INPROCESS)
        assert callable(func)

    def test_inprocess_extract_kpis_with_kwargs_loads_cleanly(self, tmp_path: Path) -> None:
        """``extract_kpis`` accepts ``**kwargs`` (for ``openstudio_version`` etc.)
        — the contract explicitly allows this."""
        from osimflow.byos import ByosTrustLevel, load_user_function

        path = _write_script(
            tmp_path,
            "good_extract.py",
            "from pathlib import Path\n"
            "def extract_kpis(simulation_dir, sample_id, out, **kwargs):\n"
            "    return out\n",
        )
        func = load_user_function(path, trust_level=ByosTrustLevel.INPROCESS)
        assert callable(func)

    def test_subprocess_wrapper_validates_via_inline_runner(self, tmp_path: Path) -> None:
        """The subprocess trust path returns a wrapper that calls
        ``_run_byos_subprocess``. The actual signature check runs inside
        the child process via the inline ``_SUBPROCESS_RUNNER`` script.

        Rather than spawn a real subprocess for this test (which would
        duplicate the subprocess-test surface in TestSubprocessIsolation),
        we directly assert that the inline runner source contains the
        ``_inline_validate`` call — this is the structural test that the
        subprocess path is wired.

        Issue #1061: this test now also verifies that the inline runner's
        embedded contract matches ``osimflow.byos_contract._BYOS_CONTRACT``.
        The contract is generated by ``tools/_generate_byos_runner.py``,
        so the test runs the generator and confirms the output file is
        up to date — the absence of drift is the actual invariant.
        """
        from osimflow import byos as byos_mod
        from osimflow import byos_contract

        runner_source = byos_mod._SUBPROCESS_RUNNER
        assert "_INLINE_BYOS_CONTRACT" in runner_source, (
            "inline runner missing _INLINE_BYOS_CONTRACT — issue #1048 "
            "subprocess validation not wired"
        )
        assert "_inline_validate" in runner_source, (
            "inline runner missing _inline_validate — issue #1048 subprocess validation not wired"
        )
        assert "_InlineByosContractError" in runner_source, (
            "inline runner missing _InlineByosContractError — issue #1048 "
            "subprocess validation not wired"
        )
        # And the inline contract is in sync with the parent process
        # contract — if the parent adds an entry, the inline copy must too.
        for entry_name, entry_spec in byos_contract._BYOS_CONTRACT.items():
            assert entry_name in runner_source, (
                f"inline runner missing contract entry for {entry_name!r}"
            )
            assert str(entry_spec.required_positional) in runner_source, (
                f"inline runner has wrong required_positional for {entry_name!r}"
            )

    def test_param_name_mismatch_emits_warning_not_error(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A function with the right arity but wrong parameter names is a soft
        warning (positional callers still work), not a hard error. The warning
        goes through ``logging.warning`` so we use ``caplog``, not
        ``pytest.warns``."""
        from osimflow.byos import ByosTrustLevel, load_user_function

        path = _write_script(
            tmp_path,
            "wrong_names.py",
            "from pathlib import Path\n"
            "def apply_parameters(a, b, c, d):  # right arity, wrong names\n"
            "    return d\n",
        )
        with caplog.at_level("WARNING", logger="osimflow.byos"):
            func = load_user_function(path, trust_level=ByosTrustLevel.INPROCESS)
        assert callable(func)
        # The validation warning appears in the captured log records.
        assert any("parameter names" in record.getMessage() for record in caplog.records), (
            f"Expected a 'parameter names' warning; got {[r.getMessage() for r in caplog.records]}"
        )

    def test_module_without_documented_function_does_not_invoke_validation(
        self, tmp_path: Path
    ) -> None:
        """A user script that defines no matching callable still raises the
        legacy ``AttributeError`` (from ``_CANDIDATE_NAMES``); it does not
        trip the new contract validator."""
        from osimflow.byos import ByosTrustLevel, load_user_function

        path = _write_script(
            tmp_path,
            "no_match.py",
            "def completely_unrelated_function(x, y, z):\n    return x\n",
        )
        with pytest.raises(AttributeError):
            load_user_function(path, trust_level=ByosTrustLevel.INPROCESS)


# ---------------------------------------------------------------------------
# Single source of truth (issue #1061)
# ---------------------------------------------------------------------------
class TestByosContractSingleSource:
    """Issue #1061: ``osimflow.byos._BYOS_CONTRACT`` and the inline subprocess
    runner previously held a literal copy of the same contract table.  These
    tests lock the new invariant — one source of truth, one generated output.
    """

    def test_byos_contract_is_byos_contract_attribute(self) -> None:
        """``osimflow.byos._BYOS_CONTRACT`` is the same object as
        ``osimflow.byos_contract._BYOS_CONTRACT``.  This is the structural
        proof that there is a single source of truth at the parent level."""
        from osimflow import byos as byos_mod
        from osimflow import byos_contract

        assert byos_mod._BYOS_CONTRACT is byos_contract._BYOS_CONTRACT

    def test_subprocess_runner_imports_from_generated_module(self) -> None:
        """``osimflow.byos._SUBPROCESS_RUNNER`` is the same string as the one
        exported by the generated module — no duplication at the parent level."""
        from osimflow import _byos_runner_generated as gen_mod
        from osimflow import byos as byos_mod

        assert byos_mod._SUBPROCESS_RUNNER is gen_mod._SUBPROCESS_RUNNER

    def test_generated_runner_is_up_to_date(self) -> None:
        """Re-running ``tools/_generate_byos_runner.py`` must produce no diff
        in ``osimflow/_byos_runner_generated.py``.  This is the drift
        detector: if a contributor adds an entry to ``byos_contract``
        without re-running the generator, this test fails."""
        import subprocess
        import sys
        from pathlib import Path

        repo_root = Path(__file__).resolve().parents[2]
        result = subprocess.run(
            [sys.executable, "tools/_generate_byos_runner.py"],
            capture_output=True,
            text=True,
            cwd=str(repo_root),
        )
        assert result.returncode == 0, (
            f"generated runner is out of date (re-run `make contract`).\n"
            f"stdout={result.stdout}\nstderr={result.stderr}"
        )
        assert "up to date" in result.stdout

    def test_inline_contract_matches_parent_contract(self) -> None:
        """The inline ``_INLINE_BYOS_CONTRACT`` dict inside the subprocess
        runner is structurally equal to the parent
        ``byos_contract._BYOS_CONTRACT``.  Because both are derived from
        the same source-of-truth at generation time, they must agree on
        every (name, required_positional, param_names, accepts_kwargs)
        tuple."""
        import ast

        from osimflow import byos as byos_mod
        from osimflow import byos_contract

        runner_source = byos_mod._SUBPROCESS_RUNNER
        # Find the inline contract assignment in the runner source.
        marker = "_INLINE_BYOS_CONTRACT = "
        start = runner_source.index(marker) + len(marker)
        # Extract until the next blank line followed by a non-continuation
        # character — Python literal continues until a blank line.
        end = runner_source.index("\n\n", start)
        literal = runner_source[start:end]
        inline_contract: dict[str, dict[str, object]] = ast.literal_eval(literal)

        assert set(inline_contract.keys()) == set(byos_contract._BYOS_CONTRACT.keys()), (
            f"inline contract entries {set(inline_contract.keys())} "
            f"do not match parent contract entries "
            f"{set(byos_contract._BYOS_CONTRACT.keys())}"
        )
        for name, parent_entry in byos_contract._BYOS_CONTRACT.items():
            inline_entry = inline_contract[name]
            assert inline_entry["required_positional"] == parent_entry.required_positional
            assert tuple(inline_entry["param_names"]) == parent_entry.param_names
            assert inline_entry["accepts_kwargs"] == parent_entry.accepts_kwargs

    def test_generated_module_docstring_explains_source(self) -> None:
        """The generated module docstring points operators at the source of
        truth and the regeneration command, so a future contributor who
        wants to add a BYOS entry finds the instructions in plain text."""
        from osimflow import _byos_runner_generated as gen_mod

        doc = gen_mod.__doc__ or ""
        assert "do not edit by hand" in doc.lower()
        assert "byos_contract" in doc
        assert "regenerate" in doc.lower() or "_generate_byos_runner" in doc

    def test_contract_entries_have_required_positional_int(self) -> None:
        """Every contract entry's ``required_positional`` is a positive
        int.  This protects against accidental typos (e.g. ``"4"``) that
        would compare unequal to the integer the validator counts."""
        from osimflow import byos_contract

        for name, entry in byos_contract._BYOS_CONTRACT.items():
            assert isinstance(entry.required_positional, int), (
                f"{name}.required_positional must be int, got {type(entry.required_positional).__name__}"
            )
            assert entry.required_positional > 0, (
                f"{name}.required_positional must be positive, got {entry.required_positional}"
            )
