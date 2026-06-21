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

import abc
import dataclasses
import json
import logging
import smtplib
import time
import urllib.error
import urllib.request
from collections.abc import Callable
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


@dataclasses.dataclass
class AlertRule:
    """A rule that evaluates campaign events and produces alerts."""

    name: str
    event_type: str
    condition: Callable[[dict[str, Any]], bool]
    severity: str
    message_template: str


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

    def __init__(self) -> None:
        self._rules: list[AlertRule] = []
        self._destinations: list[AlertDestination] = []
        self._cache_stats: dict[str, Any] = {}

    def add_rule(self, rule: AlertRule) -> None:
        self._rules.append(rule)

    def add_destination(self, dest: AlertDestination) -> None:
        self._destinations.append(dest)

    def notify(self, event_type: str, context: dict[str, Any]) -> None:
        """Evaluate all rules matching *event_type* and dispatch alerts.

        Best-effort: destination failures are logged but never raise.
        """
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
            )

            for dest in self._destinations:
                try:
                    dest.send(alert)
                except Exception as exc:
                    log.warning(
                        "alert destination %s failed for rule %s: %s",
                        type(dest).__name__,
                        rule.name,
                        exc,
                    )

    def update_cache_stats(self, stats: dict[str, Any]) -> None:
        self._cache_stats = stats

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
    """
    manager = AlertManager()

    if include_builtin:
        for rule in manager.builtin_rules():
            manager.add_rule(rule)

    if rules_path is not None and rules_path.is_file():
        for rule in load_alert_rules_from_yaml(rules_path):
            manager.add_rule(rule)

    if destinations_path is not None and destinations_path.is_file():
        for dest in load_alert_destinations_from_yaml(destinations_path):
            manager.add_destination(dest)

    return manager
