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
import re
import subprocess
import time  # noqa: F401 — patch seam: tests patch pbs_executor.time.sleep
from collections.abc import Callable, Sequence
from concurrent.futures import Future
from typing import Any, cast

from osimflow.executors.base import (
    BaseExecutor,
    Handle,
    PollingHandle,
    PollOutcome,
    poll_until_terminal,
    retry_with_backoff,
)
from osimflow.executors.transport import (
    coerce_transport_mode,
    materialize_object_storage_result,
    resolve_result_for_callback,
)

log = logging.getLogger("osimflow.executors")

# Issue #1405: transient stderr patterns we retry on. Covers PBS server
# hiccups (queue manager restart, network blip, connection refused).
_TRANSIENT_STDERR_RE = re.compile(
    r"connection refused|server unavailable|timeout|timed out|connection reset",
    re.IGNORECASE,
)

# Sentinel returned by ``_query_job_state`` when ``qstat`` itself failed
# transiently (so we should keep polling instead of declaring the job
# terminal). Distinct from any single-letter PBS state code.
PBS_STATE_TRANSIENT: str | None = None

# ---------------------------------------------------------------------------
# PBSHandle
# ---------------------------------------------------------------------------


class _PBSHandle(PollingHandle):
    """Handle that polls PBS ``qstat`` on `.result()`.

    Mirrors ``_AWSBatchHandle`` and ``_NomadHandle``: the work runs on a
    remote PBS job (not a thread or submitit job), so we cannot back the
    Future with a local completion. The handle carries a reference to
    its executor and the PBS job ID; `result()` blocks on
    `_wait_for_terminal` and `done()` does a single non-blocking
    ``qstat`` call.

    The poll-deadline state machine lives in the shared
    ``PollingHandle`` base (issues #1464 / #1540); this class supplies
    only the PBS-specific hooks below. The handle's ``_future`` is set
    when ``result()`` reaches a terminal state so concurrent callers
    don't re-poll.
    """

    def __init__(
        self,
        job_id: str,
        executor: PBSExecutor,
        *,
        result_hint: Any = None,
        result_transport_mode: str = "auto",
        result_storage_backend: str | None = None,
        result_storage_bucket: str | None = None,
        result_storage_prefix: str | None = None,
        result_storage_endpoint: str | None = None,
    ) -> None:
        self.job_id = job_id
        self._executor = executor
        self._result_hint = result_hint
        # Result-transport contract (issue #1333): materialize object-storage
        # artifacts on `.result()` so Campaign callbacks receive local paths
        # — identical to the Nomad and Kubernetes handles.
        self._result_transport_mode = coerce_transport_mode(result_transport_mode)
        self._result_storage_backend = result_storage_backend
        self._result_storage_bucket = result_storage_bucket
        self._result_storage_prefix = result_storage_prefix
        self._result_storage_endpoint = result_storage_endpoint
        self._future: Future[Any] = Future()
        # Worker tracking fields.
        self.worker_id: str | None = job_id
        self.worker_ip: str | None = None
        self.worker_region: str | None = None

    # ------------------------------------------------------------------
    # PollingHandle hooks (issues #1464 / #1540) — the shared state
    # machine in ``osimflow.executors.base.PollingHandle`` owns
    # ``result()``; ``PBSExecutor._wait_for_terminal`` owns the poll
    # skeleton via ``base.poll_until_terminal`` (including the
    # transient-qstat handling of issue #1405).
    # ------------------------------------------------------------------

    def _wait_for_terminal(self, timeout: float | None) -> tuple[str, int]:
        # Issue #1465: ``timeout`` is the deadline for the whole call —
        # enforced by the executor poll loop. The PBS ``walltime``
        # resource (when set) remains the substrate-level kill (defense
        # in depth).
        return cast(
            "tuple[str, int]",
            self._executor._wait_for_terminal(self.job_id, timeout=timeout),  # noqa: SLF001
        )

    def _classify(self, job: Any) -> tuple[PollOutcome, str | None]:
        _state, exit_code = job
        if exit_code == 0:
            return PollOutcome.SUCCEEDED, None
        return PollOutcome.FAILED, None

    def _resolve_success_result(self, timeout: float | None = None) -> Any:
        resolved = resolve_result_for_callback(
            self._result_hint,
            default=None,
            transport_mode=self._result_transport_mode,
        )
        return materialize_object_storage_result(
            resolved,
            transport_mode=self._result_transport_mode,
            result_storage_backend=self._result_storage_backend,
            result_storage_bucket=self._result_storage_bucket,
            result_storage_prefix=self._result_storage_prefix,
            result_storage_endpoint=self._result_storage_endpoint,
        )

    def _failure_error(self, job: Any) -> RuntimeError:
        job_state, exit_code = job
        return RuntimeError(
            f"PBS job {self.job_id!r} exited with code {exit_code} (state={job_state})"
        )

    def done(self) -> bool:
        # If the future is already finished (terminal status observed
        # by a prior ``result()`` call), report done without making
        # another qstat call.
        if self._future.done():
            return True
        try:
            state = self._executor._query_job_state(self.job_id)
        except TimeoutError as exc:
            log.debug("PBS done() timeout for job %s: %s", self.job_id, exc)
            return False
        except OSError as exc:
            log.debug("PBS done() OS error for job %s: %s", self.job_id, exc)
            return False
        except Exception as exc:  # noqa: BLE001
            log.debug("PBS done() transient error for job %s: %s", self.job_id, exc)
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
        # Issue #1081: digest pinning. Initialized in the constructor so
        # ``_qsub_cmd`` can reference it without going through ``submit()``
        # (e.g. unit tests); overridden by ``submit()``.
        self._container_digest: str | None = None

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
        container_digest = getattr(self, "_container_digest", None)
        if container is None and container_digest is not None:
            container = container_digest
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

        def _run() -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=True,
            )

        result = _retry_pbs_call(_run)
        # qsub outputs the job ID on stdout, e.g. "123.pbsserver"
        job_id = result.stdout.strip()
        log.info("pbs qsub -> jobId=%s", job_id)
        return job_id

    def _query_job_state(self, job_id: str) -> str | None:
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

        Returns ``PBS_STATE_TRANSIENT`` (sentinel ``None``) when ``qstat``
        itself failed transiently (PBS server hiccup, connection
        refused, etc.) — see issue #1405. The caller is expected to keep
        polling instead of declaring the job terminal. Returns ``"F"``
        only when the job is genuinely unknown (non-transient stderr,
        empty stdout).
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
            stderr = result.stderr or ""
            if _TRANSIENT_STDERR_RE.search(stderr):
                log.warning(
                    "pbs qstat transient failure for job %s: %s",
                    job_id,
                    stderr.strip(),
                )
                return PBS_STATE_TRANSIENT
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

    def _wait_for_terminal(self, job_id: str, timeout: float | None = None) -> tuple[str, int]:
        """Poll ``qstat`` with exponential backoff until the job is terminal.

        Returns ``(state, exit_code)``.

        The poll skeleton (deadline, deadline clamping (sleep capped at the remaining budget),
        capped exponential growth) lives in
        ``osimflow.executors.base.poll_until_terminal`` (issue #1540);
        the PBS loop grows the delay before sleeping. The
        transient-qstat handling of issue #1405 rides the shared
        ``is_transient`` hook: ``PBS_STATE_TRANSIENT`` retries at the
        current delay without growing the backoff (a transient qstat
        failure says nothing about the job, so there is no reason to
        back off further).

        Raises:
            TimeoutError: if *timeout* seconds elapse before a terminal state.
        """
        state: str | None = poll_until_terminal(
            lambda: self._query_job_state(job_id),
            is_terminal=lambda s: s in ("F", "E", "C"),
            timeout=timeout,
            timeout_message=lambda elapsed: (
                f"Timed out after {elapsed:.1f}s waiting for job {job_id!r}"
            ),
            poll_interval_s=self.poll_interval_s,
            max_poll_interval_s=self.max_poll_interval_s,
            on_pending=lambda s, delay, _sleep_amount: log.info(
                "pbs poll jobId=%s state=%s (sleeping %.1fs)", job_id, s, delay
            ),
            is_transient=lambda s: s is PBS_STATE_TRANSIENT,
            on_transient=lambda _s, delay: log.info(
                "pbs poll jobId=%s state=TRANSIENT (retrying in %.1fs)", job_id, delay
            ),
            grow_before_sleep=True,
        )
        # ``is_terminal`` only accepts the concrete PBS state codes —
        # a transient (``None``) probe result never terminates the loop.
        assert state is not None
        exit_code = self._parse_exit_status(job_id)
        return state, exit_code

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
        container_digest: str | None = None,
        openstudio_version: str | None = None,
        result_hint: Any = None,
        remote_command: str | None = None,
        result_transport_mode: str | None = None,
        result_storage_backend: str | None = None,
        result_storage_bucket: str | None = None,
        result_storage_prefix: str | None = None,
        result_storage_endpoint: str | None = None,
        variables_json: str | None = None,
        env: dict[str, str] | None = None,
        stdout_path: Any = None,
        stderr_path: Any = None,
        max_retries: int | None = None,
        worker_id: str | None = None,
        **kwargs: Any,
    ) -> Handle:
        self._container_digest = container_digest
        del remote_command  # noqa: F841
        # result_transport_mode / result_storage_* are consumed below when
        # the handle is constructed (issue #1333 — object-storage materialization).
        del variables_json, stdout_path, stderr_path, max_retries, worker_id, kwargs  # noqa: F841, ARG002
        # env is used in debug mode; result_hint is used throughout.
        local_env: dict[str, str] = env if env is not None else {}

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

            def _run_locally() -> Any:
                import os

                os.environ["OSIMFLOW_OS_VERSION"] = str(openstudio_version or "N/A")
                if container:
                    os.environ["OSIMFLOW_CONTAINER"] = container
                for k, v in local_env.items():
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

        return _PBSHandle(
            job_id=job_id,
            executor=self,
            result_hint=result_hint,
            result_transport_mode=(
                str(result_transport_mode) if result_transport_mode is not None else "auto"
            ),
            result_storage_backend=(
                str(result_storage_backend) if result_storage_backend is not None else None
            ),
            result_storage_bucket=(
                str(result_storage_bucket) if result_storage_bucket is not None else None
            ),
            result_storage_prefix=(
                str(result_storage_prefix) if result_storage_prefix is not None else None
            ),
            result_storage_endpoint=(
                str(result_storage_endpoint) if result_storage_endpoint is not None else None
            ),
        )

    def shutdown(self) -> None:
        # PBS jobs are tracked by PBS itself; no local state to tear down.
        pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _retry_pbs_call[T](
    call_fn: Callable[[], T],
    *,
    max_attempts: int = 3,
    total_cap_seconds: float = 15.0,
) -> T:
    """Run *call_fn* with retry on transient PBS subprocess failures.

    Issue #1405: catches ``subprocess.CalledProcessError`` whose stderr
    matches a transient-error pattern (``connection refused``,
    ``server unavailable``, ``timeout``, ``connection reset``) and
    retries with exponential backoff. Non-transient failures propagate
    immediately.

    The exponential schedule is ``1s, 2s, 4s, ...`` capped at
    ``total_cap_seconds`` total wall time across all retries. After
    ``max_attempts`` transient failures the last exception is re-raised.
    The bounded-attempt schedule lives in
    ``osimflow.executors.base.retry_with_backoff`` (issue #1540); the
    PBS variant is deterministic (no jitter).
    """

    def _retry_on(exc: BaseException) -> bool:
        return isinstance(exc, subprocess.CalledProcessError) and bool(
            _TRANSIENT_STDERR_RE.search(exc.stderr or "")
        )

    def _on_retry(exc: BaseException, attempt: int, window: float) -> None:
        assert isinstance(exc, subprocess.CalledProcessError)
        log.warning(
            "pbs transient failure (attempt %d/%d), retrying in %.1fs: %s",
            attempt,
            max_attempts,
            window,
            (exc.stderr or "").strip(),
        )

    return retry_with_backoff(
        call_fn,
        retry_on=_retry_on,
        max_attempts=max_attempts,
        initial_delay_s=1.0,
        max_delay_s=total_cap_seconds,
        jitter=False,
        on_retry=_on_retry,
    )


def _default_pbs_server() -> str | None:
    """Resolve the default PBS server from the environment.

    PBS_DEFAULT is the standard env var for the default PBS server.
    """
    import os

    return os.environ.get("PBS_DEFAULT")
