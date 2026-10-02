"""SQLite database wrapper: connection setup, transactions, and migrations."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Self

from sloperator.database.migrations import apply_migrations


class Database:
    """One SQLite database file with manual (explicit) transaction control.

    Connections run in autocommit mode; multi-statement atomicity is
    obtained through the transaction() context manager. DAO methods take a
    connection and never start transactions themselves, so callers compose
    several DAO calls inside one transaction when needed.
    """

    def __init__(self, path: Path, busy_timeout_ms: int = 5000) -> None:
        self._path = Path(path)
        self._busy_timeout_ms = busy_timeout_ms
        self._conn: sqlite3.Connection | None = None

    @property
    def path(self) -> Path:
        return self._path

    def connect(self) -> sqlite3.Connection:
        """Return the shared connection, opening it on first use."""
        if self._conn is None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self._path, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute(f"PRAGMA busy_timeout = {self._busy_timeout_ms}")
            conn.execute("PRAGMA journal_mode = WAL")
            self._conn = conn
        return self._conn

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """BEGIN IMMEDIATE / COMMIT, rolling back on any error (fail closed)."""
        conn = self.connect()
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")

    def migrate(self) -> tuple[int, ...]:
        """Apply pending migrations in order; returns versions applied now."""
        return apply_migrations(self.connect())

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> Self:
        self.connect()
        return self

    def __exit__(self, *args: object) -> None:
        self.close()
