"""Alerting and notification system for OSimFlow campaign events (issue #438).

Provides a rule-based alerting engine that evaluates campaign events and
dispatches notifications to configurable destinations.

Event Types
-----------
``campaign.started``
    Campaign begins.
``campaign.completed``
    Campaign completes successfully.
``campaign.failed``
    Campaign fails (exception thrown).
``sample.failed``
    Individual sample fails after max retries.
``worker.dead``
    A worker node stops responding.
``cache.miss_rate_low``
    Cache hit rate drops below 50%%.

Configuration
--------------
Alert rules are defined in a YAML file referenced by ``--alert-rules``.
Each rule has the shape::

    rules:
      - event_type: campaign.failed
        severity: CRITICAL
        message_template: "Campaign {campaign_id} failed: {error}"
        condition:
          type: always  # always, expr, threshold
          value: true

Destinations are defined in a YAML file referenced by
``--alert-destinations``::

    destinations:
      - type: webhook
        url: https://hooks.example.com/osimflow
      - type: email
        smtp_host: smtp.example.com
        recipients:
          - ops@example.com
      - type: log
        level: WARNING  # INFO, WARNING, CRITICAL
"""

from __future__ import annotations

__all__ = ["AlertManager", "build_alert_manager"]

import abc
import contextlib
import dataclasses
import json
import logging
import queue
import smtplib
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import yaml

from osimflow._eval_safe import ExpressionError, safe_eval

log = logging.getLogger("osimflow.alerting")


class AlertSeverity:
    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


@dataclasses.dataclass
class Alert:
    """An alert payload produced by the AlertManager."""

    rule_name: str
    event_type: str
    severity: str
    message: str
    context: dict[str, Any]
    timestamp: float
    delivery_status: str = "unknown"


@dataclasses.dataclass
class AlertRule:
    """A rule that evaluates campaign events and produces alerts."""

    name: str
    event_type: str
    condition: Callable[[dict[str, Any]], bool]
    severity: str
    message_template: str


#: Maximum number of failed alerts retained for retry (issue #1185).
#: The pending-alert history is a ring buffer: once full, the oldest
#: entry is evicted to make room for the newest failure.
_ALERT_HISTORY_MAXLEN = 100

#: Maximum number of in-flight alert-dispatch workers (issue #1770).
#: Pre-fix this was unbounded (one daemon thread per dispatched alert).
#: When a destination is wedged and many alerts queue up the worker
#: pool cannot exceed this many live threads, and per-alert deadlines
#: still cancel an abandoned future so a wedged destination cannot
#: clog the pool past the ceiling.
_ALERT_DISPATCH_POOL_SIZE = 4


@dataclasses.dataclass
class _PendingAlert:
    """An alert whose delivery failed and is queued for retry (issue #1185)."""

    alert: Alert
    destination: AlertDestination
    failed_at: float
    error: str


class AlertDestination(abc.ABC):
    """Abstract base for alert delivery mechanisms."""

    @abc.abstractmethod
    def send(self, alert: Alert) -> bool:
        """Send *alert* to its destination.

        Returns ``True`` on success, ``False`` on failure.
        Failures are logged but never raise.
        """
        ...


