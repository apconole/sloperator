"""Tests for multi-signal thread reconstruction."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from sloperator.database import Database, EventDao, ProjectDao, SourceDao
from sloperator.events import Actor, Event, EventType
from sloperator.threads.reconstruct import ThreadReconstructor, normalize_subject

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "threads.sqlite3")
    database.migrate()
    with database.transaction() as conn:
        ProjectDao.upsert(conn, project_id="proj", name="Proj")
        SourceDao.upsert(conn, source_id="src", source_type="mbox", project_id="proj")
    yield database
    database.close()


def insert_event(
    db: Database,
    external_id: str,
    *,
    subject: str | None = "topic",
    reply_to: str | None = None,
    references: list[str] | None = None,
    timestamp: datetime = T0,
    event_type: EventType = EventType.MESSAGE,
    metadata: dict[str, object] | None = None,
) -> Event:
    event = Event.create(
        source_id="src",
        source_type="mbox",
        external_id=external_id,
        timestamp=timestamp,
        author=Actor(name="A", email="a@example.org"),
        subject=subject,
        body=f"body of {external_id}",
        references=references,
        reply_to=reply_to,
        event_type=event_type,
        metadata=metadata,
    )
    with db.transaction() as conn:
        EventDao.insert(conn, event, project_id="proj")
    return event


def thread_map(db: Database) -> dict[str, tuple[int, str, float, tuple[str, ...]]]:
    """event_id -> (thread_rowid, relationship, confidence, basis)."""
    result = {}
    for thread in db.connect().execute("SELECT id FROM threads").fetchall():
        for member in (
            db.connect()
            .execute("SELECT * FROM thread_events WHERE thread_id = ?", (thread["id"],))
            .fetchall()
        ):
            result[member["event_id"].split(":", 1)[1]] = (
                thread["id"],
                member["relationship"],
                member["confidence"],
                tuple(json.loads(member["basis_json"])),
            )
    return result


def test_in_reply_to_links_with_full_confidence(db: Database) -> None:
    insert_event(db, "root@example.org", subject="fix bar")
    insert_event(
        db,
        "reply@example.org",
        subject="Re: fix bar",
        reply_to="root@example.org",
        timestamp=T0 + timedelta(hours=1),
    )
    stats = ThreadReconstructor(db).rebuild_project("proj")
    assert (stats.events, stats.threads, stats.in_reply_to) == (2, 1, 1)

    links = thread_map(db)
    assert links["root@example.org"][1] == "root"
    thread_id, _, confidence, basis = links["reply@example.org"]
    assert links["root@example.org"][0] == thread_id
    assert (confidence, basis) == (1.0, ("in_reply_to",))


def test_references_chain_links_when_reply_to_missing(db: Database) -> None:
    insert_event(db, "root@example.org")
    insert_event(
        db,
        "followup@example.org",
        references=["root@example.org"],
        timestamp=T0 + timedelta(hours=2),
    )
    stats = ThreadReconstructor(db).rebuild_project("proj")
    assert (stats.threads, stats.references) == (1, 1)
    links = thread_map(db)
    assert links["followup@example.org"][0] == links["root@example.org"][0]
    assert links["followup@example.org"][2] == 0.95


def test_subject_fallback_within_window(db: Database) -> None:
    insert_event(db, "m1@example.org", subject="[PATCH v2] foo: fix bar")
    insert_event(
        db,
        "m2@example.org",
        subject="Re: [PATCH v2] foo: fix bar",
        timestamp=T0 + timedelta(days=3),
    )
    stats = ThreadReconstructor(db).rebuild_project("proj")
    assert (stats.threads, stats.subject_links) == (1, 1)
    links = thread_map(db)
    assert links["m2@example.org"][2] == 0.6
    assert "subject_similarity" in links["m2@example.org"][3]


def test_subject_outside_window_starts_new_thread(db: Database) -> None:
    insert_event(db, "m1@example.org", subject="same topic")
    insert_event(
        db,
        "m2@example.org",
        subject="same topic",
        timestamp=T0 + timedelta(days=30),
    )
    stats = ThreadReconstructor(db).rebuild_project("proj")
    assert stats.threads == 2
    assert stats.subject_links == 0


def test_github_comment_threads_under_issue(db: Database) -> None:
    insert_event(
        db,
        "issue-5",
        subject="bug report",
        event_type=EventType.ISSUE,
    )
    insert_event(
        db,
        "comment-9",
        subject=None,
        timestamp=T0 + timedelta(hours=3),
        event_type=EventType.ISSUE_COMMENT,
        metadata={"issue_url": "https://github.com/o/r/issues/5"},
    )
    stats = ThreadReconstructor(db).rebuild_project("proj")
    assert (stats.threads, stats.issue_references) == (1, 1)


def test_rebuild_is_idempotent(db: Database) -> None:
    insert_event(db, "root@example.org", subject="fix bar")
    insert_event(db, "reply@example.org", reply_to="root@example.org", subject="Re: fix bar")
    recon = ThreadReconstructor(db)
    first = recon.rebuild_project("proj")
    second = recon.rebuild_project("proj")
    assert first == second
    count = db.connect().execute("SELECT COUNT(*) AS n FROM threads").fetchone()["n"]
    assert count == 1


def test_normalize_subject() -> None:
    assert normalize_subject("Re: [PATCH v2] foo: fix bar") == "foo: fix bar"
    assert normalize_subject("RE: Fwd: [LIST] Thing") == "thing"
    assert normalize_subject(None) == ""
