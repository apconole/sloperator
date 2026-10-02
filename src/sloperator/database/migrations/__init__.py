"""Numbered SQL migrations applied in order.

Migration files live in this package and are named ``NNNN_description.sql``.
Each migration is applied inside its own transaction (statements are split
on semicolons; scripts must be plain DDL/DML without embedded procedural
blocks). The schema is never destructively recreated: applied versions are
recorded in schema_migrations and re-running migrate() is a no-op.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import UTC, datetime
from importlib import resources

_MIGRATION_NAME = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")


def _split_statements(sql: str) -> list[str]:
    """Split a migration script into statements.

    Strips ``--`` line comments (scripts contain no string literals with
    comment markers or semicolons) then splits on semicolons.
    """
    stripped = "\n".join(line.split("--", 1)[0] for line in sql.splitlines())
    return [part.strip() for part in stripped.split(";") if part.strip()]


def _discover() -> dict[int, str]:
    base = resources.files(__package__)
    found: dict[int, str] = {}
    for entry in base.iterdir():
        match = _MIGRATION_NAME.match(entry.name)
        if match is None:
            continue
        version = int(match.group(1))
        if version in found:
            raise RuntimeError(f"duplicate migration version {version:04d}")
        found[version] = entry.read_text(encoding="utf-8")
    return found


def ensure_schema_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS schema_migrations (
               version INTEGER PRIMARY KEY,
               applied_at TEXT NOT NULL
           )"""
    )


def current_version(conn: sqlite3.Connection) -> int:
    ensure_schema_table(conn)
    row = conn.execute("SELECT MAX(version) AS v FROM schema_migrations").fetchone()
    value = row["v"] if row is not None else None
    return int(value) if value is not None else 0


def apply_migrations(conn: sqlite3.Connection) -> tuple[int, ...]:
    """Apply all migrations newer than the recorded version.

    Returns the versions applied by this call. On any failure the failing
    migration's transaction is rolled back and the error propagates; the
    database must not continue on a partially applied schema.
    """
    ensure_schema_table(conn)
    migrations = _discover()
    current = current_version(conn)

    applied_now: list[int] = []
    for version in sorted(v for v in migrations if v > current):
        sql = migrations[version]
        conn.execute("BEGIN IMMEDIATE")
        try:
            for statement in _split_statements(sql):
                conn.execute(statement)
            conn.execute(
                "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                (version, datetime.now(UTC).isoformat()),
            )
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        applied_now.append(version)
    return tuple(applied_now)
