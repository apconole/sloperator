"""Tests for the append-only audit log."""

from __future__ import annotations

import json
from pathlib import Path

from sloperator.audit import AuditEvent, AuditKind, AuditLog
from sloperator.security.redaction import REDACTED, register_secret


def test_event_json_line_roundtrip() -> None:
    event = AuditEvent(
        kind=AuditKind.TOOL_CALL,
        project_id="example",
        task_id="task-1",
        role="triage",
        backend="flash",
        model="qwen3.8-flash",
        tool="git.log",
        tool_arguments={"repo": "/srv/git/example.git"},
        input_references=["message:abc@example.org"],
    )
    data = json.loads(event.to_json_line())
    assert data["kind"] == "tool_call"
    assert data["project_id"] == "example"
    assert data["task_id"] == "task-1"
    assert data["tool"] == "git.log"
    assert isinstance(data["timestamp"], str)
    assert data["input_references"] == ["message:abc@example.org"]


def test_sensitive_arguments_and_text_redacted() -> None:
    register_secret("s3cr3t")
    event = AuditEvent(
        kind=AuditKind.MODEL_INVOCATION,
        tool_arguments={"api_key": "hunter2", "note": "value s3cr3t"},
        result_summary="used s3cr3t somewhere",
        message="header s3cr3t end",
    )
    data = json.loads(event.to_json_line())
    assert data["tool_arguments"]["api_key"] == REDACTED
    assert "hunter2" not in event.to_json_line()
    assert data["tool_arguments"]["note"] == f"value {REDACTED}"
    assert data["result_summary"] == f"used {REDACTED} somewhere"
    assert data["message"] == f"header {REDACTED} end"


def test_audit_log_appends_records(tmp_path: Path) -> None:
    log_path = tmp_path / "logs" / "audit.log"
    with AuditLog(log_path) as audit:
        audit.record(AuditEvent(kind=AuditKind.DECISION, message="first"))
        audit.record(AuditEvent(kind=AuditKind.ERROR, message="second"))
    lines = log_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["message"] == "first"
    assert json.loads(lines[1])["kind"] == "error"


def test_audit_log_creates_parent_dirs(tmp_path: Path) -> None:
    log_path = tmp_path / "deep" / "nested" / "audit.log"
    audit = AuditLog(log_path)
    audit.record(AuditEvent(kind=AuditKind.ARTIFACT_CREATED))
    audit.close()
    assert log_path.is_file()
