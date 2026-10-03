"""Tests for the persistent task queue: dedupe, claim, retry, stale locks."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from sloperator.database import Database
from sloperator.queue.tasks import TaskQueue


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "queue.sqlite3")
    database.migrate()
    yield database
    database.close()


@pytest.fixture()
def queue(db: Database) -> TaskQueue:
    return TaskQueue(db, worker_id="w1")


def test_enqueue_and_dedupe(queue: TaskQueue) -> None:
    first_id, created = queue.enqueue("ingest", project_id="p", dedupe_key="ingest:p")
    assert created is True
    second_id, created_again = queue.enqueue("ingest", project_id="p", dedupe_key="ingest:p")
    assert created_again is False
    assert second_id == first_id
    assert queue.stats() == {"pending": 1}


def test_claim_order_priority_then_run_after(queue: TaskQueue) -> None:
    queue.enqueue("low", priority=9)
    queue.enqueue("urgent", priority=1)
    queue.enqueue("normal", priority=5)
    order = [queue.claim_next().kind for _ in range(3)]  # type: ignore[union-attr]
    assert order == ["urgent", "normal", "low"]
    assert queue.claim_next() is None


def test_run_after_delays_claim(queue: TaskQueue) -> None:
    from sloperator.queue.tasks import utc_now

    queue.enqueue("later", run_after=utc_now() + timedelta(hours=1))
    assert queue.claim_next() is None


def test_complete_and_stats(queue: TaskQueue) -> None:
    task_id, _ = queue.enqueue("analyze")
    task = queue.claim_next()
    assert task is not None and task.id == task_id
    assert task.attempts == 1
    queue.complete(task_id)
    assert queue.stats() == {"completed": 1}


def test_retry_with_backoff_then_failure(queue: TaskQueue, db: Database) -> None:
    task_id, _ = queue.enqueue("flaky", max_attempts=2)

    task = queue.claim_next()
    assert task is not None
    rescheduled = queue.fail(task.id, "boom", backoff_base_seconds=3600)
    assert rescheduled is True
    row = (
        db.connect()
        .execute("SELECT status, last_error FROM tasks WHERE id = ?", (task.id,))
        .fetchone()
    )
    assert row["status"] == "pending"
    assert row["last_error"] == "boom"

    # not claimable yet (backoff in the future)
    assert queue.claim_next() is None

    # force it due and exhaust attempts
    with db.transaction() as conn:
        conn.execute("UPDATE tasks SET run_after = '2000-01-01 00:00:00' WHERE id = ?", (task_id,))
    task = queue.claim_next()
    assert task is not None and task.attempts == 2
    rescheduled = queue.fail(task.id, "boom again", backoff_base_seconds=3600)
    assert rescheduled is False
    assert queue.stats() == {"failed": 1}


def test_requeue_stale_locks(db: Database) -> None:
    crashed = TaskQueue(db, worker_id="dead-worker")
    healthy = TaskQueue(db, worker_id="live-worker")
    task_id, _ = crashed.enqueue("job")
    claimed = crashed.claim_next()
    assert claimed is not None
    with db.transaction() as conn:  # simulate an old lock
        conn.execute("UPDATE tasks SET locked_at = '2000-01-01 00:00:00' WHERE id = ?", (task_id,))
    assert healthy.requeue_stale(lock_timeout_seconds=300) == 1
    reclaimed = healthy.claim_next()
    assert reclaimed is not None and reclaimed.id == task_id


def test_run_once_success_and_failure(queue: TaskQueue) -> None:
    queue.enqueue("good")
    seen: list[str] = []

    def ok(task: object) -> None:
        seen.append(str(task.kind))

    task = queue.run_once(ok)
    assert task is not None and seen == ["good"]
    assert queue.stats() == {"completed": 1}

    queue.enqueue("bad")

    def boom(task: object) -> None:
        raise RuntimeError("handler exploded")

    with pytest.raises(RuntimeError):
        queue.run_once(boom)
    assert queue.stats() == {"completed": 1, "pending": 1}
