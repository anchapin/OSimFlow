"""Unit tests for BYOS audit-trail events (issue #1399).

Provenance of BYOS user-script loads and trust-level rejections must
reach ``audit.jsonl`` — previously the in-process load left no trace
and the trust-level rejection only logged at WARNING.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from osimflow.audit import AuditLogger
from osimflow.byos import ByosTrustLevel, load_user_function, validate_trust_level
from osimflow.byos_contract import BYOS_CONTRACT_VERSION


def _read_events(outdir: Path) -> list[dict[str, object]]:
    audit = outdir / "audit.jsonl"
    if not audit.exists():
        return []
    return [json.loads(line) for line in audit.read_text().splitlines() if line.strip()]


class TestTrustLevelRejectionAudit:
    def test_rejection_writes_audit_event(self, tmp_path: Path) -> None:
        audit = AuditLogger(tmp_path)
        with pytest.raises(ValueError, match="inprocess"):
            validate_trust_level(
                ByosTrustLevel.INPROCESS,
                None,
                audit_logger=audit,
                script_path=Path("/scripts/apply.py"),
            )
        events = _read_events(tmp_path)
        assert any(e["action"] == "byos.trust_level_rejected" for e in events)
        ev = next(e for e in events if e["action"] == "byos.trust_level_rejected")
        assert ev["resource"] == "/scripts/apply.py"
        details = ev["details"]
        assert details["trust_level"] == "inprocess"
        assert details["require_trusted_scripts"] is None
        assert details["contract_version"] == BYOS_CONTRACT_VERSION

    def test_rejection_with_require_trusted_scripts_true(self, tmp_path: Path) -> None:
        audit = AuditLogger(tmp_path)
        with pytest.raises(ValueError, match="require-trusted-scripts"):
            validate_trust_level(
                ByosTrustLevel.INPROCESS,
                True,
                audit_logger=audit,
            )
        events = _read_events(tmp_path)
        ev = next(e for e in events if e["action"] == "byos.trust_level_rejected")
        assert ev["details"]["require_trusted_scripts"] is True
        assert ev["resource"] == "<unknown-script>"

    def test_subprocess_level_never_rejects_or_audits(self, tmp_path: Path) -> None:
        audit = AuditLogger(tmp_path)
        validate_trust_level(
            ByosTrustLevel.SUBPROCESS,
            None,
            audit_logger=audit,
        )
        assert _read_events(tmp_path) == []

    def test_no_logger_keeps_rejection_behaviour(self) -> None:
        with pytest.raises(ValueError, match="inprocess"):
            validate_trust_level(ByosTrustLevel.INPROCESS, None)


class TestByosLoadedAudit:
    @staticmethod
    def _write_script(tmp_path: Path) -> Path:
        script = tmp_path / "my_apply.py"
        script.write_text(
            "def apply_parameters(template, parameters, sample_id, out):\n    return template\n",
            encoding="utf-8",
        )
        return script

    def test_inprocess_success_writes_loaded_event(self, tmp_path: Path) -> None:
        script = self._write_script(tmp_path)
        audit = AuditLogger(tmp_path)
        with pytest.warns(UserWarning, match="inprocess"):
            load_user_function(
                script,
                trust_level=ByosTrustLevel.INPROCESS,
                audit_logger=audit,
            )
        events = _read_events(tmp_path)
        ev = next(e for e in events if e["action"] == "byos.loaded")
        assert ev["resource"] == str(script)
        assert ev["details"]["trust_level"] == "inprocess"
        assert ev["details"]["contract_version"] == BYOS_CONTRACT_VERSION

    def test_subprocess_success_writes_loaded_event(self, tmp_path: Path) -> None:
        script = self._write_script(tmp_path)
        audit = AuditLogger(tmp_path)
        load_user_function(
            script,
            trust_level=ByosTrustLevel.SUBPROCESS,
            audit_logger=audit,
        )
        events = _read_events(tmp_path)
        ev = next(e for e in events if e["action"] == "byos.loaded")
        assert ev["resource"] == str(script)
        assert ev["details"]["trust_level"] == "subprocess"

    def test_failed_load_writes_no_loaded_event(self, tmp_path: Path) -> None:
        bad = tmp_path / "broken.py"
        bad.write_text("this is not python\n", encoding="utf-8")
        audit = AuditLogger(tmp_path)
        with pytest.raises(Exception):  # noqa: B017, PT011 — loader raises broadly
            load_user_function(bad, trust_level=ByosTrustLevel.SUBPROCESS, audit_logger=audit)
        assert not any(e["action"] == "byos.loaded" for e in _read_events(tmp_path))
