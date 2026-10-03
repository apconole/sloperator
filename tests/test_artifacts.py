"""Tests for artifact storage, provenance persistence, and path safety."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sloperator.artifacts.store import (
    ArtifactProvenance,
    ArtifactStore,
    ArtifactType,
)
from sloperator.database import Database


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "art.sqlite3")
    database.migrate()
    yield database
    database.close()


@pytest.fixture()
def store(tmp_path: Path, db: Database) -> ArtifactStore:
    return ArtifactStore(db, tmp_path / "artifacts")


PROV = ArtifactProvenance(
    backend="flash",
    model="qwen3.8-flash",
    source_event_ids=("src:a@x", "src:b@x"),
    factual=True,
    confidence=0.8,
)


def test_save_writes_file_and_metadata(db: Database, store: ArtifactStore) -> None:
    record = store.save(
        project_id="example",
        artifact_type=ArtifactType.SUMMARY,
        title="Thread summary: memory leak in net/core",
        content="# Summary\n\nBody text.",
        provenance=PROV,
    )
    assert record.content_path.exists()
    assert record.read() == "# Summary\n\nBody text."

    row = db.connect().execute("SELECT * FROM artifacts WHERE id = ?", (record.id,)).fetchone()
    assert row is not None
    assert row["artifact_type"] == "SUMMARY"
    assert row["status"] == "ready_for_review"
    assert row["factual"] == 1
    assert row["confidence"] == 0.8
    assert row["backend"] == "flash"
    assert row["model"] == "qwen3.8-flash"
    assert json.loads(row["source_event_ids_json"]) == ["src:a@x", "src:b@x"]
    assert row["content_path"] == str(record.content_path)


def test_get_and_list(db: Database, store: ArtifactStore) -> None:
    first = store.save(
        project_id="example",
        artifact_type=ArtifactType.REPORT,
        title="First report",
        content="one",
        provenance=PROV,
    )
    second = store.save(
        project_id="other",
        artifact_type=ArtifactType.INVESTIGATION,
        title="Other investigation",
        content="two",
        provenance=ArtifactProvenance(factual=False),
    )
    fetched = store.get(first.id)
    assert fetched is not None and fetched.read() == "one"

    listed = store.list_for_project("example")
    assert [r.id for r in listed] == [first.id]
    assert store.get(second.id) is not None
    assert store.get(9999) is None


def test_paths_stay_under_root(db: Database, store: ArtifactStore) -> None:
    record = store.save(
        project_id="../../etc",
        artifact_type=ArtifactType.SUMMARY,
        title="Weird / Title !!",
        content="x",
        provenance=PROV,
    )
    assert record.content_path.resolve().is_relative_to(store._root)
    assert ".." not in str(record.content_path.relative_to(store._root))


def test_tampered_external_path_rejected(
    db: Database, store: ArtifactStore, tmp_path: Path
) -> None:
    outside = tmp_path / "outside.md"
    outside.write_text("evil")
    with db.transaction() as conn:
        conn.execute(
            """INSERT INTO artifacts (project_id, artifact_type, title,
                   source_event_ids_json, content_path, created_at)
               VALUES ('p', 'SUMMARY', 't', '[]', ?, '2026-10-01 00:00:00')""",
            (str(outside),),
        )
        artifact_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
    with pytest.raises(ValueError, match="escapes artifacts root"):
        store.get(artifact_id)
