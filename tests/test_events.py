"""Tests for the canonical event model."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from sloperator.events import (
    Actor,
    Event,
    EventType,
    SourceEvent,
    SourceHealth,
    SourceHealthStatus,
    compute_content_hash,
)

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


def make_event(**overrides: object) -> Event:
    params: dict[str, object] = {
        "source_id": "project-mbox",
        "source_type": "mbox",
        "external_id": "<abc@example.org>",
        "timestamp": NOW,
        "author": Actor(name="Jane", email="Jane@Example.ORG"),
        "subject": "[PATCH] foo: fix bar",
        "body": "Fix the bar case.\r\n\r\nSigned-off-by: Jane\r\n",
    }
    params.update(overrides)
    return Event.create(**params)  # type: ignore[arg-type]


def test_event_type_values_match_design() -> None:
    expected = {
        "message",
        "review",
        "review_comment",
        "review_request",
        "commit",
        "patch",
        "patch_series",
        "ci_result",
        "issue",
        "issue_comment",
        "pr",
        "pr_update",
        "decision",
        "unknown",
    }
    assert {member.value for member in EventType} == expected


def test_event_id_is_deterministic() -> None:
    first = make_event()
    second = make_event(body="different body")
    assert first.id == "project-mbox:<abc@example.org>"
    assert first.id == second.id


def test_content_hash_stable_across_line_endings() -> None:
    unix = make_event(body="Fix the bar case.\n\nSigned-off-by: Jane\n")
    dos = make_event(body="Fix the bar case.\r\n\r\nSigned-off-by: Jane\r\n")
    assert unix.content_hash == dos.content_hash


def test_content_hash_changes_with_content() -> None:
    base = make_event()
    other = make_event(body="totally different")
    assert base.content_hash != other.content_hash
    author_change = make_event(author=Actor(email="other@example.org"))
    assert base.content_hash != author_change.content_hash


def test_content_hash_matches_helper() -> None:
    event = make_event()
    expected = compute_content_hash(
        author_email="Jane@Example.ORG",
        subject="[PATCH] foo: fix bar",
        body="Fix the bar case.\r\n\r\nSigned-off-by: Jane\r\n",
    )
    assert event.content_hash == expected


def test_naive_timestamp_rejected() -> None:
    with pytest.raises(ValidationError, match="timezone-aware"):
        make_event(timestamp=datetime(2026, 10, 1, 12, 0))


def test_non_utc_aware_timestamp_accepted() -> None:
    tokyo = datetime(2026, 10, 1, 21, 0, tzinfo=timezone(timedelta(hours=9)))
    event = make_event(timestamp=tokyo)
    assert event.timestamp.utcoffset() == timedelta(hours=9)


def test_actor_key_normalization() -> None:
    assert Actor(name="Jane", email="Jane@Example.org").key == "jane@example.org"
    assert Actor(name="  Bot Name ").key == "bot name"
    assert Actor().key == "unknown"


def test_event_is_frozen() -> None:
    event = make_event()
    with pytest.raises(ValidationError):
        event.subject = "tampered"  # type: ignore[misc]


def test_source_event_defaults() -> None:
    raw = SourceEvent(
        source_id="project-github",
        source_type="github",
        external_id="pr-42",
        timestamp=NOW,
    )
    assert raw.event_type is EventType.MESSAGE
    assert raw.references == []
    assert raw.author is None


def test_source_health_requires_aware_checked_at() -> None:
    health = SourceHealth(
        source_id="project-nntp",
        status=SourceHealthStatus.OK,
        checked_at=NOW,
    )
    assert health.status is SourceHealthStatus.OK
    with pytest.raises(ValidationError, match="timezone-aware"):
        SourceHealth(
            source_id="project-nntp",
            status=SourceHealthStatus.DOWN,
            checked_at=datetime(2026, 10, 1),
        )
