"""SQLite persistence for sessions, history, runs, and audit events."""

from __future__ import annotations

import json
import secrets
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    history_json TEXT
);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    prompt TEXT NOT NULL,
    response TEXT,
    success INTEGER NOT NULL,
    error TEXT,
    usage_json TEXT
);

CREATE TABLE IF NOT EXISTS tool_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    status TEXT NOT NULL,
    details_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_runs_session_id ON runs(session_id, id);
CREATE INDEX IF NOT EXISTS idx_tool_events_session_id
    ON tool_events(session_id, id);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class AuditStore:
    """Small SQLite store that opens a fresh connection for each operation."""

    def __init__(self, path: Path) -> None:
        self.path = path.expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        """Commit or roll back a transaction, then always release the file handle."""

        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def new_session(self) -> str:
        timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        session_id = f"{timestamp}-{secrets.token_hex(3)}"
        self.ensure_session(session_id)
        return session_id

    @staticmethod
    def _validate_session_id(session_id: str) -> None:
        if not session_id.strip() or len(session_id) > 120:
            raise ValueError("session_id must contain 1 to 120 characters")

    def ensure_session(self, session_id: str) -> None:
        self._validate_session_id(session_id)
        now = _now()
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO sessions(id, created_at, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(id) DO NOTHING
                """,
                (session_id, now, now),
            )

    def load_history(self, session_id: str) -> str | None:
        self.ensure_session(session_id)
        with self._connection() as connection:
            row = connection.execute(
                "SELECT history_json FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
        return None if row is None else row["history_json"]

    def save_history(self, session_id: str, history_json: str) -> None:
        self.ensure_session(session_id)
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE sessions
                SET history_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (history_json, _now(), session_id),
            )

    def record_run(
        self,
        session_id: str,
        prompt: str,
        *,
        response: str | None,
        success: bool,
        error: str | None = None,
        usage: dict[str, Any] | None = None,
    ) -> None:
        self.ensure_session(session_id)
        now = _now()
        usage_json = None if usage is None else json.dumps(usage, default=str)
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO runs(
                    session_id, created_at, prompt, response, success, error, usage_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    now,
                    prompt,
                    response,
                    int(success),
                    error,
                    usage_json,
                ),
            )
            connection.execute(
                "UPDATE sessions SET updated_at = ? WHERE id = ?",
                (now, session_id),
            )

    def record_tool_event(
        self,
        session_id: str,
        tool_name: str,
        status: str,
        details: dict[str, Any],
    ) -> None:
        self.ensure_session(session_id)
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO tool_events(
                    session_id, created_at, tool_name, status, details_json
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    _now(),
                    tool_name,
                    status,
                    json.dumps(details, default=str, ensure_ascii=False),
                ),
            )

    def list_sessions(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT s.id, s.created_at, s.updated_at, COUNT(r.id) AS run_count
                FROM sessions AS s
                LEFT JOIN runs AS r ON r.session_id = s.id
                GROUP BY s.id
                ORDER BY s.updated_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_runs(self, session_id: str) -> list[dict[str, Any]]:
        self._validate_session_id(session_id)
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT id, created_at, prompt, response, success, error, usage_json
                FROM runs
                WHERE session_id = ?
                ORDER BY id
                """,
                (session_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_tool_events(self, session_id: str) -> list[dict[str, Any]]:
        """Return ordered, JSON-decoded tool audit events for one session."""

        self._validate_session_id(session_id)
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT id, created_at, tool_name, status, details_json
                FROM tool_events
                WHERE session_id = ?
                ORDER BY id
                """,
                (session_id,),
            ).fetchall()

        events: list[dict[str, Any]] = []
        for row in rows:
            event = dict(row)
            event["details"] = json.loads(event.pop("details_json"))
            events.append(event)
        return events
