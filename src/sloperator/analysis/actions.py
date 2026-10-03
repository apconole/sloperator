"""Action item detection with provenance and explicitness classification.

Extraction is pluggable behind the ActionExtractor protocol: a deterministic
heuristic ships here (usable without any model backend), and the LLM role
added later implements the same protocol. Every candidate keeps its origin
(source event ids, thread) and an explicitness class so inferred items are
never presented as commitments:

- EXPLICIT_REQUEST     "please send a fix", "can you review?"
- EXPLICIT_COMMITMENT  "I will resend v3"
- INFERRED_ACTION      derived obligations (LLM territory mostly)
- SUGGESTION           "you might want to ..."
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Literal, Protocol

from pydantic import Field

from sloperator.config.models import FrozenModel
from sloperator.database.dao import ThreadDao, to_db_time, utc_now
from sloperator.database.db import Database

Explicitness = Literal["EXPLICIT_REQUEST", "EXPLICIT_COMMITMENT", "INFERRED_ACTION", "SUGGESTION"]


class ActionItemCandidate(FrozenModel):
    """Model-shaped output of an extractor for one detected action."""

    description: str = Field(min_length=1)
    explicitness: Explicitness
    confidence: float = Field(ge=0.0, le=1.0)
    owner_hint: str | None = None
    source_event_ids: list[str] = Field(min_length=1)


class ActionExtractor(Protocol):
    def extract(self, events: list[dict[str, object]]) -> list[ActionItemCandidate]:
        """Return action candidates for the given thread events."""
        ...


@dataclass(frozen=True)
class _Pattern:
    explicitness: Explicitness
    confidence: float
    regex: re.Pattern[str]


_PATTERNS: tuple[_Pattern, ...] = (
    _Pattern(
        "EXPLICIT_REQUEST",
        0.75,
        re.compile(r"\b(please\b|could you|can you|would you mind|needs? to be)\b", re.I),
    ),
    _Pattern(
        "EXPLICIT_COMMITMENT",
        0.85,
        re.compile(r"\b(i(?: will|'ll| am going to| plan to))\b", re.I),
    ),
    _Pattern(
        "SUGGESTION",
        0.45,
        re.compile(r"\b(you (?:might|should|could) want to|you should|consider\b)\b", re.I),
    ),
)


class HeuristicActionExtractor:
    """Deterministic regex-based extractor; the baseline and test oracle."""

    def extract(self, events: list[dict[str, object]]) -> list[ActionItemCandidate]:
        candidates: list[ActionItemCandidate] = []
        for event in events:
            event_id = str(event.get("id") or "")
            body = event.get("body")
            if not event_id or not isinstance(body, str):
                continue
            for line in body.splitlines():
                sentence = line.strip()
                if len(sentence) < 15:
                    continue
                for pattern in _PATTERNS:
                    if pattern.regex.search(sentence):
                        candidates.append(
                            ActionItemCandidate(
                                description=sentence,
                                explicitness=pattern.explicitness,
                                confidence=pattern.confidence,
                                source_event_ids=[event_id],
                            )
                        )
                        break
        return candidates


@dataclass(frozen=True)
class PersistStats:
    thread_id: int
    candidates: int
    inserted: int
    skipped_duplicates: int


class ActionItemWriter:
    """Persists candidates with provenance, deduplicating open items."""

    def __init__(self, db: Database, extractor: ActionExtractor | None = None) -> None:
        self._db = db
        self._extractor = extractor or HeuristicActionExtractor()

    def process_thread(self, project_id: str, thread_id: int) -> PersistStats:
        with self._db.transaction() as conn:
            members = ThreadDao.members(conn, thread_id)
            events: list[dict[str, object]] = [
                {
                    "id": m["event_id"],
                    "subject": m["subject"],
                    "body": m["body"],
                    "timestamp": m["timestamp"],
                }
                for m in members
            ]
            candidates = self._extractor.extract(events)

            inserted = 0
            duplicates = 0
            now = utc_now()
            for candidate in candidates:
                exists = conn.execute(
                    """SELECT 1 FROM action_items
                       WHERE thread_id = ? AND status = 'open'
                         AND lower(trim(description)) = lower(trim(?))""",
                    (thread_id, candidate.description),
                ).fetchone()
                if exists is not None:
                    duplicates += 1
                    continue
                conn.execute(
                    """INSERT INTO action_items (
                           project_id, thread_id, description, status, confidence,
                           explicitness, source_event_ids_json, created_at, updated_at
                       ) VALUES (?, ?, ?, 'open', ?, ?, ?, ?, ?)""",
                    (
                        project_id,
                        thread_id,
                        candidate.description,
                        candidate.confidence,
                        candidate.explicitness,
                        json.dumps(candidate.source_event_ids),
                        to_db_time(now),
                        to_db_time(now),
                    ),
                )
                inserted += 1

            return PersistStats(
                thread_id=thread_id,
                candidates=len(candidates),
                inserted=inserted,
                skipped_duplicates=duplicates,
            )
