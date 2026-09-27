"""SQLite persistence for sessions, history, runs, and audit events."""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

MIGRATION_1 = """
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

MIGRATION_2 = """
CREATE TABLE IF NOT EXISTS change_sets (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    undone_at TEXT
);

CREATE TABLE IF NOT EXISTS file_changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    change_set_id TEXT NOT NULL REFERENCES change_sets(id) ON DELETE CASCADE,
    path TEXT NOT NULL,
    existed INTEGER NOT NULL,
    before_text TEXT,
    before_sha256 TEXT,
    after_sha256 TEXT NOT NULL,
    after_bytes INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_change_sets_session_id
    ON change_sets(session_id, created_at);
CREATE INDEX IF NOT EXISTS idx_file_changes_change_set_id
    ON file_changes(change_set_id, id);
"""

MIGRATION_3 = """
CREATE TABLE IF NOT EXISTS execution_receipts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    receipt_json TEXT NOT NULL,
    previous_hash TEXT,
    receipt_hash TEXT NOT NULL UNIQUE
);

CREATE INDEX IF NOT EXISTS idx_execution_receipts_session_id
    ON execution_receipts(session_id, id);
"""

MIGRATIONS = ((1, MIGRATION_1), (2, MIGRATION_2), (3, MIGRATION_3))


def _receipt_hash(previous_hash: str | None, receipt_json: str) -> str:
    chained = f"{previous_hash or ''}\n{receipt_json}".encode()
    return hashlib.sha256(chained).hexdigest()


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class AuditStore:
    """Small SQLite store that opens a fresh connection for each operation."""

    def __init__(self, path: Path) -> None:
        self.path = path.expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            self._migrate(connection)

    @staticmethod
    def _migrate(connection: sqlite3.Connection) -> None:
        current = int(connection.execute("PRAGMA user_version").fetchone()[0])
        for version, sql in MIGRATIONS:
            if version <= current:
                continue
            connection.executescript(sql)
            connection.execute(f"PRAGMA user_version = {version}")

    @property
    def schema_version(self) -> int:
        with self._connection() as connection:
            return int(connection.execute("PRAGMA user_version").fetchone()[0])

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
    ) -> int:
        self.ensure_session(session_id)
        now = _now()
        usage_json = None if usage is None else json.dumps(usage, default=str)
        with self._connection() as connection:
            cursor = connection.execute(
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
            return int(cursor.lastrowid)

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

    def record_receipt(
        self,
        session_id: str,
        run_id: int,
        receipt: dict[str, Any],
    ) -> str:
        """Append a canonical receipt to a per-session integrity hash chain."""

        self.ensure_session(session_id)
        created_at = _now()
        canonical_receipt = {
            **receipt,
            "session_id": session_id,
            "run_id": run_id,
            "created_at": created_at,
        }
        receipt_json = json.dumps(
            canonical_receipt,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        with self._connection() as connection:
            run = connection.execute(
                "SELECT session_id FROM runs WHERE id = ?", (run_id,)
            ).fetchone()
            if run is None or run["session_id"] != session_id:
                raise ValueError("Receipt run does not belong to the session")
            previous = connection.execute(
                """
                SELECT receipt_hash FROM execution_receipts
                WHERE session_id = ? ORDER BY id DESC LIMIT 1
                """,
                (session_id,),
            ).fetchone()
            previous_hash = None if previous is None else str(previous["receipt_hash"])
            receipt_hash = _receipt_hash(previous_hash, receipt_json)
            connection.execute(
                """
                INSERT INTO execution_receipts(
                    session_id, run_id, created_at, receipt_json,
                    previous_hash, receipt_hash
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    run_id,
                    created_at,
                    receipt_json,
                    previous_hash,
                    receipt_hash,
                ),
            )
        return receipt_hash

    def get_receipts(self, session_id: str) -> list[dict[str, Any]]:
        self._validate_session_id(session_id)
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT id, run_id, created_at, receipt_json,
                       previous_hash, receipt_hash
                FROM execution_receipts
                WHERE session_id = ? ORDER BY id
                """,
                (session_id,),
            ).fetchall()
        receipts: list[dict[str, Any]] = []
        for row in rows:
            value = dict(row)
            value["receipt"] = json.loads(value.pop("receipt_json"))
            receipts.append(value)
        return receipts

    def verify_receipt_chain(
        self,
        session_id: str,
        *,
        expected_head: str | None = None,
    ) -> tuple[bool, str | None]:
        """Verify canonical content hashes and every previous-hash link."""

        self._validate_session_id(session_id)
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT id, receipt_json, previous_hash, receipt_hash
                FROM execution_receipts
                WHERE session_id = ? ORDER BY id
                """,
                (session_id,),
            ).fetchall()
        if not rows:
            return False, "session has no execution receipts"
        previous_hash: str | None = None
        for row in rows:
            if row["previous_hash"] != previous_hash:
                return False, f"receipt {row['id']} has a broken previous-hash link"
            expected = _receipt_hash(previous_hash, str(row["receipt_json"]))
            if row["receipt_hash"] != expected:
                return False, f"receipt {row['id']} content hash does not match"
            previous_hash = str(row["receipt_hash"])
        if expected_head is not None and previous_hash != expected_head.casefold():
            return False, "receipt chain head does not match the expected hash"
        return True, None

    def list_sessions(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT s.id, s.created_at, s.updated_at, COUNT(r.id) AS run_count,
                       (SELECT prompt FROM runs
                        WHERE session_id = s.id ORDER BY id DESC LIMIT 1)
                       AS last_prompt
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

    def record_change_set(
        self,
        session_id: str,
        kind: str,
        changes: list[dict[str, Any]],
    ) -> str:
        """Persist reversible file snapshots after a filesystem transaction."""

        if not changes:
            raise ValueError("A change set must contain at least one file")
        self.ensure_session(session_id)
        timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        change_set_id = f"chg-{timestamp}-{secrets.token_hex(3)}"
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO change_sets(id, session_id, created_at, kind, status)
                VALUES (?, ?, ?, ?, 'applied')
                """,
                (change_set_id, session_id, _now(), kind),
            )
            connection.executemany(
                """
                INSERT INTO file_changes(
                    change_set_id, path, existed, before_text, before_sha256,
                    after_sha256, after_bytes
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        change_set_id,
                        change["path"],
                        int(change["existed"]),
                        change.get("before_text"),
                        change.get("before_sha256"),
                        change["after_sha256"],
                        change["after_bytes"],
                    )
                    for change in changes
                ],
            )
        return change_set_id

    def list_change_sets(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT c.id, c.session_id, c.created_at, c.kind, c.status,
                       c.undone_at, COUNT(f.id) AS file_count
                FROM change_sets AS c
                LEFT JOIN file_changes AS f ON f.change_set_id = c.id
                GROUP BY c.id
                ORDER BY c.created_at DESC, c.id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_change_set(self, change_set_id: str) -> dict[str, Any] | None:
        if not change_set_id.strip():
            raise ValueError("change_set_id cannot be empty")
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT id, session_id, created_at, kind, status, undone_at
                FROM change_sets WHERE id = ?
                """,
                (change_set_id,),
            ).fetchone()
            if row is None:
                return None
            changes = connection.execute(
                """
                SELECT id, path, existed, before_text, before_sha256,
                       after_sha256, after_bytes
                FROM file_changes
                WHERE change_set_id = ?
                ORDER BY id
                """,
                (change_set_id,),
            ).fetchall()
        result = dict(row)
        result["changes"] = [dict(change) for change in changes]
        return result

    def mark_change_set_undone(self, change_set_id: str) -> None:
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE change_sets
                SET status = 'undone', undone_at = ?
                WHERE id = ? AND status = 'applied'
                """,
                (_now(), change_set_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("Change set is missing or is not currently applied")
