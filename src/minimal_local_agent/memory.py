"""Optional, explicitly approved knowledge memory; not conversation history.

Facts and structured entities share a small SQLite store. Forgetting excludes a
record from retrieval; it does not erase archived conversations or audit trails.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol
from uuid import uuid4

MemoryStatus = Literal["candidate", "approved", "rejected", "forgotten", "superseded"]
_STATUSES = {"candidate", "approved", "rejected", "forgotten", "superseded"}
_MAX_LIMIT = 100
_MAX_ATTRIBUTES = 32


class MemoryConflictError(ValueError):
    """A memory revision or confirmed entity changed before approval."""


def _text(value: str, name: str, maximum: int, *, empty: bool = False) -> str:
    if not isinstance(value, str) or "\x00" in value:
        raise ValueError(f"{name} must be a string without null characters")
    if len(value) > maximum or (not empty and not value.strip()):
        raise ValueError(
            f"{name} must contain {'0' if empty else '1'} to {maximum} characters"
        )
    return value


def _positive(value: int, name: str, maximum: int = 2_147_483_647) -> int:
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError(f"{name} must be an integer from 1 to {maximum}")
    return value


@dataclass(frozen=True, slots=True)
class MemoryRecord:
    id: str
    scope: str
    kind: Literal["fact", "entity"]
    content: str
    source: str
    entity_key: str | None
    attributes: dict[str, str]
    status: MemoryStatus
    revision: int
    created_at: str
    updated_at: str
    replaces_id: str | None = None
    base_revision: int | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class MemoryStore(Protocol):
    """Bounded, scoped records; no automatic extraction or implicit approval."""

    def propose(
        self,
        scope: str,
        content: str,
        *,
        source: str = "",
        entity_key: str | None = None,
        attributes: Mapping[str, str] | None = None,
    ) -> MemoryRecord: ...

    def propose_update(
        self,
        scope: str,
        record_id: str,
        *,
        content: str,
        source: str = "",
        attributes: Mapping[str, str] | None = None,
        expected_revision: int,
    ) -> MemoryRecord: ...

    def get(self, scope: str, record_id: str) -> MemoryRecord | None: ...

    def approve(
        self,
        scope: str,
        record_id: str,
        *,
        expected_revision: int,
    ) -> MemoryRecord: ...

    def reject(
        self,
        scope: str,
        record_id: str,
        *,
        expected_revision: int,
    ) -> MemoryRecord: ...

    def forget(
        self,
        scope: str,
        record_id: str,
        *,
        expected_revision: int,
    ) -> MemoryRecord: ...

    def search(
        self,
        scope: str,
        query: str,
        *,
        limit: int = 10,
    ) -> list[MemoryRecord]: ...

    def list_records(
        self,
        scope: str,
        *,
        status: MemoryStatus | None = None,
        limit: int = 50,
    ) -> list[MemoryRecord]: ...


class SQLiteMemoryStore:
    """Small keyword memory with compare-and-swap approval and entity updates.

    Entity keys are exact, scope-local identifiers, not automatically merged
    names. The prefixed tables can coexist with an audit database without using
    its schema version or private connection methods.
    """

    def __init__(
        self,
        path: Path | str,
        *,
        max_content_chars: int = 8_000,
        max_records: int = 10_000,
    ) -> None:
        self.max_content_chars = _positive(
            max_content_chars, "max_content_chars", 1_000_000
        )
        self.max_records = _positive(max_records, "max_records", 1_000_000)
        if not isinstance(path, (Path, str)) or not str(path).strip():
            raise ValueError("path must be a nonempty filesystem path")
        if str(path) == ":memory:":
            raise ValueError("Use a filesystem database path for persistent memory")
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection(write=True) as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS memory_meta "
                "(key TEXT PRIMARY KEY, value INTEGER NOT NULL)"
            )
            version = connection.execute(
                "SELECT value FROM memory_meta WHERE key = 'schema_version'"
            ).fetchone()
            if version is not None and version["value"] != 1:
                raise ValueError("Unsupported memory database schema")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS memory_records (
                    id TEXT PRIMARY KEY, scope TEXT NOT NULL, kind TEXT NOT NULL,
                    content TEXT NOT NULL, source TEXT NOT NULL, entity_key TEXT,
                    attributes_json TEXT NOT NULL, status TEXT NOT NULL,
                    revision INTEGER NOT NULL, created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL, search_text TEXT NOT NULL,
                    replaces_id TEXT REFERENCES memory_records(id),
                    base_revision INTEGER
                )
            """)
            connection.execute("""
                CREATE INDEX IF NOT EXISTS idx_memory_scope_status
                ON memory_records(scope, status, updated_at)
            """)
            connection.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_approved_entity
                ON memory_records(scope, entity_key)
                WHERE status = 'approved' AND entity_key IS NOT NULL
            """)
            connection.execute(
                "INSERT OR IGNORE INTO memory_meta(key, value) "
                "VALUES ('schema_version', 1)"
            )

    @contextmanager
    def _connection(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            if write:
                connection.execute("BEGIN IMMEDIATE")
            with connection:
                yield connection
        finally:
            connection.close()

    @staticmethod
    def _identity(scope: str, record_id: str | None = None) -> None:
        _text(scope, "scope", 240)
        if record_id is not None:
            _text(record_id, "record_id", 120)

    @staticmethod
    def _record(row: sqlite3.Row) -> MemoryRecord:
        value = dict(row)
        value.pop("search_text")
        value["attributes"] = json.loads(value.pop("attributes_json"))
        return MemoryRecord(**value)

    def _attributes(
        self,
        entity_key: str | None,
        attributes: Mapping[str, str] | None,
    ) -> dict[str, str]:
        if entity_key is not None:
            _text(entity_key, "entity_key", 240)
        if attributes is not None and not isinstance(attributes, Mapping):
            raise ValueError("attributes must be a string-to-string mapping")
        values = dict(attributes or {})
        if len(values) > _MAX_ATTRIBUTES:
            raise ValueError(f"attributes cannot exceed {_MAX_ATTRIBUTES} entries")
        if values and entity_key is None:
            raise ValueError("Only entity records can have attributes")
        for key, value in values.items():
            _text(key, "attribute key", 120)
            _text(value, "attribute value", 2_000, empty=True)
        if len(json.dumps(values, ensure_ascii=False)) > self.max_content_chars:
            raise ValueError("Serialized attributes exceed max_content_chars")
        return values

    def _insert(
        self,
        connection: sqlite3.Connection,
        scope: str,
        content: str,
        source: str,
        entity_key: str | None,
        attributes: dict[str, str],
        *,
        base: MemoryRecord | None = None,
    ) -> MemoryRecord:
        if (
            connection.execute("SELECT COUNT(*) FROM memory_records").fetchone()[0]
            >= self.max_records
        ):
            raise ValueError("Memory record capacity reached")
        now = datetime.now(UTC).isoformat(timespec="milliseconds")
        record = MemoryRecord(
            id=f"mem-{uuid4().hex}",
            scope=scope,
            kind="fact" if entity_key is None else "entity",
            content=content,
            source=source,
            entity_key=entity_key,
            attributes=attributes,
            status="candidate",
            revision=1,
            created_at=now,
            updated_at=now,
            replaces_id=None if base is None else base.id,
            base_revision=None if base is None else base.revision,
        )
        encoded_attributes = json.dumps(attributes, ensure_ascii=False, sort_keys=True)
        searchable = "\n".join(
            (content, entity_key or "", encoded_attributes)
        ).casefold()
        connection.execute(
            """
            INSERT INTO memory_records (
                id, scope, kind, content, source, entity_key, attributes_json,
                status, revision, created_at, updated_at, search_text,
                replaces_id, base_revision
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
            (
                record.id,
                scope,
                record.kind,
                content,
                source,
                entity_key,
                encoded_attributes,
                record.status,
                record.revision,
                now,
                now,
                searchable,
                record.replaces_id,
                record.base_revision,
            ),
        )
        return record

    def propose(
        self,
        scope: str,
        content: str,
        *,
        source: str = "",
        entity_key: str | None = None,
        attributes: Mapping[str, str] | None = None,
    ) -> MemoryRecord:
        self._identity(scope)
        _text(content, "content", self.max_content_chars)
        _text(source, "source", 2_000, empty=True)
        values = self._attributes(entity_key, attributes)
        with self._connection(write=True) as connection:
            return self._insert(connection, scope, content, source, entity_key, values)

    def get(self, scope: str, record_id: str) -> MemoryRecord | None:
        self._identity(scope, record_id)
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM memory_records WHERE scope = ? AND id = ?",
                (scope, record_id),
            ).fetchone()
        return None if row is None else self._record(row)

    def _checked(
        self,
        connection: sqlite3.Connection,
        scope: str,
        record_id: str,
        expected_revision: int,
    ) -> MemoryRecord:
        self._identity(scope, record_id)
        _positive(expected_revision, "expected_revision")
        row = connection.execute(
            "SELECT * FROM memory_records WHERE scope = ? AND id = ?",
            (scope, record_id),
        ).fetchone()
        if row is None:
            raise ValueError("Memory record not found in this scope")
        record = self._record(row)
        if record.revision != expected_revision:
            raise MemoryConflictError(
                "Memory revision changed; reload before modifying"
            )
        return record

    def propose_update(
        self,
        scope: str,
        record_id: str,
        *,
        content: str,
        source: str = "",
        attributes: Mapping[str, str] | None = None,
        expected_revision: int,
    ) -> MemoryRecord:
        self._identity(scope, record_id)
        _text(content, "content", self.max_content_chars)
        _text(source, "source", 2_000, empty=True)
        with self._connection(write=True) as connection:
            base = self._checked(connection, scope, record_id, expected_revision)
            if base.status != "approved":
                raise MemoryConflictError("Only an approved record can be updated")
            values = self._attributes(
                base.entity_key,
                base.attributes if attributes is None else attributes,
            )
            return self._insert(
                connection,
                scope,
                content,
                source or base.source,
                base.entity_key,
                values,
                base=base,
            )

    def _set_status(
        self,
        connection: sqlite3.Connection,
        record: MemoryRecord,
        status: MemoryStatus,
    ) -> MemoryRecord:
        connection.execute(
            "UPDATE memory_records SET status = ?, "
            "revision = revision + 1, updated_at = ? WHERE id = ?",
            (status, datetime.now(UTC).isoformat(timespec="milliseconds"), record.id),
        )
        return self._record(
            connection.execute(
                "SELECT * FROM memory_records WHERE id = ?",
                (record.id,),
            ).fetchone()
        )

    def approve(
        self,
        scope: str,
        record_id: str,
        *,
        expected_revision: int,
    ) -> MemoryRecord:
        with self._connection(write=True) as connection:
            candidate = self._checked(connection, scope, record_id, expected_revision)
            if candidate.status != "candidate":
                raise MemoryConflictError("Only a candidate can be approved")
            if candidate.replaces_id is not None:
                base = self._checked(
                    connection,
                    scope,
                    candidate.replaces_id,
                    candidate.base_revision,
                )
                if base.status != "approved":
                    raise MemoryConflictError("The approved base record changed")
                self._set_status(connection, base, "superseded")
            try:
                return self._set_status(connection, candidate, "approved")
            except sqlite3.IntegrityError as exc:
                raise MemoryConflictError(
                    "Entity already has an approved record; use propose_update"
                ) from exc

    def reject(
        self,
        scope: str,
        record_id: str,
        *,
        expected_revision: int,
    ) -> MemoryRecord:
        with self._connection(write=True) as connection:
            candidate = self._checked(connection, scope, record_id, expected_revision)
            if candidate.status != "candidate":
                raise MemoryConflictError("Only a candidate can be rejected")
            return self._set_status(connection, candidate, "rejected")

    def forget(
        self,
        scope: str,
        record_id: str,
        *,
        expected_revision: int,
    ) -> MemoryRecord:
        with self._connection(write=True) as connection:
            record = self._checked(connection, scope, record_id, expected_revision)
            if record.status == "forgotten":
                raise MemoryConflictError("Memory record is already forgotten")
            return self._set_status(connection, record, "forgotten")

    def search(self, scope: str, query: str, *, limit: int = 10) -> list[MemoryRecord]:
        self._identity(scope)
        _positive(limit, "limit", _MAX_LIMIT)
        _text(query, "query", 2_000)
        terms = list(dict.fromkeys(query.casefold().split()))
        if len(terms) > 16:
            raise ValueError("query cannot contain more than 16 search terms")
        predicates = " OR ".join("search_text LIKE ? ESCAPE '\\'" for _ in terms)
        values = [
            "%"
            + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            + "%"
            for term in terms
        ]
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM memory_records WHERE scope = ? "
                f"AND status = 'approved' AND ({predicates}) "
                "ORDER BY updated_at DESC, id DESC LIMIT ?",
                (scope, *values, limit),
            ).fetchall()
        return [self._record(row) for row in rows]

    def list_records(
        self,
        scope: str,
        *,
        status: MemoryStatus | None = None,
        limit: int = 50,
    ) -> list[MemoryRecord]:
        self._identity(scope)
        _positive(limit, "limit", _MAX_LIMIT)
        if status is not None and (
            not isinstance(status, str) or status not in _STATUSES
        ):
            raise ValueError("Unknown memory status")
        clause = "" if status is None else " AND status = ?"
        with self._connection() as connection:
            rows = connection.execute(
                f"SELECT * FROM memory_records WHERE scope = ?{clause} "
                "ORDER BY updated_at DESC, id DESC LIMIT ?",
                (scope, *((status,) if status is not None else ()), limit),
            ).fetchall()
        return [self._record(row) for row in rows]


__all__ = [
    "MemoryConflictError",
    "MemoryRecord",
    "MemoryStatus",
    "MemoryStore",
    "SQLiteMemoryStore",
]
