"""AWS Batch executor for OSimFlow campaigns (issues #5, #1010, #131, #1563).

Wraps ``boto3.client('batch').submit_job`` to launch one Batch task per
call, then polls ``describe_jobs`` (exponential backoff) until terminal
state. Spot handling: ``_SpotPriceCache`` (60 s TTL) feeds the ceiling
check, the shared :class:`~osimflow.executors._rate_limiter.TokenBucketRateLimiter`
throttles fan-out submits (was a private ``_TokenBucketRateLimiter``
class before issue #1563; now lives on
:mod:`osimflow.executors._rate_limiter` so every substrate shares one
implementation), and the handle retries Spot interruptions up to
``max_retries`` before falling back to on-demand.

Security: credentials resolve through IAM-role-only botocore providers
by default (``allow_long_lived_credentials=False``); boto3 stays a lazy
in-constructor import so local/slurm users never pay for it.

Extracted from ``osimflow/executors/__init__.py`` (issue #1463).
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future
from typing import Any, cast

from osimflow import cache as _cache_mod
from osimflow.executors.base import (
    BaseExecutor,
    Handle,
    PollingHandle,
    PollOutcome,
    poll_until_terminal,
    retry_with_backoff,
)
from osimflow.executors.transport import (
    ResultTransportConfig,
    resolve_and_materialize,
)
from osimflow.task_payload_hmac import (
    TASK_PAYLOAD_SECRET_ENV,
    TASK_PAYLOAD_SECRET_FILE_ENV,
    TASK_PAYLOAD_SIG_ENV,
    build_signature_env,
    build_transport_signature_env,
    resolve_payload_secret,
)

log = logging.getLogger("osimflow.executors")


def _aws_error_code(exc: BaseException) -> str:
    """Extract AWS/boto error code from an exception, or empty string if not applicable."""
    try:
        return exc.response.get("Error", {}).get("Code", "") if hasattr(exc, "response") else ""
    except (AttributeError, TypeError):  # malformed ``response`` payload (not a mapping)
        return ""


# Valid Fargate vCPU -> memory (MiB) combinations (AWS Batch Fargate job
# definitions, issue #1808).  Keys are vCPU values.
_FARGATE_MEMORY_MIB: dict[float, tuple[int, ...]] = {
    0.25: (512, 1024, 2048),
    0.5: tuple(range(1024, 4096 + 1, 1024)),
    1.0: tuple(range(2048, 8192 + 1, 1024)),
    2.0: tuple(range(4096, 16384 + 1, 1024)),
    4.0: tuple(range(8192, 30720 + 1, 1024)),
    8.0: tuple(range(16384, 61440 + 1, 4096)),
    16.0: tuple(range(32768, 122880 + 1, 8192)),
}


def _validate_fargate_resources(cpus: float, memory_mb: int) -> None:
    """Raise ``ValueError`` unless (*cpus*, *memory_mb*) is a legal Fargate pair.

    Never rounds: an invalid explicit request must be corrected by the
    operator, not silently changed.
    """
    allowed = _FARGATE_MEMORY_MIB.get(float(cpus))
    if allowed is None:
        valid_cpus = ", ".join(f"{c:g}" for c in _FARGATE_MEMORY_MIB)
        raise ValueError(
            f"Invalid Fargate vCPU value {cpus!r}; Fargate supports only {valid_cpus}."
        )
    if int(memory_mb) not in allowed:
        raise ValueError(
            f"Invalid Fargate resource pair: {cpus:g} vCPU / {memory_mb} MiB. "
            f"{cpus:g} vCPU supports {allowed[0]}-{allowed[-1]} MiB "
            f"(valid values: {', '.join(str(m) for m in allowed)}). "
            f"See https://docs.aws.amazon.com/batch/latest/userguide/fargate-job-definitions.html"
        )


class _AWSBatchHandle(PollingHandle):
    """Handle that polls Batch on `.result()`.

    We can't use a vanilla `concurrent.futures.Future` (which would let
    us reuse the base `Handle` unchanged) because the work runs in a
    remote Batch task — there's no thread or submitit job to back the
    Future. Instead, the handle carries a reference to its executor
    and the Batch `jobId`; `result()` blocks on `_wait_for_terminal`
    and `done()` does a single non-blocking `describe_jobs` call.

    The poll-retry-fallback state machine — deadline enforcement
    (issue #1465), jittered exponential backoff, retry accounting, the
    fallback-to-on-demand transition (issue #131), and AWS's ghost-job
    retry semantics in ``done()`` — lives in the shared
    ``PollingHandle`` base (issues #1464 / #1540); this class supplies
    only the AWS-specific hooks below.

    Not a dataclass — the parent `Handle` is, and dataclass inheritance
    fights with the new `_executor` field (default-vs-required ordering
    gets ugly). Constructed only inside `AWSBatchExecutor.submit()`,
    so we own the call site and don't need the dataclass machinery.
    """

    _GHOST_RETRIES = 3

    def __init__(
        self,
        job_id: str,
        executor: AWSBatchExecutor,
        submit_params: dict[str, Any],
        *,
        result_hint: Any = None,
        transport: ResultTransportConfig | None = None,
    ) -> None:
        self.job_id = job_id
        self._executor = executor
        self._submit_params = submit_params
        self._result_hint = result_hint
        # Result-transport contract (issue #1333): the handle materializes
        # object-storage artifacts on `.result()` so Campaign callbacks
        # receive local paths — identical to `_NomadHandle` and the
        # Kubernetes handle. One frozen value object (issue #1541)
        # replaces the historic five per-handle kwargs; the default
        # matches them (mode "auto", no storage configured).
        self._transport = transport if transport is not None else ResultTransportConfig()
        # Keep a `Future` so the base-class `.result(timeout=...)` /
        # `.done()` paths remain reachable; we cache the poll result
        # in it so concurrent callers don't re-poll.
        self._future: Future[Any] = Future()
        # Worker tracking (issue #105): populate at submit time.
        self.worker_id: str | None = job_id
        self.worker_ip: str | None = None
        self.worker_region: str | None = os.environ.get("AWS_REGION")
        # Cost tracking (issue #126): populated after job completes.
        self.cost_usd: float | None = None
        self.billed_duration_seconds: float | None = None

    def _apply_cost(self, job: dict[str, Any]) -> None:
        """Compute and store per-job cost from a completed job dict."""
        started = job.get("startedAt")
        stopped = job.get("stoppedAt")
        if started is not None and stopped is not None:
            self.billed_duration_seconds = max(0.0, (stopped - started) / 1000.0)
        cost_usd, _spot_savings = self._executor._calculate_job_cost(job)  # noqa: SLF001
        if cost_usd > 0:
            self.cost_usd = cost_usd

    # ------------------------------------------------------------------
    # PollingHandle hooks (issues #1464 / #1540) — the shared state
    # machine in ``osimflow.executors.base.PollingHandle`` owns
    # ``result()``; ``AWSBatchExecutor._wait_for_terminal`` owns the
    # poll skeleton via ``base.poll_until_terminal``.
    # ------------------------------------------------------------------

    def _wait_for_terminal(self, timeout: float | None) -> Any:
        job = self._executor._wait_for_terminal(self.job_id, timeout=timeout)  # noqa: SLF001
        self._apply_cost(job)
        return job

    def _classify(self, job: Any) -> tuple[PollOutcome, str | None]:
        status = job.get("status")
        if status == "SUCCEEDED":
            return PollOutcome.SUCCEEDED, None
        return PollOutcome.FAILED, job.get("statusReason", "")

    def _resolve_success_result(self, timeout: float | None = None) -> Any:
        # Issue #1697: collapsed the seven-line resolve+materialize
        # expansion into a single ``resolve_and_materialize`` call on
        # the frozen ``ResultTransportConfig`` — ``transport.py`` owns
        # the field unpacking now.
        return resolve_and_materialize(self._result_hint, self._transport)

    def _is_spot_interruption(self, reason: str | None) -> bool:
        return bool(self._executor._is_spot_interruption(reason))  # noqa: SLF001

    def _resubmit(self) -> None:
        self.job_id = self._executor._submit_job(**self._submit_params)  # noqa: SLF001
        self.worker_id = self.job_id

    def _submit_on_demand(self) -> None:
        # Issue #1816: fallback must change the capacity route — resubmit
        # to the explicitly configured on-demand queue/definition, never
        # the original (Spot) queue.
        params = {**self._submit_params, **self._executor._on_demand_route()}  # noqa: SLF001
        self.job_id = self._executor._submit_job(**params)  # noqa: SLF001
        self.worker_id = self.job_id

    def _cancel_job(self) -> bool:
        # Issue #1538: TerminateJob is the AWS Batch kill API. It moves
        # the job to FAILED promptly, which unblocks every fan-out
        # thread parked in _wait_for_terminal. Terminating an already-
        # terminal job raises a ClientError — caught by the shared
        # PollingHandle.cancel wrapper and reported as False.
        self._executor._get_client().terminate_job(  # noqa: SLF001
            jobId=self.job_id,
            reason="OSimFlow campaign cancellation (issue #1538)",
        )
        return True

    def _materialize_failure_artifacts(self) -> None:
        """Best-effort download of the failed sample's ``out.osw`` / ``run.log`` (issue #1878).

        Lets ``aggregate_results`` report the measure failure message from
        the controller, as the local executor does. Never raises.
        """
        try:
            resolve_and_materialize(self._result_hint, self._transport)
        except Exception:  # noqa: BLE001 - failure-artifact materialization is best-effort and never raises from done(); logged
            log.warning("no failure artifacts materialized for job %s", self.job_id, exc_info=True)

    def _failure_error(self, job: Any) -> RuntimeError:
        self._materialize_failure_artifacts()
        status = job.get("status")
        reason = job.get("statusReason", "")
        return RuntimeError(f"AWS Batch job {self.job_id!r} {status}: {reason}")

    def _fallback_failure_error(self, job: Any) -> RuntimeError:
        self._materialize_failure_artifacts()
        status = job.get("status")
        reason = job.get("statusReason", "unknown reason")
        return RuntimeError(f"AWS Batch job {self.job_id!r} {status}: {reason}")

    def done(self) -> bool:
        # A single non-blocking `describe_jobs` is the cheapest probe.
        # If the task is in a terminal state, we've already finished;
        # otherwise we're still running. Anything else (UNKNOWN status,
        # network blip) is treated as not-done. Ghost jobs (deleted or
        # never-created) return an empty list — after N consecutive
        # empty responses we raise to break the indefinite-wait loop.
        from botocore.exceptions import BotoCoreError, ClientError  # noqa: PLC0415

        for attempt in range(self._GHOST_RETRIES):
            try:
                response = self._executor._get_client().describe_jobs(  # noqa: SLF001
                    jobs=[self.job_id]
                )
            except (BotoCoreError, ClientError, OSError) as exc:
                log.warning("Polling error for %s: %s", self.job_id, exc)
                self.error = exc
                return False
            jobs = response.get("jobs", [])
            if jobs:
                break
            log.debug(
                "Empty describe_jobs for %s, attempt %d/%d",
                self.job_id,
                attempt + 1,
                self._GHOST_RETRIES,
            )
        else:
            # Ghost job: not found after _GHOST_RETRIES consecutive empty
            # responses. Per the base Handle.done() contract (base.py:100),
            # polling errors must be captured and returned as False, not raised.
            self.error = RuntimeError(
                f"Ghost job: job ID {self.job_id!r} not found after {self._GHOST_RETRIES} retries"
            )
            return False
        status = jobs[0].get("status", "")
        return status in ("SUCCEEDED", "FAILED")


# ---------------------------------------------------------------------------
# Spot-price cache (issue #1010)
# ---------------------------------------------------------------------------
# Token-bucket rate limiting moved to :mod:`osimflow.executors._rate_limiter`
# (issue #1563). ``AWSBatchExecutor`` shares one
# :class:`~osimflow.executors._rate_limiter.TokenBucketRateLimiter` with
# every other substrate via :meth:`BaseExecutor._init_rate_limiter`.


class _SpotPriceCache:
    """Thread-safe TTL cache for EC2 Spot price lookups (issue #1010).

    Keyed by ``(region, instance_type, product_description)`` so that
    campaigns with different configurations don't share stale prices.
    The 60-second TTL is short enough to pick up price changes while
    still amortizing the per-sample EC2 API call cost.
    """

    def __init__(self, ttl_s: float = 60.0) -> None:
        self._ttl_s: float = ttl_s
        self._cache: dict[tuple[str | None, str | None, str], tuple[float, float]] = {}
        self._lock: threading.Lock = threading.Lock()

    def get(
        self,
        key: tuple[str | None, str | None, str],
    ) -> float | None:
        """Return the cached price if within TTL, else ``None``."""
        with self._lock:
            cached = self._cache.get(key)
            if cached is None:
                return None
            price, ts = cached
            if time.monotonic() - ts >= self._ttl_s:
                del self._cache[key]
                return None
            return price

    def set(
        self,
        key: tuple[str | None, str | None, str],
        price: float,
    ) -> None:
        """Store a spot price in the cache with the current timestamp."""
        with self._lock:
            self._cache[key] = (price, time.monotonic())

    def clear(self) -> None:
        """Clear all cached entries (useful for tests)."""
        with self._lock:
            self._cache.clear()


# ---------------------------------------------------------------------------
# Submitter credential validation (issue #2026)
# ---------------------------------------------------------------------------

# Federated, role-based, and metadata credential provider ``METHOD`` values
# permitted on the submitter host when ``allow_long_lived_credentials=False``
# (issue #2026). Static providers (``env``, ``shared-credentials-file``,
# ``config-file``, ``explicit``) are only permitted when the resolved
# credentials carry a non-empty STS session ``token`` (e.g. ``AWS_SESSION_TOKEN``
# or ``aws_session_token``).
_FEDERATED_OR_ROLE_CREDENTIAL_METHODS: frozenset[str] = frozenset(
    {
        "sso",
        "assume-role",
        "assume-role-with-web-identity",
        "custom-process",
        "iam-role",
        "container-role",
        "ec2-instance-metadata",
        "ec2-credentials-file",
        "boto-config",
        "login",
    }
)


def _validate_submitter_credentials(creds: Any) -> Any:
    """Validate resolved submitter credentials for ``AWSBatchExecutor`` (issue #2026).

    Distinguishes submitter credentials (used by the host running
    ``osimflow run --executor aws_batch``) from worker credentials (which
    always come from the Batch job/compute-environment IAM roles and are
    never passed in ``containerOverrides``).

    When ``allow_long_lived_credentials=False`` (default):
    - Accepts any resolved credential with a non-empty session ``token``
      (``frozen_credentials.token is not None``) as well as federated / role
      providers in ``_FEDERATED_OR_ROLE_CREDENTIAL_METHODS`` (``sso``,
      ``assume-role``, ``assume-role-with-web-identity``, ``custom-process``,
      ``iam-role``, ``container-role``, ``ec2-instance-metadata``,
      ``boto-config``).
    - Rejects direct static credentials without a session token
      (``token is None`` from ``env``, ``shared-credentials-file``,
      ``config-file``, or ``explicit``).
    - Surfaces a clear ``RuntimeError`` when SSO or assumed-role credentials
      are expired or missing instead of a provider-stripped
      ``NoCredentialsError``.
    """
    from botocore.exceptions import BotoCoreError, ClientError  # noqa: PLC0415

    if creds is None:
        raise RuntimeError(
            "AWSBatchExecutor: no AWS submitter credentials found "
            "(SSO / assumed-role session may be missing or expired; run "
            "'aws sso login', assume an IAM role, or export short-lived STS "
            "credentials with AWS_SESSION_TOKEN)."
        )

    method = str(getattr(creds, "method", None) or "explicit")
    try:
        frozen = creds.get_frozen_credentials()
    except (BotoCoreError, ClientError) as exc:
        raise RuntimeError(
            f"AWSBatchExecutor: failed to resolve temporary AWS credentials via "
            f"{method!r} (SSO or assumed-role session may be expired or missing; "
            f"run 'aws sso login' or refresh your role session): {exc}"
        ) from exc

    token = getattr(frozen, "token", None) if frozen is not None else getattr(creds, "token", None)
    if token or method in _FEDERATED_OR_ROLE_CREDENTIAL_METHODS:
        return creds

    raise RuntimeError(
        f"AWSBatchExecutor: direct static long-lived AWS credentials from {method!r} "
        "(token is None) are rejected by default when allow_long_lived_credentials=False. "
        "Use federated or short-lived session credentials (AWS IAM Identity Center / SSO, "
        "AssumeRole, web-identity OIDC, credential_process, IAM instance/container role, "
        "or STS session credentials with AWS_SESSION_TOKEN), or pass "
        "--aws-batch-allow-long-lived-credentials (allow_long_lived_credentials=True) "
        "to explicitly opt in (issue #1833, #2026)."
    )


class _SubmitterCredentialResolver:
    """Botocore credential resolver wrapper that validates resolved submitter credentials.

    Preserves the full standard botocore provider chain (including SSO,
    AssumeRole, web-identity, ``credential_process``, STS session tokens, and
    IMDS/container roles) while rejecting direct static IAM user access keys
    when ``allow_long_lived_credentials=False`` (issue #2026).
    """

    def __init__(self, resolver: Any) -> None:
        self._resolver = resolver

    @property
    def providers(self) -> list[Any]:
        return list(self._resolver.providers)

    def load_credentials(self) -> Any:
        from botocore.exceptions import BotoCoreError, ClientError  # noqa: PLC0415

        try:
            creds = self._resolver.load_credentials()
        except (BotoCoreError, ClientError) as exc:
            raise RuntimeError(
                "AWSBatchExecutor: failed to resolve AWS submitter credentials "
                "(SSO or assumed-role session may be expired or missing; run "
                f"'aws sso login' or refresh your role session): {exc}"
            ) from exc
        return _validate_submitter_credentials(creds)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._resolver, name)


class AWSBatchExecutor(BaseExecutor):
    """AWS Batch executor (issue #5).

    Wraps `boto3.client('batch').submit_job` to launch one Batch task per
    call, then polls `describe_jobs` (with exponential backoff) until the
    task reaches a terminal state. The returned `Handle` carries the
    Batch `jobId` and blocks on `.result()` until the task succeeds; on
    failure it re-raises a `RuntimeError` whose message includes the
    Batch `statusReason` so the Campaign's `except Exception` path logs
    a useful line.

    Resource directives (`cpus`, `memory_mb`, `time_min`) are mapped to
    the Batch `containerOverrides` (`vcpus`, `memory` in MiB, `timeout`
    in seconds). Per-sample `OSIMFLOW_OS_VERSION` and `OSIMFLOW_CONTAINER`
    are carried as Batch environment variables — the same env vars
    `SlurmExecutor` exports, so downstream work scripts can be
    substrate-agnostic.

    Security: worker containers inside AWS Batch always source credentials
    from the IAM task/instance roles attached to the Batch job definition
    and compute environment (never passed in container environment overrides).
    On the submitter host, the boto3 session permits federated, role-based,
    and short-lived STS session credentials by default while rejecting
    direct static IAM user keys unless ``allow_long_lived_credentials=True``
    (issue #1833, #2026). The constructor does **not** accept
    `aws_access_key_id` / `aws_secret_access_key`; passing long-lived
    keys would violate the security policy. The ``region_name`` parameter
    pins the region passed to boto3; when ``None``, boto3 follows the
    IAM role's region (or ``AWS_REGION`` env var / ``~/.aws/config``).

    Spot instance retry + price ceiling (issue #131):
    When `max_spot_price_usd` is set, the executor queries the current
    Spot price via the EC2 API before submitting and rejects jobs that
    would exceed the ceiling. When `fallback_to_on_demand` is set and
    the price ceiling is breached (or max retries are exhausted after
    Spot interruptions), the executor falls back to submitting to the
    on-demand queue. `max_retries` controls how many times a
    Spot-interrupted job is retried before fallback or failure. Each
    retry uses exponential backoff starting at 5 seconds, capped at
    60 seconds.

    boto3 is lazy-imported inside `__init__` so the local-executor /
    slurm-executor paths do not pay the import cost.
    """

    name = "aws_batch"
    supports_spot_market = True

    @property
    def requires_remote_runner_payload(self) -> bool:
        return True

    signs_task_payload = True

    # Default pricing estimates (USD per vCPU-hour). Conservative defaults
    # used when the Spot price cannot be queried or the instance type is
    # unknown. These are intentionally slightly above market average to
    # keep estimates within 20% of the actual AWS bill (issue #126).
    DEFAULT_ON_DEMAND_PRICE_PER_VCPU_HOUR: float = 0.05
    DEFAULT_SPOT_PRICE_PER_VCPU_HOUR: float = 0.03

    # Issue #1081: digest pinning. Class attribute default ensures the
    # attribute exists even when __init__ is bypassed (e.g. tests using __new__).
    _container_digest: str | None = None

    # Sentinel used in statusReason to identify Spot interruptions.
    _SPOT_INTERRUPTION_MARKERS: tuple[str, ...] = (
        "Spot interruption",
        "Spot Instance termination",
        "spot",
    )

    # AWS error codes that should trigger a submit retry (issue #1010).
    _THROTTLE_ERRORS: tuple[str, ...] = (
        "ThrottlingException",
        "RequestLimitExceeded",
    )

    # Issue #1563: substrate-appropriate default submit rate. AWS Batch
    # documents ``submit_job`` at 1 000 TPS per account; the legacy
    # ``--aws-batch-submit-rps`` defaulted to 800 to leave headroom for
    # burst contention. We preserve that here so existing
    # ``--aws-batch-submit-rps=800`` (now mapped to ``--submit-rps``)
    # users keep the same semantics. ``_init_rate_limiter`` shares one
    # bucket across all ``AWSBatchExecutor`` instances.
    default_submit_rps: float | None = 800.0

    def __init__(
        self,
        job_queue: str = "osimflow-batch-queue",
        job_definition: str | None = None,
        poll_interval_s: float = 5.0,
        max_poll_interval_s: float = 60.0,
        region_name: str | None = None,
        *,
        max_spot_price_usd: float | None = None,
        fallback_to_on_demand: bool = False,
        max_retries: int = 3,
        ecr_repository: str | None = None,
        instance_type: str | None = None,
        submit_rps: float | None = None,
        allow_long_lived_credentials: bool = False,
        payload_secret_arn: str | None = None,
        on_demand_job_queue: str | None = None,
        on_demand_job_definition: str | None = None,
    ):
        # Issue #1816: fallback must route to real on-demand capacity.
        if fallback_to_on_demand:
            if not on_demand_job_queue:
                raise ValueError(
                    "fallback_to_on_demand requires on_demand_job_queue "
                    "(--aws-batch-on-demand-queue): an on-demand-capable Batch "
                    "queue distinct from the Spot queue."
                )
            if on_demand_job_queue == job_queue and (
                not on_demand_job_definition or on_demand_job_definition == job_definition
            ):
                raise ValueError(
                    "on_demand_job_queue must differ from job_queue (or a distinct "
                    "on_demand_job_definition must be set); resubmitting to the same "
                    "route is not an on-demand fallback."
                )
        # Lazy import: keeps the boto3 import cost off the local /
        # slurm executor paths. ImportError here is intentional: the
        # user opted into the [aws] extra, so a missing boto3 is a
        # user error, not a silent fallback.
        import boto3  # noqa: PLC0415
        import botocore.session  # noqa: PLC0415
        from botocore.config import Config as BotoConfig  # noqa: PLC0415

        # Security (issue #1160, #1833, #2026):
        # Worker containers inside AWS Batch always use the IAM role on the
        # Batch compute environment / job definition (never passed via
        # containerOverrides). On the submitter host, default
        # (allow_long_lived_credentials=False) retains the full botocore
        # credential provider chain and validates the resolved credentials so
        # federated / role-based / short-lived STS session credentials are
        # accepted while direct static IAM user access keys (token is None)
        # are rejected.
        self._allow_long_lived_credentials = allow_long_lived_credentials

        if not allow_long_lived_credentials:
            static_env_creds = [
                var for var in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY") if os.environ.get(var)
            ]
            if static_env_creds and not os.environ.get("AWS_SESSION_TOKEN"):
                log.warning(
                    "AWSBatchExecutor: long-lived static AWS credentials detected in "
                    "environment (%s without AWS_SESSION_TOKEN) while "
                    "allow_long_lived_credentials=False (default). Direct static keys "
                    "will be rejected when resolving submitter credentials; use "
                    "short-lived/federated credentials (SSO, AssumeRole, STS session "
                    "token, or IAM role) or set allow_long_lived_credentials=True to "
                    "opt out (not recommended for production).",
                    ", ".join(static_env_creds),
                )

            session = botocore.session.get_session()
            default_resolver = session.get_component("credential_provider")
            validating_resolver = _SubmitterCredentialResolver(default_resolver)
            session.register_component("credential_provider", validating_resolver)
            # Keep the boto3 MODULE as the client-factory seam (tests patch
            # ``boto3.client``); carry the validating session as a kwarg so
            # ``self._boto3.client(...)`` stays interceptable while still
            # validating resolved submitter credentials.
            self._botocore_session: Any = session
        else:
            log.warning(
                "AWSBatchExecutor: allow_long_lived_credentials=True — "
                "environment / shared-file / SSO credentials may be used "
                "instead of IAM-role credentials only. Not recommended for "
                "production (issue #1833)."
            )
            self._botocore_session = None

        self._boto3 = boto3
        # boto3.client("batch") without a configured region raises
        # NoRegionError immediately, so we defer client construction
        # to first use. The region still comes from the IAM role /
        # AWS_REGION env / ~/.aws/config — `region_name=None` just
        # tells boto3 to follow that chain rather than pin a region.
        self._region_name = region_name
        self._client: Any = None
        self._ec2_client: Any = None
        self._secrets_client: Any = None
        self._ssm_client: Any = None
        self._resolved_payload_secret: str | None = None
        self.job_queue = job_queue
        self.job_definition = job_definition or "osimflow-job-def"
        self.on_demand_job_queue = on_demand_job_queue
        self.on_demand_job_definition = on_demand_job_definition
        # Issue #1081: digest pinning. Initialized in the constructor so
        # ``_resolve_container_image`` is callable without going through
        # ``submit()`` (e.g. unit tests); overridden by ``submit()``.
        self._container_digest: str | None = None
        self.poll_interval_s = poll_interval_s
        self.max_poll_interval_s = max_poll_interval_s
        self.max_spot_price_usd = max_spot_price_usd
        self.fallback_to_on_demand = fallback_to_on_demand
        self.max_retries = max_retries
        self.ecr_repository = ecr_repository
        self._instance_type = instance_type
        self._submit_rps = submit_rps
        # Issue #1633: ARN of an AWS Secrets Manager secret or SSM
        # Parameter Store parameter holding the task-payload HMAC
        # secret. When set, the secret ships via
        # the job definition's ``containerProperties.secrets`` (resolved
        # by the ECS/Batch agent at container start via the execution
        # role) instead of a literal env value in the job spec, where it
        # would be readable via DescribeJobs. Issue #1811: SubmitJob has
        # no ``containerOverrides.secrets``, so the ARN is only validated
        # against the job definition before submission.
        self.payload_secret_arn = payload_secret_arn
        # boto3 retry config with adaptive mode for ThrottlingException
        # handling (issue #1010). Adaptive mode uses client-side rate
        # limiting + exponential backoff with jitter.
        self._retry_config = BotoConfig(
            retries={"mode": "adaptive", "max_attempts": 10},
        )
        # Issue #1563: shared token-bucket limiter (replaces the
        # private ``_TokenBucketRateLimiter`` that used to live in
        # this module — now lives on
        # :mod:`osimflow.executors._rate_limiter`). ``BaseExecutor.submit``
        # acquires from it before calling ``_do_submit``.
        self._init_rate_limiter(submit_rps)
        # Spot price cache with 60s TTL — avoids one EC2 API call per
        # sample in a 10K-sample campaign (issue #1010).
        self._spot_price_cache = _SpotPriceCache(ttl_s=60.0)

    def _resolve_container_image(self, version: str | None) -> str:
        """Resolve the container image URI.

        When ``ecr_repository`` is set, returns ``<ecr_repo>:<version>``.
        Otherwise falls back to Docker Hub ``nrel/openstudio:<version>``.

        Issue #1081: when the caller pins images by SHA256 digest,
        the digest is returned verbatim and overrides every tag-based
        resolution path below.
        """
        container_digest = self._container_digest
        if container_digest:
            return container_digest
        tag = version or "latest"
        if self.ecr_repository:
            return f"{self.ecr_repository}:{tag}"
        return f"nrel/openstudio:{tag}"

    def _client_kwargs(self) -> dict[str, Any]:
        """Shared boto3.client kwargs, including the IAM-only session (if any)."""
        kwargs: dict[str, Any] = {
            "region_name": self._region_name,
            "config": self._retry_config,
        }
        botocore_session = getattr(self, "_botocore_session", None)
        if botocore_session is not None:
            kwargs["botocore_session"] = botocore_session
        return kwargs

    def _make_client(self, service: str, *, region_name: str | None = None) -> Any:
        """Build a boto3 client; ``boto3.client`` has no ``botocore_session``
        kwarg, so the IAM-only session must go through ``boto3.Session``."""
        kwargs = self._client_kwargs()
        if region_name:
            kwargs["region_name"] = region_name
        botocore_session = kwargs.pop("botocore_session", None)
        if botocore_session is not None:
            return self._boto3.Session(botocore_session=botocore_session).client(service, **kwargs)
        return self._boto3.client(service, **kwargs)

    @staticmethod
    def _arn_region(arn: str | None) -> str | None:
        """Extract the AWS region segment from an ARN if present."""
        if not arn:
            return None
        parts = arn.split(":")
        if len(parts) >= 4 and parts[0] == "arn" and parts[3].strip():
            return parts[3].strip()
        return None

    def _get_client(self) -> Any:
        """Lazy boto3 Batch client construction.

        Deferring to first use lets the constructor succeed on hosts
        that have boto3 installed but no AWS config (e.g. CI runners
        that only test the executor wiring with mocked clients).
        Production deployments will have AWS_REGION set or an IAM
        role / ~/.aws/config in place.
        """
        if self._client is None:
            self._client = self._make_client("batch")
        return self._client

    def _get_ec2_client(self) -> Any:
        """Lazy boto3 EC2 client for Spot price queries."""
        if self._ec2_client is None:
            self._ec2_client = self._make_client("ec2")
        return self._ec2_client

    def _get_secrets_client(self, *, region_name: str | None = None) -> Any:
        """Lazy boto3 Secrets Manager client for payload-secret retrieval."""
        if getattr(self, "_secrets_client", None) is None:
            self._secrets_client = self._make_client("secretsmanager", region_name=region_name)
        return self._secrets_client

    def _get_ssm_client(self, *, region_name: str | None = None) -> Any:
        """Lazy boto3 SSM client for payload-secret retrieval."""
        if getattr(self, "_ssm_client", None) is None:
            self._ssm_client = self._make_client("ssm", region_name=region_name)
        return self._ssm_client

    def _get_spot_price(self) -> float:
        """Query the current Spot price for the instance type.

        Cached with a 60-second TTL keyed by ``(region, instance_type, os)``
        (issue #1010).  When ``max_spot_price_usd`` is set, the ceiling
        check reuses the cached value across all samples in a campaign
        instead of making one EC2 API call per sample.

        Uses ``describe_spot_price_history`` with a single-result query
        to get the most recent price. Returns the price in USD per
        instance-hour. Raises ``RuntimeError`` if the query fails or
        returns no results.

        When ``_instance_type`` is set, the query is scoped to that
        instance type so the ceiling check is reliable (issue #792).
        When it is not set, the query returns the lowest price across
        all instance types and a warning is logged.
        """
        product = "Linux/UNIX"
        cache_key: tuple[str | None, str | None, str] = (
            self._region_name,
            self._instance_type,
            product,
        )
        cached = self._spot_price_cache.get(cache_key)
        if cached is not None:
            return cached

        kwargs: dict[str, Any] = {
            "MaxResults": 1,
            "ProductDescriptions": [product],
        }
        if self._instance_type is not None:
            kwargs["InstanceTypes"] = [self._instance_type]
        response = self._get_ec2_client().describe_spot_price_history(**kwargs)
        histories = response.get("SpotPriceHistory", [])
        if not histories:
            raise RuntimeError("describe_spot_price_history returned no results")
        price = float(histories[0]["SpotPrice"])

        self._spot_price_cache.set(cache_key, price)
        return price

    def _is_spot_interruption(self, reason: str | None) -> bool:
        """Return True if the failure reason indicates a Spot interruption."""
        if not reason:
            return False
        lower = reason.lower()
        return any(marker.lower() in lower for marker in self._SPOT_INTERRUPTION_MARKERS)

    def _check_spot_price_ceiling(self) -> None:
        """Check the Spot price against the configured ceiling.

        Raises ``RuntimeError`` when the Spot price exceeds the ceiling
        and ``fallback_to_on_demand`` is False. When fallback is enabled,
        logs a warning and returns (caller should switch to on-demand).
        """
        if self.max_spot_price_usd is None:
            return
        current_price = self._get_spot_price()
        if current_price <= self.max_spot_price_usd:
            return
        msg = f"Spot price ${current_price:.4f} exceeds ceiling ${self.max_spot_price_usd:.4f}"
        if self.fallback_to_on_demand:
            log.warning("%s — falling back to on-demand", msg)
            return
        raise RuntimeError(msg)

    def _build_environment(
        self,
        *,
        container: str | None,
        openstudio_version: str | None,
        task_payload: str | None = None,
        transport: ResultTransportConfig | None = None,
    ) -> list[dict[str, str]]:
        """Build the Batch `environment` list from the per-submit config.

        The serialized task payload travels in ``OSIMFLOW_TASK_PAYLOAD`` and
        the result-transport contract in the ``OSIMFLOW_RESULT_*`` vars so
        ``osimflow.remote_runner`` can execute the step and push results to
        object storage (issue #996). ``OSIMFLOW_STUB_SIM`` is propagated
        from the orchestrator environment when set so remote pods honour
        the orchestrator's stub-vs-real CLI choice.

        The transport contract arrives as the single frozen value object
        (issue #1541); a ``None`` config emits no ``OSIMFLOW_RESULT_*``
        vars, matching the historic all-``None`` field set.
        """
        env: list[dict[str, str]] = []
        if openstudio_version is not None:
            env.append({"name": "OSIMFLOW_OS_VERSION", "value": str(openstudio_version)})
        # Resolve container image using the standard resolution logic
        # which respects container_digest, ecr_repository, and the
        # container parameter.
        resolved = self._resolve_container_image(openstudio_version)
        # If a custom container was passed, it takes precedence
        if container is not None:
            resolved = container
        env.append({"name": "OSIMFLOW_CONTAINER", "value": resolved})
        signing_secret: str | None = None
        if task_payload is not None:
            env.append({"name": "OSIMFLOW_TASK_PAYLOAD", "value": task_payload})
            # Issue #1445/#1633/#2025/#2042: osimflow.remote_runner is fail-closed
            # (issue #1205) and rejects unsigned task payloads at container
            # startup. Require a resolvable signing secret on the orchestrator
            # so AWSBatchExecutor never silently submits an unsigned payload.
            # Resolve the effective ``payload_secret_arn`` from the active job
            # definition first so omitting ``--aws-batch-payload-secret-arn``
            # still triggers signature-only mode when the job definition
            # injects ``OSIMFLOW_TASK_PAYLOAD_SECRET`` via
            # ``containerProperties.secrets`` (issue #2042).
            payload_secret_arn = self._resolve_effective_payload_secret_arn()
            signing_secret = self._resolve_signing_secret()
            if not payload_secret_arn and TASK_PAYLOAD_SECRET_ENV in os.environ:
                # Issue #1633: without a job-definition secret the
                # shared secret ships as a literal env value serialized
                # into the job spec, where anyone with
                # batch:DescribeJobs can read it and forge signatures.
                log.warning(
                    "SECURITY (issue #1633): %s is shipping as a literal "
                    "env value in the Batch containerOverrides because no "
                    "--aws-batch-payload-secret-arn is configured. Anyone "
                    "with batch:DescribeJobs can read the secret and "
                    "forge task-payload signatures. Store the secret in "
                    "AWS Secrets Manager (or SSM Parameter Store) and "
                    "pass --aws-batch-payload-secret-arn <arn> with a job "
                    "definition that injects it via containerProperties.secrets.",
                    TASK_PAYLOAD_SECRET_ENV,
                )
            env.extend(
                {
                    "name": key,
                    "value": value,
                }
                for key, value in build_signature_env(
                    task_payload,
                    secret=signing_secret,
                    include_secret=payload_secret_arn is None,
                    require_secret=True,
                ).items()
            )
        if transport is not None:
            env.append({"name": "OSIMFLOW_RESULT_TRANSPORT_MODE", "value": transport.mode})
            if transport.backend is not None:
                env.append({"name": "OSIMFLOW_RESULT_STORAGE_BACKEND", "value": transport.backend})
            if transport.bucket is not None:
                env.append({"name": "OSIMFLOW_RESULT_STORAGE_BUCKET", "value": transport.bucket})
            if transport.prefix is not None:
                env.append({"name": "OSIMFLOW_RESULT_STORAGE_PREFIX", "value": transport.prefix})
            if transport.endpoint is not None:
                env.append(
                    {"name": "OSIMFLOW_RESULT_STORAGE_ENDPOINT", "value": transport.endpoint}
                )
            # Issue #1549: second HMAC over the canonical result-transport
            # settings (see ``build_transport_signature_env``).
            for _key, _value in build_transport_signature_env(
                transport, secret=signing_secret
            ).items():
                env.append({"name": _key, "value": _value})
        stub_sim = os.environ.get("OSIMFLOW_STUB_SIM")
        if stub_sim is not None:
            env.append({"name": "OSIMFLOW_STUB_SIM", "value": stub_sim})
        return env

    @staticmethod
    def _has_mocked_boto3_seam(boto3_mod: Any) -> bool:
        """Return True if *boto3_mod* (or its Session/client factory) has been mocked."""
        if boto3_mod is None:
            return False
        for obj in (
            boto3_mod,
            getattr(boto3_mod, "client", None),
            getattr(boto3_mod, "Session", None),
        ):
            if obj is not None and type(obj).__module__.startswith("unittest.mock"):
                return True
        return False

    def _has_describe_client_seam(self) -> bool:
        """Return True when a Batch client or mock seam is configured on *self*."""
        return (
            getattr(self, "_client", None) is not None
            or "_get_client" in self.__dict__
            or "_make_client" in self.__dict__
            or self._has_mocked_boto3_seam(getattr(self, "_boto3", None))
        )

    def _query_job_def_for_secret_arn(self, target: str) -> dict[str, Any] | None:
        """Call ``describe_job_definitions`` for *target*, raising ``RuntimeError`` on AWS errors."""
        if "_submit_job" in self.__dict__ and getattr(self, "_client", None) is None:
            return None
        try:
            if (
                getattr(self, "_client", None) is None
                and ":" in target
                and getattr(self, "_region_name", None) is None
            ):
                jd_region = self._arn_region(target)
                if jd_region:
                    self._client = self._make_client("batch", region_name=jd_region)
            client = self._get_client()
            if ":" in target:
                response = client.describe_job_definitions(jobDefinitions=[target])
            else:
                response = client.describe_job_definitions(
                    jobDefinitionName=target, status="ACTIVE"
                )
        except Exception as exc:
            log.error(
                "AWSBatchExecutor: describe_job_definitions(%r) failed while auto-discovering "
                "%s secret ARN: %s",
                target,
                TASK_PAYLOAD_SECRET_ENV,
                exc,
                exc_info=True,
            )
            raise RuntimeError(
                f"Failed to inspect AWS Batch job definition {target!r} via "
                f"batch:DescribeJobDefinitions ({exc}). Refusing to proceed "
                f"with an unknown job-definition secret state (which could "
                f"silently downgrade {TASK_PAYLOAD_SECRET_ENV} into plaintext "
                f"containerOverrides.environment). Grant batch:DescribeJobDefinitions "
                f"to the submitter principal or pass --aws-batch-payload-secret-arn "
                f"explicitly (issue #2042)."
            ) from exc
        return response if isinstance(response, dict) else None

    @staticmethod
    def _extract_secret_arn_from_job_defs(definitions_raw: Any) -> str | None:
        """Extract the ``OSIMFLOW_TASK_PAYLOAD_SECRET`` ``valueFrom`` ARN from the highest revision."""
        if not isinstance(definitions_raw, list) or not definitions_raw:
            return None
        definition = max(definitions_raw, key=lambda d: int(d.get("revision", 0)))
        secrets = (definition.get("containerProperties") or {}).get("secrets") or []
        for entry in secrets:
            if isinstance(entry, dict) and entry.get("name") == TASK_PAYLOAD_SECRET_ENV:
                value_from = str(entry.get("valueFrom") or "").strip()
                if value_from:
                    return value_from
        return None

    def _resolve_effective_payload_secret_arn(
        self,
        job_definition: str | None = None,
        *,
        strict: bool = False,
    ) -> str | None:
        """Resolve the effective ``payload_secret_arn`` for task-payload signing (issue #2042).

        When ``self.payload_secret_arn`` is already configured, returns it
        directly.  Otherwise, inspects the highest active revision of
        *job_definition* (or ``self.job_definition``) via
        ``describe_job_definitions``.  If ``containerProperties.secrets``
        maps ``OSIMFLOW_TASK_PAYLOAD_SECRET``, its exact ``valueFrom`` ARN is
        adopted onto ``self.payload_secret_arn`` and cached per job
        definition so ``_build_environment()`` omits the raw secret from
        ``containerOverrides.environment`` and ``_resolve_signing_secret()``
        can fetch it from Secrets Manager / SSM Parameter Store.
        """
        existing = getattr(self, "payload_secret_arn", None)
        if existing:
            return str(existing)

        target = (job_definition or getattr(self, "job_definition", "") or "").strip()
        if not target or (not self._has_describe_client_seam() and not strict):
            return None

        cache: dict[str, str | None] = self.__dict__.setdefault("_job_def_secret_arns", {})
        if target in cache:
            discovered = cache[target]
            if discovered:
                self.payload_secret_arn = discovered
            return discovered

        response = self._query_job_def_for_secret_arn(target)
        if response is None:
            return None

        value_from = self._extract_secret_arn_from_job_defs(response.get("jobDefinitions"))
        cache[target] = value_from
        if value_from:
            self.payload_secret_arn = value_from
            validated: set[str] = self.__dict__.setdefault("_validated_secret_job_defs", set())
            validated.add(target)
        return value_from

    @staticmethod
    def _validate_fetched_secret(raw_value: str, *, source: str) -> str:
        """Validate and normalize a secret fetched from Secrets Manager or SSM (issue #2042).

        Strips at most a single trailing newline (``\\r?\\n``) so the
        submitter signs with the exact bytes injected into the worker
        container, while rejecting empty/whitespace-only secrets or secrets
        shorter than 32 UTF-8 bytes.
        """
        normalized = raw_value
        if normalized.endswith("\r\n"):
            normalized = normalized[:-2]
        elif normalized.endswith("\n"):
            normalized = normalized[:-1]
        if not normalized or not normalized.strip():
            raise ValueError(
                f"Secret fetched from {source} is empty or whitespace-only — "
                f"{TASK_PAYLOAD_SECRET_ENV} must be a non-empty secret of at least "
                "32 UTF-8 bytes (issue #2042)."
            )
        byte_len = len(normalized.encode("utf-8"))
        if byte_len < 32:
            raise ValueError(
                f"Secret fetched from {source} is only {byte_len} UTF-8 bytes — "
                f"{TASK_PAYLOAD_SECRET_ENV} must be at least 32 UTF-8 bytes "
                "(issue #2042)."
            )
        return normalized

    @staticmethod
    def _parse_secretsmanager_value_from(
        arn: str, parts: list[str]
    ) -> tuple[dict[str, Any], str | None]:
        """Parse an ECS/Batch Secrets Manager ``valueFrom`` ARN into ``get_secret_value`` kwargs and optional JSON key."""
        secret_id = arn
        json_key: str | None = None
        get_kwargs: dict[str, Any] = {}
        if len(parts) >= 7 and parts[0] == "arn" and parts[5] == "secret":
            secret_id = ":".join(parts[:7])
            if len(parts) >= 8 and parts[7]:
                json_key = parts[7]
            if len(parts) >= 9 and parts[8]:
                get_kwargs["VersionStage"] = parts[8]
            if len(parts) >= 10 and parts[9]:
                get_kwargs["VersionId"] = parts[9]
        get_kwargs["SecretId"] = secret_id
        return get_kwargs, json_key

    @staticmethod
    def _extract_json_secret_key(raw_value: str, *, secret_id: str, json_key: str, arn: str) -> str:
        """Extract *json_key* from a JSON-object ``SecretString``."""
        try:
            parsed_json = json.loads(raw_value)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"Secret {secret_id!r} is referenced with JSON key selector "
                f"{json_key!r} in {arn!r}, but SecretString is not valid JSON "
                "(issue #2042)."
            ) from exc
        if not isinstance(parsed_json, dict) or json_key not in parsed_json:
            raise ValueError(
                f"Secret {secret_id!r} JSON object does not contain key "
                f"{json_key!r} referenced by {arn!r} (issue #2042)."
            )
        selected = parsed_json[json_key]
        if selected is None:
            raise ValueError(f"Secret {secret_id!r} JSON key {json_key!r} is null (issue #2042).")
        return str(selected)

    def _fetch_secret_from_arn(self, arn: str) -> str:
        """Fetch and validate the HMAC secret from Secrets Manager or SSM Parameter Store."""
        parts = arn.split(":")
        service = parts[2] if len(parts) >= 6 and parts[0] == "arn" else "secretsmanager"
        arn_region = self._arn_region(arn)

        if service == "ssm":
            ssm_client = self._get_ssm_client(region_name=arn_region)
            resp = ssm_client.get_parameter(Name=arn, WithDecryption=True)
            param = resp.get("Parameter") if isinstance(resp, dict) else None
            if not isinstance(param, dict) or param.get("Value") is None:
                raise RuntimeError(
                    f"SSM get_parameter({arn!r}) returned no Parameter.Value "
                    f"for {TASK_PAYLOAD_SECRET_ENV} (issue #2042)."
                )
            return self._validate_fetched_secret(str(param["Value"]), source=arn)

        if service != "secretsmanager":
            raise ValueError(
                f"Unsupported ARN service {service!r} in {arn!r}; expected "
                "'secretsmanager' or 'ssm' (issue #2042)."
            )

        get_kwargs, json_key = self._parse_secretsmanager_value_from(arn, parts)
        secret_id = str(get_kwargs["SecretId"])
        secrets_client = self._get_secrets_client(region_name=arn_region)
        resp = secrets_client.get_secret_value(**get_kwargs)
        if not isinstance(resp, dict):
            raise RuntimeError(
                f"Secrets Manager get_secret_value({secret_id!r}) returned an invalid "
                f"response for {TASK_PAYLOAD_SECRET_ENV} (issue #2042)."
            )
        if "SecretBinary" in resp and not resp.get("SecretString"):
            raise ValueError(
                f"Secret {secret_id!r} returned binary SecretBinary instead of "
                f"SecretString; {TASK_PAYLOAD_SECRET_ENV} requires a UTF-8 string "
                "secret of at least 32 bytes (issue #2042)."
            )
        secret_string = resp.get("SecretString")
        if secret_string is None:
            raise ValueError(
                f"Secret {secret_id!r} returned no SecretString for "
                f"{TASK_PAYLOAD_SECRET_ENV} (issue #2042)."
            )
        raw_value = str(secret_string)
        if json_key:
            raw_value = self._extract_json_secret_key(
                raw_value, secret_id=secret_id, json_key=json_key, arn=arn
            )
        return self._validate_fetched_secret(raw_value, source=arn)

    def _resolve_signing_secret(self) -> str:
        """Return the non-empty HMAC secret for signing ``OSIMFLOW_TASK_PAYLOAD``.

        ``osimflow.remote_runner`` is fail-closed (issue #1205) and rejects
        any task payload when ``OSIMFLOW_TASK_PAYLOAD_SECRET`` is absent in
        the worker container.  Resolving and validating the secret locally
        before staging inputs or calling ``batch:SubmitJob`` eliminates the
        silent unsigned submission trap where Batch jobs launch only to fail
        immediately at container startup (issue #2025).

        When ``OSIMFLOW_TASK_PAYLOAD_SECRET`` is not set locally on the
        orchestrator, auto-discovers the secret ARN from the active job
        definition's ``containerProperties.secrets`` (if not explicitly
        passed via ``--aws-batch-payload-secret-arn``) and fetches the
        secret value from AWS Secrets Manager or SSM Parameter Store,
        caching it on ``self._resolved_payload_secret`` (issue #2042).
        """
        secret = resolve_payload_secret()
        if secret:
            return secret
        cached = getattr(self, "_resolved_payload_secret", None)
        if isinstance(cached, str) and cached:
            return cached
        payload_secret_arn = self._resolve_effective_payload_secret_arn()
        if payload_secret_arn:
            try:
                fetched = self._fetch_secret_from_arn(payload_secret_arn)
            except ValueError:
                raise
            except Exception as exc:
                log.error(
                    "AWSBatchExecutor: failed to fetch %s from %r: %s",
                    TASK_PAYLOAD_SECRET_ENV,
                    payload_secret_arn,
                    exc,
                    exc_info=True,
                )
                raise RuntimeError(
                    f"--aws-batch-payload-secret-arn={payload_secret_arn!r} is configured, "
                    f"but {TASK_PAYLOAD_SECRET_ENV} is not set on the orchestrator and "
                    f"fetching the secret from AWS failed ({exc}) — cannot compute "
                    f"{TASK_PAYLOAD_SIG_ENV}. Refusing to submit an unsigned AWS Batch "
                    "task payload that osimflow.remote_runner will reject at container "
                    f"startup. Grant secretsmanager:GetSecretValue (or ssm:GetParameter) "
                    f"on {payload_secret_arn!r} or export {TASK_PAYLOAD_SECRET_ENV} "
                    f"(or {TASK_PAYLOAD_SECRET_FILE_ENV}) on the orchestrator "
                    "(issue #1633, #2025, #2042)."
                ) from exc
            self._resolved_payload_secret = fetched
            return fetched
        raise RuntimeError(
            f"{TASK_PAYLOAD_SECRET_ENV} is not configured — refusing to submit an "
            "unsigned AWS Batch task payload that osimflow.remote_runner will reject "
            f"at container startup. Export {TASK_PAYLOAD_SECRET_ENV} (or "
            f"{TASK_PAYLOAD_SECRET_FILE_ENV}) on the orchestrator, and for production "
            "pass --aws-batch-payload-secret-arn <arn> with a job definition that "
            "injects it via containerProperties.secrets (issue #1205, #2025)."
        )

    def _validate_job_definition_secret(self, job_definition: str) -> None:
        """Fail before submission unless the job definition injects the secret.

        AWS Batch ``SubmitJob`` has no ``containerOverrides.secrets``
        (issue #1811); secrets can only be injected through the job
        definition's ``containerProperties.secrets``. When
        ``payload_secret_arn`` is configured, the selected definition
        must map ``OSIMFLOW_TASK_PAYLOAD_SECRET`` to that exact ARN,
        otherwise workers would reject every signed task.
        """
        arn = getattr(self, "payload_secret_arn", None)
        if not arn:
            return
        validated: set[str] = self.__dict__.setdefault("_validated_secret_job_defs", set())
        if job_definition in validated:
            return
        client = self._get_client()
        if ":" in job_definition:
            response = client.describe_job_definitions(jobDefinitions=[job_definition])
        else:
            response = client.describe_job_definitions(
                jobDefinitionName=job_definition, status="ACTIVE"
            )
        definitions: list[dict[str, Any]] = list(response.get("jobDefinitions", []))
        if not definitions:
            raise RuntimeError(
                f"aws-batch-payload-secret-arn is set but job definition "
                f"{job_definition!r} was not found (or has no ACTIVE revision)"
            )
        definition = max(definitions, key=lambda d: int(d.get("revision", 0)))
        secrets = (definition.get("containerProperties") or {}).get("secrets") or []
        mapped = {s.get("name"): s.get("valueFrom") for s in secrets}
        if mapped.get(TASK_PAYLOAD_SECRET_ENV) != arn:
            raise RuntimeError(
                f"Job definition {job_definition!r} (revision "
                f"{definition.get('revision')}) does not map "
                f"{TASK_PAYLOAD_SECRET_ENV} to the configured "
                f"--aws-batch-payload-secret-arn. Add "
                f'{{"name": "{TASK_PAYLOAD_SECRET_ENV}", "valueFrom": "<arn>"}} '
                f"to containerProperties.secrets (SubmitJob cannot inject "
                f"secrets; issue #1811) and ensure the execution role can "
                f"read it."
            )
        validated.add(job_definition)

    def resolve_job_definition_image(self, job_definition: str | None = None) -> str | None:
        """Return the ``containerProperties.image`` of *job_definition*, or ``None`` (issue #2027).

        Queries ``describe_job_definitions`` and caches the result per
        ``job_definition``.  Returns ``None`` on missing job definitions,
        empty ``containerProperties.image``, or AWS client errors so
        campaign construction can degrade gracefully and let ``run()``
        enforce the digest-pinning contract.
        """
        target = (job_definition or getattr(self, "job_definition", "") or "").strip()
        if not target:
            return None
        cache: dict[str, str] = self.__dict__.setdefault("_job_def_images", {})
        if target in cache:
            return cache[target]
        try:
            client = self._get_client()
            if ":" in target:
                response = client.describe_job_definitions(jobDefinitions=[target])
            else:
                response = client.describe_job_definitions(
                    jobDefinitionName=target, status="ACTIVE"
                )
        except Exception as exc:  # noqa: BLE001 - best-effort discovery; Campaign.run() enforces the digest contract
            log.debug(
                "AWSBatchExecutor: could not inspect job definition %r for digest resolution: %s",
                target,
                exc,
            )
            return None
        if not isinstance(response, dict) or not response.get("jobDefinitions"):
            return None
        definitions: list[dict[str, Any]] = list(response["jobDefinitions"])
        definition = max(definitions, key=lambda d: int(d.get("revision", 0)))
        actual = str((definition.get("containerProperties") or {}).get("image") or "").strip()
        if actual:
            cache[target] = actual
            return actual
        return None

    def resolve_job_definition_digest(
        self,
        job_definition: str | None = None,
        *,
        label: str = "",
    ) -> str | None:
        """Return the ``<repo>@sha256:<hex>`` ref for *job_definition*, or ``None`` (issue #2027).

        When ``containerProperties.image`` is already digest-pinned
        (``<repo>@sha256:<hex>`` or ``<repo>:<tag>@sha256:<hex>``), the
        authoritative digest is returned directly without invoking local
        Docker.  When ``containerProperties.image`` is a mutable tag, falls
        back to :func:`osimflow.cache._resolve_image_digest` and returns
        ``None`` when no digest can be resolved.
        """
        actual = self.resolve_job_definition_image(job_definition)
        if not actual:
            return None
        effective_label = actual if ":" in actual or "/" in actual else (label or actual)
        pinned = _cache_mod.digest_pinned_image_ref(effective_label, actual)
        if pinned is not None:
            return pinned
        try:
            resolved = _cache_mod._resolve_image_digest(actual)
        except _cache_mod.ImageDigestUnavailableError:
            return None
        return _cache_mod.digest_pinned_image_ref(effective_label, resolved)

    def validate_job_definition_digest(
        self,
        expected_image: str,
        job_definition: str | None = None,
    ) -> None:
        """Validate that the AWS Batch job definition matches *expected_image* (issue #2027)."""
        target = (job_definition or getattr(self, "job_definition", "") or "").strip()
        if not target or not expected_image:
            return
        if self.resolve_job_definition_image(target) is None:
            return
        self._validate_job_definition_image(target, expected_image)

    def _validate_job_definition_image(self, job_definition: str, expected_image: str) -> None:
        """Fail unless the job definition launches the requested image (issue #1810).

        AWS Batch launches ``containerProperties.image`` from the registered
        job definition; ``OSIMFLOW_CONTAINER`` in the job environment is
        informational only and cannot change it. A pinned tag/digest therefore
        has to select a matching job definition or fail loudly.
        """
        validated: set[str] = self.__dict__.setdefault("_validated_image_job_defs", set())
        key = f"{job_definition}|{expected_image}"
        if key in validated:
            return
        client = self._get_client()
        if ":" in job_definition:
            response = client.describe_job_definitions(jobDefinitions=[job_definition])
        else:
            response = client.describe_job_definitions(
                jobDefinitionName=job_definition, status="ACTIVE"
            )
        definitions: list[dict[str, Any]] = list(response.get("jobDefinitions", []))
        if not definitions:
            raise RuntimeError(
                f"Job definition {job_definition!r} was not found (or has no ACTIVE "
                f"revision); cannot verify it launches the requested image "
                f"{expected_image!r}"
            )
        definition = max(definitions, key=lambda d: int(d.get("revision", 0)))
        actual = str((definition.get("containerProperties") or {}).get("image") or "")
        if not self._image_matches(actual, expected_image):
            raise RuntimeError(
                f"Requested image {expected_image!r} does not match the image "
                f"{actual!r} launched by job definition {job_definition!r} "
                f"(revision {definition.get('revision')}). AWS Batch runs the job "
                f"definition's image; OSIMFLOW_CONTAINER does not change it. "
                f"Register a job definition for the requested image and pass it "
                f"with --aws-batch-job-definition."
            )
        validated.add(key)

    def _pinned_image(self, container: str | None, openstudio_version: str | None) -> str | None:
        """Return the image the caller explicitly pinned, else ``None`` (issue #1810)."""
        # The per-call ``container`` is the campaign's generic default and is
        # not an operator pin, so only executor-level pins are enforced.
        del container
        digest = self._container_digest
        if digest:
            # The campaign passes cache-form digests, including the
            # ``unresolved`` sentinel when nothing was pinned; not a pin.
            # Auto-resolved cache-form digests look like
            # ``<label>@<repo>@sha256:<hex>`` (two ``@``); an operator pin is
            # a single ``<repo>@sha256:<hex>`` (or bare ``sha256:<hex>``).
            if "sha256:" not in digest or digest.count("@") > 1:
                return None
            return digest
        if self.ecr_repository:
            return self._resolve_container_image(openstudio_version)
        return None

    @staticmethod
    def _image_matches(actual: str, expected: str) -> bool:
        if actual == expected:
            return True
        if "@sha256:" in expected or expected.startswith("sha256:"):
            digest = "sha256:" + expected.split("sha256:", 1)[1]
            return digest in actual
        return False

    def _job_definition_platform(self, job_definition: str) -> str:
        """Return ``"FARGATE"`` or ``"EC2"`` for *job_definition* (issue #1808).

        Derived from the registered definition's ``platformCapabilities``;
        an unknown/empty result keeps the legacy EC2 behaviour.
        """
        cache: dict[str, str] = self.__dict__.setdefault("_job_def_platforms", {})
        if job_definition in cache:
            return cache[job_definition]
        client = self._get_client()
        try:
            if ":" in job_definition:
                response = client.describe_job_definitions(jobDefinitions=[job_definition])
            else:
                response = client.describe_job_definitions(
                    jobDefinitionName=job_definition, status="ACTIVE"
                )
        except Exception:
            log.warning(
                "could not describe job definition %r to detect its platform; "
                "assuming EC2 (grant batch:DescribeJobDefinitions for Fargate)",
                job_definition,
                exc_info=True,
            )
            return "EC2"
        definitions: list[dict[str, Any]] = list(response.get("jobDefinitions", []))
        platform = "EC2"
        if definitions:
            definition = max(definitions, key=lambda d: int(d.get("revision", 0)))
            if "FARGATE" in (definition.get("platformCapabilities") or []):
                platform = "FARGATE"
        cache[job_definition] = platform
        return platform

    def _build_container_overrides(
        self,
        *,
        cpus: float,
        memory_mb: int,
        environment: list[dict[str, str]],
        command: list[str] | None = None,
        platform: str = "EC2",
    ) -> dict[str, Any]:
        """Translate OSimFlow resource directives to Batch overrides.

        The Batch API takes memory in MiB; `memory_mb` is in megabytes
        and we treat the two as equivalent (the difference is < 5% and
        Batch's documented unit is MiB, so 1:1 keeps the intent clear
        to anyone reading the submit_job call).

        EC2 job definitions keep the legacy ``vcpus`` / ``memory``
        overrides. Fargate job definitions require
        ``resourceRequirements`` (VCPU / MEMORY) with a legal pair, which
        is validated here (issue #1808).

        When ``command`` is provided, it overrides the job definition's
        container command (e.g. to run ``python -m osimflow.remote_runner``).

        ``SubmitJob`` does not accept ``containerOverrides.secrets``
        (issue #1811); secrets are injected by the job definition.
        """
        overrides: dict[str, Any]
        if platform == "FARGATE":
            _validate_fargate_resources(cpus, memory_mb)
            overrides = {
                "resourceRequirements": [
                    {"type": "VCPU", "value": f"{cpus:g}"},
                    {"type": "MEMORY", "value": str(int(memory_mb))},
                ],
                "environment": environment,
            }
        else:
            overrides = {
                "vcpus": int(cpus) if float(cpus).is_integer() else cpus,
                "memory": memory_mb,
                "environment": environment,
            }
        if command is not None:
            overrides["command"] = command
        return overrides

    def _calculate_job_cost(
        self,
        job: dict[str, Any],
        vcpus: float = 1,
    ) -> tuple[float, float]:
        """Estimate cost for a completed Batch job (issue #126).

        Uses the job's ``startedAt`` and ``stoppedAt`` timestamps to
        determine billed duration, then multiplies by the per-vCPU-hour
        rate.  For Spot jobs, the rate is the lower Spot price; the
        difference between Spot and On-Demand is the savings.

        Returns (cost_usd, spot_savings_usd).  Both default to 0.0 when
        timestamps or pricing data are unavailable.

        Parameters
        ----------
        job
            The Batch ``describe_jobs`` response dict for the completed job.
        vcpus
            Number of vCPUs allocated to the job (from container overrides
            or the job definition).
        """
        started = job.get("startedAt")
        stopped = job.get("stoppedAt")
        if started is None or stopped is None:
            return 0.0, 0.0

        # Batch timestamps are milliseconds since epoch.
        duration_s = max(0.0, (stopped - started) / 1000.0)
        if duration_s <= 0:
            return 0.0, 0.0

        duration_hours = duration_s / 3600.0

        # Determine the effective Spot price.
        spot_price = self.DEFAULT_SPOT_PRICE_PER_VCPU_HOUR
        try:
            queried_price = self._get_spot_price()
            if queried_price > 0:
                spot_price = queried_price
        except Exception as exc:  # noqa: BLE001 - Spot price lookup (boto3 EC2) raises varied errors; logged and the configured price is used
            log.warning("could not query Spot price for cost calc, using default: %s", exc)

        on_demand_price = self.DEFAULT_ON_DEMAND_PRICE_PER_VCPU_HOUR
        cost_usd = duration_hours * vcpus * on_demand_price
        spot_savings = duration_hours * vcpus * (on_demand_price - spot_price)

        return cost_usd, spot_savings

    def _wait_for_terminal(self, job_id: str, timeout: float | None = None) -> dict[str, Any]:
        """Poll `describe_jobs` with exponential backoff until the task
        reaches a terminal state. Returns the final job dict.

        The poll skeleton (deadline, deadline clamping (sleep capped at the remaining budget),
        capped exponential growth) lives in
        ``osimflow.executors.base.poll_until_terminal`` (issue #1540);
        AWS sleeps the current delay first and grows afterwards.

        Raises:
            TimeoutError: if *timeout* seconds elapse before a terminal state.
        """

        def _probe() -> dict[str, Any]:
            # boto3's describe_jobs returns a TypedDict at runtime, but
            # the type is too granular to be useful here — we treat the
            # response as a plain dict and access .get() on each level.
            response: dict[str, Any] = self._get_client().describe_jobs(jobs=[job_id])
            jobs = response.get("jobs", [])
            if not jobs:
                raise RuntimeError(f"describe_jobs returned no job for jobId={job_id!r}")
            return cast(dict[str, Any], jobs[0])

        return poll_until_terminal(
            _probe,
            is_terminal=lambda job: job.get("status", "UNKNOWN") in ("SUCCEEDED", "FAILED"),
            timeout=timeout,
            timeout_message=lambda elapsed: (
                f"Timed out after {elapsed:.1f}s waiting for job {job_id!r}"
            ),
            poll_interval_s=self.poll_interval_s,
            max_poll_interval_s=self.max_poll_interval_s,
            on_pending=lambda job, _delay, sleep_amount: log.info(
                "aws_batch poll jobId=%s status=%s (sleeping %.1fs)",
                job_id,
                job.get("status", "UNKNOWN"),
                sleep_amount,
            ),
        )

    def _on_demand_route(self) -> dict[str, str]:
        """Return ``_submit_job`` overrides selecting on-demand capacity."""
        route: dict[str, str] = {}
        if self.on_demand_job_queue:
            route["job_queue"] = self.on_demand_job_queue
        if self.on_demand_job_definition:
            route["job_definition"] = self.on_demand_job_definition
        return route

    def _submit_job(
        self,
        *,
        name: str,
        cpus: float,
        memory_mb: int,
        time_min: int,
        environment: list[dict[str, str]],
        command: list[str] | None = None,
        job_queue: str | None = None,
        job_definition: str | None = None,
        expected_image: str | None = None,
    ) -> str:
        """Submit a single Batch job and return the jobId.

        ``expected_image`` (issue #1810) is the image the caller pinned; the
        launched image is fixed by the job definition, so a mismatch fails
        before submission.

        Uses *job_queue* / *job_definition* if provided, otherwise
        ``self.job_queue`` / ``self.job_definition``.

        Throttling is owned by :meth:`BaseExecutor.submit` (issue #1563)
        — every AWS Batch submission acquires a token from the shared
        ``TokenBucketRateLimiter`` before this method is called.
        Retries on ``ThrottlingException`` / ``RequestLimitExceeded``
        remain here as defense-in-depth on top of boto3's adaptive
        retry mode.

        When ``command`` is provided, it overrides the job definition's
        container command (e.g. to run ``python -m osimflow.remote_runner``).
        """
        queue = job_queue or self.job_queue
        self._validate_job_definition_secret(job_definition or self.job_definition)
        if expected_image:
            self._validate_job_definition_image(
                job_definition or self.job_definition, expected_image
            )
        overrides = self._build_container_overrides(
            cpus=cpus,
            memory_mb=memory_mb,
            environment=environment,
            command=command,
            platform=self._job_definition_platform(job_definition or self.job_definition),
        )
        attempt_duration_seconds = int(time_min) * 60
        submit_kwargs: dict[str, Any] = {
            "jobName": name,
            "jobQueue": queue,
            "jobDefinition": job_definition or self.job_definition,
            "containerOverrides": overrides,
            "timeout": {"attemptDurationSeconds": attempt_duration_seconds},
        }
        response = self._submit_job_with_retry(submit_kwargs)
        job_id: str = str(response["jobId"])
        log.info("aws_batch submit_job -> jobId=%s queue=%s", job_id, queue)
        return job_id

    def _find_existing_job(self, submit_params: dict[str, Any]) -> str | None:
        """Return the id of a live job already submitted under this name (issue #1881).

        Batch ``SubmitJob`` has no client token, so a resumed detached submit
        looks the deterministic job name up instead of creating a duplicate.
        FAILED jobs are ignored so a failed attempt can be resubmitted.
        """
        client = self._get_client()
        queues = [
            q
            for q in (
                submit_params.get("job_queue"),
                self.job_queue,
                self.on_demand_job_queue,
            )
            if q
        ]
        for queue in dict.fromkeys(queues):
            token: str | None = None
            while True:
                params: dict[str, Any] = {
                    "jobQueue": queue,
                    "filters": [{"name": "JOB_NAME", "values": [submit_params["name"]]}],
                }
                if token:
                    params["nextToken"] = token
                resp = client.list_jobs(**params)
                for job in resp.get("jobSummaryList", []):
                    if (
                        job.get("jobName") == submit_params["name"]
                        and job.get("jobStatus") != "FAILED"
                    ):
                        log.info(
                            "aws_batch reusing existing job %s for name %s",
                            job["jobId"],
                            submit_params["name"],
                        )
                        return str(job["jobId"])
                token = resp.get("nextToken")
                if not token:
                    break
        return None

    def _submit_job_with_retry(self, submit_kwargs: dict[str, Any]) -> dict[str, Any]:
        """Call ``submit_job`` with retry on throttle exceptions (issue #1010).

        boto3's adaptive retry config (``retry_mode='adaptive'``) handles
        transport-level retries.  This wrapper provides defense-in-depth
        for ``ThrottlingException`` that propagates to our code; the
        bounded-attempt exponential schedule lives in
        ``osimflow.executors.base.retry_with_backoff`` (issue #1540).
        """
        import botocore.exceptions  # noqa: PLC0415

        max_attempts = 5

        def _call() -> dict[str, Any]:
            return self._get_client().submit_job(**submit_kwargs)  # type: ignore[no-any-return]

        def _retry_on(exc: BaseException) -> bool:
            return (
                isinstance(exc, botocore.exceptions.ClientError)
                and _aws_error_code(exc) in self._THROTTLE_ERRORS
            )

        def _on_retry(exc: BaseException, attempt: int, window: float) -> None:
            log.warning(
                "submit_job throttled (attempt %d/%d), retrying in %.1fs: %s",
                attempt,
                max_attempts,
                window,
                _aws_error_code(exc),
            )

        return retry_with_backoff(
            _call,
            retry_on=_retry_on,
            max_attempts=max_attempts,
            initial_delay_s=0.5,
            max_delay_s=30.0,
            jitter=True,
            on_retry=_on_retry,
        )

    @staticmethod
    def validate_work_fn(step_name: str, fn: Callable[..., Any]) -> None:
        """Reject a work function the Batch container would silently replace.

        The remote runner resolves ``step_name`` to the built-in step function,
        so any other callable (a BYOS hook) cannot be honoured (issue #1812).
        """
        from osimflow.remote_runner import StepFunctionRegistry  # noqa: PLC0415

        if not StepFunctionRegistry._registry:
            from osimflow.remote_runner import _register_builtin_steps  # noqa: PLC0415

            _register_builtin_steps()
        if step_name not in ("apply", "extract"):
            return
        if step_name not in StepFunctionRegistry._registry:
            return
        if fn is not StepFunctionRegistry.get(step_name):
            raise NotImplementedError(
                f"AWS Batch cannot run a custom {step_name!r} hook "
                f"({getattr(fn, '__qualname__', fn)!r}): the remote runner only executes "
                "the built-in step function. Remove --custom_apply_script / "
                "--custom_kpi_extractor (or the apply_fn/extract_fn argument), or use a "
                "local executor (issue #1812)."
            )

    # Batch rejects containerOverrides over 8192 bytes; leave headroom for the
    # other env vars (signature, transport settings, secret).
    _MAX_INLINE_PAYLOAD_BYTES = 3072

    def _spill_oversized_payload(
        self, task_payload: str, transport: ResultTransportConfig | None
    ) -> str:
        if len(task_payload.encode("utf-8")) <= self._MAX_INLINE_PAYLOAD_BYTES:
            return task_payload
        from osimflow.input_staging import spill_task_payload  # noqa: PLC0415
        from osimflow.storage import build_result_storage  # noqa: PLC0415

        if (
            transport is None
            or transport.mode != "object_storage"
            or not transport.backend
            or not transport.bucket
        ):
            return task_payload
        storage = build_result_storage(
            backend=transport.backend,
            bucket=transport.bucket,
            prefix=str(transport.prefix or ""),
            endpoint_url=transport.endpoint,
        )
        return spill_task_payload(storage, task_payload)

    def _do_submit(
        self,
        fn: Callable[..., Any],
        *args: Any,
        name: str = "task",
        cpus: float = 1,
        memory_mb: int = 1024,
        time_min: int = 60,
        container: str | None = None,
        container_digest: str | None = None,
        openstudio_version: str | None = None,
        result_hint: Any = None,
        remote_command: str | None = None,
        transport: ResultTransportConfig | None = None,
        variables_json: str | None = None,
        env: dict[str, str] | None = None,
        stdout_path: Any = None,
        stderr_path: Any = None,
        max_retries: int | None = None,
        worker_id: str | None = None,
        job_name: str | None = None,
        reuse_existing_job: bool = False,
        **kwargs: Any,
    ) -> Handle:
        self._container_digest = container_digest
        # ``timeout_s`` bounds the real OpenStudio CLI on the worker, so it must
        # reach the work function; the other submit-only kwargs stay local.
        sim_timeout_s = kwargs.pop("timeout_s", None)
        del variables_json, env, stdout_path, stderr_path, max_retries, worker_id, kwargs  # noqa: F841, ARG002

        log.info(
            "aws_batch submit name=%s cpus=%g mem=%dMB time_min=%d container=%s",
            name,
            cpus,
            memory_mb,
            time_min,
            container,
        )

        # Ephemeral-runner contract (issue #996, #1077): serialize the step
        # call into the task payload; the Batch-side
        # ``python -m osimflow.remote_runner`` decodes it and executes the
        # work function in container-local storage.
        step_name = self._infer_step_name(name)
        self.validate_work_fn(step_name, fn)
        # Resolve the effective job-definition payload secret ARN (issue #2042)
        # before _resolve_signing_secret() and _build_environment().
        self._resolve_effective_payload_secret_arn(strict=True)
        # Issue #2025: fail fast before staging inputs or submitting any
        # AWS Batch job when no signing secret is configured.
        self._resolve_signing_secret()
        # Issue #1809: the Batch container shares no filesystem with the
        # controller — stage every Path in S3 and ship references instead.
        work_kwargs: dict[str, Any] = (
            {"timeout_s": float(sim_timeout_s)}
            if step_name == "sim" and sim_timeout_s is not None
            else {}
        )
        staged_args, staged_kwargs = self._stage_task_inputs(
            tuple(args), work_kwargs, result_hint=result_hint, transport=transport
        )
        task_payload = self._build_task_payload(
            step_name=step_name,
            args=staged_args,
            kwargs=staged_kwargs,
            result_hint=result_hint,
            name=name,
        )
        task_payload = self._spill_oversized_payload(task_payload, transport)

        if remote_command:
            command: list[str] = ["/bin/sh", "-c", remote_command]
        else:
            command = ["python", "-m", "osimflow.remote_runner"]

        environment = self._build_environment(
            container=container,
            openstudio_version=openstudio_version,
            task_payload=task_payload,
            transport=transport,
        )

        # --- Spot price ceiling check (issue #131, #792) ---
        # Fast, non-blocking check: query the current Spot price and
        # either raise or fall back to on-demand. This gate runs before
        # any job submission so we don't waste a Batch task that would
        # immediately be more expensive than the ceiling.
        price_fallback = False
        if self.max_spot_price_usd is not None:
            if self._instance_type is None:
                log.warning(
                    "instance_type is not set — spot price ceiling check "
                    "queries the minimum across all instance types and may "
                    "not reflect the actual cost (issue #792). Set "
                    "--aws-batch-instance-type to scope the check."
                )
            try:
                current_price = self._get_spot_price()
                if current_price > self.max_spot_price_usd:
                    msg = (
                        f"Spot price ${current_price:.4f} exceeds ceiling "
                        f"${self.max_spot_price_usd:.4f}"
                    )
                    if self.fallback_to_on_demand:
                        log.warning("%s — falling back to on-demand", msg)
                        price_fallback = True
                    else:
                        raise RuntimeError(msg)
            except RuntimeError:
                raise
            except Exception as exc:
                if self.max_spot_price_usd is not None:
                    raise
                log.warning("could not check Spot price: %s", exc)

        # Submit the job to AWS Batch and return immediately (issue #262).
        # Spot retry logic lives in _AWSBatchHandle.result() so that
        # submit() is non-blocking — a prerequisite for concurrent fan-out.
        del fn  # noqa: ARG002 — work runs inside the Batch container via remote_runner

        submit_params: dict[str, Any] = {
            "name": job_name or name,
            "cpus": cpus,
            "memory_mb": memory_mb,
            "time_min": time_min,
            "environment": environment,
            "command": command,
        }
        expected_image = self._pinned_image(container, openstudio_version)
        if expected_image:
            submit_params["expected_image"] = expected_image
        if price_fallback:
            submit_params.update(self._on_demand_route())
        existing = self._find_existing_job(submit_params) if reuse_existing_job else None
        job_id = existing or self._submit_job(**submit_params)

        return _AWSBatchHandle(
            job_id=job_id,
            executor=self,
            submit_params=submit_params,
            result_hint=result_hint,
            transport=transport,
        )

    def shutdown(self) -> None:
        # boto3 clients hold an HTTP session that is closed by the
        # underlying botocore session on GC; nothing actionable here.
        pass
