"""Tests for action item extraction, provenance, and persistence."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from sloperator.analysis.actions import (
    ActionItemCandidate,
    ActionItemWriter,
    HeuristicActionExtractor,
)
from sloperator.database import Database, EventDao, ProjectDao, SourceDao
from sloperator.events import Actor, Event
from sloperator.threads.reconstruct import ThreadReconstructor

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "actions.sqlite3")
    database.migrate()
    with database.transaction() as conn:
        ProjectDao.upsert(conn, project_id="proj", name="Proj")
        SourceDao.upsert(conn, source_id="src", source_type="mbox", project_id="proj")
    yield database
    database.close()


def seed_thread(db: Database) -> int:
    messages = [
        (
            "req@example.org",
            None,
            "Please review this series before Friday so we can merge it.",
        ),
        ("cmt@example.org", "req@example.org", "I will resend v3 with the fixups."),
        ("noise@example.org", "req@example.org", "Just discussing the general architecture here."),
    ]
    for external_id, reply_to, body in messages:
        event = Event.create(
            source_id="src",
            source_type="mbox",
            external_id=external_id,
            timestamp=T0,
            author=Actor(name="A", email="a@example.org"),
            subject="topic",
            body=body,
            reply_to=reply_to,
        )
        with db.transaction() as conn:
            EventDao.insert(conn, event, project_id="proj")
    ThreadReconstructor(db).rebuild_project("proj")
    row = db.connect().execute("SELECT id FROM threads LIMIT 1").fetchone()
    assert row is not None
    return int(row["id"])


def test_heuristic_classification() -> None:
    events = [
        {"id": "e1", "body": "Could you take a look at the kselftests?"},
        {"id": "e2", "body": "I'll send a fixed version tomorrow."},
        {"id": "e3", "body": "You should run the reproducer with KASAN enabled."},
        {"id": "e4", "body": "Nothing actionable, just context about history."},
    ]
    candidates = HeuristicActionExtractor().extract(events)
    by_source = {c.source_event_ids[0]: c for c in candidates}
    assert by_source["e1"].explicitness == "EXPLICIT_REQUEST"
    assert by_source["e2"].explicitness == "EXPLICIT_COMMITMENT"
    assert by_source["e3"].explicitness == "SUGGESTION"
    assert "e4" not in by_source


def test_writer_persists_with_provenance_and_dedupes(db: Database) -> None:
    thread_id = seed_thread(db)
    writer = ActionItemWriter(db)

    stats = writer.process_thread("proj", thread_id)
    assert stats.candidates == 2
    assert stats.inserted == 2

    rows = (
        db.connect()
        .execute("SELECT description, explicitness, source_event_ids_json FROM action_items")
        .fetchall()
    )
    assert len(rows) == 2
    for row in rows:
        sources = json.loads(row["source_event_ids_json"])
        assert sources and all(s.startswith("src:") for s in sources)

    second = writer.process_thread("proj", thread_id)
    assert second.inserted == 0
    assert second.skipped_duplicates == 2


def test_custom_extractor_injection(db: Database) -> None:
    thread_id = seed_thread(db)

    class Static:
        def extract(self, events: list[dict[str, object]]) -> list[ActionItemCandidate]:
            return [
                ActionItemCandidate(
                    description="Backport the fix to stable",
                    explicitness="INFERRED_ACTION",
                    confidence=0.55,
                    source_event_ids=[str(events[0]["id"])],
                )
            ]

    stats = ActionItemWriter(db, extractor=Static()).process_thread("proj", thread_id)
    assert stats.inserted == 1
    row = db.connect().execute("SELECT explicitness, confidence FROM action_items").fetchone()
    assert row["explicitness"] == "INFERRED_ACTION"
    assert row["confidence"] == 0.55
