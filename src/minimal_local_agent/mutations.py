"""Validated, reversible workspace mutation transactions."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from minimal_local_agent.store import AuditStore
from minimal_local_agent.workspace import WorkspaceTools, WritePreview


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class EditRequest(BaseModel):
    """One exact replacement inside an existing UTF-8 file."""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1)
    old_text: str = Field(min_length=1)
    new_text: str
    expected_sha256: str | None = Field(default=None, pattern=r"^[0-9a-fA-F]{64}$")


@dataclass(frozen=True, slots=True)
class PreparedFileChange:
    path: str
    before: str | None
    after: str
    before_sha256: str | None
    after_sha256: str
    preview: WritePreview

    @property
    def existed(self) -> bool:
        return self.before is not None


@dataclass(frozen=True, slots=True)
class PreparedTransaction:
    changes: tuple[PreparedFileChange, ...]

    def approval_detail(self) -> str:
        sections = [f"Files: {len(self.changes)}"]
        for index, change in enumerate(self.changes, start=1):
            sections.append(
                f"\n[{index}/{len(self.changes)}] {change.path}\n{change.preview.diff}"
            )
        return "\n".join(sections)

    def audit_details(self) -> dict[str, Any]:
        rendered = self.approval_detail()
        return {
            "file_count": len(self.changes),
            "paths": [change.path for change in self.changes],
            "after_bytes": sum(change.preview.bytes for change in self.changes),
            "diff_sha256": hashlib.sha256(rendered.encode("utf-8")).hexdigest(),
            "changes": [
                {
                    "path": change.path,
                    "existed": change.existed,
                    "before_sha256": change.before_sha256,
                    "after_sha256": change.after_sha256,
                }
                for change in self.changes
            ],
        }


@dataclass(frozen=True, slots=True)
class PreparedUndoChange:
    path: str
    current: str
    restore: str | None
    expected_sha256: str
    preview: WritePreview


@dataclass(frozen=True, slots=True)
class PreparedUndo:
    change_set_id: str
    session_id: str
    changes: tuple[PreparedUndoChange, ...]

    def approval_detail(self) -> str:
        sections = [
            f"Undo change set: {self.change_set_id}",
            f"Files: {len(self.changes)}",
        ]
        for index, change in enumerate(self.changes, start=1):
            sections.append(
                f"\n[{index}/{len(self.changes)}] {change.path}\n{change.preview.diff}"
            )
        return "\n".join(sections)


class MutationEngine:
    """Stages and commits file changes with stale-write checks and rollback."""

    def __init__(
        self,
        workspace: WorkspaceTools,
        store: AuditStore,
        *,
        max_files: int = 8,
        max_diff_chars: int = 40_000,
    ) -> None:
        if max_files < 1 or max_diff_chars < 1:
            raise ValueError("Mutation limits must be positive")
        self.workspace = workspace
        self.store = store
        self.max_files = max_files
        self.max_diff_chars = max_diff_chars

    def _check_transaction(self, changes: list[PreparedFileChange]) -> None:
        if not changes:
            raise ValueError("A transaction must contain at least one change")
        if len(changes) > self.max_files:
            raise ValueError(
                f"Transaction has {len(changes)} files; limit is {self.max_files}"
            )
        paths = [change.path for change in changes]
        if len(paths) != len(set(paths)):
            raise ValueError("A transaction cannot change the same file twice")
        detail = PreparedTransaction(tuple(changes)).approval_detail()
        if len(detail) > self.max_diff_chars:
            raise ValueError(
                f"Combined diff is {len(detail)} characters; approval limit is "
                f"{self.max_diff_chars}"
            )
        if any(change.preview.diff_truncated for change in changes):
            raise ValueError("A diff was truncated; refusing an incomplete approval")

    def prepare_write(
        self,
        path: str,
        content: str,
        *,
        overwrite: bool = False,
    ) -> PreparedTransaction:
        preview = self.workspace.preview_write(
            path,
            content,
            overwrite=overwrite,
            max_diff_chars=self.max_diff_chars,
        )
        before = self.workspace.read_file(path) if preview.existed else None
        if before == content:
            raise ValueError(f"Write for {preview.path} does not change the file")
        change = PreparedFileChange(
            path=preview.path,
            before=before,
            after=content,
            before_sha256=None if before is None else sha256_text(before),
            after_sha256=sha256_text(content),
            preview=preview,
        )
        changes = [change]
        self._check_transaction(changes)
        return PreparedTransaction(tuple(changes))

    def prepare_edits(self, edits: list[EditRequest]) -> PreparedTransaction:
        if len(edits) > self.max_files:
            raise ValueError(
                f"Transaction has {len(edits)} files; limit is {self.max_files}"
            )
        changes: list[PreparedFileChange] = []
        for edit in edits:
            if edit.old_text == edit.new_text:
                raise ValueError(f"Edit for {edit.path} does not change the file")
            before = self.workspace.read_file(edit.path)
            before_sha256 = sha256_text(before)
            if (
                edit.expected_sha256
                and edit.expected_sha256.casefold() != before_sha256
            ):
                raise RuntimeError(f"Stale file hash for {edit.path}")
            occurrences = before.count(edit.old_text)
            if occurrences != 1:
                raise ValueError(
                    f"old_text must occur exactly once in {edit.path}; "
                    f"found {occurrences}"
                )
            after = before.replace(edit.old_text, edit.new_text, 1)
            preview = self.workspace.preview_write(
                edit.path,
                after,
                overwrite=True,
                max_diff_chars=self.max_diff_chars,
            )
            changes.append(
                PreparedFileChange(
                    path=preview.path,
                    before=before,
                    after=after,
                    before_sha256=before_sha256,
                    after_sha256=sha256_text(after),
                    preview=preview,
                )
            )
        self._check_transaction(changes)
        return PreparedTransaction(tuple(changes))

    def _validate(self, transaction: PreparedTransaction) -> None:
        for change in transaction.changes:
            target = self.workspace.guard.resolve(change.path)
            if change.before is None:
                if target.exists():
                    raise RuntimeError(f"Target appeared after preview: {change.path}")
                continue
            current = self.workspace.read_file(change.path)
            if sha256_text(current) != change.before_sha256:
                raise RuntimeError(f"Target changed after preview: {change.path}")

    def _rollback(self, changes: list[PreparedFileChange]) -> None:
        errors: list[str] = []
        for change in reversed(changes):
            try:
                self.workspace.restore_file(change.path, change.before)
            except Exception as exc:  # Best-effort rollback reports every failure.
                errors.append(f"{change.path}: {exc}")
        if errors:
            raise RuntimeError(f"Rollback failed: {'; '.join(errors)}")

    def commit(
        self,
        transaction: PreparedTransaction,
        *,
        session_id: str,
        kind: str,
    ) -> str:
        self._validate(transaction)
        applied: list[PreparedFileChange] = []
        try:
            for change in transaction.changes:
                self.workspace.write_file(
                    change.path,
                    change.after,
                    overwrite=change.existed,
                )
                applied.append(change)
            change_set_id = self.store.record_change_set(
                session_id,
                kind,
                [
                    {
                        "path": change.path,
                        "existed": change.existed,
                        "before_text": change.before,
                        "before_sha256": change.before_sha256,
                        "after_sha256": change.after_sha256,
                        "after_bytes": change.preview.bytes,
                    }
                    for change in transaction.changes
                ],
            )
        except Exception:
            self._rollback(applied)
            raise
        return change_set_id

    def prepare_undo(self, change_set_id: str) -> PreparedUndo:
        stored = self.store.get_change_set(change_set_id)
        if stored is None:
            raise ValueError(f"Unknown change set: {change_set_id}")
        if stored["status"] != "applied":
            raise ValueError(f"Change set is already {stored['status']}")

        changes: list[PreparedUndoChange] = []
        for change in stored["changes"]:
            path = str(change["path"])
            current = self.workspace.read_file(path)
            expected_sha256 = str(change["after_sha256"])
            if sha256_text(current) != expected_sha256:
                raise RuntimeError(f"Cannot undo modified file: {path}")
            restore = change["before_text"] if change["existed"] else None
            preview = (
                self.workspace.preview_delete(path, max_diff_chars=self.max_diff_chars)
                if restore is None
                else self.workspace.preview_write(
                    path,
                    str(restore),
                    overwrite=True,
                    max_diff_chars=self.max_diff_chars,
                )
            )
            if preview.diff_truncated:
                raise ValueError(
                    "Undo diff was truncated; refusing incomplete approval"
                )
            changes.append(
                PreparedUndoChange(
                    path=path,
                    current=current,
                    restore=None if restore is None else str(restore),
                    expected_sha256=expected_sha256,
                    preview=preview,
                )
            )
        plan = PreparedUndo(
            change_set_id=change_set_id,
            session_id=str(stored["session_id"]),
            changes=tuple(changes),
        )
        if len(plan.approval_detail()) > self.max_diff_chars:
            raise ValueError("Combined undo diff exceeds the approval limit")
        return plan

    def undo(self, plan: PreparedUndo) -> None:
        for change in plan.changes:
            current = self.workspace.read_file(change.path)
            if sha256_text(current) != change.expected_sha256:
                raise RuntimeError(f"Target changed after undo preview: {change.path}")

        restored: list[PreparedUndoChange] = []
        try:
            for change in reversed(plan.changes):
                self.workspace.restore_file(change.path, change.restore)
                restored.append(change)
            self.store.mark_change_set_undone(plan.change_set_id)
            self.store.record_tool_event(
                plan.session_id,
                "undo_change_set",
                "ok",
                {
                    "change_set_id": plan.change_set_id,
                    "paths": [change.path for change in plan.changes],
                },
            )
        except Exception:
            for change in reversed(restored):
                self.workspace.write_file(
                    change.path,
                    change.current,
                    overwrite=self.workspace.guard.resolve(change.path).exists(),
                )
            raise


__all__ = [
    "EditRequest",
    "MutationEngine",
    "PreparedFileChange",
    "PreparedTransaction",
    "PreparedUndo",
    "sha256_text",
]
