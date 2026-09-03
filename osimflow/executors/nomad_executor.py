"""HashiCorp Nomad batch executor for OSimFlow campaigns (issues #27, #135).

Stdlib-only transport (``urllib.request``) around the Nomad HTTP API —
no python-nomad dependency. Supports parameterized-job dispatch and
one-job-per-sample modes, allocation-based result collection, and the
``_retry_nomad_request`` 5xx/URLError retry helper (issue #1395) that
absorbs HA leader-election blips with exponential backoff.

Extracted from ``osimflow/executors/__init__.py`` (issue #1463).
"""

from __future__ import annotations

import json
import logging
import math
import os
import random  # noqa: F401 — patch seam: tests patch osimflow.executors.random.uniform
import re
import threading
import time
import warnings
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, ClassVar, cast

from osimflow.byos_contract import BYOS_CONTRACT_VERSION
from osimflow.executors.base import (
    BaseExecutor,
    Handle,
    PollingHandle,
    PollOutcome,
    poll_until_terminal,
    retry_with_backoff,
)
from osimflow.executors.transport import coerce_transport_mode, resolve_result_for_callback
from osimflow.task_payload_hmac import (
    TASK_PAYLOAD_SECRET_ENV,
    TASK_PAYLOAD_SECRET_META_KEY,
    TASK_PAYLOAD_SIG_ENV,
    TASK_PAYLOAD_SIG_META_KEY,
    build_signature_env,
)

log = logging.getLogger("osimflow.executors")


def _nomad_error_code(exc: Exception) -> int:
    """Extract HTTP status code from a Nomad/Consulate exception, or 0 if not applicable."""
    try:
        return getattr(exc, "status_code", 0) or 0
    except Exception:  # noqa: BLE001
        return 0


# ---------------------------------------------------------------------------
# Nomad HTTP retry (issue #1395)
# ---------------------------------------------------------------------------
# Nomad HA failover (infra/nomad/examples/ha/) can produce transient
# 5xx responses during leader election. A single blip must not propagate
# as a sample failure — wrap every urllib.request call with this helper
# so 500/502/503/504 and URLError are absorbed with exponential backoff.
#
# Mirrors the AWS Batch ``_submit_job_with_retry`` defense-in-depth
# contract: 5 attempts, per-retry backoff capped at 30s, jittered sleep.
_NOMAD_RETRYABLE_HTTP_CODES: frozenset[int] = frozenset({500, 502, 503, 504})
_NOMAD_RETRY_MAX_ATTEMPTS: int = 5
_NOMAD_RETRY_INITIAL_DELAY_S: float = 0.5
_NOMAD_RETRY_CAP_S: float = 30.0


def _retry_nomad_request(
    call_fn: Callable[[], Any],
    *,
    max_attempts: int = _NOMAD_RETRY_MAX_ATTEMPTS,
    total_cap_seconds: float = _NOMAD_RETRY_CAP_S,
) -> Any:
    """Invoke *call_fn*, retrying on transient 5xx and URLError (issue #1395).

    Nomad HA leader election and brief proxy restarts produce 502/503/504
    blips; this helper absorbs them so a sample does not fail on a
    network hiccup. Mirrors the AWS Batch ``_submit_job_with_retry``
    pattern: exponential backoff with up to *max_attempts* attempts and a
    per-retry sleep cap of *total_cap_seconds* (defaults to 5 attempts /
    30 s). The bounded-attempt schedule lives in
    ``osimflow.executors.base.retry_with_backoff`` (issue #1540).

    Only HTTPError codes in {500, 502, 503, 504} and URLError are
    retried; every other exception (HTTPError 4xx, RuntimeError, etc.)
    propagates immediately so genuine failures surface without delay.
    """
    import urllib.error  # noqa: PLC0415

    def _retry_on(exc: BaseException) -> bool:
        if isinstance(exc, urllib.error.HTTPError):
            return exc.code in _NOMAD_RETRYABLE_HTTP_CODES
        return isinstance(exc, urllib.error.URLError)

    def _on_retry(exc: BaseException, attempt: int, window: float) -> None:
        if isinstance(exc, urllib.error.HTTPError):
            log.warning(
                "Nomad HTTP %s (attempt %d/%d), retrying in %.1fs",
                exc.code,
                attempt,
                max_attempts,
                window,
            )
        else:
            log.warning(
                "Nomad URLError (attempt %d/%d), retrying in %.1fs: %s",
                attempt,
                max_attempts,
                window,
                exc,
            )

    return retry_with_backoff(
        call_fn,
        retry_on=_retry_on,
        max_attempts=max_attempts,
        initial_delay_s=_NOMAD_RETRY_INITIAL_DELAY_S,
        max_delay_s=total_cap_seconds,
        jitter=True,
        on_retry=_on_retry,
    )


# ---------------------------------------------------------------------------
# Nomad executor (issue #27)
# ---------------------------------------------------------------------------
# DNS-1123 label: lowercase alphanumeric + dashes, max 63 chars. Nomad
# job names must satisfy this; the executor slugifies user-supplied
# names so a sample id like "sample-0" lands as a valid job name
# without Nomad rejecting the job spec.
_DNS1123_LABEL = re.compile(r"[^a-z0-9-]+")
_DNS1123_TRIM = re.compile(r"^-+|-+$")


def _slugify_job_name(name: str) -> str:
    """Convert a user-supplied task name into a DNS-1123 label.

    Nomad rejects job specs whose ``Name`` is not a DNS-1123 label. The
    Campaign passes names like ``"sim_<sample_id>"`` (snake_case + angle
    chars); we need to lowercase, replace non-alphanumerics with
    dashes, trim leading/trailing dashes, and clip to 63 chars.
    """
    slug = name.lower()
    slug = _DNS1123_LABEL.sub("-", slug)
    slug = _DNS1123_TRIM.sub("", slug)
    slug = slug[:63]
    if not slug:
        slug = "task"
    return slug


