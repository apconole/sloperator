"""Conversation reconstruction: rebuild per-project threads from events.

Threading uses independent signals in decreasing reliability, and every
thread edge records its confidence and basis so downstream consumers can
distrust weak links:

1. In-Reply-To match        -> 1.00  ["in_reply_to"]
2. References chain match   -> 0.95  ["references"]
3. GitHub comment->issue    -> 1.00  ["issue_reference"]
4. Normalized subject +     -> 0.60  ["subject_similarity", "time_window"]
   activity window

Reconstruction is a deterministic full rebuild per project: re-running it
on unchanged events produces identical output, so it can be re-triggered
whenever new events arrive.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sloperator.database.dao import EventDao, ThreadDao, from_db_time
from sloperator.database.db import Database

SUBJECT_MATCH_WINDOW = timedelta(days=14)

_BRACKET_PREFIX = re.compile(r"^\s*\[[^\]]*\]\s*")
_REPLY_PREFIX = re.compile(r"^\s*(?:re|fwd?|aw|sv|vs):\s*", re.IGNORECASE)


def normalize_subject(subject: str | None) -> str:
    """Strip list tags and reply/forward markers for subject matching."""
    if not subject:
        return ""
    text = subject.strip()
    while True:
        previous = text
        text = _BRACKET_PREFIX.sub("", text)
        text = _REPLY_PREFIX.sub("", text)
        if text == previous:
            break
    return text.strip().lower()


@dataclass(frozen=True)
class RebuildStats:
    """Counters describing one reconstruction run."""

    project_id: str
    events: int = 0
    threads: int = 0
    roots: int = 0
    in_reply_to: int = 0
    references: int = 0
    issue_references: int = 0
    subject_links: int = 0


@dataclass
class _Thread:
    root_event_id: str
    subject: str | None
    started_at: datetime
    last_activity_at: datetime
    members: list[tuple[str, str, float, list[str]]] = field(default_factory=list)


class ThreadReconstructor:
    """Deterministic multi-signal thread graph rebuild."""

    def __init__(self, db: Database) -> None:
        self._db = db

    def rebuild_project(self, project_id: str) -> RebuildStats:
        with self._db.transaction() as conn:
            rows = EventDao.list_for_project(conn, project_id)

            threads: list[_Thread] = []
            by_message_key: dict[str, int] = {}
            by_subject: dict[str, int] = {}
            counts = {
                "in_reply_to": 0,
                "references": 0,
                "issue_references": 0,
                "subject_links": 0,
            }

            for row in rows:
                timestamp = from_db_time(row["timestamp"])
                references = json.loads(row["references_json"])
                metadata: dict[str, Any] = json.loads(row["metadata_json"])
                parent: int | None = None
                basis: list[str] = []
                confidence = 1.0

                reply_to = row["reply_to"]
                if reply_to and reply_to in by_message_key:
                    parent = by_message_key[reply_to]
                    basis, confidence = ["in_reply_to"], 1.0
                    counts["in_reply_to"] += 1
                else:
                    referenced = [
                        by_message_key[ref] for ref in reversed(references) if ref in by_message_key
                    ]
                    issue_target = self._issue_target(row["event_type"], metadata, by_message_key)
                    if referenced:
                        parent = referenced[0]
                        basis, confidence = ["references"], 0.95
                        counts["references"] += 1
                    elif issue_target is not None:
                        parent = issue_target
                        basis, confidence = ["issue_reference"], 1.0
                        counts["issue_references"] += 1
                    else:
                        norm = normalize_subject(row["subject"])
                        candidate = by_subject.get(norm) if norm else None
                        if (
                            candidate is not None
                            and timestamp - threads[candidate].last_activity_at
                            <= SUBJECT_MATCH_WINDOW
                        ):
                            parent = candidate
                            basis = ["subject_similarity", "time_window"]
                            confidence = 0.6
                            counts["subject_links"] += 1

                if parent is None:
                    thread = _Thread(
                        root_event_id=row["id"],
                        subject=row["subject"],
                        started_at=timestamp,
                        last_activity_at=timestamp,
                    )
                    thread.members.append((row["id"], "root", 1.0, ["root"]))
                    threads.append(thread)
                    index = len(threads) - 1
                    if row["external_id"]:
                        by_message_key[row["external_id"]] = index
                    norm = normalize_subject(row["subject"])
                    if norm:
                        by_subject.setdefault(norm, index)
                else:
                    thread = threads[parent]
                    thread.members.append((row["id"], "reply_of", confidence, basis))
                    thread.last_activity_at = max(thread.last_activity_at, timestamp)
                    index = parent
                    if row["external_id"]:
                        by_message_key[row["external_id"]] = index

            ThreadDao.clear_project(conn, project_id)
            for thread in threads:
                thread_id = ThreadDao.insert(
                    conn,
                    project_id=project_id,
                    root_event_id=thread.root_event_id,
                    subject=thread.subject,
                    started_at=thread.started_at,
                    last_activity_at=thread.last_activity_at,
                )
                for event_id, relationship, confidence, basis in thread.members:
                    ThreadDao.link(
                        conn,
                        thread_id=thread_id,
                        event_id=event_id,
                        relationship=relationship,
                        confidence=confidence,
                        basis=basis,
                    )

            return RebuildStats(
                project_id=project_id,
                events=len(rows),
                threads=len(threads),
                roots=len(threads),
                **counts,
            )

    @staticmethod
    def _issue_target(
        event_type: str, metadata: dict[str, Any], by_message_key: dict[str, int]
    ) -> int | None:
        """Link GitHub issue comments to their issue thread."""
        if event_type != "issue_comment":
            return None
        issue_url = str(metadata.get("issue_url") or "")
        match = re.search(r"/issues/(\d+)$", issue_url)
        if match is None:
            return None
        return by_message_key.get(f"issue-{match.group(1)}")
