"""Regression test for issue #1027 — work.py subprocess env sanitization.

The five subprocess call sites in ``osimflow/work.py`` must sanitize the
environment before invoking a child process, so the orchestrator's
``AWS_*`` / ``GITHUB_TOKEN`` / ``REDIS_URL`` secrets cannot leak to
``openstudio.cli`` or the bundled ``bin/*.py`` work scripts.

Coverage:

* :class:`TestSanitizeEnv` — direct unit tests of the ``_sanitize_env``
  allowlist (drop secrets, keep whitelist).
* :class:`TestSubprocessSitesPassSanitizedEnv` — every one of the six
  call sites identified in issue #1027 (and the two follow-ups
  ``aggregate_results`` / ``generate_plots`` tightened in issue #1388)
  passes ``env=_sanitize_env()`` to its ``subprocess.run`` /
  ``run_subprocess`` invocation.
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
    3. Forward legitimate ``OSIMFLOW_*`` framework flags and ``PYTHON*``
       interpreter variables by exact name (issue #1388: explicit
       allowlist, no prefix wildcards).
    4. Reject everything else, including ``OSIMFLOW_TASK_PAYLOAD_SECRET``
       and ``OSIMFLOW_TASK_PAYLOAD_SIG`` (issue #1388 — see
       :class:`TestTaskPayloadSecretExcluded`).
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

    def test_osimlow_explicit_allowlist_preserved(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Legitimate ``OSIMFLOW_*`` framework flags must be forwarded (exact match).

        Issue #1388 replaced the legacy ``OSIMFLOW_*`` prefix-match
        wildcard with an explicit allowlist of named vars.  Each
        ``OSIMFLOW_*`` var that a work script reads must be listed by
        name; arbitrary ``OSIMFLOW_RANDOM_FLAG`` style vars are no
        longer forwarded.
        """
        monkeypatch.setenv("OSIMFLOW_STUB_SIM", "1")
        monkeypatch.setenv("OSIMFLOW_RUN_ID", "campaign-xyz")
        monkeypatch.setenv("OSIMFLOW_LOG_LEVEL", "DEBUG")
        # An arbitrary, non-allowlisted OSIMFLOW_* var must be dropped.
        monkeypatch.setenv("OSIMFLOW_RANDOM_FLAG", "value")

        clean = _sanitize_env()

        assert clean["OSIMFLOW_STUB_SIM"] == "1"
        assert clean["OSIMFLOW_RUN_ID"] == "campaign-xyz"
        assert clean["OSIMFLOW_LOG_LEVEL"] == "DEBUG"
        assert "OSIMFLOW_RANDOM_FLAG" not in clean

    def test_python_explicit_allowlist_preserved(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """PYTHON interpreter variables are forwarded by exact name (issue #1388).

        Issue #1388 replaced the legacy ``PYTHON*`` prefix-match wildcard
        with an explicit allowlist.  Each Python interpreter var the
        child needs (``PYTHONPATH`` / ``PYTHONHOME`` / ``PYTHONIOENCODING``
        / ``PYTHONUNBUFFERED`` / ``PYTHONHASHSEED``) is listed by name;
        arbitrary ``PYTHON_*`` style vars are no longer forwarded.
        """
        monkeypatch.setenv("PYTHONPATH", "/sandbox/py")
        monkeypatch.setenv("PYTHONHOME", "/sandbox/pyhome")
        monkeypatch.setenv("PYTHONIOENCODING", "utf-8")
        monkeypatch.setenv("PYTHONUNBUFFERED", "1")
        monkeypatch.setenv("PYTHONHASHSEED", "42")
        # Arbitrary PYTHON_* var (no longer in the allowlist) must be dropped.
        monkeypatch.setenv("PYTHON_FROZEN_GARBAGE", "ignore_me")

        clean = _sanitize_env()

        assert clean["PYTHONPATH"] == "/sandbox/py"
        assert clean["PYTHONHOME"] == "/sandbox/pyhome"
        assert clean["PYTHONIOENCODING"] == "utf-8"
        assert clean["PYTHONUNBUFFERED"] == "1"
        assert clean["PYTHONHASHSEED"] == "42"
        assert "PYTHON_FROZEN_GARBAGE" not in clean

    def test_unknown_vars_dropped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Vars outside the allowlist must be dropped even if they look benign."""
        monkeypatch.setenv("SOMETHING_RANDOM", "drop_me")
        monkeypatch.setenv("UNRELATED_CONFIG_VAR", "drop_me_too")

        clean = _sanitize_env()

        assert "SOMETHING_RANDOM" not in clean
        assert "UNRELATED_CONFIG_VAR" not in clean

    def test_empty_parent_env_yields_empty_clean_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """With no parent vars, _sanitize_env() returns an empty dict (deny-all).

        Issue #1388: the allowlist is now exact-match only.  We delete
        every var in the allowlist from the parent env; ``_sanitize_env``
        must then return ``{}`` because nothing is forwarded.
        """
        for key in list(os.environ):
            if key in work_mod._WORK_SUBPROCESS_ENV_ALLOWLIST:
                monkeypatch.delenv(key, raising=False)

        assert _sanitize_env() == {}


# ===========================================================================
# Per-site regression: every callsite identified in issue #1027 must
# pass env=_sanitize_env() to its subprocess helper.
# ===========================================================================
class TestSubprocessSitesPassSanitizedEnv:
    """Every subprocess call site in ``osimflow/work.py`` must pass
    ``env=_sanitize_env()`` (issue #1027).

    The remaining sites are:

    1. ``_apply_parameters_stub``        — ``subprocess.run``
    2. ``run_openstudio_sim`` stub branch — ``run_subprocess``
    3. ``_run_real_openstudio``          — ``run_subprocess``
    4. ``generate_lhs``                  — ``subprocess.run``
    5. ``aggregate_results``             — ``subprocess.run`` (issue #1388)
    6. ``generate_plots``                — ``subprocess.run`` (issue #1388)

    ``_extract_kpis_impl`` is no longer a subprocess site (issue #1015:
    it now calls :func:`osimflow._work_scripts.extract_kpis.run_extract_kpis`
    in-process, so the credential-leak surface it created is gone
    entirely).  See :class:`TestExtractKpisNoSubprocess` below for the
    regression test that pins that behaviour.

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
    # Site 5 (removed, issue #1015): _extract_kpis_impl no longer
    # spawns a subprocess — see TestExtractKpisNoSubprocess below.
    # ---------------------------------------------------------------------

    # ---------------------------------------------------------------------
    # Site 5: aggregate_results (issue #1388)
    # ---------------------------------------------------------------------
    def test_aggregate_results_passes_sanitized_env(self, tmp_path: Path) -> None:
        """aggregate_results must pass env=_sanitize_env() to subprocess.run (issue #1388).

        Pre-fix the call site called ``subprocess.run(...)`` with no
        ``env=`` kwarg, which inherited the orchestrator's full env —
        including ``OSIMFLOW_TASK_PAYLOAD_SECRET``.  This regression
        test pins the fix.
        """
        kpi_files = [tmp_path / "k1.json"]
        kpi_files[0].write_text("{}")
        sim_dirs = [tmp_path / "s1"]
        sim_dirs[0].mkdir()
        out = tmp_path / "agg_out"

        with patch("osimflow.work.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            work_mod.aggregate_results(kpi_files, sim_dirs, out)

        self._assert_sanitized(mock_run)

    # ---------------------------------------------------------------------
    # Site 6: generate_plots (issue #1388)
    # ---------------------------------------------------------------------
    def test_generate_plots_passes_sanitized_env(self, tmp_path: Path) -> None:
        """generate_plots must pass env=_sanitize_env() (with PYTHONPATH
        override) to subprocess.run — NOT ``os.environ.copy()`` (issue #1388).

        Pre-fix the call site did ``env = os.environ.copy()`` which
        re-leaked every secret including ``OSIMFLOW_TASK_PAYLOAD_SECRET``.
        The fix is to start from the sanitized env and only override
        ``PYTHONPATH`` to point at the project root (issue #876).
        """
        csv_path = tmp_path / "agg.csv"
        csv_path.write_text("sample_id,eui\n0001,100\n")
        failed_path = tmp_path / "fail.csv"
        failed_path.write_text("sample_id,error\n")
        out = tmp_path / "plots"
        out.mkdir(parents=True, exist_ok=True)

        with patch("osimflow.work.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            work_mod.generate_plots(csv_path, failed_path, out)

        assert mock_run.called  # type: ignore[attr-defined]
        call_kwargs = mock_run.call_args.kwargs  # type: ignore[attr-defined]
        env = call_kwargs.get("env")
        assert env is not None, f"subprocess helper was called without env= (kwargs: {call_kwargs})"
        # generate_plots legitimately augments the sanitized env with
        # a PYTHONPATH override (issue #876).  We require the env to
        # be a superset of _sanitize_env() (with the extra PYTHONPATH
        # key) — anything else means a future refactor leaked a parent
        # var back into the child env.
        sanitized = work_mod._sanitize_env()
        for key, value in sanitized.items():
            assert env.get(key) == value, (
                f"env[{key!r}] drifted from _sanitize_env(): "
                f"expected {value!r}, got {env.get(key)!r}"
            )
        # The PYTHONPATH override must still be applied on top of the
        # sanitized env so the child can ``import osimflow`` (issue #876).
        assert "PYTHONPATH" in env, "generate_plots must still set PYTHONPATH for the child"
        # Belt-and-braces: even if the equality check above is loosened
        # in the future, the secrets must still be absent.
        for secret in (
            "AWS_SECRET_ACCESS_KEY",
            "AWS_ACCESS_KEY_ID",
            "GITHUB_TOKEN",
            "REDIS_URL",
        ):
            assert secret not in env
            assert not any(k.startswith("AWS_") for k in env)


# ===========================================================================
# Issue #1388 — HMAC task-payload secret must NEVER reach work-script
# subprocesses.  The orchestrator signs task payloads with a shared
# secret (``OSIMFLOW_TASK_PAYLOAD_SECRET``) so remote_runner can verify
# their provenance; that secret is the cap on what cloud-side workloads
# the orchestrator may submit.  Loss permits forging arbitrary step
# calls.  This block pins the fix: a planted secret must be absent from
# every ``subprocess.run`` / ``run_subprocess`` invocation triggered
# from ``osimflow.work``, while the positive control shows
# ``osimflow.remote_runner`` still receives the secret via its own env
# path (which is the legitimate consumer).
# ===========================================================================
class TestTaskPayloadSecretExcluded:
    """``OSIMFLOW_TASK_PAYLOAD_SECRET`` must NEVER leak into a work-script
    subprocess env (issue #1388).

    These tests plant the secret in ``os.environ`` (the way a real
    orchestrator running on Nomad / K8s with the secret mounted as an
    env var would have it) and then drive each call site that spawns a
    work-script subprocess.  The secret (and its signature companion
    ``OSIMFLOW_TASK_PAYLOAD_SIG``) must be absent from every captured
    env.
    """

    FAKE_SECRET = "deadbeef-coordinator-shared-secret-DO-NOT-LOG"
    FAKE_SIG = "f" * 64  # hex HMAC-SHA256 digest is 64 chars

    @pytest.fixture(autouse=True)
    def _plant_secret(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Plant the HMAC secret + signature in the parent env as the
        orchestrator would when running with HMAC enforcement enabled."""
        monkeypatch.setenv("OSIMFLOW_TASK_PAYLOAD_SECRET", self.FAKE_SECRET)
        monkeypatch.setenv("OSIMFLOW_TASK_PAYLOAD_SIG", self.FAKE_SIG)

    def _assert_no_secret(self, mock_subprocess: object) -> None:
        """Assert neither the secret nor the signature is in the captured env."""
        assert mock_subprocess.called  # type: ignore[attr-defined]
        call_kwargs = mock_subprocess.call_args.kwargs  # type: ignore[attr-defined]
        env = call_kwargs.get("env")
        assert env is not None, f"subprocess helper was called without env= (kwargs: {call_kwargs})"
        assert "OSIMFLOW_TASK_PAYLOAD_SECRET" not in env, (
            f"OSIMFLOW_TASK_PAYLOAD_SECRET leaked to subprocess env: "
            f"{env.get('OSIMFLOW_TASK_PAYLOAD_SECRET')!r}"
        )
        assert "OSIMFLOW_TASK_PAYLOAD_SIG" not in env, (
            f"OSIMFLOW_TASK_PAYLOAD_SIG leaked to subprocess env: "
            f"{env.get('OSIMFLOW_TASK_PAYLOAD_SIG')!r}"
        )
        assert "OSIMFLOW_TASK_PAYLOAD" not in env, (
            f"OSIMFLOW_TASK_PAYLOAD leaked to subprocess env (would let "
            f"a work script forge its own payloads): "
            f"{env.get('OSIMFLOW_TASK_PAYLOAD')!r}"
        )

    # ---------------------------------------------------------------------
    # Direct allowlist-level test
    # ---------------------------------------------------------------------
    def test_sanitize_env_directly_excludes_secret(self) -> None:
        """``_sanitize_env()`` must drop both the secret and the signature."""
        clean = _sanitize_env()
        assert "OSIMFLOW_TASK_PAYLOAD_SECRET" not in clean
        assert "OSIMFLOW_TASK_PAYLOAD_SIG" not in clean
        assert "OSIMFLOW_TASK_PAYLOAD" not in clean
        # The planted secret value must not appear anywhere in the
        # sanitized dict, even under a renamed key (belt-and-braces).
        for value in clean.values():
            assert self.FAKE_SECRET not in str(value), (
                f"planted secret value found in subprocess env: {value!r}"
            )

    # ---------------------------------------------------------------------
    # Every work-script subprocess site must scrub the secret.
    # ---------------------------------------------------------------------
    def test_apply_parameters_stub_excludes_secret(self, tmp_path: Path) -> None:
        """_apply_parameters_stub must scrub OSIMFLOW_TASK_PAYLOAD_SECRET."""
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
        self._assert_no_secret(mock_run)

    def test_run_openstudio_sim_stub_branch_excludes_secret(self, tmp_path: Path) -> None:
        """run_openstudio_sim stub branch must scrub OSIMFLOW_TASK_PAYLOAD_SECRET."""
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
        self._assert_no_secret(mock_run_subprocess)

    def test_run_real_openstudio_excludes_secret(self, tmp_path: Path) -> None:
        """_run_real_openstudio must scrub OSIMFLOW_TASK_PAYLOAD_SECRET."""
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
        self._assert_no_secret(mock_run_subprocess)

    def test_generate_lhs_excludes_secret(self, tmp_path: Path) -> None:
        """generate_lhs must scrub OSIMFLOW_TASK_PAYLOAD_SECRET."""
        variables_yml = tmp_path / "variables.yml"
        variables_yml.write_text("variables: []\n")
        out = tmp_path / "out"
        out.mkdir()
        with patch("osimflow.work.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            work_mod.generate_lhs(variables_yml, 5, out)
        self._assert_no_secret(mock_run)

    def test_aggregate_results_excludes_secret(self, tmp_path: Path) -> None:
        """aggregate_results must scrub OSIMFLOW_TASK_PAYLOAD_SECRET (issue #1388)."""
        kpi_files = [tmp_path / "k1.json"]
        kpi_files[0].write_text("{}")
        sim_dirs = [tmp_path / "s1"]
        sim_dirs[0].mkdir()
        out = tmp_path / "agg_out"
        with patch("osimflow.work.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            work_mod.aggregate_results(kpi_files, sim_dirs, out)
        self._assert_no_secret(mock_run)

    def test_generate_plots_excludes_secret(self, tmp_path: Path) -> None:
        """generate_plots must scrub OSIMFLOW_TASK_PAYLOAD_SECRET (issue #1388).

        Pre-fix the site did ``env = os.environ.copy()`` which
        re-leaked every secret — including the HMAC task-payload
        secret.  This regression test pins the sanitized-env fix.
        """
        csv_path = tmp_path / "agg.csv"
        csv_path.write_text("sample_id,eui\n0001,100\n")
        failed_path = tmp_path / "fail.csv"
        failed_path.write_text("sample_id,error\n")
        out = tmp_path / "plots"
        out.mkdir(parents=True, exist_ok=True)
        with patch("osimflow.work.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            work_mod.generate_plots(csv_path, failed_path, out)
        self._assert_no_secret(mock_run)

    # ---------------------------------------------------------------------
    # Positive control: ``osimflow.remote_runner`` IS the legitimate
    # consumer.  Its env-reading path (``os.environ.get``) is NOT
    # routed through ``work._sanitize_env``; the secret arrives via
    # the Nomad / K8s Job spec set by the executor.  This test pins
    # that distinction so a future refactor cannot silently route
    # remote_runner's env through ``_sanitize_env``.
    # ---------------------------------------------------------------------
    def test_remote_runner_legitimately_sees_secret(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Positive control: ``osimflow.remote_runner`` (via
        :func:`osimflow.task_payload_hmac.resolve_payload_secret`) still
        reads the secret from ``os.environ``.

        This is the legitimate consumer (issue #1177).  Its env path
        must NOT be the one we sanitized in ``osimflow.work``.
        """
        from osimflow.task_payload_hmac import (  # noqa: PLC0415 — test-local import
            TASK_PAYLOAD_SECRET_ENV,
            resolve_payload_secret,
        )

        # The autouse fixture already plants the secret in os.environ.
        # Verify resolve_payload_secret reads it back from the bare env,
        # not from a sanitized dict.
        assert os.environ.get(TASK_PAYLOAD_SECRET_ENV) == self.FAKE_SECRET
        assert resolve_payload_secret() == self.FAKE_SECRET


# ===========================================================================
# Issue #1015: _extract_kpis_impl must NOT spawn a subprocess.  The
# in-process path eliminates the per-sample 150-300 ms interpreter
# startup cost AND the per-sample subprocess env-leak surface.
# ===========================================================================
class TestExtractKpisNoSubprocess:
    """Pin the in-process behaviour introduced in issue #1015.

    ``_extract_kpis_impl`` used to fork a fresh Python interpreter per
    sample.  After the fix, the same-process
    :func:`osimflow._work_scripts.extract_kpis.run_extract_kpis` is
    invoked.  These tests ensure the subprocess is not re-introduced
    silently — a regression here would mean the credential-leak surface
    from issue #1027 is also back, and ~30 min of overhead for a 10K
    campaign.
    """

    def test_does_not_invoke_subprocess_run(self, tmp_path: Path) -> None:
        sim_dir = tmp_path / "sim"
        sim_dir.mkdir()
        out = tmp_path / "out"
        out.mkdir()
        with patch("osimflow.work.subprocess.run") as mock_run:
            work_mod._extract_kpis_impl(sim_dir, "0001", out)
        mock_run.assert_not_called()

    def test_does_not_invoke_run_subprocess(self, tmp_path: Path) -> None:
        sim_dir = tmp_path / "sim"
        sim_dir.mkdir()
        out = tmp_path / "out"
        out.mkdir()
        with patch("osimflow._subprocess_utils.run_subprocess") as mock_run_sub:
            work_mod._extract_kpis_impl(sim_dir, "0001", out)
        mock_run_sub.assert_not_called()

    def test_calls_in_process_run_extract_kpis(self, tmp_path: Path) -> None:
        sim_dir = tmp_path / "sim"
        sim_dir.mkdir()
        out = tmp_path / "out"
        out.mkdir()
        with patch("osimflow._work_scripts.extract_kpis.run_extract_kpis") as mock_run_extract:
            mock_run_extract.return_value = out / "kpi_0001.json"
            result = work_mod._extract_kpis_impl(sim_dir, "0001", out)
        assert result == out / "kpi_0001.json"
        mock_run_extract.assert_called_once_with(
            simulation_dir=sim_dir,
            sample_id="0001",
            out_path=out / "kpi_0001.json",
            openstudio_version=None,
            kpis=None,
        )


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