class WebhookDestination(AlertDestination):
    """Delivers alerts as JSON POST to a webhook URL."""

    def __init__(self, url: str, timeout: float = 30.0, max_retries: int = 3) -> None:
        self.url = url
        self.timeout = timeout
        self.max_retries = max_retries

    def send(self, alert: Alert) -> bool:
        payload = {
            "rule": alert.rule_name,
            "event": alert.event_type,
            "severity": alert.severity,
            "message": alert.message,
            "context": alert.context,
            "timestamp": alert.timestamp,
        }
        body = json.dumps(payload, default=str).encode("utf-8")

        for attempt in range(self.max_retries + 1):
            try:
                req = urllib.request.Request(
                    self.url,
                    data=body,
                    headers={
                        "Content-Type": "application/json",
                        "User-Agent": "OSimFlow-Alerting/1.0",
                        "Accept": "application/json",
                    },
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310
                    if 200 <= resp.status < 300:
                        log.info(
                            "alert webhook delivered to %s (attempt %d, status %d)",
                            self.url,
                            attempt + 1,
                            resp.status,
                        )
                        return True
                    log.warning(
                        "alert webhook HTTP %d from %s (attempt %d/%d)",
                        resp.status,
                        self.url,
                        attempt + 1,
                        self.max_retries + 1,
                    )
            except urllib.error.HTTPError as exc:
                log.warning(
                    "alert webhook HTTP error %d from %s (attempt %d/%d): %s",
                    exc.code,
                    self.url,
                    attempt + 1,
                    self.max_retries + 1,
                    exc,
                )
                if exc.code < 500:
                    return False
            except urllib.error.URLError as exc:
                log.warning(
                    "alert webhook URL error for %s (attempt %d/%d): %s",
                    self.url,
                    attempt + 1,
                    self.max_retries + 1,
                    exc,
                )
            except TimeoutError as exc:
                log.warning(
                    "alert webhook timeout for %s (attempt %d/%d): %s",
                    self.url,
                    attempt + 1,
                    self.max_retries + 1,
                    exc,
                )

            if attempt < self.max_retries:
                delay = min(1.0 * (2**attempt), 60.0)
                time.sleep(delay)

        log.error(
            "alert webhook delivery to %s failed after %d attempts",
            self.url,
            self.max_retries + 1,
        )
        return False


class EmailDestination(AlertDestination):
    """Delivers alerts via SMTP email."""

    def __init__(
        self,
        smtp_host: str,
        smtp_port: int = 587,
        recipients: list[str] | str = "",
        sender: str = "osimflow@example.com",
        use_tls: bool = True,
    ) -> None:
        self.smtp_host = smtp_host
        self.smtp_port = smtp_port
        if isinstance(recipients, str):
            recipients = [r.strip() for r in recipients.split(",") if r.strip()]
        self.recipients = recipients
        self.sender = sender
        self.use_tls = use_tls

    def send(self, alert: Alert) -> bool:
        if not self.recipients:
            log.warning("email destination: no recipients configured — skipping")
            return False

        msg = EmailMessage()
        msg["Subject"] = f"[OSimFlow {alert.severity}] {alert.event_type}: {alert.rule_name}"
        msg["From"] = self.sender
        msg["To"] = ", ".join(self.recipients)
        msg["Date"] = time.strftime("%a, %d %b %Y %H:%M:%S %z")
        msg.set_content(
            f"OSimFlow Alert\n"
            f"==============\n"
            f"Rule:    {alert.rule_name}\n"
            f"Event:   {alert.event_type}\n"
            f"Severity: {alert.severity}\n"
            f"Time:    {time.strftime('%Y-%m-%d %H:%M:%S %z')}\n"
            f"\n"
            f"Message:\n"
            f"{alert.message}\n"
            f"\n"
            f"Context:\n"
            f"{yaml.dump(alert.context, default_flow_style=False)}"
        )

        try:
            with smtplib.SMTP(self.smtp_host, self.smtp_port) as server:
                if self.use_tls:
                    server.starttls()
                server.send_message(msg)
            log.info(
                "alert email sent via %s to %s",
                self.smtp_host,
                self.recipients,
            )
            return True
        except Exception as exc:
            log.warning("alert email delivery failed via %s: %s", self.smtp_host, exc)
            return False


class LogDestination(AlertDestination):
    """Logs alerts using the standard logging infrastructure."""

    def __init__(self, level: str = "WARNING") -> None:
        self.level = getattr(logging, level.upper(), logging.WARNING)

    def send(self, alert: Alert) -> bool:
        log.log(
            self.level,
            "[ALERT] %s | %s | %s | %s",
            alert.severity,
            alert.event_type,
            alert.rule_name,
            alert.message,
        )
        return True


class AlertManager:
    """Registers rules and destinations, evaluates events, and dispatches alerts."""

    def __init__(
        self,
        *,
        on_alert: Callable[[Alert], None] | None = None,
        per_alert_deadline_s: float = 30.0,
    ) -> None:
        self._rules: list[AlertRule] = []
        self._destinations: list[AlertDestination] = []
        self._cache_stats: dict[str, Any] = {}
        # Optional callback invoked for every alert dispatched (issue #1308).
        # Intended to forward fired alerts to RunTrace.alerts_fired.
        self._on_alert: Callable[[Alert], None] | None = on_alert
        # Bounded ring buffer of alerts whose delivery failed (issue #1185).
        # Retried opportunistically at the start of every notify() call.
        self._alert_history: deque[_PendingAlert] = deque(maxlen=_ALERT_HISTORY_MAXLEN)
        self._history_lock = threading.Lock()
        # Background-dispatch plumbing (issue #1673). The worker thread
        # owns the blocking destination.send() calls (webhook retry sleeps,
        # SMTP timeouts) so they never run on the fan-out worker threads.
        # notify() enqueues an Alert + rule to the worker when the dispatcher
        # is started; otherwise notify() dispatches synchronously (legacy).
        self._per_alert_deadline_s = per_alert_deadline_s
        self._dispatch_queue: queue.Queue[tuple[Alert, AlertRule] | None] | None = None
        self._dispatch_thread: threading.Thread | None = None
        self._dispatch_started = threading.Event()
        self._dispatch_stopped = threading.Event()
        self._dispatch_lock = threading.Lock()
        # Bounded thread pool used by ``_dispatch_one`` (issue #1770).
        # The pre-fix implementation spawned a fresh ``daemon=True``
        # thread for *every* alert that arrived while the dispatcher
        # worker was running — 50 wedged alerts ⇒ 50 daemon threads,
        # each one independently blocked in ``_do_dispatch``.  The pool
        # caps the live-thread budget at ``_ALERT_DISPATCH_POOL_SIZE``
        # regardless of the inbound rate; per-alert deadlines still
        # cancel an abandoned future, so wedged destinations cannot
        # clog the worker pool past that ceiling.
        self._dispatch_pool: ThreadPoolExecutor | None = None

    def add_rule(self, rule: AlertRule) -> None:
        self._rules.append(rule)

    def add_destination(self, dest: AlertDestination) -> None:
        self._destinations.append(dest)

    def notify(self, event_type: str, context: dict[str, Any]) -> None:
        """Evaluate all rules matching *event_type* and dispatch alerts.

        Best-effort: destination failures are logged, queued in the
        bounded pending-alert history for retry (issue #1185), and never
        raise.

        When :meth:`start_background_dispatch` has been called, the
        dispatch itself (destination.send() and its blocking retries) is
        moved off the caller's thread onto a worker thread (issue #1673);
        :meth:`notify` then returns once the alert is enqueued. Without a
        background dispatcher, dispatch runs synchronously as before.
        """
        # Retry pending inline: this is cheap (best-effort re-attempt of
        # previously failed deliveries) and keeps the synchronous path's
        # behavior identical. When the worker is running, the retry sweep
        # runs there periodically (issue #1185) so we don't duplicate it.
        if not self._is_dispatcher_running():
            self._retry_pending()
        for rule in self._rules:
            if rule.event_type != event_type:
                continue
            try:
                if not rule.condition(context):
                    continue
            except Exception as exc:
                log.warning(
                    "alert rule %s condition raised — skipping: %s",
                    rule.name,
                    exc,
                )
                continue

            message = _render_template(rule.message_template, context)
            alert = Alert(
                rule_name=rule.name,
                event_type=event_type,
                severity=rule.severity,
                message=message,
                context=context,
                timestamp=time.time(),
                delivery_status="queued" if self._is_dispatcher_running() else "unknown",
            )

            if self._is_dispatcher_running():
                # Background-dispatch path. Enqueue and return immediately
                # so a slow/blackholed webhook can't stall the fan-out
                # worker that called notify().
                self._enqueue_dispatch(alert, rule)
                continue

            self._do_dispatch(alert, rule)

    def _do_dispatch(self, alert: Alert, rule: AlertRule) -> None:
        """Dispatch *alert* synchronously and invoke the on_alert callback.

        Called directly by :meth:`notify` when no background dispatcher
        is running, and by the dispatcher worker thread when one is.
        """
        alert.delivery_status = self._dispatch_to_destinations(alert, rule)
        if self._on_alert is not None:
            try:
                self._on_alert(alert)
            except Exception as exc:
                log.warning("on_alert callback raised — continuing: %s", exc)

    def _dispatch_to_destinations(self, alert: Alert, rule: AlertRule) -> str:
        """Send alert to all destinations and return overall delivery status."""
        if not self._destinations:
            return "no_destinations"

        delivered_count = 0
        failed_count = 0
        for dest in self._destinations:
            error = ""
            try:
                delivered = bool(dest.send(alert))
            except Exception as exc:
                delivered = False
                error = str(exc)
                log.warning(
                    "alert destination %s failed for rule %s: %s",
                    type(dest).__name__,
                    rule.name,
                    exc,
                )
            if delivered:
                delivered_count += 1
                continue
            failed_count += 1
            if not error:
                error = "destination reported delivery failure"
                log.warning(
                    "alert destination %s failed for rule %s: %s",
                    type(dest).__name__,
                    rule.name,
                    error,
                )
            self._enqueue_pending(alert, dest, error)

        if failed_count == 0:
            return "delivered"
        if delivered_count == 0:
            return "failed"
        return "partial"

    def _enqueue_pending(
        self,
        alert: Alert,
        dest: AlertDestination,
        error: str,
    ) -> None:
        """Park a failed alert in the bounded history for later retry."""
        with self._history_lock:
            self._alert_history.append(
                _PendingAlert(
                    alert=alert,
                    destination=dest,
                    failed_at=time.time(),
                    error=error,
                )
            )
        log.debug(
            "alert for rule %s queued for retry (pending=%d/%d)",
            alert.rule_name,
            len(self._alert_history),
            _ALERT_HISTORY_MAXLEN,
        )

    def _retry_pending(self) -> None:
        """Re-attempt delivery of queued alerts (issue #1185).

        Called at the start of every :meth:`notify` call. Alerts whose
        destination has recovered are removed from the history (logged at
        info); still-failing alerts are kept with an updated error and
        timestamp (logged at debug to avoid spam).
        """
        with self._history_lock:
            if not self._alert_history:
                return
            pending = list(self._alert_history)
            self._alert_history.clear()

        still_failing: list[_PendingAlert] = []
        for entry in pending:
            error = ""
            delivered = False
            try:
                delivered = bool(entry.destination.send(entry.alert))
            except Exception as exc:
                error = str(exc)
            if delivered:
                log.info(
                    "pending alert for rule %s (%s) delivered to %s after recovery",
                    entry.alert.rule_name,
                    entry.alert.event_type,
                    type(entry.destination).__name__,
                )
                continue
            if not error:
                error = "destination reported delivery failure"
            still_failing.append(
                _PendingAlert(
                    alert=entry.alert,
                    destination=entry.destination,
                    failed_at=time.time(),
                    error=error,
                )
            )
            log.debug(
                "pending alert for rule %s still failing for %s: %s",
                entry.alert.rule_name,
                type(entry.destination).__name__,
                error,
            )

        if still_failing:
            with self._history_lock:
                self._alert_history.extend(still_failing)

    # ------------------------------------------------------------------
    # Background dispatcher (issue #1673)
    # ------------------------------------------------------------------
    def _is_dispatcher_running(self) -> bool:
        """True when the background dispatcher worker thread is alive."""
        return self._dispatch_thread is not None and self._dispatch_thread.is_alive()

    def start_background_dispatch(self) -> None:
        """Start the background dispatcher thread (idempotent).

        Once started, :meth:`notify` enqueues alerts and returns
        immediately; a single daemon worker drains the queue, runs
        :meth:`_do_dispatch` per alert with a per-alert deadline, and
        sweeps the pending-retry history periodically.

        Matches the bounded-teardown pattern from issue #1672: the worker
        is a daemon (exits when the process exits) and :meth:`close`
        performs a bounded join. Calling this twice is a no-op.
        """
        with self._dispatch_lock:
            if self._dispatch_thread is not None and self._dispatch_thread.is_alive():
                return
            self._dispatch_queue = queue.Queue()
            self._dispatch_stopped.clear()
            self._dispatch_started.clear()
            thread = threading.Thread(
                target=self._dispatch_worker,
                name=f"alert-dispatcher-{id(self)}",
                daemon=True,
            )
            self._dispatch_thread = thread
            thread.start()
        # Wait for the worker to signal it has entered its main loop so
        # callers that immediately enqueue don't race the startup.
        self._dispatch_started.wait(timeout=5.0)

    def close(self, timeout_s: float = 5.0) -> None:
        """Signal the worker and join with a bounded timeout.

        Bounded by design (issue #1672 family): a wedged destination
        cannot block Campaign teardown forever. Any alerts still enqueued
        past the deadline are abandoned — their on_alert callbacks will
        not fire, matching the failure-as-best-effort contract. Idempotent.

        Also shuts down the bounded dispatch thread pool
        (:data:`_ALERT_DISPATCH_POOL_SIZE`, issue #1770) so in-flight
        per-alert workers do not survive the process teardown.
        """
        thread = self._dispatch_thread
        q = self._dispatch_queue
        if thread is not None and q is not None:
            with contextlib.suppress(Exception):  # pragma: no cover — best effort
                q.put_nowait(None)
            thread.join(timeout=timeout_s)
            self._dispatch_stopped.set()
            if thread.is_alive():
                log.warning(
                    "alert dispatcher did not exit within %.1fs — abandoning %d pending alerts",
                    timeout_s,
                    q.qsize(),
                )
        # Shut down the bounded per-alert dispatch pool. ``wait=False``
        # is the matching semantic of the bounded-teardown contract:
        # an in-flight per-alert worker that hasn't finished by the
        # caller's deadline is left to die (it's not a daemon-thread
        # leak — the pool reuses a fixed-size thread set on the next
        # campaign run, so the OS reclaims any stragglers at process
        # exit).
        if self._dispatch_pool is not None:
            self._dispatch_pool.shutdown(wait=False, cancel_futures=True)
            self._dispatch_pool = None

    def _enqueue_dispatch(self, alert: Alert, rule: AlertRule) -> None:
        q = self._dispatch_queue
        if q is None:
            # Should not happen — notify() only enqueues when the
            # dispatcher is running. Fall back to inline dispatch so
            # the alert is never silently dropped.
            self._do_dispatch(alert, rule)
            return
        try:
            q.put_nowait((alert, rule))
        except queue.Full:  # pragma: no cover — queue is unbounded
            log.warning(
                "alert dispatch queue full — falling back to inline dispatch for rule %s",
                rule.name,
            )
            self._do_dispatch(alert, rule)

    def _dispatch_worker(self) -> None:
        """Worker loop: drain the queue, run _do_dispatch per alert.

        Also runs the pending-retry sweep (issue #1185) once at startup
        and then on each enqueue wakeup. Exits cleanly when the queue
        receives a sentinel ``None``.
        """
        assert self._dispatch_queue is not None  # set by start_background_dispatch
        q = self._dispatch_queue
        self._dispatch_started.set()
        # Drain pending at startup so any alerts left from a prior
        # process / campaign session get re-attempted promptly.
        self._retry_pending()
        while True:
            try:
                item = q.get()
            except Exception as exc:
                log.warning("alert dispatcher queue.get failed: %s", exc)
                continue
            if item is None:
                q.task_done()
                break
            alert, rule = item
            try:
                self._dispatch_one(alert, rule)
            except Exception as exc:
                log.warning(
                    "alert dispatcher unhandled error on rule %s: %s",
                    rule.name,
                    exc,
                    exc_info=True,
                )
            finally:
                q.task_done()
        self._dispatch_stopped.set()

    def _dispatch_one(self, alert: Alert, rule: AlertRule) -> None:
        """Run _do_dispatch for one alert with a per-alert deadline.

        Issue #1770 — the per-alert dispatch runs on a class-level
        :class:`ThreadPoolExecutor` of size
        :data:`_ALERT_DISPATCH_POOL_SIZE` (default 4), reused across
        every alert.  Pre-fix this method spawned a fresh
        ``daemon=True`` thread for every alert that arrived while the
        dispatcher worker was running — 50 wedged alerts ⇒ 50 daemon
        threads, each independently blocked in ``_do_dispatch``.

        The per-alert deadline still prevents a single wedged
        destination (a webhook with a blackholed DNS, an unreachable
        SMTP relay) from clogging the pool indefinitely: when the
        future exceeds the deadline we log + drop the alert (the
        in-flight worker continues; the worker slot is freed on
        return).  ``Future.cancel()`` is a best-effort hint — pool
        workers cannot be interrupted mid-``send()``, so the slot may
        not free immediately on expiry, but the *pool* never grows
        past the configured ceiling.

        When ``per_alert_deadline_s <= 0`` the call runs synchronously
        on the dispatcher's worker thread (legacy fast-path).
        """
        if self._per_alert_deadline_s <= 0:
            self._do_dispatch(alert, rule)
            return
        pool = self._get_dispatch_pool()
        future: Future[None] = pool.submit(self._do_dispatch, alert, rule)
        try:
            future.result(timeout=self._per_alert_deadline_s)
        except TimeoutError:
            log.warning(
                "alert dispatch for rule %s exceeded %.1fs deadline — abandoning delivery "
                "(destination may be unreachable)",
                rule.name,
                self._per_alert_deadline_s,
            )
            # Best-effort cancel — if the worker has already finished
            # ``send()`` the cancel is a no-op.  The worker slot frees
            # on return; the pool size never grows past the ceiling
            # because we only ever call ``submit()``, not ``Thread(...)``.
            future.cancel()
        except Exception as exc:
            # ``_do_dispatch`` swallows its own exceptions, so this
            # branch only fires for programming errors in the dispatch
            # layer itself — keep the dispatcher worker alive and log.
            log.warning(
                "alert dispatch for rule %s raised in pool: %s",
                rule.name,
                exc,
            )

    def _get_dispatch_pool(self) -> ThreadPoolExecutor:
        """Return (lazily-create) the class-level dispatch thread pool."""
        if self._dispatch_pool is None:
            self._dispatch_pool = ThreadPoolExecutor(
                max_workers=_ALERT_DISPATCH_POOL_SIZE,
                thread_name_prefix=f"alert-dispatch-{id(self)}",
            )
        return self._dispatch_pool

    def update_cache_stats(self, stats: dict[str, Any]) -> None:
        self._cache_stats = stats

    def get_alert_history(self) -> list[dict[str, Any]]:
        """Return the current alert delivery-failure history as a list of dicts.

        Each entry contains the alert fields (``rule_name``, ``event_type``,
        ``severity``, ``message``, ``timestamp``) plus ``destination`` (the
        destination class name), ``failed_at``, ``error``, and
        ``delivery_status`` (``"failed"`` for all entries in the history).
        """
        with self._history_lock:
            entries = list(self._alert_history)
        result: list[dict[str, Any]] = []
        for entry in entries:
            d: dict[str, Any] = {
                "rule_name": entry.alert.rule_name,
                "event_type": entry.alert.event_type,
                "severity": entry.alert.severity,
                "message": entry.alert.message,
                "timestamp": entry.alert.timestamp,
                "destination": type(entry.destination).__name__,
                "failed_at": entry.failed_at,
                "error": entry.error,
                "delivery_status": "failed",
            }
            result.append(d)
        return result

    # ------------------------------------------------------------------
    # Pre-defined rules
    # ------------------------------------------------------------------
    @staticmethod
    def _always_condition(_: dict[str, Any]) -> bool:
        return True

    @staticmethod
    def _campaign_failed_condition(context: dict[str, Any]) -> bool:
        return context.get("status") == "failure"

    @staticmethod
    def _sample_failed_condition(context: dict[str, Any]) -> bool:
        return context.get("status") == "failed"

    @staticmethod
    def _cache_miss_rate_condition(context: dict[str, Any]) -> bool:
        hit_rate = float(context.get("cache_hit_rate", 1.0))
        return bool(hit_rate < 0.5)

    @staticmethod
    def _worker_dead_condition(context: dict[str, Any]) -> bool:
        return True

    def builtin_rules(self) -> list[AlertRule]:
        """Return the built-in alerting rules."""
        return [
            AlertRule(
                name="campaign-failed",
                event_type="campaign.failed",
                condition=self._campaign_failed_condition,
                severity=AlertSeverity.CRITICAL,
                message_template="Campaign {campaign_id} failed: {error}",
            ),
            AlertRule(
                name="sample-failed",
                event_type="sample.failed",
                condition=self._sample_failed_condition,
                severity=AlertSeverity.WARNING,
                message_template="Sample {sample_id} failed after {max_retries} retries: {error}",
            ),
            AlertRule(
                name="cache-miss-rate-low",
                event_type="cache.miss_rate_low",
                condition=self._cache_miss_rate_condition,
                severity=AlertSeverity.WARNING,
                message_template="Cache hit rate {cache_hit_rate:.1f}%% below 50%% threshold",
            ),
            AlertRule(
                name="worker-dead",
                event_type="worker.dead",
                condition=self._worker_dead_condition,
                severity=AlertSeverity.CRITICAL,
                message_template="Worker {worker_id} (node {worker_ip}) stopped responding",
            ),
            AlertRule(
                name="campaign-started",
                event_type="campaign.started",
                condition=self._always_condition,
                severity=AlertSeverity.INFO,
                message_template="Campaign {campaign_id} started ({n_samples} samples, {algorithm})",
            ),
            AlertRule(
                name="campaign-completed",
                event_type="campaign.completed",
                condition=self._always_condition,
                severity=AlertSeverity.INFO,
                message_template="Campaign {campaign_id} completed successfully in {elapsed_s:.1f}s",
            ),
        ]


def _render_template(template: str, context: dict[str, Any]) -> str:
    """Render a simple ``{key}``-style message template.

    Uses :meth:`str.format` with a safe fallback for missing keys.
    """
    try:
        return template.format_map(context)
    except KeyError:
        return template


# ---------------------------------------------------------------------------
# YAML configuration loader
# ---------------------------------------------------------------------------


def _alwaysCondition(context: dict[str, Any]) -> bool:
    return True


def load_alert_rules_from_yaml(path: Path) -> list[AlertRule]:
    """Load alert rules from a YAML file.

    Each rule entry supports two condition types:

    - ``type: always`` — condition always fires (no extra fields).
    - ``type: expr`` — condition is a Python expression evaluated with
      the event context as local variables.

    Example YAML::

        rules:
          - name: campaign-failed
            event_type: campaign.failed
            severity: CRITICAL
            message_template: "Campaign {campaign_id} failed"
            condition:
              type: always
              value: true
    """
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:
        log.error("failed to load alert rules from %s: %s", path, exc)
        return []

    if not isinstance(data, dict) or "rules" not in data:
        return []

    rules: list[AlertRule] = []
    for entry in data.get("rules", []):
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name", "unnamed"))
        event_type = str(entry.get("event_type", ""))
        severity = str(entry.get("severity", AlertSeverity.INFO))
        message_template = str(entry.get("message_template", ""))

        condition_entry = entry.get("condition", {})
        if not isinstance(condition_entry, dict):
            condition_entry = {}
        cond_type = str(condition_entry.get("type", "always"))

        if cond_type == "always":
            condition: Callable[[dict[str, Any]], bool] = _alwaysCondition
        elif cond_type == "expr":
            expr = str(condition_entry.get("value", "True"))
            condition = _make_expr_condition(expr)
        else:
            log.warning("unknown condition type %r for rule %s — skipping", cond_type, name)
            continue

        rules.append(
            AlertRule(
                name=name,
                event_type=event_type,
                condition=condition,
                severity=severity,
                message_template=message_template,
            )
        )

    return rules


def _make_expr_condition(expr: str) -> Callable[[dict[str, Any]], bool]:
    """Create a condition callable from a Python expression string."""

    def condition(context: dict[str, Any]) -> bool:
        try:
            return bool(safe_eval(expr, context))
        except (ExpressionError, SyntaxError) as exc:
            log.warning("condition expression %r raised: %s — treating as False", expr, exc)
            return False

    return condition


def load_alert_destinations_from_yaml(path: Path) -> list[AlertDestination]:
    """Load alert destinations from a YAML file.

    Example YAML::

        destinations:
          - type: webhook
            url: https://hooks.example.com/osimflow
          - type: email
            smtp_host: smtp.example.com
            recipients:
              - ops@example.com
          - type: log
            level: WARNING
    """
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:
        log.error("failed to load alert destinations from %s: %s", path, exc)
        return []

    if not isinstance(data, dict) or "destinations" not in data:
        return []

    destinations: list[AlertDestination] = []
    for entry in data.get("destinations", []):
        if not isinstance(entry, dict):
            continue
        dest_type = str(entry.get("type", ""))
        if dest_type == "webhook":
            url = str(entry.get("url", ""))
            if url:
                destinations.append(
                    WebhookDestination(
                        url=url,
                        timeout=float(entry.get("timeout", 30.0)),
                        max_retries=int(entry.get("max_retries", 3)),
                    )
                )
        elif dest_type == "email":
            smtp_host = str(entry.get("smtp_host", ""))
            if smtp_host:
                destinations.append(
                    EmailDestination(
                        smtp_host=smtp_host,
                        smtp_port=int(entry.get("smtp_port", 587)),
                        recipients=entry.get("recipients", []),
                        sender=str(entry.get("sender", "osimflow@example.com")),
                        use_tls=bool(entry.get("use_tls", True)),
                    )
                )
        elif dest_type == "log":
            destinations.append(LogDestination(level=str(entry.get("level", "WARNING"))))
        else:
            log.warning("unknown destination type %r — skipping", dest_type)

    return destinations


def build_alert_manager(
    rules_path: Path | None = None,
    destinations_path: Path | None = None,
    include_builtin: bool = True,
    *,
    on_alert: Callable[[Alert], None] | None = None,
    background_dispatch: bool = False,
    per_alert_deadline_s: float = 30.0,
) -> AlertManager:
    """Build and configure an AlertManager from YAML files.

    Parameters
    ----------
    rules_path
        Path to a YAML file defining alert rules.
        When ``None``, no custom rules are loaded.
    destinations_path
        Path to a YAML file defining alert destinations.
        When ``None``, no destinations are configured.
    include_builtin
        When ``True`` (default), the built-in rules are registered
        before loading custom rules.
    on_alert
        Optional callback invoked for every alert dispatched (issue #1308).
        Intended to forward fired alerts to RunTrace.alerts_fired.
    background_dispatch
        When ``True`` (issue #1673), start a background dispatcher thread
        so destination.send() (and its blocking retries) never run on
        the caller's thread — the campaign's fan-out workers stay free.
        Defaults to ``False`` to preserve the legacy synchronous path
        (and existing tests that assert on synchronous delivery_status).
    per_alert_deadline_s
        When background dispatch is enabled, the wall-clock budget for
        each individual alert delivery. Past the deadline the worker
        abandons that delivery (logs a WARNING) and moves on — a wedged
        webhook cannot clog the dispatcher. Only consulted when
        ``background_dispatch=True``.
    """
    manager = AlertManager(
        on_alert=on_alert,
        per_alert_deadline_s=per_alert_deadline_s,
    )

    if include_builtin:
        for rule in manager.builtin_rules():
            manager.add_rule(rule)

    if rules_path is not None and rules_path.is_file():
        for rule in load_alert_rules_from_yaml(rules_path):
            manager.add_rule(rule)

    if destinations_path is not None and destinations_path.is_file():
        for dest in load_alert_destinations_from_yaml(destinations_path):
            manager.add_destination(dest)

    if background_dispatch:
        manager.start_background_dispatch()

    return manager
