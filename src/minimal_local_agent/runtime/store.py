"""SQLite 存储：任务、步骤、事件、检查点和工具账本，是运行时唯一的事实来源。"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    parent_id TEXT REFERENCES tasks(id),
    goal TEXT NOT NULL,
    status TEXT NOT NULL,
    options TEXT NOT NULL DEFAULT '{}',
    result TEXT,
    error TEXT,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS steps (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    seq INTEGER NOT NULL,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    UNIQUE (task_id, seq)
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    step_id TEXT,
    parent_id INTEGER REFERENCES events(id),
    kind TEXT NOT NULL,
    data TEXT NOT NULL,
    input_sha TEXT,
    output_sha TEXT,
    artifact_path TEXT,
    artifact_sha TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_task ON events(task_id, id);
CREATE INDEX IF NOT EXISTS idx_events_artifact ON events(artifact_sha);
CREATE TABLE IF NOT EXISTS checkpoints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    step_seq INTEGER NOT NULL,
    state TEXT NOT NULL,
    event_id INTEGER,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_checkpoints_task ON checkpoints(task_id, id);
CREATE TABLE IF NOT EXISTS tool_ledger (
    key TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    step_id TEXT NOT NULL,
    call_id TEXT NOT NULL,
    tool TEXT NOT NULL,
    args_sha TEXT NOT NULL,
    status TEXT NOT NULL,
    prepared TEXT,
    result TEXT,
    error TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ledger_task ON tool_ledger(task_id, created_at);
CREATE TABLE IF NOT EXISTS memory_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope TEXT NOT NULL,
    task_id TEXT,
    text TEXT NOT NULL,
    keywords TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""

TERMINAL = frozenset({"completed", "failed", "cancelled"})


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    data = dict(row)
    for key in ("options", "result", "data", "state", "prepared"):
        if isinstance(data.get(key), str):
            with suppress(json.JSONDecodeError):
                data[key] = json.loads(data[key])
    return data


class Store:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(
            str(path), check_same_thread=False, isolation_level=None
        )
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.execute("PRAGMA busy_timeout=5000")
        version = self._db.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise RuntimeError(f"Database schema {version} is newer than supported")
        self._db.executescript(SCHEMA)
        self._db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def close(self) -> None:
        self._db.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield self._db
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            self._db.execute("COMMIT")

    def _one(self, sql: str, args: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        with self._lock:
            return _row(self._db.execute(sql, args).fetchone())

    def _all(self, sql: str, args: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [_row(r) for r in self._db.execute(sql, args).fetchall()]  # type: ignore[misc]

    # ---- 任务 ----
    def create_task(
        self,
        task_id: str,
        goal: str,
        options: dict[str, Any],
        parent_id: str | None = None,
    ) -> None:
        stamp = now()
        with self.tx() as db:
            db.execute(
                "INSERT INTO tasks (id, parent_id, goal, status, options, created_at, "
                "updated_at) VALUES (?, ?, ?, 'pending', ?, ?, ?)",
                (task_id, parent_id, goal, _dump(options), stamp, stamp),
            )

    def task(self, task_id: str) -> dict[str, Any] | None:
        return self._one("SELECT * FROM tasks WHERE id = ?", (task_id,))

    def tasks(
        self, limit: int = 20, parent_id: str | None = None
    ) -> list[dict[str, Any]]:
        if parent_id is not None:
            return self._all(
                "SELECT * FROM tasks WHERE parent_id = ? ORDER BY created_at",
                (parent_id,),
            )
        return self._all(
            "SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?", (limit,)
        )

    def set_task(
        self,
        task_id: str,
        status: str,
        *,
        result: Any = None,
        error: str | None = None,
    ) -> None:
        with self.tx() as db:
            db.execute(
                "UPDATE tasks SET status = ?, result = ?, error = ?, updated_at = ? "
                "WHERE id = ?",
                (
                    status,
                    None if result is None else _dump(result),
                    error,
                    now(),
                    task_id,
                ),
            )

    def request_cancel(self, task_id: str) -> bool:
        """标记取消；跨进程生效，运行中的循环在步骤边界和工具执行中检查。"""

        with self.tx() as db:
            cursor = db.execute(
                "UPDATE tasks SET cancel_requested = 1, updated_at = ? "
                "WHERE (id = ? OR parent_id = ?) AND status NOT IN "
                "('completed', 'failed', 'cancelled')",
                (now(), task_id, task_id),
            )
            return cursor.rowcount > 0

    def cancel_requested(self, task_id: str) -> bool:
        row = self._one("SELECT cancel_requested FROM tasks WHERE id = ?", (task_id,))
        return bool(row and row["cancel_requested"])

    # ---- 步骤 ----
    def start_step(self, step_id: str, task_id: str, seq: int, kind: str) -> None:
        with self.tx() as db:
            db.execute(
                "INSERT OR REPLACE INTO steps (id, task_id, seq, kind, status, started_at)"
                " VALUES (?, ?, ?, ?, 'running', ?)",
                (step_id, task_id, seq, kind, now()),
            )

    def finish_step(self, step_id: str, status: str) -> None:
        with self.tx() as db:
            db.execute(
                "UPDATE steps SET status = ?, finished_at = ? WHERE id = ?",
                (status, now(), step_id),
            )

    def steps(self, task_id: str) -> list[dict[str, Any]]:
        return self._all(
            "SELECT * FROM steps WHERE task_id = ? ORDER BY seq", (task_id,)
        )

    # ---- 事件 ----
    def add_event(
        self,
        task_id: str,
        kind: str,
        data: dict[str, Any],
        *,
        step_id: str | None = None,
        parent_id: int | None = None,
        input_sha: str | None = None,
        output_sha: str | None = None,
        artifact_path: str | None = None,
        artifact_sha: str | None = None,
    ) -> int:
        with self.tx() as db:
            cursor = db.execute(
                "INSERT INTO events (task_id, step_id, parent_id, kind, data, input_sha, "
                "output_sha, artifact_path, artifact_sha, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    task_id,
                    step_id,
                    parent_id,
                    kind,
                    _dump(data),
                    input_sha,
                    output_sha,
                    artifact_path,
                    artifact_sha,
                    now(),
                ),
            )
            return int(cursor.lastrowid)

    def events(self, task_id: str, *, kind: str | None = None) -> list[dict[str, Any]]:
        if kind:
            return self._all(
                "SELECT * FROM events WHERE task_id = ? AND kind = ? ORDER BY id",
                (task_id, kind),
            )
        return self._all(
            "SELECT * FROM events WHERE task_id = ? ORDER BY id", (task_id,)
        )

    def event(self, event_id: int) -> dict[str, Any] | None:
        return self._one("SELECT * FROM events WHERE id = ?", (event_id,))

    def artifact_events(
        self, *, path: str | None = None, sha: str | None = None
    ) -> list[dict[str, Any]]:
        if sha:
            return self._all(
                "SELECT * FROM events WHERE artifact_sha = ? ORDER BY id DESC", (sha,)
            )
        return self._all(
            "SELECT * FROM events WHERE artifact_path = ? ORDER BY id DESC", (path,)
        )

    # ---- 检查点 ----
    def save_checkpoint(
        self, task_id: str, step_seq: int, state: dict[str, Any], event_id: int | None
    ) -> int:
        with self.tx() as db:
            cursor = db.execute(
                "INSERT INTO checkpoints (task_id, step_seq, state, event_id, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (task_id, step_seq, _dump(state), event_id, now()),
            )
            return int(cursor.lastrowid)

    def latest_checkpoint(self, task_id: str) -> dict[str, Any] | None:
        return self._one(
            "SELECT * FROM checkpoints WHERE task_id = ? ORDER BY id DESC LIMIT 1",
            (task_id,),
        )

    # ---- 工具账本 ----
    def ledger(self, key: str) -> dict[str, Any] | None:
        return self._one("SELECT * FROM tool_ledger WHERE key = ?", (key,))

    def ledger_for_task(self, task_id: str) -> list[dict[str, Any]]:
        return self._all(
            "SELECT * FROM tool_ledger WHERE task_id = ? ORDER BY created_at, rowid",
            (task_id,),
        )

    def ledger_open(
        self,
        key: str,
        task_id: str,
        step_id: str,
        call_id: str,
        tool: str,
        args_sha: str,
    ) -> None:
        stamp = now()
        with self.tx() as db:
            db.execute(
                "INSERT INTO tool_ledger (key, task_id, step_id, call_id, tool, args_sha,"
                " status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)"
                " ON CONFLICT(key) DO UPDATE SET attempts = attempts, updated_at = ?",
                (key, task_id, step_id, call_id, tool, args_sha, stamp, stamp, stamp),
            )

    def ledger_update(self, key: str, **fields: Any) -> None:
        allowed = {"status", "prepared", "result", "error", "attempts"}
        if not fields or set(fields) - allowed:
            raise ValueError(f"Invalid ledger fields: {sorted(fields)}")
        values = [
            _dump(v) if k in {"prepared", "result"} and v is not None else v
            for k, v in fields.items()
        ]
        assignments = ", ".join(f"{k} = ?" for k in fields)
        with self.tx() as db:
            db.execute(
                f"UPDATE tool_ledger SET {assignments}, updated_at = ? WHERE key = ?",
                (*values, now(), key),
            )

    # ---- 记忆 ----
    def add_note(
        self, scope: str, task_id: str | None, text: str, keywords: str
    ) -> None:
        with self.tx() as db:
            db.execute(
                "INSERT INTO memory_notes (scope, task_id, text, keywords, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (scope, task_id, text, keywords, now()),
            )

    def notes(self, scope: str, limit: int = 200) -> list[dict[str, Any]]:
        return self._all(
            "SELECT * FROM memory_notes WHERE scope = ? ORDER BY id DESC LIMIT ?",
            (scope, limit),
        )


__all__ = ["SCHEMA_VERSION", "TERMINAL", "Store", "now"]
