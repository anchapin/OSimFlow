"""Kubernetes executor for OSimFlow campaigns (issue #254, #997).

Wraps the Kubernetes Python client (`kubernetes`) to create a Job per
call, then polls the job status with exponential backoff until the
pod reaches a terminal state. The returned Handle carries the job name
and blocks on `.result()` until the task succeeds; on failure it
re-raises a RuntimeError with the pod's failure message.

Each Job runs the ephemeral-runner pattern (issue #996), mirroring
``NomadExecutor``: the container command defaults to
``python -m osimflow.remote_runner`` (or an explicit ``remote_command``
override), the serialized task payload travels in the
``OSIMFLOW_TASK_PAYLOAD`` env var, and the result-transport contract
travels in the ``OSIMFLOW_RESULT_TRANSPORT_MODE`` /
``OSIMFLOW_RESULT_STORAGE_*`` env vars. The runner decodes the payload,
executes the step work function in container-local storage, and pushes
results to object storage — no shared (RWX/NFS) volume is required.
When the transport mode is ``object_storage``, the handle downloads
the job-side artifacts via ``materialize_object_storage_result`` so
Campaign callbacks receive local paths (issue #996).

Resource directives (`cpus`, `memory_mb`, `time_min`) are mapped to
Kubernetes resource requests and limits. Per-sample
`OSIMFLOW_OS_VERSION` and `OSIMFLOW_CONTAINER` are carried as
environment variables — the same env vars `SlurmExecutor` and
`AWSBatchExecutor` export, so downstream work scripts can be
substrate-agnostic.

Native Job controls (issue #997): ``backoff_limit``,
``ttl_seconds_after_finished``, and the optional
``kueue.x-k8s.io/queue-name`` label are configurable. Defaults
preserve the pre-#997 manifest byte-for-byte (backoff=0, no TTL, no
extra labels) so existing campaigns run unchanged when the flags are
left unset.

Security: credentials are sourced from the in-cluster service account
or from `~/.kube/config`. The constructor does **not** accept explicit
credentials; using the configured kubeconfig or in-cluster service
account is the recommended path.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from concurrent.futures import Future
from typing import Any, cast

from osimflow.byos_contract import BYOS_CONTRACT_VERSION
from osimflow.executors.base import (
    BaseExecutor,
    Handle,
    PollingHandle,
    PollOutcome,
    poll_until_terminal,
)
from osimflow.executors.transport import (
    coerce_transport_mode,
    materialize_object_storage_result,
    resolve_result_for_callback,
)
from osimflow.task_payload_hmac import TASK_PAYLOAD_SECRET_ENV, build_signature_env

log = logging.getLogger("osimflow.executors.kubernetes")


class _KubernetesHandle(PollingHandle):
    """Handle that polls Kubernetes on `.result()`.

    The work runs in a remote Kubernetes Job (not a thread or submitit
    job), so we cannot back the Future with a local completion.
    Instead, the handle carries a reference to its executor and the
    job name; `result()` blocks on `_wait_for_terminal` and `done()`
    does a single non-blocking status check.

    The poll-deadline state machine lives in the shared
    ``PollingHandle`` base (issues #1464 / #1540); this class supplies
    only the Kubernetes-specific hooks below.

    Result transport (issue #996): when ``result_transport_mode`` is
    ``"object_storage"``, the job-side runner uploaded its artifacts to
    the configured bucket; ``result()`` downloads them to the hint's
    local paths via ``materialize_object_storage_result`` (same
    contract as ``_NomadHandle``) so Campaign callbacks receive local
    paths. For ``"shared_fs"`` / ``"auto"`` modes the hint is decoded
    through ``resolve_result_for_callback`` only.
    """

    def __init__(
        self,
        job_name: str,
        executor: KubernetesExecutor,
        submit_params: dict[str, Any],
        *,
        result_hint: Any = None,
        result_transport_mode: str = "auto",
        result_storage_backend: str | None = None,
        result_storage_bucket: str | None = None,
        result_storage_prefix: str | None = None,
        result_storage_endpoint: str | None = None,
    ) -> None:
        self.job_id = job_name
        self._job_name = job_name
        self._executor = executor
        self._submit_params = submit_params
        self._result_hint = result_hint
        self._result_transport_mode = coerce_transport_mode(result_transport_mode)
        self._result_storage_backend = result_storage_backend
        self._result_storage_bucket = result_storage_bucket
        self._result_storage_prefix = result_storage_prefix
        self._result_storage_endpoint = result_storage_endpoint
        self._future: Future[Any] = Future()
        self.worker_id: str | None = job_name
        self.worker_ip: str | None = None
        self.worker_region: str | None = None
        self.cost_usd: float | None = None
        self.billed_duration_seconds: float | None = None

    # ------------------------------------------------------------------
    # PollingHandle hooks (issues #1464 / #1540) — the shared state
    # machine in ``osimflow.executors.base.PollingHandle`` owns
    # ``result()``; ``KubernetesExecutor._wait_for_terminal`` owns the
    # poll skeleton via ``base.poll_until_terminal``.
    # ------------------------------------------------------------------

    def _wait_for_terminal(self, timeout: float | None) -> Any:
        # Issue #1465: ``timeout`` is the deadline for the whole call —
        # enforced by the executor poll loop. The Job's
        # ``activeDeadlineSeconds`` (when set) remains the
        # substrate-level kill (defense in depth).
        return self._executor._wait_for_terminal(self._job_name, timeout=timeout)  # noqa: SLF001

    def _classify(self, job: Any) -> tuple[PollOutcome, str | None]:
        phase = job.get("status", {}).get("phase", "")
        if phase == "Succeeded":
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
        phase = job.get("status", {}).get("phase", "")
        reason = self._extract_failure_reason(job)
        return RuntimeError(f"Kubernetes job {self._job_name!r} {phase}: {reason}")

    def done(self) -> bool:
        if self._future.done():
            return True
        try:
            pod_status = self._executor._get_pod_status(self._job_name)
            phase = pod_status.get("status", {}).get("phase", "")
            return phase in ("Succeeded", "Failed")
        except TimeoutError as exc:
            log.debug("Kubernetes done() timeout for job %s: %s", self._job_name, exc)
            return False
        except ConnectionError as exc:
            log.debug("Kubernetes done() connection error for job %s: %s", self._job_name, exc)
            return False
        except Exception as exc:
            status = getattr(exc, "status", 0)
            if status in (401, 403, 404):
                log.warning(
                    "Kubernetes done() permanent error for job %s: %s [status=%s]",
                    self._job_name,
                    exc,
                    status,
                )
                self._future.set_exception(exc)
                raise
            log.debug("Kubernetes done() transient error for job %s: %s", self._job_name, exc)
            return False

    def _extract_failure_reason(self, pod_status: dict[str, Any]) -> str:
        """Extract the most useful failure reason from a pod status."""
        containers = pod_status.get("status", {}).get("containerStatuses", []) or []
        for container in containers:
            state = container.get("state", {})
            terminated = state.get("terminated", {})
            if terminated:
                exit_code = terminated.get("exitCode", 0)
                if exit_code != 0:
                    reason = terminated.get("reason", "Unknown")
                    return f"exit code {exit_code} ({reason})"
            waiting = state.get("waiting", {})
            if waiting:
                reason = str(waiting.get("reason", "Unknown"))
                message = str(waiting.get("message", ""))
                if message:
                    return f"{reason}: {message}"
                return reason
        return "unknown"


class KubernetesExecutor(BaseExecutor):
    """Kubernetes executor for OSimFlow campaigns (issue #254).

        Wraps the Kubernetes Python client (`kubernetes`) to create a Job per
        call, then polls the job status with exponential backoff until the
        pod reaches a terminal state. The returned Handle carries the job
        name and blocks on `.result()` until the task succeeds; on failure
        it re-raises a RuntimeError.

        Each Job runs the ephemeral-runner pattern (issue #996), mirroring
        ``NomadExecutor``: the container command defaults to
        ``python -m osimflow.remote_runner`` (or an explicit
        ``remote_command`` override run via ``/bin/sh -c``), the task
        payload travels in ``OSIMFLOW_TASK_PAYLOAD``, and the
        result-transport contract in ``OSIMFLOW_RESULT_TRANSPORT_MODE`` /
        ``OSIMFLOW_RESULT_STORAGE_*`` env vars. Worker images must ship the
        ``osimflow`` package for the default command to resolve (see
        ``docs/kubernetes-deployment.md``).

        Resource directives (`cpus`, `memory_mb`, `time_min`) are mapped to
        Kubernetes resource requests and limits. Per-sample
        `OSIMFLOW_OS_VERSION` and `OSIMFLOW_CONTAINER` are carried as
        environment variables — the same env vars `SlurmExecutor` and
        `AWSBatchExecutor` export, so downstream work scripts can be
        substrate-agnostic.

    Security: credentials are sourced from the in-cluster service account
    or from `~/.kube/config`. The constructor does **not** accept explicit
    credentials; using the configured kubeconfig or in-cluster service
    account is the recommended path.

    Pod hardening (issue #1383): the constructor accepts a
    ``security_context_strict: bool = True`` flag. When strict (the
    default) the Job's pod is submitted with a hardened
    ``V1PodSecurityContext`` (``runAsNonRoot: true``, ``runAsUser: 1000``)
    and a hardened container ``V1SecurityContext``
    (``runAsNonRoot: true``, ``readOnlyRootFilesystem: true``,
    ``allowPrivilegeEscalation: false``, ``capabilities.drop: ["ALL"]``)
    plus ``automountServiceAccountToken: false`` on the ``V1PodSpec``.
    Together these block the cluster-default service-account token
    pivot and the writable-runtime container-escape path. Set
    ``security_context_strict=False`` to fall back to the legacy
    permissive manifest for clusters that reject the strict profile
    (e.g. older admission controllers without the required PodSecurity
    policy admission plugin).

    Secret delivery (issue #1449): the constructor accepts a
    ``payload_secret_ref: str | None = None`` naming a pre-created
    Kubernetes Secret that holds the task-payload HMAC shared secret
    under the key ``OSIMFLOW_TASK_PAYLOAD_SECRET``. When set (and a
    secret is configured on the orchestrator for signing), the Job's
    env entry for ``OSIMFLOW_TASK_PAYLOAD_SECRET`` is emitted as a
    ``secretKeyRef`` instead of a literal value, so the raw secret
    never appears in the Job spec — the kubelet resolves it at pod
    admission. Readers of ``kubectl get pod -o yaml``, etcd snapshots,
    and the API-server audit trail see only the Secret *name*. When
    unset (default), the secret ships as a literal env value exactly
    as before (backward compat). The signature
    (``OSIMFLOW_TASK_PAYLOAD_SIG``) always ships as a literal — it is
    public by design.

    The Kubernetes Python client is lazy-imported inside `__init__` so
    the local-executor / slurm-executor paths do not pay the import cost.
    """

    name = "kubernetes"

    @property
    def requires_remote_runner_payload(self) -> bool:
        return True

    signs_task_payload = True

    def __init__(
        self,
        namespace: str = "default",
        poll_interval_s: float = 5.0,
        max_poll_interval_s: float = 60.0,
        backoff_limit: int = 0,
        ttl_seconds_after_finished: int | None = None,
        queue_name: str | None = None,
        security_context_strict: bool = True,
        payload_secret_ref: str | None = None,
    ):
        self.namespace = namespace
        self.poll_interval_s = poll_interval_s
        self.max_poll_interval_s = max_poll_interval_s
        # Native Job controls (issue #997). Defaults preserve the
        # pre-#997 manifest byte-for-byte: backoff_limit=0, no TTL, no
        # extra labels. Setting ``backoff_limit`` > 0 enables K8s-native
        # pod retry as an alternative to ``--max-sample-retries`` (the
        # orchestrator-side retry loop); pick one, not both.
        self.backoff_limit = int(backoff_limit)
        self.ttl_seconds_after_finished = (
            int(ttl_seconds_after_finished) if ttl_seconds_after_finished is not None else None
        )
        # Kueue opt-in: applied as the ``kueue.x-k8s.io/queue-name`` label
        # on the Job's metadata. Inert on clusters without Kueue
        # installed (the label is harmless without the controller).
        self.queue_name = queue_name
        # Pod hardening (issue #1383). When True (default), the Job's
        # pod is submitted with a hardened ``V1PodSecurityContext``
        # (``runAsNonRoot: true``, ``runAsUser: 1000``), a hardened
        # container ``V1SecurityContext`` (``runAsNonRoot: true``,
        # ``readOnlyRootFilesystem: true``,
        # ``allowPrivilegeEscalation: false``, ``capabilities.drop:
        # ["ALL"]``) and ``automountServiceAccountToken: false`` on the
        # ``V1PodSpec``. Together these block the cluster-default
        # service-account token pivot and the writable-runtime
        # container-escape path. Set to False to fall back to the
        # legacy permissive manifest for clusters that reject the
        # strict profile (e.g. older admission controllers).
        self.security_context_strict = bool(security_context_strict)
        # Issue #1449: native secret delivery. When set, the env entry
        # for ``OSIMFLOW_TASK_PAYLOAD_SECRET`` is emitted as a
        # ``secretKeyRef`` against this user-provided Secret name
        # instead of a literal value, so the raw secret never appears
        # in the Job spec (readable via ``kubectl get pod -o yaml``,
        # etcd snapshots, and the API-server audit trail). ``None``
        # (default) preserves the pre-#1449 literal-env behaviour.
        self.payload_secret_ref = payload_secret_ref
        # Issue #1081: digest pinning. Initialized in the constructor so
        # ``_resolve_container_image`` is callable without going through
        # ``submit()`` (e.g. unit tests); overridden by ``submit()``.
        self._container_digest: str | None = None
        self._client: Any = None
        # Issue #1331: cached version negotiation results.
        self._negotiated_versions: list[str] | None = None
        self._negotiated_image: str | None = None

    def _get_client(self) -> Any:
        """Lazy Kubernetes client construction using config.load_kube_config
        or config.load_incluster_config.
        """
        if self._client is None:
            from kubernetes import client, config

            try:
                config.load_kube_config()
            except Exception:
                config.load_incluster_config()
            self._client = client.BatchV1Api()
        return self._client

    def _check_contract_version_compatibility(
        self,
        container: str | None,
        container_digest: str | None,
        openstudio_version: str | None,
    ) -> None:
        """Verify BYOS contract version compatibility before submitting (issue #1331).

        Queries the remote runner for its supported contract versions and raises
        ``RuntimeError`` if the orchestrator's ``BYOS_CONTRACT_VERSION`` is not
        in the supported list.

        This enables fail-fast at submission time rather than discovering a
        version mismatch at runtime inside the container.
        """
        supported = self.negotiate_contract_version(
            container=container,
            container_digest=container_digest,
            openstudio_version=openstudio_version,
        )
        if BYOS_CONTRACT_VERSION not in supported:
            raise RuntimeError(
                f"BYOS contract version mismatch: orchestrator has "
                f"version {BYOS_CONTRACT_VERSION!r} but remote runner "
                f"only supports {supported!r}. Cannot submit work to this "
                f"container image. Ensure the container image matches the "
                f"osimflow version used by the orchestrator (issue #1331)."
            )

    def _build_job_name(self, name: str) -> str:
        """Build a valid Kubernetes job name from the task name.

        Kubernetes job names must match DNS-1123 subdomain naming rules:
        lowercase alphanumeric + hyphens, max 253 chars, start/end with
        alphanumeric.
        """
        safe_name = name.lower().replace("_", "-")[:253].strip("-")
        if not safe_name:
            safe_name = "osimflow-task"
        return f"osimflow-{safe_name}"

    def _signature_env_entries(self, task_payload: str) -> list[dict[str, Any]]:
        """Return env entries for the task-payload signature pair (issues #1177, #1449).

        When a shared secret is configured, sign the exact payload bytes
        and propagate secret + signature so the remote_runner verifies
        before decoding/executing. No-op (empty list) in legacy unsigned
        mode. When a ``payload_secret_ref`` is configured, the secret
        entry is emitted as a ``secretKeyRef`` instead of a literal env
        value so the raw secret never appears in the Job spec; the
        signature still ships as a literal (public by design).
        """
        signature_env = build_signature_env(task_payload)
        payload_secret_ref = getattr(self, "payload_secret_ref", None)
        if payload_secret_ref and not signature_env:
            log.warning(
                "payload_secret_ref=%r is configured but no %s is set "
                "on the orchestrator, so the payload cannot be signed; "
                "submitting unsigned (legacy mode). Set %s on the "
                "orchestrator — and the same value in the referenced "
                "Secret — to enable HMAC verification (issue #1449).",
                payload_secret_ref,
                TASK_PAYLOAD_SECRET_ENV,
                TASK_PAYLOAD_SECRET_ENV,
            )
        entries: list[dict[str, Any]] = []
        for key, value in signature_env.items():
            if key == TASK_PAYLOAD_SECRET_ENV and payload_secret_ref:
                entries.append(
                    {
                        "name": key,
                        "valueFrom": {
                            "secretKeyRef": {
                                "name": payload_secret_ref,
                                "key": TASK_PAYLOAD_SECRET_ENV,
                            }
                        },
                    }
                )
            else:
                entries.append({"name": key, "value": value})
        return entries

    def _build_environment(
        self,
        *,
        container: str | None,
        openstudio_version: str | None,
        task_payload: str | None = None,
        result_transport_mode: str | None = None,
        result_storage_backend: str | None = None,
        result_storage_bucket: str | None = None,
        result_storage_prefix: str | None = None,
        result_storage_endpoint: str | None = None,
    ) -> list[dict[str, Any]]:
        """Build environment variables for the container.

        Mirrors the ``NomadExecutor._build_job_spec`` env block: the
        serialized task payload travels in ``OSIMFLOW_TASK_PAYLOAD`` and
        the result-transport contract in the ``OSIMFLOW_RESULT_*`` vars
        so ``osimflow.remote_runner`` can execute the step and push
        results to object storage (issue #996). ``OSIMFLOW_STUB_SIM``
        is propagated from the orchestrator environment when set so
        remote pods honour the orchestrator's stub-vs-real CLI choice.

        Entries are ``{"name": ..., "value": ...}`` dicts; in
        ``secretKeyRef`` mode (issue #1449) the
        ``OSIMFLOW_TASK_PAYLOAD_SECRET`` entry instead carries
        ``{"name": ..., "valueFrom": {"secretKeyRef": {...}}}`` so the
        kubelet resolves the secret at pod admission.
        """
        env: list[dict[str, Any]] = []
        if openstudio_version is not None:
            env.append({"name": "OSIMFLOW_OS_VERSION", "value": str(openstudio_version)})
        # Issue #1081: a pinned SHA256 digest overrides the mutable tag
        # for the OSIMFLOW_CONTAINER env var that remote_runner reads.
        container_digest = getattr(self, "_container_digest", None)
        if container_digest is not None:
            resolved = container_digest
        else:
            resolved = container or f"nrel/openstudio:{openstudio_version or 'latest'}"
        env.append({"name": "OSIMFLOW_CONTAINER", "value": resolved})
        if task_payload is not None:
            env.append({"name": "OSIMFLOW_TASK_PAYLOAD", "value": task_payload})
            # Issue #1281: verify BYOS contract version compatibility between
            # orchestrator and remote runner.
            env.append({"name": "OSIMFLOW_CONTRACT_VERSION", "value": BYOS_CONTRACT_VERSION})
            # Issues #1177 / #1449: signature pair (see
            # ``_signature_env_entries`` — the secret entry becomes a
            # ``secretKeyRef`` when ``payload_secret_ref`` is set).
            env.extend(self._signature_env_entries(task_payload))
        if result_transport_mode is not None:
            env.append({"name": "OSIMFLOW_RESULT_TRANSPORT_MODE", "value": result_transport_mode})
        if result_storage_backend is not None:
            env.append({"name": "OSIMFLOW_RESULT_STORAGE_BACKEND", "value": result_storage_backend})
        if result_storage_bucket is not None:
            env.append({"name": "OSIMFLOW_RESULT_STORAGE_BUCKET", "value": result_storage_bucket})
        if result_storage_prefix is not None:
            env.append({"name": "OSIMFLOW_RESULT_STORAGE_PREFIX", "value": result_storage_prefix})
        if result_storage_endpoint is not None:
            env.append(
                {"name": "OSIMFLOW_RESULT_STORAGE_ENDPOINT", "value": result_storage_endpoint}
            )
        stub_sim = os.environ.get("OSIMFLOW_STUB_SIM")
        if stub_sim is not None:
            env.append({"name": "OSIMFLOW_STUB_SIM", "value": stub_sim})
        return env

    def _wait_for_terminal(self, job_name: str, timeout: float | None = None) -> dict[str, Any]:
        """Poll job status with exponential backoff until terminal state.

        Returns the pod status dict for the job's pod.

        The poll skeleton (deadline, deadline clamping (sleep capped at the remaining budget),
        capped exponential growth) lives in
        ``osimflow.executors.base.poll_until_terminal`` (issue #1540);
        the Kubernetes loop grows the delay before sleeping and
        tolerates transient pod-status probe errors.

        Raises:
            TimeoutError: if *timeout* seconds elapse before a terminal state.
        """
        return poll_until_terminal(
            lambda: self._get_pod_status(job_name),
            is_terminal=lambda pod_status: (
                pod_status.get("status", {}).get("phase", "") in ("Succeeded", "Failed")
            ),
            timeout=timeout,
            timeout_message=lambda elapsed: (
                f"Timed out after {elapsed:.1f}s waiting for job {job_name!r}"
            ),
            poll_interval_s=self.poll_interval_s,
            max_poll_interval_s=self.max_poll_interval_s,
            on_pending=lambda pod_status, delay, _sleep_amount: log.info(
                "kubernetes poll job=%s phase=%s (sleeping %.1fs)",
                job_name,
                pod_status.get("status", {}).get("phase", ""),
                delay,
            ),
            tolerate_probe_errors=True,
            on_probe_error=lambda exc, delay: log.warning(
                "error getting pod status for %s: %s (sleeping %.1fs)",
                job_name,
                exc,
                delay,
            ),
            grow_before_sleep=True,
        )

    def _get_pod_status(self, job_name: str) -> dict[str, Any]:
        """Get the pod status for a job's pod.

        Lists pods matching the job selector and returns the first one.
        """
        client = self._get_client()
        label_selector = f"job-name={job_name}"
        pods = client.list_namespaced_pod(
            namespace=self.namespace,
            label_selector=label_selector,
        )
        if not pods.items:
            return {"status": {"phase": "Pending"}}
        return cast(dict[str, Any], pods.items[0].to_dict())

    def _submit_job(
        self,
        *,
        name: str,
        cpus: int,
        memory_mb: int,
        time_min: int,
        environment: list[dict[str, Any]],
        command: list[str] | None = None,
    ) -> str:
        """Submit a Kubernetes Job and return the job name.

        ``command`` is the container entrypoint. It defaults to the
        ephemeral runner (``python -m osimflow.remote_runner``) so each
        Job executes real campaign work (issue #996); an explicit
        ``remote_command`` override arrives here as
        ``["/bin/sh", "-c", remote_command]``.
        """
        from kubernetes import client

        job_name = self._build_job_name(name)
        container_image = "nrel/openstudio:latest"
        for e in environment:
            if e.get("name") == "OSIMFLOW_CONTAINER" and e.get("value") is not None:
                container_image = e["value"]
                break

        # Issue #1449: entries carrying ``valueFrom`` become native
        # ``secretKeyRef`` sources so the kubelet resolves the secret at
        # pod admission — the raw value is never serialized into the Job
        # spec. Literal entries map to plain values as before.
        env_vars: list[Any] = []
        for e in environment:
            if "valueFrom" in e:
                ref = e["valueFrom"]["secretKeyRef"]
                env_vars.append(
                    client.V1EnvVar(
                        name=e["name"],
                        value_from=client.V1EnvVarSource(
                            secret_key_ref=client.V1SecretKeySelector(
                                name=ref["name"],
                                key=ref["key"],
                            ),
                        ),
                    )
                )
            else:
                env_vars.append(client.V1EnvVar(name=e["name"], value=e["value"]))

        resources = client.V1ResourceRequirements(
            requests={"cpu": str(cpus), "memory": f"{memory_mb}Mi"},
            limits={"cpu": str(cpus), "memory": f"{memory_mb}Mi"},
        )

        # Pod hardening (issue #1383). Strict mode emits a hardened
        # container ``V1SecurityContext`` (``runAsNonRoot: true``,
        # ``readOnlyRootFilesystem: true``,
        # ``allowPrivilegeEscalation: false``,
        # ``capabilities.drop: ["ALL"]``) so a compromised remote-runner
        # image cannot pivot via the writable runtime path. Relaxed
        # mode (legacy clusters without the strict admission profile)
        # emits no container security context at all.
        container_security_context: client.V1SecurityContext | None
        pod_security_context: client.V1PodSecurityContext | None
        pod_automount_token: bool | None
        if self.security_context_strict:
            container_security_context = client.V1SecurityContext(
                run_as_non_root=True,
                read_only_root_filesystem=True,
                allow_privilege_escalation=False,
                capabilities=client.V1Capabilities(drop=["ALL"]),
            )
            # Pod-level ``runAsNonRoot`` + a fixed non-zero UID enforce
            # the same invariant at the Pod boundary, so init containers
            # and sidecars inherit the non-root guarantee. ``runAsUser``
            # must be > 0 (Kubernetes rejects UID 0 with
            # ``runAsNonRoot: true``); 1000 is the standard
            # first-user UID on the Debian/Ubuntu base images used by
            # ``nrel/openstudio``.
            pod_security_context = client.V1PodSecurityContext(
                run_as_non_root=True,
                run_as_user=1000,
            )
            # ``automountServiceAccountToken: false`` blocks the
            # cluster-default service-account token pivot that issue
            # #1177 raised in the threat model. The flag lives on the
            # ``V1PodSpec`` (not the ``V1PodSecurityContext``) per the
            # Kubernetes API spec.
            pod_automount_token = False
        else:
            container_security_context = None
            pod_security_context = None
            pod_automount_token = None

        container = client.V1Container(
            name="osimflow",
            image=container_image,
            command=command or ["python", "-m", "osimflow.remote_runner"],
            env=env_vars,
            resources=resources,
            security_context=container_security_context,
        )

        pod_spec_kwargs: dict[str, Any] = {
            "containers": [container],
            "restart_policy": "Never",
            "security_context": pod_security_context,
        }
        if pod_automount_token is not None:
            pod_spec_kwargs["automount_service_account_token"] = pod_automount_token

        template = client.V1PodTemplateSpec(
            spec=client.V1PodSpec(**pod_spec_kwargs),
        )

        # Native Job controls (issue #997): plumb ``backoff_limit`` /
        # ``ttl_seconds_after_finished`` from the executor instance onto
        # the V1JobSpec. Defaults preserve the pre-#997 manifest byte
        # representation: backoff_limit=0, ttl_seconds_after_finished
        # absent. The Kueue ``queue-name`` label is added on the
        # metadata only when the executor was constructed with a
        # ``queue_name`` — leaving the metadata untouched by default
        # so behavior is unchanged without opt-in.
        job_spec_kwargs: dict[str, Any] = {
            "template": template,
            "backoff_limit": self.backoff_limit,
            "active_deadline_seconds": int(time_min) * 60 if time_min > 0 else None,
        }
        if self.ttl_seconds_after_finished is not None:
            job_spec_kwargs["ttl_seconds_after_finished"] = self.ttl_seconds_after_finished

        metadata_kwargs: dict[str, Any] = {"name": job_name}
        if self.queue_name:
            metadata_kwargs["labels"] = {"kueue.x-k8s.io/queue-name": self.queue_name}

        job = client.V1Job(
            api_version="batch/v1",
            kind="Job",
            metadata=client.V1ObjectMeta(**metadata_kwargs),
            spec=client.V1JobSpec(**job_spec_kwargs),
        )

        client = self._get_client()
        client.create_namespaced_job(namespace=self.namespace, body=job)

        log.info(
            "kubernetes submit_job -> job=%s namespace=%s",
            job_name,
            self.namespace,
        )
        return job_name

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
        del variables_json, env, stdout_path, stderr_path, max_retries, worker_id, kwargs  # noqa: F841, ARG002

        log.info(
            "kubernetes submit name=%s cpus=%d mem=%dMB time_min=%d container=%s",
            name,
            cpus,
            memory_mb,
            time_min,
            container,
        )

        # Issue #1331: fail-fast version negotiation before creating any pods.
        self._check_contract_version_compatibility(
            container=container,
            container_digest=container_digest,
            openstudio_version=openstudio_version,
        )

        # Ephemeral-runner contract (issue #996), mirroring NomadExecutor:
        # serialize the step call into the task payload; the Job-side
        # ``python -m osimflow.remote_runner`` decodes it and executes the
        # work function in container-local storage.
        step_name = self._infer_step_name(name)
        task_payload = self._build_task_payload(
            step_name=step_name,
            args=args,
            kwargs={},
            result_hint=result_hint,
            name=name,
        )
        del fn  # noqa: ARG002 — work runs inside the Job container

        if remote_command:
            command: list[str] = ["/bin/sh", "-c", remote_command]
        else:
            command = ["python", "-m", "osimflow.remote_runner"]

        environment = self._build_environment(
            container=container,
            openstudio_version=openstudio_version,
            task_payload=task_payload,
            result_transport_mode=(
                str(result_transport_mode) if result_transport_mode is not None else None
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

        submit_params: dict[str, Any] = {
            "name": name,
            "cpus": cpus,
            "memory_mb": memory_mb,
            "time_min": time_min,
            "environment": environment,
            "command": command,
        }
        job_name = self._submit_job(**submit_params)

        return _KubernetesHandle(
            job_name=job_name,
            executor=self,
            submit_params=submit_params,
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

    def negotiate_contract_version(
        self,
        container: str | None = None,
        container_digest: str | None = None,
        openstudio_version: str | None = None,
    ) -> list[str]:
        """Query the remote runner for its supported BYOS contract versions (issue #1331).

        Creates a minimal pod that runs ``python -m osimflow.remote_runner --negotiate-version``
        in the target container image, waits for completion, and returns the list of
        supported contract versions. This allows the Campaign to fail fast at submission
        time rather than discovering a version mismatch at runtime inside the container.

        Arguments ``container``, ``container_digest``, and ``openstudio_version`` determine
        the image to query. If not provided, the method uses cached values from a prior
        ``submit()`` call or falls back to ``nrel/openstudio:latest``.

        The result is cached on the instance after the first call, so subsequent calls
        with the same image do not create additional pods.

        Returns
        -------
        list[str]
            Supported BYOS contract version strings, e.g. ``["1.0.0"]``.

        Raises
        ------
        RuntimeError
            If the version negotiation fails (pod creation, timeout, or incompatible versions).
        """
        if getattr(self, "_negotiated_versions", None) is not None:
            cached_image = getattr(self, "_negotiated_image", None)
            current_image = self._resolve_container_image(
                container, container_digest, openstudio_version
            )
            if cached_image == current_image:
                return cast("list[str]", self._negotiated_versions)

        import json
        import uuid

        from kubernetes import client

        container_image = self._resolve_container_image(
            container, container_digest, openstudio_version
        )
        job_name = f"osimflow-version-check-{uuid.uuid4().hex[:8]}"

        env_vars = [
            client.V1EnvVar(name="OSIMFLOW_TASK_PAYLOAD", value="{}"),
        ]

        # Apply the same pod hardening (issue #1383) as the real
        # submission path: the version-check pod runs the same image
        # with the same SA, so omitting the security context here would
        # leave a privilege-escalation gap on every campaign startup.
        version_check_container: client.V1Container = client.V1Container(
            name="osimflow",
            image=container_image,
            command=["python", "-m", "osimflow.remote_runner", "--negotiate-version"],
            env=env_vars,
            resources=client.V1ResourceRequirements(
                requests={"cpu": "100m", "memory": "64Mi"},
                limits={"cpu": "100m", "memory": "64Mi"},
            ),
        )
        version_check_pod_kwargs: dict[str, Any] = {
            "containers": [version_check_container],
            "restart_policy": "Never",
        }
        if self.security_context_strict:
            version_check_container.security_context = client.V1SecurityContext(
                run_as_non_root=True,
                read_only_root_filesystem=True,
                allow_privilege_escalation=False,
                capabilities=client.V1Capabilities(drop=["ALL"]),
            )
            version_check_pod_kwargs["security_context"] = client.V1PodSecurityContext(
                run_as_non_root=True,
                run_as_user=1000,
            )
            version_check_pod_kwargs["automount_service_account_token"] = False

        pod_spec = client.V1PodSpec(**version_check_pod_kwargs)

        pod = client.V1Pod(
            api_version="v1",
            kind="Pod",
            metadata=client.V1ObjectMeta(
                name=job_name,
                namespace=self.namespace,
                labels={"osimflow-version-check": "true"},
            ),
            spec=pod_spec,
        )

        core_api = self._get_core_api()
        try:
            core_api.create_namespaced_pod(namespace=self.namespace, body=pod)
        except Exception as exc:
            raise RuntimeError(
                f"failed to create version-check pod for image {container_image!r}: {exc}"
            ) from exc

        try:
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                try:
                    status = core_api.read_namespaced_pod_status(
                        name=job_name, namespace=self.namespace
                    )
                    phase = status.status.phase if status.status else "Pending"
                    if phase == "Succeeded":
                        logs = core_api.read_namespaced_pod_log(
                            name=job_name,
                            namespace=self.namespace,
                            container="osimflow",
                        )
                        try:
                            parsed = json.loads(logs.strip())
                            if not parsed.get("ok"):
                                raise RuntimeError(f"version check returned error: {parsed}")
                            self._negotiated_versions = parsed.get("supported_versions", [])
                            self._negotiated_image = container_image
                            return self._negotiated_versions
                        except json.JSONDecodeError as exc:
                            raise RuntimeError(
                                f"invalid JSON from version-check pod: {logs!r}"
                            ) from exc
                    elif phase in ("Failed", "Error"):
                        raise RuntimeError(
                            f"version-check pod {phase.lower()} for image {container_image!r}"
                        )
                except Exception as exc:
                    log.debug("version-check pod status poll error (retrying): %s", exc)
                time.sleep(2)
            raise RuntimeError(
                f"version-check pod timed out after 60s for image {container_image!r}"
            )
        finally:
            try:
                core_api.delete_namespaced_pod(
                    name=job_name,
                    namespace=self.namespace,
                    body=client.V1DeleteOptions(),
                )
            except Exception as exc:
                log.debug("failed to delete version-check pod %s: %s", job_name, exc)

    def _get_core_api(self) -> Any:
        """Return the CoreV1Api client (lazy)."""
        from kubernetes import client

        return client.CoreV1Api()

    def _resolve_container_image(
        self,
        container: str | None,
        container_digest: str | None,
        openstudio_version: str | None,
    ) -> str:
        """Return the container image to use for version checking.

        Mirrors the resolution logic in ``_build_environment``.
        """
        if container_digest is not None:
            return container_digest
        if container is not None:
            return container
        return f"nrel/openstudio:{openstudio_version or 'latest'}"

    def shutdown(self) -> None:
        pass
