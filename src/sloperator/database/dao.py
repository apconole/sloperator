"""Data access objects over the canonical schema.

DAO methods execute on the connection they are given and never start
transactions themselves; callers that need multi-statement atomicity wrap
several DAO calls in Database.transaction(). Single statements are atomic
on their own because connections run in autocommit mode.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from typing import cast

from sloperator.events.model import Event


def utc_now() -> datetime:
    return datetime.now(UTC)


def to_db_time(value: datetime) -> str:
    """Serialize a timezone-aware datetime as ISO-8601 UTC text."""
    return value.astimezone(UTC).isoformat()


def from_db_time(value: str) -> datetime:
    """Parse ISO-8601 text back into an aware datetime (assumes UTC if naive)."""
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


class RepositoryDao:
    @staticmethod
    def upsert(
        conn: sqlite3.Connection,
        *,
        repository_id: str,
        path: str,
        kind: str = "working_tree",
        default_branch: str = "main",
    ) -> None:
        conn.execute(
            """INSERT INTO repositories (id, path, kind, default_branch)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                   path = excluded.path,
                   kind = excluded.kind,
                   default_branch = excluded.default_branch""",
            (repository_id, path, kind, default_branch),
        )


class ProjectDao:
    @staticmethod
    def upsert(
        conn: sqlite3.Connection,
        *,
        project_id: str,
        name: str,
        description: str | None = None,
        repository_id: str | None = None,
    ) -> None:
        conn.execute(
            """INSERT INTO projects (id, name, description, repository_id, created_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                   name = excluded.name,
                   description = excluded.description,
                   repository_id = excluded.repository_id""",
            (project_id, name, description, repository_id, to_db_time(utc_now())),
        )


class SourceDao:
    @staticmethod
    def upsert(
        conn: sqlite3.Connection,
        *,
        source_id: str,
        source_type: str,
        project_id: str,
    ) -> None:
        conn.execute(
            """INSERT INTO sources (id, type, project_id, created_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                   type = excluded.type,
                   project_id = excluded.project_id""",
            (source_id, source_type, project_id, to_db_time(utc_now())),
        )

    @staticmethod
    def get_cursor(conn: sqlite3.Connection, source_id: str) -> str | None:
        row = conn.execute("SELECT cursor FROM sources WHERE id = ?", (source_id,)).fetchone()
        if row is None:
            raise LookupError(f"unknown source {source_id!r}")
        value = row["cursor"]
        return str(value) if value is not None else None

    @staticmethod
    def set_cursor(conn: sqlite3.Connection, source_id: str, cursor: str | None) -> None:
        conn.execute("UPDATE sources SET cursor = ? WHERE id = ?", (cursor, source_id))

    @staticmethod
    def mark_success(conn: sqlite3.Connection, source_id: str, at: datetime) -> None:
        conn.execute(
            "UPDATE sources SET last_success_at = ? WHERE id = ?",
            (to_db_time(at), source_id),
        )


class ActorDao:
    @staticmethod
    def get_or_create(
        conn: sqlite3.Connection,
        *,
        name: str | None = None,
        email: str | None = None,
        is_principal: bool = False,
    ) -> int:
        key = (email or "").strip().lower() or (name or "").strip().lower() or "unknown"
        row = conn.execute("SELECT id FROM actors WHERE key = ?", (key,)).fetchone()
        if row is not None:
            return int(row["id"])
        cur = conn.execute(
            """INSERT INTO actors (key, name, email, is_principal, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (key, name, email, int(is_principal), to_db_time(utc_now())),
        )
        row_id = cur.lastrowid
        assert row_id is not None
        return int(row_id)


class EventDao:
    @staticmethod
    def insert(
        conn: sqlite3.Connection,
        event: Event,
        *,
        project_id: str,
        actor_id: int | None = None,
        raw_stored: bool = False,
    ) -> bool:
        """Insert the event; return False if (source_id, external_id) existed.

        Re-ingesting the same message is a no-op, which keeps polling
        idempotent across crashes and partial runs.
        """
        cur = conn.execute(
            """INSERT OR IGNORE INTO events (
                   id, source_id, source_type, external_id, project_id,
                   event_type, timestamp, actor_id, author_key, subject,
                   body, reply_to, content_hash, metadata_json, raw_stored,
                   ingested_at, references_json
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                event.id,
                event.source_id,
                event.source_type,
                event.external_id,
                project_id,
                event.event_type.value,
                to_db_time(event.timestamp),
                actor_id,
                event.author.key,
                event.subject,
                event.body,
                event.reply_to,
                event.content_hash,
                json.dumps(event.metadata),
                int(raw_stored),
                to_db_time(utc_now()),
                json.dumps(event.references),
            ),
        )
        return cur.rowcount > 0

    @staticmethod
    def list_for_project(conn: sqlite3.Connection, project_id: str) -> list[sqlite3.Row]:
        """All project events in causal-ish (timestamp, id) order."""
        return list(
            conn.execute(
                "SELECT * FROM events WHERE project_id = ? ORDER BY timestamp, id",
                (project_id,),
            ).fetchall()
        )

    @staticmethod
    def get(conn: sqlite3.Connection, event_id: str) -> sqlite3.Row | None:
        row = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
        return cast("sqlite3.Row | None", row)

    @staticmethod
    def find_by_content_hash(
        conn: sqlite3.Connection, project_id: str, content_hash: str
    ) -> list[sqlite3.Row]:
        """Cross-source duplicate lookup (§8: same message via mbox+web)."""
        return list(
            conn.execute(
                "SELECT * FROM events WHERE project_id = ? AND content_hash = ?",
                (project_id, content_hash),
            ).fetchall()
        )


class ThreadDao:
    """Thread graph writes; reconstruction rebuilds per project atomically."""

    @staticmethod
    def clear_project(conn: sqlite3.Connection, project_id: str) -> None:
        conn.execute(
            "DELETE FROM thread_events WHERE thread_id IN "
            "(SELECT id FROM threads WHERE project_id = ?)",
            (project_id,),
        )
        conn.execute("DELETE FROM threads WHERE project_id = ?", (project_id,))

    @staticmethod
    def insert(
        conn: sqlite3.Connection,
        *,
        project_id: str,
        root_event_id: str | None,
        subject: str | None,
        started_at: datetime,
        last_activity_at: datetime,
        status: str = "active",
    ) -> int:
        cur = conn.execute(
            """INSERT INTO threads (
                   project_id, root_event_id, subject, status,
                   started_at, last_activity_at
               ) VALUES (?, ?, ?, ?, ?, ?)""",
            (
                project_id,
                root_event_id,
                subject,
                status,
                to_db_time(started_at),
                to_db_time(last_activity_at),
            ),
        )
        row_id = cur.lastrowid
        assert row_id is not None
        return int(row_id)

    @staticmethod
    def link(
        conn: sqlite3.Connection,
        *,
        thread_id: int,
        event_id: str,
        relationship: str,
        confidence: float,
        basis: list[str],
    ) -> None:
        conn.execute(
            """INSERT OR REPLACE INTO thread_events (
                   thread_id, event_id, relationship, confidence, basis_json
               ) VALUES (?, ?, ?, ?, ?)""",
            (thread_id, event_id, relationship, confidence, json.dumps(basis)),
        )

    @staticmethod
    def touch(conn: sqlite3.Connection, thread_id: int, last_activity_at: datetime) -> None:
        conn.execute(
            "UPDATE threads SET last_activity_at = ? WHERE id = ?",
            (to_db_time(last_activity_at), thread_id),
        )

    @staticmethod
    def list_for_project(conn: sqlite3.Connection, project_id: str) -> list[sqlite3.Row]:
        return list(
            conn.execute(
                "SELECT * FROM threads WHERE project_id = ? ORDER BY started_at, id",
                (project_id,),
            ).fetchall()
        )

    @staticmethod
    def members(conn: sqlite3.Connection, thread_id: int) -> list[sqlite3.Row]:
        return list(
            conn.execute(
                """SELECT te.*, e.timestamp FROM thread_events te
                   JOIN events e ON e.id = te.event_id
                   WHERE te.thread_id = ? ORDER BY e.timestamp""",
                (thread_id,),
            ).fetchall()
        )
