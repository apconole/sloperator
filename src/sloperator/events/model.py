"""Canonical event model shared by all source adapters and consumers.

All source-specific information (mail, NNTP, HTML archives, GitHub) is
normalized into Event objects before persistence. Events are immutable,
carry a deterministic id derived from (source_id, external_id), and a
content hash used for deduplication across transports and re-polls.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import Field, field_validator

from sloperator.config.models import FrozenModel


class EventType(StrEnum):
    """Canonical event classifications."""

    MESSAGE = "message"
    REVIEW = "review"
    REVIEW_COMMENT = "review_comment"
    REVIEW_REQUEST = "review_request"
    COMMIT = "commit"
    PATCH = "patch"
    PATCH_SERIES = "patch_series"
    CI_RESULT = "ci_result"
    ISSUE = "issue"
    ISSUE_COMMENT = "issue_comment"
    PR = "pr"
    PR_UPDATE = "pr_update"
    DECISION = "decision"
    UNKNOWN = "unknown"


def ensure_aware(value: datetime) -> datetime:
    """Reject naive datetimes; all persisted timestamps must be timezone-aware."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware (convert to UTC before persisting)")
    return value


def _canonical_text(value: str | None) -> str:
    """Canonicalize text for content hashing only.

    Unifies line endings and strips trailing whitespace so that the same
    message transported over different media (mbox, NNTP, HTML archive)
    hashes identically. Raw originals are preserved separately; this
    function is never used for display or patch reconstruction.
    """
    if value is None:
        return ""
    text = value.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in text.split("\n")]
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


def compute_content_hash(
    *,
    author_email: str | None,
    subject: str | None,
    body: str | None,
) -> str:
    """Return a stable sha256 hex digest of the normalized event content."""
    parts = [
        (author_email or "").strip().lower(),
        _canonical_text(subject),
        _canonical_text(body),
    ]
    joined = "\x1e".join(parts).encode("utf-8")
    return hashlib.sha256(joined).hexdigest()


class Actor(FrozenModel):
    """A person or system identity that produces events."""

    name: str | None = None
    email: str | None = None

    @property
    def key(self) -> str:
        """Stable lowercase identity used to match actors across sources."""
        if self.email:
            return self.email.strip().lower()
        if self.name:
            return self.name.strip().lower()
        return "unknown"


class Event(FrozenModel):
    """One normalized, persisted occurrence from an external source."""

    id: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    source_type: str = Field(min_length=1)
    external_id: str = Field(min_length=1)
    timestamp: datetime
    author: Actor
    event_type: EventType = EventType.MESSAGE
    subject: str | None = None
    body: str | None = None
    references: list[str] = Field(default_factory=list)
    reply_to: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    content_hash: str = Field(min_length=1)

    @field_validator("timestamp")
    @classmethod
    def _check_timestamp(cls, value: datetime) -> datetime:
        return ensure_aware(value)

    @staticmethod
    def make_id(source_id: str, external_id: str) -> str:
        """Deterministic event id so re-ingestion never creates duplicates."""
        return f"{source_id}:{external_id}"

    @classmethod
    def create(
        cls,
        *,
        source_id: str,
        source_type: str,
        external_id: str,
        timestamp: datetime,
        author: Actor,
        event_type: EventType = EventType.MESSAGE,
        subject: str | None = None,
        body: str | None = None,
        references: list[str] | None = None,
        reply_to: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Event:
        """Build an event with deterministic id and computed content hash."""
        return cls(
            id=cls.make_id(source_id, external_id),
            source_id=source_id,
            source_type=source_type,
            external_id=external_id,
            timestamp=timestamp,
            author=author,
            event_type=event_type,
            subject=subject,
            body=body,
            references=list(references or []),
            reply_to=reply_to,
            metadata=dict(metadata or {}),
            content_hash=compute_content_hash(
                author_email=author.email,
                subject=subject,
                body=body,
            ),
        )


class SourceEvent(FrozenModel):
    """Raw event handed to the normalizer by a source adapter.

    Adapters must supply stable external ids (Message-ID, PR number plus
    revision, article identifiers) so repeated polling is idempotent.
    """

    source_id: str = Field(min_length=1)
    source_type: str = Field(min_length=1)
    external_id: str = Field(min_length=1)
    event_type: EventType = EventType.MESSAGE
    timestamp: datetime
    author: Actor | None = None
    subject: str | None = None
    body: str | None = None
    references: list[str] = Field(default_factory=list)
    reply_to: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("timestamp")
    @classmethod
    def _check_timestamp(cls, value: datetime) -> datetime:
        return ensure_aware(value)


class SourceHealthStatus(StrEnum):
    """Liveness classification for a configured source."""

    OK = "ok"
    DEGRADED = "degraded"
    DOWN = "down"


class SourceHealth(FrozenModel):
    """Result of a source health probe."""

    source_id: str = Field(min_length=1)
    status: SourceHealthStatus
    checked_at: datetime
    detail: str | None = None
    last_success: datetime | None = None

    @field_validator("checked_at", "last_success")
    @classmethod
    def _check_timestamp(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return ensure_aware(value)
