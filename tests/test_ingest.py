"""End-to-end ingestion: mbox adapter -> pipeline -> database, idempotency."""

from __future__ import annotations

from pathlib import Path

import pytest

from sloperator.config.models import MboxSourceConfig
from sloperator.database import Database, ProjectDao, SourceDao
from sloperator.ingest import IngestPipeline
from sloperator.sources.mbox import MboxAdapter


def _msg(msg_id: str, frm: str, subject: str, body: str) -> bytes:
    head = (
        f"From bounce@example.org Mon Oct  1 12:00:00 2026\n"
        f"Message-ID: <{msg_id}>\nFrom: {frm}\nSubject: {subject}\n"
        f"Date: Mon, 01 Oct 2026 12:00:00 +0000\n\n{body}\n"
    )
    return head.encode()


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "ingest.sqlite3")
    database.migrate()
    with database.transaction() as conn:
        ProjectDao.upsert(conn, project_id="proj", name="Proj")
    yield database
    database.close()


@pytest.fixture()
def mbox(tmp_path: Path) -> MboxSourceConfig:
    path = tmp_path / "list.mbox"
    path.write_bytes(
        _msg("m1@example.org", "Jane <jane@example.org>", "one", "first")
        + _msg("m2@example.org", "Bob <bob@example.org>", "two", "second")
    )
    return MboxSourceConfig(id="proj-mbox", project="proj", type="mbox", path=path)


def test_ingest_persists_events_and_actors(db: Database, mbox: MboxSourceConfig) -> None:
    pipeline = IngestPipeline(db)
    stats = pipeline.ingest_source(MboxAdapter(mbox), project_id="proj")
    assert stats.received == 2
    assert stats.inserted == 2
    assert stats.duplicate_ids == 0

    rows = db.connect().execute("SELECT external_id FROM events ORDER BY external_id").fetchall()
    assert [r["external_id"] for r in rows] == ["m1@example.org", "m2@example.org"]
    actors = db.connect().execute("SELECT key FROM actors ORDER BY key").fetchall()
    assert {a["key"] for a in actors} == {"jane@example.org", "bob@example.org"}


def test_reingest_is_idempotent(db: Database, mbox: MboxSourceConfig) -> None:
    pipeline = IngestPipeline(db)
    adapter = MboxAdapter(mbox)
    first = pipeline.ingest_source(adapter, project_id="proj")
    second = pipeline.ingest_source(adapter, project_id="proj")
    assert first.inserted == 2
    assert second.received == 0
    count = db.connect().execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"]
    assert count == 2


def test_cursor_rewind_does_not_duplicate(db: Database, mbox: MboxSourceConfig) -> None:
    pipeline = IngestPipeline(db)
    adapter = MboxAdapter(mbox)
    first = pipeline.ingest_source(adapter, project_id="proj")
    with db.transaction() as conn:
        SourceDao.set_cursor(conn, "proj-mbox", "0")
    rewound = pipeline.ingest_source(adapter, project_id="proj")
    assert rewound.received == 2
    assert rewound.inserted == 0
    assert rewound.duplicate_ids == 2
    count = db.connect().execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"]
    assert count == 2
    assert first.cursor is not None


def test_cross_source_content_duplicate_detected(db: Database, mbox: MboxSourceConfig) -> None:
    pipeline = IngestPipeline(db)
    pipeline.ingest_source(MboxAdapter(mbox), project_id="proj")

    second_path = Path(mbox.path).with_name("mirror.mbox")
    second_path.write_bytes(_msg("m1@example.org", "Jane <jane@example.org>", "one", "first"))
    mirror = MboxSourceConfig(id="proj-mbox-mirror", project="proj", type="mbox", path=second_path)
    stats = pipeline.ingest_source(MboxAdapter(mirror), project_id="proj")
    assert stats.inserted == 1
    assert stats.content_duplicates == 1