class _NomadClient:
    """Thin HTTP client for the Nomad HTTP API.

    Wraps ``urllib.request.urlopen`` so the executor can be tested by
    patching a single function (the same pattern the boto3 executor
    uses with ``boto3.client``). The client carries the Nomad address
    and ACL token (sourced from the environment at construction time)
    and exposes ``submit_job(spec)`` / ``get_allocation(alloc_id)``
    helpers that return parsed JSON.

    The client is lazy-imported (``urllib.request`` is stdlib, so this
    is mostly about deferring the import out of ``__init__.py``). No
    third-party HTTP library is required.

    TLS verification: when ``verify_tls`` is ``True`` (the default),
    the client delegates to ``urllib.request.urlopen`` (the stdlib
    default, which uses system CA certs and verifies the server cert).
    When ``False``, the client builds a custom opener with an SSL
    context that skips certificate verification (for development with
    self-signed certs). Tests patch ``urllib.request.urlopen`` to
    intercept the wire format; this works because the
    ``verify_tls=True`` path calls ``self.urlopen`` directly.
    """

    def __init__(
        self,
        address: str,
        token: str | None,
        verify_tls: bool = True,
        tls: bool = False,
        cert: str | None = None,
        key: str | None = None,
        ca_cert: str | None = None,
    ) -> None:
        import ssl  # noqa: PLC0415
        import urllib.request  # noqa: PLC0415

        self.address = address.rstrip("/")
        self.token = token
        self.verify_tls = verify_tls
        self.tls = tls
        self.cert = cert
        self.key = key
        self.ca_cert = ca_cert

        # Store urlopen so tests can patch ``urllib.request.urlopen``
        # and intercept every request through ``self.urlopen``.
        self.urlopen = urllib.request.urlopen

        # Build custom opener based on TLS configuration:
        # - tls=False: plain HTTP (no TLS)
        # - tls=True, verify_tls=True, cert+key: mTLS with custom client certs
        # - tls=True, verify_tls=True: TLS with system CA certs
        # - tls=True, verify_tls=False: TLS with CERT_NONE (skip verification)
        # - verify_tls=False alone (tls=False): legacy behavior for dev with
        #   self-signed certs - uses HTTPSHandler with CERT_NONE even without tls=True
        self._opener: urllib.request.OpenerDirector | None = None
        self._ssl_context: ssl.SSLContext | None = None

        if not tls and verify_tls:
            # Plain HTTP, no TLS
            pass
        elif verify_tls and cert and key:
            # mTLS: custom SSL context with client cert + CA verification.
            # Defer loading cert/key/ca until first request so tests can
            # verify parameters without requiring real certificate files.
            ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ssl_context.check_hostname = True
            ssl_context.verify_mode = ssl.CERT_REQUIRED
            self._ssl_context = ssl_context
            self._opener = urllib.request.build_opener(
                urllib.request.HTTPSHandler(context=ssl_context),
            )
        elif verify_tls:
            # TLS with system CA certs (no client cert)
            self._opener = None  # use stdlib urlopen
        else:
            # verify_tls=False: skip cert verification (legacy dev mode)
            ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE
            self._opener = urllib.request.build_opener(
                urllib.request.HTTPSHandler(context=ssl_context),
            )

    def _request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Issue a single HTTP request and return the parsed JSON body.

        ``path`` is appended to ``self.address``; the ACL token is
        forwarded as the ``X-Nomad-Token`` header (Nomad's documented
        header for external clients — see
        https://developer.hashicorp.com/nomad/api-docs).
        """
        import urllib.error  # noqa: PLC0415
        import urllib.request  # noqa: PLC0415

        url = f"{self.address}{path}"
        data: bytes | None = None
        headers: dict[str, str] = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if self.token:
            # Nomad canonicalizes the header name to lowercase.
            headers["X-Nomad-Token"] = self.token
        request = urllib.request.Request(
            url,
            data=data,
            headers=headers,
            method=method,
        )
        try:
            # Lazily load certificates on first mTLS request.
            if self._ssl_context is not None:
                if self.ca_cert:
                    self._ssl_context.load_verify_locations(cafile=self.ca_cert)
                if self.cert and self.key:
                    self._ssl_context.load_cert_chain(certfile=self.cert, keyfile=self.key)
                self._ssl_context = None  # only load once

            # Issue #1395: retry transient 5xx and URLError with
            # exponential backoff so Nomad HA leader-election blips do
            # not propagate as sample failures. The retry helper returns
            # the response object on success; we then read it inside a
            # context manager so the socket is closed on success.
            def _open() -> Any:
                if self._opener is not None:
                    # verify_tls=False or mTLS: use the custom opener.
                    return self._opener.open(request)
                # verify_tls=True: use stdlib urlopen (system CA certs, test-mockable).
                return self.urlopen(request)

            resp = _retry_nomad_request(_open)
            with resp:
                payload = resp.read()
        except urllib.error.HTTPError as exc:  # pragma: no cover — error path
            # Non-retryable HTTPError (4xx), or a retryable 5xx that
            # exhausted 5 attempts. Convert to RuntimeError so the
            # caller-facing contract is preserved.
            body_text = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(
                f"Nomad {method} {path} failed: HTTP {exc.code} {body_text!r}"
            ) from exc
        except urllib.error.URLError as exc:  # pragma: no cover — error path
            # URLError that exhausted retries (DNS, connection refused,
            # etc.). Convert to RuntimeError to match the HTTPError path.
            raise RuntimeError(f"Nomad {method} {path} failed: {exc.reason}") from exc
        if not payload:
            return {}
        return cast(dict[str, Any], json.loads(payload.decode("utf-8")))

    def submit_job(self, spec: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/v1/jobs", body=spec)

    def register_job(self, spec: dict[str, Any]) -> dict[str, Any]:
        """Register (or update) a job spec via ``POST /v1/jobs``.

        Idempotent — re-registering an identical job is a no-op.
        Returns the Nomad response (``JobID``, ``EvalID``, ``Index``).
        """
        return self._request("POST", "/v1/jobs", body=spec)

    def dispatch_job(self, job_id: str, meta: dict[str, str] | None = None) -> dict[str, Any]:
        """Dispatch a parameterized job via ``POST /v1/job/{job_id}/dispatch``.

        ``meta`` is the per-dispatch payload that lands as
        ``NOMAD_META_<key>`` env vars inside the task container. Returns
        the Nomad dispatch response carrying ``JobID`` and ``EvalID`` of
        the child (dispatched) job.
        """
        body: dict[str, Any] = {}
        if meta:
            body["Meta"] = meta
        return self._request("POST", f"/v1/job/{job_id}/dispatch", body=body)

    def get_allocation(self, alloc_id: str) -> dict[str, Any]:
        return self._request("GET", f"/v1/allocation/{alloc_id}")

    def get_eval_allocations(self, eval_id: str) -> list[dict[str, Any]]:
        """Return the allocations created by an evaluation.

        ``GET /v1/evaluation/{eval_id}/allocations`` returns a list of
        allocation stubs. Each stub carries ``ID``, ``JobID``,
        ``ClientStatus``, etc.
        """
        result = self._request("GET", f"/v1/evaluation/{eval_id}/allocations")
        # The API returns a list, but ``_request`` casts to dict. When
        # the response is a JSON array we need to unwrap it.
        if isinstance(result, dict) and not result:
            return []
        if isinstance(result, list):
            return result
        return []

    def get_job_allocations(self, job_id: str) -> list[dict[str, Any]]:
        """Return all allocations for a job.

        ``GET /v1/job/{job_id}/allocations`` returns a list of
        allocation stubs.
        """
        result = self._request("GET", f"/v1/job/{job_id}/allocations")
        if isinstance(result, list):
            return result
        return []

    def resolve_allocation(self, eval_id: str, job_id: str, *, timeout_s: float = 30.0) -> str:
        """Resolve a submitted job's evaluation to its allocation ID.

        Nomad's ``POST /v1/jobs`` returns an ``EvalID`` but not the
        allocation ID directly. We resolve it by:

          1. Looking up allocations from the evaluation.
          2. Falling back to looking up allocations from the job.

        Returns the first allocation ID found, or raises ``RuntimeError``
        if no allocation is created within the polling window.
        """
        effective_timeout_s = max(float(timeout_s), 0.1)
        deadline = time.monotonic() + effective_timeout_s
        poll_delay = 0.5
        while time.monotonic() < deadline:
            # Try eval-based lookup first (fast path).
            allocs = self.get_eval_allocations(eval_id)
            if allocs:
                return str(allocs[0].get("ID", ""))
            # Fall back to job-based lookup.
            allocs = self.get_job_allocations(job_id)
            if allocs:
                return str(allocs[0].get("ID", ""))
            time.sleep(poll_delay)
            poll_delay = min(poll_delay * 1.5, 5.0)
        raise RuntimeError(
            f"No allocation created for eval={eval_id!r} job={job_id!r} "
            f"within {effective_timeout_s:.1f}s"
        )


class _NomadHandle(PollingHandle):
    """Handle that polls Nomad on ``.result()``.

    Mirrors ``_AWSBatchHandle``: the work runs on a remote Nomad client
    (not a thread or submitit job), so we cannot back the Future with
    a local completion. Instead, the handle carries a reference to
    its executor and the Nomad ``jobId``; ``result()`` blocks on
    ``_wait_for_terminal`` and ``done()`` does a single non-blocking
    allocation lookup.

    The poll-deadline state machine lives in the shared
    ``PollingHandle`` base (issues #1464 / #1540); this class supplies
    only the Nomad-specific hooks below. The handle's ``_future`` is
    set when ``result()`` reaches a terminal state so concurrent
    callers don't re-poll. The base-class ``.result(timeout=...)`` /
    ``.done()`` paths remain reachable through the cached Future.
    """

    _GHOST_RETRIES = 3

    def __init__(
        self,
        job_id: str,
        eval_id: str,
        executor: NomadExecutor,
        *,
        local_future: Future[Any] | None = None,
        result_hint: Any = None,
        result_transport_mode: str = "auto",
        result_storage_backend: str | None = None,
        result_storage_bucket: str | None = None,
        result_storage_prefix: str | None = None,
        result_storage_endpoint: str | None = None,
    ) -> None:
        self.job_id = job_id
        self._eval_id = eval_id
        self._allocation_id: str | None = None
        self._executor = executor
        self._local_future = local_future
        self._result_hint = result_hint
        self._result_transport_mode = coerce_transport_mode(result_transport_mode)
        self._result_storage_backend = result_storage_backend
        self._result_storage_bucket = result_storage_bucket
        self._result_storage_prefix = result_storage_prefix
        self._result_storage_endpoint = result_storage_endpoint
        self._future: Future[Any] = Future()
        # Worker tracking (issue #105): populate at submit time.
        # allocation_id is not yet resolved; worker_id uses the job ID
        # and is updated to the allocation ID when available.
        self.worker_id: str | None = job_id
        self.worker_ip: str | None = None
        self.worker_region: str | None = executor.datacentre

    def _ensure_allocation_id(self) -> str:
        """Lazily resolve the eval/job to a concrete allocation ID.

        The first call queries the Nomad API to resolve the evaluation
        into an allocation. The result is cached so subsequent calls
        return immediately.
        """
        if self._allocation_id is None:
            timeout_s = float(getattr(self._executor, "allocation_resolution_timeout_s", 30.0))
            try:
                self._allocation_id = self._executor._client.resolve_allocation(  # noqa: SLF001
                    eval_id=self._eval_id,
                    job_id=self.job_id,
                    timeout_s=timeout_s,
                )
            except TypeError:
                self._allocation_id = self._executor._client.resolve_allocation(  # noqa: SLF001
                    eval_id=self._eval_id,
                    job_id=self.job_id,
                )
            # Guard against resolve_allocation returning None or "None" (the
            # str(None) string, e.g. when resolve_allocation itself had a bug
            # and called str() on a None result).  Both are invalid allocation
            # IDs that would cause downstream NoneType errors.
            if self._allocation_id is None or self._allocation_id == "None":
                raise RuntimeError(
                    f"resolve_allocation returned {self._allocation_id!r} for eval={self._eval_id!r} "
                    f"job={self.job_id!r}; allocation could not be resolved"
                )
        return self._allocation_id

    # ------------------------------------------------------------------
    # PollingHandle hooks (issues #1464 / #1540) — the shared state
    # machine in ``osimflow.executors.base.PollingHandle`` owns
    # ``result()``; ``NomadExecutor._wait_for_terminal`` owns the poll
    # skeleton via ``base.poll_until_terminal``.
    # ------------------------------------------------------------------

    def _poll_job_id(self) -> str:
        # Deadline messages name the allocation once resolved.
        return self._allocation_id or self.job_id

    def _wait_for_terminal(self, timeout: float | None) -> Any:
        alloc_id = self._ensure_allocation_id()
        return self._executor._wait_for_terminal(alloc_id, timeout=timeout)  # noqa: SLF001

    def _classify(self, job: Any) -> tuple[PollOutcome, str | None]:
        status = job.get("ClientStatus", "unknown")
        if status == "complete":
            return PollOutcome.SUCCEEDED, None
        # FAILED (or any non-complete terminal state — ``failed``,
        # ``lost``): the shared machine routes to ``_failure_error``.
        return PollOutcome.FAILED, None

    def _resolve_success_result(self, timeout: float | None = None) -> Any:
        # Issue #1463: resolved through the package namespace at call time
        # so tests that monkeypatch ``osimflow.executors.
        # materialize_object_storage_result`` keep intercepting this call
        # site after the executor moved out of the package __init__.
        from osimflow.executors import materialize_object_storage_result

        local_result: Any = resolve_result_for_callback(
            self._result_hint,
            default=None,
            transport_mode=self._result_transport_mode,
        )
        local_result = materialize_object_storage_result(
            local_result,
            transport_mode=self._result_transport_mode,
            result_storage_backend=self._result_storage_backend,
            result_storage_bucket=self._result_storage_bucket,
            result_storage_prefix=self._result_storage_prefix,
            result_storage_endpoint=self._result_storage_endpoint,
        )
        if self._local_future is not None:
            # Non-remote-results mode: the local mirror future ran the
            # work function locally; its result is authoritative and
            # honours the caller-supplied deadline.
            local_result = self._local_future.result(timeout=timeout)
        return local_result

    def _failure_error(self, job: Any) -> RuntimeError:
        # Re-raise with the most useful status description we can
        # extract from the task events. The Campaign's `except
        # Exception` path needs a string it can log.
        alloc_id = self._allocation_id or self.job_id
        status = job.get("ClientStatus", "unknown")
        task_states = job.get("TaskStates", {}) or {}
        description = self._extract_failure_description(task_states)
        return RuntimeError(f"Nomad allocation {alloc_id!r} {status}: {description}")

    def done(self) -> bool:  # noqa: PLR0911
        # If the future is already finished (terminal status observed
        # by a prior ``result()`` call), report done without making
        # another HTTP call. This mirrors the base ``Handle.done()``
        # contract — a cached terminal state is authoritative.
        if self._future.done():
            return True
        # Allocation not resolved yet — not done.
        if self._allocation_id is None:
            try:
                self._ensure_allocation_id()
            except Exception as exc:  # noqa: BLE001
                log.warning("Polling error for %s: %s", self.job_id, exc)
                self.error = exc
                return False
        # Do a non-blocking allocation lookup. If the task is in a
        # terminal state, we've already finished; otherwise we're still
        # running. Ghost allocations (deleted or never-created) return
        # an empty dict — after N consecutive empty responses we raise
        # to break the indefinite-wait loop.
        assert self._allocation_id is not None  # guaranteed after _ensure
        for attempt in range(self._GHOST_RETRIES):
            try:
                alloc = self._executor._client.get_allocation(self._allocation_id)  # noqa: SLF001
            except Exception as exc:  # noqa: BLE001 — never raise from done()
                log.warning("Polling error for %s: %s", self.job_id, exc)
                self.error = exc
                return False
            if alloc:
                break
            log.debug(
                "Empty allocation for %s, attempt %d/%d",
                self.job_id,
                attempt + 1,
                self._GHOST_RETRIES,
            )
        else:
            # Ghost allocation: not found after _GHOST_RETRIES consecutive
            # empty responses. Per the base Handle.done() contract (base.py:100),
            # polling errors must be captured and returned as False, not raised.
            self.error = RuntimeError(
                f"Ghost job: job ID {self.job_id!r} not found after {self._GHOST_RETRIES} retries"
            )
            return False
        status = alloc.get("ClientStatus", "")
        if status not in ("complete", "failed", "lost"):
            return False
        # An allocation in a terminal Nomad state is "done" only when the
        # local future also finished without raising.  A FAILED/CANCELLED
        # local future must still report done() == False so that result()
        # gets called and propagates the error instead of silently succeeding.
        # Use result() instead of done() to distinguish FAILED (raises) from
        # COMPLETED (returns normally) per the done()/result() contract.
        # When there is no local future, we can only report done() == True
        # if the allocation itself succeeded (status == "complete").
        # Failed/lost allocations must return False so callers invoke result()
        # and receive the error.
        if self._local_future is None:
            return cast(bool, status == "complete")
        done: bool
        try:
            self._local_future.result()
            done = True
        except Exception:
            done = False
        return done

    @staticmethod
    def _extract_failure_description(task_states: dict[str, Any]) -> str:
        """Walk the task state events to find the first failure
        description (e.g. ``"Exit Code: 137 (OOM killed)"``).

        Nomad's failed-task events carry a human-readable
        ``Description``; we surface the first one so the Campaign
        log line is actionable. Falls back to ``"unknown reason"``
        if no description is available.
        """
        best: tuple[int, str] | None = None
        priority_by_type = {
            "Driver Failure": 50,
            "Failed Validation": 45,
            "Terminated": 40,
            "Not Restarting": 30,
            "Restarting": 10,
        }
        for state in task_states.values():
            if not isinstance(state, dict):
                continue
            for event in state.get("Events", []) or []:
                desc = (
                    event.get("Description") or event.get("DisplayMessage") or event.get("Message")
                )
                if desc:
                    message = str(desc)
                    event_type = str(event.get("Type", ""))
                    score = priority_by_type.get(event_type, 20)
                    if message.lower().startswith("task restarting"):
                        score = min(score, 5)
                    if best is None or score > best[0]:
                        best = (score, message)
        if best is not None:
            return best[1]
        return "unknown reason"


class NomadExecutor(BaseExecutor):
    """HashiCorp Nomad batch executor (issue #27, issue #135).

    Supports two dispatch modes:

    * **Dispatch mode**: registers a parameterized job spec once,
      then uses ``POST /v1/job/osimflow-worker/dispatch`` for per-sample
      work. Each ``submit()`` call dispatches a child job with the sample
      parameters as Nomad meta vars.
    * **Direct mode** (default/backward compatible): builds and submits a unique ``batch`` job
      per ``submit()`` call via ``POST /v1/jobs``. Used when the
      parameterized job is not yet registered or when ``use_dispatch`` is
      ``False``.

    Resource directives (``cpus``, ``memory_mb``, ``time_min``) are
    mapped to the Nomad ``resources`` block (``CPU`` in MHz, ``MemoryMB``
    in MB). Per-sample ``OSIMFLOW_OS_VERSION`` and ``OSIMFLOW_CONTAINER``
    are carried as task env vars — the same env vars ``SlurmExecutor``
    and ``AWSBatchExecutor`` export, so downstream work scripts can be
    substrate-agnostic.

    Security: the Nomad ACL token is sourced from the ``NOMAD_TOKEN``
    env var (the documented Nomad pattern for CI/automation). The
    constructor does **not** accept a ``token`` kwarg; passing a
    long-lived token would violate the same security policy the AWS
    Batch executor enforces. Similarly, no address is pinned — the
    ``NOMAD_ADDR`` env var (or constructor kwarg) decides.

    Secret delivery (issue #1449): the constructor accepts an opt-in
    ``vault_secret_path: str | None = None`` (plus
    ``vault_secret_key: str = "payload_secret"``). When set, the
    task-payload HMAC shared secret is rendered into the task
    environment by the Nomad client from Vault via a ``template``
    stanza with ``env = true`` — the env entry for
    ``OSIMFLOW_TASK_PAYLOAD_SECRET`` and the dispatch-meta copy are
    omitted, so the raw secret never appears in the job spec
    (``nomad job inspect``), dispatch payload, or Nomad server state.
    Only the signature (``task_payload_sig`` /
    ``OSIMFLOW_TASK_PAYLOAD_SIG``) keeps travelling inline — it is
    public by design. When unset (default), the secret ships as a
    literal env value / dispatch meta exactly as before (backward
    compat). See ``docs/nomad-production.md`` for the Vault policy and
    example job snippet.

    The HTTP transport is stdlib ``urllib.request``, lazy-imported
    inside ``_NomadClient`` so the local-executor / slurm-executor /
    aws-batch paths do not pay the import cost. Tests patch
    ``urllib.request.urlopen`` to mock the wire format.
    """

    name = "nomad"

    @property
    def requires_remote_runner_payload(self) -> bool:
        return True

    signs_task_payload = True

    # The parameterized job ID used for dispatch mode.
    DISPATCH_JOB_ID = "osimflow-worker"
    _LEGACY_DISPATCH_POLICY_ALIASES: ClassVar[dict[str, str]] = {
        "direct": "keep_manual",
        "dispatch": "force_dispatch",
        "auto": "auto_prefer_dispatch",
    }
    _VALID_DISPATCH_POLICIES: ClassVar[set[str]] = {
        "keep_manual",
        "force_dispatch",
        "auto_prefer_dispatch",
    }

    def __init__(
        self,
        address: str | None = None,
        datacentre: str = "dc1",
        poll_interval_s: float = 5.0,
        max_poll_interval_s: float = 60.0,
        use_dispatch: bool = False,
        dispatch_policy: str | None = None,
        estimated_run_size: int | None = None,
        fanout_submit_rate_per_sec: float | None = None,
        fanout_submit_chunk_size: int = 0,
        allocation_resolution_timeout_s: float = 30.0,
        remote_results_only: bool = True,
        verify_tls: bool = True,
        tls: bool = False,
        cert: str | None = None,
        key: str | None = None,
        ca_cert: str | None = None,
        dispatch_job_id: str | None = None,
        allow_insecure_token: bool = False,
        vault_secret_path: str | None = None,
        vault_secret_key: str = "payload_secret",
    ):
        # Address precedence: explicit kwarg > NOMAD_ADDR env > 127.0.0.1.
        # Pinning the address in code would hard-code the deployment,
        # which is a portability trap.
        self.address = address or os.environ.get("NOMAD_ADDR") or "http://127.0.0.1:4646"
        # Token precedence: NOMAD_TOKEN env var only. The constructor
        # does NOT accept a token kwarg (see test_nomad_executor_does_not_accept_token_kwarg).
        self.datacentre = datacentre
        self.poll_interval_s = self._sanitize_positive_delay(poll_interval_s, fallback=5.0)
        max_interval = self._sanitize_positive_delay(max_poll_interval_s, fallback=60.0)
        self.max_poll_interval_s = max(max_interval, self.poll_interval_s)
        self._fanout_submit_rate_per_sec = (
            self._sanitize_positive_delay(fanout_submit_rate_per_sec, fallback=1.0)
            if fanout_submit_rate_per_sec is not None
            else None
        )
        self._fanout_submit_chunk_size = max(int(fanout_submit_chunk_size), 0)
        self.estimated_run_size = (
            max(int(estimated_run_size), 0) if estimated_run_size is not None else None
        )
        self._auto_dispatch_threshold = (
            self._fanout_submit_chunk_size if self._fanout_submit_chunk_size > 0 else 25
        )
        self._submit_count = 0
        self._active_waiters = 0
        self._waiters_lock = threading.Lock()
        if dispatch_policy is None:
            resolved_dispatch_policy = "force_dispatch" if use_dispatch else "keep_manual"
        else:
            resolved_dispatch_policy = self._LEGACY_DISPATCH_POLICY_ALIASES.get(
                dispatch_policy, dispatch_policy
            )
        if resolved_dispatch_policy not in self._VALID_DISPATCH_POLICIES:
            raise ValueError(
                "dispatch_policy must be one of: "
                "keep_manual, force_dispatch, auto_prefer_dispatch "
                "(legacy aliases: direct, dispatch, auto) "
                f"(got {resolved_dispatch_policy!r})"
            )
        self.dispatch_policy = resolved_dispatch_policy
        self._manual_dispatch_requested = bool(use_dispatch)
        self.use_dispatch = self._select_dispatch_mode()
        self.allocation_resolution_timeout_s = max(float(allocation_resolution_timeout_s), 0.1)
        self.remote_results_only = remote_results_only
        if not remote_results_only:
            warnings.warn(
                "Nomad local-callable compatibility mode (--no-nomad-remote-results-only) is deprecated "
                "and will be removed after one minor release. Migrate now by using the default "
                "remote-results mode and removing the compatibility flag.",
                DeprecationWarning,
                stacklevel=2,
            )
        self.verify_tls = verify_tls
        self.tls = tls
        self.cert = cert
        self.key = key
        self.ca_cert = ca_cert
        # SEC-009 (issue #1112): a bearer token sent over plain HTTP to a
        # non-local address can be intercepted by anyone on the network
        # path. Issue #1450 upgrades this guard from warn-only to
        # fail-closed: when a token IS configured (``NOMAD_TOKEN``) and
        # the resolved address is non-local without TLS, construction
        # raises unless the operator explicitly opts in with
        # ``allow_insecure_token=True`` (``--nomad-allow-insecure-token``),
        # mirroring the ``--allow-insecure-storage-endpoint`` opt-out from
        # issue #1386. Loopback addresses stay exempt — loopback traffic
        # never leaves the host. Without a token the plaintext path
        # carries no secret, so the loud warning is retained.
        self.allow_insecure_token = allow_insecure_token
        # Issue #1449: opt-in Vault-template delivery of the
        # task-payload HMAC secret. When ``vault_secret_path`` is set,
        # the secret is rendered from Vault by the Nomad client via a
        # ``template { env = true }`` stanza instead of travelling as a
        # literal task env value / dispatch meta, so the raw secret
        # never appears in the job spec, dispatch payload, or Nomad
        # server state. ``None`` (default) preserves the pre-#1449
        # literal behaviour.
        self.vault_secret_path = vault_secret_path
        self.vault_secret_key = vault_secret_key or "payload_secret"
        if not tls and not self._is_local_address(self.address):
            if os.environ.get("NOMAD_TOKEN") and not allow_insecure_token:
                raise ValueError(
                    f"insecure Nomad endpoint (issue #1450): {self.address} "
                    "does not use TLS while NOMAD_TOKEN is configured — the "
                    "ACL token would be transmitted in cleartext and can be "
                    "intercepted (SEC-009), granting job submission/dispatch "
                    "across the cluster. Enable TLS with --nomad-tls (plus "
                    "--nomad-cert/--nomad-key/--nomad-ca-cert), or pass "
                    "--nomad-allow-insecure-token to override (dev/test only)."
                )
            if allow_insecure_token:
                log.warning(
                    "SEC-009: Nomad TLS disabled for non-local address %s — "
                    "NOMAD_TOKEN transmitted in cleartext because "
                    "--nomad-allow-insecure-token was set; do not use in "
                    "production",
                    self.address,
                )
            warnings.warn(
                f"Nomad TLS is DISABLED for non-local address {self.address}: "
                "the NOMAD_TOKEN ACL token is transmitted in cleartext and can "
                "be intercepted (SEC-009). Enable TLS with --nomad-tls and "
                "configure --nomad-cert/--nomad-key/--nomad-ca-cert.",
                UserWarning,
                stacklevel=2,
            )
            log.warning(
                "SEC-009: Nomad TLS disabled for non-local address %s — "
                "NOMAD_TOKEN transmitted in cleartext",
                self.address,
            )
        self._dispatch_job_registered = False
        # Issue #1316: dispatch job ID is configurable so concurrent campaigns
        # on the same Nomad cluster use distinct parameterized jobs instead
        # of overwriting each other's job spec.
        self._dispatch_job_id = (
            dispatch_job_id if dispatch_job_id is not None else self.DISPATCH_JOB_ID
        )
        # Compatibility mode:
        # - remote_results_only=True (default): do not run local callables; Handle.result()
        #   returns result_hint on terminal success, enabling fully remote flows.
        # - remote_results_only=False: legacy compatibility path that runs callables locally.
        self._local_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="osimflow-nomad")
        self._client = _NomadClient(
            address=self.address,
            token=os.environ.get("NOMAD_TOKEN"),
            verify_tls=verify_tls,
            tls=tls,
            cert=cert,
            key=key,
            ca_cert=ca_cert,
        )

    @staticmethod
    def _is_local_address(address: str) -> bool:
        """True when *address* points at the local machine (issue #1112).

        Loopback addresses are exempt from the cleartext-token warning
        because loopback traffic never leaves the host.
        """
        import urllib.parse  # noqa: PLC0415

        candidate = address if "//" in address else f"//{address}"
        try:
            hostname = (urllib.parse.urlsplit(candidate).hostname or "").lower()
        except ValueError:
            return False
        if not hostname:
            return False
        if hostname in {"localhost", "::1"} or hostname.startswith("127."):
            return True
        # Bracketed IPv6 loopback variants ([::1], [0:0:0:0:0:0:0:1]).
        return hostname in {"0:0:0:0:0:0:0:1"}

    @staticmethod
    def _sanitize_positive_delay(value: float | None, *, fallback: float) -> float:
        if value is None:
            return fallback
        try:
            delay = float(value)
        except (TypeError, ValueError):
            return fallback
        if not math.isfinite(delay) or delay <= 0:
            return fallback
        return delay

    def _select_dispatch_mode(self) -> bool:
        if self.dispatch_policy == "force_dispatch":
            return True
        if self.dispatch_policy == "keep_manual":
            return self._manual_dispatch_requested
        if self._manual_dispatch_requested:
            return True
        if self.estimated_run_size is not None:
            return self.estimated_run_size >= self._auto_dispatch_threshold
        return self._submit_count >= self._auto_dispatch_threshold

    def fanout_submit_chunk_size(self, total: int) -> int:
        """Return the bounded chunk size for Nomad fan-out submission."""
        if total <= 0:
            return 1
        chunk = self._fanout_submit_chunk_size
        if chunk <= 0:
            return total
        return min(total, max(1, chunk))

    @property
    def fanout_submit_rate_per_sec(self) -> float | None:
        """Return the fan-out submit rate in submissions per second."""
        return self._fanout_submit_rate_per_sec

    def fanout_submit_interval_s(self) -> float:
        """Return the per-submit pacing interval for Nomad fan-out submission."""
        rate = self._fanout_submit_rate_per_sec
        if rate is None or rate <= 0:
            return 0.0
        return 1.0 / rate

    @staticmethod
    def _resolve_nomad_image(
        *,
        container: str | None,
        openstudio_version: str | None,
    ) -> str:
        """Resolve task image for Nomad with local-tag preference + fallback.

        Resolution order:
        1) explicit submit(container=...)
        2) OSIMFLOW_NOMAD_PREFERRED_IMAGE env override
        3) OSIMFLOW_OPENSTUDIO_CONTAINER_IMAGE env override
        4) nrel/openstudio:<openstudio_version|latest>
        """
        if container:
            return container
        preferred = os.environ.get("OSIMFLOW_NOMAD_PREFERRED_IMAGE")
        if preferred:
            return preferred
        openstudio_image = os.environ.get("OSIMFLOW_OPENSTUDIO_CONTAINER_IMAGE")
        if openstudio_image:
            return openstudio_image
        tag = openstudio_version or "latest"
        return f"nrel/openstudio:{tag}"

    def _vault_secret_template(self) -> str | None:
        """Return the Vault env-template line for the payload secret (issue #1449).

        The template is rendered by the Nomad client (consul-template
        engine) when the allocation is placed, via the ``template``
        stanza with ``Env = true`` this executor emits alongside it.
        Rendered output is a single ``KEY=VALUE`` line, so the value
        lands in the task environment as
        ``OSIMFLOW_TASK_PAYLOAD_SECRET`` without ever appearing in the
        job spec itself.

        KV mount handling: a path containing ``/data/`` (the
        conventional KV v2 API path, e.g. ``secret/data/osimflow``)
        reads the field from ``.Data.data.<key>``; anything else is
        treated as KV v1 and reads ``.Data.<key>``.

        Returns ``None`` when Vault mode is not configured.
        """
        path: str | None = getattr(self, "vault_secret_path", None)
        if not path:
            return None
        key = str(getattr(self, "vault_secret_key", "payload_secret") or "payload_secret")
        field = f".Data.data.{key}" if "/data/" in path else f".Data.{key}"
        return (
            TASK_PAYLOAD_SECRET_ENV
            + '={{ with secret "'
            + path
            + '" }}{{ '
            + field
            + " }}{{ end }}"
        )

    def _build_job_spec(
        self,
        *,
        name: str,
        cpus: int,
        memory_mb: int,
        container: str | None,
        openstudio_version: str | None,
        remote_command: str | None = None,
        task_payload: str | None = None,
        result_transport_mode: str | None = None,
        result_storage_backend: str | None = None,
        result_storage_bucket: str | None = None,
        result_storage_prefix: str | None = None,
        result_storage_endpoint: str | None = None,
    ) -> dict[str, Any]:
        """Build a Nomad ``batch`` job spec for one OpenStudio task.

        The spec uses a single task group with a single ``docker`` task
        that runs the standard NREL OpenStudio container (or a custom
        ``container`` tag if the caller passed one). Per-sample
        metadata travels in the job ``Meta`` block — useful when the
        cluster does not have shared storage for the seed model and
        the work script needs to fetch it.

        ``CPU`` is in MHz (Nomad resource model); a 1-cpu job is
        1000 MHz. ``MemoryMB`` is in MB. ``time_min`` is mapped to a
        task-group-level ``KillTimeout`` (Go duration string) so a
        runaway task is hard-killed by the Nomad client.
        """
        env: dict[str, str] = {}
        if openstudio_version is not None:
            env["OSIMFLOW_OS_VERSION"] = str(openstudio_version)
        if container is not None:
            env["OSIMFLOW_CONTAINER"] = container
        # Issue #1449: in Vault mode the secret is rendered by the
        # Nomad client from a ``template { env = true }`` stanza, so it
        # must be kept out of the literal Env block.
        vault_template = self._vault_secret_template()
        if task_payload is not None:
            env["OSIMFLOW_TASK_PAYLOAD"] = task_payload
            # Issue #1281: verify BYOS contract version compatibility.
            env["OSIMFLOW_CONTRACT_VERSION"] = BYOS_CONTRACT_VERSION
            # Issue #1177: when a shared secret is configured, sign the
            # exact payload bytes and propagate secret + signature so the
            # remote_runner verifies before decoding/executing. No-op in
            # legacy unsigned mode.
            signature_env = build_signature_env(task_payload)
            if vault_template is not None and not signature_env:
                log.warning(
                    "vault_secret_path=%r is configured but no %s is set "
                    "on the orchestrator, so the payload cannot be signed; "
                    "submitting unsigned (legacy mode). Set %s on the "
                    "orchestrator — and the same value in Vault — to "
                    "enable HMAC verification (issue #1449).",
                    self.vault_secret_path,
                    TASK_PAYLOAD_SECRET_ENV,
                    TASK_PAYLOAD_SECRET_ENV,
                )
            env.update(signature_env)
            if vault_template is not None:
                env.pop(TASK_PAYLOAD_SECRET_ENV, None)
        if result_transport_mode is not None:
            env["OSIMFLOW_RESULT_TRANSPORT_MODE"] = result_transport_mode
        if result_storage_backend is not None:
            env["OSIMFLOW_RESULT_STORAGE_BACKEND"] = result_storage_backend
        if result_storage_bucket is not None:
            env["OSIMFLOW_RESULT_STORAGE_BUCKET"] = result_storage_bucket
        if result_storage_prefix is not None:
            env["OSIMFLOW_RESULT_STORAGE_PREFIX"] = result_storage_prefix
        if result_storage_endpoint is not None:
            env["OSIMFLOW_RESULT_STORAGE_ENDPOINT"] = result_storage_endpoint

        image = self._resolve_nomad_image(
            container=container,
            openstudio_version=openstudio_version,
        )
        task_command = remote_command or "python -m osimflow.remote_runner"
        import uuid  # noqa: PLC0415

        job_id = _slugify_job_name(f"osimflow-{name}-{uuid.uuid4().hex[:8]}")

        task: dict[str, Any] = {
            "Name": "osimflow",
            "Driver": "docker",
            "Config": {
                "image": image,
                "entrypoint": [
                    "/bin/sh",
                    "-c",
                    task_command,
                ],
            },
            "Resources": {
                "CPU": int(cpus) * 1000,
                "MemoryMB": int(memory_mb),
            },
            "Restart": {
                "Attempts": 0,
            },
            "Env": env,
        }
        if vault_template is not None:
            # Issue #1449: Nomad renders the embedded template through
            # the Vault integration and injects each ``KEY=VALUE`` line
            # as a task env var (``env = true``). The literal secret is
            # absent from the spec; only the template reference to the
            # Vault path travels with the job.
            task["Templates"] = [
                {
                    "Env": True,
                    "ChangeMode": "restart",
                    "EmbeddedTmpl": vault_template,
                }
            ]

        return {
            "Job": {
                "ID": job_id,
                "Name": job_id,
                "Type": "batch",
                "Datacenters": [self.datacentre],
                "Meta": {
                    "OSIMFLOW_SAMPLE_NAME": name,
                    "OSIMFLOW_OS_VERSION": str(openstudio_version or ""),
                },
                "TaskGroups": [
                    {
                        "Name": "osimflow",
                        "Tasks": [task],
                    }
                ],
            }
        }

    #: Per-container tmpfs working-directory size in bytes (100 MB).
    #: Sized for a typical OSimFlow sample: a handful of ``.osw`` /
    #: ``.osm`` artifacts plus ``eplusout.sql`` staging before the
    #: result-transport uploader ships them out. Issue #1387.
    _DISPATCH_TMPFS_SIZE_BYTES: int = 100_000_000

    def _build_dispatch_job_spec(self) -> dict[str, Any]:
        """Build the parameterized job spec for dispatch mode (issue #135).

        Returns a Nomad ``batch`` job spec with ``ParameterizedJob`` set
        so the executor can dispatch child jobs via
        ``POST /v1/job/<dispatch_job_id>/dispatch``.

        Security:
          * ``privileged = false`` — no host-level access.
          * ``cap_drop = ["ALL"]`` — drop the full default Linux
            capability set so a compromised ``remote_runner`` cannot
            ``mount``, ``ptrace``, ``setuid`` or load kernel modules
            even on a kernel that would otherwise allow them. Issue #1387.
          * ``read_only = true`` — root filesystem is read-only;
            every writable path is on an explicit ``tmpfs`` mount.
          * ``tmpfs`` mounts at ``/tmp`` and ``/work`` give the
            per-sample ``remote_runner`` the writable scratch it needs
            (the default container working dir is on the read-only
            rootfs otherwise).
          * Memory limited to 4096 MB, CPU to 2000 MHz (2 logical CPUs).
          * No host network, no bind mounts.
        """
        default_image = self._resolve_nomad_image(
            container=os.environ.get("OSIMFLOW_NOMAD_PREFERRED_IMAGE"),
            openstudio_version="3.11.0",
        )
        tmpfs_size = self._DISPATCH_TMPFS_SIZE_BYTES
        # The Nomad Docker driver expects ``mount`` as a list of mount
        # blocks (the HCL ``mount { ... }`` desugars to this JSON shape).
        # Both mounts are tmpfs so they live in memory only and vanish
        # with the container — no host filesystem exposure.
        dispatch_mounts: list[dict[str, Any]] = [
            {
                "type": "tmpfs",
                "target": "/tmp",
                "read_only": False,
                "tmpfs_options": {"size": tmpfs_size},
            },
            {
                "type": "tmpfs",
                "target": "/work",
                "read_only": False,
                "tmpfs_options": {"size": tmpfs_size},
            },
        ]
        dispatch_task: dict[str, Any] = {
            "Name": "simulate",
            "Driver": "docker",
            "Config": {
                "image": default_image,
                "command": "/bin/sh",
                "args": ["-c", "python -m osimflow.remote_runner"],
                "privileged": False,
                "cap_drop": ["ALL"],
                "read_only": True,
                "mount": dispatch_mounts,
            },
            "Resources": {
                "CPU": 2000,
                "MemoryMB": 4096,
            },
            "Restart": {
                "Attempts": 0,
            },
            "Env": {},
        }
        # Issue #1449: in Vault mode the parameterized job renders
        # ``OSIMFLOW_TASK_PAYLOAD_SECRET`` from Vault via an env
        # template stanza, so per-dispatch meta never carries the raw
        # secret (dispatch meta is visible via ``nomad job inspect``
        # and ``nomad alloc status``).
        vault_template = self._vault_secret_template()
        if vault_template is not None:
            dispatch_task["Templates"] = [
                {
                    "Env": True,
                    "ChangeMode": "restart",
                    "EmbeddedTmpl": vault_template,
                }
            ]
        return {
            "Job": {
                "ID": self._dispatch_job_id,
                "Name": self._dispatch_job_id,
                "Type": "batch",
                "Datacenters": [self.datacentre],
                "ParameterizedJob": {
                    "MetaRequired": ["sample_id"],
                    "MetaOptional": [
                        "variables_json",
                        "openstudio_version",
                        "container_image",
                        "task_payload",
                        "task_payload_sig",
                        "task_payload_secret",
                        "result_transport_mode",
                        "result_storage_backend",
                        "result_storage_bucket",
                        "result_storage_prefix",
                        "result_storage_endpoint",
                    ],
                },
                "Meta": {
                    "variables_json": "{}",
                    "openstudio_version": "3.11.0",
                    "container_image": default_image,
                    "task_payload": "{}",
                    "result_transport_mode": "auto",
                    "result_storage_backend": "",
                    "result_storage_bucket": "",
                    "result_storage_prefix": "",
                    "result_storage_endpoint": "",
                },
                "TaskGroups": [
                    {
                        "Name": "osimflow",
                        "Tasks": [dispatch_task],
                    }
                ],
            }
        }

    def _ensure_dispatch_job_registered(self) -> None:
        """Register the parameterized job spec (idempotent).

        Called once on the first ``submit()`` when ``use_dispatch`` is
        True. Subsequent calls are no-ops.
        """
        if self._dispatch_job_registered:
            return
        spec = self._build_dispatch_job_spec()
        log.info("nomad: registering parameterized dispatch job %s", self._dispatch_job_id)
        self._client.register_job(spec)
        self._dispatch_job_registered = True

    def _wait_for_terminal(
        self, allocation_id: str, timeout: float | None = None
    ) -> dict[str, Any]:
        """Poll ``GET /v1/allocation/<id>`` with exponential backoff
        until the allocation reaches a terminal state (``complete`` /
        ``failed`` / ``lost``). Returns the final allocation dict.

        The poll skeleton (deadline, remaining clamp, capped growth)
        lives in ``osimflow.executors.base.poll_until_terminal``
        (issue #1540). Nomad keeps two substrate-specific extensions
        via the documented hooks: ``sleep_for`` phases concurrent
        waiters apart (issue #1378 anti-thundering-herd offset) and
        ``next_delay`` grows the delay with an adaptive factor
        (``1.6 + min(pressure, 0.4)``) instead of the plain doubling.

        Raises:
            TimeoutError: if *timeout* seconds elapse before a terminal state.
        """
        with self._waiters_lock:
            self._active_waiters += 1
            active_waiters = self._active_waiters
        phase_offset = 0.0
        if active_waiters > 1:
            phase_offset = (sum(ord(ch) for ch in allocation_id) % 10) / 100.0

        def _phased_sleep(delay: float, remaining: float | None) -> float:
            # Nomad's phased sleep: add the per-allocation anti-herd
            # offset, then clamp to the poll cap and the remaining
            # deadline (the clamp duty the ``sleep_for`` hook takes
            # over from the shared loop).
            bounds = [delay + phase_offset, self.max_poll_interval_s]
            if remaining is not None:
                bounds.append(remaining)
            return min(bounds)

        try:
            return poll_until_terminal(
                lambda: self._client.get_allocation(allocation_id),
                is_terminal=lambda alloc: (
                    alloc.get("ClientStatus", "UNKNOWN") in ("complete", "failed", "lost")
                ),
                timeout=timeout,
                timeout_message=lambda elapsed: (
                    f"Timed out after {elapsed:.1f}s waiting for allocation {allocation_id!r}"
                ),
                poll_interval_s=self.poll_interval_s,
                max_poll_interval_s=self.max_poll_interval_s,
                on_pending=lambda alloc, _delay, sleep_amount: log.info(
                    "nomad poll alloc=%s status=%s (sleeping %.2fs, active_waiters=%d)",
                    allocation_id,
                    alloc.get("ClientStatus", "UNKNOWN"),
                    sleep_amount,
                    active_waiters,
                ),
                sleep_for=_phased_sleep,
                next_delay=lambda delay: min(
                    delay * self._poll_backoff_factor(active_waiters),
                    self.max_poll_interval_s,
                ),
            )
        finally:
            with self._waiters_lock:
                self._active_waiters = max(self._active_waiters - 1, 0)

    def _poll_backoff_factor(self, active_waiters: int) -> float:
        """Adaptive poll-backoff factor (issue #1378 anti-herd pressure).

        Grows from the 1.6 base towards 2.0 as concurrent waiters or a
        high fan-out submit rate signal Nomad API pressure, so many
        concurrent ``result()`` callers stagger their polls.
        """
        concurrency_pressure = max(active_waiters - 8, 0) * 0.05
        rate_pressure = 0.0
        if self._fanout_submit_rate_per_sec is not None:
            rate_pressure = max(self._fanout_submit_rate_per_sec - 10.0, 0.0) * 0.01
        return 1.6 + min(concurrency_pressure + rate_pressure, 0.4)

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
        # Assemble local_callable_kwargs from the explicit fields we received.
        # stdout_path, stderr_path, env are for the campaign's use (not Nomad's env).
        local_callable_kwargs: dict[str, Any] = {}
        del kwargs  # noqa: F841, ARG002

        step_name = self._infer_step_name(name)
        task_payload = self._build_task_payload(
            step_name=step_name,
            args=args,
            kwargs=local_callable_kwargs,
            result_hint=result_hint,
            name=name,
        )
        local_future: Future[Any] | None = None
        if not self.remote_results_only:
            local_future = self._local_pool.submit(fn, *args, **local_callable_kwargs)
        else:
            del fn, args
        self._submit_count += 1
        dispatch_mode = self._select_dispatch_mode()
        self.use_dispatch = dispatch_mode

        log.info(
            "nomad submit name=%s cpus=%d mem=%dMB time_min=%d container=%s dispatch=%s "
            "policy=%s threshold=%d count=%d remote_results_only=%s",
            name,
            cpus,
            memory_mb,
            time_min,
            container,
            dispatch_mode,
            self.dispatch_policy,
            self._auto_dispatch_threshold,
            self._submit_count,
            self.remote_results_only,
        )

        if dispatch_mode:
            # Dispatch mode: register the parameterized job once, then
            # dispatch per-sample work via POST /v1/job/{id}/dispatch.
            self._ensure_dispatch_job_registered()

            # Build the per-dispatch meta payload.
            image = self._resolve_nomad_image(
                container=container,
                openstudio_version=(str(openstudio_version) if openstudio_version else None),
            )
            meta: dict[str, str] = {
                "sample_id": _slugify_job_name(name),
                "openstudio_version": str(openstudio_version or ""),
                "container_image": image,
            }
            if variables_json is not None:
                meta["variables_json"] = (
                    variables_json
                    if isinstance(variables_json, str)
                    else json.dumps(variables_json)
                )
            meta["task_payload"] = task_payload
            # Issue #1177: sign the dispatch payload when a shared secret
            # is configured; the runner reads these back as
            # NOMAD_META_task_payload_sig / NOMAD_META_task_payload_secret.
            # Issue #1449: in Vault mode the secret itself is rendered
            # by the Nomad client from the parameterized job's env
            # template stanza, so it is kept out of the dispatch meta
            # (visible via ``nomad job inspect`` / ``nomad alloc
            # status``); only the signature travels inline.
            signature_env = build_signature_env(task_payload)
            if signature_env:
                meta[TASK_PAYLOAD_SIG_META_KEY] = signature_env[TASK_PAYLOAD_SIG_ENV]
                if self._vault_secret_template() is None:
                    meta[TASK_PAYLOAD_SECRET_META_KEY] = signature_env[TASK_PAYLOAD_SECRET_ENV]
            meta["result_transport_mode"] = (
                str(result_transport_mode) if result_transport_mode is not None else "auto"
            )
            if result_storage_backend is not None:
                meta["result_storage_backend"] = str(result_storage_backend)
            if result_storage_bucket is not None:
                meta["result_storage_bucket"] = str(result_storage_bucket)
            if result_storage_prefix is not None:
                meta["result_storage_prefix"] = str(result_storage_prefix)
            if result_storage_endpoint is not None:
                meta["result_storage_endpoint"] = str(result_storage_endpoint)

            response = self._client.dispatch_job(self._dispatch_job_id, meta=meta)
        else:
            # Legacy (direct) mode: build and submit a unique job per call.
            spec = self._build_job_spec(
                name=name,
                cpus=cpus,
                memory_mb=memory_mb,
                container=container,
                openstudio_version=openstudio_version,
                remote_command=(str(remote_command) if remote_command else None),
                task_payload=task_payload,
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
            response = self._client.submit_job(spec)

        job_id = response.get("JobID") or (spec["Job"]["ID"] if not dispatch_mode else "")
        eval_id = response.get("EvalID", "")
        log.info("nomad submit_job -> jobId=%s evalId=%s", job_id, eval_id)

        # Return a lazy handle: the allocation is resolved from the
        # evaluation on first ``result()`` / ``done()`` call, so the
        # submit path stays fast. This matches the
        # LocalExecutor / SlurmExecutor / AWSBatchExecutor
        # ergonomics — ``submit()`` is non-blocking; ``result()``
        # blocks — and lets the Campaign's
        # ``.result(timeout=...)`` semantics work uniformly across
        # substrates.
        return _NomadHandle(
            job_id=job_id,
            eval_id=eval_id,
            executor=self,
            local_future=local_future,
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
        self._local_pool.shutdown(wait=True)
