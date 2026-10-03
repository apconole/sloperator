"""Persistent task queue: durable enqueue, claim, retry with backoff.

Tasks survive restarts; workers claim one task at a time with an owner id
and a lock timestamp. Failed attempts are retried with exponential backoff
until max_attempts is exhausted, and stale locks from crashed workers are
reclaimable after a timeout. Deduplication uses the UNIQUE dedupe_key so
producers can enqueue idempotently.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from sloperator.database.dao import from_db_time, to_db_time, utc_now
from sloperator.database.db import Database


class TaskStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True)
class Task:
    id: int
    kind: str
    project_id: str | None
    dedupe_key: str | None
    status: TaskStatus
    priority: int
    attempts: int
    max_attempts: int
    run_after: datetime
    payload: dict[str, Any]
    last_error: str | None

    @staticmethod
    def from_row(row: Any) -> Task:
        return Task(
            id=int(row["id"]),
            kind=str(row["kind"]),
            project_id=row["project_id"],
            dedupe_key=row["dedupe_key"],
            status=TaskStatus(str(row["status"])),
            priority=int(row["priority"]),
            attempts=int(row["attempts"]),
            max_attempts=int(row["max_attempts"] or 1),
            run_after=from_db_time(str(row["run_after"])),
            payload=dict(json.loads(row["payload_json"] or "{}")),
            last_error=row["last_error"],
        )


class TaskQueue:
    def __init__(self, db: Database, worker_id: str) -> None:
        self._db = db
        self._worker_id = worker_id

    def enqueue(
        self,
        kind: str,
        *,
        project_id: str | None = None,
        dedupe_key: str | None = None,
        priority: int = 5,
        max_attempts: int = 3,
        run_after: datetime | None = None,
        payload: dict[str, Any] | None = None,
    ) -> tuple[int, bool]:
        """Insert a task; (id, created). dedupe_key collisions return existing."""
        now = utc_now()
        with self._db.transaction() as conn:
            cursor = conn.execute(
                """INSERT OR IGNORE INTO tasks (
                       kind, project_id, dedupe_key, status, priority, attempts,
                       max_attempts, run_after, payload_json, created_at, updated_at
                   ) VALUES (?, ?, ?, 'pending', ?, 0, ?, ?, ?, ?, ?)""",
                (
                    kind,
                    project_id,
                    dedupe_key,
                    priority,
                    max_attempts,
                    to_db_time(run_after or now),
                    json.dumps(payload or {}),
                    to_db_time(now),
                    to_db_time(now),
                ),
            )
            if cursor.rowcount:
                return int(cursor.lastrowid or 0), True
            if dedupe_key is not None:
                row = conn.execute(
                    "SELECT id FROM tasks WHERE dedupe_key = ?", (dedupe_key,)
                ).fetchone()
                if row is not None:
                    return int(row["id"]), False
            raise RuntimeError("enqueue matched no row and no dedupe key")

    def claim_next(self) -> Task | None:
        now = utc_now()
        with self._db.transaction() as conn:
            row = conn.execute(
                """SELECT id FROM tasks
                   WHERE status = 'pending' AND run_after <= ?
                   ORDER BY priority ASC, run_after ASC, id ASC
                   LIMIT 1""",
                (to_db_time(now),),
            ).fetchone()
            if row is None:
                return None
            task_id = int(row["id"])
            conn.execute(
                """UPDATE tasks SET status = 'running', locked_by = ?, locked_at = ?,
                          attempts = attempts + 1, updated_at = ?
                   WHERE id = ? AND status = 'pending'""",
                (self._worker_id, to_db_time(now), to_db_time(now), task_id),
            )
            claimed = conn.execute(
                "SELECT * FROM tasks WHERE id = ? AND locked_by = ?",
                (task_id, self._worker_id),
            ).fetchone()
            return Task.from_row(claimed) if claimed is not None else None

    def complete(self, task_id: int) -> None:
        now = to_db_time(utc_now())
        with self._db.transaction() as conn:
            conn.execute(
                """UPDATE tasks SET status = 'completed', locked_by = NULL,
                          locked_at = NULL, updated_at = ?
                   WHERE id = ? AND status = 'running'""",
                (now, task_id),
            )

    def fail(self, task_id: int, error: str, *, backoff_base_seconds: float = 60.0) -> bool:
        """Record failure; returns True when the task was rescheduled."""
        now = utc_now()
        with self._db.transaction() as conn:
            row = conn.execute(
                "SELECT attempts, max_attempts FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if row is None:
                return False
            attempts = int(row["attempts"])
            max_attempts = int(row["max_attempts"] or 1)
            if attempts < max_attempts:
                delay = backoff_base_seconds * (2 ** (attempts - 1))
                conn.execute(
                    """UPDATE tasks SET status = 'pending', locked_by = NULL,
                              locked_at = NULL, last_error = ?, run_after = ?, updated_at = ?
                       WHERE id = ?""",
                    (
                        error[:2000],
                        to_db_time(now + timedelta(seconds=delay)),
                        to_db_time(now),
                        task_id,
                    ),
                )
                return True
            conn.execute(
                """UPDATE tasks SET status = 'failed', locked_by = NULL,
                          locked_at = NULL, last_error = ?, updated_at = ?
                   WHERE id = ?""",
                (error[:2000], to_db_time(now), task_id),
            )
            return False

    def requeue_stale(self, lock_timeout_seconds: int) -> int:
        cutoff = to_db_time(utc_now() - timedelta(seconds=lock_timeout_seconds))
        with self._db.transaction() as conn:
            cursor = conn.execute(
                """UPDATE tasks SET status = 'pending', locked_by = NULL,
                          locked_at = NULL, updated_at = ?
                   WHERE status = 'running' AND locked_at <= ?""",
                (to_db_time(utc_now()), cutoff),
            )
            return int(cursor.rowcount)

    def run_once(
        self, handler: Callable[[Task], None], *, backoff_base_seconds: float = 60.0
    ) -> Task | None:
        """Claim one task and run the handler; returns the claimed task."""
        task = self.claim_next()
        if task is None:
            return None
        try:
            handler(task)
        except Exception as exc:
            self.fail(
                task.id, f"{type(exc).__name__}: {exc}", backoff_base_seconds=backoff_base_seconds
            )
            raise
        self.complete(task.id)
        return task

    def stats(self) -> dict[str, int]:
        rows = (
            self._db.connect()
            .execute("SELECT status, COUNT(*) AS n FROM tasks GROUP BY status")
            .fetchall()
        )
        return {str(row["status"]): int(row["n"]) for row in rows}
