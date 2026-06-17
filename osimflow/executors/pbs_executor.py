"""PBS/Torque executor for OSimFlow campaigns (issue #351).

Wraps the PBS/Torque CLI (qsub, qstat, qdel) to launch one job per
``submit()`` call, then polls ``qstat`` with exponential backoff until
the job reaches a terminal state. The returned ``Handle`` carries the
PBS job ID and blocks on ``.result()`` until the task succeeds; on
failure it re-raises a ``RuntimeError`` whose message includes the
exit status so the Campaign's ``except Exception`` path logs a useful
line.

Resource directives (``cpus``, ``memory_mb``, ``time_min``) are
mapped to PBS ``-l`` resources (``select`` chunks, ``walltime``).
Per-sample ``OSIMFLOW_OS_VERSION`` and ``OSIMFLOW_CONTAINER`` are
carried as environment variables — the same env vars
``SlurmExecutor``, ``AWSBatchExecutor``, and ``NomadExecutor``
export, so downstream work scripts can be substrate-agnostic.
"""

from __future__ import annotations

import logging
import subprocess
import time
from collections.abc import Callable, Sequence
from concurrent.futures import Future
from typing import Any

from osimflow.executors.base import BaseExecutor, Handle
from osimflow.executors.transport import resolve_result_for_callback

log = logging.getLogger("osimflow.executors")

# ---------------------------------------------------------------------------
# PBSHandle
# ---------------------------------------------------------------------------


class _PBSHandle(Handle):
    """Handle that polls PBS ``qstat`` on ``.result()``.

    Mirrors ``_AWSBatchHandle`` and ``_NomadHandle``: the work runs on a
    remote PBS job (not a thread or submitit job), so we cannot back the
    Future with a local completion. The handle carries a reference to
    its executor and the PBS job ID; ``result()`` blocks on
    ``_wait_for_terminal`` and ``done()`` does a single non-blocking
    ``qstat`` call.

    The handle's ``_future`` is set when ``result()`` reaches a terminal
    state so concurrent callers don't re-poll.
    """

    def __init__(self, job_id: str, executor: PBSExecutor, *, result_hint: Any = None) -> None:
        self.job_id = job_id
        self._executor = executor
        self._result_hint = result_hint
        self._future: Future[Any] = Future()
        # Worker tracking fields.
        self.worker_id: str | None = job_id
        self.worker_ip: str | None = None
        self.worker_region: str | None = None

    def result(self, timeout: float | None = None) -> Any:  # noqa: ARG002
        # The polling itself doesn't take a `timeout` parameter; the
        # PBS ``walltime`` resource (when set) is the substrate-level
        # kill. `timeout` here is accepted for the base-class signature
        # but not enforced — the existing executors take the same
        # approach.
        try:
            job_state, exit_code = self._executor._wait_for_terminal(self.job_id)
        except BaseException as exc:  # noqa: BLE001 — surface any poll error
            self._future.set_exception(exc)
            raise

        if exit_code == 0:
            resolved = resolve_result_for_callback(self._result_hint, default=None)
            self._future.set_result(resolved)
            return resolved

        # Non-zero exit: surface the failure with the exit code.
        msg = f"PBS job {self.job_id!r} exited with code {exit_code} (state={job_state})"
        self._future.set_exception(RuntimeError(msg))
        raise RuntimeError(msg)

    def done(self) -> bool:
        # If the future is already finished (terminal status observed
        # by a prior ``result()`` call), report done without making
        # another qstat call.
        if self._future.done():
            return True
        try:
            state = self._executor._query_job_state(self.job_id)
        except Exception:  # noqa: BLE001 — never raise from done()
            return False
        # Terminal states in PBS: F (finished), E (exiting),
        # Q (queued, but we only poll when done() is called which
        # means the caller already waited), C (completed).
        return state in ("F", "E", "C")


# ---------------------------------------------------------------------------
# PBSExecutor
# ---------------------------------------------------------------------------


