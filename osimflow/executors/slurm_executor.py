"""Slurm executor for OSimFlow campaigns (issues #4, #1477 note in AGENTS.md).

Wraps ``submitit.AutoExecutor``: ``debug=True`` (the default) runs jobs
locally through ``submitit.DebugExecutor``; ``--slurm-real`` promotes to
a real cluster submission with per-call resource directives via
``_apply_slurm_params`` (submitit >= 1.5 kwarg-name tolerant). Extracted
from ``osimflow/executors/__init__.py`` (issue #1463).
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from typing import Any

from osimflow.executors.base import BaseExecutor, Handle
from osimflow.executors.transport import validate_transport_mode

log = logging.getLogger("osimflow.executors")


def _apply_slurm_params(
    auto_executor: Any,
    *,
    partition: str,
    account: str | None,
    cpus_per_task: int,
    mem_gb: int,
    time_min: int,
    qos: str | None = None,
    constraint: str | None = None,
    gres: str | None = None,
) -> None:
    """Apply Slurm parameters to a submitit AutoExecutor with cross-version
    kwarg compatibility.

    submitit 1.5+ renamed `partition` -> `slurm_partition`, `time` ->
    `slurm_time`, and added `slurm_qos` / `slurm_constraint` /
    `slurm_gres`. This helper tries the new spelling first; on
    `TypeError` (older submitit) it retries with the legacy kwarg
    names. `slurm_qos` / `slurm_constraint` / `slurm_gres` are silently
    dropped on legacy submitit (callers on old submitit cannot use the
    advanced flags). `None` values are filtered out so submitit does
    not receive explicit-None assignments for unset directives.
    """
    new_kwargs: dict[str, Any] = {
        "slurm_partition": partition,
        "slurm_account": account,
        "slurm_cpus_per_task": cpus_per_task,
        "slurm_mem_gb": mem_gb,
        "slurm_time": time_min,
        "slurm_qos": qos,
        "slurm_constraint": constraint,
        "slurm_gres": gres,
    }
    new_kwargs = {k: v for k, v in new_kwargs.items() if v is not None}
    legacy_kwargs: dict[str, Any] = {
        "partition": partition,
        "account": account,
        "cpus_per_task": cpus_per_task,
        "mem_gb": mem_gb,
        "time": time_min,
    }
    try:
        auto_executor.update_parameters(**new_kwargs)
    except TypeError:
        auto_executor.update_parameters(**legacy_kwargs)


class SlurmExecutor(BaseExecutor):
    """Slurm executor wrapping `submitit.AutoExecutor`.

    With `debug=True` (the default), submitit runs the job as a local
    subprocess *but still emits the exact `sbatch` command it would have
    run* in the log. This is the documented submitit pattern for
    development without a real cluster.

    In production, set `debug=False` and the same code path submits to
    the configured Slurm partition. Zero application changes.

    Per-submit resource directives (`cpus`, `memory_mb`, `time_min`) are
    honored by constructing a fresh `AutoExecutor` per submission with
    the desired overrides — submitit 1.5+ does not propagate per-call
    kwargs to its inner `update_parameters`. The result is that the
    per-sample cpus/mem/time show up in the actual sbatch header (when
    `debug=False` runs against a real cluster) or in the dry-run
    debug-logged script (when `debug=True`).

    Advanced directives (`qos`, `constraint`, `gres`) are forwarded to
    the executor-level sbatch header for advanced workloads (e.g. GPU
    jobs). All are optional; the corresponding `#SBATCH` line is omitted
    when unset. Requires submitit >= 1.5.
    """

    name = "slurm"

    #: Slurm's ``sbatch`` controller is not a hard-rate-limited API in
    #: the same way AWS Batch / Kubernetes are, but the cluster scheduler
    #: does choke on thousands of submissions per second. A 100 RPS
    #: default keeps the orchestrator polite on a shared cluster without
    #: slowing large fan-out runs visibly (issue #1563). Set to a lower
    #: value via ``--submit-rps`` for cluster benchmarks / conformance
    #: checks.
    default_submit_rps: float | None = 100.0

    def __init__(
        self,
        partition: str = "short",
        account: str | None = None,
        cpus_per_task: int = 2,
        mem_gb: int = 4,
        time_h: int = 2,
        debug: bool = True,
        qos: str | None = None,
        constraint: str | None = None,
        gres: str | None = None,
        *,
        submit_rps: float | None = None,
    ):
        # Lazy import so users who only ever run the local executor do
        # not pay the submitit import cost.
        import submitit  # noqa: PLC0415

        self._submitit = submitit

        self.partition = partition
        self.account = account
        self.cpus_per_task = cpus_per_task
        self.mem_gb = mem_gb
        self.time_h = time_h
        self.debug = debug
        self.qos = qos
        self.constraint = constraint
        self.gres = gres

        ex = submitit.AutoExecutor(
            folder=os.environ.get("OSIMFLOW_SLURM_LOGS", "/tmp/osimflow-slurm-logs"),
            slurm_pty=False,
        )
        _apply_slurm_params(
            ex,
            partition=partition,
            account=account,
            cpus_per_task=cpus_per_task,
            mem_gb=mem_gb,
            time_min=int(time_h * 60),
            qos=qos,
            constraint=constraint,
            gres=gres,
        )
        if debug:
            log.warning(
                "SlurmExecutor running in DEBUG mode — jobs run locally; "
                "the exact `sbatch` script that would have been submitted is "
                "logged at INFO level under the `submitit` logger. "
                "Set debug=False for real Slurm."
            )
        self._ex = ex
        # Issue #1563: shared rate limiter honouring the executor's
        # substrate-appropriate default. ``--submit-rps`` overrides it.
        self._init_rate_limiter(submit_rps)

    def _do_submit(
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
        # Issue #4: per-submit resource directives. submitit 1.5+ does
        # NOT propagate per-submit kwargs to its inner update_parameters
        # — `ex.submit(fn, **kwargs)` passes the kwargs to `fn` itself.
        # The submitit-recommended pattern for per-call overrides is to
        # build a fresh `AutoExecutor` per submission with the desired
        # parameters, then call `.submit()` on it. We do that here so
        # per-sample resources (different memory ceilings for a heavy
        # sample, etc.) are honored in the resulting sbatch header, not
        # just logged.
        # Unused fields: result_hint, remote_command, result_storage_*,
        # variables_json, env, stdout/stderr_path, max_retries,
        # worker_id — accepted for API compatibility but not consumed
        # locally.  result_transport_mode is validated against the
        # capability matrix (issue #1473) — slurm is in-band only.
        validate_transport_mode(self.name, result_transport_mode)
        if container:
            log.info(
                "slurm submit name=%s cpus=%d mem=%dMB time_min=%d container=%s",
                name,
                cpus,
                memory_mb,
                time_min,
                container,
            )
        else:
            log.info(
                "slurm submit name=%s cpus=%d mem=%dMB time_min=%d",
                name,
                cpus,
                memory_mb,
                time_min,
            )

        # Convert MB -> GB (rounded up so we never under-allocate).
        # submitit speaks integer GB on `slurm_mem_gb`; fractional
        # values are accepted on some versions but integer is the
        # safe cross-version path.
        mem_gb_override = max(1, (memory_mb + 1023) // 1024)

        # Build a fresh AutoExecutor for this submission so the
        # per-call resource directives flow into the rendered sbatch
        # header. The folder is shared (so log files cluster per
        # campaign), and the `slurm_pty=False` matches the init-time
        # config. debug=True uses submitit.DebugExecutor (local) which
        # also honors the per-submit overrides (they appear in the
        # debug-logged sbatch script).
        call_ex = self._submitit.AutoExecutor(
            folder=self._ex.folder,
            slurm_pty=False,
        )
        _apply_slurm_params(
            call_ex,
            partition=self.partition,
            account=self.account,
            cpus_per_task=cpus,
            mem_gb=mem_gb_override,
            time_min=time_min,
            qos=self.qos,
            constraint=self.constraint,
            gres=self.gres,
        )

        # Issue #654: capture values at closure-creation time to avoid
        # late-binding in fast loops where the outer scope changes before
        # _wrapped executes.
        os_version = str(openstudio_version or "N/A")
        cont = container
        fn_to_call = fn
        args_to_pass = args

        def _wrapped() -> Any:
            # Resource directive also becomes an env var so the task can
            # read it (e.g. OpenStudio CLI threading control).
            os.environ["OSIMFLOW_OS_VERSION"] = os_version
            if cont:
                os.environ["OSIMFLOW_CONTAINER"] = cont
            return fn_to_call(*args_to_pass)

        fut: Any = call_ex.submit(_wrapped)
        return Handle(
            job_id=str(fut.job_id),
            _future=fut,
            worker_id=str(fut.job_id),
            worker_ip=None,
            worker_region=None,
        )

    def shutdown(self) -> None:
        # submitit.AutoExecutor has no explicit shutdown; the underlying
        # DebugExecutor / SlurmExecutor cleans up on process exit.
        pass
