"""Ingestion pipeline: poll adapters, normalize, deduplicate, persist."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sloperator.database.dao import ActorDao, EventDao, SourceDao, utc_now
from sloperator.database.db import Database
from sloperator.events import Actor, Event, SourceEvent
from sloperator.sources import SourceAdapter

logger = logging.getLogger(__name__)


def normalize(raw: SourceEvent) -> Event:
    """Turn an adapter-provided SourceEvent into a canonical Event."""
    return Event.create(
        source_id=raw.source_id,
        source_type=raw.source_type,
        external_id=raw.external_id,
        timestamp=raw.timestamp,
        author=raw.author or Actor(),
        event_type=raw.event_type,
        subject=raw.subject,
        body=raw.body,
        references=list(raw.references),
        reply_to=raw.reply_to,
        metadata=dict(raw.metadata),
    )


@dataclass(frozen=True)
class IngestStats:
    """Outcome counters for one ingestion round."""

    source_id: str
    received: int = 0
    inserted: int = 0
    duplicate_ids: int = 0
    content_duplicates: int = 0
    cursor: str | None = None


class IngestPipeline:
    """Polls one adapter and durably persists what it returns.

    Events are inserted with INSERT OR IGNORE on (source_id, external_id),
    so re-polling after a crash or cursor rewind never duplicates rows.
    The cursor advances in the same transaction as the events; adapters
    guarantee their returned cursor covers exactly the returned events.
    """

    def __init__(self, db: Database) -> None:
        self._db = db

    def ingest_source(self, adapter: SourceAdapter, *, project_id: str) -> IngestStats:
        with self._db.transaction() as conn:
            SourceDao.upsert(
                conn,
                source_id=adapter.source_id,
                source_type=adapter.source_type,
                project_id=project_id,
            )
            cursor = SourceDao.get_cursor(conn, adapter.source_id)

        result = adapter.poll(cursor)

        inserted = 0
        duplicate_ids = 0
        content_duplicates = 0
        with self._db.transaction() as conn:
            for raw in result.events:
                event = normalize(raw)
                actor_id: int | None = None
                if event.author.email or event.author.name:
                    actor_id = ActorDao.get_or_create(
                        conn, name=event.author.name, email=event.author.email
                    )
                matches = EventDao.find_by_content_hash(conn, project_id, event.content_hash)
                if any(m["source_id"] != event.source_id for m in matches):
                    content_duplicates += 1
                    logger.info(
                        "cross-source duplicate",
                        extra={"event_id": event.id, "project_id": project_id},
                    )
                if EventDao.insert(conn, event, project_id=project_id, actor_id=actor_id):
                    inserted += 1
                else:
                    duplicate_ids += 1
            SourceDao.set_cursor(conn, adapter.source_id, result.cursor)
            SourceDao.mark_success(conn, adapter.source_id, utc_now())

        return IngestStats(
            source_id=adapter.source_id,
            received=len(result.events),
            inserted=inserted,
            duplicate_ids=duplicate_ids,
            content_duplicates=content_duplicates,
            cursor=result.cursor,
        )
