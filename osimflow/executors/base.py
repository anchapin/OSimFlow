"""Shared executor primitives: BaseExecutor and Handle.

These are in a separate module to avoid circular import issues when
per-executor modules (e.g. azure_batch_executor.py) need to inherit
from them before the full __init__.py is initialized.
"""

from __future__ import annotations

__all__ = ["BaseExecutor", "Handle", "SubmitRequest"]

import abc
import dataclasses
import json
from collections.abc import Callable
from concurrent.futures import Future
from typing import Any

from osimflow.executors.transport import encode_transport_value


@dataclasses.dataclass
class Handle:
    """A future-like handle. Substrate-specific implementations subclass this.

    The Handle abstracts over both `concurrent.futures.Future` (local)
    and `submitit.Future` (Slurm), whose `.result()` and `.done()`
    signatures differ slightly. We unify them here.

    Worker tracking fields (issue #105) are populated by each executor
    at submit time so the Campaign can attribute every sample to the
    worker that processed it — essential for cost attribution and
    debugging large campaigns.

    Error tracking (issue #721): polling errors are captured here so
    callers can distinguish "still running" from "failed with error".
    """

    job_id: str
    _future: Future[Any]
    worker_id: str | None = None
    worker_ip: str | None = None
    worker_region: str | None = None
    cost_usd: float | None = None
    billed_duration_seconds: float | None = None
    error: Exception | None = None

    def result(self, timeout: float | None = None) -> Any:
        if self.error is not None:
            raise self.error
        try:
            return self._future.result(timeout=timeout)
        except TypeError:
            return self._future.result()

    def done(self) -> bool:
        try:
            return self._future.done()
        except AttributeError:
            return getattr(self._future, "_completed", False)

    def is_failed(self) -> bool:
        """Return True if a polling error has been captured."""
        return self.error is not None


@dataclasses.dataclass
class SubmitRequest:
    """Structured submit request replacing implicit kwargs (issue #725, #1273).

    Construct and pass a ``SubmitRequest`` to ``BaseExecutor.submit()`` instead
    of passing keyword arguments.  This enforces field completeness at the
    type-checker level: missing required fields become mypy errors rather
    than silent runtime failures when executors receive unexpected ``**kwargs``.

    Example::

        request = SubmitRequest(
            fn=run_openstudio_sim,
            args=(mod_pkg, sid, os_version, out_dir),
            name=f"sim_{sid}",
            cpus=4,
            memory_mb=8 * 1024,
            time_min=240,
            container="nrel/openstudio:3.11.0",
        )
        handle = executor.submit_request(request)
    """

    fn: Callable[..., Any]
    """The callable to execute."""

    args: tuple[Any, ...] = ()
    """Positional arguments passed to *fn*."""

    name: str = "task"
    cpus: int = 1
    memory_mb: int = 1024
    time_min: int = 60
    container: str | None = None
    container_digest: str | None = None
    openstudio_version: str | None = None
    result_hint: Any = None
    remote_command: str | None = None
    result_transport_mode: str | None = None
    result_storage_backend: str | None = None
    result_storage_bucket: str | None = None
    result_storage_prefix: str | None = None
    result_storage_endpoint: str | None = None
    variables_json: str | None = None
    env: dict[str, str] | None = None
    stdout_path: Any = None
    stderr_path: Any = None
    max_retries: int | None = None
    worker_id: str | None = None


