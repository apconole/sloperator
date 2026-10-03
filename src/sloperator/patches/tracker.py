"""Patch series detection and version tracking.

Recognizes git-style subjects (``[PATCH]``, ``[PATCH 3/5]``,
``[PATCH v2 1/4]``, ``[PATCH RFC 0/2]``), groups patches into series keyed
by the normalized subject without markers, and associates revisions (v1 ->
v2) of the same series by title. Like thread reconstruction this is a
deterministic full rebuild per project.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from sloperator.database.dao import EventDao, PatchDao, PatchSeriesDao, from_db_time
from sloperator.database.db import Database
from sloperator.threads.reconstruct import normalize_subject

_BRACKET = re.compile(r"^\s*\[([^\]]*)\]\s*")
_PATCH_COMMON = r"(?:\s+(?:rfc|resend|re-?send))?"
_PATCH_V_FIRST = re.compile(
    rf"^patch(?:\s+v?(\d+))?{_PATCH_COMMON}(?:\s+(\d+)/(\d+))?$", re.IGNORECASE
)
_PATCH_RANGE_FIRST = re.compile(
    rf"^patch(?:\s+(\d+)/(\d+))?{_PATCH_COMMON}(?:\s+v?(\d+))?$", re.IGNORECASE
)


@dataclass(frozen=True)
class PatchHeader:
    """Parsed ``[PATCH ...]`` subject decoration."""

    version: int
    position: int | None
    total: int | None
    base_subject: str

    @property
    def is_cover_letter(self) -> bool:
        return self.position == 0


def parse_patch_subject(subject: str | None) -> PatchHeader | None:
    """Parse a git-style patch subject, or None if it is not a patch."""
    if not subject:
        return None
    bracket = _BRACKET.match(subject)
    if bracket is None:
        return None
    inner = bracket.group(1).strip()

    version: int | None = None
    position: int | None = None
    total: int | None = None

    match = _PATCH_V_FIRST.match(inner)
    if match:
        version_s, position_s, total_s = match.groups()
    else:
        match = _PATCH_RANGE_FIRST.match(inner)
        if match is None:
            return None
        position_s, total_s, version_s = match.groups()

    if version_s:
        version = int(version_s)
    if position_s:
        position = int(position_s)
    if total_s:
        total = int(total_s)

    return PatchHeader(
        version=version or 1,
        position=position,
        total=total,
        base_subject=subject[bracket.end() :].strip(),
    )


@dataclass(frozen=True)
class PatchStats:
    """Counters for one patch-tracking rebuild."""

    project_id: str
    events_scanned: int = 0
    series: int = 0
    patches: int = 0
    version_bumps: int = 0


@dataclass
class _SeriesState:
    id: int
    latest_version: int


class PatchSeriesTracker:
    """Groups patch events into series and tracks revisions."""

    def __init__(self, db: Database) -> None:
        self._db = db

    def rebuild_project(self, project_id: str) -> PatchStats:
        with self._db.transaction() as conn:
            rows = EventDao.list_for_project(conn, project_id)
            PatchSeriesDao.clear_project(conn, project_id)

            series_by_key: dict[str, _SeriesState] = {}
            key_by_message: dict[str, str] = {}
            patches = 0
            version_bumps = 0

            for row in rows:
                header = parse_patch_subject(row["subject"])
                if header is None:
                    continue
                references = json.loads(row["references_json"])
                if row["reply_to"]:
                    references.append(row["reply_to"])
                key = next(
                    (key_by_message[ref] for ref in reversed(references) if ref in key_by_message),
                    None,
                )
                if key is None:
                    key = normalize_subject(header.base_subject) or row["subject"]
                if row["external_id"]:
                    key_by_message[row["external_id"]] = key
                timestamp = from_db_time(row["timestamp"])

                state = series_by_key.get(key)
                if state is None:
                    series_id = PatchSeriesDao.insert(
                        conn,
                        project_id=project_id,
                        title=row["subject"],
                        latest_version=header.version,
                        created_at=timestamp,
                    )
                    series_by_key[key] = _SeriesState(series_id, header.version)
                else:
                    if header.version > state.latest_version:
                        PatchSeriesDao.set_latest_version(conn, state.id, header.version, timestamp)
                        state.latest_version = header.version
                        version_bumps += 1
                    series_id = state.id

                PatchDao.upsert(
                    conn,
                    series_id=series_id,
                    event_id=row["id"],
                    version=header.version,
                    position=header.position if header.position is not None else 1,
                    total=header.total,
                    subject=row["subject"],
                    content_hash=row["content_hash"],
                )
                patches += 1

            return PatchStats(
                project_id=project_id,
                events_scanned=len(rows),
                series=len(series_by_key),
                patches=patches,
                version_bumps=version_bumps,
            )
