"""Unit tests for osimflow.executors.pbs_executor (issue #351).

Covers:
  - PBSExecutor: debug mode, server/queue config, name attribute
  - PBSExecutor._qsub_cmd: command-line construction
  - PBSExecutor._query_job_state: qstat parsing
  - PBSExecutor._parse_exit_status: exit status parsing
  - PBSExecutor._wait_for_terminal: polling with exponential backoff
  - PBSExecutor.submit: returns handle, wires through to qsub
  - _PBSHandle: result() and done() polling
  - _default_pbs_server: env var resolution
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

from osimflow.executors.pbs_executor import (
    PBSExecutor,
    _default_pbs_server,
    _PBSHandle,
)

# ---------------------------------------------------------------------------
# PBSExecutor
# ---------------------------------------------------------------------------


class TestPBSExecutor:
    """PBSExecutor wraps PBS CLI (qsub, qstat)."""

    def test_debug_mode_default(self) -> None:
        ex = PBSExecutor(debug=True)
        assert ex.debug is True
        ex.shutdown()

    def test_debug_false_uses_real_pbs(self) -> None:
        ex = PBSExecutor(debug=False, server="pbsserver", queue="batch")
        assert ex.debug is False
        assert ex.server == "pbsserver"
        assert ex.queue == "batch"
        ex.shutdown()

    def test_server_stored(self) -> None:
        ex = PBSExecutor(server="pbsserver")
        assert ex.server == "pbsserver"
        ex.shutdown()

    def test_queue_stored(self) -> None:
        ex = PBSExecutor(queue="default")
        assert ex.queue == "default"
        ex.shutdown()

    def test_name_attribute(self) -> None:
        assert PBSExecutor.__new__(PBSExecutor).name == "pbs"  # noqa: SLF001

    def test_default_server_from_env(self) -> None:
        with patch.dict(os.environ, {"PBS_DEFAULT": "envserver"}):
            server = _default_pbs_server()
            assert server == "envserver"

    def test_default_server_none_when_unset(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            # If PBS_DEFAULT is not set, we get None (uses system default).
            # Note: we patch so subprocess won't inherit PBS_DEFAULT from
            # the test environment.
            server = _default_pbs_server()
            # _default_pbs_server reads os.environ directly; if PBS_DEFAULT
            # is not in the (patched) env, it returns None.
            assert server is None

    def test_shutdown_is_noop(self) -> None:
        ex = PBSExecutor()
        ex.shutdown()  # should not raise


class TestPBSExecutorQsubCmd:
    """_qsub_cmd builds correct qsub command lines."""

    def _make(self, **kw: object) -> PBSExecutor:
        return PBSExecutor(**kw)  # type: ignore[arg-type]

    def test_basic_command(self) -> None:
        ex = self._make(server="pbs", queue="default", debug=False)
        cmd = ex._qsub_cmd(
            name="test",
            cpus=1,
            memory_mb=1024,
            time_min=60,
            container=None,
            openstudio_version=None,
            script_lines=["echo hello"],
        )
        assert cmd[0] == "qsub"
        assert "-N" in cmd
        name_idx = cmd.index("-N")
        assert cmd[name_idx + 1] == "test"

    def test_server_flag(self) -> None:
        ex = self._make(server="pbsserver", debug=False)
        cmd = ex._qsub_cmd(
            name="s", cpus=1, memory_mb=512, time_min=30,
            container=None, openstudio_version=None, script_lines=["true"],
        )
        assert "-q" in cmd
        q_idx = cmd.index("-q")
        assert cmd[q_idx + 1] == "pbsserver"

    def test_queue_flag(self) -> None:
        ex = self._make(queue="batch", debug=False)
        cmd = ex._qsub_cmd(
            name="s", cpus=1, memory_mb=512, time_min=30,
            container=None, openstudio_version=None, script_lines=["true"],
        )
        assert "-q" in cmd
        q_idx = cmd.index("-q")
        assert cmd[q_idx + 1] == "batch"

    def test_select_resource(self) -> None:
        ex = self._make(cpus_per_node=2, mem_mb_per_node=4096, debug=False)
        cmd = ex._qsub_cmd(
            name="r", cpus=4, memory_mb=8192, time_min=60,
            container=None, openstudio_version=None, script_lines=["true"],
        )
        assert "-l" in cmd
        li = cmd.index("-l")
        resource_str = cmd[li + 1]
        assert "select=" in resource_str
        assert "ncpus=4" in resource_str
        assert "mem=" in resource_str

    def test_walltime_format(self) -> None:
        ex = self._make(debug=False)
        # 90 minutes -> 01:30:00
        cmd = ex._qsub_cmd(
            name="w", cpus=1, memory_mb=512, time_min=90,
            container=None, openstudio_version=None, script_lines=["true"],
        )
        assert "-l" in cmd
        li = cmd.index("-l")
        # walltime is the second -l argument
        walltime_arg = cmd[li + 3]  # first -l is select, second is walltime
        assert walltime_arg.startswith("walltime=")
        assert "01:30:00" in walltime_arg

    def test_script_lines_appended(self) -> None:
        ex = self._make(debug=False)
        cmd = ex._qsub_cmd(
            name="s", cpus=1, memory_mb=512, time_min=10,
            container=None, openstudio_version=None,
            script_lines=["echo hello", "sleep 1"],
        )
        assert "--" in cmd
        dash_idx = cmd.index("--")
        # full_script is ["#!/bin/sh", "set -euo pipefail", ...env_lines, ...script_lines]
        assert cmd[dash_idx + 1] == "#!/bin/sh"
        assert cmd[dash_idx + 2] == "set -euo pipefail"
        assert cmd[dash_idx + 3] == "echo hello"
        assert cmd[dash_idx + 4] == "sleep 1"


class TestPBSExecutorQueryState:
    """_query_job_state parses qstat output."""

    def _make(self) -> PBSExecutor:
        return PBSExecutor(debug=False)

    def test_running_state(self) -> None:
        ex = self._make()
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="    job_state = R\n",
            )
            state = ex._query_job_state("123.pbs")
            assert state == "R"

    def test_finished_state(self) -> None:
        ex = self._make()
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="    job_state = F\n",
            )
            state = ex._query_job_state("123.pbs")
            assert state == "F"

    def test_unknown_job_returns_F(self) -> None:
        ex = self._make()
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="")
            state = ex._query_job_state("999.pbs")
            assert state == "F"


class TestPBSExecutorParseExitStatus:
    """_parse_exit_status extracts exit code from qstat."""

    def _make(self) -> PBSExecutor:
        return PBSExecutor(debug=False)

    def test_exit_status_found(self) -> None:
        ex = self._make()
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="    exit_status = 0\n",
            )
            code = ex._parse_exit_status("123.pbs")
            assert code == 0

    def test_exit_status_nonzero(self) -> None:
        ex = self._make()
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="    exit_status = 137\n",
            )
            code = ex._parse_exit_status("123.pbs")
            assert code == 137

    def test_exit_status_not_found(self) -> None:
        ex = self._make()
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="no exit_status")
            code = ex._parse_exit_status("123.pbs")
            assert code == -1

    def test_qstat_error(self) -> None:
        ex = self._make()
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="")
            code = ex._parse_exit_status("123.pbs")
            assert code == -1


class TestPBSExecutorWaitForTerminal:
    """_wait_for_terminal polls until terminal state."""

    def _make(self) -> PBSExecutor:
        return PBSExecutor(debug=False, poll_interval_s=0.01, max_poll_interval_s=0.02)

    def test_returns_on_F(self) -> None:
        ex = self._make()
        with patch.object(ex, "_query_job_state", return_value="F"):
            with patch.object(ex, "_parse_exit_status", return_value=0):
                state, code = ex._wait_for_terminal("123.pbs")
                assert state == "F"
                assert code == 0

    def test_polls_until_terminal(self) -> None:
        ex = self._make()
        call_count = 0

        def _state(job_id: str) -> str:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return "R"
            return "F"

        with patch.object(ex, "_query_job_state", side_effect=_state):
            with patch.object(ex, "_parse_exit_status", return_value=0):
                with patch("osimflow.executors.pbs_executor.time.sleep"):
                    state, code = ex._wait_for_terminal("123.pbs")
        assert state == "F"
        assert call_count == 2


class TestPBSExecutorSubmit:
    """submit() wires through to qsub in non-debug mode."""

    def _make(self) -> PBSExecutor:
        ex = PBSExecutor.__new__(PBSExecutor)
        ex.server = "pbsserver"
        ex.queue = None
        ex.debug = False
        ex.poll_interval_s = 0.01
        ex.max_poll_interval_s = 0.02
        ex.cpus_per_node = 1
        ex.mem_mb_per_node = 1024
        return ex

    def test_submit_returns_handle(self) -> None:
        ex = self._make()
        with patch.object(ex, "_submit_job", return_value="456.pbs"):
            handle = ex.submit(lambda: None, name="test")
        assert hasattr(handle, "result")
        assert hasattr(handle, "done")
        assert handle.job_id == "456.pbs"
        ex.shutdown()

    def test_submit_with_resources(self) -> None:
        ex = self._make()
        with patch.object(ex, "_submit_job", return_value="456.pbs") as mock_submit:
            handle = ex.submit(
                lambda: None, name="heavy",
                cpus=4, memory_mb=16384, time_min=120,
            )
            assert handle.job_id == "456.pbs"
            # Verify _submit_job was called with correct resources.
            call_kwargs = mock_submit.call_args[1]
            assert call_kwargs["cpus"] == 4
            assert call_kwargs["memory_mb"] == 16384
            assert call_kwargs["time_min"] == 120
        ex.shutdown()

    def test_submit_debug_runs_locally(self) -> None:
        ex = PBSExecutor(debug=True)
        called = False

        def _fn() -> None:
            nonlocal called
            called = True

        handle = ex.submit(_fn, name="local-test")
        assert handle.job_id.startswith("pbs-debug-")
        assert called is True
        result = handle.result(timeout=5)
        assert result is None
        ex.shutdown()


class TestPBSHandle:
    """_PBSHandle polls qstat on result() and done()."""

    def _make_handle(
        self, *, state: str = "F", exit_code: int = 0
    ) -> tuple[_PBSHandle, MagicMock]:
        mock_ex = MagicMock()
        mock_ex._wait_for_terminal.return_value = (state, exit_code)
        mock_ex.poll_interval_s = 0.01
        mock_ex.max_poll_interval_s = 0.02
        handle = _PBSHandle(job_id="123.pbs", executor=mock_ex)
        return handle, mock_ex

    def test_result_success(self) -> None:
        handle, mock_ex = self._make_handle(state="F", exit_code=0)
        with patch("osimflow.executors.pbs_executor.time.sleep"):
            assert handle.result() is None
        assert handle._future.done()

    def test_result_failure_raises(self) -> None:
        handle, mock_ex = self._make_handle(state="F", exit_code=137)
        with (
            patch("osimflow.executors.pbs_executor.time.sleep"),
            pytest.raises(RuntimeError, match="137"),
        ):
            handle.result()

    def test_done_true_when_future_done(self) -> None:
        handle, _ = self._make_handle(state="F", exit_code=0)
        with patch("osimflow.executors.pbs_executor.time.sleep"):
            handle.result()
        assert handle.done() is True

    def test_done_false_when_running(self) -> None:
        handle, mock_ex = self._make_handle(state="R")
        assert handle.done() is False

    def test_done_api_error_returns_false(self) -> None:
        handle, mock_ex = self._make_handle(state="R")
        mock_ex._query_job_state.side_effect = Exception("network")
        assert handle.done() is False

    def test_worker_fields(self) -> None:
        handle, _ = self._make_handle()
        assert handle.worker_id == "123.pbs"
        assert handle.worker_ip is None
        assert handle.worker_region is None