class BaseExecutor(abc.ABC):
    """All executors conform to this interface."""

    name: str = "base"

    @property
    def requires_remote_runner_payload(self) -> bool:
        """Whether this executor dispatches work via ``python -m osimflow.remote_runner``.

        Executors that use a remote-runner payload (e.g. Nomad, Kubernetes)
        marshal step calls into ``OSIMFLOW_TASK_PAYLOAD`` and execute them
        inside a job container.  Other executors (Local, Slurm, AWS Batch, etc.)
        invoke work scripts directly and never need the payload path.

        Override in subclasses that differ from the default.
        """
        return False

    @abc.abstractmethod
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
    ) -> Handle: ...

    def submit_request(self, request: SubmitRequest) -> Handle:
        """Submit a structured request (preferred over raw kwargs, issue #725).

        This is the type-safe path for ``executor.submit()`` calls.
        All fan-out submit calls in campaign.py should use this method
        rather than passing implicit ``**kwargs`` to ``submit()``.

        Args:
            request: A ``SubmitRequest`` dataclass describing the callable,
                positional args, and resource requirements.

        Returns:
            A ``Handle`` that can be used to retrieve the result.

        Raises:
            TypeError: If called with any kwargs (enforced by the
                ``submit_request`` overload signature).
        """
        return self.submit(
            request.fn,
            *request.args,
            name=request.name,
            cpus=request.cpus,
            memory_mb=request.memory_mb,
            time_min=request.time_min,
            container=request.container,
            container_digest=request.container_digest,
            openstudio_version=request.openstudio_version,
            result_hint=request.result_hint,
            remote_command=request.remote_command,
            result_transport_mode=request.result_transport_mode,
            result_storage_backend=request.result_storage_backend,
            result_storage_bucket=request.result_storage_bucket,
            result_storage_prefix=request.result_storage_prefix,
            result_storage_endpoint=request.result_storage_endpoint,
            variables_json=request.variables_json,
            env=request.env,
            stdout_path=request.stdout_path,
            stderr_path=request.stderr_path,
            max_retries=request.max_retries,
            worker_id=request.worker_id,
        )

    @abc.abstractmethod
    def shutdown(self) -> None: ...

    def cancel(self) -> None:
        """Cancel all active futures (issue #255).

        Override in subclasses that manage their own job queues
        (Slurm, AWS Batch) to send cancellation signals to the
        underlying substrate. The base implementation is a no-op
        for executors that do not need explicit cancellation.
        """
        return None

    def fanout_submit_chunk_size(self, total: int) -> int:
        """Return the bounded chunk size for fan-out submission.

        Override in executors that need to limit the number of
        concurrent submissions (e.g., Nomad's rate-limiting).
        The default returns *total* (no chunking).
        """
        return total

    def get_bounded_fanout_chunk_size(self, total: int) -> int:
        """Return the bounded chunk size; delegates to fanout_submit_chunk_size.

        This method exists so NomadExecutor can expose a
        fanout_submit_chunk_size property without losing the bounded
        computation. Subclasses that override fanout_submit_chunk_size
        should not need to override this.
        """
        return self.fanout_submit_chunk_size(total)

    def fanout_submit_interval_s(self) -> float:
        """Return the per-submit pacing interval for fan-out submission.

        Override in executors that need to pace submissions
        (e.g., Nomad's submit rate limiting). The default
        returns 0.0 (no pacing).
        """
        return 0.0

    # --- Shared remote-runner payload helpers (issue #1168) ---
    # Used by executors that dispatch via ``python -m osimflow.remote_runner``
    # (Nomad, Kubernetes, AWS Batch, Azure Batch, Google Batch, Docker Swarm).

    @staticmethod
    def _infer_step_name(submit_name: str) -> str:
        """Map a submit name to the remote_runner step identifier.

        The Campaign names fan-out tasks ``apply_<sid>`` / ``sim_<sid>``
        / ``kpi_<sid>`` and the single-shot steps ``aggregate`` / ``plots``;
        the remote runner resolves the work function from the step identifier.
        """
        lower = submit_name.lower()
        if lower.startswith("apply_"):
            return "apply"
        if lower.startswith("sim_"):
            return "sim"
        if lower.startswith("kpi_"):
            return "extract"
        if lower.startswith("aggregate"):
            return "aggregate"
        if lower.startswith("plots"):
            return "plots"
        return "unknown"

    @staticmethod
    def _encode_payload_value(value: Any) -> Any:  # noqa: ANN401
        """Encode Python values for transport-safe JSON payloads."""
        return encode_transport_value(value)

    @staticmethod
    def _build_task_payload(
        *,
        step_name: str,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        result_hint: Any,  # noqa: ANN401
        name: str,
    ) -> str:
        """Serialize the step call for the ephemeral runner.

        Uses the shared serialization so ``osimflow.remote_runner`` can
        decode any executor's Jobs identically (issue #996).
        """
        payload = {
            "schema_version": 1,
            "name": name,
            "step": step_name,
            "args": [BaseExecutor._encode_payload_value(a) for a in args],
            "kwargs": {k: BaseExecutor._encode_payload_value(v) for k, v in kwargs.items()},
            "result_hint": BaseExecutor._encode_payload_value(result_hint),
        }
        return json.dumps(payload)
