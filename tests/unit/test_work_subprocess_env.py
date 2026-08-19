"""Regression test for issue #1027 — work.py subprocess env sanitization.

The five subprocess call sites in ``osimflow/work.py`` must sanitize the
environment before invoking a child process, so the orchestrator's
``AWS_*`` / ``GITHUB_TOKEN`` / ``REDIS_URL`` secrets cannot leak to
``openstudio.cli`` or the bundled ``bin/*.py`` work scripts.

Coverage:

* :class:`TestSanitizeEnv` — direct unit tests of the ``_sanitize_env``
  allowlist (drop secrets, keep whitelist).
* :class:`TestSubprocessSitesPassSanitizedEnv` — every one of the five
  call sites identified in issue #1027 passes ``env=_sanitize_env()``
  to its ``subprocess.run`` / ``run_subprocess`` invocation.
* :class:`TestRealSubprocessPropagation` — a real subprocess spawned
  with ``env=work._sanitize_env()`` does NOT see
  ``AWS_SECRET_ACCESS_KEY`` in its environment (probed via
  ``/proc/self/environ`` on Linux, via a probe script on other
  platforms).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from osimflow import work as work_mod
from osimflow.work import _sanitize_env


# ===========================================================================
# Direct unit tests of osimflow.work._sanitize_env
# ===========================================================================
class TestSanitizeEnv:
    """Direct unit tests for ``osimflow.work._sanitize_env`` (issue #1027).

    ``_sanitize_env`` is the single defence against accidental credential
    leakage from the parent orchestrator process to the children spawned
    by ``osimflow/work.py`` (i.e. ``openstudio.cli run -w ...`` and the
    bundled ``bin/*.py`` work scripts).  It must:

    1. Drop credential-like secrets (``AWS_*``, ``GITHUB_TOKEN``,
       ``REDIS_URL``) even when they are set in the parent env.
    2. Preserve whitelisted variables (``PATH``, ``HOME``, ``LANG``,
       ``LC_ALL``, ``TMPDIR``) so the child can locate binaries, locale
       data, and the temp directory.
    3. Forward ``OSIMFLOW_*`` framework flags and ``PYTHON*``
       interpreter variables (covered by the prefix-match allowlist).
    4. Reject everything else, even other framework-internal vars that
       are not in the allowlist.
    """

    def test_aws_credentials_dropped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """AWS_* credentials in the parent env must not leak to the child.

        Both secret-like (``AWS_SECRET_ACCESS_KEY``,
        ``AWS_ACCESS_KEY_ID``, ``AWS_SESSION_TOKEN``) and non-secret
        (``AWS_REGION``) AWS_* vars are dropped — a leaking region can
        still help an attacker reason about the deployment.
        """
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "leaked_aws_secret")
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "leaked_aws_id")
        monkeypatch.setenv("AWS_SESSION_TOKEN", "leaked_aws_session")
        monkeypatch.setenv("AWS_REGION", "us-west-2")

        clean = _sanitize_env()

        assert "AWS_SECRET_ACCESS_KEY" not in clean
        assert "AWS_ACCESS_KEY_ID" not in clean
        assert "AWS_SESSION_TOKEN" not in clean
        assert "AWS_REGION" not in clean
        assert not any(k.startswith("AWS_") for k in clean), (
            f"unexpected AWS_* key leaked to child env: "
            f"{[k for k in clean if k.startswith('AWS_')]}"
        )

    def test_other_secrets_dropped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Other secret-like env vars (GH_TOKEN, REDIS_URL) must also be dropped."""
        monkeypatch.setenv("GITHUB_TOKEN", "leaked_github")
        monkeypatch.setenv("GH_TOKEN", "leaked_gh")
        monkeypatch.setenv("REDIS_URL", "redis://user:pass@host:6379")
        monkeypatch.setenv("SOME_RANDOM_SECRET", "leaked")

        clean = _sanitize_env()

        assert "GITHUB_TOKEN" not in clean
        assert "GH_TOKEN" not in clean
        assert "REDIS_URL" not in clean
        assert "SOME_RANDOM_SECRET" not in clean

    def test_whitelisted_exact_match_preserved(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """PATH / HOME / LANG / LC_ALL / TMPDIR / TEMP / TMP must be forwarded."""
        monkeypatch.setenv("PATH", "/sandbox/bin:/usr/bin")
        monkeypatch.setenv("HOME", "/sandbox/home")
        monkeypatch.setenv("LANG", "C.UTF-8")
        monkeypatch.setenv("LC_ALL", "C.UTF-8")
        monkeypatch.setenv("TMPDIR", "/tmp")
        monkeypatch.setenv("TEMP", "/tmp")
        monkeypatch.setenv("TMP", "/tmp")

        clean = _sanitize_env()

        assert clean["PATH"] == "/sandbox/bin:/usr/bin"
        assert clean["HOME"] == "/sandbox/home"
        assert clean["LANG"] == "C.UTF-8"
        assert clean["LC_ALL"] == "C.UTF-8"
        assert clean["TMPDIR"] == "/tmp"
        assert clean["TEMP"] == "/tmp"
        assert clean["TMP"] == "/tmp"

    def test_osimlow_wildcard_preserved(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """OSIMFLOW_* framework vars must be forwarded (prefix match)."""
        monkeypatch.setenv("OSIMFLOW_STUB_SIM", "1")
        monkeypatch.setenv("OSIMFLOW_RANDOM_FLAG", "value")

        clean = _sanitize_env()

        assert clean["OSIMFLOW_STUB_SIM"] == "1"
        assert clean["OSIMFLOW_RANDOM_FLAG"] == "value"

    def test_python_wildcard_preserved(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """PYTHON* interpreter variables must be forwarded (prefix match)."""
        monkeypatch.setenv("PYTHONPATH", "/sandbox/py")
        monkeypatch.setenv("PYTHONHOME", "/sandbox/pyhome")
        monkeypatch.setenv("PYTHONIOENCODING", "utf-8")
        monkeypatch.setenv("PYTHONUNBUFFERED", "1")
        monkeypatch.setenv("PYTHON_FROZEN_GARBAGE", "ignore_me")

        clean = _sanitize_env()

        assert clean["PYTHONPATH"] == "/sandbox/py"
        assert clean["PYTHONHOME"] == "/sandbox/pyhome"
        assert clean["PYTHONIOENCODING"] == "utf-8"
        assert clean["PYTHONUNBUFFERED"] == "1"
        # The PYTHON* prefix is intentionally liberal: any var starting
        # with PYTHON is forwarded.  This documents the current
        # behaviour so a future tightening is a conscious decision.
        assert clean["PYTHON_FROZEN_GARBAGE"] == "ignore_me"

    def test_unknown_vars_dropped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Vars outside the allowlist must be dropped even if they look benign."""
        monkeypatch.setenv("SOMETHING_RANDOM", "drop_me")
        monkeypatch.setenv("UNRELATED_CONFIG_VAR", "drop_me_too")

        clean = _sanitize_env()

        assert "SOMETHING_RANDOM" not in clean
        assert "UNRELATED_CONFIG_VAR" not in clean

    def test_empty_parent_env_yields_empty_clean_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """With no parent vars, _sanitize_env() returns an empty dict (deny-all)."""
        # Wipe the relevant prefixes; the allowlist is exact + prefix
        # match, so an empty parent env means an empty sanitized env.
        for key in list(os.environ):
            if (
                key in {"PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "TEMP", "TMP"}
                or key.startswith("OSIMFLOW_")
                or key.startswith("PYTHON")
            ):
                monkeypatch.delenv(key, raising=False)

        assert _sanitize_env() == {}


# ===========================================================================
# Per-site regression: every callsite identified in issue #1027 must
# pass env=_sanitize_env() to its subprocess helper.
# ===========================================================================
class TestSubprocessSitesPassSanitizedEnv:
    """Every subprocess call site in ``osimflow/work.py`` must pass
    ``env=_sanitize_env()`` (issue #1027).

    The five sites are:

    1. ``_apply_parameters_stub``        — ``subprocess.run``
    2. ``run_openstudio_sim`` stub branch — ``run_subprocess``
    3. ``_run_real_openstudio``          — ``run_subprocess``
    4. ``generate_lhs``                  — ``subprocess.run``
    5. ``_extract_kpis_impl``            — ``subprocess.run``

    These tests mock the underlying ``subprocess.run`` / ``run_subprocess``
    helper and assert the ``env=`` kwarg equals ``_sanitize_env()``.
    They are platform-independent and fast.
    """

    @pytest.fixture(autouse=True)
    def _plant_secrets(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Plant fake secrets in the parent env so _sanitize_env() has
        something to drop.  This proves the parent had them and the
        child env did not."""
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "leaked_secret_value")
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIA_LEAKED")
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_leaked")
        monkeypatch.setenv("REDIS_URL", "redis://user:pass@host:6379")

    def _assert_sanitized(self, mock_subprocess: object) -> dict[str, str]:
        """Helper: pull the ``env=`` kwarg off a mock call and return it.

        Also asserts the env is the result of ``_sanitize_env()`` and
        does NOT contain any of the planted secrets.
        """
        assert mock_subprocess.called  # type: ignore[attr-defined]
        call_kwargs = mock_subprocess.call_args.kwargs  # type: ignore[attr-defined]
        env = call_kwargs.get("env")
        assert env is not None, f"subprocess helper was called without env= (kwargs: {call_kwargs})"
        assert env == work_mod._sanitize_env(), (
            f"env passed to subprocess helper does not match _sanitize_env():\n"
            f"  expected: {sorted(work_mod._sanitize_env().items())}\n"
            f"  got:      {sorted(env.items())}"
        )
        # Defensive: even if the equality check above is loosened in
        # the future, the secrets must still be absent.
        for secret in (
            "AWS_SECRET_ACCESS_KEY",
            "AWS_ACCESS_KEY_ID",
            "GITHUB_TOKEN",
            "REDIS_URL",
        ):
            assert secret not in env, f"{secret} leaked to subprocess env: {env.get(secret)!r}"
            assert not any(k.startswith("AWS_") for k in env), (
                f"AWS_* key leaked to subprocess env: {[k for k in env if k.startswith('AWS_')]}"
            )
        return env

    # ---------------------------------------------------------------------
    # Site 1: _apply_parameters_stub
    # ---------------------------------------------------------------------
    def test_apply_parameters_stub_passes_sanitized_env(self, tmp_path: Path) -> None:
        """_apply_parameters_stub must pass env=_sanitize_env()."""
        template = tmp_path / "template"
        template.mkdir()
        (template / "model.osm").write_text("{}")
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        param_file = tmp_path / "params.json"
        param_file.write_text("{}")

        with patch("osimflow.work.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            work_mod._apply_parameters_stub(template, "0001", out_dir, param_file)

        self._assert_sanitized(mock_run)

    # ---------------------------------------------------------------------
    # Site 2: run_openstudio_sim stub branch (line ~855)
    # ---------------------------------------------------------------------
    def test_run_openstudio_sim_stub_branch_passes_sanitized_env(self, tmp_path: Path) -> None:
        """The stub branch of ``run_openstudio_sim`` must pass env=_sanitize_env().

        We force the stub branch by setting ``OSIMFLOW_STUB_SIM=1`` and
        making the openstudio CLI look unavailable.
        """
        stdout_path = tmp_path / "stdout.log"
        stderr_path = tmp_path / "stderr.log"
        sim_out = tmp_path / "sim"
        sim_out.mkdir()
        (sim_out / "model.osm").write_text("{}")
        (sim_out / "workflow.osw").write_text(json.dumps({"name": "t"}))

        with (
            patch.dict(os.environ, {"OSIMFLOW_STUB_SIM": "1"}),
            patch("osimflow.work._is_openstudio_available", return_value=False),
            patch("osimflow.work.run_subprocess") as mock_run_subprocess,
        ):
            mock_run_subprocess.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            work_mod.run_openstudio_sim(
                modified_sim_package=sim_out,
                sample_id="0001",
                openstudio_version="3.11.0",
                out=sim_out,
                simulate_work_s=0.0,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
            )

        self._assert_sanitized(mock_run_subprocess)

    # ---------------------------------------------------------------------
    # Site 3: _run_real_openstudio
    # ---------------------------------------------------------------------
    def test_run_real_openstudio_passes_sanitized_env(self, tmp_path: Path) -> None:
        """_run_real_openstudio must pass env=_sanitize_env() to run_subprocess."""
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        (pkg / "workflow.osw").write_text(json.dumps({"name": "t"}))
        stdout_path = tmp_path / "stdout.log"
        stderr_path = tmp_path / "stderr.log"

        with patch("osimflow.work.run_subprocess") as mock_run_subprocess:
            mock_run_subprocess.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            work_mod._run_real_openstudio(
                modified_sim_package=pkg,
                sample_id="0001",
                sim_out=pkg,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
            )

        self._assert_sanitized(mock_run_subprocess)

    # ---------------------------------------------------------------------
    # Site 4: generate_lhs
    # ---------------------------------------------------------------------
    def test_generate_lhs_passes_sanitized_env(self, tmp_path: Path) -> None:
        """generate_lhs must pass env=_sanitize_env() to subprocess.run."""
        variables_yml = tmp_path / "variables.yml"
        variables_yml.write_text("variables: []\n")
        out = tmp_path / "out"
        out.mkdir()

        with patch("osimflow.work.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            work_mod.generate_lhs(variables_yml, 5, out)

        self._assert_sanitized(mock_run)

    # ---------------------------------------------------------------------
    # Site 5: _extract_kpis_impl
    # ---------------------------------------------------------------------
    def test_extract_kpis_impl_passes_sanitized_env(self, tmp_path: Path) -> None:
        """_extract_kpis_impl must pass env=_sanitize_env() to subprocess.run."""
        sim_dir = tmp_path / "sim"
        sim_dir.mkdir()
        out = tmp_path / "out"
        out.mkdir()

        with patch("osimflow.work.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            work_mod._extract_kpis_impl(sim_dir, "0001", out)

        self._assert_sanitized(mock_run)


# ===========================================================================
# End-to-end: a REAL subprocess spawned with env=_sanitize_env() must
# not see AWS_SECRET_ACCESS_KEY in its environment.  This is the
# integration check the issue asked for.
# ===========================================================================
# A small probe script: dump the requested env var presence and the
# child PID, in JSON, on stdout.  On Linux the probe also reads
# /proc/self/environ as a belt-and-braces verification that the env
# delivered by the OS matches the env we passed to subprocess.run.
#
# We use ``\n`` rather than ``;`` because the probe contains a
# ``with`` block, a ``for`` loop, and an ``if/else`` that cannot
# be expressed as one-line statements on Python 3.12.
_PROBE_SCRIPT = """\
import json, os

keys = ['AWS_SECRET_ACCESS_KEY', 'AWS_ACCESS_KEY_ID',
        'GITHUB_TOKEN', 'REDIS_URL', 'SOME_RANDOM_SECRET']

proc_environ = None
try:
    with open('/proc/self/environ', 'rb') as f:
        raw = f.read().split(b'\\x00')
    pairs = []
    for entry in raw:
        if b'=' in entry:
            k, _, v = entry.partition(b'=')
            pairs.append(
                (k.decode('utf-8', 'replace'), v.decode('utf-8', 'replace'))
            )
    proc_environ = dict(pairs)
except OSError:
    pass

present = [k for k in keys if k in os.environ]
if proc_environ is not None:
    proc_present = [k for k in keys if k in proc_environ]
else:
    proc_present = None

print(json.dumps({
    'pid': os.getpid(),
    'present': present,
    'proc_present': proc_present,
}))
"""


def _run_probe(env: dict[str, str] | None) -> dict[str, object]:
    """Spawn a fresh interpreter that runs the probe script with the given env.

    Returns the parsed JSON dict the probe emitted.  ``env=None`` means
    "inherit parent env" — used by the negative-control test to prove
    the test setup can actually see the planted secret.
    """
    result = subprocess.run(  # noqa: S603 — test-controlled argv
        [sys.executable, "-c", _PROBE_SCRIPT],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    return json.loads(result.stdout)  # type: ignore[no-any-return]


@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="issue #1027 regression: AWS_SECRET_ACCESS_KEY leak test (uses /proc)",
)
class TestRealSubprocessPropagation:
    """A real subprocess spawned with ``env=work._sanitize_env()`` does
    NOT see the parent's ``AWS_SECRET_ACCESS_KEY``.

    The probe script reads both ``os.environ`` (the Python-level view
    of the env) and ``/proc/self/environ`` (the OS-level view) and
    reports which of the planted secrets are present.  The two views
    should agree on Linux.
    """

    def test_negative_control_secret_visible_without_sanitize(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Sanity check: with NO sanitization, the secret IS in the child env.

        This proves the test setup is real: if this test fails, the
        positive test below cannot be trusted.
        """
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test_leaked_value")

        # Inherit the parent env (no env= kwarg) so the child can see
        # the planted secret.  This mimics the pre-#1027 behaviour
        # where secrets leaked.
        probe = _run_probe(env=None)
        assert "AWS_SECRET_ACCESS_KEY" in probe["present"], (
            f"test setup broken: child did not see planted secret. probe output: {probe}"
        )

    def test_aws_secret_absent_with_sanitize(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Set ``AWS_SECRET_ACCESS_KEY=test`` in the parent env, spawn
        a child with ``env=work._sanitize_env()``, assert the secret
        is NOT in the child's environment (issue #1027).

        Reads ``/proc/self/environ`` as well as ``os.environ`` to catch
        the case where the env block was silently enlarged between
        Python and the OS.
        """
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test_leaked_value")
        monkeypatch.setenv("GITHUB_TOKEN", "test_github")
        monkeypatch.setenv("REDIS_URL", "redis://u:p@host:6379")

        clean = _sanitize_env()
        # Sanity: the secret is in the parent env but the sanitized
        # child env must drop it.
        assert "AWS_SECRET_ACCESS_KEY" in os.environ
        assert "AWS_SECRET_ACCESS_KEY" not in clean

        probe = _run_probe(env=clean)

        # The Python-level view of the child env must not have the secret.
        assert "AWS_SECRET_ACCESS_KEY" not in probe["present"], (
            f"AWS_SECRET_ACCESS_KEY leaked to child env (pid={probe['pid']}): {probe}"
        )
        assert "GITHUB_TOKEN" not in probe["present"], (
            f"GITHUB_TOKEN leaked to child env (pid={probe['pid']}): {probe}"
        )
        assert "REDIS_URL" not in probe["present"], (
            f"REDIS_URL leaked to child env (pid={probe['pid']}): {probe}"
        )

        # The OS-level view (/proc/self/environ) must also agree.
        proc_present = probe.get("proc_present")
        if proc_present is not None:
            assert "AWS_SECRET_ACCESS_KEY" not in proc_present, (
                f"AWS_SECRET_ACCESS_KEY present in /proc/self/environ (pid={probe['pid']}): {probe}"
            )
            assert "GITHUB_TOKEN" not in proc_present, (
                f"GITHUB_TOKEN present in /proc/self/environ (pid={probe['pid']}): {probe}"
            )
            assert "REDIS_URL" not in proc_present, (
                f"REDIS_URL present in /proc/self/environ (pid={probe['pid']}): {probe}"
            )
