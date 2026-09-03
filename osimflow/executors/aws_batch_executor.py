"""AWS Batch executor for OSimFlow campaigns (issues #5, #1010, #131).

Wraps ``boto3.client('batch').submit_job`` to launch one Batch task per
call, then polls ``describe_jobs`` (exponential backoff) until terminal
state. Spot handling: ``_SpotPriceCache`` (60 s TTL) feeds the ceiling
check, ``_TokenBucketRateLimiter`` (shared, token-bucket) throttles
fan-out submits, and the handle retries Spot interruptions up to
``max_retries`` before falling back to on-demand.

Security: credentials resolve through IAM-role-only botocore providers
by default (``allow_long_lived_credentials=False``); boto3 stays a lazy
in-constructor import so local/slurm users never pay for it.

Extracted from ``osimflow/executors/__init__.py`` (issue #1463).
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future
from typing import Any, ClassVar, cast

from osimflow.executors.base import (
    BaseExecutor,
    Handle,
    PollingHandle,
    PollOutcome,
    poll_until_terminal,
    retry_with_backoff,
)
from osimflow.executors.transport import coerce_transport_mode, resolve_result_for_callback
from osimflow.task_payload_hmac import build_signature_env

log = logging.getLogger("osimflow.executors")


def _aws_error_code(exc: BaseException) -> str:
    """Extract AWS/boto error code from an exception, or empty string if not applicable."""
    try:
        return exc.response.get("Error", {}).get("Code", "") if hasattr(exc, "response") else ""
    except Exception:  # noqa: BLE001
        return ""


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
        result_transport_mode: str = "auto",
        result_storage_backend: str | None = None,
        result_storage_bucket: str | None = None,
        result_storage_prefix: str | None = None,
        result_storage_endpoint: str | None = None,
    ) -> None:
        self.job_id = job_id
        self._executor = executor
        self._submit_params = submit_params
        self._result_hint = result_hint
        # Result-transport contract (issue #1333): the handle materializes
        # object-storage artifacts on `.result()` so Campaign callbacks
        # receive local paths — identical to `_NomadHandle` and the
        # Kubernetes handle.
        self._result_transport_mode = coerce_transport_mode(result_transport_mode)
        self._result_storage_backend = result_storage_backend
        self._result_storage_bucket = result_storage_bucket
        self._result_storage_prefix = result_storage_prefix
        self._result_storage_endpoint = result_storage_endpoint
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
        # Issue #1463: resolved through the package namespace at call time
        # so tests that monkeypatch ``osimflow.executors.
        # materialize_object_storage_result`` keep intercepting this call
        # site after the executor moved out of the package __init__.
        from osimflow.executors import materialize_object_storage_result

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

    def _is_spot_interruption(self, reason: str | None) -> bool:
        return bool(self._executor._is_spot_interruption(reason))  # noqa: SLF001

    def _resubmit(self) -> None:
        self.job_id = self._executor._submit_job(**self._submit_params)  # noqa: SLF001
        self.worker_id = self.job_id

    def _submit_on_demand(self) -> None:
        # AWS falls back by resubmitting with the same submit params —
        # the queue/spot selection is implicit in the job queue.
        self.job_id = self._executor._submit_job(**self._submit_params)  # noqa: SLF001
        self.worker_id = self.job_id

    def _failure_error(self, job: Any) -> RuntimeError:
        status = job.get("status")
        reason = job.get("statusReason", "")
        return RuntimeError(f"AWS Batch job {self.job_id!r} {status}: {reason}")

    def _fallback_failure_error(self, job: Any) -> RuntimeError:
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
        for attempt in range(self._GHOST_RETRIES):
            try:
                response = self._executor._get_client().describe_jobs(  # noqa: SLF001
                    jobs=[self.job_id]
                )
            except Exception as exc:  # noqa: BLE001 — never raise from done()
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
# Token-bucket rate limiter + spot-price cache (issue #1010)
# ---------------------------------------------------------------------------
class _TokenBucketRateLimiter:
    """Thread-safe token-bucket rate limiter for AWS Batch submit throttling.

    Shared across all ``AWSBatchExecutor`` instances via
    :meth:`get_shared` so that concurrent submissions from multiple
    executor objects stay within the per-account submit_job rate limit
    (issue #1010).  AWS Batch documents ``submit_job`` at 1 000 TPS per
    account; the default of 800 RPS leaves headroom for burst contention
    between concurrent fan-out threads.
    """

    DEFAULT_RPS: float = 800.0

    _INSTANCES: ClassVar[dict[float, _TokenBucketRateLimiter]] = {}
    _INSTANCES_LOCK: ClassVar[threading.Lock] = threading.Lock()

    def __init__(self, rps: float = DEFAULT_RPS) -> None:
        self._disabled: bool = rps <= 0
        self._rate: float = float(rps) if not self._disabled else 0.0
        self._capacity: int = max(int(rps), 1) if not self._disabled else 1
        self._tokens: float = float(self._capacity)
        self._last_refill: float = time.monotonic()
        self._lock: threading.Lock = threading.Lock()

    @classmethod
    def get_shared(cls, rps: float | None = None) -> _TokenBucketRateLimiter:
        """Return a shared limiter for the requested RPS (singleton per RPS).

        All executor instances requesting the same RPS share the same
        bucket, ensuring the aggregate submit rate stays bounded.
        ``rps=None`` falls back to the default (800). ``rps=0`` disables
        rate limiting entirely (use with caution).
        """
        effective: float = cls.DEFAULT_RPS if rps is None else float(rps)
        with cls._INSTANCES_LOCK:
            if effective not in cls._INSTANCES:
                cls._INSTANCES[effective] = cls(rps=effective)
            return cls._INSTANCES[effective]

    def acquire(self) -> None:
        """Block until a token is available, then consume one."""
        if self._disabled:
            return
        while True:
            with self._lock:
                now = time.monotonic()
                elapsed = now - self._last_refill
                self._tokens = min(
                    float(self._capacity),
                    self._tokens + elapsed * self._rate,
                )
                self._last_refill = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                deficit = 1.0 - self._tokens
                wait_s = deficit / self._rate
            # Sleep without holding the lock so other threads aren't blocked.
            time.sleep(wait_s)


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

    Security: the boto3 client
    sources credentials from the IAM role attached to the Batch compute
    environment. The constructor does **not** accept
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

    _DEFAULT_SUBMIT_RPS: float = 800.0

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
    ):
        # Lazy import: keeps the boto3 import cost off the local /
        # slurm executor paths. ImportError here is intentional: the
        # user opted into the [aws] extra, so a missing boto3 is a
        # user error, not a silent fallback.
        import boto3  # noqa: PLC0415
        import botocore.credentials  # noqa: PLC0415
        import botocore.session  # noqa: PLC0415
        from botocore.config import Config as BotoConfig  # noqa: PLC0415

        # Security: by default, restrict credential providers to IAM role
        # only (EC2 instance metadata / ECS container credentials).
        # This prevents accidental use of long-lived AWS_ACCESS_KEY_ID /
        # AWS_SECRET_ACCESS_KEY from the environment or ~/.aws/credentials.
        # Set allow_long_lived_credentials=True to opt out (not recommended
        # for production). See issue #1160.
        self._allow_long_lived_credentials = allow_long_lived_credentials

        if not allow_long_lived_credentials:
            # Check for long-lived credentials in environment and warn.
            import os

            env_creds = []
            if os.environ.get("AWS_ACCESS_KEY_ID"):
                env_creds.append("AWS_ACCESS_KEY_ID")
            if os.environ.get("AWS_SECRET_ACCESS_KEY"):
                env_creds.append("AWS_SECRET_ACCESS_KEY")
            if os.environ.get("AWS_SESSION_TOKEN"):
                env_creds.append("AWS_SESSION_TOKEN")
            if env_creds:
                log.warning(
                    "AWSBatchExecutor: long-lived AWS credentials detected in "
                    "environment (%s). These will be IGNORED because "
                    "allow_long_lived_credentials=False (default). The executor "
                    "will only use IAM role credentials from the EC2/ECS "
                    "metadata service. Set allow_long_lived_credentials=True "
                    "to opt out (not recommended for production).",
                    ", ".join(env_creds),
                )

            # Create a custom botocore session with ONLY the IAM role
            # credential providers. Filter the default chain to keep only:
            # - InstanceMetadataProvider: IMDSv2 (EC2 instance profile)
            # - ContainerProvider: ECS/Fargate/Batch task role
            # - OriginalEC2Provider: Legacy IMDSv1 (EC2 instance profile)
            # - BotoProvider: boto config (for region, etc.)
            # This excludes: EnvProvider, SharedCredentialProvider, ConfigProvider,
            # ProcessProvider, SSOProvider, LoginProvider, AssumeRoleProvider, etc.
            session = botocore.session.get_session()
            default_resolver = session.get_component("credential_provider")
            iam_role_provider_names = {
                "InstanceMetadataProvider",
                "ContainerProvider",
                "OriginalEC2Provider",
                "BotoProvider",
            }
            iam_role_providers = [
                p for p in default_resolver.providers if type(p).__name__ in iam_role_provider_names
            ]
            restricted_resolver = botocore.credentials.CredentialResolver(iam_role_providers)
            session.register_component("credential_provider", restricted_resolver)
            # Keep the boto3 MODULE as the client-factory seam (tests patch
            # ``boto3.client``); carry the restricted session as a kwarg so
            # ``self._boto3.client(...)`` stays interceptable while still
            # routing credentials through IAM-role providers only.
            self._botocore_session: Any = session
        else:
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
        self.job_queue = job_queue
        self.job_definition = job_definition or "osimflow-job-def"
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
        # boto3 retry config with adaptive mode for ThrottlingException
        # handling (issue #1010). Adaptive mode uses client-side rate
        # limiting + exponential backoff with jitter.
        self._retry_config = BotoConfig(
            retries={"mode": "adaptive", "max_attempts": 10},
        )
        # Token-bucket submit rate limiter, shared across all executor
        # instances (issue #1010). Prevents ThrottlingException at fan-out.
        self._submit_limiter = _TokenBucketRateLimiter.get_shared(rps=submit_rps)
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

    def _get_client(self) -> Any:
        """Lazy boto3 Batch client construction.

        Deferring to first use lets the constructor succeed on hosts
        that have boto3 installed but no AWS config (e.g. CI runners
        that only test the executor wiring with mocked clients).
        Production deployments will have AWS_REGION set or an IAM
        role / ~/.aws/config in place.
        """
        if self._client is None:
            self._client = self._boto3.client("batch", **self._client_kwargs())
        return self._client

    def _get_ec2_client(self) -> Any:
        """Lazy boto3 EC2 client for Spot price queries."""
        if self._ec2_client is None:
            self._ec2_client = self._boto3.client("ec2", **self._client_kwargs())
        return self._ec2_client

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
        result_transport_mode: str | None = None,
        result_storage_backend: str | None = None,
        result_storage_bucket: str | None = None,
        result_storage_prefix: str | None = None,
        result_storage_endpoint: str | None = None,
    ) -> list[dict[str, str]]:
        """Build the Batch `environment` list from the per-submit kwargs.

        The serialized task payload travels in ``OSIMFLOW_TASK_PAYLOAD`` and
        the result-transport contract in the ``OSIMFLOW_RESULT_*`` vars so
        ``osimflow.remote_runner`` can execute the step and push results to
        object storage (issue #996). ``OSIMFLOW_STUB_SIM`` is propagated
        from the orchestrator environment when set so remote pods honour
        the orchestrator's stub-vs-real CLI choice.
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
        if task_payload is not None:
            env.append({"name": "OSIMFLOW_TASK_PAYLOAD", "value": task_payload})
            # Issue #1445: when a shared secret is configured, sign the
            # exact payload bytes and propagate secret + signature so
            # the remote_runner verifies before decoding/executing
            # (same contract as the Nomad / Azure / Google / DockerSwarm
            # paths). No-op in legacy unsigned mode.
            env.extend(
                {"name": key, "value": value}
                for key, value in build_signature_env(task_payload).items()
            )
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

    def _build_container_overrides(
        self,
        *,
        cpus: int,
        memory_mb: int,
        environment: list[dict[str, str]],
        command: list[str] | None = None,
    ) -> dict[str, Any]:
        """Translate OSimFlow resource directives to Batch overrides.

        The Batch API takes memory in MiB; `memory_mb` is in megabytes
        and we treat the two as equivalent (the difference is < 5% and
        Batch's documented unit is MiB, so 1:1 keeps the intent clear
        to anyone reading the submit_job call).

        When ``command`` is provided, it overrides the job definition's
        container command (e.g. to run ``python -m osimflow.remote_runner``).
        """
        overrides: dict[str, Any] = {
            "vcpus": cpus,
            "memory": memory_mb,
            "environment": environment,
        }
        if command is not None:
            overrides["command"] = command
        return overrides

    def _calculate_job_cost(
        self,
        job: dict[str, Any],
        vcpus: int = 1,
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
        except Exception as exc:
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

    def _submit_job(
        self,
        *,
        name: str,
        cpus: int,
        memory_mb: int,
        time_min: int,
        environment: list[dict[str, str]],
        command: list[str] | None = None,
        job_queue: str | None = None,
    ) -> str:
        """Submit a single Batch job and return the jobId.

        Uses *job_queue* if provided, otherwise ``self.job_queue``.

        Throttles via the shared token-bucket rate limiter (issue #1010)
        and retries on ``ThrottlingException`` / ``RequestLimitExceeded``
        with exponential backoff as defense-in-depth on top of boto3's
        adaptive retry mode.

        When ``command`` is provided, it overrides the job definition's
        container command (e.g. to run ``python -m osimflow.remote_runner``).
        """
        queue = job_queue or self.job_queue
        overrides = self._build_container_overrides(
            cpus=cpus,
            memory_mb=memory_mb,
            environment=environment,
            command=command,
        )
        attempt_duration_seconds = int(time_min) * 60
        submit_kwargs: dict[str, Any] = {
            "jobName": name,
            "jobQueue": queue,
            "jobDefinition": self.job_definition,
            "containerOverrides": overrides,
            "timeout": {"attemptDurationSeconds": attempt_duration_seconds},
        }
        # Acquire a rate-limiter token before submitting (issue #1010).
        self._submit_limiter.acquire()
        response = self._submit_job_with_retry(submit_kwargs)
        job_id: str = str(response["jobId"])
        log.info("aws_batch submit_job -> jobId=%s queue=%s", job_id, queue)
        return job_id

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
            "aws_batch submit name=%s cpus=%d mem=%dMB time_min=%d container=%s",
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
        task_payload = self._build_task_payload(
            step_name=step_name,
            args=args,
            kwargs={},
            result_hint=result_hint,
            name=name,
        )

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

        # --- Spot price ceiling check (issue #131, #792) ---
        # Fast, non-blocking check: query the current Spot price and
        # either raise or fall back to on-demand. This gate runs before
        # any job submission so we don't waste a Batch task that would
        # immediately be more expensive than the ceiling.
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
            "name": name,
            "cpus": cpus,
            "memory_mb": memory_mb,
            "time_min": time_min,
            "environment": environment,
            "command": command,
        }
        job_id = self._submit_job(**submit_params)

        return _AWSBatchHandle(
            job_id=job_id,
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

    def shutdown(self) -> None:
        # boto3 clients hold an HTTP session that is closed by the
        # underlying botocore session on GC; nothing actionable here.
        pass
