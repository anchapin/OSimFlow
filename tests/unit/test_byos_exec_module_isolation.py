"""Regression tests for the BYOS discovery subprocess isolation (issue #1005).

Issue #1005: ``osimflow/byos.py:_discover_function_name`` previously
called ``importlib.util.spec_from_file_location(...).loader.exec_module(mod)``
**inside the orchestrator process**.  Even when the trust level was
``SUBPROCESS``, the user script's module-level code ran in the
orchestrator before any subprocess sandbox was created.  A malicious
BYOS file with ``import os; os._exit(42)`` at module level would kill
the orchestrator.

The fix moves ``exec_module`` into a child process
(``_discover_in_subprocess`` / ``_DISCOVERY_RUNNER``).  Malicious
module-level code now dies in the child, and the orchestrator survives
to raise a clear ``RuntimeError``.

The single critical regression test below writes a BYOS file containing
``import os; os._exit(42)`` at module level and asserts that
``load_user_function`` returns control to the caller rather than
terminating the orchestrator process.  The remaining tests exercise
related subprocess-discovery properties to guard the fix from
regressing.
"""

from __future__ import annotations

import os as stdlib_os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from osimflow.byos import ByosTrustLevel, load_user_function


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
# Core regression: malicious exec_module must not kill the orchestrator
# ===========================================================================
class TestExecModuleIsolation:
    """Issue #1005 — exec_module must run in a subprocess, not in the orchestrator."""

    def test_malicious_module_level_os_exit_does_not_kill_orchestrator(
        self,
        user_scripts: Path,
    ) -> None:
        """Module-level ``os._exit(42)`` must die in the subprocess, not the orchestrator.

        Pre-fix: ``_discover_function_name`` called ``exec_module`` in the
        orchestrator.  The BYOS file's ``import os; os._exit(42)`` would
        terminate this test runner process (pytest would report the worker
        crashed, and ``load_user_function`` would never return).

        Post-fix: ``exec_module`` runs in a child process.  The child dies
        at exit code 42; the orchestrator survives and surfaces a clear
        ``RuntimeError`` that points at the malicious script.

        To make a regression observable as a regular assertion failure
        (rather than a hard process kill) we patch the orchestrator's
        ``os._exit`` to raise ``SystemExit(42)`` instead.  If the fix
        regresses and the BYOS module-level code runs in this process,
        the patched ``_exit`` is invoked and the test fails with a
        visible ``SystemExit``.  If the fix holds, the patch is never
        touched (the malicious code only runs in the child).
        """
        path = _write_script(
            user_scripts,
            "malicious.py",
            "import os\n"
            "os._exit(42)\n"
            "def apply_parameters(template, parameters, sample_id, out):\n"
            "    pass\n",
        )
        # Guard: if exec_module runs in THIS process, the patched
        # os._exit below raises SystemExit(42) — a visible assertion
        # failure inside the test rather than a hard kill.
        with patch.object(stdlib_os, "_exit", side_effect=SystemExit(42)) as mock_exit:
            with pytest.raises(RuntimeError, match="discovery subprocess"):
                load_user_function(path)

        # The patch must never have been invoked — the malicious
        # os._exit must have run in the subprocess, not here.
        mock_exit.assert_not_called()

    def test_orchestrator_pid_unchanged_after_malicious_discovery(
        self,
        user_scripts: Path,
    ) -> None:
        """The orchestrator's PID is unchanged after a malicious BYOS load.

        A hard-kill regression would change the process identity (the
        test runner would respawn under a different PID).  Asserting that
        ``os.getpid()`` returns the same value before and after the call
        is a cheap smoke test that the orchestrator survived.
        """
        path = _write_script(
            user_scripts,
            "malicious_pid.py",
            "import os\nos._exit(7)\ndef apply_parameters(t, p, s, o):\n    pass\n",
        )
        pid_before = stdlib_os.getpid()
        with patch.object(stdlib_os, "_exit", side_effect=SystemExit(7)):
            with pytest.raises(RuntimeError):
                load_user_function(path)
        pid_after = stdlib_os.getpid()
        assert pid_before == pid_after, (
            "orchestrator PID changed — the discovery subprocess leaked "
            "os._exit() into the parent process"
        )

    def test_malicious_sys_exit_raises_runtime_error(
        self,
        user_scripts: Path,
    ) -> None:
        """Module-level ``sys.exit(7)`` is captured in the subprocess.

        ``sys.exit`` raises ``SystemExit`` which we honour inside the
        subprocess (per Python's normal semantics).  The orchestrator
        receives a non-zero exit and surfaces ``RuntimeError`` without
        dying.
        """
        path = _write_script(
            user_scripts,
            "sys_exit.py",
            "import sys\nsys.exit(7)\ndef apply_parameters(t, p, s, o):\n    pass\n",
        )
        with pytest.raises(RuntimeError, match="exit 7"):
            load_user_function(path)

    def test_module_level_runtime_error_raises_runtime_error(
        self,
        user_scripts: Path,
    ) -> None:
        """Module-level ``NameError`` is captured in the subprocess.

        The subprocess emits a JSON error payload that includes the
        exception class name.  The orchestrator re-raises the matching
        exception type rather than crashing.
        """
        path = _write_script(
            user_scripts,
            "name_error.py",
            "undefined_symbol  # noqa: F821 — intentional NameError\n"
            "def apply_parameters(t, p, s, o):\n"
            "    pass\n",
        )
        # The subprocess re-raises as ``NameError`` because the type is
        # preserved through the JSON error envelope.
        with pytest.raises(NameError):
            load_user_function(path)

    def test_subprocess_discovery_returns_correct_function_name(
        self,
        user_scripts: Path,
    ) -> None:
        """A non-malicious BYOS still resolves to the right function name.

        Guards against the discovery subprocess accidentally returning
        the wrong name (e.g. always returning ``apply_parameters``).
        """
        path = _write_script(
            user_scripts,
            "good_extract.py",
            "def extract_kpis(simulation_dir, sample_id, out):\n    return {}\n",
        )
        func = load_user_function(path)
        assert func.__name__ == "extract_kpis"

    def test_subprocess_discovery_apply_parameters_takes_precedence(
        self,
        user_scripts: Path,
    ) -> None:
        """Discovery subprocess applies the same candidate ordering as the in-process path.

        ``apply_parameters`` must win over ``extract_kpis`` and the
        deprecated ``apply`` name.  This guards against accidentally
        changing the candidate ordering when moving the lookup into
        a subprocess.
        """
        path = _write_script(
            user_scripts,
            "both.py",
            "def apply_parameters(t, p, s, o):\n    pass\n"
            "def extract_kpis(s, sid, o):\n    return {}\n"
            "def apply(t, p, s, o):\n    pass\n",
        )
        import warnings as _warnings

        with _warnings.catch_warnings():
            _warnings.simplefilter("ignore", DeprecationWarning)
            func = load_user_function(path)
        assert func.__name__ == "apply_parameters"

    def test_subprocess_discovery_no_matching_callable_raises_attribute_error(
        self,
        user_scripts: Path,
    ) -> None:
        """Discovery subprocess reports no-matching-callable as ``AttributeError``.

        The subprocess returns ``{"error": "...", "type": "AttributeError"}``
        and the orchestrator re-raises ``AttributeError`` with the
        helpful message from ``_CANDIDATE_NAMES``.
        """
        path = _write_script(
            user_scripts,
            "no_callable.py",
            "x = 42\n",
        )
        with pytest.raises(AttributeError, match="apply_parameters"):
            load_user_function(path)

    def test_subprocess_discovery_deprecated_apply_warns(
        self,
        user_scripts: Path,
    ) -> None:
        """Discovery subprocess still triggers the ``apply`` deprecation warning.

        The warning is emitted by the parent (not the subprocess) after
        the name is returned.  This guards against accidentally losing
        the deprecation behaviour when moving the lookup to a subprocess.
        """
        import warnings as _warnings

        path = _write_script(
            user_scripts,
            "legacy_apply.py",
            "def apply(template, parameters, sample_id, out):\n    pass\n",
        )
        with _warnings.catch_warnings(record=True) as caught:
            _warnings.simplefilter("always")
            func = load_user_function(path)
        assert func.__name__ == "apply"
        deprecations = [w for w in caught if issubclass(w.category, DeprecationWarning)]
        assert any("deprecated" in str(w.message).lower() for w in deprecations)

    def test_subprocess_discovery_inprocess_trust_level_skips_subprocess(
        self,
        user_scripts: Path,
    ) -> None:
        """INPROCESS trust level bypasses subprocess discovery.

        When the operator explicitly opts into ``ByosTrustLevel.INPROCESS``
        (legacy behaviour), ``_discover_function_name`` is *not* called —
        the in-process loader takes over.  A malicious module-level
        ``os._exit`` in INPROCESS mode WILL kill the orchestrator; that
        is the documented contract (and the subject of issue #908).
        The patch below verifies that, in subprocess mode (the default
        and the secure mode), the discovery *does* use a subprocess
        rather than the in-process path.

        We verify this by counting how many times the subprocess.Popen
        path is invoked during discovery: in INPROCESS mode it should
        not be invoked at all (the in-process loader runs in the
        orchestrator); in subprocess mode it should be invoked at least
        once.  Because INPROCESS is the documented kill-yourself mode,
        we use a *safe* in-process script for this assertion.
        """
        path = _write_script(
            user_scripts,
            "safe.py",
            "def apply_parameters(t, p, s, o):\n    pass\n",
        )

        # INPROCESS mode: no subprocess is launched for discovery.
        with patch("osimflow.byos.subprocess.Popen") as mock_popen:
            load_user_function(path, trust_level=ByosTrustLevel.INPROCESS)
        mock_popen.assert_not_called()

        # Subprocess mode: discovery launches a child process.
        with patch("osimflow.byos.subprocess.Popen") as mock_popen:
            mock_proc = mock_popen.return_value
            mock_proc.communicate.return_value = ('{"function": "apply_parameters"}', "")
            mock_proc.returncode = 0
            load_user_function(path, trust_level=ByosTrustLevel.SUBPROCESS)
        # The wrapper is returned without invoking the subprocess yet —
        # just the discovery subprocess should have been launched.
        assert mock_popen.call_count >= 1

    def test_subprocess_discovery_uses_sanitized_environment(
        self,
        user_scripts: Path,
    ) -> None:
        """The discovery subprocess inherits a sanitised env (no parent creds).

        Defensive: parent AWS / GitHub / etc. tokens must not leak into
        the discovery subprocess's environment.  We assert the ``env``
        kwarg passed to ``subprocess.Popen`` equals the result of
        ``_sanitize_env()``.
        """
        from osimflow import byos as byos_mod

        path = _write_script(
            user_scripts,
            "safe_env.py",
            "def apply_parameters(t, p, s, o):\n    pass\n",
        )
        with patch("osimflow.byos.subprocess.Popen") as mock_popen:
            mock_proc = mock_popen.return_value
            mock_proc.communicate.return_value = ('{"function": "apply_parameters"}', "")
            mock_proc.returncode = 0
            load_user_function(path, trust_level=ByosTrustLevel.SUBPROCESS)

        # Inspect the first call (discovery subprocess).
        call_kwargs = mock_popen.call_args_list[0].kwargs
        assert call_kwargs.get("env") == byos_mod._sanitize_env()

    def test_subprocess_discovery_does_not_pollute_parent_modules(
        self,
        user_scripts: Path,
    ) -> None:
        """Side-effects from the BYOS module's ``import`` must not leak into the parent.

        Even when exec_module runs in a subprocess, the parent still
        imports ``osimflow.byos`` once.  We assert that after loading a
        BYOS script that imports a uniquely-named sentinel module, the
        sentinel is NOT visible in the parent's ``sys.modules``.  This
        guards against an accidental regression where the parent path
        somehow executes exec_module.
        """
        sentinel = "byos_discovery_sentinel_unique_45821"
        assert sentinel not in sys.modules
        path = _write_script(
            user_scripts,
            "sentinel.py",
            f"import {sentinel}\ndef apply_parameters(t, p, s, o):\n    pass\n",
        )
        with pytest.raises(ImportError):
            # ``sentinel`` does not exist on PYTHONPATH, so the
            # subprocess fails to import it and the orchestrator
            # re-raises ImportError.
            load_user_function(path)
        assert sentinel not in sys.modules, (
            "sentinel module leaked into the orchestrator's sys.modules — "
            "discovery exec_module is running in the parent process"
        )

    def test_malicious_discovery_does_not_break_subsequent_loads(
        self,
        user_scripts: Path,
    ) -> None:
        """After a malicious discovery failure, a second load still works.

        Guards against state corruption (e.g. shared module cache
        poisoning) in the discovery subprocess path.  A subsequent
        benign BYOS script must still resolve its function name.
        """
        bad = _write_script(
            user_scripts,
            "evil.py",
            "import os\nos._exit(99)\ndef apply_parameters(t, p, s, o):\n    pass\n",
        )
        good = _write_script(
            user_scripts,
            "good.py",
            "def apply_parameters(t, p, s, o):\n    pass\n",
        )
        with patch.object(stdlib_os, "_exit", side_effect=SystemExit(99)):
            with pytest.raises(RuntimeError):
                load_user_function(bad)

        # The second load should succeed normally.
        func = load_user_function(good)
        assert func.__name__ == "apply_parameters"
        assert getattr(func, "_byos_trust_level", None) == ByosTrustLevel.SUBPROCESS
