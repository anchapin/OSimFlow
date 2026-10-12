"""Tests for osimflow/audit.py (issue #439)."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from osimflow.audit import (
    _REDACTED,
    CAMPAIGN_COMPLETED,
    CAMPAIGN_CREATED,
    CAMPAIGN_FAILED,
    CAMPAIGN_STARTED,
    CAMPAIGN_STOPPED,
    CONFIG_CHANGED,
    JOB_COMPLETED,
    JOB_FAILED,
    JOB_SUBMITTED,
    SAMPLE_COMPLETED,
    SAMPLE_CREATED,
    SAMPLE_FAILED,
    SECRETS_ACCESSED,
    AuditEvent,
    AuditLogger,
    AuditOutcome,
    _redact_dict,
    api_actor,
    api_actor_from_request,
    cli_actor,
)


class TestAuditEvent:
    def test_required_fields_present(self):
        event = AuditEvent(
            action="test.action",
            resource="test-resource",
        )
        assert event.action == "test.action"
        assert event.resource == "test-resource"
        assert event.actor == "system"
        assert event.outcome == AuditOutcome.SUCCESS

    def test_outcome_is_enum(self):
        assert AuditOutcome.SUCCESS.value == "SUCCESS"
        assert AuditOutcome.FAILURE.value == "FAILURE"

    def test_to_dict_includes_all_fields(self):
        event = AuditEvent(
            actor="cli:alex",
            action="campaign.started",
            resource="c-001",
            details={"executor": "local"},
            outcome=AuditOutcome.SUCCESS,
        )
        d = event.to_dict()
        assert d["actor"] == "cli:alex"
        assert d["action"] == "campaign.started"
        assert d["resource"] == "c-001"
        assert d["details"]["executor"] == "local"
        assert d["outcome"] == "SUCCESS"

    def test_to_json_line_produces_valid_json(self):
        event = AuditEvent(
            actor="cli:alex",
            action="campaign.started",
            resource="c-001",
        )
        line = event.to_json_line()
        parsed = json.loads(line)
        assert parsed["actor"] == "cli:alex"
        assert parsed["action"] == "campaign.started"

    def test_timestamp_default_is_utc_now(self):
        before = datetime.now(UTC)
        event = AuditEvent(action="test", resource="r")
        after = datetime.now(UTC)
        assert before <= event.timestamp <= after

    def test_details_redacted_on_serialization(self):
        event = AuditEvent(
            actor="cli:alex",
            action="test",
            resource="r",
            details={"password": "secret123", "username": "alex"},
        )
        d = event.to_dict()
        assert d["details"]["password"] == _REDACTED
        assert d["details"]["username"] == "alex"


class TestRedaction:
    def test_redact_dict_redacts_sensitive_fields(self):
        original = {
            "password": "secret123",
            "api_key": "key-123",
            "username": "alex",
            "token": "bearer-token",
        }
        redacted = _redact_dict(original)
        assert redacted["password"] == _REDACTED
        assert redacted["api_key"] == _REDACTED
        assert redacted["username"] == "alex"
        assert redacted["token"] == _REDACTED

    def test_redact_dict_case_insensitive(self):
        original = {"PASSWORD": "secret", "Api_Key": "key", "SECRET": "val"}
        redacted = _redact_dict(original)
        assert redacted["PASSWORD"] == _REDACTED
        assert redacted["Api_Key"] == _REDACTED
        assert redacted["SECRET"] == _REDACTED

    def test_redact_dict_nested(self):
        original = {"user": {"password": "secret", "name": "alex"}}
        redacted = _redact_dict(original)
        assert redacted["user"]["password"] == _REDACTED
        assert redacted["user"]["name"] == "alex"

    def test_redact_dict_unknown_fields_preserved(self):
        original = {"username": "alex", "age": 30}
        redacted = _redact_dict(original)
        assert redacted["username"] == "alex"
        assert redacted["age"] == 30

    def test_redact_dict_empty(self):
        assert _redact_dict({}) == {}

    def test_redact_dict_none_value(self):
        original = {"password": None, "username": "alex"}
        redacted = _redact_dict(original)
        assert redacted["password"] == _REDACTED
        assert redacted["username"] == "alex"


class TestActorHelpers:
    def test_cli_actor_returns_correct_format(self):
        actor = cli_actor()
        assert actor.startswith("cli:")

    def test_api_actor_with_name(self):
        assert api_actor("alice") == "api:alice"

    def test_api_actor_with_none(self):
        assert api_actor(None) == "anonymous"

    def test_api_actor_with_empty_string(self):
        assert api_actor("") == "anonymous"

    def test_api_actor_from_request_with_api_user(self):
        class MockState:
            user_id = "bob"

        class MockRequest:
            state = MockState()

        actor = api_actor_from_request(MockRequest())
        assert actor == "api:bob"

    def test_api_actor_from_request_without_state(self):
        class MockRequest:
            pass

        actor = api_actor_from_request(MockRequest())
        assert actor == "anonymous"

    def test_api_actor_from_request_without_user_id(self):
        class MockState:
            pass

        class MockRequest:
            state = MockState()

        actor = api_actor_from_request(MockRequest())
        assert actor == "anonymous"


class TestActionConstants:
    def test_campaign_constants(self):
        assert CAMPAIGN_CREATED == "campaign.created"
        assert CAMPAIGN_STARTED == "campaign.started"
        assert CAMPAIGN_STOPPED == "campaign.stopped"
        assert CAMPAIGN_COMPLETED == "campaign.completed"
        assert CAMPAIGN_FAILED == "campaign.failed"

    def test_sample_constants(self):
        assert SAMPLE_CREATED == "sample.created"
        assert SAMPLE_COMPLETED == "sample.completed"
        assert SAMPLE_FAILED == "sample.failed"

    def test_config_secrets_constants(self):
        assert CONFIG_CHANGED == "config.changed"
        assert SECRETS_ACCESSED == "secrets.accessed"

    def test_job_constants(self):
        assert JOB_SUBMITTED == "job.submitted"
        assert JOB_COMPLETED == "job.completed"
        assert JOB_FAILED == "job.failed"


class TestAuditLogger:
    def test_writes_jsonl_file(self, tmp_path):
        logger = AuditLogger(outdir=tmp_path)
        logger.log(
            AuditEvent(
                action="test.action",
                resource="test-resource",
            )
        )
        audit_file = tmp_path / "audit.jsonl"
        assert audit_file.exists()
        lines = audit_file.read_text().splitlines()
        assert len(lines) == 1
        parsed = json.loads(lines[0])
        assert parsed["action"] == "test.action"
        assert parsed["resource"] == "test-resource"

    def test_writes_multiple_events(self, tmp_path):
        logger = AuditLogger(outdir=tmp_path)
        for i in range(3):
            logger.log(
                AuditEvent(
                    action=f"test.action.{i}",
                    resource="test-resource",
                )
            )
        audit_file = tmp_path / "audit.jsonl"
        lines = audit_file.read_text().splitlines()
        assert len(lines) == 3

    def test_audit_path_property(self, tmp_path):
        logger = AuditLogger(outdir=tmp_path)
        assert logger.audit_path == tmp_path / "audit.jsonl"

    def test_creates_outdir_if_missing(self, tmp_path):
        outdir = tmp_path / "nested" / "dir"
        logger = AuditLogger(outdir=outdir)
        logger.log(AuditEvent(action="test", resource="r"))
        assert outdir.exists()

    def test_campaign_created_event(self, tmp_path):
        logger = AuditLogger(outdir=tmp_path)
        logger.campaign_created(
            campaign_id="c-001",
            executor="local",
            n_samples=10,
            openstudio_version="3.11.0",
        )
        audit_file = tmp_path / "audit.jsonl"
        parsed = json.loads(audit_file.read_text())
        assert parsed["action"] == "campaign.created"
        assert parsed["resource"] == "c-001"
        assert parsed["details"]["executor"] == "local"
        assert parsed["details"]["n_samples"] == 10
        assert parsed["outcome"] == "SUCCESS"

    def test_campaign_started_event(self, tmp_path):
        logger = AuditLogger(outdir=tmp_path)
        logger.campaign_started(campaign_id="c-001")
        audit_file = tmp_path / "audit.jsonl"
        parsed = json.loads(audit_file.read_text())
        assert parsed["action"] == "campaign.started"
        assert parsed["resource"] == "c-001"

    def test_campaign_completed_event(self, tmp_path):
        logger = AuditLogger(outdir=tmp_path)
        logger.campaign_completed(
            campaign_id="c-001",
            duration_s=120.5,
            n_succeeded=9,
            n_failed=1,
        )
        audit_file = tmp_path / "audit.jsonl"
        parsed = json.loads(audit_file.read_text())
        assert parsed["action"] == "campaign.completed"
        assert parsed["resource"] == "c-001"
        assert parsed["details"]["duration_s"] == 120.5
        assert parsed["details"]["n_succeeded"] == 9
        assert parsed["details"]["n_failed"] == 1
        assert parsed["outcome"] == "SUCCESS"

    def test_campaign_failed_event(self, tmp_path):
        logger = AuditLogger(outdir=tmp_path)
        logger.campaign_failed(campaign_id="c-001", reason="seed model invalid")
        audit_file = tmp_path / "audit.jsonl"
        parsed = json.loads(audit_file.read_text())
        assert parsed["action"] == "campaign.failed"
        assert parsed["resource"] == "c-001"
        assert parsed["details"]["reason"] == "seed model invalid"
        assert parsed["outcome"] == "FAILURE"

    def test_campaign_stopped_event(self, tmp_path):
        logger = AuditLogger(outdir=tmp_path)
        logger.campaign_stopped(campaign_id="c-001")
        audit_file = tmp_path / "audit.jsonl"
        parsed = json.loads(audit_file.read_text())
        assert parsed["action"] == "campaign.stopped"
        assert parsed["resource"] == "c-001"

    def test_sample_created_event(self, tmp_path):
        logger = AuditLogger(outdir=tmp_path)
        logger.sample_created(
            campaign_id="c-001",
            sample_id="s-001",
            values={"temperature": 22.0},
        )
        audit_file = tmp_path / "audit.jsonl"
        parsed = json.loads(audit_file.read_text())
        assert parsed["action"] == "sample.created"
        assert parsed["resource"] == "c-001/s-001"
        assert parsed["details"]["values"]["temperature"] == 22.0

    def test_sample_completed_event(self, tmp_path):
        logger = AuditLogger(outdir=tmp_path)
        logger.sample_completed(campaign_id="c-001", sample_id="s-001")
        audit_file = tmp_path / "audit.jsonl"
        parsed = json.loads(audit_file.read_text())
        assert parsed["action"] == "sample.completed"
        assert parsed["resource"] == "c-001/s-001"

    def test_sample_failed_event(self, tmp_path):
        logger = AuditLogger(outdir=tmp_path)
        logger.sample_failed(
            campaign_id="c-001",
            sample_id="s-001",
            error="EnergyPlus crashed",
        )
        audit_file = tmp_path / "audit.jsonl"
        parsed = json.loads(audit_file.read_text())
        assert parsed["action"] == "sample.failed"
        assert parsed["resource"] == "c-001/s-001"
        assert parsed["details"]["error"] == "EnergyPlus crashed"
        assert parsed["outcome"] == "FAILURE"

    def test_config_changed_event(self, tmp_path):
        logger = AuditLogger(outdir=tmp_path)
        logger.config_changed(
            campaign_id="c-001",
            field="n_samples",
            old_value=100,
            new_value=200,
        )
        audit_file = tmp_path / "audit.jsonl"
        parsed = json.loads(audit_file.read_text())
        assert parsed["action"] == "config.changed"
        assert parsed["resource"] == "c-001"
        assert parsed["details"]["field"] == "n_samples"
        assert parsed["details"]["old_value"] == "100"
        assert parsed["details"]["new_value"] == "200"

    def test_secrets_accessed_event(self, tmp_path):
        logger = AuditLogger(outdir=tmp_path)
        logger.secrets_accessed(campaign_id="c-001", field="api_key")
        audit_file = tmp_path / "audit.jsonl"
        parsed = json.loads(audit_file.read_text())
        assert parsed["action"] == "secrets.accessed"
        assert parsed["resource"] == "c-001"
        assert parsed["details"]["field"] == "api_key"
        assert parsed["outcome"] == "FAILURE"

    def test_job_submitted_event(self, tmp_path):
        logger = AuditLogger(outdir=tmp_path)
        logger.job_submitted(
            campaign_id="c-001",
            sample_id="s-001",
            job_id="j-001",
            executor="slurm",
        )
        audit_file = tmp_path / "audit.jsonl"
        parsed = json.loads(audit_file.read_text())
        assert parsed["action"] == "job.submitted"
        assert parsed["resource"] == "c-001/s-001"
        assert parsed["details"]["job_id"] == "j-001"
        assert parsed["details"]["executor"] == "slurm"

    def test_job_completed_event(self, tmp_path):
        logger = AuditLogger(outdir=tmp_path)
        logger.job_completed(
            campaign_id="c-001",
            sample_id="s-001",
            job_id="j-001",
        )
        audit_file = tmp_path / "audit.jsonl"
        parsed = json.loads(audit_file.read_text())
        assert parsed["action"] == "job.completed"
        assert parsed["resource"] == "c-001/s-001"
        assert parsed["details"]["job_id"] == "j-001"

    def test_job_failed_event(self, tmp_path):
        logger = AuditLogger(outdir=tmp_path)
        logger.job_failed(
            campaign_id="c-001",
            sample_id="s-001",
            job_id="j-001",
            error="memory limit exceeded",
        )
        audit_file = tmp_path / "audit.jsonl"
        parsed = json.loads(audit_file.read_text())
        assert parsed["action"] == "job.failed"
        assert parsed["resource"] == "c-001/s-001"
        assert parsed["details"]["job_id"] == "j-001"
        assert parsed["details"]["error"] == "memory limit exceeded"
        assert parsed["outcome"] == "FAILURE"

    def test_actor_defaults_to_cli(self, tmp_path):
        logger = AuditLogger(outdir=tmp_path)
        logger.campaign_started(campaign_id="c-001")
        audit_file = tmp_path / "audit.jsonl"
        parsed = json.loads(audit_file.read_text())
        assert parsed["actor"].startswith("cli:")

    def test_explicit_actor_overrides_default(self, tmp_path):
        logger = AuditLogger(outdir=tmp_path)
        logger.campaign_started(campaign_id="c-001", actor="api:bob")
        audit_file = tmp_path / "audit.jsonl"
        parsed = json.loads(audit_file.read_text())
        assert parsed["actor"] == "api:bob"

    def test_timestamp_is_iso_format(self, tmp_path):
        logger = AuditLogger(outdir=tmp_path)
        logger.campaign_started(campaign_id="c-001")
        audit_file = tmp_path / "audit.jsonl"
        parsed = json.loads(audit_file.read_text())
        assert "T" in parsed["timestamp"]
        assert "+00:00" in parsed["timestamp"]

    def test_details_redacted_in_factory_methods(self, tmp_path):
        logger = AuditLogger(outdir=tmp_path)
        logger.campaign_created(
            campaign_id="c-001",
            executor="local",
            n_samples=10,
            openstudio_version="3.11.0",
            actor="cli:alex",
        )
        audit_file = tmp_path / "audit.jsonl"
        parsed = json.loads(audit_file.read_text())
        assert parsed["actor"] == "cli:alex"
        assert parsed["details"]["executor"] == "local"
