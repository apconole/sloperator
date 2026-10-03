"""Tests for patch subject parsing and series tracking."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from sloperator.database import Database, EventDao, ProjectDao, SourceDao
from sloperator.events import Actor, Event
from sloperator.patches.tracker import PatchSeriesTracker, parse_patch_subject

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


def test_parse_patch_subject_variants() -> None:
    plain = parse_patch_subject("[PATCH] add thing")
    assert plain is not None and (plain.version, plain.position, plain.total) == (1, None, None)
    assert plain.base_subject == "add thing"

    ranged = parse_patch_subject("[PATCH 3/5] add thing")
    assert ranged is not None and (ranged.version, ranged.position, ranged.total) == (1, 3, 5)

    v2 = parse_patch_subject("[PATCH v2 1/4] add thing")
    assert v2 is not None and (v2.version, v2.position, v2.total) == (2, 1, 4)

    swapped = parse_patch_subject("[PATCH 01/12 v3] thing")
    assert swapped is not None and (swapped.version, swapped.position) == (3, 1)

    cover = parse_patch_subject("[PATCH RFC 0/2] big change")
    assert cover is not None and cover.is_cover_letter and cover.version == 1

    padded = parse_patch_subject("[PATCH 02/12] thing")
    assert padded is not None and (padded.position, padded.total) == (2, 12)


def test_non_patch_subjects_are_ignored() -> None:
    assert parse_patch_subject("hello world") is None
    assert parse_patch_subject("[BUGFIX] not a patch") is None
    assert parse_patch_subject("[LIST PATCH] discussion") is None
    assert parse_patch_subject(None) is None


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "patches.sqlite3")
    database.migrate()
    with database.transaction() as conn:
        ProjectDao.upsert(conn, project_id="proj", name="Proj")
        SourceDao.upsert(conn, source_id="src", source_type="mbox", project_id="proj")
    yield database
    database.close()


def insert(
    db: Database,
    external_id: str,
    subject: str,
    *,
    when: datetime = T0,
    references: list[str] | None = None,
) -> None:
    event = Event.create(
        source_id="src",
        source_type="mbox",
        external_id=external_id,
        timestamp=when,
        author=Actor(name="A", email="a@example.org"),
        subject=subject,
        body=f"body {external_id}",
        references=references,
    )
    with db.transaction() as conn:
        EventDao.insert(conn, event, project_id="proj")


def test_series_grouping_and_version_bump(db: Database) -> None:
    insert(db, "c1", "[PATCH 0/2] add thing", when=T0)
    insert(db, "p1", "[PATCH 1/2] add thing: part one", when=T0, references=["c1"])
    insert(
        db,
        "p2",
        "[PATCH 2/2] add thing: part two",
        when=T0 + timedelta(minutes=1),
        references=["c1"],
    )
    insert(db, "c3", "[PATCH v2 0/2] add thing", when=T0 + timedelta(days=2))

    stats = PatchSeriesTracker(db).rebuild_project("proj")
    assert (stats.series, stats.patches, stats.version_bumps) == (1, 4, 1)

    row = db.connect().execute("SELECT * FROM patch_series").fetchone()
    assert row["latest_version"] == 2
    versions = {
        r["version"] for r in db.connect().execute("SELECT version FROM patches").fetchall()
    }
    assert versions == {1, 2}


def test_distinct_subjects_are_distinct_series(db: Database) -> None:
    insert(db, "a", "[PATCH 1/1] fix alpha")
    insert(db, "b", "[PATCH 1/1] fix beta")
    stats = PatchSeriesTracker(db).rebuild_project("proj")
    assert stats.series == 2


def test_non_patch_events_do_not_create_series(db: Database) -> None:
    insert(db, "m", "just a question about the build")
    stats = PatchSeriesTracker(db).rebuild_project("proj")
    assert (stats.series, stats.patches) == (0, 0)
    assert stats.events_scanned == 1


def test_rebuild_is_idempotent(db: Database) -> None:
    insert(db, "p1", "[PATCH 1/1] add thing")
    tracker = PatchSeriesTracker(db)
    first = tracker.rebuild_project("proj")
    second = tracker.rebuild_project("proj")
    assert first == second
    count = db.connect().execute("SELECT COUNT(*) AS n FROM patches").fetchone()["n"]
    assert count == 1
