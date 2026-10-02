"""Append-only audit log.

Every significant action (tool call, model invocation, artifact creation,
decision, denial, error) is recorded as one JSON object per line so that
any conclusion the system produces can be traced back to its evidence.

Credential values never appear in audit records: tool arguments are
redacted by key name and registered secret values are masked in text.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Self

from pydantic import Field

from sloperator.config.models import FrozenModel
from sloperator.security.redaction import redact_mapping, redact_text


class AuditKind(StrEnum):
    """Categories of auditable events."""

    TOOL_CALL = "tool_call"
    MODEL_INVOCATION = "model_invocation"
    ARTIFACT_CREATED = "artifact_created"
    DECISION = "decision"
    SECURITY_DENIAL = "security_denial"
    ERROR = "error"


def _utc_now() -> datetime:
    return datetime.now(UTC)


class AuditEvent(FrozenModel):
    """One auditable occurrence, traceable to a task, role, and inputs."""

    kind: AuditKind
    timestamp: datetime = Field(default_factory=_utc_now)
    project_id: str | None = None
    task_id: str | None = None
    role: str | None = None
    backend: str | None = None
    model: str | None = None
    tool: str | None = None
    tool_arguments: dict[str, Any] | None = None
    result_summary: str | None = None
    input_references: list[str] = Field(default_factory=list)
    artifacts: list[str] = Field(default_factory=list)
    message: str | None = None

    def to_json_line(self) -> str:
        """Serialize as a single JSON Lines record, with secrets masked."""
        data = self.model_dump(mode="json")
        if self.tool_arguments is not None:
            data["tool_arguments"] = redact_mapping(self.tool_arguments)
        for key in ("result_summary", "message"):
            value = data.get(key)
            if isinstance(value, str):
                data[key] = redact_text(value)
        return json.dumps(data, ensure_ascii=False) + "\n"


class AuditLog:
    """Append-only JSONL audit file."""

    def __init__(self, path: Path) -> None:
        self._path = path.expanduser().resolve(strict=False)
        self._handle: Any | None = None

    @property
    def path(self) -> Path:
        return self._path

    def open(self) -> None:
        """Open the audit file for appending, creating parent directories."""
        if self._handle is not None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self._path.open("a", encoding="utf-8")

    def record(self, event: AuditEvent) -> None:
        """Append one audit event and flush immediately."""
        if self._handle is None:
            self.open()
        assert self._handle is not None
        self._handle.write(event.to_json_line())
        self._handle.flush()

    def close(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None

    def __enter__(self) -> Self:
        self.open()
        return self

    def __exit__(self, *args: object) -> None:
        self.close()
