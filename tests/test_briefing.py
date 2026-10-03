"""Tests for daily briefing generation and urgency notifications."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from sloperator.artifacts.store import ArtifactStore, ArtifactType
from sloperator.config.loading import load_config
from sloperator.config.models import NotificationsConfig, Urgency
from sloperator.database import Database, EventDao, ProjectDao, SourceDao
from sloperator.events import Actor, Event
from sloperator.notify.briefing import DailyBriefing

FIXTURE = Path(__file__).parent / "fixtures" / "valid_config.yaml"
T0 = datetime.now(UTC)


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "brief.sqlite3")
    database.migrate()
    with database.transaction() as conn:
        ProjectDao.upsert(conn, project_id="example", name="Example")
        SourceDao.upsert(conn, source_id="src", source_type="mbox", project_id="example")
    yield database
    database.close()


@pytest.fixture()
def config(tmp_path: Path):  # type: ignore[no-untyped-def]
    base = load_config(FIXTURE)
    return base.model_copy(
        update={
            "notifications": NotificationsConfig(
                enabled=True, directory=tmp_path / "notes", min_urgency=Urgency.INFO
            )
        }
    )


def seed(db: Database, *, with_request: bool = True) -> None:
    event = Event.create(
        source_id="src",
        source_type="mbox",
        external_id="e1",
        timestamp=T0 - timedelta(hours=2),
        author=Actor(name="A", email="a@example.org"),
        subject="[PATCH 1/2] feature work",
        body="please review this before the freeze",
    )
    with db.transaction() as conn:
        EventDao.insert(conn, event, project_id="example")
        conn.execute(
            """INSERT INTO action_items (project_id, description, status, confidence,
                   explicitness, source_event_ids_json, created_at, updated_at)
               VALUES ('example', ?, 'open', 0.75, 'EXPLICIT_REQUEST', ?, ?, ?)""",
            (
                "please review this before the freeze",
                json.dumps([event.id]),
                str(T0 - timedelta(hours=1)),
                str(T0 - timedelta(hours=1)),
            ),
        )
        if not with_request:
            conn.execute("DELETE FROM action_items")
        conn.execute(
            """INSERT INTO patch_series (project_id, title, latest_version,
                   created_at, updated_at)
               VALUES ('example', 'feature work', 2, ?, ?)""",
            (str(T0 - timedelta(hours=3)), str(T0 - timedelta(hours=1))),
        )


def make_briefing(db: Database, config, tmp_path: Path) -> DailyBriefing:  # type: ignore[no-untyped-def]
    store = ArtifactStore(db, tmp_path / "artifacts")
    return DailyBriefing(db, store, config)


def test_briefing_artifact_is_factual_and_lists_items(
    db: Database,
    config,
    tmp_path: Path,  # type: ignore[no-untyped-def]
) -> None:
    seed(db)
    result = make_briefing(db, config, tmp_path).generate("example")

    assert result.new_events == 1
    assert result.open_action_items == 1
    assert result.outstanding_requests == 1
    assert result.series_updates == 1
    content = result.artifact.read()
    assert "please review this before the freeze" in content
    assert result.artifact.artifact_type is ArtifactType.DAILY_BRIEFING

    row = (
        db.connect()
        .execute("SELECT factual FROM artifacts WHERE id = ?", (result.artifact.id,))
        .fetchone()
    )
    assert row["factual"] == 1


def test_notifications_urgency_and_file_copy(
    db: Database,
    config,
    tmp_path: Path,  # type: ignore[no-untyped-def]
) -> None:
    seed(db)
    result = make_briefing(db, config, tmp_path).generate("example")
    assert result.notifications == 2

    rows = db.connect().execute("SELECT urgency, title FROM notifications ORDER BY id").fetchall()
    urgencies = [r["urgency"] for r in rows]
    assert urgencies == ["INFO", "HIGH_PRIORITY"]

    notes_dir = tmp_path / "notes"
    copies = list(notes_dir.iterdir())
    assert len(copies) == 2
    high = next(p for p in copies if "high_priority" in p.name)
    assert "awaiting a response" in high.read_text()


def test_min_urgency_filters_and_disabled(
    db: Database,
    config,
    tmp_path: Path,  # type: ignore[no-untyped-def]
) -> None:
    seed(db)
    strict = config.model_copy(
        update={
            "notifications": NotificationsConfig(enabled=True, min_urgency=Urgency.HIGH_PRIORITY)
        }
    )
    store = ArtifactStore(db, tmp_path / "artifacts")
    result = DailyBriefing(db, store, strict).generate("example")
    assert result.notifications == 1

    quiet = config.model_copy(update={"notifications": NotificationsConfig(enabled=False)})
    result = DailyBriefing(db, ArtifactStore(db, tmp_path / "artifacts"), quiet).generate("example")
    assert result.notifications == 0


def test_list_notifications_filters(
    db: Database,
    config,
    tmp_path: Path,  # type: ignore[no-untyped-def]
) -> None:
    seed(db)
    briefing = make_briefing(db, config, tmp_path)
    briefing.generate("example")

    everything = briefing.list_notifications()
    assert len(everything) == 2
    urgent = briefing.list_notifications(min_urgency=Urgency.HIGH_PRIORITY)
    assert [r["urgency"] for r in urgent] == ["HIGH_PRIORITY"]
    undelivered = briefing.list_notifications(undelivered_only=True)
    assert len(undelivered) == 2