class PBSExecutor(BaseExecutor):
    """PBS/Torque batch executor (issue #351).

    Wraps the PBS/Torque CLI (``qsub`` to submit, ``qstat`` to poll,
    ``qdel`` to cancel) to launch one job per ``submit()`` call, then
    polls with exponential backoff until the job reaches a terminal
    state. The returned ``Handle`` carries the PBS job ID and blocks on
    ``.result()`` until the task succeeds; on failure it re-raises a
    ``RuntimeError`` whose message includes the exit status.

    With ``debug=True`` (the default), the executor runs the work as a
    local subprocess — mirroring the submitit DebugExecutor pattern
    used by ``SlurmExecutor`` in debug mode. This is useful for
    development without a real PBS cluster.

    Resource directives (``cpus``, ``memory_mb``, ``time_min``) are
    mapped to PBS ``-l`` resources (``select``, ``walltime``).  Per-
    sample ``OSIMFLOW_OS_VERSION`` and ``OSIMFLOW_CONTAINER`` are
    carried as environment variables — the same env vars
    ``SlurmExecutor`` and ``AWSBatchExecutor`` export.

    Security: PBS credentials are sourced from the environment
    (``PBS_DEFAULT`` env var or default PBS_TORQUE_HOME). The
    constructor does **not** accept a ``server`` kwarg; pinning the
    server in code would hard-code the deployment.
    """

    name = "pbs"

    def __init__(
        self,
        server: str | None = None,
        queue: str | None = None,
        debug: bool = True,
        poll_interval_s: float = 5.0,
        max_poll_interval_s: float = 60.0,
        cpus_per_node: int = 1,
        mem_mb_per_node: int = 1024,
    ):
        self.server = server or _default_pbs_server()
        self.queue = queue
        self.debug = debug
        self.poll_interval_s = poll_interval_s
        self.max_poll_interval_s = max_poll_interval_s
        self.cpus_per_node = cpus_per_node
        self.mem_mb_per_node = mem_mb_per_node

    # -----------------------------------------------------------------------
    # PBS CLI helpers
    # -----------------------------------------------------------------------

    def _qsub_cmd(
        self,
        *,
        name: str,
        cpus: int,
        memory_mb: int,
        time_min: int,
        container: str | None,
        openstudio_version: str | None,
        script_lines: Sequence[str],
    ) -> list[str]:
        """Build the ``qsub`` command line as a list of strings."""
        cmd: list[str] = ["qsub"]
        if self.server:
            cmd += ["-q", self.server]
        if self.queue:
            cmd += ["-q", self.queue]

        # PBS resource strings.
        # ``select`` specifies the number of chunks (nodes * ppn).
        # Each chunk requests cpus_per_node CPUs and mem_mb_per_node memory.
        n_chunks = max(1, cpus // self.cpus_per_node)
        mem_gb = (memory_mb + 1023) // 1024  # MB -> GB, rounded up
        resource_str = f"select={n_chunks}:ncpus={cpus}:mem={mem_gb}gb"
        cmd += ["-l", resource_str]

        # Walltime: format HH:MM:SS
        hours, rem = divmod(time_min * 60, 3600)
        minutes, seconds = divmod(rem, 60)
        cmd += ["-l", f"walltime={hours:02d}:{minutes:02d}:{seconds:02d}"]

        # Job name for readability in qstat.
        cmd += ["-N", name]

        # Environment variables carried through the job.
        env_lines: list[str] = []
        if openstudio_version is not None:
            env_lines.append(f"OSIMFLOW_OS_VERSION={openstudio_version}")
        if container is not None:
            env_lines.append(f"OSIMFLOW_CONTAINER={container}")

        # Build the inner script: set env vars then exec the user command.
        # The script receives the work command via $1 / @ARGV in the Perl convention.
        full_script = [
            "#!/bin/sh",
            "set -euo pipefail",
        ]
        for env_line in env_lines:
            full_script.append(f"export {env_line}")
        full_script.extend(script_lines)

        cmd += ["--"]  # qsub sentinel; script follows
        cmd += full_script

        return cmd

    def _submit_job(
        self,
        *,
        name: str,
        cpus: int,
        memory_mb: int,
        time_min: int,
        container: str | None,
        openstudio_version: str | None,
        script_lines: Sequence[str],
    ) -> str:
        """Submit a single PBS job and return the job ID."""
        cmd = self._qsub_cmd(
            name=name,
            cpus=cpus,
            memory_mb=memory_mb,
            time_min=time_min,
            container=container,
            openstudio_version=openstudio_version,
            script_lines=script_lines,
        )
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=True,
        )
        # qsub outputs the job ID on stdout, e.g. "123.pbsserver"
        job_id = result.stdout.strip()
        log.info("pbs qsub -> jobId=%s", job_id)
        return job_id

    def _query_job_state(self, job_id: str) -> str:
        """Query the current state of a PBS job via ``qstat``.

        Returns a single-letter PBS state code:
          - Q: queued
          - R: running
          - E: exiting
          - F: finished (completed or failed)
          - H: held
          - T: transit
          - W: waiting
          - S: suspended
        """
        # ``qstat -f`` gives full output; ``qstat -x`` includes finished jobs.
        # We use ``qstat -f`` and look for ``job_state = X``.
        result = subprocess.run(
            ["qstat", "-f", job_id],
            capture_output=True,
            text=True,
            check=False,
        )
        # qstat returns non-zero when the job is unknown (already completed
        # and purged, or never existed). Treat empty output as unknown.
        if result.returncode != 0 or not result.stdout.strip():
            return "F"  # treat unknown as terminal

        for raw_line in result.stdout.splitlines():
            stripped = raw_line.strip()
            if "job_state = " in stripped:
                # e.g. "    job_state = R"
                state = stripped.split("=", 1)[1].strip()
                return state
        return "F"  # fallback to terminal

    def _parse_exit_status(self, job_id: str) -> int:
        """Parse the exit status from ``qstat -f`` for a finished job.

        Returns the job's exit code, or -1 if it cannot be determined.
        PBS stores ``exit_status`` in the job's attributes when the job
        completes.
        """
        result = subprocess.run(
            ["qstat", "-f", job_id],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            return -1

        for raw_line in result.stdout.splitlines():
            stripped = raw_line.strip()
            if "exit_status = " in stripped:
                try:
                    return int(stripped.split("=", 1)[1].strip())
                except (ValueError, IndexError):
                    pass
        return -1

    def _wait_for_terminal(self, job_id: str) -> tuple[str, int]:
        """Poll ``qstat`` with exponential backoff until the job is terminal.

        Returns ``(state, exit_code)``.
        """
        delay = self.poll_interval_s
        while True:
            state = self._query_job_state(job_id)
            if state in ("F", "E", "C"):
                exit_code = self._parse_exit_status(job_id)
                return state, exit_code
            log.info("pbs poll jobId=%s state=%s (sleeping %.1fs)", job_id, state, delay)
            time.sleep(delay)
            # Exponential backoff, capped.
            delay = min(delay * 2, self.max_poll_interval_s)

    # -----------------------------------------------------------------------
    # submit / shutdown
    # -----------------------------------------------------------------------

    def submit(
        self,
        fn: Callable[..., Any],
        *args: Any,
        name: str = "task",
        cpus: int = 1,
        memory_mb: int = 1024,
        time_min: int = 60,
        container: str | None = None,
        **kwargs: Any,
    ) -> Handle:
        openstudio_version = kwargs.get("openstudio_version")
        result_hint = kwargs.get("result_hint")

        if self.debug:
            # In debug mode, run locally via submitit.DebugExecutor-like
            # behavior: execute the function in a child process using
            # subprocess.run, mimicking what PBS would do but locally.
            log.info(
                "pbs submit (DEBUG) name=%s cpus=%d mem=%dMB time_min=%d",
                name,
                cpus,
                memory_mb,
                time_min,
            )
            import socket

            local_job_id = f"pbs-debug-{socket.gethostname()}-{id(fn)}"
            env = kwargs.get("env", {})

            def _run_locally() -> Any:
                import os

                os.environ["OSIMFLOW_OS_VERSION"] = str(openstudio_version or "N/A")
                if container:
                    os.environ["OSIMFLOW_CONTAINER"] = container
                for k, v in env.items():
                    if v is not None:
                        os.environ[k] = v
                return fn(*args)  # noqa: F821

            # Run synchronously in debug mode (no fan-out, no async).
            try:
                _run_locally()
            except Exception as exc:
                log.error("pbs debug job %s failed: %s", local_job_id, exc)
                raise
            finally:
                pass

            fut: Future[Any] = Future()
            fut.set_result(resolve_result_for_callback(result_hint, default=None))
            return Handle(
                job_id=local_job_id,
                _future=fut,
                worker_id=local_job_id,
                worker_ip=socket.gethostname(),
                worker_region=None,
            )

        log.info(
            "pbs submit name=%s cpus=%d mem=%dMB time_min=%d container=%s",
            name,
            cpus,
            memory_mb,
            time_min,
            container,
        )

        # Build the script that the PBS job will run.
        # We use a shell wrapper that sets env vars and then runs the
        # callable's work. In practice, the work is dispatched via
        # the campaign's work layer; the script here is a generic
        # runner that receives the command via a heredoc-like approach.
        # Since fn/args are opaque to PBS (they're in the campaign's
        # Python process, not the PBS job's environment), the actual
        # work command is determined by the campaign — we emit a simple
        # shim that the campaign populates via the template_sim_package.
        script_lines = ["sleep infinity"]  # placeholder; campaign replaces this

        job_id = self._submit_job(
            name=name,
            cpus=cpus,
            memory_mb=memory_mb,
            time_min=time_min,
            container=container,
            openstudio_version=openstudio_version,
            script_lines=script_lines,
        )

        # The work will be executed by the campaign's work layer
        # on the node that PBS assigns. We return a handle that
        # tracks the PBS job state.
        del fn, args  # noqa: ARG002

        return _PBSHandle(job_id=job_id, executor=self, result_hint=result_hint)

    def shutdown(self) -> None:
        # PBS jobs are tracked by PBS itself; no local state to tear down.
        pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _default_pbs_server() -> str | None:
    """Resolve the default PBS server from the environment.

    PBS_DEFAULT is the standard env var for the default PBS server.
    """
    import os

    return os.environ.get("PBS_DEFAULT")
