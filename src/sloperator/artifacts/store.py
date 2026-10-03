"""Artifact persistence: markdown on disk, metadata + provenance in the DB.

Every artifact records its origin per the design contract: task, agent run,
source events, backend/model, timestamp, confidence, factual-vs-inferred,
workspace, and project/thread applicability.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from sloperator.database.dao import to_db_time, utc_now
from sloperator.database.db import Database


class ArtifactType(StrEnum):
    REPORT = "REPORT"
    SUMMARY = "SUMMARY"
    ACTION_LIST = "ACTION_LIST"
    REVIEW = "REVIEW"
    PROPOSED_PATCH = "PROPOSED_PATCH"
    TEST_RESULT = "TEST_RESULT"
    PROPOSED_RESPONSE = "PROPOSED_RESPONSE"
    INVESTIGATION = "INVESTIGATION"
    DECISION_RECORD = "DECISION_RECORD"
    DAILY_BRIEFING = "DAILY_BRIEFING"


@dataclass(frozen=True)
class ArtifactProvenance:
    backend: str | None = None
    model: str | None = None
    source_event_ids: tuple[str, ...] = ()
    factual: bool = False
    confidence: float | None = None
    task_id: int | None = None
    agent_run_id: int | None = None
    workspace_path: str | None = None


@dataclass(frozen=True)
class ArtifactRecord:
    id: int
    artifact_type: ArtifactType
    title: str
    status: str
    content_path: Path
    created_at: str

    def read(self) -> str:
        return self.content_path.read_text(encoding="utf-8")


_SLUG = re.compile(r"[^a-z0-9]+")


def _slugify(title: str) -> str:
    slug = _SLUG.sub("-", title.lower()).strip("-")
    return slug[:48] or "artifact"


class ArtifactStore:
    """Writes artifacts below a dedicated root; paths never escape it."""

    def __init__(self, db: Database, root: Path) -> None:
        self._db = db
        self._root = root.resolve()

    def save(
        self,
        *,
        project_id: str,
        artifact_type: ArtifactType,
        title: str,
        content: str,
        provenance: ArtifactProvenance,
        thread_id: int | None = None,
    ) -> ArtifactRecord:
        now = utc_now()
        with self._db.transaction() as conn:
            cursor = conn.execute(
                """INSERT INTO artifacts (
                       task_id, agent_run_id, project_id, thread_id, artifact_type,
                       title, status, factual, confidence, workspace_path, backend,
                       model, source_event_ids_json, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, 'ready_for_review', ?, ?, ?, ?, ?, ?, ?)""",
                (
                    provenance.task_id,
                    provenance.agent_run_id,
                    project_id,
                    thread_id,
                    artifact_type.value,
                    title,
                    1 if provenance.factual else 0,
                    provenance.confidence,
                    provenance.workspace_path,
                    provenance.backend,
                    provenance.model,
                    json.dumps(list(provenance.source_event_ids)),
                    to_db_time(now),
                ),
            )
            artifact_id = int(cursor.lastrowid or 0)
            path = self._path_for(project_id, artifact_id, title)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            conn.execute(
                "UPDATE artifacts SET content_path = ? WHERE id = ?",
                (str(path), artifact_id),
            )
        return ArtifactRecord(
            id=artifact_id,
            artifact_type=artifact_type,
            title=title,
            status="ready_for_review",
            content_path=path,
            created_at=to_db_time(now),
        )

    def get(self, artifact_id: int) -> ArtifactRecord | None:
        row = (
            self._db.connect()
            .execute("SELECT * FROM artifacts WHERE id = ?", (artifact_id,))
            .fetchone()
        )
        if row is None or row["content_path"] is None:
            return None
        path = Path(str(row["content_path"]))
        self._require_under_root(path)
        return ArtifactRecord(
            id=int(row["id"]),
            artifact_type=ArtifactType(row["artifact_type"]),
            title=str(row["title"] or ""),
            status=str(row["status"]),
            content_path=path,
            created_at=str(row["created_at"]),
        )

    def list_for_project(self, project_id: str) -> list[ArtifactRecord]:
        rows = (
            self._db.connect()
            .execute(
                """SELECT * FROM artifacts WHERE project_id = ?
               ORDER BY created_at DESC, id DESC""",
                (project_id,),
            )
            .fetchall()
        )
        records: list[ArtifactRecord] = []
        for row in rows:
            if row["content_path"] is None:
                continue
            path = Path(str(row["content_path"]))
            self._require_under_root(path)
            records.append(
                ArtifactRecord(
                    id=int(row["id"]),
                    artifact_type=ArtifactType(row["artifact_type"]),
                    title=str(row["title"] or ""),
                    status=str(row["status"]),
                    content_path=path,
                    created_at=str(row["created_at"]),
                )
            )
        return records

    def _path_for(self, project_id: str, artifact_id: int, title: str) -> Path:
        date = to_db_time(utc_now())[:10]
        path = (
            self._root
            / _slugify(project_id)
            / f"{date}-artifact-{artifact_id}-{_slugify(title)}.md"
        )
        self._require_under_root(path)
        return path

    def _require_under_root(self, path: Path) -> None:
        resolved = path.resolve()
        if self._root not in resolved.parents:
            raise ValueError(f"artifact path {resolved} escapes artifacts root {self._root}")
