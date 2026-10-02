"""Tests for the SQLite database layer: migrations, constraints, DAOs."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from sloperator.database import (
    ActorDao,
    Database,
    EventDao,
    ProjectDao,
    SourceDao,
    current_version,
    from_db_time,
    to_db_time,
)
from sloperator.events import Actor, Event

EXPECTED_TABLES = {
    "repositories",
    "projects",
    "sources",
    "actors",
    "events",
    "threads",
    "thread_events",
    "patch_series",
    "patches",
    "reviews",
    "review_requests",
    "decisions",
    "questions",
    "action_items",
    "commits",
    "ci_results",
    "tasks",
    "agent_runs",
    "tool_calls",
    "artifacts",
    "notifications",
    "schema_migrations",
}

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


@pytest.fixture()
def db(tmp_path: Path) -> Iterator[Database]:
    database = Database(tmp_path / "sloperator.sqlite3")
    yield database
    database.close()


def make_event(external_id: str = "<msg-1@example.org>") -> Event:
    return Event.create(
        source_id="project-mbox",
        source_type="mbox",
        external_id=external_id,
        timestamp=NOW,
        author=Actor(name="Jane", email="jane@example.org"),
        subject="[PATCH] foo: fix bar",
        body="Fix the bar case.",
    )


def seed_source(conn: sqlite3.Connection) -> None:
    ProjectDao.upsert(conn, project_id="example", name="Example")
    SourceDao.upsert(conn, source_id="project-mbox", source_type="mbox", project_id="example")


def test_migrate_creates_full_schema(db: Database) -> None:
    applied = db.migrate()
    assert applied == (1,)
    tables = {
        row["name"]
        for row in db.connect().execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert tables >= EXPECTED_TABLES
    assert current_version(db.connect()) == 1


def test_migrate_is_idempotent(db: Database) -> None:
    db.migrate()
    assert db.migrate() == ()
    assert current_version(db.connect()) == 1


def test_foreign_keys_are_enforced(db: Database) -> None:
    db.migrate()
    with pytest.raises(sqlite3.IntegrityError):
        db.connect().execute(
            "INSERT INTO sources (id, type, project_id, created_at) "
            "VALUES ('s', 'mbox', 'nope', '2026-10-01T00:00:00+00:00')"
        )


def test_transaction_rolls_back_on_error(db: Database) -> None:
    db.migrate()
    with pytest.raises(RuntimeError), db.transaction() as conn:
        ProjectDao.upsert(conn, project_id="example", name="Example")
        raise RuntimeError("boom")
    row = db.connect().execute("SELECT COUNT(*) AS n FROM projects").fetchone()
    assert row["n"] == 0


def test_actor_get_or_create_is_stable(db: Database) -> None:
    db.migrate()
    conn = db.connect()
    first = ActorDao.get_or_create(conn, name="Jane", email="Jane@Example.ORG")
    second = ActorDao.get_or_create(conn, name="Jane R.", email="jane@example.org")
    other = ActorDao.get_or_create(conn, email="other@example.org")
    assert first == second
    assert other != first


def test_source_cursor_roundtrip(db: Database) -> None:
    db.migrate()
    with db.transaction() as conn:
        seed_source(conn)
        assert SourceDao.get_cursor(conn, "project-mbox") is None
        SourceDao.set_cursor(conn, "project-mbox", "article-4212")
    assert SourceDao.get_cursor(db.connect(), "project-mbox") == "article-4212"
    with pytest.raises(LookupError):
        SourceDao.get_cursor(db.connect(), "missing-source")


def test_event_insert_is_idempotent(db: Database) -> None:
    db.migrate()
    event = make_event()
    with db.transaction() as conn:
        seed_source(conn)
        actor_id = ActorDao.get_or_create(conn, name="Jane", email="jane@example.org")
        assert EventDao.insert(conn, event, project_id="example", actor_id=actor_id)
    with db.transaction() as conn:
        assert not EventDao.insert(conn, event, project_id="example")
    row = db.connect().execute("SELECT COUNT(*) AS n FROM events").fetchone()
    assert row["n"] == 1


def test_event_roundtrip_fields(db: Database) -> None:
    db.migrate()
    event = make_event()
    with db.transaction() as conn:
        seed_source(conn)
        actor_id = ActorDao.get_or_create(conn, name="Jane", email="jane@example.org")
        EventDao.insert(conn, event, project_id="example", actor_id=actor_id)
    row = EventDao.get(db.connect(), event.id)
    assert row is not None
    assert row["content_hash"] == event.content_hash
    assert from_db_time(row["timestamp"]) == event.timestamp
    assert row["author_key"] == "jane@example.org"


def test_content_hash_finds_cross_source_duplicates(db: Database) -> None:
    db.migrate()
    event = make_event()
    with db.transaction() as conn:
        seed_source(conn)
        EventDao.insert(conn, event, project_id="example")
    matches = EventDao.find_by_content_hash(db.connect(), "example", event.content_hash)
    assert len(matches) == 1
    assert matches[0]["id"] == event.id


def test_timestamp_serialization_normalizes_to_utc() -> None:
    tokyo = NOW.astimezone(timezone(timedelta(hours=9)))
    stored = to_db_time(tokyo)
    assert stored.endswith("+00:00")
    assert from_db_time(stored) == NOW
