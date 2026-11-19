"""文件变更集：可序列化、可对账、可补偿的写操作（移植并改写自 v1 mutations）。

一个变更集记录每个文件的写前/写后内容与哈希。它会先写入工具账本，再落盘，
所以进程在任意时刻崩溃后，恢复逻辑都能仅凭账本判断文件处于写前、写后还是冲突状态。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from minimal_local_agent.tools.fs import Workspace, sha256_text, unified_diff

FileState = Literal["before", "after", "partial", "conflict"]


class FileConflictError(RuntimeError):
    """文件当前内容既不是写前也不是写后状态，说明被外部改动过。"""

    def __init__(self, paths: list[str]) -> None:
        self.paths = paths
        super().__init__(f"File state conflict: {', '.join(paths)}")


@dataclass(frozen=True, slots=True)
class FileChange:
    path: str
    before: str | None
    after: str
    before_sha: str | None
    after_sha: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "before": self.before,
            "after": self.after,
            "before_sha": self.before_sha,
            "after_sha": self.after_sha,
        }


@dataclass(frozen=True, slots=True)
class FileChangeSet:
    changes: tuple[FileChange, ...]

    @property
    def paths(self) -> list[str]:
        return [change.path for change in self.changes]

    def diff(self) -> str:
        return "\n".join(unified_diff(c.path, c.before, c.after) for c in self.changes)

    def to_dict(self) -> dict[str, Any]:
        return {"changes": [change.to_dict() for change in self.changes]}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FileChangeSet:
        return cls(tuple(FileChange(**item) for item in data["changes"]))

    def summary(self) -> dict[str, Any]:
        """写进事件与结果的精简信息（不含全文）。"""

        return {
            "paths": self.paths,
            "files": [
                {"path": c.path, "before_sha": c.before_sha, "after_sha": c.after_sha}
                for c in self.changes
            ],
        }


def _change(path: str, before: str | None, after: str) -> FileChange:
    return FileChange(
        path=path,
        before=before,
        after=after,
        before_sha=None if before is None else sha256_text(before),
        after_sha=sha256_text(after),
    )


def _check(changes: list[FileChange], max_files: int) -> FileChangeSet:
    if not changes:
        raise ValueError("A change set must contain at least one file")
    if len(changes) > max_files:
        raise ValueError(f"Change set has {len(changes)} files; limit is {max_files}")
    paths = [c.path for c in changes]
    if len(paths) != len(set(paths)):
        raise ValueError("A change set cannot change the same file twice")
    return FileChangeSet(tuple(changes))


def prepare_write(
    ws: Workspace, path: str, content: str, *, overwrite: bool, max_files: int = 8
) -> FileChangeSet:
    target = ws.guard.resolve(path)
    relative = ws.guard.relative(target)
    before = ws.read_optional(relative)
    if before is not None and not overwrite:
        raise FileExistsError(
            f"File already exists: {relative}. Set overwrite=true only when "
            "replacement is intended."
        )
    if len(content.encode("utf-8")) > ws.max_file_bytes:
        raise ValueError(f"Content exceeds {ws.max_file_bytes} bytes")
    if before == content:
        raise ValueError(f"Write for {relative} does not change the file")
    return _check([_change(relative, before, content)], max_files)


def prepare_edits(
    ws: Workspace, edits: list[dict[str, Any]], *, max_files: int = 8
) -> FileChangeSet:
    """每条 edit 为 {path, old_text, new_text}，old_text 必须在文件当前内容中恰好出现一次。

    同一文件的多条 edit 按顺序依次应用，合并成该文件的一个变更。
    """

    originals: dict[str, str] = {}
    current: dict[str, str] = {}
    for edit in edits:
        path, old, new = edit["path"], edit["old_text"], edit["new_text"]
        if not old:
            raise ValueError("old_text cannot be empty")
        if old == new:
            raise ValueError(f"Edit for {path} does not change the file")
        relative = ws.guard.relative(ws.guard.resolve(path))
        if relative not in current:
            originals[relative] = current[relative] = ws.read_file(relative)
        text = current[relative]
        count = text.count(old)
        if count != 1:
            raise ValueError(
                f"old_text must occur exactly once in {relative}; found {count}"
            )
        current[relative] = text.replace(old, new, 1)
    changes = [_change(path, originals[path], after) for path, after in current.items()]
    return _check(changes, max_files)


def _state(ws: Workspace, change: FileChange) -> Literal["before", "after", "conflict"]:
    current = ws.read_optional(change.path)
    current_sha = None if current is None else sha256_text(current)
    if current_sha == change.after_sha:
        return "after"
    if current_sha == change.before_sha:
        return "before"
    return "conflict"


def probe(ws: Workspace, change_set: FileChangeSet) -> FileState:
    """对账：判断变更集整体处于哪种状态。"""

    states = {_state(ws, change) for change in change_set.changes}
    if "conflict" in states:
        return "conflict"
    if states == {"after"}:
        return "after"
    if states == {"before"}:
        return "before"
    return "partial"


def conflicts(ws: Workspace, change_set: FileChangeSet) -> list[str]:
    return [c.path for c in change_set.changes if _state(ws, c) == "conflict"]


def apply(ws: Workspace, change_set: FileChangeSet) -> None:
    """把仍处于写前状态的文件写成写后状态；写到一半出错则回滚本次已写部分。"""

    bad = conflicts(ws, change_set)
    if bad:
        raise FileConflictError(bad)
    applied: list[FileChange] = []
    try:
        for change in change_set.changes:
            if _state(ws, change) == "after":
                continue
            ws.write_file(change.path, change.after)
            applied.append(change)
    except Exception:
        for change in reversed(applied):
            ws.restore_file(change.path, change.before)
        raise


def compensate(ws: Workspace, change_set: FileChangeSet) -> None:
    """补偿：把写后状态的文件恢复成写前快照；被外部改过的文件拒绝覆盖。"""

    bad = conflicts(ws, change_set)
    if bad:
        raise FileConflictError(bad)
    for change in reversed(change_set.changes):
        if _state(ws, change) == "after":
            ws.restore_file(change.path, change.before)


__all__ = [
    "FileChange",
    "FileChangeSet",
    "FileConflictError",
    "FileState",
    "apply",
    "compensate",
    "conflicts",
    "prepare_edits",
    "prepare_write",
    "probe",
]
